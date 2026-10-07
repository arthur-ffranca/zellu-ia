"""
Embedding Service - Zellu IA Empresa

Serviço responsável por gerar embeddings (vetores numéricos) de texto
usando a API da OpenAI. Esses embeddings são usados para busca semântica
no sistema RAG.

O que são Embeddings:
    Embeddings são representações vetoriais de texto que capturam o
    significado semântico. Textos com significados similares terão
    embeddings próximos no espaço vetorial.

Modelo utilizado:
    text-embedding-3-small (OpenAI) - 1536 dimensões
    - Rápido e econômico
    - Boa qualidade para RAG
    - Limite de ~8000 tokens por texto

Classes:
    EmbeddingService: Gera embeddings para textos individuais ou em lote

Uso:
    >>> from src.services import embedding_service
    >>> vector = embedding_service.generate_embedding("Qual o prazo de entrega?")
    >>> print(len(vector))  # 1536
"""

from typing import List
from openai import OpenAI
import logging
import time

from config import get_settings, llm_default_headers
from src.services.ai_usage_tracker import track_usage
from src.services.embedding_cache import get_embedding_cache

logger = logging.getLogger(__name__)


class EmbeddingService:
    """
    Serviço para geração de embeddings de texto.

    Utiliza a API de embeddings da OpenAI para transformar textos
    em vetores numéricos que podem ser comparados por similaridade.

    Attributes:
        settings: Configurações da aplicação
        client: Cliente OpenAI configurado
        model: Nome do modelo de embedding a usar

    Example:
        >>> service = EmbeddingService()
        >>> embedding = service.generate_embedding("Olá, como posso ajudar?")
        >>> print(f"Vetor com {len(embedding)} dimensões")
    """

    def __init__(self):
        """
        Inicializa o serviço com as configurações da aplicação.

        Carrega a API key e modelo de embedding das configurações.
        """
        self.settings = get_settings()
        self.client = OpenAI(default_headers=llm_default_headers(), api_key=self.settings.openai_api_key)
        self.model = self.settings.openai_embedding_model
        # Cache sha256(model+texto) -> vetor (memoria + Redis). Evita reembedar
        # queries/chunks repetidos (contrato 20260629).
        self.cache = get_embedding_cache()

    def generate_embedding(self, text: str) -> List[float]:
        """
        Gera embedding para um único texto.

        Processa o texto (limpeza e truncamento) e envia para a API
        da OpenAI para gerar o vetor de embedding.

        Args:
            text: Texto para gerar embedding (máx ~8000 caracteres)

        Returns:
            Lista de floats representando o vetor de embedding
            (1536 dimensões para text-embedding-3-small)

        Raises:
            Exception: Se houver erro na chamada à API da OpenAI

        Example:
            >>> embedding = service.generate_embedding("Qual o prazo?")
            >>> print(type(embedding))  # <class 'list'>
        """
        try:
            # Limpar e truncar texto se necessário
            text = text.replace("\n", " ").strip()
            if len(text) > 8000:
                text = text[:8000]

            # Cache hit: retorna sem chamar a API (sem custo, sem track_usage).
            cached = self.cache.get(text, self.model)
            if cached is not None:
                return cached

            _t0 = time.perf_counter()
            response = self.client.embeddings.create(
                model=self.model,
                input=text,
            )
            track_usage(
                module="embedding",
                model=self.model,
                response=response,
                event_type="embedding",
                unit="tokens",
                duration_ms=int((time.perf_counter() - _t0) * 1000),
            )

            vector = response.data[0].embedding
            self.cache.set(text, self.model, vector)
            return vector

        except Exception as e:
            logger.error(f"Erro ao gerar embedding: {e}")
            raise

    def generate_embeddings_batch(self, texts: List[str]) -> List[List[float]]:
        """
        Gera embeddings para múltiplos textos em uma única chamada.

        Mais eficiente que chamar generate_embedding() múltiplas vezes,
        pois agrupa tudo em uma única requisição à API.

        Args:
            texts: Lista de textos para gerar embeddings

        Returns:
            Lista de embeddings na mesma ordem dos textos de entrada

        Raises:
            Exception: Se houver erro na chamada à API da OpenAI

        Example:
            >>> textos = ["Texto 1", "Texto 2", "Texto 3"]
            >>> embeddings = service.generate_embeddings_batch(textos)
            >>> print(len(embeddings))  # 3
        """
        try:
            # Limpar textos
            cleaned_texts = [
                t.replace("\n", " ").strip()[:8000]
                for t in texts
            ]

            # Separa hits (cache) de misses; so os misses vao para a API.
            results: List[List[float]] = [None] * len(cleaned_texts)  # type: ignore[list-item]
            miss_texts: List[str] = []
            miss_idx: List[int] = []
            for i, t in enumerate(cleaned_texts):
                cached = self.cache.get(t, self.model)
                if cached is not None:
                    results[i] = cached
                else:
                    miss_texts.append(t)
                    miss_idx.append(i)

            # Tudo em cache: nenhuma chamada/custo.
            if not miss_texts:
                return results

            _t0 = time.perf_counter()
            response = self.client.embeddings.create(
                model=self.model,
                input=miss_texts,
            )
            track_usage(
                module="embedding",
                model=self.model,
                response=response,
                event_type="embedding_batch",
                unit="tokens",
                duration_ms=int((time.perf_counter() - _t0) * 1000),
            )

            for j, item in enumerate(response.data):
                idx = miss_idx[j]
                results[idx] = item.embedding
                self.cache.set(miss_texts[j], self.model, item.embedding)

            return results

        except Exception as e:
            logger.error(f"Erro ao gerar embeddings em batch: {e}")
            raise


# =============================================================================
# INSTÂNCIA GLOBAL
# =============================================================================
# Singleton para uso em toda a aplicação
# Import: from src.services import embedding_service
embedding_service = EmbeddingService()
