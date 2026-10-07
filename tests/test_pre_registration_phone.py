# -*- coding: utf-8 -*-
"""
Testes da emenda de telefone do pre-registro.

Contrato: user-exists-webhook-spec.md, emenda de 2026-09-04
  - phone irreconhecivel  -> 400 (antes: 201 com conta sem telefone)
  - phone de outra conta   -> 409 com phoneInUse: true (antes: 500 generico)
  - nos dois casos NAO ha conta criada; a IA pede o numero de novo na conversa
    e NAO envia o webhook de userExists

Nao faz rede: o httpx.AsyncClient e substituido por um fake com resposta
programada.

Uso:
    cd C:\\Users\\Kaue\\Downloads\\zellinhu\\zellinho_chat-1
    set PYTHONIOENCODING=utf-8 && python tests\\test_pre_registration_phone.py

    # ou, se usar pytest:
    pytest tests/test_pre_registration_phone.py -v
"""

import asyncio
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# config.Settings exige a chave da OpenAI so para instanciar; nenhum teste
# aqui chama a OpenAI.
os.environ.setdefault("OPENAI_API_KEY", "test-key")

import httpx  # noqa: E402

from src.utils.phone import (  # noqa: E402
    PHONE_FORM_PATTERN,
    is_valid_phone,
    normalize_phone,
)


class _Skip(Exception):
    """Teste pulado (dependencia ausente)."""


def _skip(motivo):
    raise _Skip(motivo)


try:
    from src.pre_registration_client import PreRegistrationClient
    from src.services.company_intake import CompanyIntakeService
    _IMPORTAVEL, _ERRO_IMPORT = True, None
except Exception as e:  # pragma: no cover - ambiente sem dependencias
    _IMPORTAVEL, _ERRO_IMPORT = False, f"{type(e).__name__}: {e}"


# =============================================================================
# DUBLES
# =============================================================================

class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """Substitui httpx.AsyncClient com uma resposta fixa e registra as chamadas."""

    chamadas = []
    resposta = _FakeResponse(201, {"success": True, "data": {"id": "u1", "username": "joao"}})

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, json=None, headers=None):
        _FakeAsyncClient.chamadas.append({"url": url, "json": json})
        return _FakeAsyncClient.resposta


def _com_resposta(status_code, payload):
    """Programa a resposta do backend e zera o registro de chamadas."""
    _FakeAsyncClient.chamadas = []
    _FakeAsyncClient.resposta = _FakeResponse(status_code, payload)


def _criar(phone="(11) 99999-9999"):
    """Roda PreRegistrationClient.create com o httpx trocado pelo fake."""
    cliente = PreRegistrationClient(base_url="http://backend.local", api_key="k")
    original = httpx.AsyncClient
    httpx.AsyncClient = _FakeAsyncClient
    try:
        return asyncio.run(cliente.create(
            email="joao@email.com",
            first_name="Joao",
            last_name="Silva",
            phone=phone,
            session_id="sessao-1"
        ))
    finally:
        httpx.AsyncClient = original


def _state_visitante(phone="(11) 99999-9999"):
    return {
        "chat_id": "sessao-1",
        "client_email": "joao@email.com",
        "client_name": "Joao Silva",
        "first_name": "Joao",
        "last_name": "Silva",
        "client_phone": phone,
        "email_validated": True,
        "email_exists": False,
        "messages": [],
        "step": 0,
    }


# =============================================================================
# FORMATO DO TELEFONE (src/utils/phone.py)
# =============================================================================

def test_mascarado_e_ddi_continuam_valendo():
    """A emenda garante que o que a IA ja enviava continua aceito."""
    for numero in ["(11) 99999-9999", "+5511999999999", "11999999999",
                   "+55 11 99999-9999", "(11) 3333-4444", "1133334444"]:
        assert is_valid_phone(numero), numero


def test_celular_sem_o_nove_e_recusado():
    """O caso que antes virava conta sem telefone, calado."""
    assert is_valid_phone("(11) 9999-9999") is False


def test_numero_curto_ddd_invalido_e_texto_sao_recusados():
    for numero in ["119999999", "(01) 99999-9999", "1099999999", "meu telefone", "", None]:
        assert is_valid_phone(numero) is False, numero


def test_fixo_nao_pode_comecar_com_9_nem_com_1():
    assert is_valid_phone("1193334444") is False
    assert is_valid_phone("1113334444") is False


def test_ddi_55_nao_come_o_ddd_55():
    """55 so e codigo de pais quando sobra telefone inteiro depois dele."""
    assert normalize_phone("55999999999") == "55999999999"   # DDD 55 (RS)
    assert normalize_phone("5511999999999") == "11999999999"  # DDI + DDD 11


def test_pattern_do_form_bate_com_a_validacao():
    """O front tem que recusar exatamente o que o backend recusa."""
    aceitos = ["(11) 99999-9999", "+5511999999999", "11999999999", "(11) 3333-4444"]
    recusados = ["(11) 9999-9999", "(01) 99999-9999", "119999999", "abc"]
    for numero in aceitos:
        assert re.match(PHONE_FORM_PATTERN, numero), numero
    for numero in recusados:
        assert not re.match(PHONE_FORM_PATTERN, numero), numero


# =============================================================================
# CLIENTE DO PRE-REGISTRO (src/pre_registration_client.py)
# =============================================================================

def test_telefone_invalido_nem_chega_no_backend():
    if not _IMPORTAVEL:
        _skip(_ERRO_IMPORT)
    _com_resposta(201, {"success": True, "data": {"id": "u1"}})
    r = _criar(phone="(11) 9999-9999")
    assert r.success is False
    assert r.phone_invalid is True
    assert _FakeAsyncClient.chamadas == []


def test_criacao_normal_continua_funcionando():
    if not _IMPORTAVEL:
        _skip(_ERRO_IMPORT)
    _com_resposta(201, {"success": True, "data": {"id": "u1", "username": "joao"}})
    r = _criar()
    assert r.success is True
    assert r.user_id == "u1"
    assert r.phone_invalid is False and r.phone_in_use is False
    assert len(_FakeAsyncClient.chamadas) == 1


def test_400_de_telefone_marca_phone_invalid():
    if not _IMPORTAVEL:
        _skip(_ERRO_IMPORT)
    _com_resposta(400, {"success": False, "error": "Telefone invalido. Use DDD + 9 digitos"})
    r = _criar()
    assert r.success is False
    assert r.phone_invalid is True


def test_400_de_outro_campo_nao_pede_telefone():
    if not _IMPORTAVEL:
        _skip(_ERRO_IMPORT)
    _com_resposta(400, {"success": False, "error": "firstName e obrigatorio"})
    r = _criar()
    assert r.success is False
    assert r.phone_invalid is False


def test_409_com_phone_in_use_nao_e_sucesso():
    """O ponto critico: antes isso virava 'pre-registro ok' e a conta nunca nascia."""
    if not _IMPORTAVEL:
        _skip(_ERRO_IMPORT)
    _com_resposta(409, {"success": False, "phoneInUse": True,
                        "error": "Telefone ja cadastrado em outra conta"})
    r = _criar()
    assert r.success is False
    assert r.phone_in_use is True
    assert r.phone_invalid is False


def test_409_de_email_continua_sendo_tratado_como_sucesso():
    """Comportamento antigo preservado para o conflito de e-mail."""
    if not _IMPORTAVEL:
        _skip(_ERRO_IMPORT)
    _com_resposta(409, {"success": False, "userId": "u9", "error": "Usuario ja existe"})
    r = _criar()
    assert r.success is True
    assert r.user_id == "u9"
    assert r.phone_in_use is False


# =============================================================================
# AGENTE (agents/intake_agent.py)
# =============================================================================

















# =============================================================================
# RUNNER STANDALONE
# =============================================================================

def _run_all():
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]

    passed, skipped, failed = 0, 0, []
    for name, fn in tests:
        try:
            fn()
            passed += 1
            print(f"  [OK]   {name}")
        except _Skip as e:
            skipped += 1
            print(f"  [SKIP] {name}: {e}")
        except AssertionError as e:
            failed.append(name)
            print(f"  [FALHA] {name}: {e}")
        except Exception as e:
            failed.append(name)
            print(f"  [ERRO] {name}: {type(e).__name__}: {e}")

    print("\n" + "=" * 60)
    print(f"Total: {len(tests)} | OK: {passed} | Pulados: {skipped} | Falhas: {len(failed)}")
    print("=" * 60)
    return 1 if failed else 0


if __name__ == "__main__":
    print("=" * 60)
    print("TELEFONE NO PRE-REGISTRO - EMENDA 20260904")
    print("=" * 60)
    sys.exit(_run_all())
