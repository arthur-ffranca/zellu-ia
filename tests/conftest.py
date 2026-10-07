# -*- coding: utf-8 -*-
"""
Configuracao comum da suite.

GUARDA DE DESPACHO PERSISTIDA DESLIGADA POR PADRAO
--------------------------------------------------
Em producao ela nasce LIGADA (NEGOTIATION_DISPATCH_IDEMPOTENCY_ENABLED em
config.py). Aqui ela fica desligada para toda a suite, por um motivo mecanico e
nao por opiniao sobre o comportamento:

Os dubles de backend trocam `webhooks_sender.httpx.AsyncClient` no MODULO
inteiro. A consulta da guarda usa o mesmo httpx, entao ela cairia no mesmo duble
e entraria na lista de POSTs que os testes contam - e dezenas de asserts sobre
"quantas mensagens o ZelinhU despachou" passariam a falar de um POST reverso que
nunca existiu.

Os testes da propria guarda ligam a flag explicitamente
(tests/test_dispatch_idempotency.py).
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("OPENAI_API_KEY", "test-key")


@pytest.fixture(autouse=True)
def isolate_company_mongo(monkeypatch):
    """Testes unitarios nao acessam a base real configurada no .env.

    Testes do cache podem fornecer seu proprio double. Validacao real fica
    nos scripts explicitos validate_company_mongo/search.
    """
    from src.services import company_lookup_cache
    monkeypatch.setattr(company_lookup_cache, '_get_collection', lambda: None)
# config.py nao traz mais chave padrao; o cliente Pinecone exige alguma no import.
os.environ.setdefault("PINECONE_API_KEY", "test-key")
os.environ.setdefault("API_KEY_ZELLU_IA", "test-s2s-key")


@pytest.fixture(autouse=True)
def guarda_persistida_desligada():
    from config import get_settings

    settings = get_settings()
    anterior = settings.NEGOTIATION_DISPATCH_IDEMPOTENCY_ENABLED
    settings.NEGOTIATION_DISPATCH_IDEMPOTENCY_ENABLED = False
    try:
        yield
    finally:
        settings.NEGOTIATION_DISPATCH_IDEMPOTENCY_ENABLED = anterior


os.environ.setdefault("DOCUMENT_CASE_LLM_ENABLED", "false")
