# -*- coding: utf-8 -*-
"""
Configuracao das Bases de Conhecimento - Zellu IA Empresa

Define todas as bases de conhecimento disponiveis no sistema.
Estas definicoes sao usadas no system prompt para que a IA saiba onde buscar.

IMPORTANTE:
- Bases POR EMPRESA: Cada empresa tem suas proprias bases (namespace: company_{id})
- Bases COMPARTILHADAS: Acessiveis por todas as IAs (namespace: global)
"""

from typing import Dict, List, Optional
from dataclasses import dataclass
from enum import Enum


# =============================================================================
# ENUMS
# =============================================================================

class KnowledgeBaseType(str, Enum):
    """Tipo da base de conhecimento."""
    COMPANY = "company"      # Base privada da empresa
    SHARED = "shared"        # Base compartilhada (global)


# =============================================================================
# DATACLASS
# =============================================================================

@dataclass
class KnowledgeBaseConfig:
    """Configuracao de uma base de conhecimento."""
    id: str                           # ID unico da base
    name: str                         # Nome para exibicao
    description: str                  # Descricao para o system prompt
    type: KnowledgeBaseType           # Tipo: company ou shared
    metadata_filter: Optional[str] = None  # Filtro adicional de metadata


# =============================================================================
# BASES POR EMPRESA (namespace: company_{id})
# =============================================================================

COMPANY_KNOWLEDGE_BASES: List[KnowledgeBaseConfig] = [
    KnowledgeBaseConfig(
        id="politicas_gerais",
        name="Politicas Gerais",
        description="Politicas de troca, devolucao, garantia, cancelamento e reembolso da empresa",
        type=KnowledgeBaseType.COMPANY,
    ),
    KnowledgeBaseConfig(
        id="produtos_servicos",
        name="Produtos e Servicos",
        description="Catalogo de produtos, especificacoes tecnicas, precos e disponibilidade",
        type=KnowledgeBaseType.COMPANY,
    ),
    KnowledgeBaseConfig(
        id="contratos_termos",
        name="Contratos e Termos",
        description="Termos de uso, contratos, SLAs e condicoes gerais de servico",
        type=KnowledgeBaseType.COMPANY,
    ),
    KnowledgeBaseConfig(
        id="processos_internos",
        name="Processos Internos",
        description="Fluxos de atendimento, procedimentos operacionais e prazos",
        type=KnowledgeBaseType.COMPANY,
    ),
    KnowledgeBaseConfig(
        id="faq",
        name="FAQ",
        description="Perguntas frequentes e respostas padrao para duvidas comuns",
        type=KnowledgeBaseType.COMPANY,
    ),
    KnowledgeBaseConfig(
        id="casos_resolvidos",
        name="Historico de Casos",
        description="Casos anteriores resolvidos que servem como referencia",
        type=KnowledgeBaseType.COMPANY,
    ),
]


# =============================================================================
# BASES COMPARTILHADAS (namespace: global)
# =============================================================================

SHARED_KNOWLEDGE_BASES: List[KnowledgeBaseConfig] = [
    KnowledgeBaseConfig(
        id="jurisprudencia",
        name="Jurisprudencia",
        description="Decisoes judiciais de tribunais sobre direito do consumidor, com precedentes e entendimentos consolidados",
        type=KnowledgeBaseType.SHARED,
    ),
    KnowledgeBaseConfig(
        id="legislacao",
        name="Legislacao",
        description="Codigo de Defesa do Consumidor (CDC), leis e normas aplicaveis a relacoes de consumo",
        type=KnowledgeBaseType.SHARED,
    ),
]


# =============================================================================
# TODAS AS BASES (para referencia rapida)
# =============================================================================

ALL_KNOWLEDGE_BASES: List[KnowledgeBaseConfig] = COMPANY_KNOWLEDGE_BASES + SHARED_KNOWLEDGE_BASES


# =============================================================================
# FUNCOES AUXILIARES
# =============================================================================

def get_base_by_id(base_id: str) -> Optional[KnowledgeBaseConfig]:
    """
    Busca uma base de conhecimento pelo ID.

    Args:
        base_id: ID da base

    Returns:
        Configuracao da base ou None se nao encontrada
    """
    for base in ALL_KNOWLEDGE_BASES:
        if base.id == base_id:
            return base
    return None


def get_base_description(base_id: str) -> str:
    """
    Retorna a descricao de uma base de conhecimento.

    Args:
        base_id: ID da base

    Returns:
        Descricao da base ou string vazia se nao encontrada
    """
    base = get_base_by_id(base_id)
    return base.description if base else ""


def is_shared_base(base_id: str) -> bool:
    """
    Verifica se uma base e compartilhada (global).

    Args:
        base_id: ID da base

    Returns:
        True se for compartilhada, False se for por empresa
    """
    base = get_base_by_id(base_id)
    return base.type == KnowledgeBaseType.SHARED if base else False


def get_company_bases_for_prompt() -> str:
    """
    Gera texto das bases da empresa para incluir no system prompt.

    Returns:
        Texto formatado com lista de bases disponiveis
    """
    lines = ["Bases de conhecimento da empresa disponiveis para consulta:"]
    for base in COMPANY_KNOWLEDGE_BASES:
        lines.append(f"- {base.id}: {base.description}")
    return "\n".join(lines)


def get_shared_bases_for_prompt() -> str:
    """
    Gera texto das bases compartilhadas para incluir no system prompt.

    Returns:
        Texto formatado com lista de bases compartilhadas
    """
    lines = ["Bases de conhecimento compartilhadas (jurisprudencia e legislacao):"]
    for base in SHARED_KNOWLEDGE_BASES:
        lines.append(f"- {base.id}: {base.description}")
    return "\n".join(lines)


def get_all_bases_for_prompt() -> str:
    """
    Gera texto de todas as bases para incluir no system prompt.

    Returns:
        Texto formatado completo
    """
    return f"""
{get_company_bases_for_prompt()}

{get_shared_bases_for_prompt()}

IMPORTANTE: Para consultar uma base, use a funcao search_knowledge_base(base_id, query).
"""
