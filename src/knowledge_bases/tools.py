# -*- coding: utf-8 -*-
"""
Knowledge Base Tools - Zellu IA Empresa

Define as ferramentas (tools/functions) que a IA pode usar para consultar
as bases de conhecimento. Compativel com OpenAI Function Calling.

Uso:
    from src.knowledge_bases.tools import (
        KNOWLEDGE_BASE_TOOLS,
        execute_tool_call,
        get_tools_description_for_prompt
    )

    # Passar tools para o LLM
    response = llm.invoke(messages, tools=KNOWLEDGE_BASE_TOOLS)

    # Executar tool call
    result = await execute_tool_call(tool_name, tool_args, company_id)
"""

from typing import List, Dict, Any, Optional
import json
import logging

from .service import knowledge_base_service
from .schemas import ConsumerRightCategory

logger = logging.getLogger(__name__)


# =============================================================================
# DEFINICOES DE TOOLS (FORMATO OPENAI)
# =============================================================================

KNOWLEDGE_BASE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_knowledge_base",
            "description": """Busca informacoes em uma base de conhecimento especifica.
Use esta funcao quando precisar consultar documentos da empresa ou informacoes juridicas.

Bases disponiveis da EMPRESA:
- politicas_gerais: Politicas de troca, devolucao, garantia, cancelamento
- produtos_servicos: Catalogo, especificacoes, precos
- contratos_termos: Termos de uso, contratos, SLAs
- processos_internos: Fluxos de atendimento, procedimentos
- faq: Perguntas frequentes
- casos_resolvidos: Historico de casos anteriores

Bases COMPARTILHADAS (juridicas):
- jurisprudencia: Decisoes judiciais sobre direito do consumidor
- legislacao: CDC, leis e normas aplicaveis""",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Texto da busca - o que voce quer encontrar"
                    },
                    "base_id": {
                        "type": "string",
                        "description": "ID da base para buscar",
                        "enum": [
                            "politicas_gerais",
                            "produtos_servicos",
                            "contratos_termos",
                            "processos_internos",
                            "faq",
                            "casos_resolvidos",
                            "jurisprudencia",
                            "legislacao"
                        ]
                    }
                },
                "required": ["query", "base_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_jurisprudencia",
            "description": """Busca jurisprudencia (decisoes judiciais) com filtros especificos.
Use quando precisar de precedentes juridicos para fundamentar uma posicao.

Categorias disponiveis:
- cobranca_indevida: Casos de cobranca indevida
- vicio_produto: Produto com defeito
- vicio_servico: Servico mal prestado
- propaganda_enganosa: Publicidade enganosa
- negativacao_indevida: Nome negativado indevidamente
- atraso_entrega: Atraso na entrega
- cancelamento: Problemas com cancelamento
- garantia: Questoes de garantia
- danos_morais: Indenizacao por danos morais""",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Texto da busca"
                    },
                    "categoria": {
                        "type": "string",
                        "description": "Categoria do direito do consumidor",
                        "enum": [
                            "cobranca_indevida",
                            "vicio_produto",
                            "vicio_servico",
                            "propaganda_enganosa",
                            "negativacao_indevida",
                            "atraso_entrega",
                            "cancelamento",
                            "garantia",
                            "danos_morais"
                        ]
                    },
                    "decisao_favoravel_consumidor": {
                        "type": "boolean",
                        "description": "Se True, busca apenas decisoes favoraveis ao consumidor"
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_legislacao",
            "description": """Busca na legislacao (CDC, leis, sumulas).
Use quando precisar citar artigos de lei ou normas aplicaveis.""",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Texto da busca (ex: 'prazo troca produto', 'direito arrependimento')"
                    },
                    "categoria": {
                        "type": "string",
                        "description": "Categoria relacionada (opcional)",
                        "enum": [
                            "cobranca_indevida",
                            "vicio_produto",
                            "vicio_servico",
                            "propaganda_enganosa",
                            "negativacao_indevida",
                            "atraso_entrega",
                            "cancelamento",
                            "garantia",
                            "danos_morais"
                        ]
                    }
                },
                "required": ["query"]
            }
        }
    }
]


# =============================================================================
# EXECUTOR DE TOOLS
# =============================================================================

async def execute_tool_call(
    tool_name: str,
    tool_args: Dict[str, Any],
    company_id: str,
) -> str:
    """
    Executa uma chamada de tool e retorna o resultado formatado.

    Args:
        tool_name: Nome da tool a executar
        tool_args: Argumentos da tool
        company_id: ID da empresa (para bases privadas)

    Returns:
        String formatada com os resultados da busca
    """
    try:
        logger.info(f"[TOOL] Executando {tool_name} com args: {tool_args}")

        if tool_name == "search_knowledge_base":
            return await _execute_search_knowledge_base(tool_args, company_id)

        elif tool_name == "search_jurisprudencia":
            return await _execute_search_jurisprudencia(tool_args)

        elif tool_name == "search_legislacao":
            return await _execute_search_legislacao(tool_args)

        else:
            return f"Erro: Tool '{tool_name}' nao encontrada"

    except Exception as e:
        logger.error(f"[TOOL] Erro ao executar {tool_name}: {e}")
        return f"Erro ao buscar informacoes: {str(e)}"


async def _execute_search_knowledge_base(args: Dict, company_id: str) -> str:
    """Executa busca generica em base de conhecimento."""
    query = args.get("query", "")
    base_id = args.get("base_id", "")

    if not query or not base_id:
        return "Erro: query e base_id sao obrigatorios"

    # Bases compartilhadas nao precisam de company_id
    if base_id in ["jurisprudencia", "legislacao"]:
        results = knowledge_base_service.search(
            query=query,
            base_id=base_id,
            top_k=3,
            min_score=0.3,
        )
    else:
        results = knowledge_base_service.search(
            query=query,
            base_id=base_id,
            company_id=company_id,
            top_k=3,
            min_score=0.5,
        )

    return _format_search_results(results, base_id)


async def _execute_search_jurisprudencia(args: Dict) -> str:
    """Executa busca em jurisprudencia com filtros."""
    query = args.get("query", "")
    categoria_str = args.get("categoria")
    decisao_favoravel = args.get("decisao_favoravel_consumidor")

    if not query:
        return "Erro: query e obrigatoria"

    # Converter categoria para enum
    categoria = None
    if categoria_str:
        try:
            categoria = ConsumerRightCategory(categoria_str)
        except ValueError:
            pass

    results = knowledge_base_service.search_jurisprudencia(
        query=query,
        categoria=categoria,
        decisao_favoravel=decisao_favoravel,
        top_k=3,
    )

    return _format_jurisprudencia_results(results)


async def _execute_search_legislacao(args: Dict) -> str:
    """Executa busca em legislacao."""
    query = args.get("query", "")
    categoria_str = args.get("categoria")

    if not query:
        return "Erro: query e obrigatoria"

    # Converter categoria para enum
    categoria = None
    if categoria_str:
        try:
            categoria = ConsumerRightCategory(categoria_str)
        except ValueError:
            pass

    results = knowledge_base_service.search_legislacao(
        query=query,
        categoria=categoria,
        top_k=3,
    )

    return _format_legislacao_results(results)


# =============================================================================
# FORMATADORES DE RESULTADO
# =============================================================================

def _format_search_results(results: List[Dict], base_id: str) -> str:
    """Formata resultados genericos de busca."""
    if not results:
        return f"Nenhum documento encontrado na base '{base_id}'."

    formatted = [f"Encontrados {len(results)} documentos na base '{base_id}':\n"]

    for i, result in enumerate(results, 1):
        metadata = result.get("metadata", {})
        score = result.get("score", 0)
        title = metadata.get("title", "Sem titulo")
        content = metadata.get("content", "")[:500]

        formatted.append(f"[{i}] {title} (relevancia: {score:.0%})")
        formatted.append(f"    {content}")
        formatted.append("")

    return "\n".join(formatted)


def _format_jurisprudencia_results(results: List[Dict]) -> str:
    """Formata resultados de jurisprudencia."""
    if not results:
        return "Nenhuma jurisprudencia encontrada para esta busca."

    formatted = [f"Encontradas {len(results)} jurisprudencias relevantes:\n"]

    for i, result in enumerate(results, 1):
        metadata = result.get("metadata", {})
        score = result.get("score", 0)

        tribunal = metadata.get("tribunal", "")
        numero = metadata.get("numero_processo", "")
        titulo = metadata.get("title", "")
        decisao = metadata.get("decisao", "")
        valor_indenizacao = metadata.get("valor_indenizacao", 0)
        tese = metadata.get("tese_juridica", "")
        content = metadata.get("content", "")[:400]

        formatted.append(f"[{i}] {titulo}")
        formatted.append(f"    Tribunal: {tribunal} | Processo: {numero}")
        formatted.append(f"    Decisao: {decisao}")
        if valor_indenizacao:
            formatted.append(f"    Valor indenizacao: R$ {valor_indenizacao:,.2f}")
        if tese:
            formatted.append(f"    Tese: {tese}")
        formatted.append(f"    Resumo: {content}")
        formatted.append("")

    return "\n".join(formatted)


def _format_legislacao_results(results: List[Dict]) -> str:
    """Formata resultados de legislacao."""
    if not results:
        return "Nenhuma legislacao encontrada para esta busca."

    formatted = [f"Encontradas {len(results)} referencias legais:\n"]

    for i, result in enumerate(results, 1):
        metadata = result.get("metadata", {})
        score = result.get("score", 0)

        titulo = metadata.get("title", "")
        tipo = metadata.get("tipo", "")
        numero = metadata.get("numero", "")
        artigo = metadata.get("artigo", "")
        content = metadata.get("content", "")[:500]

        formatted.append(f"[{i}] {titulo}")
        if artigo:
            formatted.append(f"    {tipo.upper()} {numero} - Art. {artigo}")
        else:
            formatted.append(f"    {tipo.upper()} {numero}")
        formatted.append(f"    {content}")
        formatted.append("")

    return "\n".join(formatted)


# =============================================================================
# HELPER PARA SYSTEM PROMPT
# =============================================================================

def get_tools_description_for_prompt() -> str:
    """
    Gera descricao das tools para incluir no system prompt.

    Isso ajuda a IA a saber quais ferramentas tem disponiveis
    sem precisar ver a definicao completa da tool.
    """
    return """
FERRAMENTAS DISPONIVEIS PARA CONSULTA:

Voce tem acesso a ferramentas para buscar informacoes. Use-as quando precisar:

1. search_knowledge_base(query, base_id)
   - Busca em bases da empresa: politicas_gerais, produtos_servicos, contratos_termos, processos_internos, faq, casos_resolvidos
   - Busca em bases juridicas: jurisprudencia, legislacao

2. search_jurisprudencia(query, categoria?, decisao_favoravel_consumidor?)
   - Busca decisoes judiciais com filtros por categoria

3. search_legislacao(query, categoria?)
   - Busca artigos do CDC e outras leis

QUANDO USAR:
- Cliente pergunta sobre politica de troca → search_knowledge_base("troca produto", "politicas_gerais")
- Precisa de precedente juridico → search_jurisprudencia("cobranca indevida", "cobranca_indevida")
- Precisa citar lei → search_legislacao("prazo para troca", "vicio_produto")
"""
