# -*- coding: utf-8 -*-
"""
Knowledge Bases - Zellu IA Empresa

Modulo responsavel pela configuracao e gerenciamento das bases de conhecimento.

Estrutura:
- config.py: Definicao das bases disponiveis (IDs, descricoes)
- schemas.py: Schemas de documentos (jurisprudencia, legislacao)
- service.py: Servico de busca nas bases

Tipos de Bases:
1. BASES POR EMPRESA (namespace: company_{id})
   - Documentos privados de cada empresa

2. BASES COMPARTILHADAS (namespace: global)
   - Jurisprudencia e legislacao acessiveis por todas as IAs
"""

from .config import (
    COMPANY_KNOWLEDGE_BASES,
    SHARED_KNOWLEDGE_BASES,
    ALL_KNOWLEDGE_BASES,
    get_base_description,
    get_base_by_id,
    is_shared_base,
)

from .schemas import (
    JurisprudenciaDocument,
    LegislacaoDocument,
    KnowledgeBaseDocument,
    CompanyDocument,
    ConsumerRightCategory,
    TribunalType,
    DecisionType,
    LegislationType,
)

from .service import knowledge_base_service, KnowledgeBaseService
from .indexer import knowledge_base_indexer, KnowledgeBaseIndexer
from .tools import (
    KNOWLEDGE_BASE_TOOLS,
    execute_tool_call,
    get_tools_description_for_prompt,
)
from .chat_handler import (
    ChatWithToolsHandler,
    ToolCallLimitExceeded,
    chat_with_knowledge_base,
)

__all__ = [
    # Config
    "COMPANY_KNOWLEDGE_BASES",
    "SHARED_KNOWLEDGE_BASES",
    "ALL_KNOWLEDGE_BASES",
    "get_base_description",
    "get_base_by_id",
    "is_shared_base",
    # Schemas
    "JurisprudenciaDocument",
    "LegislacaoDocument",
    "KnowledgeBaseDocument",
    "CompanyDocument",
    # Enums
    "ConsumerRightCategory",
    "TribunalType",
    "DecisionType",
    "LegislationType",
    # Service
    "knowledge_base_service",
    "KnowledgeBaseService",
    # Indexer
    "knowledge_base_indexer",
    "KnowledgeBaseIndexer",
    # Tools (Function Calling)
    "KNOWLEDGE_BASE_TOOLS",
    "execute_tool_call",
    "get_tools_description_for_prompt",
    # Chat Handler
    "ChatWithToolsHandler",
    "ToolCallLimitExceeded",
    "chat_with_knowledge_base",
]
