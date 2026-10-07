"""Early progress bubbles during intake (is_finished=False).

Fire-and-forget UX: failures must never break the webhook contract.
Does not append to state['messages'] so LLM history stays clean.
"""
from __future__ import annotations

import logging
import sys

logger = logging.getLogger(__name__)

MSG_BEFORE_LLM = "Beleza, tô lendo o que você contou…"
MSG_BEFORE_COMPANY_SEARCH = "Vou localizar a empresa com o que você passou…"
MSG_CACHE_MISS = "Ainda buscando… um instante."
MSG_ANALYZING = "Perfeito! Estou analisando seu caso agora…"
MSG_DEEP_SEARCH = "Vou procurar mais a fundo na web… pode levar alguns segundos."
MSG_REFERENCE_SEARCH = "Boa, vou buscar de novo com essa referência…"


def _zellu_client():
    """Cliente do src.main ja carregado (uvicorn "src.main:app").

    Nunca importa src.main daqui: em teste/worker isolado isso subiria a aplicacao
    inteira so para mandar uma bolha opcional.
    """
    for name in ("src.main", "main"):  # "main" = copia legada na raiz do repo
        client = getattr(sys.modules.get(name), "zellu_client", None)
        if client is not None:
            return client
    return None


async def send_progress(state, message: str) -> None:
    """Send an interim chat bubble via zellu_client (is_finished=False)."""
    text = (message or "").strip()
    chat_id = state.get("chat_id") if isinstance(state, dict) else None
    if not text or not chat_id:
        return
    client = _zellu_client()
    if client is None:
        return
    try:
        # Sem dispatch_id de proposito: o eco do despacho (contrato 20260807) fica so
        # na resposta final do turno, para o app nao tratar a bolha como "a" resposta
        # e descartar a final como duplicada.
        await client.send_message(
            chat_id=str(chat_id),
            message=text,
            is_finished=False,
        )
    except Exception as exc:
        logger.warning("[INTAKE-PROGRESS] falha ao enviar bolha (%s): %s", type(exc).__name__, exc)
