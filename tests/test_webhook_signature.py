import json
import os

os.environ.setdefault("OPENAI_API_KEY", "dummy")

from src.services.webhook_signature import build_signed_headers, redact_headers


def test_build_signed_headers_uses_timestamp_and_signature():
    body = json.dumps({"hello": "world"}, ensure_ascii=False, separators=(",", ":"))
    headers = build_signed_headers(body, secret="secret")

    assert headers["x-timestamp"].isdigit()
    assert len(headers["x-signature"]) == 64
    assert headers["x-signature"] == __import__("hmac").new(
        b"secret",
        f"{headers['x-timestamp']}.{body}".encode("utf-8"),
        __import__("hashlib").sha256,
    ).hexdigest()


# =============================================================================
# REDACAO DE CREDENCIAL NO LOG (23/08/2026)
# =============================================================================
# O log de producao imprimia o dicionario de headers inteiro a cada mensagem
# enviada ao backend, com a x-api-key em texto puro.

CHAVE = "super-secreta-nao-pode-vazar"


def test_api_key_nao_aparece_no_log():
    saida = redact_headers({"Content-Type": "application/json", "x-api-key": CHAVE})

    assert CHAVE not in str(saida), "a chave vazou no header redigido"
    assert saida["x-api-key"] == "***"


def test_redacao_nao_depende_da_caixa_do_nome():
    saida = redact_headers({"X-API-Key": CHAVE, "Authorization": f"Bearer {CHAVE}"})

    assert CHAVE not in str(saida)


def test_credencial_vazia_e_distinguivel_de_configurada():
    """O log precisa continuar respondendo 'a chave esta setada?'."""
    assert redact_headers({"x-api-key": ""})["x-api-key"] == "(vazio)"
    assert redact_headers({"x-api-key": CHAVE})["x-api-key"] == "***"


def test_assinatura_e_timestamp_continuam_visiveis():
    """
    Nao sao credencial: a assinatura e derivada do corpo, muda a cada
    requisicao e e o que se olha quando o backend recusa por assinatura
    invalida.
    """
    headers = build_signed_headers("{}", secret="secret")
    saida = redact_headers(headers)

    assert saida["x-signature"] == headers["x-signature"]
    assert saida["x-timestamp"] == headers["x-timestamp"]


def test_redacao_nao_altera_o_dicionario_original():
    """Os headers redigidos vao para o log; os reais seguem no request."""
    originais = {"x-api-key": CHAVE}
    redact_headers(originais)

    assert originais["x-api-key"] == CHAVE, "os headers do request foram mascarados"


def test_headers_vazios_nao_quebram():
    assert redact_headers({}) == {}
    assert redact_headers(None) == {}
