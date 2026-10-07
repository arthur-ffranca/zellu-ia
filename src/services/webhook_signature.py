# -*- coding: utf-8 -*-
"""Helpers para assinatura HMAC dos webhooks enviados para o backend Zellu."""

import hashlib
import hmac
import os
import time
from typing import Dict, Optional


def build_signed_headers(raw_body: str, secret: Optional[str] = None) -> Dict[str, str]:
    """Cria headers HMAC conforme o contrato /api/ai/rag-metrics e webhooks IA."""
    secret_value = secret or os.getenv("AI_WEBHOOK_HMAC_SECRET", "")
    timestamp = str(int(time.time() * 1000))
    signature = hmac.new(
        secret_value.encode("utf-8") if secret_value else b"",
        f"{timestamp}.{raw_body}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return {
        "x-timestamp": timestamp,
        "x-signature": signature,
    }


# Headers que carregam credencial e nunca podem ir para o log. O log de
# 23/08/2026 imprimia o dicionario inteiro a cada mensagem enviada, com a
# x-api-key em texto puro.
#
# x-timestamp e x-signature NAO entram aqui de proposito: a assinatura e
# derivada do corpo, muda a cada requisicao e nao revela o segredo HMAC - e e o
# que se olha quando o backend recusa por assinatura invalida.
SENSITIVE_HEADERS = frozenset({
    "x-api-key",
    "api-key",
    "authorization",
    "x-auth-token",
})


def redact_headers(headers: Dict[str, str]) -> Dict[str, str]:
    """
    Copia dos headers com as credenciais mascaradas, para log.

    Mantem o sinal que interessa na operacao - se a credencial esta configurada
    ou nao - sem imprimir o valor.
    """
    seguros = {}
    for nome, valor in (headers or {}).items():
        if nome.lower() in SENSITIVE_HEADERS:
            seguros[nome] = "***" if valor else "(vazio)"
        else:
            seguros[nome] = valor
    return seguros
