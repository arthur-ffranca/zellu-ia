# -*- coding: utf-8 -*-
"""
Rotas para análise de reabertura de tickets.

Endpoint: POST /api/ai/analyze-ticket-reopen
"""

from fastapi import APIRouter, HTTPException, Header, Depends
from fastapi.responses import JSONResponse
from api.schemas_ticket_reopen import TicketReopenRequest, TicketReopenResponse
from src.services.ticket_reopen_service import get_ticket_reopen_service
from config import get_settings

router = APIRouter(prefix="/api/ai", tags=["AI Services"])
settings = get_settings()


def verify_api_key(x_api_key: str = Header(..., alias="x-api-key")):
    """Verify API key for protected endpoints."""
    if x_api_key != settings.API_KEY_ZELLU_IA:
        raise HTTPException(status_code=401, detail="Não autorizado")
    return x_api_key


@router.post(
    "/analyze-ticket-reopen",
    response_model=TicketReopenResponse,
    responses={
        200: {"description": "Análise concluída com sucesso"},
        400: {"description": "Payload inválido"},
        401: {"description": "Não autorizado"},
        500: {"description": "Erro interno na análise"}
    },
    summary="Analisar Solicitação de Reabertura",
    description="Analisa o motivo de reabertura de um ticket e decide entre judicial/rejected/zellu_support."
)
async def analyze_ticket_reopen(
    request: TicketReopenRequest,
    api_key: str = Depends(verify_api_key)
) -> TicketReopenResponse:
    """
    Analisa uma solicitação de reabertura de ticket.

    Recebe o motivo da reabertura + contexto do ticket e usa IA para decidir:
    - judicial: reabrir como judicial
    - rejected: motivo insuficiente
    - zellu_support: encaminhar para suporte

    Timeout: ~10s (síncrono).
    """
    try:
        ticket_number = request.context.ticket.ticketNumber if request.context and request.context.ticket else None
        resolution_type = request.context.ticket.resolutionType if request.context and request.context.ticket else None
        previous_status = request.context.ticket.previousStatus if request.context and request.context.ticket else None

        print(f"[REOPEN-ANALYSIS] ========================================")
        print(f"[REOPEN-ANALYSIS] Analisando reabertura")
        print(f"[REOPEN-ANALYSIS] TicketId: {request.ticketId}")
        print(f"[REOPEN-ANALYSIS] ClientId: {request.clientId}")
        print(f"[REOPEN-ANALYSIS] TicketNumber: {ticket_number}")
        print(f"[REOPEN-ANALYSIS] PreviousStatus/ResolutionType: {previous_status} / {resolution_type}")
        print(f"[REOPEN-ANALYSIS] Reason length: {len(request.reason)} chars")
        print(f"[REOPEN-ANALYSIS] Reason preview: {request.reason[:200]!r}")

        service = get_ticket_reopen_service()
        result = await service.analyze_reopen(request)

        print(f"[REOPEN-ANALYSIS] Decisão: {result.decision}")
        print(f"[REOPEN-ANALYSIS] Confidence: {result.confidence}")
        print(f"[REOPEN-ANALYSIS] Justification: {result.justification!r}")
        print(f"[REOPEN-ANALYSIS] ========================================")

        return result

    except Exception as e:
        print(f"[REOPEN-ANALYSIS] Erro na análise: {e}")
        return TicketReopenResponse(
            decision="zellu_support",
            confidence=0.0,
            justification="Erro na análise automática. Encaminhado para suporte.",
            internalNotes=f"Erro: {str(e)}",
            tagsForSupport=["analysis_error"]
        )