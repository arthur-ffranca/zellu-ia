# -*- coding: utf-8 -*-
"""Chamada de voz no app (ElevenLabs Agents, WebRTC).

O cliente clica no icone de ligacao dentro da plataforma e fala com o agente pelo
microfone do proprio aparelho. O telefone dele NAO toca: nao ha numero, nao ha
operadora e nao ha minuto de telefonia. O audio vai do navegador direto para a
ElevenLabs - este servico nao fica no meio da conversa.

Este modulo faz UMA coisa: entrega o token de entrada da conversa.

A ELEVENLABS_API_KEY vale para a workspace inteira e NUNCA pode chegar ao
aparelho do cliente. Por isso quem pede o token e o backend Zellu, autenticado
com x-api-key, e o front recebe so o token, que serve para uma conversa e
expira sozinho.

O fim da ligacao continua no mesmo caminho de sempre: a ElevenLabs chama
POST /webhooks/elevenlabs/post-call (post_call.py) e de la sai o unico
POST /api/phone/calls.

O agente e o MESMO do telefone (ELEVENLABS_AGENT_ID) - mesmo prompt, mesmas
tools, mesmo data collection. Nao ha um segundo agente para manter. O roteiro
muda pelas variaveis abaixo: no app ninguem pergunta idade nem le o
consentimento (a pessoa aceitou por escrito no modal da Zellu), e quem esta
logado pula nome, telefone e o codigo por SMS. O visitante nao logado passa
pelo mesmo codigo de seis digitos de quem liga.

Contrato: 20260823-contrato-atendimento-telefonico-elevenlabs.md
Emenda:   20260903-emenda-chamada-de-voz-no-app.md
"""

from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
import httpx
from pydantic import BaseModel

from config import get_settings

# Pedir o token e uma chamada curta: se demorar mais que isso, o cliente ja
# desistiu do clique.
TIMEOUT_TOKEN = 15.0

router = APIRouter(tags=["Chamada de voz no app"])


class WebCallTokenRequest(BaseModel):
    """Quem esta do outro lado do icone.

    Todos os campos sao opcionais de proposito: o icone tambem aparece para
    visitante nao logado, e nesse caso o agente coleta nome, e-mail e telefone
    na propria conversa, como faz no telefone.
    """

    userId: Optional[str] = None
    sessionId: Optional[str] = None
    clientName: Optional[str] = None
    clientEmail: Optional[str] = None


def verify_api_key(x_api_key: str = Header(..., alias="x-api-key")):
    """Mesma chave das outras integracoes do servico."""
    if x_api_key != get_settings().API_KEY_ZELLU_IA:
        raise HTTPException(status_code=401, detail="Nao autorizado")
    return x_api_key


def montar_variaveis_dinamicas(pedido: WebCallTokenRequest) -> dict:
    """dynamic_variables que o front manda no startSession.

    Elas voltam inteiras no post-call webhook, dentro de
    data.conversation_initiation_client_data.dynamic_variables. E por elas que o
    pos-chamada consegue saber QUEM falou sem depender do e-mail dito em voz
    alta - que e o ponto mais fragil da ligacao telefonica.

    Montadas aqui, e nao no front, para o nome de cada variavel viver num lugar
    so: o dia que mudar, muda aqui e o front nao precisa de deploy.
    """

    # `logged_in` e o unico destes que o ROTEIRO le, e por isso ele existe: o
    # agente precisa saber se promete o link por WhatsApp no fim (visitante) ou
    # se diz que o caso ja esta na conta (logado). Ele responde isso sem o
    # user_id chegar ao modelo - o id fica no payload, nunca no prompt.
    variaveis = {
        "origin": "web_app",
        "logged_in": "sim" if pedido.userId else "nao",
    }

    campos = (
        ("user_id", pedido.userId),
        ("session_id", pedido.sessionId),
        ("client_name", pedido.clientName),
        ("client_email", pedido.clientEmail),
    )

    for nome, valor in campos:
        if valor:
            variaveis[nome] = valor

    return variaveis


@router.post(
    "/phone/web-call/token",
    responses={
        200: {"description": "Token emitido"},
        401: {"description": "Nao autorizado"},
        502: {"description": "ElevenLabs indisponivel ou recusou o pedido"},
        503: {"description": "Chamada de voz no app nao configurada neste ambiente"},
    },
    summary="Token da chamada de voz no app",
    description=(
        "Emite o token que o front usa para abrir a conversa por voz com o agente. "
        "Chamado pelo backend Zellu quando o cliente clica no icone de ligacao."
    ),
)
async def gerar_token_chamada_web(
    pedido: WebCallTokenRequest,
    api_key: str = Depends(verify_api_key),
):
    """Pede a ElevenLabs o token de uma conversa nova e devolve para o backend."""

    settings = get_settings()
    agent_id = settings.ELEVENLABS_AGENT_ID

    # Sem agente ou sem chave nao ha o que emitir. 503 e nao 500: e ambiente
    # faltando configuracao, nao bug - a mensagem diz o que falta.
    if not agent_id:
        raise HTTPException(
            status_code=503,
            detail="ELEVENLABS_AGENT_ID nao configurado neste ambiente",
        )

    if not settings.ELEVENLABS_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="ELEVENLABS_API_KEY nao configurada neste ambiente",
        )

    parametros = {"agent_id": agent_id}

    # So para o atendimento aparecer com nome no painel da ElevenLabs.
    if pedido.clientName:
        parametros["participant_name"] = pedido.clientName

    url = f"{settings.ELEVENLABS_API_URL.rstrip('/')}/v1/convai/conversation/token"

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_TOKEN) as client:
            resposta = await client.get(
                url,
                headers={"xi-api-key": settings.ELEVENLABS_API_KEY},
                params=parametros,
            )
    except Exception as e:
        print(f"[CHAMADA-WEB] Falha ao pedir o token para o agente {agent_id}: {e}")
        raise HTTPException(status_code=502, detail="Nao foi possivel iniciar a chamada")

    if resposta.status_code != 200:
        print(
            f"[CHAMADA-WEB] Token veio {resposta.status_code} para o agente "
            f"{agent_id}: {resposta.text[:300]}"
        )
        raise HTTPException(status_code=502, detail="Nao foi possivel iniciar a chamada")

    dados = resposta.json() or {}
    conversa = dados.get("conversation_id")

    print(f"[CHAMADA-WEB] Token emitido para a conversa {conversa} (user {pedido.userId or '-'})")

    return {
        "success": True,
        "token": dados.get("token"),
        "conversationId": conversa,
        "agentId": agent_id,
        # O front repassa isto no startSession, sem montar nada por conta propria.
        "dynamicVariables": montar_variaveis_dinamicas(pedido),
    }
