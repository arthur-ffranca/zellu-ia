# -*- coding: utf-8 -*-
"""
Serviço de análise de reabertura de tickets.

Classifica motivo de reabertura em judicial/rejected/zellu_support.
"""

import json
import re
import time

from src.llm import ChatModel
from api.schemas_ticket_reopen import TicketReopenRequest, TicketReopenResponse
from config import get_settings
from src.services.ai_usage_tracker import track_usage

settings = get_settings()

VALID_DECISIONS = ("judicial", "rejected", "zellu_support")


class TicketReopenService:
    """
    Serviço para analisar solicitações de reabertura de tickets.
    """

    def __init__(self):
        self.llm = ChatModel(
            model=settings.COMPANY_RESPONSE_AI_MODEL,
            temperature=0.1,
            max_tokens=1000,
            timeout=10,
            model_kwargs={"response_format": {"type": "json_object"}},
        )

    async def analyze_reopen(self, request: TicketReopenRequest) -> TicketReopenResponse:
        """
        Analisa o motivo de reabertura e decide o caminho apropriado.

        Args:
            request: Dados da solicitação de reabertura

        Returns:
            Decisão com justificativa
        """
        # Preparar contexto para o prompt
        ticket_info = self._format_ticket_info(request.context.ticket)
        reason = request.reason

        prompt_template = """
        Você é um assistente jurídico especializado em analisar solicitações de reabertura de casos já resolvidos.

        ANALISE a solicitação de reabertura abaixo e decida o caminho apropriado.

        MOTIVO DA REABERTURA:
        {reason}

        INFORMAÇÕES DO TICKET:
        {ticket_info}

        RESUMO DO CHAT DE ABERTURA:
        {opening_chat_summary}

        RESULTADO DA NEGOCIAÇÃO ANTERIOR:
        {negotiation_outcome}

        RESUMO DA RESOLUÇÃO ANTERIOR:
        {previous_resolution_summary}

        CRITÉRIOS PARA DECISÃO:

        1. **JUDICIAL**: Escolha quando:
           - Há novos fatos jurídicos relevantes não considerados antes
           - Violação clara de direitos do cliente
           - Necessidade de intervenção judicial (liminar, execução, etc.)
           - Mudança significativa nas circunstâncias que justifique processo

        2. **REJECTED**: Escolha quando:
           - Motivo insuficiente ou já tratado
           - Reclamação operacional sem fundamento jurídico
           - Cliente apenas insatisfeito com o resultado amigável
           - Tentativa de renegociar termos já acordados

        3. **ZELLU_SUPPORT**: Escolha quando:
           - Problema técnico na plataforma
           - Dúvida sobre funcionamento do sistema
           - Solicitação de suporte administrativo
           - Erro operacional da Zellu

        Responda SOMENTE com um objeto JSON válido, sem texto adicional, sem markdown.
        Estrutura obrigatória do JSON (use exatamente estas chaves em camelCase):
        {{
            "decision": "judicial" | "rejected" | "zellu_support",
            "confidence": 0.0-1.0,
            "justification": "explicação curta para o cliente (máx 200 chars)",
            "internalNotes": "notas técnicas para auditoria",
            "tagsForSupport": ["tag1", "tag2"]
        }}
        Inclua "tagsForSupport" como array vazio quando decision != "zellu_support".
        """

        try:
            _t0 = time.perf_counter()
            response = await self.llm.complete(prompt_template.format(**{
                "reason": reason,
                "ticket_info": ticket_info,
                "opening_chat_summary": request.context.openingChatSummary or "Não disponível",
                "negotiation_outcome": request.context.negotiationOutcome or "Não disponível",
                "previous_resolution_summary": request.context.previousResolutionSummary or "Não disponível"
            }))
            track_usage(
                module="ticket_reopen",
                model=settings.COMPANY_RESPONSE_AI_MODEL,
                response=response,
                event_type="ticket_reopen_analysis",
                ticket_id=getattr(request, "ticketId", None),
                duration_ms=int((time.perf_counter() - _t0) * 1000),
            )

            raw_content = (response.content or "").strip()
            print(f"[REOPEN_ANALYSIS] LLM raw response ({len(raw_content)} chars): {raw_content[:500]}")

            result = self._parse_llm_json(raw_content)

            decision = result.get("decision", "zellu_support")
            if decision not in VALID_DECISIONS:
                print(f"[REOPEN_ANALYSIS] Decisão inválida do LLM: {decision!r} → fallback zellu_support")
                decision = "zellu_support"

            justification = (result.get("justification") or "").strip() or "Análise em andamento"
            internal_notes = result.get("internalNotes") or result.get("internal_notes")
            tags_raw = result.get("tagsForSupport") or result.get("tags_for_support") or []
            tags = tags_raw if isinstance(tags_raw, list) else []

            return TicketReopenResponse(
                decision=decision,
                confidence=result.get("confidence", 0.5),
                justification=justification,
                internalNotes=internal_notes,
                tagsForSupport=tags if decision == "zellu_support" else None,
            )

        except Exception as e:
            print(f"[REOPEN_ANALYSIS] Erro na análise: {e}")
            return TicketReopenResponse(
                decision="zellu_support",
                confidence=0.0,
                justification="Erro na análise automática. Encaminhado para suporte.",
                internalNotes=f"Erro LLM: {str(e)}",
                tagsForSupport=["analysis_error"]
            )

    @staticmethod
    def _parse_llm_json(content: str) -> dict:
        """Parse JSON da resposta do LLM, tolerando code fences eventuais."""
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            pass

        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
        if fenced:
            return json.loads(fenced.group(1))

        first = content.find("{")
        last = content.rfind("}")
        if first != -1 and last != -1 and last > first:
            return json.loads(content[first:last + 1])

        raise ValueError(f"Resposta do LLM não contém JSON parseável: {content[:200]}")

    def _format_ticket_info(self, ticket) -> str:
        """Formata informações do ticket para o prompt."""
        return f"""
        Título: {ticket.title or 'N/A'}
        Descrição: {ticket.description or 'N/A'}
        Categoria: {ticket.category or 'N/A'}
        Tipo de resolução anterior: {ticket.resolutionType or 'N/A'}
        Status anterior: {ticket.previousStatus or 'N/A'}
        Data de fechamento: {ticket.closedAt or 'N/A'}
        Número de reaberturas: {ticket.reopenCount or 0}
        Número do ticket: {ticket.ticketNumber or 'N/A'}
        """


# Singleton
_reopen_service = None

def get_ticket_reopen_service() -> TicketReopenService:
    """Retorna instância singleton do serviço de análise de reabertura."""
    global _reopen_service
    if _reopen_service is None:
        _reopen_service = TicketReopenService()
    return _reopen_service