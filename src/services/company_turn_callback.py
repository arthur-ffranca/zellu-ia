# -*- coding: utf-8 -*-
"""
Entrega do resultado do turno da IA da empresa ao backend Zellu.

Contrato: 20260911-turno-assincrono-ia-da-empresa.md (secao 3.2).

Quando o payload de turno traz `callbackUrl`, respondemos 202 na hora e o
resultado volta por aqui: um POST assinado para a URL que veio no despacho.

Duas regras do contrato moram neste modulo:

- A ASSINATURA E REFEITA A CADA TENTATIVA. A janela do `x-timestamp` e de 5
  minutos; uma retentativa que reaproveitasse os cabecalhos da primeira levaria
  400 depois desse prazo (secao 3.2).
- 5xx SIGNIFICA QUE NADA FOI GRAVADO, entao retentamos (secao 3.3, regra 3).
  4xx e permanente - corpo fora do contrato ou credencial invalida - e repetir
  so gastaria tentativa. 2xx encerra, inclusive `duplicate: true` e
  `discarded`, que sao sucesso do ponto de vista da entrega.
"""

import asyncio
import json
from typing import Any, Dict, Optional

import httpx

from config import get_settings
from src.services.webhook_signature import build_signed_headers, redact_headers


def resolve_callback_url(url: str) -> str:
    """Resolve URL relativa contra o ZELLU_BACKEND_URL, como o doc-gen ja faz."""
    if url.startswith("http://") or url.startswith("https://"):
        return url
    settings = get_settings()
    base = settings.ZELLU_BACKEND_URL.rstrip("/")
    path = url if url.startswith("/") else f"/{url}"
    resolved = f"{base}{path}"
    print(f"[TURNO-CALLBACK] URL relativa detectada: {url} -> {resolved}")
    return resolved


def build_turn_callback_payload(
    turn_id: Optional[str],
    session_id: str,
    last_message_id: Optional[str],
    turn_body: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Monta o callback de turno concluido.

    Leva os MESMOS campos que o 200 sincrono devolve hoje (secao 3.2), o que
    inclui os aditivos que o backend ja conhece - `requiresHumanApproval`,
    `messagedata` com o `replyToMessageId`, e os marcadores de curto-circuito
    (`skipped`, `awaitingContext`). O que muda e so a moldura: `turnId`,
    `sessionId`, `lastMessageId` e `status`.
    """
    payload = dict(turn_body or {})
    payload.update({
        "turnId": turn_id,
        "sessionId": session_id,
        "lastMessageId": last_message_id,
        "status": "completed",
    })
    payload.setdefault("messageType", "text")
    payload.setdefault("negotiationComplete", False)
    return payload


def build_turn_failure_payload(
    turn_id: Optional[str],
    session_id: str,
    last_message_id: Optional[str],
    error: str,
    code: str,
    retryable: bool,
) -> Dict[str, Any]:
    """
    Monta o aviso de falha depois do 202 (secao 3.3, regra 2).

    Sem ele, um turno que morre do nosso lado vira silencio do lado deles: nao
    sabem se esperam ou se redespacham.
    """
    return {
        "turnId": turn_id,
        "sessionId": session_id,
        "lastMessageId": last_message_id,
        "status": "failed",
        "error": (error or "erro desconhecido")[:300],
        "code": code,
        "retryable": retryable,
    }


async def send_turn_callback(
    callback_url: str,
    payload: Dict[str, Any],
    api_key: Optional[str] = None,
) -> bool:
    """
    Entrega o resultado do turno, com retry em 5xx e em falha de transporte.

    Returns:
        True se o backend respondeu 2xx (recebido e gravado), False caso
        contrario. Nunca levanta: e chamada de background task, e derrubar a
        task so trocaria um log por um traceback perdido.
    """
    settings = get_settings()
    url = resolve_callback_url(callback_url)
    key = api_key or settings.API_KEY_ZELLU_IA
    secret = settings.AI_WEBHOOK_HMAC_SECRET

    timeout = float(getattr(settings, "COMPANY_TURN_CALLBACK_TIMEOUT_S", 30.0))
    max_attempts = int(getattr(settings, "COMPANY_TURN_CALLBACK_MAX_ATTEMPTS", 3))
    backoff = float(getattr(settings, "COMPANY_TURN_CALLBACK_BACKOFF_S", 2.0))

    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    turn_id = payload.get("turnId")
    status = payload.get("status")

    for attempt in range(1, max_attempts + 1):
        # Assinatura NOVA a cada tentativa: a janela do timestamp e de 5 min.
        headers = {"x-api-key": key, "Content-Type": "application/json"}
        if secret:
            headers.update(build_signed_headers(body, secret))
        else:
            print("[TURNO-CALLBACK] AVISO: AI_WEBHOOK_HMAC_SECRET vazio - callback sem assinatura")

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(url, content=body, headers=headers)

            if 200 <= response.status_code < 300:
                print(
                    f"[TURNO-CALLBACK] Entregue - turnId={turn_id} status={status} "
                    f"HTTP {response.status_code} {response.text[:120]}"
                )
                return True

            if response.status_code < 500:
                # Permanente: corpo fora do contrato, assinatura ou sessao.
                print(
                    f"[TURNO-CALLBACK] Recusado em definitivo - turnId={turn_id} "
                    f"HTTP {response.status_code} {response.text[:200]} "
                    f"headers={redact_headers(headers)}"
                )
                return False

            if attempt < max_attempts:
                print(
                    f"[TURNO-CALLBACK] HTTP {response.status_code} (turno NAO gravado); "
                    f"retry {attempt}/{max_attempts - 1} em {backoff:.0f}s"
                )
                await asyncio.sleep(backoff)
                backoff *= 2
                continue

            print(
                f"[TURNO-CALLBACK] Desisti apos {max_attempts} tentativas - "
                f"turnId={turn_id} HTTP {response.status_code}"
            )
            return False

        except httpx.RequestError as e:
            # TimeoutException herda de RequestError - timeout tambem retenta.
            if attempt < max_attempts:
                print(
                    f"[TURNO-CALLBACK] Falha de transporte ({type(e).__name__}); "
                    f"retry {attempt}/{max_attempts - 1} em {backoff:.0f}s"
                )
                await asyncio.sleep(backoff)
                backoff *= 2
                continue

            print(
                f"[TURNO-CALLBACK] Desisti apos {max_attempts} tentativas - "
                f"turnId={turn_id} ({type(e).__name__}: {e})"
            )
            return False

    return False
