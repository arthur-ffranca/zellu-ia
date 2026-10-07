# -*- coding: utf-8 -*-
"""
GuideRAG — RAG dedicado ao GuideAgent (assistente standalone).

Por que namespace separado?
    O analyst_agent (native runtime) consome RAGService.search() que olha o
    namespace default do Pinecone (onde vivem os docs juridicos de
    data/documents/ - CDC, CC, etc.). Se o material do guia (FAQ,
    fluxos, glossario) for misturado nesse mesmo namespace, ha
    contaminacao cruzada: o analyst pode resgatar trechos do guia
    quando analisar um caso, e o GuideAgent pode resgatar lei seca
    quando deveria explicar "como abrir um chamado". Por isso,
    namespace dedicado: 'zellu_guide'.

Compatibilidade com o GuideAgent:
    expoe `async search(query, top_k) -> List[Dict]` no mesmo formato
    que o GuideAgent ja espera de RAGService.search.

Ingestao:
    index_markdown_file(path, title) / index_markdown_text(text, source, title)
    fazem split (TextChunker, mesmos chunk_size/overlap
    do RAGService) + embedding (OPENAI_EMBEDDING_MODEL) + upsert no
    namespace dedicado. delete_source(source) limpa antes de reindexar.
"""

import asyncio
import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.services.embedding_cache import CachedOpenAIEmbeddings as OpenAIEmbeddings
from src.services.text_chunks import TextChunker
from pinecone import Pinecone

from config import get_settings


GUIDE_NAMESPACE = "zellu_guide"


class GuideRAG:
    """RAG isolado para o GuideAgent. Namespace separado, mesma infra."""

    def __init__(self, namespace: str = GUIDE_NAMESPACE):
        settings = get_settings()
        self.namespace = namespace
        self.embeddings = OpenAIEmbeddings(
            api_key=settings.OPENAI_API_KEY,
            model=settings.OPENAI_EMBEDDING_MODEL,
        )
        self._pc = Pinecone(api_key=settings.PINECONE_API_KEY)
        self._index_name = settings.PINECONE_INDEX_NAME
        try:
            self.index = self._pc.Index(self._index_name)
        except Exception as exc:
            print(f"[GUIDE-RAG] Falha ao conectar no index '{self._index_name}': {exc}")
            self.index = None

        self.splitter = TextChunker(
            chunk_size=settings.RAG_CHUNK_SIZE,
            chunk_overlap=settings.RAG_CHUNK_OVERLAP,
            separators=["\n\n", "\n", ". ", " ", ""],
        )

    # ------------------------------------------------------------------
    # API esperada pelo GuideAgent
    # ------------------------------------------------------------------

    async def search(
        self,
        query: str,
        top_k: int = 4,
        audience: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Busca semantica no namespace do guide.

        Args:
            query: pergunta do usuario.
            top_k: numero de chunks a recuperar.
            audience: filtro por persona (visitor|client|lawyer|company).
                Quando definido, restringe a busca a chunks com esse
                metadata. None = sem filtro (admin/debug).

        Retorna lista de { content, metadata, score } - mesmo shape do
        RAGService.search, para o GuideAgent consumir sem mudancas.
        """
        if not self.index or not query or not query.strip():
            return []
        try:
            # OpenAIEmbeddings.embed_query e sincrono; rodamos em thread para
            # nao bloquear o event loop.
            qvec = await asyncio.to_thread(self.embeddings.embed_query, query)
            # Filtro Pinecone por audience garante isolamento entre personas:
            # uma pergunta do advogado NUNCA retorna chunk do material da
            # empresa, etc. (defesa primaria; o prompt e a defesa secundaria).
            pinecone_filter = {"audience": {"$eq": audience}} if audience else None
            results = await asyncio.to_thread(
                self.index.query,
                vector=qvec,
                top_k=top_k,
                include_metadata=True,
                namespace=self.namespace,
                filter=pinecone_filter,
            )
            docs: List[Dict[str, Any]] = []
            for match in results.get("matches", []) if isinstance(results, dict) else getattr(results, "matches", []):
                if isinstance(match, dict):
                    meta = match.get("metadata") or {}
                    score = match.get("score", 0.0)
                else:
                    meta = getattr(match, "metadata", {}) or {}
                    score = getattr(match, "score", 0.0)
                docs.append(
                    {
                        "content": meta.get("text", ""),
                        "metadata": meta,
                        "score": float(score) if score is not None else 0.0,
                    }
                )
            return docs
        except Exception as exc:
            print(f"[GUIDE-RAG] Busca falhou: {exc}")
            return []

    # ------------------------------------------------------------------
    # Ingestao (chamada por scripts ou pipeline futuro)
    # ------------------------------------------------------------------

    def index_markdown_text(
        self,
        text: str,
        source: str,
        title: Optional[str] = None,
        audience: Optional[str] = None,
        extra_metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Indexa texto markdown no namespace do guide.

        Args:
            text: conteudo markdown bruto.
            source: identificador estavel do doc (geralmente nome do arquivo).
            title: titulo amigavel para exibicao como fonte na UI.
            audience: persona-alvo do conteudo (visitor|client|lawyer|company).
                Gravado no metadata de cada chunk para que search() consiga
                isolar a recuperacao por perfil. None = chunk sem tag (admin
                ou conteudo geral que serve todo mundo).
            extra_metadata: campos aplicados a TODOS os chunks (ex.: versao).
        """
        if not self.index:
            return {"success": False, "error": "Index Pinecone nao disponivel"}
        if not text or not text.strip():
            return {"success": False, "error": "Conteudo vazio"}

        chunks = self.splitter.split_text(text)
        if not chunks:
            return {"success": False, "error": "Nenhum chunk gerado"}

        try:
            vectors = []
            for i, chunk in enumerate(chunks):
                embedding = self.embeddings.embed_query(chunk)
                meta = {
                    "text": chunk,
                    "source": source,
                    "title": title or source,
                    "chunk_index": i,
                    "total_chunks": len(chunks),
                }
                if audience:
                    meta["audience"] = audience
                if extra_metadata:
                    meta.update(extra_metadata)
                vectors.append(
                    {
                        "id": self._vector_id(source, i),
                        "values": embedding,
                        "metadata": meta,
                    }
                )

            for batch_start in range(0, len(vectors), 100):
                batch = vectors[batch_start : batch_start + 100]
                self.index.upsert(vectors=batch, namespace=self.namespace)

            print(
                f"[GUIDE-RAG] Indexado '{source}': {len(chunks)} chunks "
                f"-> namespace='{self.namespace}'"
            )
            return {
                "success": True,
                "chunk_count": len(chunks),
                "source": source,
                "namespace": self.namespace,
            }
        except Exception as exc:
            print(f"[GUIDE-RAG] Falha ao indexar '{source}': {exc}")
            return {"success": False, "error": str(exc)}

    def index_markdown_file(
        self,
        path,
        title: Optional[str] = None,
        audience: Optional[str] = None,
        extra_metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Le um .md do disco e indexa. source = nome do arquivo."""
        path = Path(path)
        if not path.exists():
            return {"success": False, "error": f"Arquivo nao encontrado: {path}"}
        text = path.read_text(encoding="utf-8")
        return self.index_markdown_text(
            text=text,
            source=path.name,
            title=title or path.stem.replace("-", " ").replace("_", " ").title(),
            audience=audience,
            extra_metadata=extra_metadata,
        )

    def delete_source(self, source: str) -> Dict[str, Any]:
        """Remove todos os chunks de um source do namespace do guide.

        Usado antes de reindexar para evitar chunks orfaos (caso o numero
        de chunks diminua na nova versao do doc).
        """
        if not self.index:
            return {"success": False, "error": "Index Pinecone nao disponivel"}
        try:
            self.index.delete(
                filter={"source": {"$eq": source}},
                namespace=self.namespace,
            )
            print(f"[GUIDE-RAG] Chunks do source '{source}' removidos do namespace '{self.namespace}'")
            return {"success": True}
        except Exception as exc:
            print(f"[GUIDE-RAG] Falha ao deletar source '{source}': {exc}")
            return {"success": False, "error": str(exc)}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _vector_id(source: str, idx: int) -> str:
        """ID determinstico - reindexar sobrescreve em vez de duplicar."""
        digest = hashlib.md5(source.encode("utf-8")).hexdigest()[:12]
        return f"guide_{digest}_{idx}"
