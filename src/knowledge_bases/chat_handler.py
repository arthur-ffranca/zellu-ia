# -*- coding: utf-8 -*-
"""
Chat Handler com Function Calling - Zellu IA Empresa

Handler que encapsula a logica de chamar o LLM com suporte a tools.
Processa automaticamente as tool calls e retorna a resposta final.

Uso:
    from src.knowledge_bases.chat_handler import ChatWithToolsHandler

    handler = ChatWithToolsHandler(company_id="empresa_123")
    response = await handler.chat(messages, system_prompt)
"""

from typing import List, Dict, Any, Optional
from src.llm import ChatModel
from src.llm import (
    system_message,
    user_message,
    assistant_message,
    tool_message,
)
import asyncio
import json
import logging

from config import Settings
from .tools import KNOWLEDGE_BASE_TOOLS, execute_tool_call
from src.services.ai_usage_tracker import track_usage, new_event_id
import time

logger = logging.getLogger(__name__)
settings = Settings()


class ToolCallLimitExceeded(RuntimeError):
    """
    O modelo esgotou o orcamento de tool calls e nao produziu resposta.

    Antes o handler devolvia "Desculpe, nao consegui processar sua solicitacao"
    como se fosse conteudo normal - e esse texto virava a mensagem oficial da
    empresa dentro de uma negociacao, sem disparar erro nenhum (achado C-10 da
    auditoria de 22/08/2026). Falhar alto e melhor: o chamador devolve erro, o
    backend Zellu reenvia, e nada indevido e dito em nome da empresa.
    """


# =============================================================================
# HANDLER PRINCIPAL
# =============================================================================

class ChatWithToolsHandler:
    """
    Handler para chat com suporte a function calling.

    Encapsula a logica de:
    1. Enviar mensagens para o LLM com tools
    2. Detectar e executar tool calls
    3. Enviar resultados de volta ao LLM
    4. Retornar resposta final

    Attributes:
        company_id: ID da empresa (para buscar em bases privadas)
        model: Modelo do LLM
        temperature: Temperatura do modelo
        max_tool_calls: Maximo de tool calls por turno (previne loops)
    """

    def __init__(
        self,
        company_id: str,
        model: str = "gpt-4o-mini",
        temperature: float = 0.3,
        max_tool_calls: int = 5,
    ):
        """
        Inicializa o handler.

        Args:
            company_id: ID da empresa
            model: Nome do modelo OpenAI
            temperature: Temperatura (0.0 a 1.0)
            max_tool_calls: Limite de tool calls por turno
        """
        self.company_id = company_id
        self.model = model
        self.temperature = temperature
        self.max_tool_calls = max_tool_calls

        self.llm = ChatModel(
            model=model,
            temperature=temperature,
            api_key=settings.OPENAI_API_KEY,
        )

        # Bind tools ao LLM
        self.llm_with_tools = self.llm.with_tools(KNOWLEDGE_BASE_TOOLS)

    async def chat(
        self,
        messages: List,
        include_tools: bool = True,
    ) -> Dict[str, Any]:
        """
        Processa uma conversa com suporte a tools.

        Args:
            messages: Lista de mensagens (system_message, user_message, etc)
            include_tools: Se True, permite uso de tools

        Returns:
            Dict com:
            - content: Resposta final do LLM
            - tool_calls_made: Lista de tools chamadas
            - tokens_used: Total de tokens usados
        """
        tool_calls_made = []
        total_tokens = 0
        current_messages = messages.copy()
        # eventId unico para correlacionar todas as chamadas LLM deste turno de chat
        usage_event_id = new_event_id()

        # Limite de iteracoes para prevenir loops infinitos
        for iteration in range(self.max_tool_calls + 1):
            # Chamar LLM
            _t0 = time.perf_counter()
            if include_tools:
                response = await self.llm_with_tools.complete(current_messages)
            else:
                response = await self.llm.complete(current_messages)

            # Observabilidade de custo (fire-and-forget) - 1 evento por chamada LLM
            track_usage(
                module="kb_chat",
                model=self.model,
                response=response,
                event_id=usage_event_id,
                event_type="kb_chat",
                company_id=self.company_id,
                duration_ms=int((time.perf_counter() - _t0) * 1000),
            )

            # Contar tokens
            if hasattr(response, 'usage_metadata') and response.usage_metadata:
                total_tokens += response.usage_metadata.get("total_tokens", 0)

            # Verificar se ha tool calls
            if not hasattr(response, 'tool_calls') or not response.tool_calls:
                # Sem tool calls - retornar resposta final
                return {
                    "content": response.content,
                    "tool_calls_made": tool_calls_made,
                    "tokens_used": total_tokens,
                }

            # Processar tool calls
            logger.info(f"[CHAT] {len(response.tool_calls)} tool call(s) detectada(s)")

            # Adicionar resposta do AI (com tool calls) ao historico
            current_messages.append(response)

            # Executar as tool calls da MESMA iteracao em paralelo. Elas sao
            # independentes entre si (cada uma e uma busca vetorial), e o modelo
            # so volta a rodar quando todas terminam - executar em sequencia so
            # somava latencia (diagnostico A1, 22/08/2026).
            logger.info(
                f"[CHAT] Executando {len(response.tool_calls)} tool(s): "
                f"{[t['name'] for t in response.tool_calls]}"
            )

            results = await asyncio.gather(*[
                execute_tool_call(
                    tool_name=tool_call["name"],
                    tool_args=tool_call["args"],
                    company_id=self.company_id,
                )
                for tool_call in response.tool_calls
            ], return_exceptions=True)

            for tool_call, tool_result in zip(response.tool_calls, results):
                if isinstance(tool_result, BaseException):
                    logger.error(f"[CHAT] Tool {tool_call['name']} falhou: {tool_result}")
                    tool_result = f"Erro ao consultar a base: {tool_result}"

                # Registrar
                tool_calls_made.append({
                    "name": tool_call["name"],
                    "args": tool_call["args"],
                    "result_preview": tool_result[:200] if tool_result else "",
                })

                # Adicionar resultado ao historico
                current_messages.append(
                    tool_message(
                        content=tool_result,
                        tool_call_id=tool_call["id"],
                    )
                )

        # Limite de tool calls atingido. O modelo ja tem os resultados das buscas
        # no historico: falta so redigir. Uma ultima chamada SEM tools forca a
        # resposta em texto, em vez de devolver uma desculpa como se fosse a
        # mensagem da empresa (achado C-10).
        logger.warning(
            f"[CHAT] Limite de {self.max_tool_calls} tool calls atingido - "
            f"forcando resposta final sem tools"
        )

        try:
            _t0 = time.perf_counter()
            response = await self.llm.complete(current_messages)
            track_usage(
                module="kb_chat",
                model=self.model,
                response=response,
                event_id=usage_event_id,
                event_type="kb_chat",
                company_id=self.company_id,
                duration_ms=int((time.perf_counter() - _t0) * 1000),
            )
            if hasattr(response, 'usage_metadata') and response.usage_metadata:
                total_tokens += response.usage_metadata.get("total_tokens", 0)

            content = (response.content or "").strip()
        except Exception as e:
            raise ToolCallLimitExceeded(
                f"Limite de {self.max_tool_calls} tool calls atingido e a resposta "
                f"final falhou: {type(e).__name__}: {e}"
            ) from e

        if not content:
            raise ToolCallLimitExceeded(
                f"Limite de {self.max_tool_calls} tool calls atingido e o modelo "
                f"nao produziu resposta final"
            )

        return {
            "content": content,
            "tool_calls_made": tool_calls_made,
            "tokens_used": total_tokens,
            "tool_limit_reached": True,
        }


# =============================================================================
# FUNCAO HELPER PARA USO SIMPLES
# =============================================================================

async def chat_with_knowledge_base(
    messages: List,
    company_id: str,
    model: str = "gpt-4o-mini",
    temperature: float = 0.3,
) -> Dict[str, Any]:
    """
    Funcao helper para chat simples com suporte a knowledge bases.

    Args:
        messages: Lista de mensagens
        company_id: ID da empresa
        model: Modelo do LLM
        temperature: Temperatura

    Returns:
        Dict com content, tool_calls_made, tokens_used
    """
    handler = ChatWithToolsHandler(
        company_id=company_id,
        model=model,
        temperature=temperature,
    )
    return await handler.chat(messages)
