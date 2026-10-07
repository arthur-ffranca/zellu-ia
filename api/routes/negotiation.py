# -*- coding: utf-8 -*-
"""
Rotas de Webhook para o Agente Sender (ZelinhU).

Este serviço recebe chamadas do Backend Zellu e gerencia negociações automáticas.

Endpoints recebidos do Zellu Backend:
- POST /negotiation/start - Iniciar negociação
- POST /negotiation/company-response - Receber resposta da empresa (quando Zellu envia diretamente)
- POST /negotiation/cancel - Cancelar negociação
- GET /negotiation/{negotiation_id} - Consultar status

Webhooks chamados no Zellu Backend:
- POST /api/webhooks/sender/message - Enviar mensagens do ZelinhU
- POST /api/webhooks/sender/status - Notificar mudanças de status
"""

from fastapi import APIRouter, HTTPException, Header, BackgroundTasks, Depends
from fastapi.responses import JSONResponse
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta
from uuid import uuid4
import asyncio

from api.schemas_sender import (
    StartNegotiationRequest,
    StartNegotiationResponse,
    CompanyResponseRequest,
    CompanyResponseAck,
    NegotiationStatusResponse,
    NegotiationStatus,
    NegotiationEventType,
    ErrorResponse,
    ErrorDetail,
    NegotiationHistoryEntry,
    NegotiationDeadline
)
from src.clients.api_client import ZelluAPIClient
from config import get_settings
# Mensagens do ZelinhU (modulo centralizado)
from src.utils.zelinhu_messages import create_zelinhu_initial_message, create_zelinhu_counter_message

# Router
router = APIRouter(prefix="/negotiation", tags=["Sender Agent - Negotiation"])

# Storage em memória para negociações (em produção: usar banco de dados)
_negotiations: Dict[str, Dict[str, Any]] = {}
_negotiation_locks: Dict[str, asyncio.Lock] = {}
# Indice sessionId -> negotiation_id (chave canonica de idempotencia, contrato 20260626).
# O cron de reconciliacao do Zellu pode reenviar start_negotiation com o mesmo
# sessionId; usamos este mapa para responder de forma idempotente (no-op).
_sessions_to_negotiation: Dict[str, str] = {}


def _resolve_session_id(request: "StartNegotiationRequest") -> str:
    """Resolve o sessionId canonico da negociacao.

    Prioridade (contrato 20260626 §4): sessionId do payload > ultimo segmento do
    callback_url (compat legada) > ticket.id (ultimo recurso).
    """
    sid = getattr(request, "session_id", None)
    if sid:
        return str(sid)
    callback_url = (request.config.callback_url or "").rstrip("/")
    if "/" in callback_url:
        derived = callback_url.split("/")[-1]
        if derived:
            return derived
    return request.ticket.id


def get_negotiation_lock(negotiation_id: str) -> asyncio.Lock:
    """Obtém lock para uma negociação específica."""
    if negotiation_id not in _negotiation_locks:
        _negotiation_locks[negotiation_id] = asyncio.Lock()
    return _negotiation_locks[negotiation_id]


def get_api_client() -> ZelluAPIClient:
    """Retorna instância do cliente API do Zellu."""
    settings = get_settings()
    return ZelluAPIClient(
        base_url=settings.ZELLU_BACKEND_URL,
        api_key=settings.ZELLU_API_KEY or settings.API_KEY_ZELLU_IA
    )


# ===== DEPENDÊNCIAS =====

async def verify_api_key(
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key")
) -> bool:
    """
    Verifica API Key da requisição.

    Em produção, validar contra lista de API Keys autorizadas.
    Por enquanto, aceita qualquer requisição (desenvolvimento).
    """
    # TODO: Implementar validação real de API Key
    return True


# ===== HELPERS PARA CRIAR MENSAGENS DO ZELINHU =====
# NOTA: Funcoes de criacao de mensagens foram movidas para
# src/utils/zelinhu_messages.py (modulo centralizado)
# Importado como: create_zelinhu_initial_message, create_zelinhu_counter_message


def _analyze_company_response(
    response_text: str,
    negotiation_data: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Analisa a resposta da empresa para determinar próximos passos.

    Returns:
        Dict com:
        - agreement_reached: bool - Se houve acordo
        - should_counter: bool - Se deve fazer contraproposta
        - should_escalate: bool - Se deve escalar
        - counter_proposal: str - Contraproposta (se houver)
        - agreement_details: dict - Detalhes do acordo (se houver)
    """
    response_lower = response_text.lower()

    # Palavras que indicam acordo
    agreement_keywords = [
        "aceito", "concordo", "acordo", "aprovado",
        "pode ser", "fechado", "combinado", "ok",
        "vamos fazer", "autorizo", "confirmo"
    ]

    # Palavras que indicam recusa total
    refusal_keywords = [
        "não podemos", "impossível", "não é possível",
        "recusamos", "negamos", "não aceitamos",
        "não há como", "inviável"
    ]

    # Verificar se a empresa aceitou
    for keyword in agreement_keywords:
        if keyword in response_lower:
            # Tentar extrair detalhes do acordo
            agreement_details = _extract_agreement_details(response_text)
            return {
                "agreement_reached": True,
                "should_counter": False,
                "should_escalate": False,
                "counter_proposal": None,
                "agreement_details": agreement_details
            }

    # Verificar se a empresa recusou totalmente
    for keyword in refusal_keywords:
        if keyword in response_lower:
            current_round = negotiation_data.get("current_round", 1)
            max_rounds = negotiation_data.get("max_rounds") or get_settings().NEGOTIATION_MAX_ROUNDS

            # Se ainda há rodadas, fazer contraproposta
            if current_round < max_rounds:
                return {
                    "agreement_reached": False,
                    "should_counter": True,
                    "should_escalate": False,
                    "counter_proposal": _generate_counter_proposal(negotiation_data, response_text),
                    "agreement_details": None
                }
            else:
                # Sem mais rodadas, escalar
                return {
                    "agreement_reached": False,
                    "should_counter": False,
                    "should_escalate": True,
                    "counter_proposal": None,
                    "agreement_details": None
                }

    # Resposta neutra - continuar negociando
    return {
        "agreement_reached": False,
        "should_counter": True,
        "should_escalate": False,
        "counter_proposal": _generate_counter_proposal(negotiation_data, response_text),
        "agreement_details": None
    }


def _extract_agreement_details(response_text: str) -> Dict[str, Any]:
    """Extrai detalhes do acordo da resposta."""
    # Implementação básica - em produção, usar NLP mais sofisticado
    import re

    details = {
        "value": None,
        "terms": response_text[:500],  # Primeiros 500 chars como resumo
        "deadline": None
    }

    # Tentar extrair valor monetário
    value_match = re.search(r'R\$\s*([\d.,]+)', response_text)
    if value_match:
        value_str = value_match.group(1).replace('.', '').replace(',', '.')
        try:
            details["value"] = float(value_str)
        except ValueError:
            pass

    # Tentar extrair prazo
    date_patterns = [
        r'até\s*(\d{1,2}/\d{1,2}/\d{4})',
        r'prazo\s*de\s*(\d+)\s*dias?',
        r'em\s*até\s*(\d+)\s*dias?'
    ]
    for pattern in date_patterns:
        match = re.search(pattern, response_text, re.IGNORECASE)
        if match:
            details["deadline"] = match.group(1)
            break

    return details


def _generate_counter_proposal(
    negotiation_data: Dict[str, Any],
    company_response: str
) -> str:
    """Gera contraproposta baseada no contexto da negociacao.

    Usa o modulo centralizado create_zelinhu_counter_message.
    """
    client_name = negotiation_data.get("client", {}).get("name", "cliente")
    current_round = negotiation_data.get("current_round", 1)
    estimated_value = negotiation_data.get("ticket", {}).get("estimated_value", 0)
    max_rounds = negotiation_data.get("max_rounds") or get_settings().NEGOTIATION_MAX_ROUNDS

    # Usar modulo centralizado
    return create_zelinhu_counter_message(
        client_name=client_name,
        current_round=current_round,
        estimated_value=estimated_value,
        max_rounds=max_rounds
    )


# ===== ENDPOINTS =====

@router.post(
    "/start",
    response_model=StartNegotiationResponse,
    responses={
        202: {"description": "Negociação iniciada com sucesso"},
        400: {"description": "Payload inválido"},
        409: {"description": "Negociação já existe para este ticket"},
        422: {"description": "Empresa sem IA habilitada"}
    },
    summary="Iniciar Negociação",
    description="Recebe dados do Backend Zellu e inicia negociação com a empresa."
)
async def start_negotiation(
    request: StartNegotiationRequest,
    background_tasks: BackgroundTasks,
    x_request_id: Optional[str] = Header(None, alias="X-Request-ID"),
    _auth: bool = Depends(verify_api_key)
):
    """
    Inicia uma nova negociação.

    Chamado pelo Backend Zellu quando um ticket é finalizado
    e o cliente escolhe uma solução (amigável ou extrajudicial).

    Fluxo:
    1. Valida dados recebidos
    2. Cria registro da negociação
    3. Gera mensagem inicial do ZelinhU
    4. Envia mensagem via webhook /api/webhooks/sender/message
    5. Recebe resposta da IA da empresa
    6. Processa resposta em background
    """
    print(f"\n[SENDER] ========== INICIANDO NEGOCIAÇÃO ==========")
    print(f"[SENDER] ticket_id={request.ticket.id}")
    print(f"[SENDER] client={request.client.name}")
    print(f"[SENDER] company={request.company.name}")

    # Resolver sessionId (chave canonica de idempotencia, contrato 20260626 §3.1/§4)
    session_id = _resolve_session_id(request)

    # IDEMPOTENCIA POR sessionId: o cron de reconciliacao do Zellu pode reenviar
    # start_negotiation com o MESMO sessionId quando nao recebe callback. Reenvio
    # vira no-op: retorna o estado da negociacao existente, sem duplicar/reiniciar.
    existing_neg_id = _sessions_to_negotiation.get(session_id)
    if existing_neg_id and existing_neg_id in _negotiations:
        existing = _negotiations[existing_neg_id]
        print(f"[SENDER] start_negotiation idempotente - sessionId={session_id} ja em negociacao (neg_id={existing_neg_id}, status={existing.get('status')})")
        return StartNegotiationResponse(
            success=True,
            negotiation_id=existing_neg_id,
            status=NegotiationStatus(existing.get("status") or NegotiationStatus.STARTED),
            message="Negociacao ja existente para este sessionId (no-op idempotente)"
        )

    # Idempotencia adicional por X-Request-ID (compat)
    if x_request_id:
        existing = _negotiations.get(f"req_{x_request_id}")
        if existing:
            print(f"[SENDER] Request duplicada ignorada - x_request_id={x_request_id}")
            return StartNegotiationResponse(
                success=True,
                negotiation_id=existing.get("negotiation_id"),
                status=NegotiationStatus.STARTED,
                message="Negociação já iniciada (request duplicada)"
            )

    # Verificar se empresa tem IA habilitada
    if not request.company.ai_enabled:
        print(f"[SENDER] Empresa sem IA habilitada - company_id={request.company.id}")
        return JSONResponse(
            status_code=422,
            content=ErrorResponse(
                success=False,
                error=ErrorDetail(
                    code="COMPANY_AI_DISABLED",
                    message="A empresa não possui IA habilitada para negociação automática",
                    recommendation="Encaminhar para atendimento humano"
                )
            ).model_dump()
        )

    # Idempotencia por ticket: reenvio do mesmo ticket -> no-op (retorna existente).
    # Antes retornava 409, que o cron de reconciliacao poderia interpretar como falha.
    for neg_id, neg_data in _negotiations.items():
        if neg_data.get("ticket_id") == request.ticket.id and not neg_id.startswith("req_"):
            print(f"[SENDER] start_negotiation idempotente - ticket {request.ticket.id} ja em negociacao (neg_id={neg_id})")
            return StartNegotiationResponse(
                success=True,
                negotiation_id=neg_id,
                status=NegotiationStatus(neg_data.get("status") or NegotiationStatus.STARTED),
                message="Negociacao ja existente para este ticket (no-op idempotente)"
            )

    # Gerar ID da negociação
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    negotiation_id = f"neg_{timestamp}_{request.ticket.id[:8]}"
    # session_id ja resolvido acima (chave canonica)

    # Criar registro da negociação
    negotiation_data = {
        "negotiation_id": negotiation_id,
        "ticket_id": request.ticket.id,
        "session_id": session_id,
        "ticket": request.ticket.model_dump(),
        "client": request.client.model_dump(),
        "company": request.company.model_dump(),
        "analysis": request.analysis.model_dump() if request.analysis else None,
        "documents": request.documents.model_dump() if request.documents else None,
        "config": request.config.model_dump(),
        "status": NegotiationStatus.STARTED,
        "current_round": 0,
        "max_rounds": request.config.max_negotiation_rounds,
        "created_at": datetime.now().isoformat(),
        "last_activity_at": datetime.now().isoformat(),
        "history": [],
        "last_sender_message": None,
        "last_company_response": None,
        "error": None
    }

    _negotiations[negotiation_id] = negotiation_data
    # Registrar sessionId -> negotiation_id (chave de idempotencia, contrato 20260626)
    _sessions_to_negotiation[session_id] = negotiation_id

    # Marcar request ID como processado (para idempotência)
    if x_request_id:
        _negotiations[f"req_{x_request_id}"] = {"negotiation_id": negotiation_id}

    print(f"[SENDER] Negociação criada - negotiation_id={negotiation_id}")

    # Processar em background (enviar mensagem inicial)
    background_tasks.add_task(
        process_negotiation_start,
        negotiation_id,
        negotiation_data
    )

    return StartNegotiationResponse(
        success=True,
        negotiation_id=negotiation_id,
        status=NegotiationStatus.STARTED,
        message="Negociação iniciada com sucesso",
        estimated_first_response=datetime.now() + timedelta(minutes=2)
    )


@router.post(
    "/company-response",
    response_model=CompanyResponseAck,
    responses={
        200: {"description": "Resposta processada"},
        404: {"description": "Negociação não encontrada"},
        410: {"description": "Negociação já encerrada"}
    },
    summary="Receber Resposta da Empresa",
    description="Recebe resposta da IA da empresa para processar."
)
async def receive_company_response(
    request: CompanyResponseRequest,
    background_tasks: BackgroundTasks,
    _auth: bool = Depends(verify_api_key)
):
    """
    Recebe e processa resposta da empresa.

    NOTA: Este endpoint é um fallback. O fluxo principal recebe a resposta
    diretamente do webhook /api/webhooks/sender/message que já retorna
    a resposta da IA da empresa.
    """
    print(f"\n[SENDER] Recebido company_response - negotiation_id={request.negotiation_id}")

    # Buscar negociação
    negotiation_data = _negotiations.get(request.negotiation_id)

    if not negotiation_data:
        print(f"[SENDER] Negociação não encontrada - negotiation_id={request.negotiation_id}")
        raise HTTPException(
            status_code=404,
            detail=f"Negociação {request.negotiation_id} não encontrada"
        )

    # Verificar se negociação ainda está ativa
    status = negotiation_data.get("status")
    terminal_statuses = [
        NegotiationStatus.AGREEMENT,
        NegotiationStatus.NO_AGREEMENT,
        NegotiationStatus.TIMEOUT,
        NegotiationStatus.CANCELLED
    ]
    if status in terminal_statuses:
        print(f"[SENDER] Negociação já encerrada - status={status}")
        raise HTTPException(
            status_code=410,
            detail=f"Negociação já encerrada com status: {status}"
        )

    # Atualizar estado com resposta da empresa
    negotiation_data["last_company_response"] = {
        "content": request.response.content,
        "metadata": request.response.metadata,
        "received_at": datetime.now().isoformat()
    }
    negotiation_data["last_activity_at"] = datetime.now().isoformat()

    # Processar resposta em background
    background_tasks.add_task(
        process_company_response,
        request.negotiation_id,
        negotiation_data,
        request.response.content
    )

    return CompanyResponseAck(
        success=True,
        message="Resposta recebida e processada",
        next_action="analyzing"
    )


@router.post(
    "/cancel",
    responses={
        200: {"description": "Negociação cancelada"},
        404: {"description": "Negociação não encontrada"}
    },
    summary="Cancelar Negociação",
    description="Cancela uma negociação ativa."
)
async def cancel_negotiation(
    negotiation_id: str,
    reason: Optional[str] = "Cancelado pelo usuário",
    _auth: bool = Depends(verify_api_key)
):
    """
    Cancela uma negociação ativa.

    Chamado pelo Backend Zellu quando o usuário ou sistema cancela a negociação.
    """
    print(f"\n[SENDER] Cancelando negociação - negotiation_id={negotiation_id}")

    negotiation_data = _negotiations.get(negotiation_id)

    if not negotiation_data:
        raise HTTPException(
            status_code=404,
            detail=f"Negociação {negotiation_id} não encontrada"
        )

    # Atualizar status local
    negotiation_data["status"] = NegotiationStatus.CANCELLED
    negotiation_data["ended_at"] = datetime.now().isoformat()
    negotiation_data["cancel_reason"] = reason

    # Notificar Zellu Backend
    api_client = get_api_client()
    session_id = negotiation_data.get("session_id")

    if session_id:
        await api_client.send_negotiation_status(
            session_id=session_id,
            status="cancelled",
            negotiation_id=negotiation_id,
            final_round=negotiation_data.get("current_round", 0),
            reason=reason
        )

    print(f"[SENDER] Negociação cancelada com sucesso")

    return {
        "success": True,
        "negotiation_id": negotiation_id,
        "status": "cancelled",
        "reason": reason
    }


@router.get(
    "/{negotiation_id}",
    response_model=NegotiationStatusResponse,
    responses={
        200: {"description": "Status da negociação"},
        404: {"description": "Negociação não encontrada"}
    },
    summary="Consultar Status da Negociação",
    description="Retorna status atual e histórico da negociação."
)
async def get_negotiation_status(
    negotiation_id: str,
    _auth: bool = Depends(verify_api_key)
):
    """
    Consulta status de uma negociação.
    """
    negotiation_data = _negotiations.get(negotiation_id)

    if not negotiation_data:
        raise HTTPException(
            status_code=404,
            detail=f"Negociação {negotiation_id} não encontrada"
        )

    # Converter histórico para formato de resposta
    history = [
        NegotiationHistoryEntry(
            round=entry.get("round", 1),
            sender=entry.get("sender", "unknown"),
            summary=entry.get("summary", ""),
            timestamp=datetime.fromisoformat(entry.get("timestamp", datetime.now().isoformat()))
        )
        for entry in negotiation_data.get("history", [])
    ]

    return NegotiationStatusResponse(
        negotiation_id=negotiation_id,
        ticket_id=negotiation_data.get("ticket_id", ""),
        status=NegotiationStatus(negotiation_data.get("status", "started")),
        current_round=negotiation_data.get("current_round", 0),
        created_at=datetime.fromisoformat(negotiation_data.get("created_at", datetime.now().isoformat())),
        last_activity_at=datetime.fromisoformat(negotiation_data.get("last_activity_at", datetime.now().isoformat())) if negotiation_data.get("last_activity_at") else None,
        deadlines={},
        history=history
    )


# ===== FUNÇÕES DE PROCESSAMENTO EM BACKGROUND =====

async def process_negotiation_start(negotiation_id: str, negotiation_data: Dict[str, Any]):
    """
    Processa início da negociação em background.

    Fluxo conforme documentação:
    1. Cria mensagem inicial do ZelinhU
    2. Envia via POST /api/webhooks/sender/message
    3. Recebe resposta da IA da empresa no mesmo request
    4. Analisa resposta e decide próximos passos
    """
    print(f"\n[SENDER] ========== PROCESSANDO INÍCIO ==========")
    print(f"[SENDER] negotiation_id={negotiation_id}")

    try:
        api_client = get_api_client()

        # Extrair dados
        ticket = negotiation_data.get("ticket", {})
        client = negotiation_data.get("client", {})
        session_id = negotiation_data.get("session_id")

        if not session_id:
            print(f"[SENDER] ERRO: session_id não encontrado")
            negotiation_data["error"] = "session_id não encontrado"
            return

        # 1. Criar mensagem inicial do ZelinhU (modulo centralizado)
        zelinhu_message = create_zelinhu_initial_message(
            client_name=client.get("name", "Cliente"),
            ticket_title=ticket.get("title", ""),
            ticket_description=ticket.get("description", ""),
            ticket_category=ticket.get("category"),
            estimated_value=ticket.get("estimated_value"),
            solution_type=ticket.get("solution_type", "amigavel")
        )

        print(f"[SENDER] Mensagem inicial do ZelinhU criada ({len(zelinhu_message)} chars)")

        # Atualizar negociação
        negotiation_data["current_round"] = 1
        negotiation_data["status"] = NegotiationStatus.NEGOTIATING
        negotiation_data["last_sender_message"] = zelinhu_message
        negotiation_data["last_activity_at"] = datetime.now().isoformat()

        # Adicionar ao histórico
        negotiation_data["history"].append({
            "round": 1,
            "sender": "zelinhu",
            "summary": zelinhu_message[:200] + "..." if len(zelinhu_message) > 200 else zelinhu_message,
            "timestamp": datetime.now().isoformat()
        })

        # 2. Enviar mensagem via webhook do Zellu Backend
        print(f"[SENDER] Enviando mensagem via /api/webhooks/sender/message...")

        response = await api_client.send_zelinhu_message(
            session_id=session_id,
            message_body=zelinhu_message,
            round=1,
            negotiation_id=negotiation_id
        )

        if not response.get("success", True):
            error = response.get("error", "Erro desconhecido")
            print(f"[SENDER] ERRO ao enviar mensagem: {error}")
            negotiation_data["error"] = error

            # Se o erro for retryable, podemos tentar novamente
            if response.get("retryable"):
                print(f"[SENDER] Erro é retryable, aguardando para tentar novamente...")
                await asyncio.sleep(5)
                response = await api_client.send_zelinhu_message(
                    session_id=session_id,
                    message_body=zelinhu_message,
                    round=1,
                    negotiation_id=negotiation_id
                )

            if not response.get("success", True):
                return

        # 3. Processar resposta da IA da empresa
        company_response = response.get("response", {})
        company_message = company_response.get("messageBody", "")
        negotiation_complete = company_response.get("negotiationComplete", False)

        print(f"[SENDER] Resposta da empresa recebida ({len(company_message)} chars)")
        print(f"[SENDER] negotiationComplete={negotiation_complete}")

        # Salvar resposta
        negotiation_data["last_company_response"] = {
            "content": company_message,
            "received_at": datetime.now().isoformat(),
            "message_id": response.get("companyMessageId")
        }

        # Adicionar ao histórico
        negotiation_data["history"].append({
            "round": 1,
            "sender": "company",
            "summary": company_message[:200] + "..." if len(company_message) > 200 else company_message,
            "timestamp": datetime.now().isoformat()
        })

        # 4. Verificar se negociação foi concluída
        if negotiation_complete:
            print(f"[SENDER] Negociação concluída na primeira rodada!")
            await finalize_negotiation(
                negotiation_id=negotiation_id,
                negotiation_data=negotiation_data,
                status="agreement",
                api_client=api_client
            )
            return

        # 5. Analisar resposta e decidir próximos passos
        analysis = _analyze_company_response(company_message, negotiation_data)

        if analysis["agreement_reached"]:
            print(f"[SENDER] Acordo detectado na resposta!")
            await finalize_negotiation(
                negotiation_id=negotiation_id,
                negotiation_data=negotiation_data,
                status="agreement",
                api_client=api_client,
                agreement_details=analysis["agreement_details"]
            )
        elif analysis["should_counter"]:
            print(f"[SENDER] Preparando contraproposta...")
            # Continuar em background
            await continue_negotiation(
                negotiation_id=negotiation_id,
                negotiation_data=negotiation_data,
                counter_proposal=analysis["counter_proposal"],
                api_client=api_client
            )
        elif analysis["should_escalate"]:
            print(f"[SENDER] Escalando negociação...")
            await finalize_negotiation(
                negotiation_id=negotiation_id,
                negotiation_data=negotiation_data,
                status="escalated",
                api_client=api_client,
                reason="Empresa recusou todas as propostas"
            )

        print(f"[SENDER] ========== PROCESSAMENTO INICIAL CONCLUÍDO ==========\n")

    except Exception as e:
        print(f"[SENDER] ERRO ao processar início: {e}")
        import traceback
        traceback.print_exc()
        negotiation_data["error"] = str(e)


async def continue_negotiation(
    negotiation_id: str,
    negotiation_data: Dict[str, Any],
    counter_proposal: str,
    api_client: ZelluAPIClient
):
    """
    Continua a negociação com uma contraproposta.
    """
    try:
        current_round = negotiation_data.get("current_round", 1) + 1
        max_rounds = negotiation_data.get("max_rounds") or get_settings().NEGOTIATION_MAX_ROUNDS
        session_id = negotiation_data.get("session_id")

        print(f"\n[SENDER] ========== RODADA {current_round}/{max_rounds} ==========")

        # Verificar se atingiu limite de rodadas
        if current_round > max_rounds:
            print(f"[SENDER] Limite de rodadas atingido!")
            await finalize_negotiation(
                negotiation_id=negotiation_id,
                negotiation_data=negotiation_data,
                status="no_agreement",
                api_client=api_client,
                reason=f"Limite de {max_rounds} rodadas atingido sem acordo"
            )
            return

        # Atualizar negociação
        negotiation_data["current_round"] = current_round
        negotiation_data["last_sender_message"] = counter_proposal
        negotiation_data["last_activity_at"] = datetime.now().isoformat()

        # Adicionar ao histórico
        negotiation_data["history"].append({
            "round": current_round,
            "sender": "zelinhu",
            "summary": counter_proposal[:200] + "..." if len(counter_proposal) > 200 else counter_proposal,
            "timestamp": datetime.now().isoformat()
        })

        # Enviar contraproposta
        print(f"[SENDER] Enviando contraproposta...")

        response = await api_client.send_zelinhu_message(
            session_id=session_id,
            message_body=counter_proposal,
            round=current_round,
            negotiation_id=negotiation_id
        )

        if not response.get("success", True):
            error = response.get("error", "Erro desconhecido")
            print(f"[SENDER] ERRO ao enviar contraproposta: {error}")
            negotiation_data["error"] = error
            return

        # Processar resposta
        company_response = response.get("response", {})
        company_message = company_response.get("messageBody", "")
        negotiation_complete = company_response.get("negotiationComplete", False)

        print(f"[SENDER] Resposta da empresa na rodada {current_round}")

        # Salvar resposta
        negotiation_data["last_company_response"] = {
            "content": company_message,
            "received_at": datetime.now().isoformat(),
            "message_id": response.get("companyMessageId")
        }

        # Adicionar ao histórico
        negotiation_data["history"].append({
            "round": current_round,
            "sender": "company",
            "summary": company_message[:200] + "..." if len(company_message) > 200 else company_message,
            "timestamp": datetime.now().isoformat()
        })

        # Verificar conclusão
        if negotiation_complete:
            print(f"[SENDER] Negociação concluída na rodada {current_round}!")
            await finalize_negotiation(
                negotiation_id=negotiation_id,
                negotiation_data=negotiation_data,
                status="agreement",
                api_client=api_client
            )
            return

        # Analisar e decidir
        analysis = _analyze_company_response(company_message, negotiation_data)

        if analysis["agreement_reached"]:
            await finalize_negotiation(
                negotiation_id=negotiation_id,
                negotiation_data=negotiation_data,
                status="agreement",
                api_client=api_client,
                agreement_details=analysis["agreement_details"]
            )
        elif analysis["should_counter"] and current_round < max_rounds:
            # Aguardar um pouco antes da próxima rodada
            await asyncio.sleep(2)
            await continue_negotiation(
                negotiation_id=negotiation_id,
                negotiation_data=negotiation_data,
                counter_proposal=analysis["counter_proposal"],
                api_client=api_client
            )
        elif analysis["should_escalate"] or current_round >= max_rounds:
            await finalize_negotiation(
                negotiation_id=negotiation_id,
                negotiation_data=negotiation_data,
                status="no_agreement",
                api_client=api_client,
                reason="Não foi possível chegar a um acordo"
            )

    except Exception as e:
        print(f"[SENDER] ERRO ao continuar negociação: {e}")
        import traceback
        traceback.print_exc()
        negotiation_data["error"] = str(e)


async def finalize_negotiation(
    negotiation_id: str,
    negotiation_data: Dict[str, Any],
    status: str,
    api_client: ZelluAPIClient,
    agreement_details: Optional[Dict[str, Any]] = None,
    reason: Optional[str] = None
):
    """
    Finaliza a negociação e notifica o Zellu Backend.
    """
    print(f"\n[SENDER] ========== FINALIZANDO NEGOCIAÇÃO ==========")
    print(f"[SENDER] negotiation_id={negotiation_id}")
    print(f"[SENDER] status={status}")

    session_id = negotiation_data.get("session_id")

    # Atualizar dados locais
    negotiation_data["status"] = NegotiationStatus(status)
    negotiation_data["ended_at"] = datetime.now().isoformat()

    if agreement_details:
        negotiation_data["agreement_details"] = agreement_details
    if reason:
        negotiation_data["end_reason"] = reason

    # Notificar Zellu Backend via webhook de status
    if session_id:
        print(f"[SENDER] Notificando Zellu Backend via /api/webhooks/sender/status...")

        result = await api_client.send_negotiation_status(
            session_id=session_id,
            status=status,
            negotiation_id=negotiation_id,
            final_round=negotiation_data.get("current_round", 0),
            reason=reason,
            agreement_details=agreement_details
        )

        if result.get("success"):
            print(f"[SENDER] ✅ Zellu Backend notificado com sucesso!")
            print(f"[SENDER] isTerminal={result.get('isTerminal')}")
        else:
            print(f"[SENDER] ⚠️ Erro ao notificar Zellu: {result.get('error')}")

    print(f"[SENDER] ========== NEGOCIAÇÃO FINALIZADA ==========\n")


async def process_company_response(
    negotiation_id: str,
    negotiation_data: Dict[str, Any],
    response_content: str
):
    """
    Processa resposta da empresa recebida via endpoint direto (fallback).
    """
    print(f"\n[SENDER] Processando resposta da empresa (fallback)")

    try:
        api_client = get_api_client()

        # Adicionar ao histórico
        current_round = negotiation_data.get("current_round", 1)
        negotiation_data["history"].append({
            "round": current_round,
            "sender": "company",
            "summary": response_content[:200] + "..." if len(response_content) > 200 else response_content,
            "timestamp": datetime.now().isoformat()
        })

        # Analisar resposta
        analysis = _analyze_company_response(response_content, negotiation_data)

        if analysis["agreement_reached"]:
            await finalize_negotiation(
                negotiation_id=negotiation_id,
                negotiation_data=negotiation_data,
                status="agreement",
                api_client=api_client,
                agreement_details=analysis["agreement_details"]
            )
        elif analysis["should_counter"]:
            await continue_negotiation(
                negotiation_id=negotiation_id,
                negotiation_data=negotiation_data,
                counter_proposal=analysis["counter_proposal"],
                api_client=api_client
            )
        elif analysis["should_escalate"]:
            await finalize_negotiation(
                negotiation_id=negotiation_id,
                negotiation_data=negotiation_data,
                status="escalated",
                api_client=api_client,
                reason="Empresa recusou propostas"
            )

    except Exception as e:
        print(f"[SENDER] ERRO ao processar resposta: {e}")
        import traceback
        traceback.print_exc()
