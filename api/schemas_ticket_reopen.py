# -*- coding: utf-8 -*-
"""
Schemas para análise de reabertura de ticket.

Conforme docs/20260514-doc-ia-fluxos-geracao-documento-e-reabertura.md:
- Request: Backend -> IA (action: analyze_ticket_reopen)
- Response: IA -> Backend (decision, justification, etc.)
"""

from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any, Literal


# ===== REQUEST BACKEND -> IA =====

class TicketReopenTicket(BaseModel):
    """Dados do ticket."""
    title: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    resolutionType: Optional[str] = None  # "amigavel" | "extrajudicial" | "judicial"
    previousStatus: Optional[str] = None  # "resolved" | "closed"
    closedAt: Optional[str] = None  # ISO-8601 UTC
    reopenCount: Optional[int] = None
    ticketNumber: Optional[str] = None


class TicketReopenContext(BaseModel):
    """Contexto do ticket para análise."""
    ticket: TicketReopenTicket
    openingChatSummary: Optional[str] = Field(None, description="Resumo do chat de abertura")
    negotiationOutcome: Optional[str] = Field(None, description="Como o ticket foi resolvido/fechado")
    previousResolutionSummary: Optional[str] = Field(None, description="O que ficou acordado antes")


class TicketReopenRequest(BaseModel):
    """
    Request: Backend -> IA para análise de reabertura.

    Timeout: ~10s (síncrono).
    """
    action: Literal["analyze_ticket_reopen"] = Field(
        "analyze_ticket_reopen", description="Sempre 'analyze_ticket_reopen'"
    )
    ticketId: str = Field(..., description="UUID do ticket")
    clientId: str = Field(..., description="UUID do cliente")
    reason: str = Field(
        ...,
        min_length=20,
        max_length=2000,
        description="Motivo da reabertura (20-2000 chars)",
    )
    context: TicketReopenContext


# ===== RESPONSE IA -> BACKEND =====

class TicketReopenResponse(BaseModel):
    """
    Response: IA -> Backend com decisão da análise.

    Decisões possíveis:
    - "judicial": Reabrir como judicial
    - "rejected": Motivo insuficiente, manter fechado
    - "zellu_support": Encaminhar para suporte humano da Zellu
    """
    decision: Literal["judicial", "rejected", "zellu_support"] = Field(
        ..., description="'judicial' | 'rejected' | 'zellu_support'"
    )
    confidence: Optional[float] = Field(
        None, ge=0.0, le=1.0, description="Confiança da decisão (0.0-1.0)"
    )
    justification: str = Field(..., description="Justificativa curta para mostrar ao cliente")
    internalNotes: Optional[str] = Field(None, description="Notas internas para auditoria")
    tagsForSupport: Optional[List[str]] = Field(
        None, description="Tags para suporte (se decision='zellu_support')"
    )