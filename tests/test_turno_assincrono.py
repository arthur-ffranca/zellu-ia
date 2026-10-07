# -*- coding: utf-8 -*-
"""
Testes do turno assincrono da IA da empresa.

Contrato: 20260911-turno-assincrono-ia-da-empresa.md

O QUE ESTES TESTES IMPEDEM
--------------------------
A Cloudflare corta /api/company/base-dados em ~125s com 524. Medido pelo
backend: 125,04 / 125,06 / 125,13 / 125,43s (25/08) e 125,1s (31/08). Em 31/08
um turno levou 127,6s e MORREU - a negociacao so voltou a andar quando o cron
a resgatou, 10 minutos depois.

Com `callbackUrl` no payload, o turno responde 202 na hora e entrega o
resultado por callback assinado. Sem `callbackUrl`, NADA muda: 200 sincrono.

Os tres buracos que estes testes cobrem, todos ja custaram regressao na
integracao com o Sender:
  1. tratar o async como opcional e quebrar o caminho sincrono de hoje;
  2. um turno que morre depois do 202 virar silencio (sem `status: "failed"`);
  3. reaproveitar a assinatura HMAC entre tentativas (janela de 5 min -> 400).

Uso:
    cd C:\\Users\\Kaue\\Downloads\\zellinhu\\zellinho_chat-1
    set PYTHONIOENCODING=utf-8 && python tests\\test_turno_assincrono.py

    # ou, se usar pytest:
    pytest tests/test_turno_assincrono.py -v
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("OPENAI_API_KEY", "sk-test-nao-usada")
os.environ.setdefault("AI_USAGE_ENABLED", "false")

from starlette.responses import JSONResponse  # noqa: E402

from src import main  # noqa: E402
from src.services import company_turn_callback as turno_callback  # noqa: E402


# =============================================================================
# APOIO
# =============================================================================

def _payload(callback_url="https://zellu/api/ai-service/company-turn-callback", **extra):
    corpo = {
        "sessionId": "sess_1",
        "lastMessageId": "msg_zelinhu_1",
        "turnId": "turn_a",
        "context": {"ticket": {"id": "t1"}, "client": {"name": "Joao"}, "company": {"name": "Loja"}},
        "messageHistory": [],
    }
    if callback_url is not None:
        corpo["callbackUrl"] = callback_url
    corpo.update(extra)
    return corpo


class _TurnoFake:
    """Substitui _resolver_e_processar_turno contando quantas vezes rodou."""

    def __init__(self, delay=0.0, resposta=None, erro=None):
        self.chamadas = 0
        self.delay = delay
        self.resposta = resposta
        self.erro = erro

    async def __call__(self, raw_body):
        self.chamadas += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.erro:
            raise self.erro
        if self.resposta is not None:
            return self.resposta
        return JSONResponse(content={
            "messageBody": "Nossa posicao e...",
            "messageType": "text",
            "tokensUsed": 812,
            "modelUsed": "gpt-4o-mini",
            "negotiationComplete": False,
            "messagedata": {"replyToMessageId": "msg_zelinhu_1"},
        })


class _CallbackFake:
    """Substitui send_turn_callback guardando o que teria sido entregue."""

    def __init__(self):
        self.enviados = []

    async def __call__(self, callback_url, payload, api_key=None):
        self.enviados.append({"url": callback_url, "payload": payload, "api_key": api_key})
        return True


def _rodar(turno=None, callback=None, corpo=None):
    """Executa `corpo()` com o turno e o envio de callback trocados por duplos."""
    turno = turno or _TurnoFake()
    callback = callback or _CallbackFake()

    orig_turno = main._resolver_e_processar_turno
    orig_callback = turno_callback.send_turn_callback

    main._resolver_e_processar_turno = turno
    turno_callback.send_turn_callback = callback
    try:
        resultado = asyncio.run(corpo())
    finally:
        main._resolver_e_processar_turno = orig_turno
        turno_callback.send_turn_callback = orig_callback

    return resultado, turno, callback


def _corpo_json(response):
    return json.loads(bytes(response.body))


class _RequestFake:
    """
    Substitui o Request do FastAPI: o endpoint so le o corpo JSON.

    Nas chamadas diretas passamos `x_api_key=None`: fora do FastAPI, o default
    do parametro e o objeto Header(), que e truthy e cairia no 401.
    """

    def __init__(self, corpo):
        self._corpo = corpo

    async def json(self):
        return self._corpo


# =============================================================================
# CENARIO 5 - SEM callbackUrl, TUDO COMO HOJE
# =============================================================================

def test_sem_callback_url_o_endpoint_responde_200_sincrono():
    """
    O interruptor e a presenca do callbackUrl (secao 3.1). Sem ele, a resposta
    continua saindo no corpo do 200, como sempre saiu.
    """
    async def corpo():
        return await main.empresa_base_dados(_RequestFake(_payload(callback_url=None)), x_api_key=None)

    resposta, turno, callback = _rodar(corpo=corpo)

    assert resposta.status_code == 200, "sem callbackUrl nada muda"
    assert _corpo_json(resposta)["messageBody"] == "Nossa posicao e..."
    assert turno.chamadas == 1
    assert callback.enviados == [], "sem callbackUrl nao existe callback"


def test_com_callback_url_o_endpoint_responde_202():
    async def corpo():
        return await main.empresa_base_dados(_RequestFake(_payload()), x_api_key=None)

    resposta, turno, callback = _rodar(corpo=corpo)

    assert resposta.status_code == 202
    assert _corpo_json(resposta)["accepted"] is True


def test_action_company_turn_tambem_e_turno():
    """O discriminador explicito do §5, pergunta 1, se eles preferirem manda-lo."""
    async def corpo():
        return await main.empresa_base_dados(_RequestFake(_payload(action="company_turn")), x_api_key=None)

    resposta, _, _ = _rodar(corpo=corpo)
    assert resposta.status_code == 202


def test_geracao_de_documento_nao_passa_pelo_turno_assincrono():
    """
    `generate_legal_document` chega na MESMA URL e ja tem o callback dele. Ele
    nao pode ser confundido com um turno de negociacao.
    """
    async def corpo():
        return await main.empresa_base_dados(_RequestFake({
            "action": "generate_legal_document",
            "ticketId": "t1",
            "callbackUrl": "https://zellu/api/ai-service/document-generation-callback",
        }), x_api_key=None)

    resposta, turno, callback = _rodar(corpo=corpo)

    assert resposta.status_code != 202, "o doc-gen tem o fluxo dele"
    assert turno.chamadas == 0, "nao e turno de negociacao"
    assert callback.enviados == [], "nao sai callback de turno"


def test_flag_desligada_volta_ao_sincrono():
    """
    A flag existe so para NOS voltarmos ao sincrono sem depender de deploy do
    backend - o interruptor oficial continua sendo o callbackUrl deles.
    """
    anterior = main.settings.COMPANY_TURN_ASYNC_ENABLED
    main.settings.COMPANY_TURN_ASYNC_ENABLED = False

    async def corpo():
        return await main.empresa_base_dados(_RequestFake(_payload()), x_api_key=None)

    try:
        resposta, turno, callback = _rodar(corpo=corpo)
    finally:
        main.settings.COMPANY_TURN_ASYNC_ENABLED = anterior

    assert resposta.status_code == 200
    assert callback.enviados == []


# =============================================================================
# CENARIO 1 - TURNO NORMAL: 202 IMEDIATO + CALLBACK COM O RESULTADO
# =============================================================================

def test_despacho_com_callback_url_responde_202_na_hora():
    demorado = _TurnoFake(delay=0.2)

    async def corpo():
        resposta = await main._aceitar_turno_assincrono(_payload(), "https://zellu/cb")
        # O 202 sai ANTES de o turno terminar: e o ponto todo do contrato.
        em_voo = demorado.chamadas
        await asyncio.sleep(0.3)
        return resposta, em_voo

    (resposta, em_voo), turno, callback = _rodar(turno=demorado, corpo=corpo)

    assert resposta.status_code == 202, "o contrato pede 202, nao 200"
    assert _corpo_json(resposta)["turnId"] == "turn_a", "o turnId volta ecoado"
    assert em_voo == 0, "o 202 nao pode esperar o LLM"
    assert len(callback.enviados) == 1, "o resultado tem de chegar por callback"


def test_callback_leva_os_mesmos_campos_do_200_de_hoje():
    async def corpo():
        await main._aceitar_turno_assincrono(_payload(), "https://zellu/cb")
        await asyncio.sleep(0.05)

    _, _, callback = _rodar(corpo=corpo)
    enviado = callback.enviados[0]["payload"]

    assert enviado["status"] == "completed"
    assert enviado["turnId"] == "turn_a"
    assert enviado["sessionId"] == "sess_1"
    assert enviado["lastMessageId"] == "msg_zelinhu_1"
    assert enviado["messageBody"] == "Nossa posicao e..."
    assert enviado["messageType"] == "text"
    assert enviado["tokensUsed"] == 812
    assert enviado["modelUsed"] == "gpt-4o-mini"
    assert enviado["negotiationComplete"] is False
    assert enviado["messagedata"] == {"replyToMessageId": "msg_zelinhu_1"}


def test_campos_aditivos_nao_se_perdem_no_caminho():
    """
    `requiresHumanApproval` e o gate de alcada: sem ele o backend fecharia
    sozinho um acordo que depende de humano.
    """
    com_gate = _TurnoFake(resposta=JSONResponse(content={
        "messageBody": "Proposta acima da alcada.",
        "negotiationComplete": False,
        "requiresHumanApproval": True,
    }))

    async def corpo():
        await main._aceitar_turno_assincrono(_payload(), "https://zellu/cb")
        await asyncio.sleep(0.05)

    _, _, callback = _rodar(turno=com_gate, corpo=corpo)
    assert callback.enviados[0]["payload"]["requiresHumanApproval"] is True


# =============================================================================
# CENARIO 4 - FALHA DEPOIS DO 202 TEM DE CHEGAR ATE ELES
# =============================================================================

def test_falha_depois_do_202_vira_callback_de_falha():
    """Sem isto, um turno que morre do nosso lado vira silencio (regra 2)."""
    quebrado = _TurnoFake(erro=RuntimeError("a OpenAI devolveu 500"))

    async def corpo():
        resposta = await main._aceitar_turno_assincrono(_payload(), "https://zellu/cb")
        await asyncio.sleep(0.05)
        return resposta

    resposta, _, callback = _rodar(turno=quebrado, corpo=corpo)

    assert resposta.status_code == 202, "o despacho foi aceito; a falha veio depois"
    enviado = callback.enviados[0]["payload"]
    assert enviado["status"] == "failed"
    assert enviado["turnId"] == "turn_a"
    assert enviado["sessionId"] == "sess_1"
    assert enviado["lastMessageId"] == "msg_zelinhu_1"
    assert "OpenAI" in enviado["error"]
    assert enviado["code"] == "TURN_FAILED"
    assert enviado["retryable"] is True, "vale redespachar"


def test_timeout_do_llm_e_identificado_e_retentavel():
    lento = _TurnoFake(erro=asyncio.TimeoutError())

    async def corpo():
        await main._aceitar_turno_assincrono(_payload(), "https://zellu/cb")
        await asyncio.sleep(0.05)

    _, _, callback = _rodar(turno=lento, corpo=corpo)
    enviado = callback.enviados[0]["payload"]

    assert enviado["code"] == "LLM_TIMEOUT"
    assert enviado["retryable"] is True


def test_resposta_de_erro_do_turno_vira_falha_e_nao_mensagem():
    """Um 5xx interno nao pode virar mensagem da empresa na negociacao."""
    com_erro = _TurnoFake(resposta=JSONResponse(status_code=502, content={"error": "LLM fora do ar"}))

    async def corpo():
        await main._aceitar_turno_assincrono(_payload(), "https://zellu/cb")
        await asyncio.sleep(0.05)

    _, _, callback = _rodar(turno=com_erro, corpo=corpo)
    enviado = callback.enviados[0]["payload"]

    assert enviado["status"] == "failed"
    assert enviado["code"] == "TURN_HTTP_502"
    assert enviado["retryable"] is True
    assert "messageBody" not in enviado


def test_turno_sem_texto_e_falha_e_nao_turno_concluido():
    """
    Resposta do backend, 14/09/2026: corpo vazio SEM `skipped` ja era tratado
    la como resposta invalida. Dizer "completed" esconderia um turno que nao
    produziu nada; "failed" devolve o turno para o redespacho do cron deles.
    """
    mudo = _TurnoFake(resposta=JSONResponse(content={
        "messageBody": "",
        "messageType": "text",
        "negotiationComplete": False,
    }))

    async def corpo():
        await main._aceitar_turno_assincrono(_payload(), "https://zellu/cb")
        await asyncio.sleep(0.05)

    _, _, callback = _rodar(turno=mudo, corpo=corpo)
    enviado = callback.enviados[0]["payload"]

    assert enviado["status"] == "failed"
    assert enviado["code"] == "EMPTY_RESPONSE"
    assert enviado["retryable"] is True, "vale redespachar: o turno nao produziu nada"


def test_modo_manual_continua_sendo_turno_concluido_com_skipped():
    """
    Turno pulado DE PROPOSITO - a empresa assumiu a conversa. Nao e falha: eles
    respondem `discarded: "skipped"`, nao gravam nada e liberam o turno em voo.
    Mandar `failed` aqui faria o cron redespachar um turno que nao deve rodar.
    """
    manual = _TurnoFake(resposta=JSONResponse(content={
        "messageBody": "",
        "messageType": "text",
        "tokensUsed": 0,
        "modelUsed": "none",
        "negotiationComplete": False,
        "manualModeActive": True,
        "skipped": True,
        "reason": "Modo manual ativo. IA aguardando humano devolver controle.",
    }))

    async def corpo():
        await main._aceitar_turno_assincrono(_payload(), "https://zellu/cb")
        await asyncio.sleep(0.05)

    _, _, callback = _rodar(turno=manual, corpo=corpo)
    enviado = callback.enviados[0]["payload"]

    assert enviado["status"] == "completed", "modo manual nao e falha"
    assert enviado["skipped"] is True, "o marcador tem de chegar ate eles"
    assert enviado["messageBody"] == ""
    assert enviado["manualModeActive"] is True


def test_awaiting_context_chega_com_o_texto_que_tem():
    """
    O fallback de autoStart devolve uma saudacao generica. Nao e vazio, entao
    nao e falha - vai como concluido, com o marcador `awaitingContext` a
    reboque para eles decidirem o que fazer.
    """
    sem_contexto = _TurnoFake(resposta=JSONResponse(content={
        "messageBody": "Ola! Sou o assistente virtual da empresa.",
        "negotiationComplete": False,
        "awaitingContext": True,
    }))

    async def corpo():
        await main._aceitar_turno_assincrono(_payload(), "https://zellu/cb")
        await asyncio.sleep(0.05)

    _, _, callback = _rodar(turno=sem_contexto, corpo=corpo)
    enviado = callback.enviados[0]["payload"]

    assert enviado["status"] == "completed"
    assert enviado["awaitingContext"] is True


def test_payload_invalido_continua_levando_400_na_hora():
    """
    Depois do 202 nao ha mais como responder erro de contrato. Corpo quebrado
    tem de ser recusado ANTES, sincrono, como sempre foi.
    """
    async def corpo():
        return await main._aceitar_turno_assincrono({"callbackUrl": "https://zellu/cb"}, "https://zellu/cb")

    resposta, turno, callback = _rodar(corpo=corpo)

    assert resposta.status_code == 400, "sem sessionId o payload e invalido"
    assert turno.chamadas == 0, "nao se gasta LLM com payload quebrado"
    assert callback.enviados == [], "nem callback de falha: o backend corrige o despacho"


def test_classificacao_da_falha():
    assert main._classificar_falha_do_turno(asyncio.TimeoutError()) == ("LLM_TIMEOUT", True)
    assert main._classificar_falha_do_turno(RuntimeError("rate limit atingido")) == ("LLM_RATE_LIMIT", True)
    assert main._classificar_falha_do_turno(RuntimeError("qualquer outra")) == ("TURN_FAILED", True)

    class ValidationError(Exception):
        pass

    assert main._classificar_falha_do_turno(ValidationError("campo x")) == ("INVALID_PAYLOAD", False)


# =============================================================================
# CENARIO 7 - REDESPACHO DO MESMO TURNO (pergunta 2 do §5)
# =============================================================================

def test_redespacho_do_mesmo_turno_nao_dobra_o_custo_de_llm():
    """
    O cron deles pode redespachar o mesmo turno com outro turnId. A chave de
    dedupe continua sendo sessionId:lastMessageId, entao o single-flight faz UM
    turno de LLM so - e cada despacho recebe o callback dele.
    """
    main._empresa_inflight.clear()
    main._empresa_responses.clear()

    lento = _TurnoFake(delay=0.15)

    # O mecanismo: os dois despachos tem a MESMA chave de turno, mesmo com
    # turnId diferente. E o que faz o single-flight de
    # _process_empresa_request_deduped rodar um turno de LLM so
    # (tests/test_empresa_idempotencia.py cobre o dedupe em si).
    assert main._empresa_dedupe_key(_payload(turnId="turn_a")) ==            main._empresa_dedupe_key(_payload(turnId="turn_b")) == "sess_1:msg_zelinhu_1"

    async def corpo():
        primeira = await main._aceitar_turno_assincrono(_payload(turnId="turn_a"), "https://zellu/cb")
        segunda = await main._aceitar_turno_assincrono(_payload(turnId="turn_b"), "https://zellu/cb")
        await asyncio.sleep(0.4)
        return primeira, segunda

    (primeira, segunda), turno, callback = _rodar(turno=lento, corpo=corpo)

    assert primeira.status_code == 202 and segunda.status_code == 202
    assert len(callback.enviados) == 2, "um callback por despacho"
    ecoados = sorted(e["payload"]["turnId"] for e in callback.enviados)
    assert ecoados == ["turn_a", "turn_b"], "cada callback ecoa o turnId do seu despacho"


# =============================================================================
# CENARIO 3 e 6 - ENTREGA DO CALLBACK
# =============================================================================

class _RespostaHTTP:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


class _ClienteHTTPFake:
    """Substitui httpx.AsyncClient registrando cada tentativa."""

    respostas = []
    tentativas = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, content=None, headers=None):
        _ClienteHTTPFake.tentativas.append({"url": url, "content": content, "headers": dict(headers or {})})
        resposta = _ClienteHTTPFake.respostas[min(len(_ClienteHTTPFake.tentativas) - 1,
                                                  len(_ClienteHTTPFake.respostas) - 1)]
        if isinstance(resposta, Exception):
            raise resposta
        return resposta


def _entregar(respostas, payload=None):
    """Roda send_turn_callback de verdade contra um transporte falso."""
    _ClienteHTTPFake.respostas = respostas
    _ClienteHTTPFake.tentativas = []

    corpo = payload or {"turnId": "turn_a", "sessionId": "sess_1", "status": "completed",
                        "messageBody": "ok"}

    original_cliente = turno_callback.httpx.AsyncClient
    original_sleep = turno_callback.asyncio.sleep
    turno_callback.httpx.AsyncClient = _ClienteHTTPFake

    async def _sem_espera(_s):
        return None

    turno_callback.asyncio.sleep = _sem_espera
    try:
        entregue = asyncio.run(turno_callback.send_turn_callback("https://zellu/cb", corpo, "chave"))
    finally:
        turno_callback.httpx.AsyncClient = original_cliente
        turno_callback.asyncio.sleep = original_sleep

    return entregue, _ClienteHTTPFake.tentativas


def test_200_encerra_a_entrega():
    entregue, tentativas = _entregar([_RespostaHTTP(200, '{"accepted":true}')])
    assert entregue is True
    assert len(tentativas) == 1


def test_duplicate_tambem_e_sucesso():
    """Repeticao nunca recebe 4xx: um callback que ja deu certo volta 200."""
    entregue, tentativas = _entregar([_RespostaHTTP(200, '{"duplicate":true}')])
    assert entregue is True
    assert len(tentativas) == 1, "duplicate nao se retenta"


def test_5xx_retenta_porque_nada_foi_gravado():
    entregue, tentativas = _entregar([_RespostaHTTP(500), _RespostaHTTP(500), _RespostaHTTP(200)])
    assert entregue is True
    assert len(tentativas) == 3, "so a terceira gravou"


def test_callback_fora_do_ar_e_reentregue_quando_volta():
    """Cenario 6: o endpoint deles cai por um minuto e volta."""
    import httpx

    entregue, tentativas = _entregar([httpx.ConnectError("connection refused"), _RespostaHTTP(200)])
    assert entregue is True
    assert len(tentativas) == 2


def test_4xx_nao_retenta():
    entregue, tentativas = _entregar([_RespostaHTTP(400, "corpo fora do contrato")])
    assert entregue is False
    assert len(tentativas) == 1, "repetir o mesmo corpo daria no mesmo 400"


def test_401_nao_retenta():
    entregue, tentativas = _entregar([_RespostaHTTP(401)])
    assert entregue is False
    assert len(tentativas) == 1


def test_desiste_depois_do_maximo_de_tentativas():
    entregue, tentativas = _entregar([_RespostaHTTP(500)])
    assert entregue is False
    assert len(tentativas) == 3, "o default e 3 tentativas, a primeira inclusa"


def test_assinatura_e_refeita_a_cada_tentativa():
    """
    A janela do x-timestamp e de 5 min. Reaproveitar os cabecalhos da primeira
    tentativa faria a segunda levar 400 (secao 3.2).
    """
    from config import get_settings

    settings = get_settings()
    anterior = settings.AI_WEBHOOK_HMAC_SECRET
    settings.AI_WEBHOOK_HMAC_SECRET = "segredo-de-teste"
    try:
        entregue, tentativas = _entregar([_RespostaHTTP(500), _RespostaHTTP(200)])
    finally:
        settings.AI_WEBHOOK_HMAC_SECRET = anterior

    assert len(tentativas) == 2
    assinaturas = [t["headers"].get("x-signature") for t in tentativas]
    assert all(assinaturas), "toda tentativa vai assinada"
    carimbos = [t["headers"].get("x-timestamp") for t in tentativas]
    assert all(carimbos), "e com o timestamp dela"


def test_api_key_acompanha_todo_callback():
    entregue, tentativas = _entregar([_RespostaHTTP(200)])
    assert tentativas[0]["headers"]["x-api-key"] == "chave"


def test_url_relativa_e_resolvida_contra_o_backend():
    resolvida = turno_callback.resolve_callback_url("/api/ai-service/company-turn-callback")
    assert resolvida.endswith("/api/ai-service/company-turn-callback")
    assert resolvida.startswith("http"), "URL relativa nao pode chegar crua no httpx"

    absoluta = "https://zellu/api/ai-service/company-turn-callback"
    assert turno_callback.resolve_callback_url(absoluta) == absoluta


# =============================================================================
# MONTAGEM DO CORPO DO CALLBACK
# =============================================================================

def test_corpo_de_sucesso_preenche_defaults():
    payload = turno_callback.build_turn_callback_payload(
        "turn_a", "sess_1", "msg_1", {"messageBody": "texto"}
    )
    assert payload["status"] == "completed"
    assert payload["messageType"] == "text"
    assert payload["negotiationComplete"] is False


def test_corpo_de_falha_corta_erro_gigante():
    payload = turno_callback.build_turn_failure_payload(
        "turn_a", "sess_1", "msg_1", "x" * 5000, "LLM_TIMEOUT", True
    )
    assert len(payload["error"]) == 300, "o campo e 'texto curto'"
    assert payload["status"] == "failed"
    assert payload["retryable"] is True


# =============================================================================
# RUNNER
# =============================================================================

def _run_all():
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]

    passed, failed = 0, []
    for name, fn in tests:
        try:
            fn()
            passed += 1
            print(f"  [OK]   {name}")
        except AssertionError as e:
            failed.append(name)
            print(f"  [FALHA] {name}: {e}")
        except Exception as e:
            failed.append(name)
            print(f"  [ERRO] {name}: {type(e).__name__}: {e}")

    print("\n" + "=" * 60)
    print(f"Total: {len(tests)} | OK: {passed} | Falhas: {len(failed)}")
    print("=" * 60)
    return 1 if failed else 0


if __name__ == "__main__":
    print("=" * 60)
    print("TURNO ASSINCRONO DA IA DA EMPRESA - contrato 11/09/2026")
    print("=" * 60)
    sys.exit(_run_all())
