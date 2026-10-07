# -*- coding: utf-8 -*-
"""
Testes da identidade por telefone na ligacao (Emenda 6).

Cobrem o que a emenda cobra e o que ela deixou em aberto:

- os seis ramos do lookup e os quatro do verify chegam ao agente como `outcome`;
- o `challengeId` e o `userId` NUNCA saem para o modelo;
- a prova aprovada vira `phoneVerificationId` no POST /api/phone/calls, e so ela:
  telefone diferente, desafio nao aprovado ou prova vencida nao valem;
- falha nossa (Zellu muda) responde `unchecked`, que o roteiro trata seguindo a
  ligacao - nunca encerrando.

Nao faz rede: troca a funcao que fala com a Zellu por um duble.

Uso:
    cd C:/Users/Kaue/Downloads/zellinhu/zellinho_chat-1
    set PYTHONIOENCODING=utf-8 && python tests/test_phone_identity.py
"""

import asyncio
import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("OPENAI_API_KEY", "test-key")

from agents.conversational_agent import phone_identity  # noqa: E402
from agents.conversational_agent.phone_identity import (  # noqa: E402
    VALIDADE_DA_PROVA,
    LookupRequest,
    VerifyRequest,
    challenge_aprovado,
    confirmar_codigo,
    consultar_telefone,
)
from agents.conversational_agent.post_call import montar_payload  # noqa: E402

TELEFONE = "(11) 99999-1111"
DIGITOS = "11999991111"


# =============================================================================
# DUBLES
# =============================================================================

def _responder(resposta, registro=None):
    """Troca a conversa com a Zellu por uma resposta fixa."""

    async def duble(caminho, corpo, timeout):
        if registro is not None:
            registro.append((caminho, corpo))
        return resposta

    phone_identity._chamar_zellu = duble


def _limpar():
    phone_identity._desafios.clear()


def _lookup(telefone=TELEFONE):
    return asyncio.run(consultar_telefone(LookupRequest(phone=telefone), api_key="ok"))


def _verify(code="123456", telefone=TELEFONE):
    return asyncio.run(
        confirmar_codigo(VerifyRequest(phone=telefone, code=code), api_key="ok")
    )


def _ligacao_completa(telefone=DIGITOS):
    """Dados de uma ligacao que foi ate o fim, como o pos-chamada monta."""
    from datetime import datetime, timezone

    inicio = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)

    return {
        "conversationId": "conv_abc123",
        "agentId": "agent_1",
        "callerPhone": telefone,
        # Ligacao pelo telefone: nao ha dynamic_variables, entao os dois campos
        # da chamada pelo app saem vazios (ver post_call.extrair_dados).
        "origin": "",
        "userId": "",
        "direction": "inbound",
        "inicio": inicio,
        "fim": inicio,
        "duracao": 120,
        "endReason": "completed",
        "consentAccepted": True,
        "consentAcceptedAt": "2026-09-17T12:00:10Z",
        "declaredAdultAt": None,
        "firstName": "Joao",
        "lastName": "Silva",
        "email": "",
        "phone": telefone,
        "summary": "Voce relatou que a loja nao devolveu o dinheiro.",
        "transcricao": [],
    }


# =============================================================================
# LOOKUP
# =============================================================================

def test_numero_sem_digito_nao_vai_para_a_zellu():
    _limpar()
    chamadas = []
    _responder({"outcome": "challenge", "challengeId": "ch_1"}, chamadas)

    assert _lookup("nao sei") == {"outcome": "invalid_format"}
    assert chamadas == [], "invalid_format local nao pode gastar um lookup da Zellu"


def test_lookup_manda_so_os_digitos():
    _limpar()
    chamadas = []
    _responder({"outcome": "challenge", "challengeId": "ch_1"}, chamadas)

    _lookup("+55 (11) 99999-1111")

    caminho, corpo = chamadas[0]
    assert caminho == "/api/ai/phone-identity/lookup"
    assert corpo == {"phone": DIGITOS}


def test_challenge_nao_devolve_o_challenge_id_ao_agente():
    _limpar()
    _responder({"outcome": "challenge", "challengeId": "ch_1"})

    resposta = _lookup()

    assert resposta == {"outcome": "challenge"}
    assert "challengeId" not in resposta


def test_os_ramos_que_encerram_chegam_inteiros_ao_agente():
    for outcome in ("in_use", "unavailable", "sms_failed", "rate_limited", "invalid_format"):
        _limpar()
        _responder({"outcome": outcome})
        assert _lookup() == {"outcome": outcome}, outcome


def test_zellu_muda_no_lookup_vira_unchecked():
    _limpar()
    _responder(None)

    # unchecked, e nao sms_failed: o roteiro encerra a ligacao no sms_failed e
    # segue no unchecked. Confundir os dois custa o relato da pessoa.
    assert _lookup() == {"outcome": "unchecked"}


def test_challenge_sem_id_vira_unchecked():
    _limpar()
    _responder({"outcome": "challenge"})

    assert _lookup() == {"outcome": "unchecked"}
    assert challenge_aprovado(DIGITOS) is None


def test_lookup_novo_substitui_o_desafio_anterior():
    _limpar()
    _responder({"outcome": "challenge", "challengeId": "ch_1"})
    _lookup()
    _responder({"outcome": "challenge", "challengeId": "ch_2"})
    _lookup()

    _responder({"outcome": "verified"})
    _verify()

    assert challenge_aprovado(DIGITOS) == "ch_2"


# =============================================================================
# VERIFY
# =============================================================================

def test_verify_sem_desafio_aberto_e_invalid():
    _limpar()
    chamadas = []
    _responder({"outcome": "verified"}, chamadas)

    assert _verify() == {"outcome": "invalid"}
    assert chamadas == [], "sem challengeId nao ha o que perguntar a Zellu"


def test_verify_manda_o_challenge_id_guardado():
    _limpar()
    _responder({"outcome": "challenge", "challengeId": "ch_1"})
    _lookup()

    chamadas = []
    _responder({"outcome": "verified"}, chamadas)
    _verify(code=" 123456 ")

    caminho, corpo = chamadas[0]
    assert caminho == "/api/ai/phone-identity/verify"
    assert corpo == {"challengeId": "ch_1", "code": "123456"}


def test_found_devolve_o_nome_e_esconde_o_user_id():
    _limpar()
    _responder({"outcome": "challenge", "challengeId": "ch_1"})
    _lookup()
    _responder({
        "outcome": "found",
        "userId": "user_segredo",
        "firstName": "Joao",
        "lastName": "Silva",
    })

    resposta = _verify()

    assert resposta == {"outcome": "found", "firstName": "Joao", "lastName": "Silva"}
    assert "userId" not in resposta


def test_invalid_e_locked_nao_aprovam_a_prova():
    for outcome in ("invalid", "locked"):
        _limpar()
        _responder({"outcome": "challenge", "challengeId": "ch_1"})
        _lookup()
        _responder({"outcome": outcome})

        assert _verify() == {"outcome": outcome}
        assert challenge_aprovado(DIGITOS) is None, outcome


def test_zellu_muda_no_verify_vira_unchecked():
    _limpar()
    _responder({"outcome": "challenge", "challengeId": "ch_1"})
    _lookup()
    _responder(None)

    assert _verify() == {"outcome": "unchecked"}
    assert challenge_aprovado(DIGITOS) is None


# =============================================================================
# A PROVA ATE O FIM DA LIGACAO
# =============================================================================

def test_prova_aprovada_vale_para_o_telefone_mascarado_tambem():
    _limpar()
    _responder({"outcome": "challenge", "challengeId": "ch_1"})
    _lookup()
    _responder({"outcome": "verified"})
    _verify()

    assert challenge_aprovado("+55 (11) 99999-1111") == "ch_1"
    assert challenge_aprovado("11988882222") is None


def test_prova_vencida_nao_vale():
    _limpar()
    _responder({"outcome": "challenge", "challengeId": "ch_1"})
    _lookup()
    _responder({"outcome": "verified"})
    _verify()

    phone_identity._desafios[DIGITOS]["em"] -= VALIDADE_DA_PROVA + timedelta(minutes=1)

    assert challenge_aprovado(DIGITOS) is None


def test_payload_leva_o_phone_verification_id():
    _limpar()
    _responder({"outcome": "challenge", "challengeId": "ch_1"})
    _lookup()
    _responder({"outcome": "found", "userId": "u1"})
    _verify()

    payload = montar_payload(_ligacao_completa())

    assert payload["phoneVerificationId"] == "ch_1"
    assert payload["contact"]["phone"] == DIGITOS


def test_sem_confirmacao_o_payload_sai_como_sempre_saiu():
    _limpar()

    payload = montar_payload(_ligacao_completa())

    # Enquanto a Zellu nao ligar a exigencia, mandar ou nao o campo da no mesmo -
    # e por isso que a emenda sobe sem coordenacao de horario com eles.
    assert "phoneVerificationId" not in payload
    assert payload["contact"]["phone"] == DIGITOS


def test_prova_de_outro_numero_nao_entra_no_payload():
    _limpar()
    _responder({"outcome": "challenge", "challengeId": "ch_1"})
    _lookup()
    _responder({"outcome": "verified"})
    _verify()

    payload = montar_payload(_ligacao_completa(telefone="11988882222"))

    assert "phoneVerificationId" not in payload


def test_ligacao_encerrada_no_telefone_sai_so_como_auditoria():
    _limpar()

    dados = _ligacao_completa()
    # Os quatro ramos que encerram acontecem ANTES do relato.
    dados["endReason"] = "failed"
    dados["summary"] = ""

    payload = montar_payload(dados, audio_url="https://storage/x.mp3")

    # Aprovada sem summary o backend devolve 400 e a ligacao sumiria do registro.
    assert payload["endReason"] == "failed"
    assert "summary" not in payload
    assert "contact" not in payload
    assert "audioUrl" not in payload


# =============================================================================
# RUNNER
# =============================================================================

def main():
    testes = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    ok = falhas = 0

    for nome, funcao in testes:
        try:
            funcao()
            print(f"  [OK]   {nome}")
            ok += 1
        except AssertionError as e:
            print(f"  [FALHA] {nome}: {e}")
            falhas += 1
        except Exception as e:
            print(f"  [ERRO] {nome}: {type(e).__name__}: {e}")
            falhas += 1

    print("\n" + "=" * 60)
    print(f"Total: {len(testes)} | OK: {ok} | Falhas: {falhas}")
    print("=" * 60)
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
