# -*- coding: utf-8 -*-
"""Tracker de métricas RAG para o backend Zellu (contrato /api/ai/rag-metrics)."""

import asyncio
import hashlib
import threading
import uuid
from typing import Any, Dict, List, Optional

import httpx


class RAGMetricsTracker:
    """Cliente fire-and-forget para POST /api/ai/rag-metrics e /batch."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        enabled: bool = True,
        timeout_seconds: float = 8.0,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.enabled = bool(enabled)
        self.timeout_seconds = float(timeout_seconds)
        self.headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
        }

    def build_event(
        self,
        *,
        operation: str,
        channel: Optional[str] = None,
        company_id: Optional[str] = None,
        knowledge_base_id: Optional[str] = None,
        knowledge_file_id: Optional[str] = None,
        ticket_id: Optional[str] = None,
        session_id: Optional[str] = None,
        event_id: Optional[str] = None,
        request_id: Optional[str] = None,
        provider: Optional[str] = None,
        embedding_model: Optional[str] = None,
        vector_store: Optional[str] = None,
        index_namespace: Optional[str] = None,
        chunk_strategy: Optional[str] = None,
        chunk_count: Optional[int] = None,
        chunk_size_tokens: Optional[int] = None,
        chunk_overlap_tokens: Optional[int] = None,
        min_chunk_tokens: Optional[int] = None,
        max_chunk_tokens: Optional[int] = None,
        embedding_dimensions: Optional[int] = None,
        vectors_upserted: Optional[int] = None,
        tokens_embedded: Optional[int] = None,
        avg_chunk_tokens: Optional[float] = None,
        top_k: Optional[int] = None,
        chunks_retrieved: Optional[int] = None,
        chunks_used: Optional[int] = None,
        context_tokens: Optional[int] = None,
        score_max: Optional[float] = None,
        score_min: Optional[float] = None,
        score_avg: Optional[float] = None,
        rerank_used: Optional[bool] = None,
        context_truncated: Optional[bool] = None,
        rerank_model: Optional[str] = None,
        query_text: Optional[str] = None,
        query_hash: Optional[str] = None,
        total_latency_ms: Optional[int] = None,
        embedding_latency_ms: Optional[int] = None,
        vector_search_latency_ms: Optional[int] = None,
        rerank_latency_ms: Optional[int] = None,
        cost_usd: Optional[float] = None,
        status: Optional[str] = None,
        error_message: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Monta um payload compatível com o contrato /api/ai/rag-metrics."""
        if not operation:
            raise ValueError("operation is required")

        event: Dict[str, Any] = {"operation": operation}

        optional: Dict[str, Any] = {
            "channel": channel,
            "companyId": company_id,
            "knowledgeBaseId": knowledge_base_id,
            "knowledgeFileId": knowledge_file_id,
            "ticketId": ticket_id,
            "sessionId": session_id,
            "eventId": event_id or str(uuid.uuid4()),
            "requestId": request_id,
            "provider": provider,
            "embeddingModel": embedding_model,
            "vectorStore": vector_store,
            "indexNamespace": index_namespace,
            "chunkStrategy": chunk_strategy,
            "chunkCount": chunk_count,
            "chunkSizeTokens": chunk_size_tokens,
            "chunkOverlapTokens": chunk_overlap_tokens,
            "minChunkTokens": min_chunk_tokens,
            "maxChunkTokens": max_chunk_tokens,
            "embeddingDimensions": embedding_dimensions,
            "vectorsUpserted": vectors_upserted,
            "tokensEmbedded": tokens_embedded,
            "avgChunkTokens": avg_chunk_tokens,
            "topK": top_k,
            "chunksRetrieved": chunks_retrieved,
            "chunksUsed": chunks_used,
            "contextTokens": context_tokens,
            "scoreMax": score_max,
            "scoreMin": score_min,
            "scoreAvg": score_avg,
            "rerankUsed": rerank_used,
            "contextTruncated": context_truncated,
            "rerankModel": rerank_model,
            "queryHash": query_hash or (hashlib.sha256(query_text.encode("utf-8")).hexdigest() if query_text else None),
            "totalLatencyMs": total_latency_ms,
            "embeddingLatencyMs": embedding_latency_ms,
            "vectorSearchLatencyMs": vector_search_latency_ms,
            "rerankLatencyMs": rerank_latency_ms,
            "costUsd": cost_usd,
            "status": status or "success",
            "errorMessage": error_message,
            "metadata": metadata,
        }

        for key, value in optional.items():
            if value is not None:
                event[key] = value

        return event

    async def _post(self, path: str, payload: Dict[str, Any]) -> None:
        if not self.enabled:
            return
        if not self.base_url:
            return
        url = f"{self.base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(url, headers=self.headers, json=payload)
                if response.status_code not in (200, 201):
                    print(f"[RAG-METRICS] {path} returned {response.status_code}: {response.text[:500]}")
        except Exception as exc:  # noqa: BLE001
            print(f"[RAG-METRICS] Failed to send {path}: {exc}")

    def _dispatch(self, coro) -> None:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(coro)
        except RuntimeError:
            def _runner() -> None:
                try:
                    asyncio.run(coro)
                except Exception as exc:  # noqa: BLE001
                    print(f"[RAG-METRICS] Failed in sync runner: {exc}")

            threading.Thread(target=_runner, daemon=True).start()

    def track_event(self, event: Dict[str, Any]) -> None:
        if not self.enabled or not event:
            return
        self._dispatch(self._post("/api/ai/rag-metrics", event))

    def track_batch(self, events: List[Dict[str, Any]]) -> None:
        if not self.enabled or not events:
            return
        try:
            for i in range(0, len(events), 500):
                chunk = events[i : i + 500]
                self._dispatch(self._post("/api/ai/rag-metrics/batch", {"events": chunk}))
        except Exception as exc:  # noqa: BLE001
            print(f"[RAG-METRICS] Failed to schedule batch: {exc}")


_tracker_instance: Optional[RAGMetricsTracker] = None


def get_rag_metrics_tracker() -> RAGMetricsTracker:
    global _tracker_instance
    if _tracker_instance is None:
        from config import get_settings

        settings = get_settings()
        _tracker_instance = RAGMetricsTracker(
            base_url=settings.ZELLU_WEBHOOK_URL,
            api_key=settings.ai_usage_api_key,
            enabled=settings.AI_USAGE_ENABLED,
            timeout_seconds=settings.AI_USAGE_TIMEOUT_MS / 1000.0,
        )
    return _tracker_instance


def track_rag_metrics(event: Dict[str, Any]) -> None:
    get_rag_metrics_tracker().track_event(event)


def track_rag_metrics_batch(events: List[Dict[str, Any]]) -> None:
    get_rag_metrics_tracker().track_batch(events)
