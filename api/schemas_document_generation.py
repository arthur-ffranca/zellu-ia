# -*- coding: utf-8 -*-
"""
Schemas para geração de documentos jurídicos via IA.

Conforme docs/DOCUMENT_GENERATION_API.md:
- Request: Backend -> IA (action: generate_legal_document)
- Callback: IA -> Backend (ticketId, fileUrl, fileName)
"""

from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any


# ===== CONTEXTO DO REQUEST =====

class DocGenTicket(BaseModel):
    """Dados do ticket/caso."""
    title: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    estimatedValue: Optional[float] = None
    resolutionType: Optional[str] = None
    ticketNumber: Optional[str] = None


class DocGenClient(BaseModel):
    """Dados do cliente."""
    name: Optional[str] = None
    cpf: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None


class DocGenOpposingParty(BaseModel):
    """Dados da parte contrária."""
    name: Optional[str] = None
    document: Optional[str] = None


class DocGenCompany(BaseModel):
    """Dados da empresa (fluxo empresa → acordo)."""
    name: Optional[str] = None
    cnpj: Optional[str] = None


class DocGenChatMessage(BaseModel):
    """Mensagem do chat de abertura (ZelinhU)."""
    role: str
    content: str
    createdAt: Optional[str] = None


class DocGenNegotiationMessage(BaseModel):
    """Mensagem da negociação entre ZelinhU e Company AI."""
    role: str  # zelinhu, company_ai, company_manual, system
    content: str
    createdAt: Optional[str] = None


class DocGenDirectMessage(BaseModel):
    """Mensagem direta advogado <-> cliente."""
    senderRole: str
    content: str
    createdAt: Optional[str] = None


class DocGenAnalysisRecommendation(BaseModel):
    """Recomendação da análise."""
    type: str
    description: Optional[str] = None


class DocGenAnalysisData(BaseModel):
    """Dados da análise do caso (pode ser null se chat não finalizou)."""
    problem: Optional[str] = None
    rights: List[str] = Field(default_factory=list)
    recommendations: List[DocGenAnalysisRecommendation] = Field(default_factory=list)


class DocGenAttachment(BaseModel):
    """Arquivo anexo de origem para geração do documento."""
    fileId: str = Field(..., description="UUID do arquivo no backend Zellu")
    fileUrl: str = Field(..., description="URL absoluta para download do arquivo")
    fileName: str = Field(..., description="Nome original do arquivo")
    mimeType: str = Field(..., description="Tipo MIME do arquivo")


class DocGenContext(BaseModel):
    """Contexto completo enviado pelo backend."""
    documentType: Optional[str] = Field(
        None,
        description="Tipo de documento solicitado: 'petition', 'notification', 'agreement' ou 'power_of_attorney'. Se ausente, inferir do contexto."
    )
    customPrompt: Optional[str] = Field(None, description="Prompt customizado do usuário")
    ticket: Optional[DocGenTicket] = None
    client: Optional[DocGenClient] = None
    opposingParty: Optional[DocGenOpposingParty] = None
    company: Optional[DocGenCompany] = None
    openingChatMessages: List[DocGenChatMessage] = Field(default_factory=list)
    directMessages: List[DocGenDirectMessage] = Field(default_factory=list)
    negotiationMessages: List[DocGenNegotiationMessage] = Field(default_factory=list)
    analysisData: Optional[DocGenAnalysisData] = None
    attachments: List[DocGenAttachment] = Field(default_factory=list, description="Arquivos anexos de origem")


# ===== REQUEST PRINCIPAL =====

class DocumentGenerationRequest(BaseModel):
    """
    Request: Backend -> IA.

    Recebido no mesmo webhook (ticket-response) com action='generate_legal_document'.
    """
    action: str = Field("generate_legal_document", description="Sempre 'generate_legal_document'")
    ticketId: str = Field(..., description="UUID do ticket (obrigatório no callback)")
    generatedById: Optional[str] = Field(None, description="UUID do owner da empresa (indica fluxo empresa)")
    callbackUrl: str = Field(..., description="URL para chamar após gerar o documento")
    uploadUrl: str = Field(..., description="URL para upload do PDF")
    # Aceito e IGNORADO desde 17/09. A chave do upload e do callback passou a sair
    # da nossa configuracao (src/services/document_generation_service.py): mandar
    # um segredo nosso dentro do corpo de cada pedido obrigava a plataforma a
    # carrega-lo, e foi o que eles pediram para acabar em 15/09, §2.
    #
    # Continua OPCIONAL no schema, e nao removido, porque quem manda o campo hoje
    # e o backend deles: recusar o pedido com 422 por causa de um campo a mais
    # quebraria a geracao de documento no intervalo entre os dois deploys.
    apiKey: Optional[str] = Field(None, description="Obsoleto: aceito e ignorado")
    context: DocGenContext = Field(default_factory=DocGenContext)


# ===== CALLBACK IA -> BACKEND =====

class SourceValidation(BaseModel):
    """Validação do arquivo/prompt de origem."""
    fileId: str = Field(..., description="UUID do arquivo de origem validado")
    valid: bool = Field(..., description="True se válido para geração, False se rejeitado")
    rejectionReason: Optional[str] = Field(None, description="Motivo da rejeição (até 200 chars)")
    analysisDetails: Optional[Dict[str, Any]] = Field(None, description="Detalhes técnicos da análise")


class DocumentGenerationCallbackPayload(BaseModel):
    """
    Payload enviado pela IA ao callback do backend após gerar o documento.

    Campos obrigatórios: ticketId, fileUrl, fileName.
    """
    ticketId: str = Field(..., description="Mesmo ticketId recebido no request")
    sourceValidation: Optional[SourceValidation] = Field(None, description="Validação pré-geração (se rejeitado)")
    fileUrl: Optional[str] = Field(None, description="URL retornada pelo upload (MinIO) - presente se valid")
    fileName: Optional[str] = Field(None, description="Nome do arquivo (.pdf ou .docx) - presente se valid")
    fileSize: Optional[int] = Field(None, description="Tamanho do arquivo em bytes (BigInt no banco) - presente se valid")
    mimeType: Optional[str] = Field(
        None,
        description="Tipo MIME do arquivo. 'application/pdf' ou "
        "'application/vnd.openxmlformats-officedocument.wordprocessingml.document'. "
        "Backend Zellu faz fallback para PDF se ausente (retrocompatibilidade).",
    )
    documentSummary: Optional[str] = Field(None, description="Resumo do conteúdo do documento")
    messageBody: Optional[str] = Field(None, description="Mensagem exibida no chat direto")
    generatedById: Optional[str] = Field(None, description="UUID do owner da empresa (fluxo empresa)")
    documentPreview: Optional[str] = Field(None, description="Resumo ou preview curto do documento gerado")
    sourceFileRef: Optional[Dict[str, Any]] = Field(None, description="Referência ao arquivo de origem")
