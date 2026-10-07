# -*- coding: utf-8 -*-
"""
Testes da tool de validacao de idade (POST /phone/age-check).

A barreira de menor de idade saiu do julgamento do LLM e virou codigo. Estes
testes cobrem a fronteira que importa: quem faz 18 hoje entra, quem faz amanha
nao.

Nao faz rede. Chama as funcoes puras do modulo e o handler direto.

Uso:
    cd C:/Users/Kaue/Downloads/zellinhu/zellinho_chat-1
    set PYTHONIOENCODING=utf-8 && python tests/test_age_check.py
"""

import asyncio
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("OPENAI_API_KEY", "test-key")

from agents.conversational_agent.age_check import (  # noqa: E402
    IDADE_MINIMA,
    AgeCheckRequest,
    calcular_idade,
    hoje_no_brasil,
    parse_data_de_nascimento,
    verificar_idade,
)


def _checar(birth_date):
    """Roda o handler como a ElevenLabs rodaria."""
    return asyncio.run(
        verificar_idade(AgeCheckRequest(birthDate=birth_date), api_key="ok")
    )


def _nascimento_para_idade(anos, dias_de_folga=0):
    """Data de nascimento de quem completa `anos` hoje, deslocada em dias."""
    hoje = hoje_no_brasil()
    try:
        aniversario = hoje.replace(year=hoje.year - anos)
    except ValueError:  # 29 de fevereiro
        aniversario = hoje.replace(year=hoje.year - anos, day=28)
    return aniversario + timedelta(days=dias_de_folga)


# =============================================================================
# PARSE
# =============================================================================

def test_aceita_iso_e_o_formato_brasileiro():
    assert parse_data_de_nascimento("1990-05-12") == date(1990, 5, 12)
    assert parse_data_de_nascimento("12/05/1990") == date(1990, 5, 12)
    assert parse_data_de_nascimento("12-05-1990") == date(1990, 5, 12)
    assert parse_data_de_nascimento("  1990-05-12  ") == date(1990, 5, 12)


def test_recusa_o_que_nao_e_data():
    for entrada in ("", "   ", "doze de maio", "1990", "05/1990", "abacaxi", "1990-13-01", "1990-02-30"):
        assert parse_data_de_nascimento(entrada) is None, entrada


def test_nao_confunde_dia_com_mes():
    # 03/04/1990 e 3 de abril no Brasil, nao 4 de marco.
    assert parse_data_de_nascimento("03/04/1990") == date(1990, 4, 3)


# =============================================================================
# CONTA DA IDADE
# =============================================================================

def test_idade_de_quem_ainda_nao_fez_aniversario():
    # Nasceu em dezembro, hoje e junho: ainda tem um ano a menos.
    assert calcular_idade(date(2000, 12, 31), date(2026, 6, 15)) == 25
    assert calcular_idade(date(2000, 1, 1), date(2026, 6, 15)) == 26


def test_no_dia_do_aniversario_a_idade_ja_conta():
    assert calcular_idade(date(2008, 6, 15), date(2026, 6, 15)) == 18


# =============================================================================
# A FRONTEIRA DOS 18
# =============================================================================

def test_quem_faz_18_hoje_entra():
    resposta = _checar(_nascimento_para_idade(IDADE_MINIMA).isoformat())
    assert resposta["isAdult"] is True, resposta
    assert resposta["age"] == IDADE_MINIMA


def test_quem_faz_18_amanha_nao_entra():
    # Um dia de diferenca e a unica coisa que separa os dois casos.
    resposta = _checar(_nascimento_para_idade(IDADE_MINIMA, dias_de_folga=1).isoformat())
    assert resposta["isAdult"] is False, resposta
    assert resposta["age"] == IDADE_MINIMA - 1


def test_adulto_folgado_entra():
    resposta = _checar("1990-05-12")
    assert resposta["isAdult"] is True
    assert resposta["age"] >= 30


def test_crianca_nao_entra():
    resposta = _checar(_nascimento_para_idade(10).isoformat())
    assert resposta["isAdult"] is False
    assert resposta["age"] == 10


# =============================================================================
# DATA QUE NAO SERVE: invalidDate, e NAO isAdult false
# =============================================================================
# A diferenca muda o que o agente faz: invalidDate pergunta de novo, isAdult
# false encerra a ligacao. Confundir os dois desligaria na cara de um adulto.

def test_data_irreconhecivel_volta_invalid_e_nao_reprova():
    resposta = _checar("nao sei")
    assert resposta.get("invalidDate") is True
    assert "isAdult" not in resposta


def test_data_no_futuro_volta_invalid():
    amanha = hoje_no_brasil() + timedelta(days=1)
    resposta = _checar(amanha.isoformat())
    assert resposta.get("invalidDate") is True
    assert "isAdult" not in resposta


def test_ano_improvavel_volta_invalid():
    resposta = _checar("1200-05-12")
    assert resposta.get("invalidDate") is True


def test_resposta_boa_devolve_a_data_normalizada():
    resposta = _checar("12/05/1990")
    assert resposta["birthDate"] == "1990-05-12"


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
