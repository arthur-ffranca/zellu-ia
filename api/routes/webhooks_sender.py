# -*- coding: utf-8 -*-
"""
Webhooks do Agente Sender (ZelinhU).

Endpoints para o ZelinhU (agente que representa o cliente):
- POST /api/webhooks/sender/message - Receber resposta da IA Empresa e gerar proxima msg do ZelinhU
- POST /api/webhooks/sender/status - Receber notificacao de status da negociacao

Fluxo correto:
1. Zellu Backend recebe msg inicial do ZelinhU
2. Backend envia para IA Empresa (/api/company/base-dados)
3. IA Empresa responde
4. Backend chama ESTE WEBHOOK com a resposta da empresa
5. ZelinhU analisa a resposta e gera a proxima mensagem
6. Backend envia msg do ZelinhU para IA Empresa
7. Loop ate negotiationComplete=true
"""

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from typing import Dict, Any, List, Optional, Tuple
from collections import defaultdict
from datetime import datetime
import asyncio
from decimal import Decimal
import hashlib
import uuid
import traceback
import httpx
import re

from src.llm import ChatModel

from config import get_settings
from src.clients import ZelluAPIClient
from src.pendencias_cliente import (
    MAX_TENTATIVAS_POR_PEDIDO,
    dar_baixa,
    filtrar_ja_solicitados,
    registrar_solicitacoes,
    render_para_cliente as render_pendencias_para_cliente,
    tentativas_invalidas,
    titulos_ao_cliente,
)
from src.utils.anexos import CAP_TOTAL, MAX_ANEXOS, ingerir_anexos, nomes_dos_anexos
from src.utils.veredito_documentos import (
    MOTIVO_CONFERIDO_NA_ENTREGA,
    VEREDITO_INDETERMINADO,
    VEREDITO_INVALIDO,
    VEREDITO_PARA_CONTRATO,
    julgar_documentos,
    traduzir_do_contrato,
    veredito_do_pedido,
)
from src.negotiation_ledger import LedgerUpdateBatch, NegotiationLedger
from src.open_dots.negotiation import Move, NegotiationRequest, ReadingError, ZelinhuNegotiator
from api.schemas_sender import (
    WebhookSenderMessageRequest,
    WebhookSenderMessageResponse,
    WebhookSenderMessageResponseData,
    WebhookSenderResumeRequest,
    WebhookSenderResumeResponse,
    WebhookSenderStatusRequest,
    WebhookSenderStatusResponse
)

router = APIRouter(
    prefix="/api/webhooks/sender",
    tags=["Webhooks Sender"]
)


# =============================================================================
# ARMAZENAMENTO EM MEMORIA (para desenvolvimento)
# =============================================================================

# Armazenar sessoes e contexto em memoria
_sender_sessions: Dict[str, Dict[str, Any]] = {}

# Serializa o turno do Sender por sessao (log de homolog de 23/08/2026).
#
# O backend entregou DUAS chamadas deste webhook para a mesma sessao com corpos
# diferentes - a descricao do ticket (round=1) e a saudacao da IA empresa
# (round=0). As duas rodaram ao mesmo tempo em cima do MESMO dict de sessao:
# leram o estado, chamaram o LLM e gravaram por cima uma da outra. O livro-razao
# terminou a rodada 2 com dois resultados opostos (5 abertos / 0 acordados e
# depois 0 abertos / 5 acordados).
#
# O fluxo de chat e o da IA empresa neste projeto ja serializavam por sessao
# (chat_locks e _ledger_locks em src/main.py); o do Sender nunca teve.
#
# O lock evita a corrida e a corrupcao do estado. Ele NAO deduplica sozinho:
# mensagens diferentes rodam uma depois da outra e cada uma geraria a sua
# contraproposta. Quem impede isso e a trava de turno logo abaixo.
_session_locks: Dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

# Turnos em que o ZelinhU ja falou, por sessao.
#
# Duas entregas do backend para a mesma sessao, com corpos diferentes, geraram
# DUAS mensagens do ZelinhU com 0,5s de diferenca (23:05:13.782 e 23:05:14.307
# no log de 23/08/2026). As duas passaram por todas as travas anteriores, porque
# tanto a chave de rodada (A3) quanto a de despacho (QA-01) sao derivadas do
# TEXTO da mensagem - textos diferentes nunca colidem.
#
# O cliente apareceu na frente da empresa com duas posicoes distintas na mesma
# rodada: uma oferecia desfazimento com devolucao de R$ 9.850, a outra criava
# multa diaria e abatimento de R$ 600. E o defeito Z-02 (escalada e contradicao
# entre rodadas) voltando por outra porta.
#
# A regra que falta e de negociacao, nao de transporte: o ZelinhU responde UMA
# vez por vez que a bola esta com ele. Se uma segunda mensagem chega enquanto o
# turno anterior ainda estava correndo, ela nao ganha resposta propria - entra
# no historico da sessao e o proximo turno a considera junto com o resto.
#
# O contador so avanca quando o turno realmente produziu mensagem: modo manual,
# espera pela decisao do cliente e reenvio ignorado nao gastam turno, senao uma
# entrega legitima logo depois seria descartada sem nunca ter sido respondida.
_answered_turns: Dict[str, int] = defaultdict(int)

# Status terminais que encerram a negociacao
# NOTE: agreement representa uma proposta submetida ao cliente, nao um encerramento definitivo.
TERMINAL_STATUSES = ["no_agreement", "timeout", "escalated", "cancelled"]

# Status validos
VALID_STATUSES = ["started", "negotiating", "agreement", "no_agreement", "timeout", "escalated", "cancelled"]

# Maximo de rodadas antes de encerrar e enviar proposta ao cliente.
# Fonte unica: config.NEGOTIATION_MAX_ROUNDS (achado Z-09).
MAX_ROUNDS = get_settings().NEGOTIATION_MAX_ROUNDS


def _round_cap(session: Optional[dict]) -> int:
    """
    Teto de rodadas DESTA sessao.

    Comeca em MAX_ROUNDS. A recusa do cliente estende o teto (ver
    _estender_teto_apos_recusa): sem isso a rodada da retomada ja nascia acima
    de MAX_ROUNDS e a primeira resposta da empresa ia direto ao cliente, sem
    contraproposta - o ZelinhU parava de negociar depois de qualquer recusa.
    """
    try:
        estendido = int((session or {}).get("roundCap") or 0)
    except (TypeError, ValueError):
        estendido = 0
    return max(MAX_ROUNDS, estendido)


def _estender_teto_apos_recusa(session: dict, rodada_da_recusa: int) -> int:
    """Da ao ZelinhU NEGOTIATION_ROUNDS_AFTER_REJECTION rodadas apos a recusa."""
    extra = max(1, get_settings().NEGOTIATION_ROUNDS_AFTER_REJECTION)
    teto = max(_round_cap(session), rodada_da_recusa + extra)
    session["roundCap"] = teto
    return teto

# Usado para reconhecer fala repetida da empresa nas ultimas mensagens da sessao.
MAX_HISTORY_MESSAGES = 8


# =============================================================================
# NEGOCIADOR DO ZELINHU (open_dots) - Lazy Init
# =============================================================================
# Substitui o SenderAgent (05/10/2026). A jogada de cada rodada e decidida em
# codigo (src/open_dots/negotiation/policy.py); o modelo le a empresa e redige.

_negotiator_instance: Optional[ZelinhuNegotiator] = None


def _get_negotiator() -> ZelinhuNegotiator:
    """Instancia unica do negociador, com o modelo da negociacao."""
    global _negotiator_instance
    if _negotiator_instance is None:
        settings = get_settings()
        model = settings.NEGOTIATION_MODEL or settings.OPENAI_MODEL
        # Leitura + redacao + uma correcao no gpt-5 passam de 60 s com folga; o
        # POST reverso do backend espera ate 120 s pela resposta inteira.
        llm = ChatModel(
            model=model,
            api_key=settings.OPENAI_API_KEY,
            temperature=settings.NEGOTIATION_TEMPERATURE,
            timeout=float(getattr(settings, "NEGOTIATION_LLM_TIMEOUT_S", 90) or 90),
            max_retries=0,
        )
        _negotiator_instance = ZelinhuNegotiator(llm=llm, max_rounds=MAX_ROUNDS)
        print(f"[SENDER-WEBHOOK] Negociador do ZelinhU inicializado com modelo {model} (max_rounds {MAX_ROUNDS})")
    return _negotiator_instance


# =============================================================================
# IDEMPOTENCIA POR RODADA (diagnostico A3, 22/08/2026)
# =============================================================================
# O backend estoura o timeout de 60s, reenvia o webhook, e o Sender gerava uma
# SEGUNDA contraproposta para a mesma rodada - duas mensagens do ZelinhU com
# ~10s de diferenca, ambas postadas. Duas travas resolvem:
#
#   1. entrada  - reenvio da mesma rodada com a mesma mensagem da empresa nao
#                 reprocessa: devolve o resultado anterior, ou avisa que ainda
#                 esta processando
#   2. despacho - a mesma mensagem nao e postada duas vezes na mesma rodada
#
# Tudo por sessao, junto do resto do estado do Sender.

ROUND_STATE_IN_PROGRESS = "in_progress"
ROUND_STATE_DONE = "done"


def _fingerprint(text: str) -> str:
    """Impressao digital curta e estavel de uma mensagem."""
    return hashlib.sha256((text or "").strip().encode("utf-8")).hexdigest()[:16]


def _round_key(round_number: int, message_body: str) -> str:
    return f"{round_number}:{_fingerprint(message_body)}"


def _requested_round(round_value: Optional[int], fallback: int) -> int:
    """
    Rodada que veio no webhook, com `0` valendo como rodada de verdade.

    A forma antiga era `request.round or fallback`. Em Python `0` e falsy, entao
    uma entrega com `round=0` - que o backend usou no log de 23/08/2026 para a
    saudacao inicial da IA empresa - era lida como "rodada ausente" e caia no
    fallback. Duas entregas com rodadas diferentes (1 e 0) viravam a mesma
    rodada 1, e a idempotencia por rodada passava a comparar coisas que nao sao
    o mesmo turno.
    """
    return round_value if round_value is not None else fallback


def _get_round_record(session: dict, round_number: int, message_body: str) -> Optional[dict]:
    return (session.get("rounds") or {}).get(_round_key(round_number, message_body))


def _mark_round_in_progress(session: dict, round_number: int, message_body: str) -> None:
    session.setdefault("rounds", {})[_round_key(round_number, message_body)] = {
        "state": ROUND_STATE_IN_PROGRESS,
        "startedAt": datetime.now().isoformat(),
    }


def _mark_round_done(session: dict, round_number: int, message_body: str, result: dict) -> None:
    record = {"state": ROUND_STATE_DONE, "finishedAt": datetime.now().isoformat()}
    record.update(result)
    session.setdefault("rounds", {})[_round_key(round_number, message_body)] = record


def _clear_round(session: dict, round_number: int, message_body: str) -> None:
    """Libera a rodada apos falha, para que um reenvio legitimo possa reprocessar."""
    (session.get("rounds") or {}).pop(_round_key(round_number, message_body), None)


# Sentinela devolvida quando o envio e ignorado por ja ter sido feito. NAO e
# falha de transporte: quem chama precisa distinguir uma coisa da outra, senao
# um no-op idempotente derruba o loop como se o backend tivesse recusado.
DISPATCH_DUPLICATE = {"duplicate": True, "success": True}

# Turno assincrono: o backend respondeu 202 e vai processar o turno da empresa
# por fora, devolvendo a resposta dela num callback novo (confirmado com o time
# do backend em 25/08/2026 - e status HTTP mesmo, nao um 200 com accepted:true).
#
# Precisa ser distinto de "sucesso com corpo" por um motivo pratico: o corpo de
# um 202 e um ack vazio. Se o loop tratasse 202 como 200, leria messageBody="",
# analisaria uma mensagem de empresa VAZIA, redigiria uma contraproposta para o
# nada e a despacharia. Tambem nao e falha: o despacho chegou, e a resposta vem
# depois.
DISPATCH_ACCEPTED = {"accepted": True, "success": True}


# =============================================================================
# AGUARDANDO DECISAO DO CLIENTE (auditoria QA-02/04/05, 23/08/2026)
# =============================================================================
# Quando o ZelinhU emite [PROPOSTA PARA APROVACAO DO CLIENTE], a negociacao
# automatica acabou: a bola esta com o cliente. Antes isso nao mudava estado
# nenhum - session["status"] so era tocado dentro de `if negotiation_complete:`,
# e send_to_client devolve False. Consequencias no log de homolog:
#
#   - a empresa mandou nova oferta as 11:43, depois da proposta das 11:42
#   - o status reportado (agreement) era residuo de uma rodada anterior
#
# A flag e INTERNA: o contrato com o backend continua recebendo "agreement" em
# send_negotiation_status. E ela NAO torna a sessao terminal - TERMINAL_STATUSES
# exclui "agreement" de proposito, porque o /resume precisa poder reabrir.

def _mark_awaiting_client_decision(session: dict, action: str) -> None:
    """Registra que a proposta foi encaminhada e a decisao e do cliente."""
    if action != "send_to_client":
        return
    session["awaitingClientDecision"] = True
    session["status"] = "agreement"
    session["awaitingSince"] = datetime.now().isoformat()
    print(
        "[SENDER-WEBHOOK] Proposta encaminhada ao cliente - novas rodadas "
        "automaticas ficam bloqueadas ate a decisao dele"
    )


def _is_awaiting_client_decision(session: dict) -> bool:
    return bool(session.get("awaitingClientDecision"))


def _clear_awaiting_client_decision(session: dict) -> None:
    """O cliente decidiu (ou recusou): a negociacao volta a andar."""
    session.pop("awaitingClientDecision", None)
    session.pop("awaitingSince", None)


# =============================================================================
# AGUARDANDO SOLICITACAO (spec 20260901, secao 3)
# =============================================================================
# Emitido um pedido, a negociacao PAUSA e sai do ar ate uma pessoa responder -
# horas ou dias, sem prazo, porque nada expira sozinho do lado do backend.
#
# Sem esta trava o ZelinhU continuaria contrapondo enquanto o cliente procura o
# comprovante, e a rodada seguinte chegaria como se o pedido nunca tivesse
# existido. E o mesmo desenho da espera pela decisao do cliente, pelo mesmo
# motivo: quem destrava e o resume, que traz a resposta.

def _mark_awaiting_request(session: dict, pedidos: List[Dict[str, Any]]) -> None:
    """Registra que a bola esta com o humano que precisa atender o pedido.

    Guarda tambem O QUE foi pedido, e nao so quantos pedidos sairam. Sem isto,
    quando o documento chega na retomada nao ha contra o que compara-lo: o
    ZelinhU nao lembra que pediu "comprovante de marco" e nao tem como dizer que
    recebeu o de fevereiro (spec 01/09, §5.1 - a correcao e nossa).
    """
    session["awaitingRequest"] = True
    session["awaitingRequestSince"] = datetime.now().isoformat()
    session["awaitingRequestCount"] = len(pedidos)
    session["solicitacoesPendentes"] = pedidos
    # O registro que sobrevive a pausa: `solicitacoesPendentes` sai no resume, e
    # sem isto o ZelinhU nao saberia o que ja pediu (QA de 10/09, achado 4).
    registrar_solicitacoes(session, pedidos)
    print(
        f"[SENDER-WEBHOOK] {len(pedidos)} solicitacao(oes) emitida(s) - negociacao "
        f"pausada ate o resume. Novas rodadas automaticas ficam bloqueadas"
    )


def _is_awaiting_request(session: dict) -> bool:
    return bool(session.get("awaitingRequest"))


def _clear_awaiting_request(session: dict) -> None:
    """As pendencias foram resolvidas: a negociacao volta a andar.

    `solicitacoesPendentes` sai junto - mas o julgamento do que chegou acontece
    ANTES desta chamada, em `_resume_negotiation`. Quem inverter essa ordem tira
    do ZelinhU a memoria do que ele mesmo pediu.
    """
    session.pop("awaitingRequest", None)
    session.pop("awaitingRequestSince", None)
    session.pop("awaitingRequestCount", None)
    session.pop("solicitacoesPendentes", None)


# =============================================================================
# RETOMADA APOS A RECUSA DO CLIENTE (auditoria QA de 24/08/2026)
# =============================================================================
# O backend nao tem uma URL por operacao: manda tudo para a SENDER_WEBHOOK_URL e
# espera que a gente distinga pelo campo `action`. Nunca implementamos esse
# roteamento - o `action` sequer existia no schema de entrada -, entao a recusa
# do cliente chegava em /message como se fosse mensagem da empresa, batia no
# guard de espera pela decisao do cliente e voltava vazia. O backend registrava
# "nenhum canal entregou mensagem" e a negociacao congelava.
#
# O endpoint /resume, que faz a coisa certa, nunca chegou a ser chamado.

RESUME_ACTION = "resume_negotiation"

# =============================================================================
# SOLICITACOES NA NEGOCIACAO (spec 20260901-spec-solicitacoes-na-negociacao.md)
# =============================================================================
REQUEST_ACTION = "request"

# Teto de pedidos por rajada. O backend corta em 10 PENDENTES na negociacao
# inteira; 6 e o que dissemos que emitiriamos, e serve para um turno sozinho nao
# consumir a cota toda.
MAX_REQUESTS_POR_RAJADA = 6

# Motivos de retomada (secao 4.1). O ZelinhU nao inventa nenhum: sao os valores
# que o backend manda em resumeReason.
RESUME_CLIENT_REJECTED = "client_rejected"
RESUME_REQUEST_FULFILLED = "request_fulfilled"
RESUME_REQUEST_DENIED = "request_denied"
RESUME_REQUEST_EXPIRED = "request_expired"

# Recusas do backend a um action=request (secao 2.5). Em 400 a mensagem NAO e
# salva do lado deles - a recusa e explicita justamente para a gente saber.
REQUEST_LIMIT_REACHED = "REQUEST_LIMIT_REACHED"
REQUEST_AND_TERMINAL_CONFLICT = "REQUEST_AND_TERMINAL_CONFLICT"
REQUEST_TITLE_TOO_LONG = "REQUEST_TITLE_TOO_LONG"
REQUEST_MALFORMED = "REQUEST_MALFORMED"

# Sinal interno: o backend recusou por lotacao. Nao e falha de transporte nem
# defeito de payload - o mesmo pedido volta a ser aceito assim que alguem
# responder uma pendencia, e reenviar o corpo identico e seguro (secao 2.5).
DISPATCH_REQUEST_LIMIT = {"__dispatch__": "request_limit_reached"}

# =============================================================================
# FALHA DO TURNO DA IA DA EMPRESA (plano de 25/08/2026, §2.3)
# =============================================================================
# Ate agora, turno da empresa que falhava virava silencio: o erro ficava no log
# do backend e a negociacao so voltava pelo cron, 15 minutos depois.
#
# Este handler existe ANTES do backend ligar o emissor, e a ordem importa: uma
# action desconhecida cai no caminho normal e seria processada como se fosse
# mensagem da empresa - o aviso de falha viraria uma fala falsa da empresa no
# chat do cliente.
#
# A CONDUTA (repetir a rodada, escalar ou avisar o cliente) ainda nao foi
# decidida. Ate la, registramos e confirmamos o recebimento sem gerar mensagem
# nenhuma: e o comportamento seguro e reversivel.
COMPANY_AI_FAILED_ACTION = "company_ai_failed"

# =============================================================================
# CONFERENCIA DE UM PEDIDO (contrato 20260911, §3.2)
# =============================================================================
# O backend chama ANTES de dar o pedido por atendido, no momento em que a pessoa
# envia o arquivo. Propusemos uma rota propria e eles recusaram, com razao: o
# prefixo /api/webhooks/sender/ e o dos webhooks que ELES chamam em NOS, e uma
# rota nova ali tornaria impossivel saber pelo caminho quem chama quem. Fica na
# URL de sempre, distinguida pelo `action`, como todo o resto.
VERIFY_REQUEST_ACTION = "verify_request"

# Desfecho de um pedido, no vocabulario do backend (contrato 20260911, §2.1).
STATUS_PEDIDO_ATENDIDO = "fulfilled"
STATUS_PEDIDO_RECUSADO = "declined"

# Actions que nunca podem ser engolidas pela trava de turno: nao sao mensagens
# da negociacao, sao eventos sobre ela.
ACTIONS_FORA_DA_TRAVA_DE_TURNO = frozenset({RESUME_ACTION, COMPANY_AI_FAILED_ACTION})


def _resume_key(
    rejection_count: Optional[int],
    client_feedback: str,
    requests: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """
    Identidade da retomada, para reconhecer a reentrega da mesma.

    O REQUESTID E A CHAVE (contrato 20260911, §2.3)
    -----------------------------------------------
    O backend passou a REENVIAR a rajada que nao chegou ate nos - a fila dele e
    em memoria, e um restart no meio fazia o resume evaporar com a negociacao
    parada sem ninguem saber. Junto veio o pedido explicito: "tratem o requestId
    como chave; as presigned vem NOVAS a cada tentativa, nao comparem por URL".

    E nao da para comparar por texto tambem: o `clientFeedback` e redigido por
    eles a cada tentativa, e na mesma emenda o texto da rajada mudou de "foram
    atendidas" para "foram respondidas". Uma palavra diferente entre duas
    tentativas faria o reenvio parecer informacao nova - segundo julgamento,
    segunda baixa, segunda mensagem do ZelinhU na mesma altura da negociacao.

    Sem `requests` (retomada por recusa do cliente, ou backend anterior a
    Etapa 1), a chave e a de sempre.
    """
    ids = sorted(
        (pedido.get("requestId") or "").strip()
        for pedido in (requests or [])
        if isinstance(pedido, dict) and (pedido.get("requestId") or "").strip()
    )
    if ids:
        return "requests:" + ",".join(ids)
    return f"{rejection_count or 0}:{_fingerprint(client_feedback)}"


def _resume_already_handled(
    session: Optional[dict],
    rejection_count: Optional[int],
    client_feedback: str,
    requests: Optional[List[Dict[str, Any]]] = None,
) -> Optional[dict]:
    """
    Resultado de uma retomada ja processada, se esta for reentrega da mesma.

    A recusa do cliente sempre reabre a negociacao - mas a MESMA recusa,
    reenviada por timeout do backend, nao pode gerar uma segunda mensagem do
    ZelinhU. Uma recusa nova traz outro motivo (ou outro rejectionCount) e cai
    em outra chave.

    O QUE FALHOU NAO ESTA PROCESSADO (contrato 20260911, §2.3)
    ----------------------------------------------------------
    Ate aqui qualquer retomada ja vista devolvia o resultado guardado, INCLUSIVE
    quando o despacho dela nunca chegou ao backend. O reenvio que eles acabaram
    de criar existe exatamente para esse caso - a negociacao parada porque a
    mensagem se perdeu - e devolver "ja processei" seria responder ao socorro
    com o eco do afogamento.
    """
    if not session:
        return None
    anterior = (session.get("resumes") or {}).get(
        _resume_key(rejection_count, client_feedback, requests)
    )
    if anterior and not anterior.get("success"):
        print(
            "[SENDER-WEBHOOK] Retomada ja vista, mas o despacho dela nunca chegou "
            "ao backend - reprocessando em vez de repetir a falha"
        )
        return None
    return anterior


def _mark_resume_handled(
    session: dict,
    rejection_count: Optional[int],
    client_feedback: str,
    result: dict,
    requests: Optional[List[Dict[str, Any]]] = None,
) -> None:
    session.setdefault("resumes", {})[
        _resume_key(rejection_count, client_feedback, requests)
    ] = result

# Documentos guardados na sessao. Uma negociacao longa com varias solicitacoes
# acumula anexos, e o prompt tem um teto - os mais recentes sao os que importam
# para a rodada que esta sendo escrita agora.
MAX_DOCUMENTOS_NA_SESSAO = 12

_DISPATCHED_MAX = 40


def dispatch_key_for(origin_message: str) -> str:
    """
    Chave de despacho: a mensagem que ORIGINOU este envio.

    Auditoria de 23/08/2026 (QA-01): a versao anterior usava a impressao digital
    da NOSSA mensagem mais o numero da rodada. As mensagens de send_to_client e
    escalate sao templates fixos - texto identico byte a byte em momentos
    diferentes. Quando a rodada tambem se repetiu, o guard bloqueou uma
    contraproposta legitima ("round 7 ja despachado") e o loop parou com a
    negociacao em aberto.

    A mensagem da outra parte que disparou o turno e o que realmente identifica
    o despacho: duas respostas nossas a mensagens diferentes nunca sao duplicata,
    mesmo que o texto saia igual.
    """
    return _fingerprint(origin_message)


def _company_response_with_attachment_context(message_body: str, attachments: Optional[List[Any]]) -> str:
    """Acrescenta contexto de anexo sem expor URLs presigned para LLM/logica."""
    total = len(attachments or [])
    if total <= 0:
        return message_body

    marcador = f"[{total} arquivo(s) anexado(s) pela empresa]"
    corpo = (message_body or "").strip()
    if not corpo:
        return marcador
    if marcador in corpo:
        return corpo
    return f"{corpo}\n\n{marcador}"


async def _store_company_response_attachments(session: dict, attachments: Optional[List[Any]]) -> List[Dict[str, str]]:
    """Le anexos enviados pela empresa em company_response e guarda na sessao."""
    if not attachments:
        return []

    documentos = await ingerir_anexos(attachments)
    for doc in documentos:
        # O agente precisa saber de quem veio: comprovante enviado pela EMPRESA
        # e prova do que ela diz ter feito, nao documento do cliente.
        doc["remetente"] = "empresa"
    if documentos:
        acumulados = session.setdefault("documentosRecebidos", [])
        # Reenvio do mesmo webhook (timeout do backend) nao duplica o documento
        ja_lidos = {(d.get("nome"), d.get("texto")) for d in acumulados}
        documentos = [d for d in documentos if (d.get("nome"), d.get("texto")) not in ja_lidos]
        acumulados.extend(documentos)
        del acumulados[:-MAX_DOCUMENTOS_NA_SESSAO]
        lidos = sum(1 for doc in documentos if doc.get("texto"))
        print(
            f"[SENDER-WEBHOOK] company_response trouxe {len(documentos)} anexo(s); "
            f"{lidos} com texto aproveitado"
        )
    return documentos


def _already_dispatched(session: Optional[dict], dispatch_key: str) -> bool:
    """Se ja despachamos uma resposta para esta mesma mensagem de origem."""
    if session is None or not dispatch_key:
        return False
    return dispatch_key in (session.get("dispatched") or [])


def _mark_dispatched(session: Optional[dict], dispatch_key: str) -> None:
    if session is None or not dispatch_key:
        return
    despachadas = session.setdefault("dispatched", [])
    if dispatch_key not in despachadas:
        despachadas.append(dispatch_key)
    del despachadas[:-_DISPATCHED_MAX]


# =============================================================================
# GUARDA DE DESPACHO PERSISTIDA (sugestao 8 da spec de 01/09, secao 4.3)
# =============================================================================
# `_mark_dispatched` vive num dict da sessao, em memoria. No restart do processo
# essa memoria some, o ZelinhU redespacha o mesmo turno e a IA da empresa roda de
# novo - custo de LLM em cima de uma conversa que ja andou.
#
# Nenhuma chave do lado do backend pega isso: no restart nem o `round` nem o
# corpo da NOSSA mensagem sobrevivem, e sem identidade estavel nao existe formula
# que acerte. Eles escolheram errar para o lado do turno a mais, que e visivel e
# auditado, e apontaram o unico conserto de raiz - do nosso lado: registrar o
# despacho num lugar que sobreviva ao restart.
#
# O /api/ai/idempotency/check-or-create e persistido, atomico e tem TTL de 24h.
# Contrato sondado em 08/09/2026: corpo {requestId, endpoint}, 200 com hit=false
# na primeira chamada e hit=true nas seguintes.
#
# O QUE A SONDAGEM MUDOU NO DESENHO
# ---------------------------------
# NAO existe liberacao nem conclusao que a gente alcance - campos a mais sao
# ignorados e `pending` fica true ate o TTL. Entao "reservar antes de despachar"
# pioraria o modo de falha: POST que falha depois da reserva deixa a chave
# envenenada por 24h, e o reenvio legitimo e engolido - negociacao morta em
# silencio, que e exatamente o custo caro que ninguem quis pagar.
#
# Por isso a reserva fica PENDENTE ate o despacho confirmar. Quem lembra que ela
# esta em aberto somos nos, ja que nao da para desfaze-la la.
#
# A janela de silencio que sobra e "despacho falhou E o processo reiniciou antes
# da retentativa". Nela o Bucket D do cron deles (15 min) ainda retoma.
IDEMPOTENCY_PATH = "/api/ai/idempotency/check-or-create"

# Rotulo do chamador. A sondagem mostrou que a identidade e SO o requestId - a
# mesma requestId com outro endpoint deu hit -, entao isto e legenda para quem
# for ler os registros deles, nunca parte da chave.
IDEMPOTENCY_LABEL = "sender-reverse-post"


def _chave_de_despacho_persistida(session_id: str, dispatch_key: str) -> str:
    """
    Identidade do despacho para o armazenamento persistido.

    SEM `round` de proposito, por duas razoes que apontam para o mesmo lado:

    1. E a semantica da guarda em memoria, que identifica o despacho pela
       mensagem que o ORIGINOU. Colocar a rodada aqui faria as duas guardas
       responderem coisas diferentes sobre o mesmo turno.
    2. O `round` regride no restart (aviso de 01/09) - e o restart e justamente o
       caso que esta chave existe para pegar. A mensagem da EMPRESA, que e o que
       o `dispatch_key` resume, chega igual no reenvio do webhook.
    """
    return f"sender-dispatch|{session_id}|{dispatch_key}"


def _tem_reserva_pendente(session: Optional[dict], dispatch_key: str) -> bool:
    """Reserva nossa cujo despacho nunca confirmou."""
    if session is None or not dispatch_key:
        return False
    return dispatch_key in (session.get("reservasPendentes") or [])


def _marcar_reserva_pendente(session: Optional[dict], dispatch_key: str) -> None:
    if session is None or not dispatch_key:
        return
    pendentes = session.setdefault("reservasPendentes", [])
    if dispatch_key not in pendentes:
        pendentes.append(dispatch_key)
    del pendentes[:-_DISPATCHED_MAX]


def _confirmar_reserva(session: Optional[dict], dispatch_key: str) -> None:
    """O despacho saiu: a reserva deixa de estar em aberto."""
    if session is None or not dispatch_key:
        return
    pendentes = session.get("reservasPendentes") or []
    if dispatch_key in pendentes:
        pendentes.remove(dispatch_key)


async def _e_redespacho_apos_restart(
    session: Optional[dict], session_id: str, dispatch_key: str
) -> bool:
    """
    Pergunta ao backend se este despacho ja saiu - inclusive antes de um restart.

    Returns:
        True SOMENTE quando a chave ja estava reservada e a reserva nao e nossa
        de uma tentativa que falhou. Em qualquer duvida - flag desligada, chave
        ausente, timeout, status inesperado, corpo que nao reconhecemos -
        devolve False e o despacho segue como hoje.

        A assimetria e deliberada, e e a mesma que o backend adotou: despachar a
        mais custa um turno de LLM, visivel e auditado; despachar a menos mata a
        negociacao em silencio.
    """
    settings = get_settings()
    if not settings.NEGOTIATION_DISPATCH_IDEMPOTENCY_ENABLED:
        return False

    # Sem chave de despacho nao ha o que identificar - e nao e caso de borda: e a
    # RAJADA de solicitacoes. Os pedidos vao de proposito sem `dispatch_key`
    # porque compartilham a mensagem de origem, e todos na mesma rodada. Uma
    # chave so para os seis faria cinco deles serem engolidos aqui.
    if not dispatch_key or not session_id:
        return False

    if _tem_reserva_pendente(session, dispatch_key):
        print(
            f"[SENDER-WEBHOOK] Reserva de despacho em aberto (chave {dispatch_key}) - "
            f"isto e retentativa de um envio que falhou, nao redespacho. Segue."
        )
        return False

    chave = _chave_de_despacho_persistida(session_id, dispatch_key)

    try:
        async with httpx.AsyncClient(
            timeout=settings.NEGOTIATION_DISPATCH_IDEMPOTENCY_TIMEOUT
        ) as client:
            resposta = await client.post(
                f"{settings.ZELLU_BACKEND_URL}{IDEMPOTENCY_PATH}",
                json={"requestId": chave, "endpoint": IDEMPOTENCY_LABEL},
                headers={
                    "Content-Type": "application/json",
                    # Mesma chave da familia /api/ai (a de /api/ai/usage), que e a
                    # que a sondagem de 08/09 autenticou neste endpoint. O POST
                    # reverso usa outra: e outra familia de rotas.
                    "x-api-key": settings.ai_usage_api_key,
                },
            )

        if resposta.status_code != 200:
            print(
                f"[SENDER-WEBHOOK] Guarda persistida indisponivel (HTTP "
                f"{resposta.status_code}) - despachando como antes dela existir"
            )
            return False

        corpo = resposta.json() or {}
    except Exception as e:  # noqa: BLE001 - timeout, transporte, corpo nao-JSON
        print(
            f"[SENDER-WEBHOOK] Guarda persistida falhou ({type(e).__name__}: {e}) - "
            f"despachando como antes dela existir"
        )
        return False

    hit = corpo.get("hit")

    if hit is True:
        return True

    if hit is False:
        # A reserva e nossa a partir de agora, e fica em ABERTO ate o despacho
        # confirmar: se o POST falhar, a proxima tentativa nao pode ser bloqueada
        # pela nossa propria reserva - nao ha como desfaze-la do lado deles.
        _marcar_reserva_pendente(session, dispatch_key)
        return False

    print(
        f"[SENDER-WEBHOOK] Guarda persistida devolveu corpo sem `hit` "
        f"({sorted(corpo.keys())}) - despachando como antes dela existir"
    )
    return False


# =============================================================================
# LIVRO-RAZAO DA SESSAO (Fase 2)
# =============================================================================

def _get_ledger(session: dict) -> NegotiationLedger:
    """Livro-razao da sessao. Em memoria, como o resto da sessao."""
    return NegotiationLedger.from_dict(session.get("ledger"))


def _save_ledger(session: dict, ledger: NegotiationLedger) -> None:
    session["ledger"] = ledger.to_dict()


def _validate_state(session: dict, round_number: int, contradictions: List[str]) -> str:
    """
    Passo 2 do turno: validacao em codigo puro, antes de redigir.

    Junta o que o livro-razao diz sobre o estado atual e devolve um relatorio
    que entra no prompt de redacao: contradicoes e itens abandonados. O minimo
    do cliente e decidido pela politica (open_dots/negotiation/policy.py).
    """
    ledger = _get_ledger(session)
    ledger.current_round = max(ledger.current_round, round_number)

    linhas: List[str] = []

    if contradictions:
        linhas.append("CONTRADICOES DETECTADAS NO LIVRO-RAZAO (corrija nesta mensagem):")
        linhas.extend(f"- {c}" for c in contradictions)

    abandonados = ledger.abandoned_items()
    if abandonados:
        linhas.append("")
        linhas.append("PEDIDOS SEM MOVIMENTO HA DUAS OU MAIS RODADAS:")
        linhas.append(
            "Retome cada um ou diga expressamente que o cliente esta abrindo mao "
            "dele e por que."
        )
        linhas.extend(f"- {item.description}" for item in abandonados)

    if not linhas:
        return ""

    return "# RELATORIO DE VALIDACAO (PASSO 2)\n\n" + "\n".join(linhas)


# =============================================================================
# FUNCOES AUXILIARES DO ZELINHU
# =============================================================================

def _generate_zelinhu_message_fallback(
    action: str,
    client_name: str,
    round_number: int,
    estimated_value: float,
    company_response: str,
    pendencias_do_cliente: Optional[List[str]] = None,
) -> tuple[str, bool]:
    """
    FALLBACK: Gera mensagem do ZelinhU por templates (usado se IA falhar).

    Args:
        action: "send_to_client", "counter_proposal" ou "escalate"
        client_name: Nome do cliente
        round_number: Rodada atual
        estimated_value: Valor estimado do dano
        company_response: Resposta da empresa (para referencia)

    Returns:
        (mensagem, negotiation_complete)
    """
    first_name = client_name.split()[0] if client_name else "cliente"

    if action in ("accept", "send_to_client"):
        # AS PENDENCIAS NAO PODEM SUMIR NO FECHAMENTO (auditoria QA de 08/09/2026)
        # -----------------------------------------------------------------------
        # Este texto dizia so "aceite ou recuse" - e o QA mediu o custo disso: na
        # ultima mensagem da negociacao auditada a empresa ainda aguardava NF/OS,
        # comprovantes, tres janelas de horario, a localizacao do veiculo e os
        # dados bancarios do reembolso. Tudo isso evaporava aqui, porque o
        # encerramento so sabia falar da decisao sobre a proposta.
        bloco_pendencias = render_pendencias_para_cliente(pendencias_do_cliente)

        message = f"""Obrigado pelas propostas apresentadas durante esta negociacao.

Registro a proposta da empresa para analise do {first_name}. A decisao de aceitar ou recusar pertence ao cliente, e retornaremos com a posicao dele.

O cliente sera notificado e podera responder diretamente."""

        if bloco_pendencias:
            message += f"\n\n{bloco_pendencias}"

        message += "\n\n[PROPOSTA PARA APROVACAO DO CLIENTE]"
        return message, False

    elif action == "escalate":
        message = f"""Infelizmente, apos {round_number} rodadas de negociacao, nao foi possivel chegar a um acordo satisfatorio.

{first_name} optou por escalar este caso para instancias superiores (Procon, Juizado Especial ou outros orgaos competentes).

A empresa tera nova oportunidade de apresentar proposta durante o processo formal.

[NEGOCIACAO ENCERRADA - ESCALADO]"""
        return message, True

    else:  # counter_proposal - so quando o modelo falhou: mantem o pedido, sem ceder
        from src.open_dots.negotiation import writer as zelinhu_writer
        from src.open_dots.negotiation.policy import HOLD

        valor = _parse_brl_number(str(estimated_value)) if estimated_value else None
        move = Move(action="counter_proposal", posture=HOLD, reason="texto seguro",
                    ask=Decimal(str(valor)) if valor else None)
        return zelinhu_writer.fallback(move, client_name=client_name), False


# =============================================================================
# FUNCOES COM IA (negociador do ZelinhU em src/open_dots/negotiation)
# =============================================================================

def _last_zelinhu_message(session: dict) -> str:
    for msg in reversed(session.get("messages") or []):
        if msg.get("origin") == "zelinhu":
            return msg.get("body", "") or ""
    return ""


async def _analyze_company_response(response_text: str, round_number: int, session: dict) -> dict:
    """
    Le a resposta da empresa e decide a jogada do ZelinhU.

    1. o modelo extrai FATOS da mensagem (oferta, ultima proposta, pedidos ao
       cliente, itens do livro-razao) - nenhuma recomendacao;
    2. o livro-razao e as solicitacoes sao atualizados como antes;
    3. a jogada sai da politica em codigo (open_dots/negotiation/policy.py).

    Sem leitura (modelo fora), nao ha fallback por palavra-chave: a politica roda
    sem oferta conhecida e o ZelinhU MANTEM o pedido - nunca aceita por engano.

    Retorna dict com action, reason, keywords_found (compatibilidade),
    ai_analysis (requests, agreement_reached, agreement_details, move) e, sem
    leitura, degraded=True.
    """
    negotiator = _get_negotiator()
    ledger = _get_ledger(session)
    reading: Optional[dict] = None
    degraded = False
    try:
        reading = await negotiator.read(
            session, response_text,
            last_zelinhu_message=_last_zelinhu_message(session),
            ledger=ledger,
        )
    except ReadingError as e:
        print(f"[SENDER-WEBHOOK] ERRO: leitura da resposta da empresa indisponivel - {e}")
        degraded = True
    except Exception as e:  # noqa: BLE001
        print(f"[SENDER-WEBHOOK] ERRO inesperado na leitura: {type(e).__name__}: {e}")
        traceback.print_exc()
        degraded = True

    analysis: Dict[str, Any] = dict(reading or {})
    contradictions: List[str] = []
    if reading:
        contradictions = ledger.apply(
            LedgerUpdateBatch(items=reading.get("ledger_updates") or []),
            round_number=round_number,
            actor="empresa",
        )
        _save_ledger(session, ledger)

        # O que ja foi pedido nao sai de novo (auditoria QA de 10/09).
        novos, repetidos = filtrar_ja_solicitados(
            reading.get("requests"), session.get("solicitacoesFeitas")
        )
        analysis["requests"] = novos
        if novos or repetidos:
            print(
                f"[SOLICITACOES] turno={round_number} detectadas={len(novos) + len(repetidos)} "
                f"ja_feitas={len(repetidos)} novas={len(novos)} "
                f"ao_cliente={len(titulos_ao_cliente(novos))}"
            )
        if contradictions:
            print(f"[LEDGER] Contradicoes na resposta da empresa: {contradictions}")
        print(f"[LEDGER] Itens: {len(ledger.items)} | abertos: {len(ledger.open_items())} "
              f"| acordados: {len(ledger.settled_items())}")

    session["validation_report"] = _validate_state(session, round_number, contradictions)

    teto = _round_cap(session)
    move = negotiator.decide(session, reading, round_number, teto, ledger=ledger)
    from src.open_dots.negotiation.policy import CLOSE_MET
    # Empresa que atendeu o pedido (aceite expresso OU oferta que cobre o valor)
    # e acordo: o fechamento vence o pedido ao cliente, como sempre venceu.
    analysis["agreement_reached"] = bool((reading or {}).get("accepts_client_ask")) or move.posture == CLOSE_MET
    if move.action == "send_to_client" and move.offer is not None:
        details = dict(analysis.get("agreement_details") or {})
        if details.get("value") in (None, ""):
            details["value"] = float(move.offer)
            details.setdefault("summary", (reading or {}).get("offer_summary") or "")
            details.setdefault("conditions", [])
            analysis["agreement_details"] = details
    analysis["move"] = move.to_dict()
    action = move.action

    print(
        f"[ZELINHU] rodada={round_number}/{teto} jogada={move.posture} acao={action} "
        f"pedido={move.ask} oferta={move.offer} piso={move.floor} | {move.reason}"
    )

    # PEDIDO AO CLIENTE ANTES DE FECHAR (auditoria QA de 10/09, achado 6): pedido
    # e fechamento nao saem juntos. Sem acordo, o pedido vence e a negociacao
    # pausa; fecha depois do resume.
    if _pedido_ao_cliente_vence_o_fechamento(action, analysis, round_number, max_rounds=teto):
        print(
            "[SOLICITACOES] Fechamento adiado: a empresa pediu ao cliente itens "
            "que ainda nao foram solicitados - o pedido sai antes da proposta"
        )
        action = "counter_proposal"
        analysis["move"] = Move(
            action=action, posture="manter", reason="pedido ao cliente antes de fechar",
            ask=move.ask, previous_ask=move.previous_ask, offer=move.offer, floor=move.floor,
        ).to_dict()

    result = {
        "action": action,
        "reason": ("[DEGRADADO] leitura indisponivel | " if degraded else "") + move.reason,
        "keywords_found": [],
        "ai_analysis": analysis,
    }
    if degraded:
        result["degraded"] = True
    if analysis.get("agreement_reached"):
        result["agreement_details"] = analysis.get("agreement_details")
    return result


async def _generate_zelinhu_message(
    action: str,
    client_name: str,
    round_number: int,
    estimated_value: float,
    company_response: str,
    session: dict,
    ai_analysis: dict = None
) -> tuple[str, bool]:
    """
    Redige a mensagem do ZelinhU para a jogada ja decidida.

    A jogada vem em ai_analysis['move']. No fechamento (send_to_client) o texto
    e redigido pelo modelo, sem aceitar nada, e recebe as pendencias do cliente e
    o marcador que encerra a etapa entre as IAs. escalate segue no texto fixo.
    """
    analysis = ai_analysis or {}
    pendencias = titulos_ao_cliente(analysis.get("requests"))

    if action == "accept":  # o ZelinhU nunca aceita em nome do cliente
        action = "send_to_client"
    if action == "escalate":
        return _generate_zelinhu_message_fallback(
            action, client_name, round_number, estimated_value, company_response,
            pendencias_do_cliente=pendencias,
        )

    move = Move.from_dict(analysis.get("move")) or Move(
        action=action, posture="manter" if action == "counter_proposal" else "levar_ao_cliente_limite",
        reason="sem jogada registrada",
    )
    if move.action != action:
        # A acao mudou depois da politica (pedido ao cliente vence o fechamento):
        # o texto tem de ser de quem segue negociando, nao de quem encerra.
        move = Move(action=action, posture="manter" if action == "counter_proposal" else "levar_ao_cliente_limite",
                    reason=f"acao ajustada para {action}", ask=move.ask, previous_ask=move.ask,
                    offer=move.offer, floor=move.floor)
    try:
        text = await _get_negotiator().write(session, move, analysis)
    except Exception as e:  # noqa: BLE001
        print(f"[SENDER-WEBHOOK] Erro na redacao ({type(e).__name__}: {e}); usando texto seguro")
        # O texto seguro usa o pedido DESTA jogada: depois de um ajuste, voltar
        # ao valor cheio seria desdizer a rodada anterior.
        valor_do_pedido = move.ask if move.ask is not None else estimated_value
        return _generate_zelinhu_message_fallback(
            action, client_name, round_number, valor_do_pedido, company_response,
            pendencias_do_cliente=pendencias,
        )

    if action != "send_to_client":
        return text, False

    bloco_pendencias = render_pendencias_para_cliente(pendencias)
    if bloco_pendencias:
        text += f"\n\n{bloco_pendencias}"
    text += "\n\n[PROPOSTA PARA APROVACAO DO CLIENTE]"
    return text, False


def _parse_brl_number(raw: str) -> Optional[float]:
    """Converte um numero monetario brasileiro explicito para float."""
    value = (raw or "").strip().replace(" ", "")
    if not value:
        return None

    if "." in value and "," in value:
        value = value.replace(".", "").replace(",", ".")
    elif "," in value:
        value = value.replace(".", "").replace(",", ".")
    elif "." in value:
        left, right = value.rsplit(".", 1)
        # Em pt-BR, 9.500 normalmente e separador de milhar.
        if right.isdigit() and len(right) == 3 and left.replace(".", "").isdigit():
            value = value.replace(".", "")

    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _extract_unambiguous_offer_value(text: str) -> Optional[float]:
    """
    Extrai valor monetario somente quando a mensagem contem um unico valor
    explicito (podendo repeti-lo). Multiplos valores distintos sao ambiguos e
    ficam para ``agreement_details`` estruturado da analise da IA.
    """
    if not text:
        return None

    patterns = (
        r"r\$\s*([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{1,2})?|[0-9]+(?:[,.][0-9]{1,2})?)",
        r"\b([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{1,2})?|[0-9]+(?:[,.][0-9]{1,2})?)\s*reais\b",
    )

    values = []
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            parsed = _parse_brl_number(match.group(1))
            if parsed is not None:
                values.append(parsed)

    unique = sorted(set(values))
    return unique[0] if len(unique) == 1 else None


def _get_agreement_details(ai_analysis: Optional[dict], company_response: str, estimated_value: float) -> Optional[Dict[str, Any]]:
    """
    Retorna agreementDetails sem confundir valor da causa com valor da oferta.

    ``estimated_value`` e mantido na assinatura por compatibilidade com os
    chamadores, mas NAO e usado como fallback financeiro. Se a analise
    estruturada nao trouxer valor, so registramos um valor quando a propria
    mensagem da empresa tiver um montante explicito e nao ambiguo.
    """
    explicit_value = _extract_unambiguous_offer_value(company_response)

    if ai_analysis:
        details = ai_analysis.get("agreement_details")
        if isinstance(details, dict) and details:
            result = dict(details)
            if result.get("value") in (None, "") and explicit_value is not None:
                result["value"] = explicit_value
            result.setdefault("terms", (company_response or "").strip())
            result.setdefault("deadline", "")
            return result

    if not company_response:
        return None

    result: Dict[str, Any] = {
        "value": explicit_value,
        "terms": company_response.strip(),
        "deadline": "",
    }
    if explicit_value is None:
        print(
            "[SENDER-WEBHOOK] agreementDetails sem valor explicito: "
            "nao usando estimatedValue do caso como se fosse oferta"
        )
    return result


async def _get_session_context(session_id: str) -> Optional[Dict[str, Any]]:
    """
    Busca o contexto da sessao no Zellu Backend.

    Retorna informacoes do ticket, cliente e empresa.
    """
    try:
        settings = get_settings()
        zellu_api_client = ZelluAPIClient(
            base_url=settings.ZELLU_BACKEND_URL,
            api_key=settings.API_KEY_ZELLU_IA
        )

        # Buscar payload completo da sessao
        session_payload = await zellu_api_client.build_payload_from_session(
            session_id=session_id
        )

        if session_payload:
            context = session_payload.get("context", {})
            return {
                "ticket": context.get("ticket", {}),
                "client": context.get("client", {}),
                "company": context.get("company", {}),
                # Analise juridica: fonte preferencial dos direitos do cliente.
                # Pode vir na raiz do payload ou dentro do context, conforme o
                # endpoint que o backend Zellu usou para montar a sessao.
                "analysis": session_payload.get("analysis") or context.get("analysis") or {},
                "estimated_value": context.get("ticket", {}).get("estimatedValue", 0)
            }

        return None
    except Exception as e:
        print(f"[SENDER-WEBHOOK] Erro ao buscar contexto: {e}")
        return None


def absorb_session_context(
    session_id: str,
    context: Optional[Dict[str, Any]],
    origem: str = "payload da empresa",
) -> bool:
    """
    Aproveita o contexto que chegou pelo lado da IA da Empresa.

    ASSIMETRIA DE INFORMACAO (log de 25/08/2026)
    --------------------------------------------
    `_get_session_context` deu 404 nas tres tentativas e o ZelinhU negociou as
    cinco rodadas sem valor da causa, sem nome do cliente e sem direitos - foi o
    que forcou o fallback "sem direitos na analise... apurar os fatos antes de
    fixar valores".

    So que o MESMO contexto chega completo, um minuto depois, no payload que o
    backend manda para /api/company/base-dados: ticket, cliente, empresa e o
    estimatedValue. A IA da Empresa negociava com o valor da causa na mao e o
    ZelinhU no escuro, contra o adversario dele.

    As duas pontas rodam no mesmo processo. Entao o que o backend nao entrega
    pela porta do Sender, a gente aproveita pela porta da empresa.

    NAO sobrescreve contexto existente: se a busca do backend funcionar, ela
    continua sendo a fonte. Isto e rede de seguranca, nao caminho principal.

    A partir da spec de 01/09 a RETOMADA tambem traz os blocos do contexto, e
    passa por aqui com `origem` propria: a regra "nao sobrescreve o que ja
    funciona" e a mesma, e so o log muda de nome. Sem o parametro, um contexto
    preenchido pela retomada apareceria no log como se tivesse vindo do lado da
    empresa - e quem for atras de um incidente iria olhar o caminho errado.

    Returns:
        True se o contexto foi preenchido agora.
    """
    session = _sender_sessions.get(session_id)
    if not session or not context:
        return False

    ticket = context.get("ticket") or {}
    if not ticket:
        return False

    atual = session.get("context") or {}
    if atual.get("ticket"):
        return False

    session["context"] = {
        "ticket": ticket,
        "client": context.get("client") or {},
        "company": context.get("company") or {},
        "analysis": context.get("analysis") or {},
        "estimated_value": ticket.get("estimatedValue", 0),
    }
    print(
        f"[SENDER-WEBHOOK] Contexto da sessao {session_id} preenchido pelo {origem} "
        f"(valor da causa: {ticket.get('estimatedValue', 0)}). O ZelinhU "
        f"deixa de negociar no escuro a partir da proxima rodada."
    )
    return True


def _contexto_da_retomada(
    ticket: Optional[Dict[str, Any]],
    client: Optional[Dict[str, Any]],
    company: Optional[Dict[str, Any]],
    selected_rights: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Monta o contexto da sessao com os blocos que vieram no proprio resume.

    CONTEXTO EM TODA RETOMADA (spec 20260901, secao 4.2)
    ----------------------------------------------------
    Ate 01/09 a retomada trazia so clientFeedback, rejectionCount e
    previousAgreement. Quando a sessao ja nao estava em memoria - restart do
    processo, ou uma recusa que chega dias depois -, a unica saida era
    `_get_session_context`, o endpoint que deu 404 nas tres tentativas de 25/08.
    O ZelinhU negociou cinco rodadas sem valor da causa e sem nome do cliente.

    O backend passou a mandar ticket, client, company e selectedRights em toda
    retomada exatamente para essa dependencia acabar.

    Devolve None quando nao ha bloco `ticket`: e o sinal de que este payload nao
    carrega contexto (backend anterior ao deploy da spec, que e o estado de hoje),
    e quem chama volta a buscar no endpoint de sempre.

    O formato e o mesmo de `absorb_session_context` de proposito: a sessao tem UM
    formato de contexto, nao um por porta de entrada.
    """
    ticket = dict(ticket or {})
    if not ticket:
        return None

    # selectedRights viaja solto no payload da retomada, mas dentro do ticket no
    # start_negotiation (TicketData.selectedRights). Guardamos no endereco do
    # start para o mesmo dado nao ter dois lugares onde morar.
    if selected_rights and not ticket.get("selectedRights"):
        ticket["selectedRights"] = list(selected_rights)

    valor = ticket.get("estimatedValue")
    if valor in (None, ""):
        valor = ticket.get("estimated_value", 0)

    return {
        "ticket": ticket,
        "client": client or {},
        "company": company or {},
        # A retomada NAO traz analise juridica - a secao 4.2 lista so os quatro
        # blocos. Sem ela, resolve_client_rights cai na deteccao pela descricao
        # do ticket, que so e possivel porque o ticket agora chega. E menos que o
        # start_negotiation e muito mais que o escuro de antes.
        "analysis": {},
        "estimated_value": valor or 0,
    }


async def _send_zelinhu_message_to_backend(
    session_id: str,
    negotiation_id: Optional[str],
    message_body: str,
    round_number: int,
    negotiation_complete: bool = False,
    action: Optional[str] = None,
    session: Optional[dict] = None,
    dispatch_key: str = "",
    request_payload: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Envia mensagem do ZelinhU de volta para o Zellu Backend.

    Este e o POST REVERSO que continua o loop de negociacao.
    O Backend vai:
    1. Salvar a mensagem do ZelinhU no banco
    2. Enviar para a IA da Empresa
    3. Receber resposta da IA
    4. Disparar novo callback para nos
    5. Loop continua!

    CAMPOS NULOS (diagnostico A2, 22/08/2026)
    -----------------------------------------
    `negotiationId` e opcional e ia no JSON como `null` quando ausente. O backend
    responde 400 "Dados invalidos" e o loop para. O mesmo POST feito por
    ZelluAPIClient.send_zelinhu_message ja omitia o campo corretamente - eram duas
    implementacoes do mesmo request, uma certa e outra nao.

    IDEMPOTENCIA POR RODADA (diagnostico A3)
    ----------------------------------------
    Quando o backend estoura o timeout e reenvia o webhook, o Sender gerava e
    postava uma segunda contraproposta para a MESMA rodada. Com `session`, o
    envio vira no-op se a mesma mensagem ja foi despachada naquela rodada.

    Args:
        session_id: ID da sessao
        negotiation_id: ID da negociacao (pode ser None - o campo e omitido)
        message_body: Mensagem do ZelinhU para enviar
        round_number: Numero da rodada atual
        session: Sessao do Sender, usada para a idempotencia por rodada

    Returns:
        Resposta do Backend, ou None se erro / envio duplicado
    """
    try:
        settings = get_settings()
        backend_url = f"{settings.ZELLU_BACKEND_URL}/api/webhooks/sender/message"

        # Campos obrigatorios: sem eles o POST e lixo garantido
        if not session_id or not (message_body or "").strip():
            print(
                f"[SENDER-WEBHOOK] ERRO: POST abortado por campo obrigatorio ausente "
                f"(sessionId={'ok' if session_id else 'VAZIO'}, "
                f"messageBody={'ok' if (message_body or '').strip() else 'VAZIO'})"
            )
            return None

        if _already_dispatched(session, dispatch_key):
            print(
                f"[SENDER-WEBHOOK] Envio ignorado - ja respondemos a esta mensagem "
                f"da outra parte (round {round_number}, chave {dispatch_key}). "
                f"Nao e falha de transporte."
            )
            return DISPATCH_DUPLICATE

        # A memoria nao sabe, mas o registro persistido pode saber: e o unico
        # jeito de pegar o redespacho que atravessa um restart do processo
        # (sugestao 8 da spec de 01/09). Falha em ABERTO - qualquer duvida
        # despacha, como antes desta guarda existir.
        if await _e_redespacho_apos_restart(session, session_id, dispatch_key):
            print(
                f"[SENDER-WEBHOOK] Envio ignorado - este turno ja foi despachado "
                f"antes de um restart (round {round_number}, chave {dispatch_key}). "
                f"A memoria do processo se perdeu; o registro persistido nao."
            )
            _mark_dispatched(session, dispatch_key)
            return DISPATCH_DUPLICATE

        # Nenhum campo com valor None: o backend rejeita o payload inteiro (A2)
        payload: Dict[str, Any] = {
            "sessionId": session_id,
            "messageBody": message_body,
            "round": round_number,
            "negotiationComplete": negotiation_complete,
        }
        if negotiation_id:
            payload["negotiationId"] = negotiation_id
        if action:
            payload["action"] = action
        # So viaja em action=request. Mandar o objeto vazio numa rodada comum
        # colocaria um campo que o backend nao espera ali (spec, secao 2.2).
        if request_payload:
            payload["request"] = request_payload

        print(f"[SENDER-WEBHOOK] ========== POST REVERSO PARA BACKEND ==========")
        print(f"[SENDER-WEBHOOK] URL: {backend_url}")
        print(f"[SENDER-WEBHOOK] sessionId: {session_id}")
        print(f"[SENDER-WEBHOOK] round: {round_number}")
        print(f"[SENDER-WEBHOOK] negotiationComplete: {negotiation_complete}")
        if action:
            print(f"[SENDER-WEBHOOK] action: {action}")
        print(f"[SENDER-WEBHOOK] messageBody: {message_body[:100]}...")

        # 120s por decisao do backend (plano de 25/08/2026, §2.6): quem corta
        # primeiro no caminho sincrono e o Cloudflare do app.zellu.tec.br, aos
        # ~100s, entao este teto nunca chega a opinar. O que ele preserva e o
        # rollback: com a flag desligada o backend segura ~103s antes do 200, e
        # um teto de 10s faria TODA rodada estourar. Foi por isso que o §3.2
        # item 4 do contrato (timeout de 10s) foi retirado.
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                backend_url,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": settings.API_KEY_ZELLU_IA
                }
            )

            if response.status_code == 202:
                # Aceito para processamento assincrono. O despacho CHEGOU, entao
                # conta como despachado - antes isto caia no `else` e a guarda de
                # duplicata nao registrava, deixando um reenvio identico passar.
                #
                # O corpo do ack e opcional e pode vir vazio: o parse fica num
                # try/except PROPRIO, porque uma excecao aqui subiria para o
                # except de fora e viraria `None` - de volta ao bug que este
                # bloco conserta. O que fazemos com ele nao pode custar o
                # despacho.
                #
                # E ha uma coisa no corpo que passamos a usar: o `requestId`
                # (spec 01/09, §2.4). Sem ele, o veredito sobre o documento so
                # consegue apontar o pedido pelo TITULO, que nao e chave - dois
                # pedidos podem se chamar "Comprovante". `requestId: null` e
                # duplicata suprimida, e nao um erro: nada a guardar.
                if request_payload is not None:
                    try:
                        corpo = response.json() or {}
                        request_id = corpo.get("requestId")
                        if request_id:
                            # Escrito DENTRO do proprio pedido, que o chamador ja
                            # guarda na sessao - sem encanamento novo.
                            request_payload["requestId"] = request_id
                            print(f"[SENDER-WEBHOOK] Solicitacao criada: requestId={request_id}")
                        else:
                            print("[SENDER-WEBHOOK] 202 sem requestId - duplicata suprimida do lado do backend")
                    except Exception as e:  # noqa: BLE001 - corpo vazio ou nao-JSON
                        print(f"[SENDER-WEBHOOK] 202 sem corpo aproveitavel ({type(e).__name__}) - segue sem requestId")

                _mark_dispatched(session, dispatch_key)
                _confirmar_reserva(session, dispatch_key)
                print(
                    f"[SENDER-WEBHOOK] ✅ Despacho aceito (202) - o backend processa o "
                    f"turno da empresa por fora e devolve num callback novo. Nao ha "
                    f"corpo para consumir aqui."
                )
                return DISPATCH_ACCEPTED

            if response.status_code == 200:
                result = response.json()
                # So marca apos uma resposta de sucesso: se falhou, um reenvio e legitimo
                _mark_dispatched(session, dispatch_key)
                _confirmar_reserva(session, dispatch_key)
                print(f"[SENDER-WEBHOOK] ✅ Mensagem ZelinhU enviada para Backend com sucesso!")
                print(f"[SENDER-WEBHOOK] Backend response: {result}")
                return result

            if response.status_code == 400 and action == REQUEST_ACTION:
                # As quatro recusas de solicitacao (spec, secao 2.5). Em 400 a
                # mensagem nao e salva do lado deles: nada ficou pela metade.
                codigo = ""
                try:
                    codigo = (response.json() or {}).get("code") or ""
                except Exception:
                    pass

                if codigo == REQUEST_LIMIT_REACHED:
                    # 10 pendencias abertas na negociacao. NAO marcamos como
                    # despachado: a vaga abre quando alguem responder, e a spec
                    # garante que reenviar o mesmo corpo funciona.
                    print(
                        f"[SENDER-WEBHOOK] Solicitacao recusada por lotacao "
                        f"({REQUEST_LIMIT_REACHED}) - o pedido segue reenviavel"
                    )
                    return DISPATCH_REQUEST_LIMIT

                # Os outros tres sao defeito NOSSO: pedido malformado, titulo
                # grande demais ou pedido junto de um turno terminal. O schema
                # do sender_agent existe para que nao aconteca - se aconteceu, o
                # log tem que gritar. A negociacao segue sem o pedido: travar a
                # conversa por causa de um pedido acessorio seria pior.
                print(
                    f"[SENDER-WEBHOOK] ❌ Solicitacao RECUSADA pelo backend "
                    f"(400 {codigo or 'sem codigo'}) - DEFEITO NOSSO, o pedido nao "
                    f"virou pendencia. Resposta: {response.text[:300]}"
                )
                return None

            else:
                print(f"[SENDER-WEBHOOK] ❌ Erro ao enviar para Backend: {response.status_code}")
                print(f"[SENDER-WEBHOOK] Response: {response.text}")
                print(f"[SENDER-WEBHOOK] Payload enviado (campos): {sorted(payload.keys())}")
                return None

    except httpx.TimeoutException:
        print(f"[SENDER-WEBHOOK] ❌ Timeout ao enviar para Backend")
        return None
    except Exception as e:
        print(f"[SENDER-WEBHOOK] ❌ Erro ao enviar para Backend: {e}")
        traceback.print_exc()
        return None


def _pedido_ao_cliente_vence_o_fechamento(
    action: str,
    ai_analysis: Optional[dict],
    round_number: int,
    max_rounds: int = MAX_ROUNDS,
) -> bool:
    """Se o turno deve pedir ao cliente em vez de fechar (QA de 10/09, achado 6).

    Tudo isto precisa valer ao mesmo tempo:

    - a acao seria fechar (send_to_client);
    - a analise traz pedido NOVO ao cliente - o ja feito foi filtrado antes;
    - NAO houve acordo. Com acordo, o acordo vence, como sempre venceu: o pedido
      vai listado no texto da proposta;
    - a flag esta ligada. Sem ela o pedido nao sairia, e adiar faria a
      negociacao passar do fechamento a toa;
    - a rodada nao passou do teto. E o que impede o adiamento de virar laco:
      depois dele o fechamento sai de qualquer jeito.
    """
    if action != "send_to_client" or not ai_analysis:
        return False
    if ai_analysis.get("agreement_reached"):
        return False
    if not get_settings().NEGOTIATION_REQUEST_ENABLED:
        return False
    if round_number > max_rounds:
        return False
    return bool(titulos_ao_cliente(ai_analysis.get("requests")))


def _requests_a_emitir(
    ai_analysis: Optional[dict],
    action: str,
    negotiation_complete: bool,
) -> List[Any]:
    """Pedidos que devem virar POST neste turno. Lista vazia = turno normal.

    Tres filtros, nesta ordem:

    1. A FLAG. Desligada, a analise pode pedir o que quiser que nao sai nada. E
       o freio que a spec (secao 8) pede enquanto a rota do backend nao esta
       deployada: la o schema usa .passthrough(), entao um action=request que
       chegue cedo demais NAO da erro - vira rodada comum e a IA da empresa
       responde ao pedido, sem nada quebrar em lugar nenhum.

    2. EXCLUSIVIDADE. Pedido junto de send_to_client, de negotiationComplete ou
       de acordo e 400 REQUEST_AND_TERMINAL_CONFLICT (secao 2.5). Quando os dois
       aparecem no mesmo turno, o ACORDO vence: ele fecha o caso, e o pedido
       viraria uma pergunta sobre uma negociacao que ja acabou.

    3. TETO DA RAJADA. Corta em MAX_REQUESTS_POR_RAJADA para um turno sozinho
       nao consumir a cota de 10 pendentes do backend.
    """
    if not ai_analysis:
        return []

    pedidos = ai_analysis.get("requests") or []
    if not pedidos:
        return []

    if not get_settings().NEGOTIATION_REQUEST_ENABLED:
        print(
            f"[SENDER-WEBHOOK] {len(pedidos)} solicitacao(oes) descartada(s): "
            f"NEGOTIATION_REQUEST_ENABLED esta desligada"
        )
        return []

    if _e_despacho_terminal(action) or action == "send_to_client" or negotiation_complete:
        print(
            f"[SENDER-WEBHOOK] {len(pedidos)} solicitacao(oes) descartada(s): "
            f"o turno e terminal (action={action}, complete={negotiation_complete}). "
            f"Pedido e acordo sao mutuamente exclusivos e o acordo vence"
        )
        return []

    if len(pedidos) > MAX_REQUESTS_POR_RAJADA:
        print(
            f"[SENDER-WEBHOOK] Rajada de {len(pedidos)} cortada em "
            f"{MAX_REQUESTS_POR_RAJADA} solicitacoes"
        )
        return list(pedidos[:MAX_REQUESTS_POR_RAJADA])

    return list(pedidos)


def _registrar_company_request_rejected(
    session: dict,
    pedido: Any,
    motivo: str,
) -> None:
    """Guarda a recusa de pedido feito pela empresa para auditoria/debug."""

    rejeitados = session.setdefault("companyRequestsRejected", [])
    titulo = ""
    if isinstance(pedido, dict):
        titulo = str(pedido.get("title") or "").strip()
    rejeitados.append({
        "title": titulo[:200],
        "reason": motivo,
        "createdAt": datetime.now().isoformat(),
    })
    del rejeitados[:-20]
    print(
        f"[SOLICITACOES] pedido da empresa nao repassado: {motivo} "
        f"(title={titulo[:80] or '-'})"
    )


def _company_request_a_emitir(
    request: WebhookSenderMessageRequest,
    session: dict,
) -> Optional[Dict[str, Any]]:
    """Valida o `request` opcional vindo em company_response.

    Caminho A do documento de 29/09: a empresa pede algo dentro de
    company_response, mas o ZelinhU continua sendo o mediador. Por isso o pedido
    entra na mesma esteira de action=request, com os mesmos limites, dedupe,
    pausa e registro de pendencia.

    O primeiro corte e conservador: pedido vindo da empresa so pode mirar o
    cliente. Pedidos para a propria empresa devem continuar sendo gerados pelo
    ZelinhU/analise, nao aceitos cegamente de quem esta do outro lado.
    """

    pedido = request.request
    if not pedido:
        return None

    # Em verify_request, `request` tem outro significado: e o pedido que esta
    # sendo conferido. Resume tambem nao e company_response comum.
    action = (request.action or "").strip()
    if action in {VERIFY_REQUEST_ACTION, RESUME_ACTION, COMPANY_AI_FAILED_ACTION}:
        return None

    if not isinstance(pedido, dict):
        _registrar_company_request_rejected(session, pedido, "request nao e objeto")
        return None

    normalizado = {
        "audience": pedido.get("audience"),
        "type": pedido.get("type"),
        "response_type": pedido.get("response_type") or pedido.get("responseType"),
        "title": pedido.get("title"),
        "description": pedido.get("description") or "",
    }

    if normalizado.get("audience") != "client":
        _registrar_company_request_rejected(
            session,
            pedido,
            "empresa so pode pedir encaminhamento ao cliente neste fluxo",
        )
        return None

    try:
        validado = NegotiationRequest.model_validate(normalizado).model_dump()
    except ValidationError as e:
        _registrar_company_request_rejected(
            session,
            pedido,
            f"request invalido: {e.errors()[0].get('type') if e.errors() else 'validation_error'}",
        )
        return None

    novos, repetidos = filtrar_ja_solicitados(
        [validado], session.get("solicitacoesFeitas")
    )
    if not novos:
        _registrar_company_request_rejected(
            session,
            pedido,
            "pedido ja solicitado anteriormente",
        )
        return None

    print(
        f"[SOLICITACOES] pedido estruturado da empresa aceito para mediacao: "
        f"{validado.get('title')}"
    )
    if repetidos:
        print(f"[SOLICITACOES] repetidos ignorados={len(repetidos)}")

    return novos[0]


def _request_para_payload(pedido: Any) -> Dict[str, Any]:
    """Um NegotiationRequest da analise no formato do contrato (secao 2.3).

    O schema usa response_type (snake_case, como o resto do projeto) e o contrato
    pede responseType. A traducao vive aqui, num lugar so.
    """
    dados = pedido if isinstance(pedido, dict) else pedido.model_dump()

    payload = {
        "audience": dados.get("audience"),
        "type": dados.get("type"),
        "responseType": dados.get("response_type") or dados.get("responseType"),
        "title": (dados.get("title") or "").strip(),
    }

    descricao = (dados.get("description") or "").strip()
    if descricao:
        payload["description"] = descricao

    return payload


async def _dispatch_requests(
    session_id: str,
    negotiation_id: Optional[str],
    message_body: str,
    round_number: int,
    pedidos: List[Any],
    session: dict,
) -> List[Dict[str, Any]]:
    """Emite a rajada e devolve os pedidos que o backend ACEITOU.

    Devolvia so a contagem. Os pedidos aceitos passam a ser guardados na sessao
    para que o documento que chegar na retomada possa ser conferido contra o que
    foi pedido - e a contagem continua disponivel no `len()`.

    Um POST por pedido, TODOS na mesma rodada: do lado do backend a solicitacao
    nao avanca a negociacao, e mandar rodadas diferentes faria a rajada parecer
    varios turnos (spec, secao 3).

    Sem dispatch_key de proposito. A guarda de duplicata identifica o despacho
    pela mensagem da empresa que originou o turno - a mesma para os seis pedidos
    -, entao o primeiro POST marcaria a chave e os outros cinco seriam engolidos
    como reenvio. Quem protege contra pedido repetido aqui e a chave do backend,
    que inclui os campos do proprio `request`.

    A MENSAGEM SAI UMA VEZ SO (auditoria QA de 10/09, achado 1)
    ------------------------------------------------------------
    Cada `messageBody` vira uma mensagem no chat (spec, secao 2.2). Ate 10/09 os
    cinco pedidos da rajada levavam o MESMO texto inteiro, e o QA viu a proposta
    publicada cinco vezes seguidas. Agora o texto inteiro vai no primeiro pedido
    ACEITO; os seguintes levam so uma linha com o titulo. Se o primeiro for
    recusado com 400 - que nao salva mensagem nenhuma -, o texto passa para o
    proximo, senao a proposta nao apareceria em lugar nenhum.
    """
    aceitas: List[Dict[str, Any]] = []
    mensagem_publicada = False

    for indice, pedido in enumerate(pedidos, start=1):
        request_payload = _request_para_payload(pedido)
        corpo = (
            f"Solicitacao: {request_payload.get('title')}"
            if mensagem_publicada else message_body
        )

        print(
            f"[SENDER-WEBHOOK] Solicitacao {indice}/{len(pedidos)}: "
            f"{request_payload.get('type')} para {request_payload.get('audience')} "
            f"({request_payload.get('responseType')}) - {request_payload.get('title')}"
        )

        resultado = await _send_zelinhu_message_to_backend(
            session_id=session_id,
            negotiation_id=negotiation_id,
            message_body=corpo,
            round_number=round_number,
            negotiation_complete=False,
            action=REQUEST_ACTION,
            session=session,
            request_payload=request_payload,
        )

        if resultado is DISPATCH_REQUEST_LIMIT:
            # Lotou. Os pedidos seguintes desta rajada tambem lotariam - paramos
            # aqui em vez de colecionar recusas identicas.
            print(
                f"[SENDER-WEBHOOK] Rajada interrompida na solicitacao {indice}: "
                f"a negociacao ja tem o maximo de pendencias abertas"
            )
            break

        if resultado is None:
            # 400 de defeito nosso ou falha de transporte: ja gritou no log de
            # dentro. Seguimos com os proximos - um pedido malformado nao e
            # motivo para os outros nao existirem.
            continue

        aceitas.append(request_payload)
        mensagem_publicada = True

    # Rastreabilidade pedida pelo QA de 10/09 (§12): do pedido detectado ate a
    # baixa, cada etapa deixa uma linha com o mesmo prefixo.
    print(
        f"[SOLICITACOES] rodada={round_number} emitidas={len(pedidos)} aceitas={len(aceitas)} "
        f"requestIds={[p.get('requestId') for p in aceitas]}"
    )
    return aceitas


def _auditar_pedido_da_empresa(
    session: dict,
    pedido: Dict[str, Any],
    emitidos: List[Any],
    aceitos: List[Dict[str, Any]],
) -> None:
    """Registra em companyRequestsRejected o pedido da empresa que nao saiu."""
    titulo = (pedido.get("title") or "").strip().casefold()

    def _mesmo(p: Any) -> bool:
        return isinstance(p, dict) and (p.get("title") or "").strip().casefold() == titulo

    if any(_mesmo(p) for p in aceitos):
        return
    if not any(_mesmo(p) for p in emitidos):
        motivo = (
            "nao repassado: o turno encerrou a negociacao automatica ou as "
            "solicitacoes estao desligadas (NEGOTIATION_REQUEST_ENABLED)"
        )
    else:
        motivo = "nao repassado: o backend nao aceitou a solicitacao"
    _registrar_company_request_rejected(session, pedido, motivo)


def _guardar_fala_em_pausa(
    session: dict,
    request: WebhookSenderMessageRequest,
    message_id: str,
    corpo: str,
    motivo_da_pausa: str,
) -> None:
    """
    Fala da empresa que chegou com a negociacao pausada.

    Nao vira rodada - a pausa continua valendo -, mas nao pode sumir. Ate 30/09
    ela nao entrava no historico: o ZelinhU, na retomada, nao sabia o que a
    empresa tinha dito nesse meio tempo. Os anexos ja foram lidos antes daqui
    (_store_company_response_attachments). O pedido estruturado, se veio, fica
    registrado como nao repassado, com o motivo.
    """
    session.setdefault("messages", []).append({
        "id": message_id,
        "origin": "company_ai",
        "body": corpo,
        "duringPause": True,
        "createdAt": datetime.now().isoformat(),
    })
    if request.request:
        _registrar_company_request_rejected(
            session, request.request, f"nao repassado: {motivo_da_pausa}"
        )


def _record_superseded_message(session_id: str, message_body: str) -> None:
    """
    Guarda no historico a mensagem que nao virou turno.

    A mensagem nao ganha resposta propria, mas nao pode sumir: ela entra na
    sessao e o proximo turno a le junto com o resto. Sem isso, suprimir a
    duplicata viraria perda de informacao da negociacao.
    """
    session = _sender_sessions.get(session_id)
    if not session or not message_body:
        return

    mensagens = session.setdefault("messages", [])

    # O turno que estava correndo pode ter recebido esta mesma mensagem pela
    # resposta HTTP do despacho e ja te-la registrado.
    if any((m.get("body") or "") == message_body for m in mensagens[-MAX_HISTORY_MESSAGES:]):
        return

    mensagens.append({
        "id": f"msg_company_{uuid.uuid4().hex[:12]}",
        "origin": "company_ai",
        "body": message_body,
        "supersededTurn": True,
        "createdAt": datetime.now().isoformat(),
    })


def _superseded_turn_response(request: WebhookSenderMessageRequest) -> WebhookSenderMessageResponse:
    """Resposta de uma entrega que chegou durante um turno ja respondido."""
    print(
        f"[SENDER-WEBHOOK] Turno ja respondido nesta sessao enquanto esta mensagem "
        f"esperava - registrada no historico, sem gerar segunda mensagem do "
        f"ZelinhU (trava de turno)"
    )
    _record_superseded_message(request.sessionId, request.messageBody)

    session = _sender_sessions.get(request.sessionId) or {}
    return WebhookSenderMessageResponse(
        success=True,
        companyMessageId=f"msg_company_{uuid.uuid4().hex[:12]}",
        zelinhuMessageId="",
        response=WebhookSenderMessageResponseData(
            messageBody="",
            negotiationComplete=False,
            action="superseded_turn",
        ),
        round=_requested_round(request.round, session.get("currentRound", 1)),
    )


def _e_despacho_terminal(action: str) -> bool:
    """
    Se esta mensagem fecha a negociacao automatica e vai ao cliente.

    O `send_to_client` e o unico desfecho que a negociacao alcanca na pratica - o
    action_map so produz send_to_client e counter_proposal. Ate 26/08/2026 ele
    saia sem discriminador nenhum, e o backend nao tinha como distinguir a
    mensagem final de uma que avanca rodada.

    O interruptor existe porque o risco de ligar cedo demais e caro: sem o
    backend lendo o campo, o POST terminal faz a IA da empresa responder a uma
    proposta ja encerrada, em TODA negociacao.
    """
    return action == "send_to_client" and get_settings().NEGOTIATION_TERMINAL_DISPATCH_ENABLED


def _company_reply_from(backend_response: Optional[Dict[str, Any]]) -> str:
    """
    Resposta da IA da Empresa que veio dentro do POST que acabamos de fazer.

    O backend responde o nosso despacho com o texto da empresa - e assim que o
    turno normal continua a negociacao (ver o loop mais abaixo). Devolve string
    vazia quando nao ha o que continuar: POST falhou, corpo sem mensagem, ou o
    backend ja marcou a negociacao como concluida.
    """
    if not isinstance(backend_response, dict):
        return ""

    # `negotiationComplete` na raiz e a forma do POST terminal (plano de
    # 25/08/2026, §2.2); dentro de `response` e a do POST comum. Qualquer uma
    # significa que nao ha negociacao para continuar.
    if backend_response.get("negotiationComplete"):
        return ""

    data = backend_response.get("response") or {}
    if data.get("negotiationComplete"):
        return ""

    return (data.get("messageBody") or "").strip()


def _resume_response(
    request: WebhookSenderMessageRequest, resultado: Dict[str, Any]
) -> WebhookSenderMessageResponse:
    """
    Resposta de uma retomada que parou nela mesma.

    negotiationComplete=False e action=resume_negotiation de proposito: no
    backend, `action=send_to_client` ou `status=agreement` na resposta HTTP
    disparam markSessionAsAgreement. Uma retomada nao pode reabrir esse caminho -
    ela esta justamente desfazendo a espera pelo cliente.

    A excecao e a retomada que ENCERRA (limite de recusas do cliente): ai sai
    negotiationComplete=True com status=no_agreement - terminal, mas nunca
    agreement.
    """
    encerrou = resultado.get("status") == "no_agreement"
    return WebhookSenderMessageResponse(
        success=resultado["success"],
        companyMessageId=f"msg_company_{uuid.uuid4().hex[:12]}",
        zelinhuMessageId=resultado["zelinhuMessageId"],
        response=WebhookSenderMessageResponseData(
            messageBody=resultado["messageBody"],
            negotiationComplete=encerrou,
            action=RESUME_ACTION,
        ),
        round=resultado["round"],
        negotiationComplete=True if encerrou else None,
        status="no_agreement" if encerrou else None,
    )


def _handle_company_ai_failed(request: WebhookSenderMessageRequest) -> WebhookSenderMessageResponse:
    """
    Aviso de que o turno da IA da empresa falhou (plano de 25/08/2026, §2.3).

    Nao gera mensagem e nao move a negociacao: a conduta - repetir, escalar ou
    avisar o cliente - ainda e decisao aberta. O que este handler garante hoje e
    que o aviso NAO seja confundido com uma fala da empresa, que e o que
    aconteceria se ele caisse no caminho normal.
    """
    print(
        f"[SENDER-WEBHOOK] 🔴 TURNO DA IA DA EMPRESA FALHOU - sessao {request.sessionId}, "
        f"rodada {request.round}, code={request.code}, retryable={request.retryable}"
    )
    print(f"[SENDER-WEBHOOK] Erro relatado pelo backend: {request.error}")
    print(
        f"[SENDER-WEBHOOK] Registrado sem gerar mensagem - a conduta (repetir / "
        f"escalar / avisar o cliente) ainda nao foi definida"
    )

    session = _sender_sessions.get(request.sessionId)
    if session is not None:
        session["lastCompanyFailure"] = {
            "round": request.round,
            "error": request.error,
            "code": request.code,
            "retryable": request.retryable,
            "source": request.source,
            "at": datetime.now().isoformat(),
        }
    else:
        print(f"[SENDER-WEBHOOK] Sessao {request.sessionId} nao esta em memoria - so registrado no log")

    return WebhookSenderMessageResponse(
        success=True,
        companyMessageId="",
        zelinhuMessageId="",
        response=WebhookSenderMessageResponseData(
            messageBody="",
            negotiationComplete=False,
            action=COMPANY_AI_FAILED_ACTION,
        ),
        round=_requested_round(request.round, (session or {}).get("currentRound", 1)),
    )


async def _handle_verify_request(request: WebhookSenderMessageRequest) -> JSONResponse:
    """
    Confere UM pedido no momento em que a pessoa envia o arquivo (§3.2).

    E a Etapa 2 do contrato: em vez de descobrir na retomada - dias depois, com
    a negociacao ja andando - que o comprovante era do mes errado, o backend
    pergunta antes de dar o pedido por atendido, e quem enviou ve o motivo na
    mesma tela, com o pedido ainda aberto.

    Resposta no corpo do 200: {requestId, verdict, reason}, com o `verdict` no
    vocabulario deles (valid / invalid / undetermined).

    🔴 FALHA NOSSA DEVOLVE 5xx, NUNCA `undetermined`
    ------------------------------------------------
    Pela tabela do §3.1, os dois nao sao a mesma coisa do lado deles:

        undetermined -> "leram e nao decidiram". Eles NAO julgam de novo na
                        retomada - ler outra vez daria no mesmo.
        chamada falha -> `verdict: null`, e nos julgamos na retomada.

    Entao responder `undetermined` quando quem falhou fomos nos desligaria a
    rede de seguranca inteira: ninguem teria conferido o documento e ninguem
    mais iria conferir. `undetermined` fica SO para o arquivo que chegou e nao
    deu para ler - HEIC, PDF digitalizado, foto tremida -, que e limitacao de
    leitura e nao erro de quem enviou.

    Nao toca a sessao: tudo o que precisamos vem no corpo. Pode ser repetida, e
    cada repeticao custa so uma leitura a mais.
    """
    request_id = (request.requestId or "").strip()
    pedido = dict(request.request or {})
    arquivos = _anexos_do_payload(request)

    print(f"\n[SENDER-VERIFY] ========== CONFERENCIA DE PEDIDO ==========")
    print(f"[SENDER-VERIFY] sessionId: {request.sessionId}")
    print(f"[SENDER-VERIFY] requestId: {request_id}")
    print(f"[SENDER-VERIFY] pedido: {pedido.get('title')} ({pedido.get('responseType')})")
    print(f"[SENDER-VERIFY] arquivos: {len(arquivos)}")

    # Defeito do despacho, e nao falha de conferencia: sem id nao ha o que
    # responder, e sem arquivo nao ha o que conferir. 400 e o unico caso em que
    # o problema esta no corpo - e pela regra deles ele tambem vira `null`, com
    # a retomada como rede.
    if not request_id or not arquivos:
        print(f"[SENDER-VERIFY] Corpo incompleto - nada a conferir")
        return JSONResponse(
            status_code=400,
            content={
                "error": "requestId e files sao obrigatorios em action=verify_request",
                "requestId": request_id or None,
            },
        )

    pedido["requestId"] = request_id

    try:
        documentos = await ingerir_anexos(arquivos)
        veredictos = await julgar_documentos(
            [pedido], documentos, llm=_get_negotiator().llm
        )
    except Exception as e:  # noqa: BLE001
        print(f"[SENDER-VERIFY] ERRO ao conferir ({type(e).__name__}: {e})")
        traceback.print_exc()
        return JSONResponse(
            status_code=500,
            content={"error": "falha ao conferir o documento", "requestId": request_id},
        )

    if not veredictos:
        # Sem LLM, ou o modelo falhou. Nao e "conferi e nao sei": e "nao
        # conferi". 5xx para cair na regra do `null`.
        print(f"[SENDER-VERIFY] Nenhum veredito emitido - respondendo 5xx para o julgamento ficar na retomada")
        return JSONResponse(
            status_code=503,
            content={"error": "conferencia indisponivel", "requestId": request_id},
        )

    veredito = veredictos[0]
    resposta = {
        "requestId": request_id,
        "verdict": VEREDITO_PARA_CONTRATO.get(veredito.get("veredito"), "undetermined"),
        "reason": veredito.get("motivo") or "",
    }
    print(f"[SENDER-VERIFY] Veredito: {resposta['verdict']} - {resposta['reason'][:120]}")
    return JSONResponse(status_code=200, content=resposta)


async def _handle_resume_action(request: WebhookSenderMessageRequest) -> WebhookSenderMessageResponse:
    """
    Retomada chegando por /message com action=resume_negotiation.

    O conteudo util esta em `clientFeedback`; o `messageBody` desta action e
    so o embrulho do backend ("Cliente recusou a proposta. Motivo: ...") e
    existe porque o campo e obrigatorio no schema. Se por algum motivo o
    feedback vier vazio, o embrulho serve - ele carrega o mesmo texto.
    """
    client_feedback = (request.clientFeedback or "").strip() or (request.messageBody or "")

    print(
        f"[SENDER-WEBHOOK] action={RESUME_ACTION} - a negociacao volta a andar "
        f"(motivo={request.resumeReason or 'ausente (recusa do cliente)'}, "
        f"rejeicoes={request.rejectionCount})"
    )

    session = _sender_sessions.get(request.sessionId)
    anterior = _resume_already_handled(
        session, request.rejectionCount, client_feedback, request.requests
    )
    if anterior:
        # Reentrega da MESMA recusa: devolve o resultado guardado e nao volta a
        # consumir a resposta da empresa - ela ja foi processada da primeira vez.
        print(f"[SENDER-WEBHOOK] Reentrega da mesma recusa - devolvendo o resultado ja processado")
        return _resume_response(request, anterior)

    resultado = await _resume_negotiation(
        session_id=request.sessionId,
        negotiation_id=request.negotiationId,
        client_feedback=client_feedback,
        rejection_count=request.rejectionCount,
        previous_agreement=request.previousAgreement,
        resume_reason=request.resumeReason,
        ticket=request.ticket,
        client=request.client,
        company=request.company,
        selected_rights=request.selectedRights,
        attachments=_anexos_do_payload(request),
        requests=request.requests,
    )
    _mark_resume_handled(
        _sender_sessions[request.sessionId],
        request.rejectionCount,
        client_feedback,
        resultado,
        request.requests,
    )

    # A NEGOCIACAO PRECISA CONTINUAR DEPOIS DA RETOMADA (25/08/2026)
    # ---------------------------------------------------------------
    # O despacho da retomada volta com a resposta da IA da Empresa dentro dele. A
    # primeira versao deste caminho ignorava esse retorno: a empresa respondia, o
    # ZelinhU nunca lia, e a negociacao congelava um passo depois de onde
    # congelava antes do G8 - sem contraproposta e sem devolver a proposta ao
    # cliente.
    #
    # A resposta da empresa e uma mensagem de empresa como qualquer outra. Em vez
    # de duplicar analise, livro-razao e loop aqui, ela entra pelo turno normal.
    # `_process_sender_message` e o corpo interno: o lock da sessao fica no
    # wrapper e nao e readquirido, entao nao ha deadlock nem recursao alem deste
    # unico nivel (a mensagem encadeada nao tem action).
    resposta_da_empresa = _company_reply_from(resultado["backendResponse"])
    if not resposta_da_empresa:
        return _resume_response(request, resultado)

    print(
        f"[SENDER-WEBHOOK] A empresa ja respondeu a retomada - seguindo com o turno "
        f"normal em vez de parar aqui"
    )
    return await _process_sender_message(
        WebhookSenderMessageRequest(
            sessionId=request.sessionId,
            negotiationId=request.negotiationId,
            messageBody=resposta_da_empresa,
            # Sem round: usa a rodada da sessao, que a retomada acabou de avancar.
            round=None,
        )
    )


# =============================================================================
# ENDPOINT: POST /api/webhooks/sender/message
# =============================================================================

@router.post(
    "/message",
    response_model=WebhookSenderMessageResponse,
    summary="Receber resposta da IA Empresa",
    description="""
    Webhook chamado pelo Zellu Backend para enviar a RESPOSTA DA IA EMPRESA
    para o ZelinhU processar.

    O ZelinhU:
    1. Recebe a resposta da empresa
    2. Analisa a proposta (aceitar, contrapropor, escalar)
    3. Gera a proxima mensagem da negociacao
    4. Retorna para o Backend continuar o loop

    Quando negotiationComplete=true, a negociacao foi concluida.
    """
)
async def webhook_sender_message(request: WebhookSenderMessageRequest):
    """
    Processa resposta da IA Empresa e gera proxima mensagem do ZelinhU.

    Uma sessao por vez: o turno le a sessao, chama o LLM e grava o resultado.
    Sem o lock, duas entregas do backend para a mesma sessao faziam
    leitura-modificacao-escrita concorrente e a segunda sobrescrevia a primeira.

    E uma resposta por turno: se o ZelinhU ja falou enquanto esta entrega
    esperava a vez, ela e registrada no historico em vez de virar uma segunda
    mensagem para a mesma altura da negociacao.
    """
    # Sem sessionId nao ha o que serializar - deixa o corpo devolver o 400.
    if not request.sessionId:
        return await _process_sender_message(request)

    # CONFERENCIA DE PEDIDO (contrato 20260911, §3.2): fica FORA do lock, e de
    # proposito. Ela nao e turno de negociacao e nao toca a sessao - so le o
    # arquivo que veio no corpo e devolve o veredito. Enfileirada atras de um
    # turno de LLM, queimaria o timeout de 115s do backend com uma pessoa parada
    # na tela esperando para saber se o documento dela serve.
    if (request.action or "").strip() == VERIFY_REQUEST_ACTION:
        return await _handle_verify_request(request)

    # Lido ANTES do await: e a foto do estado no momento em que a mensagem
    # chegou. Se mudar ate conseguirmos a vez, outro turno respondeu no meio.
    turnos_na_chegada = _answered_turns[request.sessionId]

    # Evento sobre a negociacao nunca perde a vez. A decisao do cliente reabre a
    # negociacao; o aviso de falha da IA da empresa precisa ser registrado. Nenhum
    # dos dois e "mais uma mensagem" que possa ser descartada por ter chegado
    # durante outro turno - foi assim que a auditoria de 24/08/2026 achou o
    # congelamento. O lock continua valendo: esperam a vez, so nao sao
    # descartados.
    evento_da_negociacao = (request.action or "").strip() in ACTIONS_FORA_DA_TRAVA_DE_TURNO

    # Anexo ou pedido estruturado tambem nunca perde a vez (pausa da empresa,
    # 29/09). No modo manual o humano manda o texto e, logo em seguida, o
    # arquivo: a segunda entrega chegava durante o turno da primeira, era
    # suprimida, e so o texto dela ficava - o anexo nao era lido e o pedido
    # sumia. Mensagem so de texto continua sujeita a trava.
    traz_anexo_ou_pedido = bool(_anexos_do_payload(request)) or bool(request.request)

    async with _session_locks[request.sessionId]:
        if (
            _answered_turns[request.sessionId] != turnos_na_chegada
            and not evento_da_negociacao
            and not traz_anexo_ou_pedido
        ):
            return _superseded_turn_response(request)

        resposta = await _process_sender_message(request)

        # So gasta turno quem produziu mensagem. Modo manual, espera pela
        # decisao do cliente e reenvio ignorado devolvem messageBody vazio.
        if resposta.response.messageBody:
            _answered_turns[request.sessionId] += 1

        return resposta


async def _process_sender_message(request: WebhookSenderMessageRequest):
    """Corpo do turno do Sender, ja dentro do lock da sessao."""
    print(f"\n[SENDER-WEBHOOK] ========== RESPOSTA DA EMPRESA RECEBIDA ==========")
    print(f"[SENDER-WEBHOOK] sessionId: {request.sessionId}")
    print(f"[SENDER-WEBHOOK] negotiationId: {request.negotiationId}")
    print(f"[SENDER-WEBHOOK] round: {request.round}")
    print(f"[SENDER-WEBHOOK] negotiationComplete (da empresa): {request.negotiationComplete}")
    print(f"[SENDER-WEBHOOK] manualModeActive: {request.manualModeActive}")
    if request.firstReplyOrigin:
        print(f"[SENDER-WEBHOOK] firstReplyOrigin: {request.firstReplyOrigin}")
    corpo = request.messageBody or ""
    print(f"[SENDER-WEBHOOK] messageBody (empresa): {corpo[:150]}..." if len(corpo) > 150 else f"[SENDER-WEBHOOK] messageBody: {corpo}")

    try:
        # Validar campos obrigatorios
        if not request.sessionId:
            print(f"[SENDER-WEBHOOK] Erro: sessionId is required")
            raise HTTPException(status_code=400, detail="sessionId is required")

        # Aviso de falha do turno da empresa: vem SEM messageBody (nao ha fala
        # nenhuma), entao precisa ser roteado antes da exigencia de corpo.
        if (request.action or "").strip() == COMPANY_AI_FAILED_ACTION:
            return _handle_company_ai_failed(request)

        company_attachments = _anexos_do_payload(request)
        e_retomada = (request.action or "").strip() == RESUME_ACTION

        # CORPO VAZIO (pausa da empresa, 29/09). A ordem importa: ate 30/09 a
        # exigencia de corpo vinha antes do modo manual, e o aviso de modo manual
        # sem texto - que o documento prometia tratar - morria em 400. Tambem
        # morria a resposta humana SO com arquivo, se a plataforma deixasse de
        # mandar o texto "[N arquivo(s) anexado(s)]".
        if not (request.messageBody or "").strip():
            if company_attachments and not e_retomada:
                # Anexo sem texto e fala da empresa: o corpo vira o marcador, com
                # os nomes - dois envios so de arquivo nao podem ter o mesmo
                # texto, senao a guarda de despacho os trata como duplicata.
                nomes = ", ".join(nomes_dos_anexos(company_attachments))
                request.messageBody = (
                    f"[{len(company_attachments)} arquivo(s) anexado(s) pela empresa]"
                    f"{' ' + nomes if nomes else ''}"
                )
            elif e_retomada and (company_attachments or (request.clientFeedback or "").strip()):
                pass  # a retomada le clientFeedback e os anexos, nao o corpo
            elif request.manualModeActive:
                print(f"[SENDER-WEBHOOK] ⚠️ MODO MANUAL ATIVO - ignorando processamento, humano assumiu controle")
                return WebhookSenderMessageResponse(
                    success=True,
                    companyMessageId=f"msg_company_{uuid.uuid4().hex[:12]}",
                    zelinhuMessageId="",
                    response=WebhookSenderMessageResponseData(
                        messageBody="",
                        negotiationComplete=False,
                        action="manual_mode"
                    ),
                    round=_requested_round(request.round, 1)
                )
            else:
                print(f"[SENDER-WEBHOOK] Erro: messageBody is required")
                raise HTTPException(status_code=400, detail="messageBody is required")
        company_message_for_analysis = _company_response_with_attachment_context(
            request.messageBody or "",
            company_attachments,
        )

        # ROTEAMENTO POR ACTION: o backend usa uma URL so para todas as
        # operacoes. Fica ANTES do guard de espera pela decisao do cliente -
        # esta chamada e justamente a decisao dele chegando. Action
        # desconhecida ou ausente segue o caminho de sempre.
        if (request.action or "").strip() == RESUME_ACTION:
            return await _handle_resume_action(request)

        # Gerar IDs para as mensagens
        company_message_id = f"msg_company_{uuid.uuid4().hex[:12]}"
        zelinhu_message_id = f"msg_zelinhu_{uuid.uuid4().hex[:12]}"

        # CONTEXTO DO PROPRIO PAYLOAD (pausa da empresa, 29/09)
        # -----------------------------------------------------
        # O start_negotiation traz ticket, client e company, mas ate 30/09 so a
        # retomada os lia. A sessao nova dependia de _get_session_context (o
        # endpoint do 404 de 25/08), e quem cobria a falha era a IA da Empresa,
        # repassando o contexto dela (absorb_session_context). Com a empresa
        # pausada ela nao roda: o ZelinhU comecava com "Cliente", R$ 0 e sem
        # direitos.
        contexto_do_payload = _contexto_da_retomada(
            request.ticket, request.client, request.company, request.selectedRights
        )

        # Inicializar ou obter sessao
        if request.sessionId not in _sender_sessions:
            if contexto_do_payload:
                print(
                    f"[SENDER-WEBHOOK] Nova sessao - contexto veio no proprio payload "
                    f"(valor da causa: {contexto_do_payload['estimated_value']})"
                )
                context = contexto_do_payload
            else:
                print(f"[SENDER-WEBHOOK] Nova sessao - buscando contexto...")
                context = await _get_session_context(request.sessionId)

            _sender_sessions[request.sessionId] = {
                "negotiationId": request.negotiationId,
                "status": "negotiating",
                "currentRound": _requested_round(request.round, 1),
                "messages": [],
                "context": context or {},
                "createdAt": datetime.now().isoformat()
            }
        elif contexto_do_payload:
            # Sessao viva sem contexto: preenche. Contexto que ja existe fica.
            absorb_session_context(
                request.sessionId, contexto_do_payload, origem="payload da mensagem"
            )

        session = _sender_sessions[request.sessionId]
        current_round = _requested_round(request.round, session.get("currentRound", 1))
        session["currentRound"] = current_round
        await _store_company_response_attachments(session, company_attachments)

        # Verificar se sessao esta ativa
        if session.get("status") in TERMINAL_STATUSES:
            print(f"[SENDER-WEBHOOK] Erro: Session is not active (status={session.get('status')})")
            raise HTTPException(status_code=400, detail="Session is not active")

        # AGUARDANDO DECISAO DO CLIENTE (QA-04): depois de
        # [PROPOSTA PARA APROVACAO DO CLIENTE], a negociacao automatica acabou.
        # Nova oferta da empresa nao reabre rodada sozinha - so o /resume, que
        # carrega a decisao do cliente, volta a mover a negociacao.
        if _is_awaiting_client_decision(session):
            print(
                f"[SENDER-WEBHOOK] Sessao aguardando decisao do cliente desde "
                f"{session.get('awaitingSince')} - nova mensagem da empresa nao "
                f"gera rodada automatica"
            )
            _guardar_fala_em_pausa(
                session, request, company_message_id, company_message_for_analysis,
                "a proposta esta com o cliente, aguardando a decisao dele",
            )
            return WebhookSenderMessageResponse(
                success=True,
                companyMessageId=company_message_id,
                zelinhuMessageId="",
                response=WebhookSenderMessageResponseData(
                    messageBody="",
                    negotiationComplete=False,
                    action="awaiting_client_decision",
                ),
                round=current_round,
            )

        # AGUARDANDO SOLICITACAO (spec 20260901, secao 3): emitido um pedido, a
        # negociacao pausa ate uma PESSOA responder. Nova mensagem da empresa nao
        # reabre rodada - quem destrava e o resume, que traz o que foi respondido.
        if _is_awaiting_request(session):
            print(
                f"[SENDER-WEBHOOK] Sessao aguardando "
                f"{session.get('awaitingRequestCount')} solicitacao(oes) desde "
                f"{session.get('awaitingRequestSince')} - nova mensagem da empresa "
                f"nao gera rodada automatica"
            )
            _guardar_fala_em_pausa(
                session, request, company_message_id, company_message_for_analysis,
                "a negociacao esta pausada aguardando solicitacao ja enviada ao cliente",
            )
            return WebhookSenderMessageResponse(
                success=True,
                companyMessageId=company_message_id,
                zelinhuMessageId="",
                response=WebhookSenderMessageResponseData(
                    messageBody="",
                    negotiationComplete=False,
                    action="awaiting_request",
                ),
                round=current_round,
            )

        # IDEMPOTENCIA DE ENTRADA (A3): o backend estoura o timeout de 60s e
        # reenvia este webhook. Sem esta trava, o Sender gera uma SEGUNDA
        # contraproposta para a mesma rodada e posta as duas.
        previous = _get_round_record(session, current_round, request.messageBody)
        if previous:
            if previous.get("state") == ROUND_STATE_DONE:
                print(
                    f"[SENDER-WEBHOOK] Reenvio da rodada {current_round} - devolvendo "
                    f"o resultado ja processado (idempotencia A3)"
                )
                return WebhookSenderMessageResponse(
                    success=True,
                    companyMessageId=previous.get("companyMessageId", company_message_id),
                    zelinhuMessageId=previous.get("zelinhuMessageId", ""),
                    response=WebhookSenderMessageResponseData(
                        messageBody=previous.get("messageBody", ""),
                        negotiationComplete=previous.get("negotiationComplete", False),
                        action=previous.get("action", "duplicate_replay"),
                    ),
                    round=previous.get("round", current_round),
                )

            print(
                f"[SENDER-WEBHOOK] Rodada {current_round} ainda em processamento - "
                f"reenvio ignorado (idempotencia A3)"
            )
            return WebhookSenderMessageResponse(
                success=True,
                companyMessageId=company_message_id,
                zelinhuMessageId="",
                response=WebhookSenderMessageResponseData(
                    messageBody="",
                    negotiationComplete=False,
                    action="duplicate_in_progress",
                ),
                round=current_round,
            )

        _mark_round_in_progress(session, current_round, request.messageBody)

        # Salvar mensagem da empresa
        session["messages"].append({
            "id": company_message_id,
            "origin": "company_ai",
            # Com o marcador de anexo: no historico das proximas rodadas o
            # ZelinhU ve que esta fala veio acompanhada de arquivo
            "body": company_message_for_analysis,
            "createdAt": datetime.now().isoformat()
        })
        print(f"[SENDER-WEBHOOK] Mensagem da empresa salva: {company_message_id}")

        # Obter dados do contexto
        context = session.get("context", {})
        client_name = context.get("client", {}).get("name", "Cliente")
        estimated_value = context.get("estimated_value", 0)

        # Se nao temos contexto, tentar buscar novamente
        if not client_name or client_name == "Cliente":
            print(f"[SENDER-WEBHOOK] Contexto incompleto, buscando novamente...")
            fresh_context = await _get_session_context(request.sessionId)
            if fresh_context:
                session["context"] = fresh_context
                client_name = fresh_context.get("client", {}).get("name", "Cliente")
                estimated_value = fresh_context.get("estimated_value", 0)

        # Se a empresa ja marcou como completo, aceitar
        ai_analysis = None
        if request.negotiationComplete:
            print(f"[SENDER-WEBHOOK] Empresa marcou negotiationComplete=true")
            action = "send_to_client"
            reason = "Empresa indicou conclusao; proposta sera submetida ao cliente para aprovacao"
        else:
            # Analisar resposta da empresa usando IA
            print(f"[SENDER-WEBHOOK] Analisando resposta da empresa com IA...")
            analysis = await _analyze_company_response(company_message_for_analysis, current_round, session)
            action = analysis["action"]
            reason = analysis["reason"]
            ai_analysis = analysis.get("ai_analysis")
            print(f"[SENDER-WEBHOOK] Analise: action={action}, reason={reason}")

        company_request = _company_request_a_emitir(request, session)
        if company_request:
            ai_analysis = dict(ai_analysis or {})
            existentes = list(ai_analysis.get("requests") or [])
            ja_detectado = any(
                isinstance(pedido, dict)
                and (pedido.get("audience") or "") == company_request.get("audience")
                and (pedido.get("title") or "").strip().casefold()
                == (company_request.get("title") or "").strip().casefold()
                for pedido in existentes
            )
            if ja_detectado:
                print(
                    "[SOLICITACOES] request estruturado da empresa ja apareceu na "
                    "analise do ZelinhU - evitando duplicidade"
                )
            else:
                ai_analysis["requests"] = [company_request, *existentes]

            # O pedido da empresa vence o fechamento, como o pedido da analise ja
            # vencia (QA de 10/09, achado 6). Antes ele entrava DEPOIS dessa
            # regra: se o turno ia mandar a proposta ao cliente, o pedido era
            # descartado em silencio por "pedido e acordo nao saem juntos".
            # Empresa que declarou o caso encerrado (negotiationComplete) nao
            # tem pedido pendente - ali o fechamento continua valendo.
            if not request.negotiationComplete and _pedido_ao_cliente_vence_o_fechamento(
                action, ai_analysis, current_round, max_rounds=_round_cap(session)
            ):
                print(
                    "[SOLICITACOES] Fechamento adiado: a empresa pediu ao cliente "
                    "um item estruturado - o pedido sai antes da proposta"
                )
                action = "counter_proposal"

        # Se a proposta chegou no ponto de acordo, preparar detalhes de acordo
        if action == "send_to_client":
            session["agreementDetails"] = _get_agreement_details(
                ai_analysis,
                company_message_for_analysis,
                estimated_value
            )

        # Gerar proxima mensagem do ZelinhU usando IA
        print(f"[SENDER-WEBHOOK] Gerando mensagem do ZelinhU com IA (action={action})...")
        zelinhu_message, negotiation_complete = await _generate_zelinhu_message(
            action=action,
            client_name=client_name,
            round_number=current_round,
            estimated_value=estimated_value,
            company_response=company_message_for_analysis,
            session=session,
            ai_analysis=ai_analysis
        )

        # Salvar mensagem do ZelinhU
        session["messages"].append({
            "id": zelinhu_message_id,
            "origin": "zelinhu",
            "body": zelinhu_message,
            "action": action,
            "createdAt": datetime.now().isoformat()
        })
        print(f"[SENDER-WEBHOOK] Mensagem do ZelinhU gerada: {zelinhu_message_id}")
        print(f"[SENDER-WEBHOOK] Mensagem: {zelinhu_message[:150]}...")

        # SOLICITACOES (spec 20260901): antes de seguir o turno normal, ver se a
        # analise pediu alguma coisa. Emitida a rajada, a negociacao PAUSA aqui -
        # nao ha contraproposta nem loop ate uma pessoa responder.
        pedidos = _requests_a_emitir(ai_analysis, action, negotiation_complete)

        aceitas: List[Dict[str, Any]] = []
        if pedidos:
            aceitas = await _dispatch_requests(
                session_id=request.sessionId,
                negotiation_id=request.negotiationId,
                message_body=zelinhu_message,
                round_number=current_round,
                pedidos=pedidos,
                session=session,
            )

        # O pedido da empresa que nao saiu fica registrado - nunca some calado
        if company_request:
            _auditar_pedido_da_empresa(session, company_request, pedidos, aceitas)

        if pedidos:
            if aceitas:
                _mark_awaiting_request(session, aceitas)

                return WebhookSenderMessageResponse(
                    success=True,
                    companyMessageId=company_message_id,
                    zelinhuMessageId=zelinhu_message_id,
                    response=WebhookSenderMessageResponseData(
                        messageBody=zelinhu_message,
                        negotiationComplete=False,
                        action=REQUEST_ACTION,
                    ),
                    round=current_round,
                )

            # Nenhuma foi aceita (lotacao ou defeito nosso): a negociacao NAO
            # pode ficar parada esperando pendencia que nao existe. Seguimos o
            # turno normal, que e o comportamento de antes das solicitacoes.
            print(
                "[SENDER-WEBHOOK] Nenhuma solicitacao foi aceita - seguindo o "
                "turno normal em vez de pausar a negociacao"
            )

        # send_to_client encerra a negociacao automatica mesmo com
        # negotiation_complete=False - e o que faltava marcar (QA-02/04/05)
        _mark_awaiting_client_decision(session, action)

        # Atualizar status se negociacao concluida
        if negotiation_complete:
            if action == "send_to_client":
                session["status"] = "agreement"
            elif action == "escalate":
                session["status"] = "escalated"
            else:
                session["status"] = "no_agreement"
            session["endedAt"] = datetime.now().isoformat()
            print(f"[SENDER-WEBHOOK] Negociacao concluida: status={session['status']}")

            # Enviar mensagem final do ZelinhU para o Backend (salvar no banco)
            print(f"[SENDER-WEBHOOK] Enviando mensagem final do ZelinhU para Backend (action={action})...")
            final_response = await _send_zelinhu_message_to_backend(
                session_id=request.sessionId,
                negotiation_id=request.negotiationId,
                message_body=zelinhu_message,
                round_number=current_round + 1,
                negotiation_complete=True,
                action=action,
                session=session,
                dispatch_key=dispatch_key_for(request.messageBody)
            )

            # Verificar se modo manual foi ativado durante o processamento
            if final_response and final_response.get("manualModeActive", False):
                print(f"[SENDER-WEBHOOK] ⚠️ MODO MANUAL ATIVO detectado na resposta - parando (mensagem ja salva)")
                session["status"] = "paused_manual"
            else:
                # Notificar Backend sobre status final da negociacao
                print(f"[SENDER-WEBHOOK] Notificando status final: {session['status']}")
                settings = get_settings()
                zellu_api_client = ZelluAPIClient(
                    base_url=settings.ZELLU_BACKEND_URL,
                    api_key=settings.API_KEY_ZELLU_IA
                )
                status_result = await zellu_api_client.send_negotiation_status(
                    session_id=request.sessionId,
                    status=session["status"],
                    negotiation_id=request.negotiationId,
                    final_round=current_round + 1,
                    reason=f"ZelinhU {action}",
                    agreement_details=session.get("agreementDetails") if session.get("status") == "agreement" else None,
                    final_message=zelinhu_message
                )
                if status_result.get("success"):
                    print(f"[SENDER-WEBHOOK] ✅ Status notificado: isTerminal={status_result.get('isTerminal')}")
                else:
                    print(f"[SENDER-WEBHOOK] ⚠️ Erro ao notificar status: {status_result.get('error')}")

        # Se a acao e apenas submeter ao cliente, nao continuar o loop localmente
        should_continue_loop = action == "counter_proposal"

        if should_continue_loop:
            # =====================================================================
            # LOOP DE NEGOCIACAO: Continua ate negotiationComplete=true
            # O Backend retorna a resposta da IA empresa no mesmo HTTP response,
            # entao precisamos processar em loop ate concluir.
            # =====================================================================
            next_round = current_round + 1
            backend_response = None
            loop_count = 0
            max_loop_iterations = MAX_ROUNDS * 2  # Seguranca contra loop infinito

            # Mensagem da outra parte que originou a mensagem que vamos despachar.
            # E ela que identifica o despacho, nao o nosso texto (QA-01).
            origin_message = request.messageBody

            while not negotiation_complete and loop_count < max_loop_iterations:
                loop_count += 1
                print(f"\n[SENDER-WEBHOOK] ===== LOOP ITERACAO {loop_count} (round {next_round}) =====")

                # Enviar mensagem do ZelinhU para o Backend
                # `action` aqui e o veredito que produziu `zelinhu_message`. Se for
                # terminal, o despacho leva o discriminador: sem ele o backend nao
                # distingue a mensagem final de uma que avanca rodada, salva e
                # dispara mais um turno da IA da empresa (plano §2.2).
                despacho_terminal = _e_despacho_terminal(action)
                if despacho_terminal:
                    print(f"[SENDER-WEBHOOK] Despacho TERMINAL (action={action}) - fecha a negociacao automatica")

                print(f"[SENDER-WEBHOOK] Enviando msg ZelinhU para Backend...")
                backend_response = await _send_zelinhu_message_to_backend(
                    session_id=request.sessionId,
                    negotiation_id=request.negotiationId,
                    message_body=zelinhu_message,
                    round_number=next_round,
                    negotiation_complete=despacho_terminal,
                    action=action if despacho_terminal else None,
                    session=session,
                    dispatch_key=dispatch_key_for(origin_message)
                )

                if backend_response is DISPATCH_DUPLICATE:
                    # Ja respondemos a esta mensagem. Encerrar sem erro: a
                    # negociacao nao falhou, so nao ha nada novo a enviar.
                    print(f"[SENDER-WEBHOOK] Nada novo a despachar nesta rodada - encerrando o loop sem erro")
                    break

                if backend_response is DISPATCH_ACCEPTED:
                    # Turno assincrono: a resposta da empresa vem depois, por
                    # callback. Encerrar sem erro e sem ler corpo nenhum - o de um
                    # 202 e ack vazio, e analisa-lo seria negociar com o nada.
                    print(
                        f"[SENDER-WEBHOOK] Turno entregue para processamento assincrono - "
                        f"encerrando o loop e aguardando o callback da empresa"
                    )
                    break

                if not backend_response:
                    print(f"[SENDER-WEBHOOK] ⚠️ Falha ao enviar para Backend - parando loop")
                    break

                # Rede a mais: ack que chegue como 200 com corpo {"accepted": true}
                # em vez de 202. O 202 propriamente dito ja parou no
                # DISPATCH_ACCEPTED acima, sem nem ler corpo.
                if backend_response.get("accepted") is True and "response" not in backend_response:
                    print(f"[SENDER-WEBHOOK] ACK assincrono recebido - aguardando callback")
                    break

                # Verificar se modo manual foi ativado (humano assumiu controle)
                manual_mode_active = backend_response.get("manualModeActive", False)
                if manual_mode_active:
                    print(f"[SENDER-WEBHOOK] ⚠️ MODO MANUAL ATIVO - humano assumiu controle, parando loop de negociacao")
                    session["status"] = "paused_manual"
                    break

                # Extrair resposta da IA empresa do response do Backend
                response_data = backend_response.get("response") or {}
                company_message = (response_data.get("messageBody") or "").strip()

                # `negotiationComplete` pode vir em dois lugares. No POST comum
                # ele esta dentro de `response`; no POST terminal o backend
                # responde 200 com o campo na RAIZ e sem `response` nenhum
                # (plano de 25/08/2026, §2.2). Ler so o aninhado faria a gente
                # ignorar o fim da negociacao vindo pelo caminho terminal.
                negotiation_complete_from_backend = bool(
                    response_data.get("negotiationComplete")
                    or backend_response.get("negotiationComplete")
                )

                print(f"[SENDER-WEBHOOK] Resposta do Backend recebida:")
                print(f"[SENDER-WEBHOOK] - negotiationComplete: {negotiation_complete_from_backend}")
                print(f"[SENDER-WEBHOOK] - messageBody: {company_message[:100]}..." if len(company_message) > 100 else f"[SENDER-WEBHOOK] - messageBody: {company_message}")

                # Se Backend indicou conclusao, encerrar
                if negotiation_complete_from_backend:
                    print(f"[SENDER-WEBHOOK] ✅ Backend indicou negotiationComplete=true - encerrando loop")
                    negotiation_complete = True
                    session["status"] = "agreement"
                    session["endedAt"] = datetime.now().isoformat()
                    break

                # Sucesso sem fala da empresa nao e rodada: e resposta sem nada
                # para continuar. O plano de 25/08 (§2.2) documenta um 200 assim,
                # sem o campo `response`. Analisar string vazia gastaria uma
                # chamada de LLM para redigir contraproposta ao nada - o mesmo
                # erro que o tratamento ingenuo do 202 teria criado.
                if not company_message:
                    print(
                        f"[SENDER-WEBHOOK] Backend respondeu sem mensagem da empresa - "
                        f"encerrando o loop sem erro, nada a analisar"
                    )
                    break

                # Salvar mensagem da empresa
                company_msg_id = f"msg_company_{uuid.uuid4().hex[:12]}"
                session["messages"].append({
                    "id": company_msg_id,
                    "origin": "company_ai",
                    "body": company_message,
                    "createdAt": datetime.now().isoformat()
                })

                # Analisar resposta da empresa e decidir proxima acao (IA)
                print(f"[SENDER-WEBHOOK] Analisando nova resposta da empresa com IA...")
                analysis = await _analyze_company_response(company_message, next_round, session)
                action = analysis["action"]
                loop_ai_analysis = analysis.get("ai_analysis")
                if action == "send_to_client":
                    session["agreementDetails"] = _get_agreement_details(
                        loop_ai_analysis,
                        company_message,
                        estimated_value
                    )
                print(f"[SENDER-WEBHOOK] Analise: action={action}, reason={analysis['reason']}")

                # Gerar proxima mensagem do ZelinhU (IA)
                zelinhu_message, negotiation_complete = await _generate_zelinhu_message(
                    action=action,
                    client_name=client_name,
                    round_number=next_round,
                    estimated_value=estimated_value,
                    company_response=company_message,
                    session=session,
                    ai_analysis=loop_ai_analysis
                )

                # A proxima mensagem responde a ESTA mensagem da empresa: e ela
                # que passa a identificar o despacho da proxima iteracao.
                origin_message = company_message

                # Salvar mensagem do ZelinhU
                zelinhu_msg_id = f"msg_zelinhu_{uuid.uuid4().hex[:12]}"
                session["messages"].append({
                    "id": zelinhu_msg_id,
                    "origin": "zelinhu",
                    "body": zelinhu_message,
                    "action": action,
                    "createdAt": datetime.now().isoformat()
                })

                print(f"[SENDER-WEBHOOK] Nova msg ZelinhU gerada: {zelinhu_message[:100]}...")

                # SOLICITACOES NA VOLTA DO LOOP. Ate 11/09 este caminho nunca olhava
                # `requests`: o pedido nascido aqui morria em silencio. Mesmo
                # tratamento do turno de entrada - emitida a rajada, o loop para.
                pedidos = _requests_a_emitir(loop_ai_analysis, action, negotiation_complete)
                if pedidos:
                    aceitas = await _dispatch_requests(
                        session_id=request.sessionId,
                        negotiation_id=request.negotiationId,
                        message_body=zelinhu_message,
                        round_number=next_round,
                        pedidos=pedidos,
                        session=session,
                    )
                    if aceitas:
                        _mark_awaiting_request(session, aceitas)
                        action = REQUEST_ACTION
                        break

                _mark_awaiting_client_decision(session, action)

                # Atualizar status se negociacao concluida
                if negotiation_complete:
                    if action == "send_to_client":
                        session["status"] = "agreement"
                    elif action == "escalate":
                        session["status"] = "escalated"
                    else:
                        session["status"] = "no_agreement"
                    session["endedAt"] = datetime.now().isoformat()
                    print(f"[SENDER-WEBHOOK] Negociacao concluida: status={session['status']}")

                    # Enviar mensagem final para o Backend
                    await _send_zelinhu_message_to_backend(
                        session_id=request.sessionId,
                        negotiation_id=request.negotiationId,
                        message_body=zelinhu_message,
                        round_number=next_round + 1,
                        negotiation_complete=True,
                        action=action,
                        session=session,
                        dispatch_key=dispatch_key_for(origin_message)
                    )

                    # Notificar Backend sobre status final da negociacao
                    print(f"[SENDER-WEBHOOK] Notificando status final: {session['status']}")
                    settings = get_settings()
                    zellu_api_client = ZelluAPIClient(
                        base_url=settings.ZELLU_BACKEND_URL,
                        api_key=settings.API_KEY_ZELLU_IA
                    )
                    status_result = await zellu_api_client.send_negotiation_status(
                        session_id=request.sessionId,
                        status=session["status"],
                        negotiation_id=request.negotiationId,
                        final_round=next_round + 1,
                        reason=f"ZelinhU {action}",
                        agreement_details=session.get("agreementDetails") if session.get("status") == "agreement" else None,
                        final_message=zelinhu_message
                    )
                    if status_result.get("success"):
                        print(f"[SENDER-WEBHOOK] ✅ Status notificado: isTerminal={status_result.get('isTerminal')}")
                    else:
                        print(f"[SENDER-WEBHOOK] ⚠️ Erro ao notificar status: {status_result.get('error')}")
                    break

                next_round += 1
                session["currentRound"] = next_round

            if loop_count >= max_loop_iterations:
                print(f"[SENDER-WEBHOOK] ⚠️ Atingiu limite de iteracoes ({max_loop_iterations}) - parando loop")
                session["status"] = "no_agreement"
                session["endedAt"] = datetime.now().isoformat()
        else:
            print(f"[SENDER-WEBHOOK] Acao '{action}' nao exige loop imediato; retornando para o backend para aguardar a decisao do cliente")

            # Aqui nao ha loop, entao ate 26/08/2026 nao saia POST reverso NENHUM:
            # a conclusao dependia so do corpo da resposta HTTP, que o Cloudflare
            # corta aos ~100s. Era a "rodada final que se perde" do plano - e como
            # toda negociacao termina em send_to_client, era em todas.
            #
            # O corpo continua indo como sempre (plano §2.2: "continuem enviando
            # o corpo"). Os dois canais sao seguros: a guarda do backend compara
            # conteudo em janela de 60s, e aqui o intervalo e ~0.
            if _e_despacho_terminal(action):
                print(f"[SENDER-WEBHOOK] Despacho TERMINAL fora do loop (action={action})")
                await _send_zelinhu_message_to_backend(
                    session_id=request.sessionId,
                    negotiation_id=request.negotiationId,
                    message_body=zelinhu_message,
                    round_number=current_round + 1,
                    negotiation_complete=True,
                    action=action,
                    session=session,
                    dispatch_key=dispatch_key_for(request.messageBody),
                )

        # Montar resposta final
        final_round = session.get("currentRound", current_round)
        final_status = session.get("status", "negotiating")

        response = WebhookSenderMessageResponse(
            success=True,
            companyMessageId=company_message_id,
            zelinhuMessageId=zelinhu_message_id,
            response=WebhookSenderMessageResponseData(
                messageBody=zelinhu_message,
                negotiationComplete=negotiation_complete,
                action=action
            ),
            round=final_round,
            # Top-level fields para o backend parsear diretamente
            zelinhuMessage=zelinhu_message if negotiation_complete else None,
            negotiationComplete=negotiation_complete if negotiation_complete else None,
            status=final_status if negotiation_complete else None
        )

        # Rodada concluida: um reenvio deste mesmo webhook agora devolve esta
        # resposta em vez de gerar outra contraproposta (A3).
        _mark_round_done(session, current_round, request.messageBody, {
            "companyMessageId": company_message_id,
            "zelinhuMessageId": zelinhu_message_id,
            "messageBody": zelinhu_message,
            "negotiationComplete": negotiation_complete,
            "action": action,
            "round": final_round,
        })

        if negotiation_complete:
            print(f"\n[SENDER-WEBHOOK] ========== NEGOCIACAO FINALIZADA ==========")
        else:
            print(f"\n[SENDER-WEBHOOK] ========== PROCESSAMENTO DO WEBHOOK CONCLUIDO ==========")
        print(f"[SENDER-WEBHOOK] success: True")
        print(f"[SENDER-WEBHOOK] status: {final_status}")
        print(f"[SENDER-WEBHOOK] totalRounds: {final_round}")
        print(f"[SENDER-WEBHOOK] totalMessages: {len(session.get('messages', []))}")
        print(f"[SENDER-WEBHOOK] negotiationComplete: {negotiation_complete}")
        if negotiation_complete:
            print(f"[SENDER-WEBHOOK] zelinhuMessage incluida no response ({len(zelinhu_message)} chars)")

        return response

    except HTTPException:
        raise
    except Exception as e:
        # Libera a rodada: se o processamento falhou, um reenvio do backend e
        # legitimo e precisa poder reprocessar (A3).
        #
        # A rodada tem que ser a MESMA que _mark_round_in_progress usou, senao a
        # trava fica presa em "in_progress" para sempre e todo reenvio e
        # ignorado. `session["currentRound"]` guarda esse valor.
        try:
            sessao = _sender_sessions.get(request.sessionId) or {}
            _clear_round(
                sessao,
                _requested_round(request.round, sessao.get("currentRound", 1)),
                request.messageBody,
            )
        except Exception:  # noqa: BLE001
            pass
        print(f"[SENDER-WEBHOOK] Erro inesperado: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


@router.post(
    "/resume",
    response_model=WebhookSenderResumeResponse,
    summary="Retomar negociacao apos recusa do cliente",
    description="""
    Endpoint chamado pelo app Zellu quando o cliente recusa uma proposta.
    O Sender envia uma nova mensagem ao backend para retomar a negociacao.
    """
)
async def webhook_sender_resume(request: WebhookSenderResumeRequest):
    """
    Endpoint dedicado da retomada.

    O backend NAO usa este caminho: ele manda tudo para uma URL so e distingue
    pelo campo `action` (contrato confirmado em 24/08/2026). O endpoint segue
    aqui para quem chama direto - o app e os testes - e divide o mesmo nucleo
    com o roteamento por action, para as duas portas nunca divergirem.
    """
    print(f"\n[SENDER-RESUME] ========== RETOMAR NEGOCIACAO ==========")
    print(f"[SENDER-RESUME] sessionId: {request.sessionId}")
    print(f"[SENDER-RESUME] negotiationId: {request.negotiationId}")
    print(f"[SENDER-RESUME] rejectionCount: {request.rejectionCount}")
    print(f"[SENDER-RESUME] clientFeedback: {request.clientFeedback}")

    if not request.sessionId:
        raise HTTPException(status_code=400, detail="sessionId is required")
    if not request.clientFeedback:
        raise HTTPException(status_code=400, detail="clientFeedback is required")

    resultado = await _resume_negotiation(
        session_id=request.sessionId,
        negotiation_id=request.negotiationId,
        client_feedback=request.clientFeedback,
        rejection_count=request.rejectionCount,
        previous_agreement=request.previousAgreement,
        ticket=request.ticket,
        client=request.client,
        company=request.company,
        selected_rights=request.selectedRights,
        attachments=_anexos_do_payload(request),
        requests=request.requests,
    )

    return WebhookSenderResumeResponse(
        success=resultado["success"],
        message=(
            "Retomada de negociacao solicitada" if resultado["success"]
            else "Falha ao solicitar retomada de negociacao"
        ),
        round=resultado["round"],
        backendResponse=resultado["backendResponse"],
    )


# =============================================================================
# A RAJADA, PEDIDO A PEDIDO (contrato 20260911, Etapa 1)
# =============================================================================
# Ate 11/09 a retomada trazia `files`: uma lista PLANA com as respostas de ate 6
# pedidos. O ZelinhU comparava esses arquivos com o que ELE LEMBRAVA ter pedido,
# e as duas metades dessa frase estavam quebradas:
#
#   - a lista plana nao diz qual arquivo responde a qual pedido. Dois
#     comprovantes de meses diferentes eram julgados contra o pedido errado;
#   - o que foi pedido morava na memoria do processo. Uma solicitacao pode
#     esperar dias, e um restart no meio apagava o pedido.
#
# Com `requests` o payload passa a ser a fonte da verdade. A memoria vira o
# caminho de compatibilidade, para quando o backend ainda nao mandar o campo.


def _pedidos_da_rajada(requests: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Normaliza os pedidos do payload para o formato que o ZelinhU usa.

    Aceita o que vier: um pedido sem titulo ou com status estranho nao pode
    derrubar a retomada inteira, que e o caminho onde falhar deixa um cliente
    real esperando sem prazo.
    """
    pedidos: List[Dict[str, Any]] = []

    for bruto in requests or []:
        if not isinstance(bruto, dict):
            continue

        arquivos = bruto.get("files")
        pedidos.append({
            "requestId": str(bruto.get("requestId") or "").strip(),
            "title": str(bruto.get("title") or "").strip(),
            "description": str(bruto.get("description") or "").strip(),
            "audience": bruto.get("audience") or "",
            "type": bruto.get("type") or "",
            "responseType": bruto.get("responseType") or bruto.get("response_type") or "",
            "status": str(bruto.get("status") or "").strip().lower(),
            "answer": bruto.get("answer"),
            "declineReason": bruto.get("declineReason"),
            "files": list(arquivos) if isinstance(arquivos, list) else [],
            # `null` != `undetermined`: ver traduzir_do_contrato.
            "verdict": bruto.get("verdict"),
        })

    return pedidos


async def _conferir_a_rajada(
    pedidos: List[Dict[str, Any]], llm: Any
) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    """Le e julga a rajada PEDIDO A PEDIDO.

    Returns:
        (documentos, veredictos) - os documentos de todos os pedidos, para o
        estado do agente, e um veredito por pedido que tinha o que conferir.

    Tres regras, todas do contrato:

    1. 🔑 Cada pedido e julgado contra os arquivos DELE. E o conserto do defeito
       2 do nosso plano de 11/09: na lista plana, o comprovante de marco podia
       ser julgado contra o pedido do contrato.
    2. Veredito que ja veio preenchido nao e refeito (§3.1). Eles conferiram no
       envio; julgar de novo pagaria o LLM duas vezes pela mesma resposta.
       `null` NAO e veredito - ai julgamos, e e a rede que o §3.1 preserva.
    3. Pedido RECUSADO nao e julgado (defeito 3). Ele nao tem documento: emitir
       veredito sobre ele seria julgar o pedido contra um arquivo que nunca foi
       dele.

    O orcamento de texto e de arquivos e COMPARTILHADO entre os pedidos: seis
    chamadas com o teto cheio empurrariam o caso e o livro-razao para fora da
    janela do modelo.
    """
    documentos_da_rajada: List[Dict[str, str]] = []
    veredictos: List[Dict[str, str]] = []
    restante_texto = CAP_TOTAL
    restante_anexos = MAX_ANEXOS

    for pedido in pedidos:
        titulo = pedido.get("title") or "solicitacao sem titulo"
        documentos: List[Dict[str, str]] = []

        if pedido.get("files") and restante_anexos > 0:
            documentos = await ingerir_anexos(
                pedido["files"], teto_total=restante_texto, max_anexos=restante_anexos
            )
            documentos_da_rajada.extend(documentos)
            restante_texto -= sum(len(d.get("texto") or "") for d in documentos)
            restante_anexos -= len(documentos)

        ja_conferido = traduzir_do_contrato(pedido.get("verdict"))
        if ja_conferido:
            registro = {
                "solicitacao": titulo,
                "veredito": ja_conferido,
                "motivo": MOTIVO_CONFERIDO_NA_ENTREGA,
            }
            if pedido.get("requestId"):
                registro["requestId"] = pedido["requestId"]
            veredictos.append(registro)
            print(f"[VEREDITO] {titulo}: {ja_conferido} (conferido na entrega, nao rejulgado)")
            continue

        if pedido.get("status") != STATUS_PEDIDO_ATENDIDO or not documentos:
            continue

        veredictos.extend(await julgar_documentos([pedido], documentos, llm=llm))

    return documentos_da_rajada, veredictos


def _status_por_pedido(pedidos: List[Dict[str, Any]]) -> Dict[str, str]:
    """O desfecho de cada pedido, por requestId, para a baixa na sessao.

    Numa rajada MISTA o `resumeReason` nao serve: ele e um so para a rajada
    inteira, e o backend passou a mandar `request_fulfilled` quando ao menos uma
    foi atendida (§2.2). Quem sabe o desfecho de cada pedido e o proprio pedido.
    """
    return {
        pedido["requestId"]: pedido.get("status") or ""
        for pedido in pedidos
        if pedido.get("requestId")
    }


def _resumo_da_rajada(
    pedidos: List[Dict[str, Any]],
    veredictos: Optional[List[Dict[str, str]]] = None,
) -> str:
    """Uma linha por pedido: o que foi pedido e o que voltou.

    🔴 ESTE TEXTO VAI PARA A EMPRESA, que e a outra parte da negociacao. Entram
    o titulo do pedido, o desfecho, o motivo da recusa e a resposta de texto ou
    sim/nao - tudo isso e resposta a uma pergunta que a propria empresa fez.
    NUNCA entra o conteudo de um arquivo: documento do cliente nao se despeja no
    colo do adversario. Do arquivo, so a existencia dele.
    """
    if not pedidos:
        return ""

    linhas = ["O que foi pedido e o que voltou:"]

    for pedido in pedidos:
        titulo = pedido.get("title") or "solicitacao sem titulo"
        status = pedido.get("status")

        if status == STATUS_PEDIDO_RECUSADO:
            motivo = str(pedido.get("declineReason") or "").strip()
            linhas.append(f"- {titulo}: RECUSADO{f' - {motivo}' if motivo else ''}")
            continue

        detalhes = []
        resposta = pedido.get("answer")
        if resposta not in (None, ""):
            detalhes.append(f"resposta: {resposta}")
        if pedido.get("files"):
            detalhes.append(f"{len(pedido['files'])} arquivo(s) recebido(s)")

        veredito = (veredito_do_pedido(pedido, veredictos) or {}).get("veredito")
        linhas.append(f"- {titulo}: {_desfecho_para_a_empresa(veredito, detalhes)}")

    return "\n".join(linhas)


def _desfecho_para_a_empresa(veredito: Optional[str], detalhes: List[str]) -> str:
    """Como a EMPRESA le um pedido que voltou atendido: o desfecho, nunca o motivo.

    QA de 14/09/2026: um PDF qualquer no lugar do comprovante de R$ 1.870,00 ia
    para a empresa como "ATENDIDO (1 arquivo(s) recebido(s))", e ela respondia
    que "validaria internamente". O julgamento tinha rodado; so nao chegava aqui.

    O motivo do veredito fala do documento de quem enviou e fica com quem enviou
    (§5.3, regra 3 do plano de 11/09). Para a empresa basta saber se o que ela
    pediu chegou - e "chegou um arquivo" nao e isso.
    """
    # Sem f-string aninhada: o runtime de producao e o Python 3.11, e aspas do
    # mesmo tipo dentro de uma f-string so passam do 3.12 em diante.
    sufixo = " ({})".format("; ".join(detalhes)) if detalhes else ""

    if veredito == VEREDITO_INVALIDO:
        return f"NAO ATENDIDO - o arquivo recebido nao corresponde ao que foi pedido{sufixo}"
    if veredito == VEREDITO_INDETERMINADO:
        return f"RECEBIDO, NAO CONFERIDO - nao foi possivel conferir o conteudo{sufixo}"
    return f"ATENDIDO{sufixo}"


# Marca do aviso no pedido refeito. Fixa para o aviso nao se empilhar: o pedido
# refeito pela segunda vez volta com o aviso da primeira na descricao, e ele e
# trocado, nao somado.
MARCA_PEDIDO_REFEITO = "Nova solicitacao:"


def _pedido_refeito(pedido: Dict[str, Any], motivo: str) -> Dict[str, Any]:
    """O mesmo pedido, de novo, dizendo a quem enviou por que o arquivo nao serviu.

    Mesmo titulo de proposito: e ele que liga as tentativas para o teto de
    MAX_TENTATIVAS_POR_PEDIDO. O motivo vai na descricao, que e dirigida a quem
    enviou o arquivo (decisao de 15/09/2026). Ele diz o que o documento deixa de
    provar, nunca o dado que ele contem - o prompt do julgamento proibe.
    """
    descricao = str(pedido.get("description") or "").split(MARCA_PEDIDO_REFEITO)[0].strip()
    motivo = (motivo or "").strip()
    if motivo and not motivo.endswith("."):
        motivo += "."

    aviso = " ".join(parte for parte in (
        f"{MARCA_PEDIDO_REFEITO} o arquivo enviado antes nao atendeu ao pedido.",
        motivo,
        "Envie um documento compativel.",
    ) if parte)

    return {
        "audience": pedido.get("audience"),
        "type": pedido.get("type"),
        "responseType": pedido.get("responseType") or pedido.get("response_type") or "file",
        "title": pedido.get("title"),
        "description": f"{descricao}\n\n{aviso}" if descricao else aviso,
    }


def _pedidos_a_refazer(
    pedidos: List[Dict[str, Any]],
    veredictos: List[Dict[str, str]],
    session: dict,
) -> List[Dict[str, Any]]:
    """Os pedidos que voltaram com documento invalido e devem sair de novo.

    O BURACO (QA de 14/09/2026)
    ---------------------------
    Dois arquivos deliberadamente errados, e o ZelinhU julgava, guardava o
    veredito na sessao e seguia: o pedido constava como atendido, e o bloco "JA
    SOLICITADO (nao peca de novo)" impedia de pedi-lo outra vez. A solicitacao
    nunca voltava a ficar pendente.

    Refaz so o que NOS julgamos. Veredito que veio no payload foi dado na entrega
    (Etapa 2), e la o pedido invalido nunca fecha: quem o mantem aberto e o
    backend, e pedir de novo aqui criaria uma pendencia duplicada.

    Uma linha [CONFERENCIA] por pedido conferido: e a trilha que o QA pediu, do
    arquivo recebido ao desfecho.
    """
    ligado = get_settings().NEGOTIATION_REQUEST_ENABLED
    refazer: List[Dict[str, Any]] = []

    for pedido in pedidos:
        veredito = veredito_do_pedido(pedido, veredictos)
        if not veredito:
            continue

        desfecho = veredito.get("veredito")
        if desfecho == VEREDITO_INVALIDO:
            tentativas = tentativas_invalidas(session, pedido)
            if traduzir_do_contrato(pedido.get("verdict")):
                desfecho = "invalido na entrega - o pedido segue aberto no backend"
            elif not ligado:
                desfecho = "invalido - NAO refeito: NEGOTIATION_REQUEST_ENABLED desligada"
            elif tentativas >= MAX_TENTATIVAS_POR_PEDIDO:
                desfecho = f"invalido - NAO refeito: {tentativas} tentativas, o teto"
            else:
                refazer.append(_pedido_refeito(pedido, veredito.get("motivo") or ""))
                desfecho = f"invalido - pedido refeito (tentativa {tentativas + 1})"

        print(
            f"[CONFERENCIA] requestId={pedido.get('requestId') or '-'} "
            f"titulo={pedido.get('title')!r} status={pedido.get('status') or '-'} "
            f"veredito={desfecho}"
        )

    return refazer


# Motivos de retomada que sao ciclo de SOLICITACAO, e nao rodada de negociacao.
_RETOMADAS_DE_PEDIDO = frozenset({
    RESUME_REQUEST_FULFILLED,
    RESUME_REQUEST_DENIED,
    RESUME_REQUEST_EXPIRED,
})


def _rodada_da_retomada(resume_reason: Optional[str], current_round: int) -> int:
    """A rodada em que a retomada sai. O ciclo de pedido nao gasta uma.

    O PONTO QUE O PLANO DE 11/09 MANDOU MEDIR (QA de 10/09, achado 6)
    -----------------------------------------------------------------
    Toda retomada avancava a rodada. Como a negociacao tem teto de
    MAX_ROUNDS, cada ida e volta de documento queimava uma delas - e pedir
    tres documentos empurrava a negociacao para o fechamento forcado sem que
    a empresa e o ZelinhU tivessem trocado uma palavra a mais. O resultado era
    o proprio achado 6 de volta por outra porta: a proposta ia ao cliente com
    o caso ainda pela metade.

    Do lado do backend a regra ja era essa - "a solicitacao nao avanca a
    negociacao" (spec 01/09, secao 3) -, e so a nossa contagem discordava.

    A recusa do CLIENTE continua avancando: ali houve decisao sobre a
    proposta, e a rodada seguinte e uma rodada de verdade.
    """
    if (resume_reason or "").strip() in _RETOMADAS_DE_PEDIDO:
        print(
            f"[SOLICITACOES] Retomada por pedido ({resume_reason}) - a rodada "
            f"{current_round} nao e gasta: o ciclo de solicitacao nao avanca a "
            f"negociacao"
        )
        return current_round

    return current_round + 1


CLIENT_DECISION_MARKER = "[NEGOCIACAO ENCERRADA - DECISAO DO CLIENTE]"


def _contar_recusa_do_cliente(session: dict, rejection_count: Optional[int]) -> int:
    """
    Registra mais uma recusa do cliente e devolve o total.

    O rejectionCount do backend vence quando vem maior - e a contagem dele que
    o cliente viu. A reentrega da MESMA recusa nao chega aqui
    (_resume_already_handled), entao somar um por chamada nao duplica.
    """
    try:
        do_backend = int(rejection_count or 0)
    except (TypeError, ValueError):
        do_backend = 0
    total = max(do_backend, int(session.get("clientRejections") or 0) + 1)
    session["clientRejections"] = total
    return total


async def _encerrar_para_o_cliente(
    session_id: str,
    negotiation_id: Optional[str],
    session: dict,
    round_number: int,
    recusas: int,
) -> Dict[str, Any]:
    """
    Recusa de numero NEGOTIATION_MAX_CLIENT_REJECTIONS: a negociacao automatica
    para e o caso fica com o cliente.

    Sem este limite cada recusa abria mais rodadas e a negociacao podia ir e
    voltar entre cliente e empresa indefinidamente. Aqui o ZelinhU nao
    contrapropoe: registra no chat que a decisao passou ao cliente, encerra com
    no_agreement (status terminal que o backend ja conhece) e nao espera
    resposta da empresa.
    """
    print(
        f"[SENDER-RESUME] {recusas}a recusa do cliente - negociacao automatica "
        f"encerrada, o caso volta para o cliente decidir"
    )

    message_body = (
        f"O cliente recusou {recusas} propostas nesta negociacao. A negociacao "
        f"automatica foi encerrada e o caso volta para o cliente, que vai decidir "
        f"diretamente os proximos passos.\n\n{CLIENT_DECISION_MARKER}"
    )

    zelinhu_message_id = f"msg_zelinhu_{uuid.uuid4().hex[:12]}"
    session["messages"].append({
        "id": zelinhu_message_id,
        "origin": "zelinhu",
        "body": message_body,
        "action": "no_agreement",
        "createdAt": datetime.now().isoformat(),
    })
    session["status"] = "no_agreement"
    session["endedAt"] = datetime.now().isoformat()

    backend_response = await _send_zelinhu_message_to_backend(
        session_id=session_id,
        negotiation_id=negotiation_id,
        message_body=message_body,
        round_number=round_number,
        negotiation_complete=True,
    )

    settings = get_settings()
    zellu_api_client = ZelluAPIClient(
        base_url=settings.ZELLU_BACKEND_URL,
        api_key=settings.API_KEY_ZELLU_IA,
    )
    status_result = await zellu_api_client.send_negotiation_status(
        session_id=session_id,
        status="no_agreement",
        negotiation_id=negotiation_id,
        final_round=round_number,
        reason=f"Cliente recusou {recusas} propostas - decisao devolvida ao cliente",
        final_message=message_body,
    )
    if not status_result.get("success"):
        print(f"[SENDER-RESUME] ⚠️ Erro ao notificar status: {status_result.get('error')}")

    return {
        "success": backend_response is not None and backend_response.get("success", True),
        "messageBody": message_body,
        "round": round_number,
        "zelinhuMessageId": zelinhu_message_id,
        # Sem resposta da empresa para encadear: a negociacao acabou.
        "backendResponse": None,
        "status": "no_agreement",
    }


def _texto_da_retomada(
    resume_reason: Optional[str],
    client_feedback: str,
    previous_text: str,
    pedidos: Optional[List[Dict[str, Any]]] = None,
    veredictos: Optional[List[Dict[str, str]]] = None,
    refazendo: bool = False,
) -> str:
    """Mensagem que o ZelinhU manda a empresa ao retomar, ramificada pelo motivo.

    AUSENTE cai na recusa do cliente, e nao no texto neutro: ate a spec de 01/09
    esse era o unico motivo que existia, e o campo so passa a vir preenchido
    quando o backend das solicitacoes estiver no ar. Tratar ausencia como
    "motivo desconhecido" trocaria o texto de TODA retomada de hoje.

    DESCONHECIDO cai no neutro. Se um dia aparecer um motivo que este codigo nao
    conhece, dizer "o cliente recusou" seria inventar um fato.

    A RAJADA MISTA (contrato 20260911, §2.2 e §3.3)
    -----------------------------------------------
    O `resumeReason` e UM para a rajada inteira, e a regra do backend passou a
    ser: basta UMA atendida para ele vir `request_fulfilled`. O texto fixo
    "As solicitacoes pendentes foram atendidas" viraria mentira na rajada em que
    duas foram atendidas e uma recusada - e era a mesma mentira, ao contrario,
    que o defeito 3 do plano de 11/09 descreve do lado deles.

    Com `pedidos`, o texto e COMPOSTO do desfecho de cada um. Sem eles (backend
    anterior a Etapa 1), o texto e o de sempre.

    O VEREDITO ENTRA NO TEXTO (QA de 14/09/2026)
    --------------------------------------------
    "Atendido" para o backend e "chegou um arquivo". Com `veredictos`, o pedido
    cujo documento nao era o pedido sai como NAO ATENDIDO - e a empresa para de
    ler "ATENDIDO" e prometer "validar internamente". `refazendo` diz se o pedido
    ja saiu de novo; sem ele o texto nao promete o que nao aconteceu.
    """
    motivo = (resume_reason or "").strip() or RESUME_CLIENT_REJECTED
    pedidos = pedidos or []
    veredictos = veredictos or []

    conduta_invalidos = (
        "O que esta marcado NAO ATENDIDO nao foi entregue: nao o trate como "
        "recebido nem como documento a validar."
    )
    if refazendo:
        conduta_invalidos += " Ele volta a ser solicitado a quem deveria atende-lo."

    if pedidos and motivo in (RESUME_REQUEST_FULFILLED, RESUME_REQUEST_DENIED):
        atendidos, invalidos, recusados = [], [], []
        for pedido in pedidos:
            if pedido.get("status") == STATUS_PEDIDO_RECUSADO:
                recusados.append(pedido)
            elif pedido.get("status") == STATUS_PEDIDO_ATENDIDO:
                veredito = (veredito_do_pedido(pedido, veredictos) or {}).get("veredito")
                (invalidos if veredito == VEREDITO_INVALIDO else atendidos).append(pedido)

        if invalidos or (atendidos and recusados):
            partes = []
            if atendidos:
                partes.append(f"{len(atendidos)} atendida(s)")
            if invalidos:
                partes.append(f"{len(invalidos)} com arquivo que nao corresponde ao pedido")
            if recusados:
                partes.append(f"{len(recusados)} recusada(s)")
            juntas = ", ".join(partes[:-1]) + " e " + partes[-1] if len(partes) > 1 else partes[0]
            cabecalho = f"As solicitacoes pendentes foram respondidas: {juntas}."
        elif recusados:
            cabecalho = "As solicitacoes pendentes foram recusadas por quem deveria atende-las."
        else:
            cabecalho = "As solicitacoes pendentes foram atendidas."

        conduta = []
        if atendidos or not (invalidos or recusados):
            conduta.append("Considere o que chegou e siga a negociacao a partir dai.")
        if invalidos:
            conduta.append(conduta_invalidos)
        if recusados:
            conduta.append(
                "Sobre o que foi recusado, siga SEM o item - nao insista no mesmo pedido."
                if atendidos or invalidos else
                "Siga a negociacao SEM o que foi pedido - nao insista no mesmo pedido."
            )
        texto_da_conduta = " ".join(conduta)

        return f"""{cabecalho}
{_resumo_da_rajada(pedidos, veredictos)}

{texto_da_conduta} Os termos ja aceitos seguem valendo.
"""

    if motivo == RESUME_REQUEST_FULFILLED:
        invalidos = [v for v in veredictos if v.get("veredito") == VEREDITO_INVALIDO]
        cabecalho = (
            "As solicitacoes pendentes foram respondidas." if invalidos
            else "As solicitacoes pendentes foram atendidas."
        )

        # Sem `requests`, o clientFeedback do backend ("atendida, 1 arquivo
        # anexado") e tudo o que a empresa leria. A conferencia vem logo abaixo
        # dele, e desmente o que precisa ser desmentido.
        conferencia = ""
        if veredictos:
            conferencia = "\nConferencia dos documentos:" + "".join(
                "\n- {}: {}".format(
                    v.get("solicitacao"), _desfecho_para_a_empresa(v.get("veredito"), [])
                )
                for v in veredictos
            )

        conduta = "Considere o que chegou e siga a negociacao a partir dai."
        if invalidos:
            conduta = f"{conduta} {conduta_invalidos}"

        return f"""{cabecalho}
O que foi respondido: {client_feedback}{conferencia}

{conduta} Os termos ja aceitos seguem valendo.
"""

    if motivo == RESUME_REQUEST_DENIED:
        return f"""As solicitacoes pendentes foram recusadas por quem deveria atende-las.
Motivo informado: {client_feedback}

Siga a negociacao SEM o que foi pedido - nao insista no mesmo pedido. Os termos ja aceitos seguem valendo.
"""

    if motivo == RESUME_CLIENT_REJECTED:
        # O texto anterior pedia "evite repetir os termos recusados". No caso de
        # 24/08/2026 o cliente recusou justamente para PRESERVAR o que ja tinha
        # conquistado - a frase convidava a empresa a reabrir o que estava aceito,
        # o oposto do pedido. Quem diz o que cai e o que fica e o motivo da recusa.
        return f"""O cliente analisou a proposta anterior e recusou-a.
Motivo informado: {client_feedback}

{previous_text}

Considere o motivo acima e apresente nova proposta. Os termos ja aceitos seguem valendo, salvo naquilo que o motivo da recusa expressamente contrariar.
"""

    # request_expired e qualquer motivo futuro. Nao afirmamos nada sobre o que
    # aconteceu: so o que sabemos com certeza, que a negociacao voltou a andar.
    print(f"[SENDER-RESUME] resumeReason nao reconhecido: {motivo!r} - usando o texto neutro")
    return f"""A negociacao foi retomada.
Contexto informado: {client_feedback}

Siga a negociacao a partir daqui. Os termos ja aceitos seguem valendo.
"""


def _anexos_do_payload(request: Any) -> List[Any]:
    """
    Os anexos da retomada, venham eles pelo nome que vierem.

    O backend manda `files` - lista crua de URLs presigned (contrato 09/09).
    `attachments` e o formato em dict da spec de 01/09, que continua aceito por
    ser o unico que carrega texto ja extraido. Os dois entram na mesma lista;
    quem sabe ler cada formato e `ingerir_anexos`.

    Ate 09/09 so `attachments` era lido, e como o backend nunca mandou esse nome,
    NENHUM anexo jamais chegou ao ZelinhU: ele pedia o comprovante, o cliente
    anexava, e a negociacao seguia com o agente vendo apenas a contagem em texto.
    """
    return [*(getattr(request, "files", None) or []), *(request.attachments or [])]


async def _resume_negotiation(
    session_id: str,
    negotiation_id: Optional[str],
    client_feedback: str,
    rejection_count: Optional[int],
    previous_agreement: Optional[Dict[str, Any]],
    resume_reason: Optional[str] = None,
    ticket: Optional[Dict[str, Any]] = None,
    client: Optional[Dict[str, Any]] = None,
    company: Optional[Dict[str, Any]] = None,
    selected_rights: Optional[List[str]] = None,
    # URL em string (contrato 09/09) ou objeto (spec 01/09) - os dois convivem.
    attachments: Optional[List[Any]] = None,
    # Os pedidos da rajada, um a um (contrato 20260911, Etapa 1). Quando vem,
    # substitui a memoria da sessao como fonte do que foi pedido.
    requests: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Nucleo da retomada da negociacao.

    Tira a sessao do estado de espera, monta a mensagem do ZelinhU explicando
    POR QUE a negociacao voltou, e a despacha para o backend.

    O `resume_reason` (spec 20260901, secao 4.1) decide o texto. Ate 01/09 so
    existia um motivo - a recusa do cliente - e o texto dizia isso fixo. Com as
    solicitacoes, a mesma action volta tambem quando um documento foi entregue
    ou negado, e o texto antigo faria o ZelinhU anunciar a empresa uma recusa que
    nao houve, convidando-a a reabrir o que ninguem contestou.

    Os blocos `ticket`/`client`/`company`/`selected_rights` (secao 4.2) sao o
    contexto vindo no proprio payload. Quando eles chegam, a retomada nao depende
    mais de `_get_session_context` - o endpoint que deu 404 em 25/08 e deixou o
    ZelinhU negociando no escuro contra uma empresa que tinha o valor da causa na
    mao. Quando NAO chegam - que e o payload de hoje, ate o backend deployar -, o
    caminho e o de sempre, sem nenhuma diferenca.
    """
    contexto_do_payload = _contexto_da_retomada(ticket, client, company, selected_rights)

    session = _sender_sessions.get(session_id)
    if not session:
        if contexto_do_payload:
            print(
                f"[SENDER-RESUME] Sessao inexistente - contexto veio no proprio payload "
                f"da retomada (valor da causa: {contexto_do_payload['estimated_value']}), "
                f"sem consultar o backend"
            )
            context = contexto_do_payload
        else:
            print(f"[SENDER-RESUME] Sessao inexistente - buscando contexto e criando sessao")
            context = await _get_session_context(session_id)
        session = {
            "negotiationId": negotiation_id,
            "status": "negotiating",
            "currentRound": rejection_count or 1,
            "messages": [],
            "context": context or {},
            "createdAt": datetime.now().isoformat()
        }
        _sender_sessions[session_id] = session
    elif contexto_do_payload:
        # Sessao viva mas sem contexto e o cenario exato de 25/08: a busca falhou
        # na criacao e a sessao seguiu vazia. absorb_session_context so preenche
        # quando esta vazio - contexto que ja funciona continua sendo a fonte.
        absorb_session_context(session_id, contexto_do_payload, origem="payload da retomada")

    # OS ANEXOS DA SOLICITACAO ATENDIDA
    # ---------------------------------
    # O texto extraido vai para o ESTADO do agente - o que ele usa para pensar -
    # e nunca para o corpo da mensagem: essa mensagem vai para a EMPRESA, que e a
    # outra parte da negociacao. Documento do cliente (RG, comprovante, holerite)
    # nao se despeja no colo do adversario.
    #
    # E O VEREDITO (spec 01/09, §5.1: a correcao e nossa)
    # ---------------------------------------------------
    # Antes de dar a solicitacao por atendida, confere se o que chegou e o que
    # foi pedido. Roda ANTES de _clear_awaiting_request, que apaga a memoria do
    # que foi pedido.
    #
    # DOIS CAMINHOS, e o novo e o que vale quando existe:
    #
    #   com `requests` (contrato 20260911) -> cada pedido e lido e julgado contra
    #       os arquivos DELE, com o que foi pedido vindo do payload. Nao depende
    #       da memoria do processo, entao um restart no meio da espera - que pode
    #       durar dias - deixa de apagar o que o ZelinhU pediu.
    #
    #   sem `requests` -> o caminho de sempre: lista plana de arquivos contra o
    #       que a sessao lembra ter pedido. Continua aqui porque o backend pode
    #       subir depois de nos, e ninguem espera ninguem.
    pedidos_da_rajada = _pedidos_da_rajada(requests)
    veredictos: List[Dict[str, str]] = []

    # Os pedidos contra os quais o que chegou foi conferido - do payload ou da
    # memoria. Sao eles que o documento invalido faz sair de novo, mais abaixo.
    pedidos_conferidos: List[Dict[str, Any]] = pedidos_da_rajada

    if pedidos_da_rajada:
        print(
            f"[SENDER-RESUME] Rajada com {len(pedidos_da_rajada)} pedido(s) no payload - "
            f"cada um julgado contra os arquivos dele"
        )
        documentos, veredictos = await _conferir_a_rajada(
            pedidos_da_rajada, llm=_get_negotiator().llm
        )
    else:
        documentos = await ingerir_anexos(attachments)
        pendentes = session.get("solicitacoesPendentes") or []
        pedidos_conferidos = pendentes
        if documentos and pendentes:
            veredictos = await julgar_documentos(
                pendentes, documentos, llm=_get_negotiator().llm
            )

    if documentos:
        acumulados = session.setdefault("documentosRecebidos", [])
        acumulados.extend(documentos)
        del acumulados[:-MAX_DOCUMENTOS_NA_SESSAO]

    if veredictos:
        session["veredictosDocumentos"] = veredictos
        invalidos = [v for v in veredictos if v.get("veredito") == VEREDITO_INVALIDO]
        if invalidos:
            print(
                f"[SENDER-RESUME] {len(invalidos)} solicitacao(oes) NAO atendida(s) "
                f"pelo que chegou - o ZelinhU nao vai trata-las como recebidas"
            )

    # A decisao do cliente chegou: a negociacao volta a andar (QA-04).
    _clear_awaiting_client_decision(session)
    # Se a pausa era por solicitacao, ela acabou tambem: o backend so manda o
    # resume quando a ULTIMA pendencia da rajada foi resolvida (spec, secao 3.1).
    # A baixa e o que tira o item da lista - na lista em texto de 08/09 nada
    # saia nunca, e o fechamento de 10/09 listou um item ja resolvido.
    baixadas = dar_baixa(
        session,
        recusadas=(resume_reason or "").strip() == RESUME_REQUEST_DENIED,
        # Numa rajada mista o motivo unico nao diz o desfecho de cada pedido; o
        # proprio pedido diz (contrato 20260911, §2.2).
        status_por_pedido=_status_por_pedido(pedidos_da_rajada),
        # E o arquivo que chegou pode nao ser o que foi pedido (QA de 14/09).
        veredictos=veredictos,
    )
    if baixadas:
        print(
            f"[SOLICITACOES] resume motivo={resume_reason} status={baixadas[0]['status']} "
            f"baixa={[b.get('requestId') or b.get('title') for b in baixadas]}"
        )

    # Depois da baixa de proposito: a tentativa desta retomada ja conta no teto.
    a_refazer = _pedidos_a_refazer(pedidos_conferidos, veredictos, session)
    _clear_awaiting_request(session)
    session["status"] = "negotiating"
    current_round = session.get("currentRound", 1)
    if rejection_count is not None:
        current_round = max(current_round, rejection_count)
    next_round = _rodada_da_retomada(resume_reason, current_round)
    session["currentRound"] = next_round

    # Recusa do cliente (motivo client_rejected ou ausente, o payload antigo):
    # a negociacao volta de verdade e precisa de rodadas para isso. Retomada por
    # pedido nao estende - ela nem gasta rodada.
    if (resume_reason or "").strip() not in _RETOMADAS_DE_PEDIDO:
        recusas = _contar_recusa_do_cliente(session, rejection_count)
        if recusas >= get_settings().NEGOTIATION_MAX_CLIENT_REJECTIONS:
            return await _encerrar_para_o_cliente(
                session_id=session_id,
                negotiation_id=negotiation_id,
                session=session,
                round_number=next_round,
                recusas=recusas,
            )
        teto = _estender_teto_apos_recusa(session, current_round)
        # Depois da recusa do cliente, a "ultima proposta" da empresa volta a ser
        # contestada antes de ir de novo a ele.
        ZelinhuNegotiator.reset_after_client_rejection(session)
        print(
            f"[SENDER-RESUME] Recusa do cliente na rodada {current_round} - teto da "
            f"sessao passa a {teto} rodadas"
        )

    previous_agreement = previous_agreement or {}
    previous_terms = previous_agreement.get("terms") or ""
    previous_value = previous_agreement.get("value")
    previous_summary = []
    if previous_value is not None:
        try:
            previous_summary.append(f"Valor: R$ {float(previous_value):,.2f}")
        except Exception:
            previous_summary.append(f"Valor: {previous_value}")
    if previous_terms:
        previous_summary.append(previous_terms)

    previous_text = "".join([
        "O cliente rejeitou a proposta anterior.",
        f" Termos anteriores: {'; '.join(previous_summary)}." if previous_summary else ""
    ])

    message_body = _texto_da_retomada(
        resume_reason, client_feedback, previous_text, pedidos_da_rajada,
        veredictos=veredictos, refazendo=bool(a_refazer),
    )

    zelinhu_message_id = f"msg_zelinhu_{uuid.uuid4().hex[:12]}"
    session["messages"].append({
        "id": zelinhu_message_id,
        "origin": "zelinhu",
        "body": message_body,
        "action": RESUME_ACTION,
        "createdAt": datetime.now().isoformat()
    })

    # O DOCUMENTO ERRADO VOLTA A SER PEDIDO (QA de 14/09/2026)
    # --------------------------------------------------------
    # A retomada sai como a propria rajada: o texto vai no primeiro pedido, e a
    # negociacao volta a esperar - sem turno da empresa no meio, porque o que ela
    # pediu ainda nao chegou. So o proximo resume a destrava.
    if a_refazer:
        aceitas = await _dispatch_requests(
            session_id=session_id,
            negotiation_id=negotiation_id,
            message_body=message_body,
            round_number=next_round,
            pedidos=a_refazer,
            session=session,
        )
        if aceitas:
            _mark_awaiting_request(session, aceitas)
            print(
                f"[SENDER-RESUME] Retomada concluida - round={next_round}, "
                f"{len(aceitas)} pedido(s) refeito(s) por documento invalido"
            )
            return {
                "success": True,
                "messageBody": message_body,
                "round": next_round,
                "zelinhuMessageId": zelinhu_message_id,
                # Sem resposta da empresa para encadear: ela nao e chamada.
                "backendResponse": None,
            }

        # Nenhum pedido saiu (400 nosso, lotacao, rede - ja gritou no log). O
        # texto nao pode anunciar um pedido que nao existe: segue a retomada
        # comum, sem essa frase.
        message_body = _texto_da_retomada(
            resume_reason, client_feedback, previous_text, pedidos_da_rajada,
            veredictos=veredictos, refazendo=False,
        )
        session["messages"][-1]["body"] = message_body

    # Sem session/dispatch_key de proposito: a recusa do cliente sempre reabre a
    # negociacao, mesmo que o motivo informado seja igual ao de uma recusa anterior.
    # A protecao contra reentrega da MESMA recusa fica em _resume_already_handled.
    backend_response = await _send_zelinhu_message_to_backend(
        session_id=session_id,
        negotiation_id=negotiation_id,
        message_body=message_body,
        round_number=next_round,
        negotiation_complete=False
    )

    success = backend_response is not None and backend_response.get("success", True)
    print(
        f"[SENDER-RESUME] Retomada concluida - round={next_round}, "
        f"despacho={'ok' if success else 'FALHOU'}"
    )
    return {
        "success": success,
        "messageBody": message_body,
        "round": next_round,
        "zelinhuMessageId": zelinhu_message_id,
        "backendResponse": backend_response,
    }


# =============================================================================
# ENDPOINT: POST /api/webhooks/sender/status
# =============================================================================

@router.post(
    "/status",
    response_model=WebhookSenderStatusResponse,
    summary="Receber status da negociacao",
    description="""
    Webhook chamado pelo Sender para notificar mudancas de status na negociacao.

    Status validos:
    - started: Negociacao iniciada
    - negotiating: Em andamento
    - agreement: Acordo alcancado
    - no_agreement: Sem acordo
    - timeout: Prazo expirado
    - escalated: Escalado para judicial
    - cancelled: Cancelado
    """
)
async def webhook_sender_status(request: WebhookSenderStatusRequest):
    """
    Processa notificacao de status da negociacao.
    """
    print(f"\n[SENDER-STATUS] ========== STATUS RECEBIDO ==========")
    print(f"[SENDER-STATUS] sessionId: {request.sessionId}")
    print(f"[SENDER-STATUS] negotiationId: {request.negotiationId}")
    print(f"[SENDER-STATUS] status: {request.status}")
    print(f"[SENDER-STATUS] finalRound: {request.finalRound}")
    print(f"[SENDER-STATUS] reason: {request.reason}")
    print(f"[SENDER-STATUS] agreementDetails: {request.agreementDetails}")

    try:
        # Validar campos obrigatorios
        if not request.sessionId:
            print(f"[SENDER-STATUS] Erro: sessionId is required")
            raise HTTPException(status_code=400, detail="sessionId is required")

        if not request.status:
            print(f"[SENDER-STATUS] Erro: status is required")
            raise HTTPException(status_code=400, detail="status is required")

        # Validar status
        if request.status not in VALID_STATUSES:
            print(f"[SENDER-STATUS] Erro: Invalid status '{request.status}'")
            raise HTTPException(
                status_code=400,
                detail=f"Invalid status. Must be one of: {', '.join(VALID_STATUSES)}"
            )

        # Obter ou criar sessao
        if request.sessionId not in _sender_sessions:
            _sender_sessions[request.sessionId] = {
                "negotiationId": request.negotiationId,
                "status": "started",
                "currentRound": 0,
                "messages": [],
                "createdAt": datetime.now().isoformat()
            }

        session = _sender_sessions[request.sessionId]
        previous_status = session.get("status")

        # Atualizar status
        session["status"] = request.status

        # Se o backend reabriu a negociacao, a espera pela decisao do cliente
        # acabou. Nos demais status a flag e mantida: "agreement" vindo do
        # backend continua significando proposta submetida, nao decidida.
        if request.status in ("negotiating", "started"):
            _clear_awaiting_client_decision(session)

        if request.finalRound is not None:
            session["finalRound"] = request.finalRound

        if request.reason:
            session["reason"] = request.reason

        if request.agreementDetails:
            session["agreementDetails"] = request.agreementDetails

        if request.error:
            session["error"] = request.error

        # Verificar se e status terminal
        is_terminal = request.status in TERMINAL_STATUSES

        if is_terminal:
            session["endedAt"] = datetime.now().isoformat()
            print(f"[SENDER-STATUS] Negociacao encerrada com status terminal: {request.status}")

        # Montar resposta
        response = WebhookSenderStatusResponse(
            success=True,
            sessionId=request.sessionId,
            previousStatus=previous_status,
            newStatus=request.status,
            isTerminal=is_terminal
        )

        print(f"[SENDER-STATUS] ========== STATUS ATUALIZADO ==========")
        print(f"[SENDER-STATUS] previousStatus: {previous_status}")
        print(f"[SENDER-STATUS] newStatus: {request.status}")
        print(f"[SENDER-STATUS] isTerminal: {is_terminal}")

        return response

    except HTTPException:
        raise
    except Exception as e:
        print(f"[SENDER-STATUS] Erro inesperado: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


# =============================================================================
# ENDPOINT: GET /api/webhooks/sender/session/{session_id}
# =============================================================================

@router.get(
    "/session/{session_id}",
    summary="Consultar status da sessao",
    description="Retorna informacoes sobre uma sessao de negociacao."
)
async def get_session_status(session_id: str):
    """
    Retorna o status atual de uma sessao de negociacao.
    """
    if session_id not in _sender_sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    session = _sender_sessions[session_id]

    return {
        "sessionId": session_id,
        "negotiationId": session.get("negotiationId"),
        "status": session.get("status"),
        "currentRound": session.get("currentRound", 0),
        "messageCount": len(session.get("messages", [])),
        "createdAt": session.get("createdAt"),
        "endedAt": session.get("endedAt"),
        "context": {
            "clientName": session.get("context", {}).get("client", {}).get("name"),
            "companyName": session.get("context", {}).get("company", {}).get("name")
        }
    }
