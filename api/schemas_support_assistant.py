# -*- coding: utf-8 -*-
"""
Schemas para o Support Assistant.

Fonte: docs/webhook-spec.md (contrato com o backend Zellu, v1.0).

Resumo do contrato:
    REQUEST  (Zellu -> IA): POST /webhook/support-assistant
    CALLBACK (IA -> Zellu): POST {callbackUrl do request} - URL fixa do
        backend (ex: https://zellu-app.com/api/support-assistant/webhook).

Diferencas vs chat de negociacao (importantes):
- Identificador: 'room_id' (NAO chat_id).
- NAO existe analysisData/recommendations - eh Q&A puro com RAG.
- profile_type/is_authenticated SAO enviados e a IA deve modular a
  resposta por perfil. Visitante deslogado => profile_type=null.
"""

from typing import List, Optional, Literal, Dict, Any

from pydantic import BaseModel, Field, ConfigDict


# ============================================================================
# REQUEST: Zellu -> IA
# ============================================================================

class GroupMetadata(BaseModel):
    """Metadata de batch (varias mensagens agrupadas)."""
    messageCount: int = Field(..., description="Numero de mensagens no batch")
    messageIds: List[str] = Field(default_factory=list)


# Perfis aceitos no contrato. None = visitante deslogado.
ProfileType = Literal[
    "client",
    "lawyer",
    "company_owner",
    "company_member",
    "company_admin",
    "admin",
]

MessageType = Literal["text", "audio", "file", "mixed"]


class SupportAssistantRequest(BaseModel):
    """Request do backend Zellu para o serviço de IA.

    Conforme webhook-spec.md §REQUEST. Campos nullable quando o contrato
    indica (profile_type) ou opcionais para suportar batch / sem anexo.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    # ── IDENTIFICACAO ──
    id: str = Field(..., description="UUIDv7 da mensagem (rastreamento/auditoria)")
    room_id: str = Field(..., description="UUIDv7 da sala (Socket.IO)")
    callbackUrl: str = Field(..., description="URL fixa do backend para devolver a resposta")

    # ── PERFIL / AUTENTICACAO ──
    profile_type: Optional[ProfileType] = Field(
        default=None,
        description="Perfil do usuario logado. None/ausente => visitante deslogado",
    )
    is_authenticated: bool = Field(..., description="True = logado, False = visitante anonimo")

    # ── CONTEUDO ──
    message_type: MessageType = Field(..., description="text | audio | file | mixed")
    message: str = Field("", description="Texto da pergunta. Pode ser vazio para audio/file puro")

    # ── ANEXOS (OPCIONAL) ──
    files: List[str] = Field(default_factory=list, description="URLs presigned de arquivos")
    audio: Optional[List[str]] = Field(default=None, description="URLs presigned de audios, ou null")

    # ── METADATA BATCH (OPCIONAL) ──
    group_metadata: Optional[GroupMetadata] = None


# ============================================================================
# CALLBACK: IA -> Zellu
# ============================================================================

class GroupMessage(BaseModel):
    """Bolha individual em uma resposta multi-bolha."""
    message: str = Field(..., description="Texto da bolha")
    message_type: Optional[MessageType] = Field(default="text")
    files: List[str] = Field(default_factory=list)
    audio: Optional[str] = Field(default=None, description="URL de audio (raro)")
    delay: int = Field(default=0, description="ms de 'Digitando...' antes de exibir")
    awaiting_continuation: bool = Field(
        default=False,
        description="True bloqueia inputs ate proxima bolha",
    )
    is_finished: bool = Field(default=False, description="True marca a ultima bolha como final")


class SupportAssistantCallbackPayload(BaseModel):
    """Payload do callback IA -> Zellu (resposta da IA).

    Conforme webhook-spec.md §CALLBACK. NAO incluir analysis_data - nao se
    aplica a este contrato.
    """

    model_config = ConfigDict(extra="ignore")

    room_id: str = Field(..., description="Mesmo room_id do request")
    message: str = Field(..., description="Texto da resposta da IA")
    is_finished: bool = Field(..., description="True = resposta final desta interacao")
    message_type: Optional[MessageType] = Field(default="text")
    files: List[str] = Field(default_factory=list)
    audio: Optional[str] = Field(default=None)
    messagedata: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Metadados livres (ex.: fontes/grounding) persistidos pelo backend",
    )
    group_messages: Optional[List[GroupMessage]] = Field(
        default=None,
        description="Bolhas sequenciais (opcional)",
    )
