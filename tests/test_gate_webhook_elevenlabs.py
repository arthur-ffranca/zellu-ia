# -*- coding: utf-8 -*-
"""
Testes do gate global de x-api-key na rota de pos-chamada do ElevenLabs.

POR QUE ESTE ARQUIVO EXISTE
---------------------------
O gate global e de 22/07 e vale para TODA rota, isentando so `/` e `/health`. O
pos-chamada do ElevenLabs e de 02/09 e autentica por HMAC, nao por x-api-key.
Resultado: de 02/09 a 17/09 o POST da ElevenLabs morria no gate, com 401, antes
de chegar ao handler - e e o handler que dispara o upload do audio, o
POST /api/phone/calls e o POST /api/ai/usage. Foram 41 sessoes de voz abertas em
HMG sem nenhuma chegar na Zellu.

Nenhum teste pegou isso porque todos chamavam as funcoes do modulo DIRETO. Este
aqui sobe o app de verdade e bate na rota pela rede, que e o unico jeito de ver
um erro que acontece antes do handler.

Nao faz rede externa: o webhook usado e de um tipo que o handler ignora de
propria conta, entao nada sai daqui.

Uso:
    cd C:/Users/Kaue/Downloads/zellinhu/zellinho_chat-1
    set PYTHONIOENCODING=utf-8 && python -m pytest tests/test_gate_webhook_elevenlabs.py -q
"""

import hashlib
import hmac
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("OPENAI_API_KEY", "test-key")

from fastapi.testclient import TestClient  # noqa: E402

from config import get_settings  # noqa: E402
from src.main import ROTAS_SEM_GATE_DE_CHAVE, app  # noqa: E402

ROTA = "/webhooks/elevenlabs/post-call"
SEGREDO = "wsec_de_teste"

# Tipo que o handler ignora por conta propria (ele so processa
# post_call_transcription). Serve para provar que a requisicao CHEGOU ao handler
# sem disparar upload de audio nem chamada ao backend.
CORPO = json.dumps({"type": "post_call_audio"}).encode("utf-8")


@pytest.fixture
def cliente():
    # Sem `with`: nao queremos rodar o lifespan, que inicializa Pinecone, OpenAI e
    # o resto. A rota testada aqui nao depende de nada disso.
    return TestClient(app)


@pytest.fixture
def segredo_configurado():
    """Preenche o ELEVENLABS_WEBHOOK_SECRET so durante o teste."""
    settings = get_settings()
    anterior = settings.ELEVENLABS_WEBHOOK_SECRET
    settings.ELEVENLABS_WEBHOOK_SECRET = SEGREDO
    try:
        yield SEGREDO
    finally:
        settings.ELEVENLABS_WEBHOOK_SECRET = anterior


def _assinar(corpo: bytes, segredo: str, momento: int = None) -> str:
    """Header elevenlabs-signature, no formato que a ElevenLabs manda."""
    momento = momento if momento is not None else int(time.time())
    assinatura = hmac.new(
        segredo.encode("utf-8"),
        f"{momento}.{corpo.decode('utf-8')}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"t={momento},v0={assinatura}"


# =============================================================================
# O 401 QUE CUSTOU 41 LIGACOES
# =============================================================================

def test_webhook_assinado_passa_sem_x_api_key(cliente, segredo_configurado):
    """O caso que estava quebrado: a ElevenLabs nao manda x-api-key."""

    resposta = cliente.post(
        ROTA,
        content=CORPO,
        headers={
            "content-type": "application/json",
            "elevenlabs-signature": _assinar(CORPO, segredo_configurado),
        },
    )

    assert resposta.status_code != 401, (
        "o gate global voltou a barrar o webhook do ElevenLabs - "
        "nenhuma ligacao chega na Zellu"
    )
    assert resposta.status_code == 200
    # Prova que chegou ao handler: so ele sabe ignorar por tipo.
    assert resposta.json() == {"success": True, "ignorado": "post_call_audio"}


def test_rota_esta_na_lista_de_isencao():
    assert ROTA in ROTAS_SEM_GATE_DE_CHAVE


# =============================================================================
# A ISENCAO NAO PODE VIRAR ROTA ABERTA
# =============================================================================

def test_assinatura_errada_e_recusada(cliente, segredo_configurado):
    resposta = cliente.post(
        ROTA,
        content=CORPO,
        headers={
            "content-type": "application/json",
            "elevenlabs-signature": "t=%d,v0=deadbeef" % int(time.time()),
        },
    )

    assert resposta.status_code == 401


def test_sem_assinatura_nenhuma_e_recusado(cliente, segredo_configurado):
    resposta = cliente.post(
        ROTA, content=CORPO, headers={"content-type": "application/json"}
    )

    assert resposta.status_code == 401


def test_assinatura_velha_e_recusada(cliente, segredo_configurado):
    """Replay de um POST antigo, fora da janela de 30 min."""

    velho = int(time.time()) - 3600

    resposta = cliente.post(
        ROTA,
        content=CORPO,
        headers={
            "content-type": "application/json",
            "elevenlabs-signature": _assinar(CORPO, segredo_configurado, momento=velho),
        },
    )

    assert resposta.status_code == 401


def test_sem_segredo_no_ambiente_a_rota_recusa_tudo(cliente):
    """Sem ELEVENLABS_WEBHOOK_SECRET a rota nao autentica nada - entao recusa.

    Isentar do gate sem exigir o segredo trocaria um 401 por uma rota aberta.
    """

    settings = get_settings()
    anterior = settings.ELEVENLABS_WEBHOOK_SECRET
    settings.ELEVENLABS_WEBHOOK_SECRET = ""

    try:
        resposta = cliente.post(
            ROTA,
            content=CORPO,
            headers={
                "content-type": "application/json",
                "elevenlabs-signature": _assinar(CORPO, "qualquer-segredo"),
            },
        )
    finally:
        settings.ELEVENLABS_WEBHOOK_SECRET = anterior

    assert resposta.status_code == 401


# =============================================================================
# O GATE CONTINUA VALENDO PARA O RESTO
# =============================================================================

def test_as_outras_rotas_continuam_exigindo_a_chave(cliente):
    """A isencao vale para uma rota so, e nao abriu o servico."""

    resposta = cliente.post("/api/company/base-dados", json={})

    assert resposta.status_code == 401


def test_health_continua_aberta(cliente):
    assert cliente.get("/health").status_code == 200
