# -*- coding: utf-8 -*-
"""
Knowledge Base Service - Zellu IA Empresa

Servico responsavel por buscar documentos nas bases de conhecimento.
Este servico e usado pelo function calling da IA para consultar:
- Bases da empresa (politicas, produtos, faq, etc.)
- Bases compartilhadas (jurisprudencia, legislacao)

Arquitetura:
- Usa EmbeddingService para gerar embeddings da query
- Usa PineconeService para buscar vetores similares
- Retorna documentos formatados para a IA usar na resposta

Namespaces no Pinecone:
- company_{id}: Documentos privados da empresa
- global: Jurisprudencia e legislacao (compartilhados)
"""

from typing import List, Dict, Any, Optional
import logging
import time

from src.services.embedding_service import embedding_service
from src.services.pinecone_service import pinecone_service
from src.services.rag_metrics_tracker import track_rag_metrics
from .config import (
    get_base_by_id,
    is_shared_base,
    COMPANY_KNOWLEDGE_BASES,
)
from .schemas import ConsumerRightCategory

logger = logging.getLogger(__name__)


# =============================================================================
# CONSTANTES
# =============================================================================

GLOBAL_NAMESPACE = "global"
COMPANY_NAMESPACE_PREFIX = "company_"


# =============================================================================
# SERVICO PRINCIPAL
# =============================================================================

class KnowledgeBaseService:
    """
    Servico para buscar documentos nas bases de conhecimento.

    Metodos principais:
    - search: Busca generica em qualquer base
    - search_jurisprudencia: Busca especifica em jurisprudencia
    - search_legislacao: Busca especifica em legislacao
    - search_company_base: Busca em base da empresa

    Exemplo:
        >>> service = KnowledgeBaseService()
        >>> results = service.search(
        ...     query="prazo de troca de produto",
        ...     base_id="politicas_gerais",
        ...     company_id="empresa_123"
        ... )
    """

    def __init__(self):
        """Inicializa o servico."""
        self.embedding_service = embedding_service
        self.pinecone_service = pinecone_service

    def _get_namespace(self, base_id: str, company_id: Optional[str] = None) -> str:
        """
        Determina o namespace correto para a busca.

        Args:
            base_id: ID da base de conhecimento
            company_id: ID da empresa (obrigatorio para bases de empresa)

        Returns:
            Namespace no formato correto
        """
        if is_shared_base(base_id):
            return GLOBAL_NAMESPACE
        else:
            if not company_id:
                raise ValueError(f"company_id obrigatorio para base {base_id}")
            return f"{COMPANY_NAMESPACE_PREFIX}{company_id}"

    def search(
        self,
        query: str,
        base_id: str,
        company_id: Optional[str] = None,
        top_k: int = 5,
        min_score: float = 0.5,
        filters: Optional[Dict] = None,
    ) -> List[Dict[str, Any]]:
        """
        Busca documentos em uma base de conhecimento.

        Args:
            query: Texto da busca
            base_id: ID da base (ex: "jurisprudencia", "politicas_gerais")
            company_id: ID da empresa (obrigatorio para bases de empresa)
            top_k: Numero maximo de resultados
            min_score: Score minimo de similaridade (0.0 a 1.0)
            filters: Filtros adicionais de metadata

        Returns:
            Lista de documentos encontrados com score e metadata
        """
        try:
            # Validar base
            base = get_base_by_id(base_id)
            if not base:
                logger.warning(f"Base {base_id} nao encontrada")
                return []

            # Determinar namespace
            namespace = self._get_namespace(base_id, company_id)
            total_t0 = time.perf_counter()
            embedding_t0 = time.perf_counter()

            # Gerar embedding da query
            query_embedding = self.embedding_service.generate_embedding(query)
            embedding_latency_ms = int((time.perf_counter() - embedding_t0) * 1000)

            # Construir filtro
            # Nota: Para bases compartilhadas (jurisprudencia, legislacao), filtramos por knowledge_base
            # Para bases de empresa, nao filtramos pois os docs sao indexados sem essa metadata
            search_filter = {}
            if is_shared_base(base_id):
                search_filter["knowledge_base"] = {"$eq": base_id}
            if filters:
                search_filter.update(filters)

            # Buscar no Pinecone
            vector_search_t0 = time.perf_counter()
            results = self.pinecone_service.query(
                vector=query_embedding,
                namespace=namespace,
                top_k=top_k,
                filter=search_filter if search_filter else None,
            )
            vector_search_latency_ms = int((time.perf_counter() - vector_search_t0) * 1000)

            # Filtrar por score minimo
            filtered = [r for r in results if r["score"] >= min_score]
            score_values = [r.get("score", 0.0) for r in filtered]
            score_max = max(score_values) if score_values else 0.0
            score_min = min(score_values) if score_values else 0.0
            score_avg = (sum(score_values) / len(score_values)) if score_values else 0.0
            context_tokens = int(sum(len(str(r.get("metadata", {}).get("text", "")).split()) for r in filtered))

            logger.info(f"Busca em {base_id}: {len(filtered)} resultados (query: {query[:50]}...)")

            track_rag_metrics({
                "operation": "query",
                "channel": "company_knowledge_base" if not is_shared_base(base_id) else "chat_interno",
                "companyId": company_id,
                "eventId": None,
                "provider": "openai",
                "embeddingModel": getattr(self.embedding_service, "model", None),
                "vectorStore": "pinecone",
                "indexNamespace": namespace,
                "topK": top_k,
                "chunksRetrieved": len(results),
                "chunksUsed": len(filtered),
                "scoreMax": round(score_max, 6),
                "scoreMin": round(score_min, 6),
                "scoreAvg": round(score_avg, 6),
                "contextTokens": context_tokens,
                "embeddingLatencyMs": embedding_latency_ms,
                "vectorSearchLatencyMs": vector_search_latency_ms,
                "totalLatencyMs": int((time.perf_counter() - total_t0) * 1000),
                "queryHash": None,
                "status": "success",
                "metadata": {
                    "base_id": base_id,
                    "namespace": namespace,
                    "min_score": min_score,
                },
            })

            return filtered

        except Exception as e:
            logger.error(f"Erro na busca em {base_id}: {e}")
            track_rag_metrics({
                "operation": "query",
                "channel": "company_knowledge_base" if company_id else "chat_interno",
                "companyId": company_id,
                "eventId": None,
                "provider": "openai",
                "embeddingModel": getattr(self.embedding_service, "model", None),
                "vectorStore": "pinecone",
                "status": "error",
                "errorMessage": str(e),
                "metadata": {
                    "base_id": base_id,
                    "namespace": self._get_namespace(base_id, company_id) if company_id else None,
                },
            })
            return []

    def search_jurisprudencia(
        self,
        query: str,
        categoria: Optional[ConsumerRightCategory] = None,
        tribunal: Optional[str] = None,
        decisao_favoravel: Optional[bool] = None,
        top_k: int = 5,
    ) -> List[Dict[str, Any]]:
        """
        Busca especifica em jurisprudencia com filtros.

        Args:
            query: Texto da busca
            categoria: Filtrar por categoria (ex: COBRANCA_INDEVIDA)
            tribunal: Filtrar por tribunal (ex: "STJ", "TJ")
            decisao_favoravel: Se True, apenas decisoes favoraveis ao consumidor
            top_k: Numero maximo de resultados

        Returns:
            Lista de jurisprudencias encontradas
        """
        filters = {}

        if categoria:
            filters["categoria"] = {"$eq": categoria.value}

        if tribunal:
            filters["tribunal"] = {"$eq": tribunal}

        if decisao_favoravel is not None:
            if decisao_favoravel:
                filters["decisao"] = {"$eq": "favoravel_consumidor"}
            else:
                filters["decisao"] = {"$eq": "favoravel_empresa"}

        return self.search(
            query=query,
            base_id="jurisprudencia",
            top_k=top_k,
            min_score=0.3,
            filters=filters if filters else None,
        )

    def search_legislacao(
        self,
        query: str,
        categoria: Optional[ConsumerRightCategory] = None,
        tipo: Optional[str] = None,
        top_k: int = 5,
    ) -> List[Dict[str, Any]]:
        """
        Busca especifica em legislacao com filtros.

        Args:
            query: Texto da busca
            categoria: Filtrar por categoria relacionada
            tipo: Tipo de legislacao (lei, decreto, sumula)
            top_k: Numero maximo de resultados

        Returns:
            Lista de legislacoes encontradas
        """
        filters = {}

        if categoria:
            filters["categoria"] = {"$eq": categoria.value}

        if tipo:
            filters["tipo"] = {"$eq": tipo}

        return self.search(
            query=query,
            base_id="legislacao",
            top_k=top_k,
            min_score=0.3,
            filters=filters if filters else None,
        )

    def search_company_base(
        self,
        query: str,
        company_id: str,
        base_id: str,
        top_k: int = 5,
    ) -> List[Dict[str, Any]]:
        """
        Busca em uma base especifica da empresa.

        Args:
            query: Texto da busca
            company_id: ID da empresa
            base_id: ID da base (politicas_gerais, faq, etc.)
            top_k: Numero maximo de resultados

        Returns:
            Lista de documentos encontrados
        """
        return self.search(
            query=query,
            base_id=base_id,
            company_id=company_id,
            top_k=top_k,
        )

    def search_all_company_bases(
        self,
        query: str,
        company_id: str,
        top_k_per_base: int = 3,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        Busca em todas as bases da empresa.

        Util para buscas amplas onde nao se sabe qual base tem a resposta.

        Args:
            query: Texto da busca
            company_id: ID da empresa
            top_k_per_base: Resultados por base

        Returns:
            Dicionario com resultados por base
        """
        results = {}

        for base in COMPANY_KNOWLEDGE_BASES:
            base_results = self.search(
                query=query,
                base_id=base.id,
                company_id=company_id,
                top_k=top_k_per_base,
            )
            if base_results:
                results[base.id] = base_results

        return results

    def format_results_for_prompt(
        self,
        results: List[Dict[str, Any]],
        max_chars: int = 2000,
    ) -> str:
        """
        Formata resultados para incluir no prompt da IA.

        Args:
            results: Lista de resultados da busca
            max_chars: Limite de caracteres no total

        Returns:
            Texto formatado para o prompt
        """
        if not results:
            return "Nenhum documento encontrado."

        formatted = []
        total_chars = 0

        for i, result in enumerate(results, 1):
            metadata = result.get("metadata", {})
            score = result.get("score", 0)

            title = metadata.get("title", "Sem titulo")
            content = metadata.get("content", metadata.get("summary", ""))

            # Truncar conteudo se necessario
            available = max_chars - total_chars - 100  # margem para formatacao
            if len(content) > available:
                content = content[:available] + "..."

            entry = f"[{i}] {title} (relevancia: {score:.0%})\n{content}\n"
            formatted.append(entry)
            total_chars += len(entry)

            if total_chars >= max_chars:
                break

        return "\n".join(formatted)


# =============================================================================
# INSTANCIA GLOBAL
# =============================================================================

knowledge_base_service = KnowledgeBaseService()
