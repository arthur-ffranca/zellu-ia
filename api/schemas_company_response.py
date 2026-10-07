# -*- coding: utf-8 -*-
"""
Schemas para o endpoint Company Response AI.

Define os modelos Pydantic conforme payload-spec.md:
- Payload recebido da plataforma Zellu
- Response enviada de volta para a Zellu
"""

from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any, Union
from datetime import datetime


# ===== CONTEXTO DO TICKET =====

class TicketContext(BaseModel):
    """Contexto do ticket/chamado."""
    id: str = Field(..., description="ID do ticket")
    number: Optional[str] = Field(None, description="Numero sequencial do ticket (exibicao)")
    title: Optional[str] = Field(None, description="Titulo do chamado")
    description: Optional[str] = Field(None, description="Descricao detalhada")
    status: Optional[str] = Field("in_progress", description="Status atual")
    resolutionType: Optional[str] = Field(None, description="Tipo de resolucao")
    estimatedValue: Optional[float] = Field(0, description="Valor estimado da causa")
    category: Optional[str] = Field(None, description="Categoria juridica")
    priority: Optional[str] = Field("medium", description="Prioridade")
    createdAt: Optional[str] = Field(None, description="Data de criacao (ISO 8601)")


class ClientContext(BaseModel):
    """Contexto do cliente."""
    name: Optional[str] = Field(None, description="Nome completo do cliente")
    email: Optional[str] = Field(None, description="Email de contato")
    phone: Optional[str] = Field(None, description="Telefone com DDD")
    cpf: Optional[str] = Field(None, description="CPF do cliente")


class CompanyContext(BaseModel):
    """Contexto da empresa."""
    id: Optional[str] = Field(None, description="ID da empresa")
    name: Optional[str] = Field(None, description="Razao social da empresa")
    tradeName: Optional[str] = Field(None, description="Nome fantasia")
    cnpj: Optional[str] = Field(None, description="CNPJ da empresa")
    segment: Optional[str] = Field(None, description="Segmento de atuacao")


class PayloadContext(BaseModel):
    """Contexto completo do payload."""
    ticket: Optional[TicketContext] = Field(default_factory=TicketContext)
    client: Optional[ClientContext] = Field(default_factory=ClientContext)
    company: Optional[CompanyContext] = Field(default_factory=CompanyContext)


# ===== HISTORICO DE MENSAGENS =====

class MessageHistoryItem(BaseModel):
    """Item do historico de mensagens."""
    id: Optional[str] = Field(None, description="ID da mensagem")
    origin: str = Field(..., description="Origem: zelinhu, company_ai, company_user")
    type: Optional[str] = Field("text", description="Tipo: text, file, image")
    body: str = Field(..., description="Conteudo da mensagem")
    data: Optional[Dict[str, Any]] = Field(None, description="Dados adicionais")
    hasAttachments: Optional[bool] = Field(False, description="Se possui anexos")
    timestamp: Optional[str] = Field(None, description="Data/hora (ISO 8601)")


# ===== KNOWLEDGE BASES =====

class KnowledgeBase(BaseModel):
    """Base de conhecimento ativa da empresa."""
    id: str = Field(..., description="ID da knowledge base")
    name: Optional[str] = Field(None, description="Nome da base")
    description: Optional[str] = Field(None, description="Descricao")
    # category pode ser string ou objeto (Backend Zellu retorna objeto com id, name, deletedAt, etc.)
    category: Optional[Union[str, Dict[str, Any]]] = Field(None, description="Categoria: policies, faq, legal ou objeto completo")
    isDefault: Optional[bool] = Field(False, description="Se e a base padrao")


# ===== REGRAS TEMPORARIAS =====

class TemporaryRule(BaseModel):
    """Regra temporaria ativa da empresa."""
    id: str = Field(..., description="ID da regra")
    name: Optional[str] = Field(None, description="Titulo da regra")
    type: Optional[str] = Field("temporary", description="Tipo da regra")
    content: Optional[str] = Field(None, description="Conteudo/instrucoes da regra")
    description: Optional[str] = Field(None, description="Descricao adicional")


# ===== CONFIGURACAO DA IA =====

class AIConfig(BaseModel):
    """Configuracoes de modelo e parametros para a IA."""
    model: Optional[str] = Field("gpt-4-turbo", description="Modelo de IA a ser usado")
    maxTokens: Optional[int] = Field(1500, description="Numero maximo de tokens")
    temperature: Optional[float] = Field(0.7, description="Temperatura (0.0 a 1.0)")


# ===== PAYLOAD PRINCIPAL =====

class CompanyResponsePayload(BaseModel):
    """
    Payload completo enviado pela Zellu para o servico de IA da Empresa.

    Conforme payload-spec.md.
    """
    sessionId: str = Field(..., description="ID unico da sessao de resposta")
    lastMessageId: Optional[str] = Field(None, description="ID da ultima mensagem que disparou o webhook")
    chat_id: Optional[str] = Field(None, description="ID da sessao de chat (usar nas respostas)")
    context: Optional[PayloadContext] = Field(default_factory=PayloadContext, description="Contexto completo")
    messageHistory: Optional[List[MessageHistoryItem]] = Field(default_factory=list, description="Historico de mensagens")
    knowledgeBases: Optional[List[KnowledgeBase]] = Field(default_factory=list, description="Bases de conhecimento")
    rules: Optional[List[TemporaryRule]] = Field(default_factory=list, description="Regras temporarias")
    config: Optional[AIConfig] = Field(default_factory=AIConfig, description="Configuracoes da IA")

    class Config:
        json_schema_extra = {
            "example": {
                "sessionId": "cm4abc123def456",
                "lastMessageId": "msg789ghi012",
                "context": {
                    "ticket": {
                        "id": "ticket123",
                        "number": "2024-001",
                        "title": "Cancelamento de plano premium",
                        "description": "Cliente solicitou cancelamento",
                        "status": "in_progress",
                        "estimatedValue": 350.00
                    },
                    "client": {
                        "name": "Joao Silva",
                        "email": "joao@example.com"
                    },
                    "company": {
                        "name": "TechCorp",
                        "segment": "Telecomunicacoes"
                    }
                },
                "messageHistory": [
                    {
                        "id": "msg001",
                        "origin": "zelinhu",
                        "type": "text",
                        "body": "Ola, solicito cancelamento do plano."
                    }
                ],
                "config": {
                    "model": "gpt-4-turbo",
                    "maxTokens": 1500,
                    "temperature": 0.7
                }
            }
        }


# ===== RESPONSE DA IA =====

class CompanyAIResponse(BaseModel):
    """
    Response enviada pelo servico de IA da Empresa de volta para a Zellu.

    Conforme payload-spec.md - Response Esperada.
    """
    messageBody: str = Field(..., description="Texto da resposta da IA (OBRIGATORIO)")
    messageType: Optional[str] = Field("text", description="Tipo da mensagem: text, file, image")
    messagedata: Optional[Dict[str, Any]] = Field(None, description="Dados adicionais em formato JSON")
    tokensUsed: Optional[int] = Field(None, description="Quantidade de tokens consumidos")
    modelUsed: Optional[str] = Field(None, description="Modelo utilizado para gerar a resposta")
    negotiationComplete: Optional[bool] = Field(False, description="Se true, indica que a negociacao foi concluida")

    class Config:
        json_schema_extra = {
            "example": {
                "messageBody": "Entendo perfeitamente. Estamos a disposicao para processar o cancelamento.",
                "messageType": "text",
                "tokensUsed": 87,
                "modelUsed": "gpt-4-turbo",
                "negotiationComplete": False
            }
        }


# ===== REQUEST SIMPLIFICADO (webhook-spec.md) =====

class WebhookTriggerRequest(BaseModel):
    """
    Request simplificado enviado pela Zellu para disparar o webhook.

    Conforme webhook-spec.md - o backend Zellu envia apenas IDs,
    e o servico de IA pode buscar dados adicionais via endpoints.
    """
    sessionId: str = Field(..., description="ID da sessao de resposta")
    messageId: Optional[str] = Field(None, description="ID da mensagem que disparou (para idempotency)")
    ticketId: str = Field(..., description="ID do ticket/chamado")
    companyId: str = Field(..., description="ID da empresa")

    class Config:
        json_schema_extra = {
            "example": {
                "sessionId": "cm123abc",
                "messageId": "msg456def",
                "ticketId": "ticket789ghi",
                "companyId": "comp012jkl"
            }
        }


# ===== RESPONSE DO WEBHOOK =====

class WebhookSuccessResponse(BaseModel):
    """Response de sucesso do webhook."""
    success: bool = Field(True)
    message: str = Field(..., description="Mensagem de status")
    newMessageId: Optional[str] = Field(None, description="ID da nova mensagem criada")
    existingMessageId: Optional[str] = Field(None, description="ID da mensagem existente (idempotency)")


class WebhookErrorResponse(BaseModel):
    """Response de erro do webhook."""
    error: str = Field(..., description="Mensagem de erro")
    message: Optional[str] = Field(None, description="Detalhes adicionais")
    details: Optional[str] = Field(None, description="Detalhes tecnicos")
    retryAfter: Optional[int] = Field(None, description="Segundos para retry (rate limit)")
