"""
Cache de embeddings por hash de texto (contrato 20260629 - item "Cache de
embeddings por hash de texto").

Objetivo: `embedding` e RAG reembedam os mesmos textos (query do usuario, chunks
reindexados). Cachear `sha256(model+texto) -> vetor` evita recomputar e economiza
custo/latencia na OpenAI.

Arquitetura (2 camadas, tudo tolerante a falha):
- L1: memoria (LRU limitado, por processo) - hit mais rapido, some no restart.
- L2: Redis (compartilhado entre workers/instancias, sobrevive a restart) -
  credenciais no servidor (REDIS_USERNAME/REDIS_PASSWORD/REDIS_HOST...).

Principios:
- NUNCA quebra o fluxo: qualquer erro de Redis e engolido (logado) e cai para a
  memoria. Se `redis` nem estiver instalado, opera so com L1.
- Embedding e deterministico por (model, texto): o valor cacheado nunca "expira"
  por estar velho. O TTL existe so para limitar memoria no Redis.
- Vetores sao serializados em float32 (compacto: 1536 dims = ~6KB vs ~30KB JSON).
"""

import array
import hashlib
import logging
import threading
import time
from collections import OrderedDict
from typing import List, Optional

from config import get_settings

logger = logging.getLogger(__name__)

# Import defensivo: o pacote pode nao estar instalado (ex.: dev local sem redis).
try:  # pragma: no cover - depende do ambiente
    import redis as _redis_lib
except Exception:  # noqa: BLE001
    _redis_lib = None

# Prefixo/versao da chave. Se o formato de serializacao mudar, suba a versao
# para invalidar caches antigos sem colisao.
_KEY_PREFIX = "emb:v1"

# Intervalo minimo (s) entre tentativas de reconectar ao Redis apos falha, para
# nao pagar timeout de conexao em toda chamada quando o Redis esta fora.
_REDIS_RETRY_INTERVAL_S = 30.0


def _hash_key(model: str, text: str) -> str:
    """Chave estavel `emb:v1:{model}:{sha256(text)}`."""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"{_KEY_PREFIX}:{model}:{digest}"


def _encode(vector: List[float]) -> bytes:
    """Serializa o vetor em float32 (compacto)."""
    return array.array("f", vector).tobytes()


def _decode(blob: bytes) -> List[float]:
    """Desserializa float32 de volta para lista de floats."""
    arr = array.array("f")
    arr.frombytes(blob)
    return arr.tolist()


class EmbeddingCache:
    """Cache de embeddings em duas camadas (memoria + Redis), tolerante a falha."""

    def __init__(self) -> None:
        s = get_settings()
        self.enabled: bool = bool(getattr(s, "EMBEDDING_CACHE_ENABLED", True))
        self.ttl_seconds: int = int(getattr(s, "EMBEDDING_CACHE_TTL_SECONDS", 2592000))
        self.memory_max: int = int(getattr(s, "EMBEDDING_CACHE_MEMORY_MAX", 10000))

        # L1: LRU em memoria (OrderedDict + lock, seguro entre threads).
        self._mem: "OrderedDict[str, List[float]]" = OrderedDict()
        self._mem_lock = threading.Lock()

        # L2: Redis (lazy). Configuracao vinda do servidor (Coolify).
        self._redis_enabled: bool = bool(getattr(s, "REDIS_ENABLED", True)) and bool(
            getattr(s, "REDIS_HOST", "")
        )
        self._redis_conf = {
            "host": getattr(s, "REDIS_HOST", ""),
            "port": int(getattr(s, "REDIS_PORT", 6379)),
            "db": int(getattr(s, "REDIS_DB", 0)),
            "username": getattr(s, "REDIS_USERNAME", "") or None,
            "password": getattr(s, "REDIS_PASSWORD", "") or None,
            "ssl": bool(getattr(s, "REDIS_SSL", False)),
        }
        self._redis = None
        self._redis_ok = False
        self._redis_next_retry = 0.0

        if not self.enabled:
            logger.info("[EMB-CACHE] desabilitado (EMBEDDING_CACHE_ENABLED=false).")
        elif self._redis_enabled and _redis_lib is None:
            logger.warning("[EMB-CACHE] pacote 'redis' ausente; usando so cache em memoria.")

    # ------------------------------------------------------------------ redis
    def _get_redis(self):
        """Retorna um cliente Redis vivo ou None (com backoff em caso de falha)."""
        if not self._redis_enabled or _redis_lib is None:
            return None
        if self._redis_ok and self._redis is not None:
            return self._redis
        # backoff: nao tenta reconectar a cada chamada
        now = time.monotonic()
        if now < self._redis_next_retry:
            return None
        try:
            client = _redis_lib.Redis(
                host=self._redis_conf["host"],
                port=self._redis_conf["port"],
                db=self._redis_conf["db"],
                username=self._redis_conf["username"],
                password=self._redis_conf["password"],
                ssl=self._redis_conf["ssl"],
                socket_connect_timeout=1.0,
                socket_timeout=1.0,
                retry_on_timeout=False,
                decode_responses=False,
            )
            client.ping()
            self._redis = client
            self._redis_ok = True
            logger.info(
                "[EMB-CACHE] Redis conectado em %s:%s db=%s",
                self._redis_conf["host"], self._redis_conf["port"], self._redis_conf["db"],
            )
            return client
        except Exception as e:  # noqa: BLE001 - nunca propagar
            self._redis_ok = False
            self._redis = None
            self._redis_next_retry = now + _REDIS_RETRY_INTERVAL_S
            logger.warning("[EMB-CACHE] Redis indisponivel (%s); cache so em memoria por ora.", e)
            return None

    # ------------------------------------------------------------------ memory
    def _mem_get(self, key: str) -> Optional[List[float]]:
        with self._mem_lock:
            vec = self._mem.get(key)
            if vec is not None:
                self._mem.move_to_end(key)  # marca como recem-usado (LRU)
            return vec

    def _mem_set(self, key: str, vector: List[float]) -> None:
        with self._mem_lock:
            self._mem[key] = vector
            self._mem.move_to_end(key)
            while len(self._mem) > self.memory_max:
                self._mem.popitem(last=False)  # descarta o menos usado

    # ------------------------------------------------------------------ public
    def get(self, text: str, model: str) -> Optional[List[float]]:
        """Retorna o vetor cacheado para (model, texto) ou None."""
        if not self.enabled or not text:
            return None
        key = _hash_key(model, text)

        # L1
        vec = self._mem_get(key)
        if vec is not None:
            return vec

        # L2
        client = self._get_redis()
        if client is not None:
            try:
                blob = client.get(key)
                if blob:
                    vec = _decode(blob)
                    self._mem_set(key, vec)  # promove para L1
                    return vec
            except Exception as e:  # noqa: BLE001
                self._redis_ok = False
                logger.warning("[EMB-CACHE] falha no GET Redis (%s); ignorando.", e)
        return None

    def set(self, text: str, model: str, vector: List[float]) -> None:
        """Grava o vetor no cache (memoria + Redis)."""
        if not self.enabled or not text or not vector:
            return
        key = _hash_key(model, text)
        self._mem_set(key, vector)

        client = self._get_redis()
        if client is not None:
            try:
                client.set(key, _encode(vector), ex=self.ttl_seconds)
            except Exception as e:  # noqa: BLE001
                self._redis_ok = False
                logger.warning("[EMB-CACHE] falha no SET Redis (%s); ignorando.", e)


# ---------------------------------------------------------------------------
# Singleton compartilhado
# ---------------------------------------------------------------------------
_cache_instance: Optional[EmbeddingCache] = None


def get_embedding_cache() -> EmbeddingCache:
    global _cache_instance
    if _cache_instance is None:
        _cache_instance = EmbeddingCache()
    return _cache_instance


# ---------------------------------------------------------------------------
# Native OpenAI embeddings with the existing shared cache.
class CachedOpenAIEmbeddings:
    def __init__(self, model, api_key=None, openai_api_key=None):
        from openai import OpenAI
        from config import llm_default_headers
        self.model = model
        self.client = OpenAI(default_headers=llm_default_headers(), api_key=api_key or openai_api_key, timeout=45, max_retries=1)

    def embed_query(self, text):
        return self.embed_documents([text])[0]

    def embed_documents(self, texts):
        cache = get_embedding_cache()
        results = [cache.get(text, self.model) for text in texts]
        missing = [i for i, value in enumerate(results) if value is None]
        for start in range(0, len(missing), 128):
            indices = missing[start:start + 128]
            response = self.client.embeddings.create(model=self.model, input=[texts[i] for i in indices])
            for item in response.data:
                index = indices[item.index]
                results[index] = item.embedding
                cache.set(texts[index], self.model, item.embedding)
        if any(value is None for value in results):
            raise ValueError('Embedding provider omitted a requested vector')
        return results
