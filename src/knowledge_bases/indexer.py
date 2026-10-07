# -*- coding: utf-8 -*-
"""
Knowledge Base Indexer - Zellu IA Empresa

Responsavel por indexar documentos nas bases de conhecimento do Pinecone.
Usado para:
1. Popular bases compartilhadas (jurisprudencia, legislacao)
2. Indexar documentos de empresas

Como usar:
    # Indexar jurisprudencia
    >>> from src.knowledge_bases.indexer import knowledge_base_indexer
    >>> knowledge_base_indexer.index_jurisprudencia(doc)

    # Popular bases iniciais
    >>> knowledge_base_indexer.populate_initial_jurisprudencia()
    >>> knowledge_base_indexer.populate_initial_legislacao()
"""

from typing import List, Dict, Any, Optional
from datetime import datetime
from pathlib import Path
import logging
import uuid
import json

from src.services.embedding_service import embedding_service
from src.services.pinecone_service import pinecone_service
from .schemas import (
    JurisprudenciaDocument,
    LegislacaoDocument,
    CompanyDocument,
    TribunalType,
    DecisionType,
    ConsumerRightCategory,
    LegislationType,
)

logger = logging.getLogger(__name__)


# =============================================================================
# CONSTANTES
# =============================================================================

GLOBAL_NAMESPACE = "global"
COMPANY_NAMESPACE_PREFIX = "company_"
DATA_DIR = Path(__file__).parent / "data"


# =============================================================================
# INDEXADOR
# =============================================================================

class KnowledgeBaseIndexer:
    """
    Indexador de documentos para as bases de conhecimento.

    Responsabilidades:
    - Gerar embeddings dos documentos
    - Inserir no Pinecone com metadata correta
    - Popular bases compartilhadas com dados iniciais
    """

    def __init__(self):
        """Inicializa o indexador."""
        self.embedding_service = embedding_service
        self.pinecone_service = pinecone_service

    def _generate_id(self, prefix: str) -> str:
        """Gera ID unico para documento."""
        return f"{prefix}_{uuid.uuid4().hex[:12]}"

    def _load_json_data(self, filename: str) -> List[Dict]:
        """Carrega dados de arquivo JSON."""
        filepath = DATA_DIR / filename
        if not filepath.exists():
            logger.warning(f"Arquivo nao encontrado: {filepath}")
            return []

        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)

    def index_document(
        self,
        content: str,
        metadata: Dict[str, Any],
        namespace: str,
        doc_id: Optional[str] = None,
    ) -> str:
        """
        Indexa um documento generico no Pinecone.

        Args:
            content: Texto para gerar embedding
            metadata: Metadata do documento
            namespace: Namespace no Pinecone
            doc_id: ID do documento (gera automatico se nao informado)

        Returns:
            ID do documento indexado
        """
        try:
            if not doc_id:
                doc_id = self._generate_id("doc")

            embedding = self.embedding_service.generate_embedding(content)

            vector = {
                "id": doc_id,
                "values": embedding,
                "metadata": {
                    **metadata,
                    "content": content[:1000],
                },
            }

            self.pinecone_service.upsert_vectors([vector], namespace)

            logger.info(f"Documento {doc_id} indexado em {namespace}")
            return doc_id

        except Exception as e:
            logger.error(f"Erro ao indexar documento: {e}")
            raise

    def index_jurisprudencia(self, doc: JurisprudenciaDocument) -> str:
        """Indexa um documento de jurisprudencia."""
        content_for_embedding = f"""
        {doc.title}
        {doc.content}
        Tese: {doc.tese_juridica or ''}
        Categoria: {doc.categoria.value}
        Tribunal: {doc.tribunal.value}
        """

        return self.index_document(
            content=content_for_embedding,
            metadata=doc.to_pinecone_metadata(),
            namespace=GLOBAL_NAMESPACE,
            doc_id=doc.id,
        )

    def index_legislacao(self, doc: LegislacaoDocument) -> str:
        """Indexa um documento de legislacao."""
        content_for_embedding = f"""
        {doc.title}
        {doc.content}
        Palavras-chave: {', '.join(doc.palavras_chave)}
        """

        return self.index_document(
            content=content_for_embedding,
            metadata=doc.to_pinecone_metadata(),
            namespace=GLOBAL_NAMESPACE,
            doc_id=doc.id,
        )

    def index_company_document(self, doc: CompanyDocument) -> str:
        """Indexa um documento de empresa."""
        namespace = f"{COMPANY_NAMESPACE_PREFIX}{doc.company_id}"

        return self.index_document(
            content=doc.content,
            metadata=doc.to_pinecone_metadata(),
            namespace=namespace,
            doc_id=doc.id,
        )

    def populate_initial_jurisprudencia(self) -> int:
        """
        Popula a base de jurisprudencia com dados do arquivo JSON.

        Returns:
            Quantidade de documentos indexados
        """
        data = self._load_json_data("seed_jurisprudencia.json")
        count = 0

        for item in data:
            try:
                doc = JurisprudenciaDocument(
                    id=item["id"],
                    knowledge_base="jurisprudencia",
                    title=item["title"],
                    content=item["content"],
                    tribunal=TribunalType(item["tribunal"]),
                    numero_processo=item["numero_processo"],
                    data_julgamento=datetime.fromisoformat(item["data_julgamento"]),
                    relator=item.get("relator"),
                    categoria=ConsumerRightCategory(item["categoria"]),
                    decisao=DecisionType(item["decisao"]),
                    valor_causa=item.get("valor_causa"),
                    valor_indenizacao=item.get("valor_indenizacao"),
                    valor_danos_morais=item.get("valor_danos_morais"),
                    tese_juridica=item.get("tese_juridica"),
                )
                self.index_jurisprudencia(doc)
                count += 1
            except Exception as e:
                logger.error(f"Erro ao indexar {item.get('id')}: {e}")

        logger.info(f"Jurisprudencia: {count} documentos indexados")
        return count

    def populate_initial_legislacao(self) -> int:
        """
        Popula a base de legislacao com dados do arquivo JSON.

        Returns:
            Quantidade de documentos indexados
        """
        data = self._load_json_data("seed_legislacao.json")
        count = 0

        for item in data:
            try:
                categoria = None
                if item.get("categoria"):
                    categoria = ConsumerRightCategory(item["categoria"])

                doc = LegislacaoDocument(
                    id=item["id"],
                    knowledge_base="legislacao",
                    title=item["title"],
                    content=item["content"],
                    tipo=LegislationType(item["tipo"]),
                    numero=item["numero"],
                    ano=item["ano"],
                    artigo=item.get("artigo"),
                    categoria=categoria,
                    palavras_chave=item.get("palavras_chave", []),
                )
                self.index_legislacao(doc)
                count += 1
            except Exception as e:
                logger.error(f"Erro ao indexar {item.get('id')}: {e}")

        logger.info(f"Legislacao: {count} documentos indexados")
        return count

    def populate_all_shared_bases(self) -> Dict[str, int]:
        """
        Popula todas as bases compartilhadas com dados iniciais.

        Returns:
            Dicionario com quantidade indexada por base
        """
        results = {}

        logger.info("Iniciando populacao das bases compartilhadas...")

        results["jurisprudencia"] = self.populate_initial_jurisprudencia()
        results["legislacao"] = self.populate_initial_legislacao()

        logger.info(f"Bases compartilhadas populadas: {results}")
        return results


# =============================================================================
# INSTANCIA GLOBAL
# =============================================================================

knowledge_base_indexer = KnowledgeBaseIndexer()
