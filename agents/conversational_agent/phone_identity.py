# -*- coding: utf-8 -*-
"""Identidade por telefone na ligacao (tools `consultar_telefone` e
`confirmar_codigo` do agente ElevenLabs).

Ate agora quem era a pessoa na ligacao se decidia pelo e-mail, e depois pelo
telefone que ela ditava - sem prova nenhuma de que o numero era dela. Saber o
telefone de um cliente bastava para abrir chamado no nome dele.

A Emenda 6 fecha isso: a pessoa dita o numero, a Zellu manda um codigo de seis
digitos por SMS, ela dita o codigo de volta e so entao a conversa vira conta. A
prova aprovada viaja ate o fim da ligacao e sai no `phoneVerificationId` do
POST /api/phone/calls (post_call.py).

POR QUE ESTA ROTA EXISTE, EM VEZ DE O AGENTE CHAMAR A ZELLU DIRETO
------------------------------------------------------------------
Pelo mesmo motivo da tool de idade: a ElevenLabs chamaria a Zellu de um IP que
nao e o nosso, e a restricao por IP combinada em 16/09 derrubaria a chamada. Aqui
a ElevenLabs fala com a VPS, e so a VPS fala com a Zellu.

O QUE O AGENTE NAO VE
---------------------
O `challengeId` e o `userId` nunca chegam ao modelo. O agente manda o telefone e
o codigo - as duas coisas que a pessoa falou em voz alta - e recebe de volta o
`outcome` e, no maximo, o primeiro nome. Guardar o `challengeId` aqui dentro
poupa o modelo de copiar um id opaco de uma resposta para a chamada seguinte, que
e exatamente o tipo de transcricao que ja nos custou uma data de nascimento
errada em 08/09.

Emenda: 20260917-emenda-6-identidade-por-telefone-no-canal-de-voz.md
Contrato: 20260823-contrato-atendimento-telefonico-elevenlabs.md (Emenda 6)
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
import httpx
from pydantic import BaseModel

from config import get_settings
from src.utils.phone import normalize_phone

# A Zellu so aceita a prova com menos de 24 horas (secao 5 da emenda). Guardamos
# pela mesma janela: o que passa disso nao serve mais para nada.
VALIDADE_DA_PROVA = timedelta(hours=24)

# O lookup manda um SMS antes de responder, entao demora mais que o verify. Os
# dois sao curtos de proposito: isto acontece com a pessoa esperando na linha.
TIMEOUT_LOOKUP = 15.0
TIMEOUT_VERIFY = 8.0

# Nao e outcome da Zellu: e o nosso, para quando a chamada nem chegou la. O
# roteiro trata este caso SEGUINDO a ligacao, nunca encerrando - falha nossa nao
# pode custar o relato da pessoa. Nome deliberadamente diferente de `sms_failed`,
# que encerra, para o modelo nao confundir os dois.
SEM_VERIFICACAO = "unchecked"

router = APIRouter(tags=["Pre-atendimento telefonico"])

# challengeId em aberto ou aprovado, por telefone normalizado:
#   {"11999991111": {"challengeId": "...", "aprovado": False, "em": datetime}}
#
# Memoria do processo, e isso basta: o servico sobe com um worker so (app.py) e o
# webhook de pos-chamada cai neste mesmo processo, segundos depois do fim da
# ligacao. Se o servico reiniciar nesse intervalo, o `phoneVerificationId` nao
# vai - e uma ligacao sem o campo e exatamente o que acontece hoje.
_desafios = {}


class LookupRequest(BaseModel):
    """O telefone que a pessoa confirmou digito a digito."""

    phone: str


class VerifyRequest(BaseModel):
    """O codigo que chegou por SMS, junto do telefone que o pediu."""

    phone: str
    code: str


def verify_api_key(x_api_key: str = Header(..., alias="x-api-key")):
    """Mesma chave das outras integracoes do servico."""
    if x_api_key != get_settings().API_KEY_ZELLU_IA:
        raise HTTPException(status_code=401, detail="Nao autorizado")
    return x_api_key


def _zellu():
    """URL do backend Zellu e a chave do header x-api-key."""
    settings = get_settings()
    return settings.ZELLU_BACKEND_URL.rstrip("/"), settings.ai_usage_api_key


def _agora() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# O desafio guardado
# ---------------------------------------------------------------------------
def _esquecer_vencidos() -> None:
    """Tira da memoria o que a Zellu ja nao aceitaria."""

    limite = _agora() - VALIDADE_DA_PROVA

    for telefone in [t for t, d in _desafios.items() if d["em"] < limite]:
        del _desafios[telefone]


def _guardar(telefone: str, challenge_id: str) -> None:
    """Guarda o desafio novo desse telefone.

    Sobrescreve o anterior de proposito: do lado da Zellu, um lookup novo invalida
    o codigo que ainda nao foi usado (secao 3 da emenda). Guardar os dois deixaria
    aqui um challengeId que la ja nao vale.
    """

    _esquecer_vencidos()
    _desafios[telefone] = {"challengeId": challenge_id, "aprovado": False, "em": _agora()}


def _aprovar(telefone: str) -> Optional[str]:
    """Marca como provada a posse do numero. Devolve o challengeId aprovado."""

    desafio = _desafios.get(telefone)

    if not desafio:
        return None

    desafio["aprovado"] = True
    return desafio["challengeId"]


def challenge_aprovado(telefone) -> Optional[str]:
    """challengeId aprovado para esse telefone, ou None.

    E o que o pos-chamada manda em `phoneVerificationId`. None nao e erro: e uma
    ligacao que nao passou pela confirmacao por SMS, e ela segue valendo como
    sempre valeu enquanto a Zellu nao ligar a exigencia.
    """

    desafio = _desafios.get(normalize_phone(telefone))

    if not desafio or not desafio["aprovado"]:
        return None

    if desafio["em"] < _agora() - VALIDADE_DA_PROVA:
        return None

    return desafio["challengeId"]


# ---------------------------------------------------------------------------
# Conversa com a Zellu
# ---------------------------------------------------------------------------
async def _chamar_zellu(caminho: str, corpo: dict, timeout: float) -> Optional[dict]:
    """POST numa rota /api/ai/phone-identity/*. None quando nao deu.

    Assincrono de proposito: o lookup espera o SMS sair e pode levar segundos. Com
    o cliente sincrono, esse tempo travaria o event loop inteiro do servico - e o
    turno assincrono do chat, que corre no mesmo processo, pararia junto.
    """

    base, chave = _zellu()

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resposta = await client.post(
                f"{base}{caminho}",
                headers={"x-api-key": chave, "Content-Type": "application/json"},
                json=corpo,
            )
    except Exception as e:
        print(f"[IDENTIDADE] Falha ao chamar {caminho}: {e}")
        return None

    if resposta.status_code != 200:
        # Os ramos de negocio sao todos 200 (secao 6 da emenda). Um 4xx aqui e bug
        # nosso ou configuracao faltando, nunca passo de conversa.
        print(f"[IDENTIDADE] {caminho} veio {resposta.status_code}: {resposta.text[:300]}")
        return None

    try:
        return resposta.json() or {}
    except ValueError:
        print(f"[IDENTIDADE] {caminho} respondeu 200 com corpo que nao e JSON.")
        return None


# ---------------------------------------------------------------------------
# Rotas
# ---------------------------------------------------------------------------
@router.post(
    "/phone/identity/lookup",
    responses={
        200: {"description": "Outcome do numero (challenge, invalid_format, in_use...)"},
        401: {"description": "Nao autorizado"},
    },
    summary="Pede o codigo por SMS para o telefone que a pessoa confirmou",
    description=(
        "Chamado pelo agente de voz depois de repetir o numero digito a digito e "
        "receber a confirmacao. Quando o outcome e challenge, o SMS ja saiu."
    ),
)
async def consultar_telefone(
    pedido: LookupRequest,
    api_key: str = Depends(verify_api_key),
):
    """Repassa o telefone para a Zellu e devolve so o outcome ao agente."""

    telefone = normalize_phone(pedido.phone)

    if not telefone:
        # Nao sobrou digito nenhum: nao ha o que perguntar a Zellu. Mesmo outcome
        # que ela devolveria, e o unico ramo em que o agente pergunta de novo.
        print(f"[IDENTIDADE] {pedido.phone!r} nao tem digitos - invalid_format")
        return {"outcome": "invalid_format"}

    resposta = await _chamar_zellu(
        "/api/ai/phone-identity/lookup", {"phone": telefone}, TIMEOUT_LOOKUP
    )

    if resposta is None:
        return {"outcome": SEM_VERIFICACAO}

    outcome = resposta.get("outcome") or SEM_VERIFICACAO

    if outcome == "challenge":
        challenge_id = resposta.get("challengeId")

        if not challenge_id:
            print("[IDENTIDADE] challenge sem challengeId - seguindo sem verificacao.")
            return {"outcome": SEM_VERIFICACAO}

        _guardar(telefone, challenge_id)

    print(f"[IDENTIDADE] lookup {telefone} -> {outcome}")

    return {"outcome": outcome}


@router.post(
    "/phone/identity/verify",
    responses={
        200: {"description": "Outcome do codigo (found, verified, invalid, locked)"},
        401: {"description": "Nao autorizado"},
    },
    summary="Confere o codigo que a pessoa ditou",
    description=(
        "Chamado pelo agente de voz com os seis digitos que a pessoa leu do SMS. "
        "Quando o outcome e found ou verified, a posse do numero esta provada."
    ),
)
async def confirmar_codigo(
    pedido: VerifyRequest,
    api_key: str = Depends(verify_api_key),
):
    """Confere o codigo e, se passar, guarda a prova para o fim da ligacao."""

    telefone = normalize_phone(pedido.phone)
    desafio = _desafios.get(telefone)

    if not desafio:
        # Desafio desconhecido e um dos quatro casos de `invalid` (secao 4 da
        # emenda). Nao ha o que perguntar a Zellu sem um challengeId.
        print(f"[IDENTIDADE] verify sem desafio aberto para {telefone} - invalid")
        return {"outcome": "invalid"}

    resposta = await _chamar_zellu(
        "/api/ai/phone-identity/verify",
        {"challengeId": desafio["challengeId"], "code": (pedido.code or "").strip()},
        TIMEOUT_VERIFY,
    )

    if resposta is None:
        return {"outcome": SEM_VERIFICACAO}

    outcome = resposta.get("outcome") or SEM_VERIFICACAO

    if outcome not in ("found", "verified"):
        print(f"[IDENTIDADE] verify {telefone} -> {outcome}")
        return {"outcome": outcome}

    _aprovar(telefone)
    print(f"[IDENTIDADE] verify {telefone} -> {outcome}, posse provada")

    # `found` traz tambem o userId, e ele para aqui: o agente nao precisa dele
    # para nada, e quem esta do outro lado da conversa e uma pessoa. O nome vai
    # porque poupa perguntar de novo o que a Zellu ja sabe.
    devolver = {"outcome": outcome}

    for campo in ("firstName", "lastName"):
        if resposta.get(campo):
            devolver[campo] = resposta[campo]

    return devolver
