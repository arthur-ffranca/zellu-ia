# -*- coding: utf-8 -*-
"""API request and response schemas."""

from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Optional, List, Dict, Any, Union
from datetime import datetime


class MessageRequest(BaseModel):
    """Request schema for incoming messages.

    Compatível com formato Zellu (aceita campos id, nome, message e body_message).
    """

    chat_id: str = Field(..., description="UUID da sessao de chat")
    user_id: Optional[str] = Field(default=None, description="UUID do usuario (opcional)")
    message_type: str = Field(default="text", description="Tipo da mensagem (text, audio, file, mixed)")

    # Aceita tanto "message" (backend Zellu) quanto "body_message" (nosso formato)
    body_message: Optional[str] = Field(default=None, description="Conteudo da mensagem")
    message: Optional[str] = Field(default=None, description="Conteudo da mensagem (formato alternativo)")

    audio: Optional[List[str]] = Field(default=None, description="URLs de audios anexados")
    files: List[str] = Field(default_factory=list, description="URLs de arquivos anexados")

    # Campos adicionais do formato Zellu (opcionais)
    id: Optional[str] = Field(default=None, description="UUID da mensagem (formato Zellu)")
    nome: Optional[str] = Field(default=None, description="Nome do usuario (formato Zellu)")
    group_metadata: Optional[Dict[str, Any]] = Field(default=None, description="Metadata de mensagens em grupo")

    # Identificador do despacho enviado pelo app na raiz do payload
    # (contrato docs/20260807-contrato-fila-despacho-eco-dispatch-id.md).
    # Ecoado de volta na RAIZ do callback - nunca derivado do `id`.
    dispatch_id: Optional[str] = Field(
        default=None,
        description="UUID do despacho (contrato 20260807) - ecoado na raiz do callback"
    )

    # Dados da empresa associada (quando chamado via "Busca Empresa")
    company: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Dados da empresa associada (formato Zellu: id, cnpj, company_name, trade_name, segment_id)"
    )

    # Qualificacao do cliente vinda do backend (context.client) - usada no preview
    # dos documentos juridicos: name, email, phone, cpf, address, rg, nacionalidade,
    # estadoCivil, profissao (contrato 20260624).
    client: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Dados de qualificacao do cliente (context.client) para os documentos"
    )

    # Dados adicionais (dynamic_form_response, etc)
    messagedata: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Dados adicionais (dynamic_form_response, etc)"
    )

    # Campo de controle de continuacao (Continuar Conversando)
    is_finished: Optional[bool] = Field(default=None, description="False quando usuario clica em Continuar Conversando")

    @model_validator(mode='after')
    def validate_message_content(self):
        """Valida que pelo menos um dos campos de mensagem está presente."""
        if not self.body_message and not self.message:
            has_form_response = bool(
                isinstance(self.messagedata, dict) and (
                    self.messagedata.get("dynamic_form_response")
                    or self.messagedata.get("dynamicFormResponse")
                    or self.messagedata.get("form_response")
                    or self.messagedata.get("formResponse")
                )
            )
            if has_form_response:
                self.body_message = "[Resposta de formulário]"
            elif self.files or self.audio:
                self.body_message = "[Anexos enviados para análise do caso]"
            else:
                raise ValueError("Pelo menos um dos campos 'message' ou 'body_message' deve estar presente")

        # Se message está presente mas body_message não, copia message para body_message
        if self.message and not self.body_message:
            self.body_message = self.message

        # Se body_message está presente mas message não, copia body_message para message
        if self.body_message and not self.message:
            self.message = self.body_message

        return self

    class Config:
        json_schema_extra = {
            "example": {
                "chat_id": "550e8400-e29b-41d4-a716-446655440000",
                "user_id": "550e8400-e29b-41d4-a716-446655440001",
                "message_type": "text",
                "body_message": "Ola, preciso de ajuda",
                "audio": None,
                "files": []
            }
        }


class MessageResponse(BaseModel):
    """Response schema for agent replies."""

    id: str = Field(..., description="UUID da resposta")
    chat_id: str = Field(..., description="UUID da sessao de chat")
    message: str = Field(..., description="Mensagem de resposta")
    agent: str = Field(..., description="Nome do agente que processou")
    completed: bool = Field(default=False, description="Se a tarefa foi concluida")
    timestamp: datetime = Field(default_factory=datetime.now, description="Timestamp da resposta")

    class Config:
        json_schema_extra = {
            "example": {
                "id": "550e8400-e29b-41d4-a716-446655440002",
                "chat_id": "550e8400-e29b-41d4-a716-446655440000",
                "message": "Ola! Bem-vindo ao Zellinho...",
                "agent": "intake",
                "completed": False,
                "timestamp": "2025-10-21T15:30:00"
            }
        }


class ConversationStateResponse(BaseModel):
    """Response schema for conversation state."""

    id: Optional[str]= None
    chat_id: str
    user_id: str
    current_agent: str
    step: int
    completed: bool
    validated: bool
    client_name: Optional[str] = None
    client_cpf: Optional[str] = None
    problem_type: Optional[str] = None  # DEPRECATED: Categoria é identificada automaticamente
    problem_description: Optional[str] = None

    class Config:
        json_schema_extra = {
            "example": {
                "id": "550e8400-e29b-41d4-a716-446655440003",
                "chat_id": "550e8400-e29b-41d4-a716-446655440000",
                "user_id": "550e8400-e29b-41d4-a716-446655440001",
                "current_agent": "intake",
                "step": 2,
                "completed": False,
                "validated": False,
                "client_name": "Joao Silva",
                "client_cpf": None,
                "problem_type": None,
                "problem_description": None
            }
        }


class ErrorResponse(BaseModel):
    """Error response schema."""

    error: str = Field(..., description="Mensagem de erro")
    detail: Optional[str] = Field(None, description="Detalhes adicionais do erro")
    timestamp: datetime = Field(default_factory=datetime.now, description="Timestamp do erro")

    class Config:
        json_schema_extra = {
            "example": {
                "error": "Erro ao processar mensagem",
                "detail": "Invalid chat_id format",
                "timestamp": "2025-10-21T15:30:00"
            }
        }


class HealthCheckResponse(BaseModel):
    """Health check response schema."""

    status: str = Field(default="healthy", description="Status da aplicacao")

    class Config:
        json_schema_extra = {
            "example": {
                "status": "healthy",
            }
        }


# =============================================================================
# NOVO MODELO v2 - Endpoint /api/company/base-dados e /api/webhook/process
# =============================================================================
# Aceita AMBOS os formatos: camelCase (Zellu) e snake_case (interno)
# =============================================================================

class NegotiationContext(BaseModel):
    """Contexto da negociacao em andamento."""
    negotiationId: Optional[str] = Field(default=None, alias="negotiation_id")
    status: Optional[str] = None
    currentRound: Optional[int] = Field(default=None, alias="current_round")
    maxRounds: Optional[int] = Field(default=None, alias="max_rounds")
    isActive: bool = Field(default=True, alias="is_active")
    manualModeActive: bool = Field(default=False, alias="manual_mode_active")

    class Config:
        populate_by_name = True


class TicketContext(BaseModel):
    """Contexto do ticket/chamado."""
    id: Optional[str]= None
    number: Optional[str] = None
    seqId: Optional[str] = Field(default=None, alias="seq_id")
    title: Optional[str] = None
    subject: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    resolutionType: Optional[str] = Field(default=None, alias="resolution_type")
    estimatedValue: Optional[float] = Field(default=None, alias="estimated_value")
    category: Optional[str] = None
    priority: Optional[str] = None
    createdAt: Optional[str] = Field(default=None, alias="created_at")
    solution: Optional[Dict[str, Any]] = None

    class Config:
        populate_by_name = True


class ClientContext(BaseModel):
    """Contexto do cliente."""
    name: str
    email: Optional[str] = None
    cpf: Optional[str] = None


class CompanyContext(BaseModel):
    """Contexto da empresa."""
    id: Optional[str]= None
    name: str
    tradeName: Optional[str] = Field(default=None, alias="trade_name")
    cnpj: Optional[str] = None
    segment: Optional[str] = None

    class Config:
        populate_by_name = True


class InstructionsPayload(BaseModel):
    """Instrucoes da IA (5 campos estruturados)."""
    # Campos opcionais para aceitar null do Backend Zellu
    persona: Optional[str] = Field(default=None, description="Personalidade da IA")
    rules: Optional[str] = Field(default=None, description="Regras de conduta")
    legal: Optional[str] = Field(default=None, description="Consideracoes legais")
    budget: Optional[str] = Field(default=None, description="Orcamento e negociacao")
    tone: Optional[str] = Field(default=None, description="Tom de voz (opcional)")


class MessageHistoryItem(BaseModel):
    """Item do historico de mensagens."""
    id: Optional[str]= None
    origin: str  # zelinhu, company_ai, company_user, company_manual, system
    type: str = "text"
    body: str
    data: Optional[Dict[str, Any]] = None
    hasAttachments: bool = Field(default=False, alias="has_attachments")
    timestamp: Optional[str] = None
    createdAt: Optional[str] = Field(default=None, alias="created_at")

    @model_validator(mode='after')
    def normalize_timestamp(self):
        """Aceita tanto timestamp quanto createdAt."""
        if self.createdAt and not self.timestamp:
            self.timestamp = self.createdAt
        elif self.timestamp and not self.createdAt:
            self.createdAt = self.timestamp
        return self

    class Config:
        populate_by_name = True


class KnowledgeDocument(BaseModel):
    """Documento da base de conhecimento."""
    id: Optional[str]= None
    title: str
    content: str
    category: Optional[str] = None


class KnowledgeBase(BaseModel):
    """
    Base de conhecimento conforme payload-spec.md.
    Exemplo do payload:
    {
      "id": "kb001",
      "name": "Políticas de Cancelamento 2024",
      "description": "Políticas atualizadas de cancelamento de contratos",
      "category": "policies",
      "isDefault": true
    }
    """
    id: Optional[str] = None
    name: str
    description: Optional[str] = None
    # category pode ser string ou objeto (Backend Zellu retorna objeto com id, name, etc.)
    category: Optional[Union[str, Dict[str, Any]]] = None
    isDefault: bool = Field(default=False, alias="is_default")
    documents: List[KnowledgeDocument] = Field(default_factory=list)

    class Config:
        populate_by_name = True


class TemporaryRulePayload(BaseModel):
    """
    Regra temporaria conforme payload-spec.md.
    Exemplo do payload:
    {
      "id": "rule001",
      "name": "Black Friday - Descontos Especiais",
      "type": "temporary",
      "content": "Durante novembro, oferecer 20% de desconto...",
      "description": "Regra promocional para retenção de clientes"
    }
    """
    id: Optional[str] = None
    title: Optional[str] = None
    name: Optional[str] = None
    type: Optional[str] = Field(default="temporary", description="Tipo da regra (sempre 'temporary')")
    content: str
    description: Optional[str] = None
    validFrom: Optional[str] = Field(default=None, alias="valid_from")
    validUntil: Optional[str] = Field(default=None, alias="valid_until")
    isActive: bool = Field(default=True, alias="is_active")

    @model_validator(mode='after')
    def normalize_title(self):
        """Aceita tanto title quanto name."""
        if self.name and not self.title:
            self.title = self.name
        elif self.title and not self.name:
            self.name = self.title
        return self

    class Config:
        populate_by_name = True


class AIConfig(BaseModel):
    """Configuracoes do modelo de IA."""
    model: str = "gpt-4o-mini"
    maxTokens: int = Field(default=1500, alias="max_tokens")
    temperature: float = 0.3

    class Config:
        populate_by_name = True


class EmpresaBaseDadosRequest(BaseModel):
    """
    Request para os endpoints:
    - POST /api/company/base-dados
    - POST /api/webhook/process

    Payload completo enviado pela Zellu contendo todo o contexto
    necessario para a IA gerar uma resposta.

    Aceita AMBOS os formatos: camelCase e snake_case.
    """
    sessionId: str = Field(..., alias="session_id", description="ID da sessao (required)")
    triggeredByMessageId: Optional[str] = Field(default=None, alias="triggered_by_message_id", description="ID da mensagem que disparou")
    lastMessageId: Optional[str] = Field(default=None, alias="last_message_id", description="ID da ultima mensagem")

    # Campos para autoStart (quando Sender inicia sessao)
    ticketId: Optional[str] = Field(default=None, alias="ticket_id", description="ID do ticket (usado em autoStart)")
    autoStart: bool = Field(default=False, alias="auto_start", description="Se e uma chamada de inicio automatico")

    companyId: Optional[str] = Field(default=None, alias="company_id", description="ID da empresa (campo extra, nao documentado)")

    negotiation: Optional[NegotiationContext] = Field(default=None, description="Contexto da negociacao")
    # NOTA: context agora e opcional para suportar autoStart
    context: Optional[Dict[str, Any]] = Field(default=None, description="Contexto (ticket, client, company)")

    # TEMPORARIO: instructions opcional enquanto Marcelo nao cria endpoints
    instructions: Optional[InstructionsPayload] = Field(default=None, description="Instrucoes da IA (5 campos) - OPCIONAL temporariamente")

    messageHistory: List[MessageHistoryItem] = Field(
        default_factory=list,
        alias="message_history",
        description="Historico de mensagens"
    )

    knowledgeBases: List[KnowledgeBase] = Field(
        default_factory=list,
        alias="knowledge_bases",
        description="Bases de conhecimento"
    )

    temporaryRules: List[TemporaryRulePayload] = Field(
        default_factory=list,
        alias="temporary_rules",
        description="Regras temporarias"
    )

    # Alias para compatibilidade: aceita 'rules' tambem
    rules: Optional[List[TemporaryRulePayload]] = Field(
        default=None,
        description="Regras temporarias (alias para temporaryRules)"
    )

    config: Optional[AIConfig] = Field(
        default=None,
        description="Configuracoes do modelo"
    )

    @model_validator(mode='after')
    def normalize_rules(self):
        """Aceita tanto rules quanto temporaryRules."""
        if self.rules and not self.temporaryRules:
            self.temporaryRules = self.rules
        return self

    class Config:
        populate_by_name = True  # Aceita tanto camelCase quanto snake_case


class EmpresaBaseDadosResponse(BaseModel):
    """
    Response dos endpoints /api/company/base-dados e /api/webhook/process.

    Resposta gerada pela IA para enviar de volta a Zellu.
    Retorna em camelCase (padrao Zellu).
    """
    messageBody: str = Field(..., description="Texto da resposta da IA (OBRIGATORIO)")
    messageType: str = Field(default="text", description="Tipo: text | file | image")
    tokensUsed: Optional[int] = Field(default=None, description="Tokens consumidos")
    modelUsed: Optional[str] = Field(default=None, description="Modelo utilizado")
    negotiationComplete: bool = Field(
        default=False,
        description="Se true, negociacao foi concluida com sucesso"
    )
    messagedata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Dados adicionais em JSON (usar {} se vazio, NAO null)"
    )


    class Config:
        # Serializa em camelCase para resposta
        populate_by_name = True

    def model_dump(self, **kwargs):
        # Garantir que messagedata seja {} e nunca None (conforme payload-spec.md)
        data = super().model_dump(**kwargs)
        if data.get('messagedata') is None:
            data['messagedata'] = {}
        return data


# =============================================================================
# WEBHOOK - Notificacao de mudanca na empresa
# =============================================================================
# Os schemas CompanyUpdateWebhookRequest/Response sairam em 17/09, junto do
# POST /api/webhook/company-update que era o unico a usa-los. A plataforma
# confirmou em 15/09 que nunca chamou essa rota. Quem notifica mudanca de
# empresa hoje sao os /company_notify_*_to_ia, mais abaixo neste arquivo.
# =============================================================================


# =============================================================================
# ZELLU API - Dados que buscamos da Zellu
# =============================================================================

class CompanyInstructionsResponse(BaseModel):
    """
    Response quando buscamos instructions da empresa na Zellu.
    GET /api/companies/{companyId}/instructions
    """
    companyId: str = Field(..., alias="company_id")
    persona: str
    rules: str
    legal: str
    budget: str
    tone: Optional[str] = None

    class Config:
        populate_by_name = True


class CompanyDocumentResponse(BaseModel):
    """Documento da empresa."""
    id: Optional[str] = None
    title: str
    content: str
    category: Optional[str] = None
    createdAt: Optional[str] = Field(default=None, alias="created_at")

    class Config:
        populate_by_name = True


class CompanyDocumentsResponse(BaseModel):
    """
    Response quando buscamos documentos da empresa na Zellu.

    ⚠️ A rota GET /api/companies/{companyId}/documents NUNCA EXISTIU. O cliente
    que a chamava saiu em 17/09, e a plataforma pediu para nao cria-la (15/09).
    A lista de documentos da base vem do Pinecone, alimentado pelo
    /company_notify_knowledgebase_to_ia. Nao escrevam um cliente novo para ela.
    """
    companyId: str = Field(..., alias="company_id")
    documents: List[CompanyDocumentResponse] = Field(default_factory=list)

    class Config:
        populate_by_name = True


# =============================================================================
# WEBHOOKS DE NOTIFICACAO - Company Config Notifications
# =============================================================================
# Recebemos esses payloads quando a empresa atualiza algo no painel Zellu
# =============================================================================

class KnowledgeFilePayload(BaseModel):
    """Arquivo da base de conhecimento (webhook)."""
    id: Optional[str] = None
    fileName: str = Field(..., alias="file_name")
    fileSize: str = Field(..., alias="file_size")
    fileType: str = Field(..., alias="file_type")
    folder: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    minioPath: str = Field(..., alias="minio_path")

    class Config:
        populate_by_name = True


class KnowledgeBaseWebhookRequest(BaseModel):
    """
    Webhook: Base de Conhecimento (arquivos).
    Endpoint: POST /company_notify_knowledgebase_to_ia

    Recebido quando empresa faz upload ou deleta arquivos.

    Campos novos (10/12/2025):
    - knowledge_base_id: UUID da base de conhecimento
    - knowledge_base_name: Nome da base de conhecimento
    - is_default: Se e a base padrao da empresa
    """
    company_id: str = Field(..., alias="companyId")
    action: str  # "create" | "delete"
    knowledge_base_id: str = Field(..., alias="knowledgeBaseId", description="UUID da base de conhecimento")
    knowledge_base_name: str = Field(..., alias="knowledgeBaseName", description="Nome da base de conhecimento")
    is_default: bool = Field(default=False, alias="isDefault", description="Se e a base padrao")
    files: List[KnowledgeFilePayload]

    class Config:
        populate_by_name = True


class InstructionPayload(BaseModel):
    """Instrucao da empresa (webhook)."""
    id: Optional[str] = None
    persona: str
    rules: str
    legal: str
    budget: str
    tone: Optional[str] = None

    class Config:
        populate_by_name = True


class InstructionsWebhookRequest(BaseModel):
    """
    Webhook: Instrucoes de IA.
    Endpoint: POST /company_notify_instructions_to_ia

    Recebido quando empresa atualiza as 5 instrucoes.
    """
    company_id: str = Field(..., alias="companyId")
    action: str  # sempre "update"
    instruction: InstructionPayload

    class Config:
        populate_by_name = True


class TemporaryRuleWebhookPayload(BaseModel):
    """Regra temporaria (webhook)."""
    id: Optional[str] = None
    title: str
    rule_content: str = Field(..., alias="ruleContent")
    description: Optional[str] = None
    valid_from: str = Field(..., alias="validFrom")
    valid_until: str = Field(..., alias="validUntil")
    is_paused: bool = Field(default=False, alias="isPaused")

    class Config:
        populate_by_name = True


class TemporaryRulesWebhookRequest(BaseModel):
    """
    Webhook: Regras Temporarias.
    Endpoint: POST /company_notify_temporary_rules_to_ia

    Recebido quando empresa cria/atualiza/deleta/pausa regras.
    """
    company_id: str = Field(..., alias="companyId")
    action: str  # "create" | "update" | "delete" | "active" | "inactive"
    rule: TemporaryRuleWebhookPayload

    class Config:
        populate_by_name = True


class WebhookNotificationResponse(BaseModel):
    """Response padrao para webhooks de notificacao."""
    success: bool = True
    message: str = "Notification received"


# =============================================================================
# ZELLU API - Salvar system prompt
# =============================================================================

class SaveSystemPromptRequest(BaseModel):
    """
    Request para salvar system prompt na Zellu.
    POST /api/ai/system-prompt
    """
    companyId: str = Field(..., alias="company_id")
    prompt: str
    extraInfo: Optional[Dict[str, Any]] = Field(default=None, alias="extra_info")

    class Config:
        populate_by_name = True


class SaveSystemPromptResponse(BaseModel):
    """
    Response ao salvar system prompt na Zellu.
    """
    success: bool
    data: Optional[Dict[str, Any]] = None
    previousPromptDeactivated: bool = Field(default=False, alias="previous_prompt_deactivated")
    error: Optional[str] = None

    class Config:
        populate_by_name = True


# =============================================================================
# NOVOS ENDPOINTS ZELLU (10/12/2025) - Download e Status de Processamento
# =============================================================================

class ProcessingStatusUpdateRequest(BaseModel):
    """
    Request para atualizar status de processamento de arquivo.
    POST /api/ai/knowledge-files/{fileId}/status

    Enviamos este payload para a Zellu informar o progresso do RAG.
    """
    status: str = Field(
        ...,
        description="Status: pending | in_progress | completed | failed | cancelled"
    )
    error: Optional[str] = Field(
        default=None,
        description="Mensagem de erro (obrigatorio se status=failed ou cancelled)"
    )
    totalChunks: Optional[int] = Field(
        default=None,
        alias="total_chunks",
        description="Total de chunks do documento"
    )
    chunksCompleted: Optional[int] = Field(
        default=None,
        alias="chunks_completed",
        description="Chunks processados com sucesso"
    )
    chunksFailed: Optional[int] = Field(
        default=None,
        alias="chunks_failed",
        description="Chunks que falharam"
    )

    @model_validator(mode='after')
    def validate_error_required(self):
        """Valida que error esta presente se status = failed ou cancelled."""
        if self.status in ["failed", "cancelled"] and not self.error:
            raise ValueError(f"Campo 'error' e obrigatorio quando status = {self.status}")
        return self

    class Config:
        populate_by_name = True


class ProcessingStatusFileInfo(BaseModel):
    """Informacoes do arquivo no response de status."""
    id: Optional[str] = None
    fileName: str = Field(..., alias="file_name")
    processingStatus: str = Field(..., alias="processing_status")
    processingError: Optional[str] = Field(default=None, alias="processing_error")
    totalChunks: Optional[int] = Field(default=None, alias="total_chunks")
    chunksCompleted: Optional[int] = Field(default=None, alias="chunks_completed")
    chunksFailed: Optional[int] = Field(default=None, alias="chunks_failed")
    lastProcessedAt: Optional[str] = Field(default=None, alias="last_processed_at")
    retryCount: Optional[int] = Field(default=None, alias="retry_count")
    lastRetryAt: Optional[str] = Field(default=None, alias="last_retry_at")

    class Config:
        populate_by_name = True


class ProcessingStatusResponse(BaseModel):
    """
    Response do endpoint de status de processamento.
    POST/GET /api/ai/knowledge-files/{fileId}/status
    """
    success: bool = True
    file: ProcessingStatusFileInfo

    class Config:
        populate_by_name = True


class ProcessingStatusGetResponse(BaseModel):
    """
    Response do GET /api/ai/knowledge-files/{fileId}/status.
    """
    file: ProcessingStatusFileInfo

    class Config:
        populate_by_name = True


# =============================================================================
# WEBHOOK /api/chat/webhook - Receber mensagens do Backend Zellu
# =============================================================================
# Conforme AI_SERVICE_WEBHOOK_FORMAT.md
# =============================================================================

class ChatWebhookGroupMessage(BaseModel):
    """
    Mensagem individual dentro de group_messages.
    Conforme AI_SERVICE_WEBHOOK_FORMAT.md seção "Group Messages".
    """
    message: str = Field(..., description="Texto da mensagem")
    message_type: str = Field(default="text", description="Tipo da mensagem")
    files: List[str] = Field(default_factory=list, description="URLs de arquivos")
    audio: Optional[str] = Field(default=None, description="URL de audio")
    delay: int = Field(default=0, description="Delay em ms antes de exibir")
    awaiting_continuation: bool = Field(
        default=False,
        alias="awaitingContinuation",
        description="Se true, aguarda mais mensagens"
    )
    is_finished: bool = Field(
        default=False,
        alias="isFinished",
        description="Se a analise foi concluida"
    )
    analysis_data: Optional[Dict[str, Any]] = Field(
        default=None,
        alias="analysisData",
        description="Dados da analise (obrigatorio se is_finished=true)"
    )

    class Config:
        populate_by_name = True


class ChatWebhookRequest(BaseModel):
    """
    Request do endpoint POST /api/chat/webhook.
    Conforme AI_SERVICE_WEBHOOK_FORMAT.md seção "REQUEST: Zellu → Servico IA".

    Este endpoint recebe mensagens do Backend Zellu para processamento pela IA.
    """
    # Identificacao
    id: Optional[str] = Field(default=None, description="UUID da mensagem (para rastreamento)")
    chat_id: str = Field(..., alias="chatId", description="UUID da sessao de chat")
    nome: Optional[str] = Field(default=None, description="Nome do usuario")

    # Conteudo
    message_type: str = Field(
        default="text",
        alias="messageType",
        description="Tipo: text, audio, file, mixed"
    )
    body_message: str = Field(
        default="",
        alias="bodyMessage",
        description="Texto da mensagem"
    )

    # Anexos (opcional)
    audio: Optional[List[str]] = Field(default=None, description="URLs de audios")
    files: List[str] = Field(default_factory=list, description="URLs de arquivos")

    # Metadata (batch - opcional)
    group_metadata: Optional[Dict[str, Any]] = Field(
        default=None,
        alias="groupMetadata",
        description="Metadata de mensagens em batch"
    )

    class Config:
        populate_by_name = True


class ChatWebhookAnalysisRecommendation(BaseModel):
    """Recomendacao dentro de analysis_data."""
    type: str = Field(..., description="amigavel | extrajudicial | judicial")
    score: float = Field(..., ge=0, le=10, description="Score de 0 a 10")
    reason: str = Field(..., description="Justificativa")


class ChatWebhookAnalysisUserInfo(BaseModel):
    """Informacoes do usuario em analysis_data."""
    name: Optional[str] = None
    cpf: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None


class ChatWebhookAnalysisOpposingParty(BaseModel):
    """Informacoes da parte contraria em analysis_data."""
    type: str = Field(default="pj", description="pf | pj")
    name: Optional[str] = None
    document: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None


class ChatWebhookAnalysisCaseDetails(BaseModel):
    """Detalhes do caso em analysis_data."""
    title: Optional[str] = None
    description: Optional[str] = None
    expectedSolution: Optional[str] = Field(default=None, alias="expected_solution")
    documents: List[str] = Field(default_factory=list)

    class Config:
        populate_by_name = True


class ChatWebhookAnalysisData(BaseModel):
    """
    Dados da analise completa.
    Conforme AI_SERVICE_WEBHOOK_FORMAT.md seção "analysis_data".
    """
    problem: str = Field(..., description="Descricao do problema identificado")
    rights: List[str] = Field(..., min_length=1, description="Array de direitos (minimo 1)")
    estimatedValue: float = Field(
        ...,
        alias="estimated_value",
        gt=0,
        description="Valor estimado em reais"
    )
    recommendations: List[ChatWebhookAnalysisRecommendation] = Field(
        ...,
        min_length=3,
        max_length=3,
        description="Exatamente 3 recomendacoes"
    )
    userInfo: Optional[ChatWebhookAnalysisUserInfo] = Field(
        default=None,
        alias="user_info"
    )
    opposingParty: Optional[ChatWebhookAnalysisOpposingParty] = Field(
        default=None,
        alias="opposing_party"
    )
    caseDetails: Optional[ChatWebhookAnalysisCaseDetails] = Field(
        default=None,
        alias="case_details"
    )

    class Config:
        populate_by_name = True


class ChatWebhookResponse(BaseModel):
    """
    Response do endpoint POST /api/chat/webhook.
    Confirmacao de recebimento.
    """
    success: bool = True
    message: str = "Mensagem recebida"
    chat_id: Optional[str] = Field(default=None, alias="chatId")
    message_id: Optional[str] = Field(default=None, alias="messageId")

    class Config:
        populate_by_name = True


# =============================================================================
# VALIDATE COMPANY - Buscar empresa na base de dados Zellu
# =============================================================================
# Conforme documentacao company-management/webhook-spec.md
# Endpoint: POST /api/ai/validate-company
# =============================================================================

class ValidateCompanyRequest(BaseModel):
    """
    Request para validar existencia de empresa.
    POST /api/ai/validate-company

    Busca por:
    - CNPJ (formatado ou nao, 14 digitos)
    - companyName (razao social, case-insensitive)
    - tradeName (nome fantasia, case-insensitive)

    Retorna apenas empresas com status = 'active'.
    """
    identifier: str = Field(
        ...,
        min_length=1,
        description="CNPJ, Nome da Empresa ou Nome Fantasia"
    )


class ValidateCompanyData(BaseModel):
    """Dados da empresa encontrada."""
    id: str = Field(..., description="UUID da empresa")
    seqId: Optional[str] = Field(default=None, alias="seq_id", description="ID sequencial")
    cnpj: str = Field(..., description="CNPJ formatado (XX.XXX.XXX/XXXX-XX)")
    companyName: str = Field(..., alias="company_name", description="Razao social")
    tradeName: Optional[str] = Field(default=None, alias="trade_name", description="Nome fantasia")
    segmentId: Optional[str] = Field(default=None, alias="segment_id", description="UUID do segmento")
    segmentName: Optional[str] = Field(default=None, alias="segment_name", description="Nome do segmento")
    status: str = Field(default="active", description="Status: active, pending_validation, suspended, inactive")
    shortDescription: Optional[str] = Field(default=None, alias="short_description")
    longDescription: Optional[str] = Field(default=None, alias="long_description")
    website: Optional[str] = None
    rating: Optional[str] = Field(default=None, description="Avaliacao media (0.00-5.00)")
    responseRate: Optional[int] = Field(default=None, alias="response_rate", description="Taxa de resposta (%)")
    resolutionRate: Optional[int] = Field(default=None, alias="resolution_rate", description="Taxa de resolucao (%)")
    responseTime: Optional[str] = Field(default=None, alias="response_time", description="Tempo medio de resposta")
    reputationLevel: Optional[str] = Field(
        default=None,
        alias="reputation_level",
        description="Nivel: bronze, silver, gold, platinum"
    )
    createdAt: Optional[str] = Field(default=None, alias="created_at")
    updatedAt: Optional[str] = Field(default=None, alias="updated_at")

    class Config:
        populate_by_name = True


class ValidateCompanyResponse(BaseModel):
    """
    Response do endpoint POST /api/ai/validate-company.

    Se empresa encontrada:
        success: true, data: {...}

    Se empresa nao encontrada:
        success: false, error: "mensagem"
    """
    success: bool = True
    data: Optional[ValidateCompanyData] = None
    error: Optional[str] = None

    class Config:
        populate_by_name = True


# =============================================================================
# VALIDATE USER EMAIL - Verificar se email existe na base Zellu
# =============================================================================
# Conforme documentacao user-management/webhook-spec.md
# Endpoint: POST /api/ai/validate-user-email
# =============================================================================

class ValidateUserEmailRequest(BaseModel):
    """
    Request para validar se email existe.
    POST /api/ai/validate-user-email
    """
    email: str = Field(
        ...,
        min_length=1,
        description="Email a ser verificado"
    )


class ValidateUserEmailResponse(BaseModel):
    """
    Response do endpoint POST /api/ai/validate-user-email.

    Success:
        success: true, exists: true/false

    Error:
        success: false, error: "mensagem"
    """
    success: bool = True
    exists: bool = Field(default=False, description="True se email existe na base")
    error: Optional[str] = None

    class Config:
        populate_by_name = True


# =============================================================================
# GET MESSAGE HISTORY FROM DB - Obter histórico de mensagens do DB
# =============================================================================
# Conforme documentacao CHAT_SESSION_CONTINUITY_API.md
# Endpoint: POST /api/ai/chat-data
# =============================================================================

class ChatDataRequest(BaseModel):
    """
    Request para obter historico de mensagens do DB.
    POST /api/ai/chat-data
    """
    session_id: str = Field(..., alias="sessionId", description="UUID da sessao da sessão")
    limit: Optional[int] = Field(default=50, description="Numero maximo de mensagens a retornar")

class ChatMessageData(BaseModel):
    """Dados de uma mensagem no historico."""
    session: Dict[str, Any] = Field(..., description="Dados da sessao")
    messageHistory: List[Dict[str, Any]] = Field(..., alias="messageHistory", description="Array de mensagens dados")
    userData: Optional[Dict[str, Any]] = Field(default=None, alias="userData", description="Dados do usuario")
    empresa: Optional[Dict[str, Any]] = Field(default=None, description="Dados da empresa")
    personalInfo: Optional[Dict[str, Any]] = Field(default=None, alias="personalInfo", description="Informacoes pessoais")

    class Config:
        populate_by_name = True
