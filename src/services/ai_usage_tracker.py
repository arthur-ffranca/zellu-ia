# -*- coding: utf-8 -*-
"""
Rastreador de uso/custo de IA -> POST /api/ai/usage do backend Zellu.

Contrato (fonte unica): docs/20260619-contrato-ai-usage-enriquecido.md

Principios:
- ADITIVO e OPCIONAL: nunca quebra o fluxo principal. Todo envio e fire-and-forget
  e qualquer excecao e engolida (apenas logada). Se o backend estiver fora, o
  atendimento continua normalmente.
- 1 evento por chamada de LLM, "conforme acontece". O backend agrega por eventId.
  NUNCA enviar um "total" calculado aqui.
- Modalidades nao-token (TTS/STT/OCR/imagem): enviar unit + quantityIn/quantityOut
  + costUsd real, com tokensIn=0/tokensOut=0.

Uso tipico (apos uma chamada LLM async):

    from src.services.ai_usage_tracker import track_usage, new_event_id

    response = await llm.ainvoke(messages)
    track_usage(
        module="company_ai",
        model="gpt-4o-mini",
        response=response,            # extrai tokens automaticamente
        event_id=event_id,
        event_type="company_response",
        company_id=company_id,
        session_id=session_id,
        ticket_id=ticket_id,
        duration_ms=elapsed_ms,
    )
"""

import asyncio
import threading
import uuid
from typing import Any, Dict, List, Optional, Tuple

import httpx

# ---------------------------------------------------------------------------
# Modulos validos (enum de 12 do contrato)
# ---------------------------------------------------------------------------
VALID_MODULES = {
    "intake", "classifier", "analyst", "writer", "sender", "company_ai",
    "external_chat", "kb_chat", "doc_validation", "doc_analyzer",
    "embedding", "ticket_reopen",
}

# Unidades de cobranca aceitas pelo contrato (campo `unit`).
VALID_UNITS = {"tokens", "characters", "seconds", "pages", "images", "requests"}

# ---------------------------------------------------------------------------
# Canais/produtos que usam IA (campo `channel`).
# Diferente do `module` (servico tecnico): agrupa o custo pela SUPERFICIE de
# produto onde a IA foi usada. O painel agrega SUM(costUsd) GROUP BY channel.
# ---------------------------------------------------------------------------
VALID_CHANNELS = {
    "chat_externo",       # atendimento ao cliente (abertura/negociacao/chat do cliente)
    "chat_interno",       # chat interno / base de conhecimento (advogado)
    "ia_empresa",         # IA que responde em nome da empresa
    "geracao_documentos", # geracao/analise/validacao de documentos do advogado
    "agente_assistente",  # assistente de suporte (GuideAgent / support_assistant)
}

# Mapa module -> channel (default). Cobre os modulos com atribuicao 1:1.
# `embedding` e `ticket_reopen` NAO entram aqui de proposito: sao infra/fluxo
# compartilhado e "herdam do fluxo" (channel explicito no call site, ou null).
CHANNEL_BY_MODULE: Dict[str, str] = {
    "intake": "chat_externo",
    "classifier": "chat_externo",
    "analyst": "chat_externo",
    "sender": "chat_externo",
    "external_chat": "chat_externo",
    "kb_chat": "chat_interno",
    "company_ai": "ia_empresa",
    "writer": "geracao_documentos",
    "doc_analyzer": "geracao_documentos",
    "doc_validation": "geracao_documentos",
}


def channel_for_module(module: str) -> Optional[str]:
    """Retorna o canal/produto padrao para um module, ou None se herda do fluxo."""
    return CHANNEL_BY_MODULE.get(module)

# ---------------------------------------------------------------------------
# Perfis de consumo de IA (campo `userProfile`).
# Quem gerou o custo: empresa, advogado, cliente logado ou visitante deslogado.
# ---------------------------------------------------------------------------
VALID_PROFILES = {
    "empresa",          # IA respondendo em nome da empresa
    "advogado",         # advogado usando ferramentas internas
    "cliente_interno",  # cliente final autenticado (com login)
    "cliente_externo",  # visitante NAO logado (anonimo)
}

# ---------------------------------------------------------------------------
# Tabela de precos (USD por 1.000.000 de tokens) - melhor esforco.
# input = tokens de entrada (prompt); output = tokens de saida (completion).
# Embeddings cobram apenas input (output = 0).
# A resolucao tenta match exato e, em seguida, prefixo mais longo.
# ---------------------------------------------------------------------------
PRICING_PER_MILLION: Dict[str, Dict[str, float]] = {
    # OpenAI - chat
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-4-turbo": {"input": 10.00, "output": 30.00},
    "gpt-4.1": {"input": 2.00, "output": 8.00},
    "gpt-4.1-mini": {"input": 0.40, "output": 1.60},
    "gpt-4.1-nano": {"input": 0.10, "output": 0.40},
    "gpt-5": {"input": 1.25, "output": 10.00},
    "gpt-5-mini": {"input": 0.25, "output": 2.00},
    "o1": {"input": 15.00, "output": 60.00},
    "o1-mini": {"input": 1.10, "output": 4.40},
    "o3-mini": {"input": 1.10, "output": 4.40},
    # OpenAI - embeddings (apenas input)
    "text-embedding-3-small": {"input": 0.02, "output": 0.0},
    "text-embedding-3-large": {"input": 0.13, "output": 0.0},
    "text-embedding-ada-002": {"input": 0.10, "output": 0.0},
    # Anthropic - chat (ainda nao usado, mas previsto pelo contrato/provider)
    "claude-opus-4-8": {"input": 5.00, "output": 25.00},
    "claude-opus-4": {"input": 15.00, "output": 75.00},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "claude-sonnet-4": {"input": 3.00, "output": 15.00},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
    "claude-3-5-haiku": {"input": 0.80, "output": 4.00},
}


# ---------------------------------------------------------------------------
# Fallback de preco por FAMILIA de modelo.
# Quando um modelo nao esta na tabela (tipicamente override dinamico da config
# da empresa em company_response.py:313), estimamos pelo preco da familia em vez
# de gravar $0/"desconhecido". Valores conservadores (nao subestimar o custo).
# ---------------------------------------------------------------------------
_FALLBACK_FAMILY_PRICING: Dict[str, Dict[str, float]] = {
    "openai_embedding": {"input": 0.02, "output": 0.0},    # ~text-embedding-3-small
    "openai_reasoning": {"input": 1.10, "output": 4.40},   # ~o1-mini/o3-mini
    "openai_chat": {"input": 2.50, "output": 10.00},       # ~gpt-4o (conservador)
    "anthropic": {"input": 3.00, "output": 15.00},         # ~claude-sonnet
}


def _infer_family(model_lower: str) -> Optional[str]:
    """Infere a familia do modelo para o fallback de preco. None = desconhecida."""
    if not model_lower:
        return None
    if "embedding" in model_lower:
        return "openai_embedding"
    if model_lower.startswith(("o1", "o3", "o4")):
        return "openai_reasoning"
    if model_lower.startswith(("gpt", "chatgpt")):
        return "openai_chat"
    if model_lower.startswith("claude"):
        return "anthropic"
    return None


def _fallback_family_price(model_lower: str) -> Optional[Dict[str, float]]:
    """Preco estimado pela familia do modelo, ou None se familia desconhecida."""
    fam = _infer_family(model_lower)
    return _FALLBACK_FAMILY_PRICING.get(fam) if fam else None


# ---------------------------------------------------------------------------
# Registro de modelos SEM preco na tabela, detectados NA ORIGEM (no envio).
# Como somos nos que montamos o payload, sabemos exatamente quais modelos caem
# no fallback - sem precisar consultar o DB. get_unknown_models() expoe a lista.
# ---------------------------------------------------------------------------
_UNKNOWN_MODELS: Dict[str, int] = {}
_UNKNOWN_MODELS_LOCK = threading.Lock()


def record_unknown_model(model: str) -> None:
    """Registra (e loga alto na 1a vez) um modelo fora da tabela de precos."""
    name = model or "<vazio>"
    with _UNKNOWN_MODELS_LOCK:
        first_time = name not in _UNKNOWN_MODELS
        _UNKNOWN_MODELS[name] = _UNKNOWN_MODELS.get(name, 0) + 1
    if first_time:
        fam = _infer_family(name.lower())
        origem = "estimado pela familia" if fam else "SEM familia -> custo 0"
        print(
            f"[AI-USAGE] MODELO SEM PRECO NA TABELA: '{name}' ({origem}). "
            f"Adicione em PRICING_PER_MILLION para custo exato."
        )


def get_unknown_models() -> Dict[str, int]:
    """Retorna {modelo: contagem} dos modelos desconhecidos vistos neste processo."""
    with _UNKNOWN_MODELS_LOCK:
        return dict(_UNKNOWN_MODELS)


def new_event_id() -> str:
    """Gera um eventId (UUID4) para correlacionar todas as chamadas de um fluxo."""
    return str(uuid.uuid4())


def infer_provider(model: str) -> Optional[str]:
    """Infere o `provider` a partir do nome do modelo."""
    if not model:
        return None
    m = model.lower()
    if m.startswith(("gpt", "text-embedding", "o1", "o3", "o4", "dall", "whisper", "tts", "chatgpt")):
        return "openai"
    if m.startswith("claude"):
        return "anthropic"
    if m.startswith(("eleven", "tts-eleven")):
        return "elevenlabs"
    return None


def estimate_cost_usd(model: str, tokens_in: int, tokens_out: int) -> float:
    """
    Estima o custo em USD a partir do modelo e tokens.

    Resolve o preco por match exato e, em seguida, pelo prefixo mais longo
    (ex.: "gpt-4o-2024-08-06" -> "gpt-4o"). Modelo desconhecido => 0.0.
    """
    if not model:
        record_unknown_model("<vazio>")
        return 0.0
    key = model.lower()
    price = PRICING_PER_MILLION.get(key)
    if price is None:
        # prefixo mais longo que casa com o inicio do nome do modelo
        candidates = [k for k in PRICING_PER_MILLION if key.startswith(k)]
        if candidates:
            price = PRICING_PER_MILLION[max(candidates, key=len)]
    if price is None:
        # Fora da tabela (tipicamente override dinamico da empresa). Registramos
        # na origem (sabemos AGORA que estamos enviando um desconhecido) e
        # estimamos pela familia em vez de gravar $0/"desconhecido".
        record_unknown_model(model)
        price = _fallback_family_price(key)
        if price is None:
            return 0.0
    cost = (tokens_in / 1_000_000.0) * price["input"] + (tokens_out / 1_000_000.0) * price["output"]
    # arredonda para evitar ruido de ponto flutuante
    return round(cost, 8)


def extract_tokens(response: Any) -> Tuple[int, int]:
    """
    Extrai (tokens_in, tokens_out) de uma resposta de LLM, defensivamente.

    Suporta:
    - framework antigo AIMessage: `.usage_metadata` = {input_tokens, output_tokens, ...}
    - framework antigo `.response_metadata["token_usage"]` = {prompt_tokens, completion_tokens}
    - OpenAI SDK: `.usage` = {prompt_tokens, completion_tokens} (chat) ou
      {prompt_tokens, total_tokens} (embeddings)
    - dict cru com qualquer um dos formatos acima
    Retorna (0, 0) se nada for encontrado.
    """
    if response is None:
        return 0, 0

    # 1) framework antigo AIMessage.usage_metadata
    um = getattr(response, "usage_metadata", None)
    if isinstance(um, dict) and (um.get("input_tokens") is not None or um.get("output_tokens") is not None):
        return int(um.get("input_tokens") or 0), int(um.get("output_tokens") or 0)

    # 2) framework antigo response_metadata.token_usage / usage
    rm = getattr(response, "response_metadata", None)
    if isinstance(rm, dict):
        tu = rm.get("token_usage") or rm.get("usage") or {}
        if tu:
            return int(tu.get("prompt_tokens") or 0), int(tu.get("completion_tokens") or 0)

    # 3) OpenAI SDK response.usage (objeto ou dict)
    usage = getattr(response, "usage", None)
    if usage is not None:
        def _g(obj, name):
            if isinstance(obj, dict):
                return obj.get(name)
            return getattr(obj, name, None)
        pin = _g(usage, "prompt_tokens")
        pout = _g(usage, "completion_tokens")
        if pin is not None or pout is not None:
            return int(pin or 0), int(pout or 0)
        # embeddings: so prompt_tokens/total_tokens
        total = _g(usage, "total_tokens")
        if total is not None:
            return int(total or 0), 0

    # 4) dict cru
    if isinstance(response, dict):
        for key in ("usage_metadata", "token_usage", "usage"):
            tu = response.get(key)
            if isinstance(tu, dict):
                return (
                    int(tu.get("input_tokens") or tu.get("prompt_tokens") or 0),
                    int(tu.get("output_tokens") or tu.get("completion_tokens") or 0),
                )

    return 0, 0


class AIUsageTracker:
    """
    Cliente fire-and-forget para POST /api/ai/usage (e /batch).

    Nunca lanca excecao para o chamador: erros sao logados e descartados.
    """

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

    # ------------------------------------------------------------------ build
    def build_event(
        self,
        module: str,
        model: str,
        *,
        response: Any = None,
        tokens_in: Optional[int] = None,
        tokens_out: Optional[int] = None,
        cost_usd: Optional[float] = None,
        event_id: Optional[str] = None,
        event_type: Optional[str] = None,
        provider: Optional[str] = None,
        model_version: Optional[str] = None,
        user_id: Optional[str] = None,
        channel: Optional[str] = None,
        profile: Optional[str] = None,
        unit: Optional[str] = None,
        quantity_in: Optional[float] = None,
        quantity_out: Optional[float] = None,
        session_id: Optional[str] = None,
        ticket_id: Optional[str] = None,
        company_id: Optional[str] = None,
        duration_ms: Optional[int] = None,
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Monta o payload do evento conforme o contrato (camelCase)."""
        # tokens: usa os explicitos, senao extrai da resposta
        if tokens_in is None or tokens_out is None:
            ext_in, ext_out = extract_tokens(response)
            tokens_in = ext_in if tokens_in is None else tokens_in
            tokens_out = ext_out if tokens_out is None else tokens_out
        tokens_in = int(tokens_in or 0)
        tokens_out = int(tokens_out or 0)

        # unit default: tokens (modalidade padrao)
        if unit is None:
            unit = "tokens"

        # custo: usa o explicito, senao estima por tokens (so faz sentido p/ unit=tokens)
        if cost_usd is None:
            cost_usd = estimate_cost_usd(model, tokens_in, tokens_out) if unit == "tokens" else 0.0
        cost_usd = round(float(cost_usd), 8)

        # provider inferido se nao informado
        if provider is None:
            provider = infer_provider(model)

        # canal/produto: usa o explicito, senao deriva do module (mapa central).
        # embedding/ticket_reopen ficam null quando o call site nao informa.
        if channel is None:
            channel = channel_for_module(module)
        if channel is not None and channel not in VALID_CHANNELS:
            print(f"[AI-USAGE] AVISO: channel '{channel}' fora do enum de canais.")

        # perfil de consumo (userProfile): so validado; nunca derivado de module
        # aqui (module e' ambiguo p/ perfil - kb_chat serve empresa/advogado/cliente).
        if profile is not None and profile not in VALID_PROFILES:
            print(f"[AI-USAGE] AVISO: userProfile '{profile}' fora do enum de perfis.")

        # quantidade espelha tokens quando a modalidade for por tokens
        if unit == "tokens":
            if quantity_in is None:
                quantity_in = tokens_in
            if quantity_out is None:
                quantity_out = tokens_out

        if module not in VALID_MODULES:
            print(f"[AI-USAGE] AVISO: module '{module}' fora do enum do contrato.")

        event: Dict[str, Any] = {
            "module": module,
            "model": model,
            "tokensIn": tokens_in,
            "tokensOut": tokens_out,
            "costUsd": cost_usd,
        }

        # opcionais - so incluidos quando presentes
        optional = {
            "eventId": event_id,
            "eventType": event_type,
            "provider": provider,
            "modelVersion": model_version,
            "userId": user_id,
            "channel": channel,
            "userProfile": profile,
            "unit": unit,
            "quantityIn": quantity_in,
            "quantityOut": quantity_out,
            "sessionId": session_id,
            "ticketId": ticket_id,
            "companyId": company_id,
            "durationMs": duration_ms,
            "requestId": request_id,
        }
        for k, v in optional.items():
            if v is not None:
                event[k] = v

        return event

    # ------------------------------------------------------------------ send
    async def _post(self, path: str, payload: Dict[str, Any]) -> None:
        if not self.enabled:
            return
        if not self.base_url:
            print("[AI-USAGE] base_url vazio - evento descartado.")
            return
        url = f"{self.base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                resp = await client.post(url, headers=self.headers, json=payload)
                if resp.status_code in (200, 201):
                    return
                # 400 = payload invalido; loga corpo para diagnostico (sem quebrar)
                print(f"[AI-USAGE] {path} retornou {resp.status_code}: {resp.text[:500]}")
        except Exception as e:  # noqa: BLE001 - nunca propagar
            print(f"[AI-USAGE] Falha ao enviar para {path}: {e}")

    def _dispatch(self, coro) -> None:
        """Agenda a corrotina sem bloquear o chamador (fire-and-forget)."""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(coro)
        except RuntimeError:
            # Sem event loop no contexto atual (chamada sincrona): roda em thread.
            def _runner():
                try:
                    asyncio.run(coro)
                except Exception as e:  # noqa: BLE001
                    print(f"[AI-USAGE] Falha no runner sincrono: {e}")

            threading.Thread(target=_runner, daemon=True).start()

    def track(self, module: str, model: str, **kwargs: Any) -> None:
        """
        Registra um evento de uso (fire-and-forget). Aceita os mesmos kwargs de
        build_event(). Nunca lanca excecao para o chamador.
        """
        if not self.enabled:
            return
        try:
            event = self.build_event(module, model, **kwargs)
            self._dispatch(self._post("/api/ai/usage", event))
        except Exception as e:  # noqa: BLE001
            print(f"[AI-USAGE] Falha ao montar/agendar evento ({module}/{model}): {e}")

    def track_batch(self, events: List[Dict[str, Any]]) -> None:
        """
        Registra um lote de eventos ja montados (cada um via build_event()).
        Limite do contrato: <= 500 eventos por request.
        """
        if not self.enabled or not events:
            return
        try:
            for i in range(0, len(events), 500):
                chunk = events[i : i + 500]
                self._dispatch(self._post("/api/ai/usage/batch", {"events": chunk}))
        except Exception as e:  # noqa: BLE001
            print(f"[AI-USAGE] Falha ao agendar batch: {e}")


# ---------------------------------------------------------------------------
# Singleton lazy - construido a partir de get_settings() na primeira chamada.
# ---------------------------------------------------------------------------
_tracker_instance: Optional[AIUsageTracker] = None


def get_tracker() -> AIUsageTracker:
    """Retorna o tracker singleton, inicializando a partir das Settings."""
    global _tracker_instance
    if _tracker_instance is None:
        from config import get_settings

        settings = get_settings()
        _tracker_instance = AIUsageTracker(
            base_url=settings.ZELLU_WEBHOOK_URL,
            api_key=settings.ai_usage_api_key,
            enabled=settings.AI_USAGE_ENABLED,
            timeout_seconds=settings.AI_USAGE_TIMEOUT_MS / 1000.0,
        )
        print(
            f"[AI-USAGE] Tracker inicializado | enabled={_tracker_instance.enabled} "
            f"| base_url={_tracker_instance.base_url}"
        )
    return _tracker_instance


def track_usage(module: str, model: str, **kwargs: Any) -> None:
    """Atalho de modulo: registra um evento de uso (fire-and-forget)."""
    get_tracker().track(module, model, **kwargs)


def track_usage_batch(events: List[Dict[str, Any]]) -> None:
    """Atalho de modulo: registra um lote de eventos."""
    get_tracker().track_batch(events)

