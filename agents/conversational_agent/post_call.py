# -*- coding: utf-8 -*-
"""Pos-chamada do agente telefonico da Zellu (ElevenLabs Agents).

Roda DENTRO deste servico (registrado em src/main.py), nao como processo
separado. A ElevenLabs chama POST /webhooks/elevenlabs/post-call quando a
ligacao termina, e daqui sai, nesta ordem:

1. confere a assinatura HMAC do ElevenLabs;
2. le o transcript e o data collection preenchido pelo agente (ver main.py);
3. baixa o audio da ligacao e sobe no storage da Zellu (/api/ai-service/upload);
4. envia UM POST /api/phone/calls - idempotente por conversationId;
5. registra o custo em POST /api/ai/usage, INCLUSIVE quando a ligacao foi
   reprovada na triagem.

Ligacao reprovada na triagem manda so a linha de auditoria: sem contact, sem
audioUrl, sem summary e sem transcription.

Configuracao vem toda de get_settings(), igual ao resto do servico: em producao
os valores sao as variaveis de ambiente do container (Coolify), lidas no momento
do uso - nunca no import.

Contrato: 20260823-contrato-atendimento-telefonico-elevenlabs.md
Emenda 6: 20260917-emenda-6-identidade-por-telefone-no-canal-de-voz.md
          (o `phoneVerificationId` que sai no passo 4)
"""

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
import httpx

from agents.conversational_agent.phone_identity import challenge_aprovado
from config import get_settings

PROVIDER = "elevenlabs"
CONSENT_VERSION = "v1"

# Janela de tolerancia da assinatura, para barrar replay de um POST antigo.
TOLERANCIA_ASSINATURA_SEGUNDOS = 30 * 60

# endReason que reprova a ligacao na triagem: so auditoria, nada mais vai junto.
REJEICOES = {"underage", "telemarketing", "abuse_suspected", "out_of_scope", "no_consent"}

# Trechos do texto de consentimento e da pergunta de maioridade (main.py). Servem
# para achar no transcript o instante em que o cliente respondeu.
MARCADORES_CONSENTIMENTO = ("posso seguir", "grava")
# "nascimento" entrou quando a triagem passou a perguntar a data de nascimento e
# chamar a tool validar_idade: o agente nao diz mais "18" na pergunta, e sem este
# marcador o declaredAdultAt sumiria de toda ligacao nova. "18" fica para as
# ligacoes gravadas com o roteiro anterior.
MARCADORES_MAIORIDADE = ("nascimento", "18")

TIMEOUT_PADRAO = 30.0
TIMEOUT_AUDIO = 120.0

router = APIRouter(tags=["Pos-chamada telefonica"])


def _backend():
    """URL do backend Zellu e a chave do header x-api-key."""
    settings = get_settings()
    return settings.ZELLU_BACKEND_URL.rstrip("/"), settings.ai_usage_api_key


# ---------------------------------------------------------------------------
# Assinatura do webhook
# ---------------------------------------------------------------------------
def assinatura_valida(corpo: bytes, cabecalho: str) -> bool:
    """Confere o header ElevenLabs-Signature, no formato 't=...,v0=...'."""

    segredo = get_settings().ELEVENLABS_WEBHOOK_SECRET

    if not segredo:
        # Fail-closed desde 17/09. Antes isto devolvia True, como atalho de
        # desenvolvimento, e fazia sentido enquanto a rota ainda estava atras do
        # gate global de x-api-key. Ela saiu do gate no mesmo dia
        # (src/main.py:ROTAS_SEM_GATE_DE_CHAVE), entao a assinatura passou a ser
        # a UNICA autenticacao desta rota: aceitar sem conferir deixaria qualquer
        # um na internet gravando ligacao na conta de qualquer cliente.
        print(
            "[POS-CHAMADA] ELEVENLABS_WEBHOOK_SECRET vazio - webhook RECUSADO. "
            "Preencha o segredo gerado junto com o webhook no painel da "
            "ElevenLabs, senao nenhuma ligacao chega na Zellu."
        )
        return False

    partes = dict(
        pedaco.split("=", 1) for pedaco in (cabecalho or "").split(",") if "=" in pedaco
    )
    momento = partes.get("t")
    recebida = partes.get("v0")

    if not momento or not recebida:
        return False

    try:
        if abs(time.time() - int(momento)) > TOLERANCIA_ASSINATURA_SEGUNDOS:
            print("[POS-CHAMADA] Assinatura fora da janela de tolerancia.")
            return False
    except ValueError:
        return False

    esperada = hmac.new(
        segredo.encode("utf-8"),
        f"{momento}.{corpo.decode('utf-8')}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(esperada, recebida)


# ---------------------------------------------------------------------------
# Leitura do payload do ElevenLabs
# ---------------------------------------------------------------------------
def _iso(momento: datetime) -> str:
    """ISO-8601 em UTC. O contrato recusa horario local."""
    return momento.strftime("%Y-%m-%dT%H:%M:%SZ")


def _texto(valor) -> str:
    return valor.strip() if isinstance(valor, str) else ""


def _booleano(valor) -> bool:
    """O data collection devolve bool, mas as vezes chega como texto."""
    if isinstance(valor, bool):
        return valor
    return _texto(valor).lower() == "true"


def _fim_tecnico(metadata: dict) -> str:
    """endReason quando o agente nao preencheu o campo (desligou, caiu, travou)."""

    motivo = _texto(metadata.get("termination_reason")).lower()

    if "timeout" in motivo or "silence" in motivo:
        return "timeout"
    if "error" in motivo or "fail" in motivo:
        return "failed"
    return "caller_hangup"


def _instante_da_resposta(transcript: list, inicio: datetime, marcadores: tuple):
    """Instante da primeira resposta do cliente depois que o agente falou um dos
    marcadores. Devolve None quando nao acha - melhor omitir do que inventar."""

    ouviu_marcador = False

    for turno in transcript:
        papel = turno.get("role")
        texto = _texto(turno.get("message")).lower()

        if papel == "agent":
            if any(marcador in texto for marcador in marcadores):
                ouviu_marcador = True
            continue

        if papel == "user" and ouviu_marcador:
            segundos = int(turno.get("time_in_call_secs") or 0)
            return _iso(inicio + timedelta(seconds=segundos))

    return None


def _transcricao(transcript: list, inicio: datetime) -> list:
    """Transcript do ElevenLabs no formato [{role, text, at}] do contrato."""

    linhas = []

    for turno in transcript:
        papel = turno.get("role")
        texto = _texto(turno.get("message"))

        if papel not in ("agent", "user") or not texto:
            continue

        segundos = int(turno.get("time_in_call_secs") or 0)
        linhas.append({
            "role": papel,
            "text": texto,
            "at": _iso(inicio + timedelta(seconds=segundos)),
        })

    return linhas


def extrair_dados(payload: dict) -> dict:
    """Normaliza o webhook do ElevenLabs no que o contrato precisa."""

    dados = payload.get("data") or {}
    metadata = dados.get("metadata") or {}
    analise = dados.get("analysis") or {}
    ligacao = metadata.get("phone_call") or {}

    # As dynamic_variables que o front mandou no startSession (web_call.py)
    # voltam inteiras aqui. E por elas que sabemos que a conversa veio do app -
    # nao ha `phone_call` no metadata para denunciar isso - e quem estava logado.
    variaveis = (dados.get("conversation_initiation_client_data") or {}).get(
        "dynamic_variables"
    ) or {}
    origem = _texto(variaveis.get("origin"))

    coletado = {
        chave: (item or {}).get("value")
        for chave, item in (analise.get("data_collection_results") or {}).items()
    }

    inicio = datetime.fromtimestamp(
        int(metadata.get("start_time_unix_secs") or 0), tz=timezone.utc
    )
    duracao = int(metadata.get("call_duration_secs") or 0)
    transcript = dados.get("transcript") or []

    fim_declarado = _texto(coletado.get("endReason"))

    # No app o aceite e a maioridade vem por escrito, do modal da Zellu, e o
    # agente nao fala o consentimento nem pergunta a idade (Emenda 4, §4.2, e
    # Emenda 5, §5.6). O instante do clique vale para os dois campos. Sem isto o
    # consentimento chegava vazio e a gravacao de toda conversa do app sumia.
    aceite_escrito = _texto(variaveis.get("zellu_consent_accepted_at"))

    return {
        "conversationId": _texto(dados.get("conversation_id")),
        "agentId": _texto(dados.get("agent_id")),
        # O identificador de chamadas e a fonte; o telefone falado e a reserva.
        "callerPhone": _texto(ligacao.get("external_number")) or _texto(coletado.get("phone")),
        "origin": origem,
        # O id da conta de quem falou pelo app estando logado. Vai no payload e
        # NUNCA no prompt do agente.
        "userId": _texto(variaveis.get("user_id")),
        # Conversa do app vai como `inbound`. O enum e `inbound | outbound` e
        # `web_app` volta 400, e a conversa se perde (resposta da Zellu de
        # 21/09, item 1). O canal a Zellu ja sabe pelo registro de sessao que
        # grava ao emitir o token; quem diz que veio do app aqui e o `origin`.
        "direction": _texto(ligacao.get("direction")) or "inbound",
        "inicio": inicio,
        "fim": inicio + timedelta(seconds=duracao),
        "duracao": duracao,
        "endReason": fim_declarado or _fim_tecnico(metadata),
        "consentAccepted": bool(aceite_escrito) or _booleano(coletado.get("consentAccepted")),
        "consentAcceptedAt": (
            aceite_escrito
            or _instante_da_resposta(transcript, inicio, MARCADORES_CONSENTIMENTO)
        ),
        # "web-v1" no app; o aceite falado continua sendo "v1".
        "consentVersion": (
            (_texto(variaveis.get("zellu_consent_version")) or CONSENT_VERSION)
            if aceite_escrito else CONSENT_VERSION
        ),
        "declaredAdultAt": aceite_escrito or (
            _instante_da_resposta(transcript, inicio, MARCADORES_MAIORIDADE)
            if _booleano(coletado.get("declaredAdult")) else None
        ),
        "firstName": _texto(coletado.get("firstName")),
        "lastName": _texto(coletado.get("lastName")),
        "email": _texto(coletado.get("email")).lower(),
        "phone": _texto(coletado.get("phone")),
        "summary": _texto(coletado.get("summary")),
        "transcricao": _transcricao(transcript, inicio),
    }


def reprovada_na_triagem(dados: dict) -> bool:
    return dados["endReason"] in REJEICOES


def so_auditoria(dados: dict) -> bool:
    """True quando a ligacao sai daqui como linha de auditoria e nada mais.

    Duas situacoes. A triagem reprovou - e ai o contrato manda so a auditoria. Ou
    a ligacao terminou sem relato: chamada aprovada sem `summary` e recusada com
    400 pelo backend, e a ligacao some do registro inteira. Sem relato nao ha
    chamado a abrir, entao vale mais a linha de auditoria sozinha do que nada.

    Termina sem relato a ligacao que cai nos primeiros segundos e a que encerra
    num dos quatro ramos da Emenda 6 - numero em uso, conta indisponivel, SMS que
    nao saiu e codigos demais na hora -, todos eles antes de a pessoa contar o que
    aconteceu.
    """

    return reprovada_na_triagem(dados) or not dados["summary"]


# ---------------------------------------------------------------------------
# Payload do POST /api/phone/calls
# ---------------------------------------------------------------------------
def montar_payload(dados: dict, audio_url: str = None) -> dict:
    """Monta o corpo do POST /api/phone/calls.

    Ligacao reprovada na triagem sai daqui so com a linha de auditoria: mandar
    contact ou audioUrl junto de um endReason de rejeicao devolve 400. Ligacao
    que terminou sem relato sai igual, pelo motivo oposto - ver so_auditoria().
    """

    payload = {
        "provider": PROVIDER,
        "conversationId": dados["conversationId"],
        "agentId": dados["agentId"],
        "callerPhone": dados["callerPhone"],
        "direction": dados["direction"],
        "startedAt": _iso(dados["inicio"]),
        "endedAt": _iso(dados["fim"]),
        "durationSeconds": dados["duracao"],
        "endReason": dados["endReason"],
    }

    # Chamada pelo app nao toca telefone nenhum: nao ha identificador de chamadas,
    # e o contrato (emenda de 03/09, §1) manda o campo AUSENTE. O telefone que a
    # pessoa falar ainda vai, mas em `contact.phone`, que e o lugar dele - aqui
    # seria um numero de origem que nunca existiu.
    if dados["origin"] == "web_app":
        payload.pop("callerPhone")

    if so_auditoria(dados):
        return payload

    if dados["declaredAdultAt"]:
        payload["declaredAdultAt"] = dados["declaredAdultAt"]

    # Sem consent.acceptedAt nao ha base legal para o audio: o backend devolve 400.
    if dados["consentAccepted"] and dados["consentAcceptedAt"]:
        payload["consent"] = {
            "acceptedAt": dados["consentAcceptedAt"],
            "version": dados.get("consentVersion") or CONSENT_VERSION,
        }
        if audio_url:
            payload["audioUrl"] = audio_url

    payload["summary"] = dados["summary"]

    if dados["transcricao"]:
        payload["transcription"] = dados["transcricao"]

    # Cliente logado no app ja tem conta: o vinculo vai pelo userId, e o
    # pre-cadastro pelo contact - que e o que dispara o e-mail e o WhatsApp com o
    # link de concluir cadastro - nao faz sentido para quem ja esta dentro da
    # plataforma (emenda de 03/09, §3). Por isso o contact so vai quando NAO ha
    # userId, logo abaixo.
    if dados["userId"]:
        payload["userId"] = dados["userId"]

    # Sem contato nenhum a ligacao vira so auditoria (identityOutcome:
    # unresolved) e o cliente nao recebe link.
    #
    # O gate era `if dados["email"]`. Desde que a triagem parou de pedir e-mail
    # (a identidade virou o TELEFONE), esse gate nunca mais seria verdadeiro e
    # NENHUMA ligacao mandaria contact - o cadastro simplesmente deixaria de
    # nascer, com 200 em todas as respostas e nada no log. Agora basta um dos
    # dois; o e-mail segue indo quando existir, para as ligacoes antigas e para
    # quem informar espontaneamente.
    elif dados["phone"] or dados["callerPhone"] or dados["email"]:
        payload["contact"] = {
            "firstName": dados["firstName"],
            "lastName": dados["lastName"],
            "email": dados["email"],
            "phone": dados["phone"] or dados["callerPhone"],
        }

    # Emenda 6: a prova de que o numero e de quem ligou, guardada quando a pessoa
    # ditou o codigo do SMS no meio da conversa (phone_identity.py). Opcional
    # enquanto a Zellu nao ligar a exigencia - e ate la mandar ou nao mandar da no
    # mesmo. Depois, e o que decide se a sessao nasce na conta dela ou sem dono.
    prova = challenge_aprovado(dados["phone"] or dados["callerPhone"])

    if prova:
        payload["phoneVerificationId"] = prova

    return payload


# ---------------------------------------------------------------------------
# Audio: baixa do ElevenLabs e sobe no storage da Zellu
# ---------------------------------------------------------------------------
def baixar_audio(conversation_id: str):
    """Audio consolidado da ligacao. None se nao conseguir - o resto segue."""

    settings = get_settings()
    url = (
        f"{settings.ELEVENLABS_API_URL.rstrip('/')}"
        f"/v1/convai/conversations/{conversation_id}/audio"
    )

    try:
        with httpx.Client(timeout=TIMEOUT_AUDIO) as client:
            resposta = client.get(url, headers={"xi-api-key": settings.ELEVENLABS_API_KEY})

        if resposta.status_code != 200:
            print(f"[POS-CHAMADA] Audio de {conversation_id} veio {resposta.status_code}.")
            return None

        return resposta.content

    except Exception as e:
        print(f"[POS-CHAMADA] Falha ao baixar o audio de {conversation_id}: {e}")
        return None


def subir_audio(audio, conversation_id: str):
    """Sobe no NOSSO storage e devolve a url/key.

    O contrato proibe mandar url presignada ou link do ElevenLabs em audioUrl:
    presigned expira e o audio some da conta do cliente.
    """

    if not audio:
        return None

    base, chave = _backend()

    try:
        with httpx.Client(timeout=TIMEOUT_AUDIO) as client:
            resposta = client.post(
                f"{base}/api/ai-service/upload",
                headers={"x-api-key": chave},
                files=[("files", (f"{conversation_id}.mp3", audio, "audio/mpeg"))],
            )

        if resposta.status_code not in (200, 201):
            print(f"[POS-CHAMADA] Upload do audio veio {resposta.status_code}: {resposta.text[:300]}")
            return None

        arquivos = (resposta.json() or {}).get("files") or []
        return arquivos[0].get("url") if arquivos else None

    except Exception as e:
        print(f"[POS-CHAMADA] Falha ao subir o audio de {conversation_id}: {e}")
        return None


# ---------------------------------------------------------------------------
# Envio para o backend
# ---------------------------------------------------------------------------
def enviar_chamada(payload: dict):
    """POST /api/phone/calls. Devolve o corpo da resposta, ou None se falhou.

    Idempotente por conversationId: reenviar o mesmo valor devolve o mesmo
    sessionId, sem duplicar sessao nem reenviar e-mail para o cliente.
    """

    base, chave = _backend()

    try:
        with httpx.Client(timeout=TIMEOUT_PADRAO) as client:
            resposta = client.post(
                f"{base}/api/phone/calls",
                headers={"x-api-key": chave, "Content-Type": "application/json"},
                json=payload,
            )

        if resposta.status_code not in (200, 201):
            print(
                f"[POS-CHAMADA] /api/phone/calls veio {resposta.status_code} para "
                f"{payload.get('conversationId')}: {resposta.text[:500]}"
            )
            return None

        return resposta.json()

    except Exception as e:
        print(f"[POS-CHAMADA] Falha ao enviar a chamada {payload.get('conversationId')}: {e}")
        return None


def enviar_uso(dados: dict, resposta_chamada: dict = None) -> None:
    """POST /api/ai/usage com o custo da ligacao.

    Vai tambem nas ligacoes reprovadas na triagem: elas consomem segundos de
    agente e, sem o evento, o gate fica invisivel no relatorio de margem.
    Falha aqui nunca derruba o pos-chamada.
    """

    resposta_chamada = resposta_chamada or {}
    base, chave = _backend()

    evento = {
        "module": "voice_intake",
        "provider": PROVIDER,
        "model": get_settings().ELEVENLABS_VOICE_MODEL,
        # Obrigatorios mesmo em evento de minutos: omitir devolve 400.
        "tokensIn": 0,
        "tokensOut": 0,
        "unit": "minutes",
        "quantityIn": round(dados["duracao"] / 60, 4),
        "sourceType": "phone_call",
        # 'chat_telefone' para quem ligou; 'chat_voz_app' para quem falou pelo
        # icone dentro da plataforma. Os dois no mesmo idioma de
        # chat_externo/chat_interno. O valor do app foi pedido na emenda de 03/09
        # (§4) e confirmado pela plataforma em 04/09 - ate hoje o relatorio de
        # margem somava a conversa do app como se fosse minuto de telefonia.
        "channel": "chat_voz_app" if dados["origin"] == "web_app" else "chat_telefone",
        "eventType": "post_call",
        "durationMs": dados["duracao"] * 1000,
        "metadata": {"screened_out": reprovada_na_triagem(dados)},
    }

    opcionais = {
        "sessionId": resposta_chamada.get("sessionId"),
        "userId": resposta_chamada.get("userId"),
        "sourceId": resposta_chamada.get("phoneCallId") or resposta_chamada.get("callId"),
    }
    for nome, valor in opcionais.items():
        if valor:
            evento[nome] = valor

    try:
        with httpx.Client(timeout=TIMEOUT_PADRAO) as client:
            resultado = client.post(
                f"{base}/api/ai/usage",
                headers={"x-api-key": chave, "Content-Type": "application/json"},
                json=evento,
            )

        if resultado.status_code not in (200, 201):
            print(f"[POS-CHAMADA] /api/ai/usage veio {resultado.status_code}: {resultado.text[:300]}")

    except Exception as e:
        print(f"[POS-CHAMADA] Falha ao registrar o custo da ligacao: {e}")


# ---------------------------------------------------------------------------
# Orquestracao
# ---------------------------------------------------------------------------
def processar_pos_chamada(payload_webhook: dict) -> dict:
    """Fluxo completo do fim da ligacao. Devolve o que o backend respondeu."""

    dados = extrair_dados(payload_webhook)
    conversa = dados["conversationId"]

    if not conversa:
        print("[POS-CHAMADA] Webhook sem conversation_id - ignorado.")
        return {"success": False, "error": "conversation_id ausente"}

    audio_url = None

    # Preservar contato e relato mesmo se o backend remoto estiver indisponivel.
    # Nao transformar esse registro em conta nem em prospect comercial.
    local_record = {'status': 'not_applicable'}
    if not so_auditoria(dados) and dados['consentAccepted'] and dados['consentAcceptedAt']:
        from src.services.voice_intake_records import save_voice_intake
        try:
            local_record = save_voice_intake(dados)
        except Exception:
            local_record = {'status': 'storage_failed'}
            print('[POS-CHAMADA] Falha ao persistir registro local de voz.')

    # Audio so quando a ligacao vai levar mais que a auditoria E o cliente aceitou
    # a gravacao. Sem relato o audioUrl nem sairia daqui, e subir o arquivo so
    # deixaria um orfao no storage.
    if not so_auditoria(dados) and dados["consentAccepted"] and dados["consentAcceptedAt"]:
        audio_url = subir_audio(baixar_audio(conversa), conversa)

    payload = montar_payload(dados, audio_url)

    # Nao inventamos um resumo no lugar do agente: a ligacao vira auditoria e fica
    # registrada, em vez de tomar 400 e sumir.
    if not reprovada_na_triagem(dados) and not dados["summary"]:
        print(f"[POS-CHAMADA] {conversa} terminou sem relato - vai so a linha de auditoria.")

    resposta = enviar_chamada(payload)
    enviar_uso(dados, resposta)

    if resposta is None:
        return {"success": False, "conversationId": conversa, 'localRecord': local_record}

    resposta = dict(resposta)
    resposta['localRecord'] = local_record

    if not so_auditoria(dados):
        from src.services.voice_chat_handoff import deliver_registered_call
        try:
            resposta = dict(resposta)
            resposta['chatHandoff'] = deliver_registered_call(dados, resposta)
        except Exception:
            # A chamada ja esta registrada; nao perder o registro por falha
            # posterior de entrega ao chat. A pendencia precisa ficar visivel.
            resposta['chatHandoff'] = {'status': 'delivery_failed'}

    print(
        f"[POS-CHAMADA] {conversa} -> sessao {resposta.get('sessionId')} "
        f"({resposta.get('identityOutcome')})"
    )
    return resposta


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------
@router.post("/webhooks/elevenlabs/post-call")
async def receber_post_call(request: Request):
    """Webhook que o ElevenLabs chama quando a ligacao termina."""

    corpo = await request.body()

    if not assinatura_valida(corpo, request.headers.get("elevenlabs-signature", "")):
        raise HTTPException(status_code=401, detail="Assinatura invalida")

    try:
        payload = json.loads(corpo)
    except ValueError:
        raise HTTPException(status_code=400, detail="Corpo nao e JSON")

    tipo = payload.get("type")

    # Baixamos o audio pela API, entao o webhook post_call_audio nao interessa.
    if tipo != "post_call_transcription":
        return {"success": True, "ignorado": tipo}

    # O processamento e sincrono (httpx.Client): sai do event loop para nao
    # segurar as outras rotas do servico enquanto sobe o audio.
    return await run_in_threadpool(processar_pos_chamada, payload)
