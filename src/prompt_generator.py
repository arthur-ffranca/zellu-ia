# -*- coding: utf-8 -*-
"""
Gerador de System Prompt para empresas.

Responsavel por:
1. Receber instructions + documentos da empresa
2. Gerar um system prompt completo e estruturado
3. Retornar o prompt pronto para salvar na Zellu
"""

from src.open_dots.negotiation.company import VOICE as COMPANY_VOICE, default_persona as default_company_persona
from typing import Optional, Dict, Any, List
from datetime import datetime


class PromptGenerator:
    """Gerador de system prompts para empresas."""

    def __init__(self):
        """Inicializa o gerador."""
        self.version = "1.0.0"

    def generate(
        self,
        company_id: str,
        company_name: str,
        instructions: Dict[str, Any],
        documents: Optional[List[Dict[str, Any]]] = None,
        temporary_rules: Optional[List[Dict[str, Any]]] = None
    ) -> Dict[str, Any]:
        """
        Gera o system prompt completo para uma empresa.

        Args:
            company_id: UUID da empresa
            company_name: Nome da empresa
            instructions: Dict com persona, rules, legal, budget, tone
            documents: Lista de documentos da empresa (opcional)
            temporary_rules: Lista de regras temporarias (opcional)

        Returns:
            Dict com prompt e metadados
        """
        # Extrair instructions
        persona = instructions.get("persona", "")
        rules = instructions.get("rules", "")
        legal = instructions.get("legal", "")
        budget = instructions.get("budget", "")
        tone = instructions.get("tone", "profissional")

        # Construir o prompt
        prompt_parts = []

        # 1. Persona/Identidade
        prompt_parts.append(f"""# IDENTIDADE
{persona}

Voce representa a empresa {company_name} em negociacoes com clientes.""")

        # 2. Tom de voz
        if tone:
            prompt_parts.append(f"""
# TOM DE VOZ
Seu tom deve ser: {tone}
Mantenha sempre a cordialidade e profissionalismo.""")

        # 3. Regras de conduta
        prompt_parts.append(f"""
# REGRAS DE CONDUTA
{rules}""")

        # 4. Consideracoes legais
        prompt_parts.append(f"""
# CONSIDERACOES LEGAIS
{legal}

IMPORTANTE: Sempre respeite o CDC (Codigo de Defesa do Consumidor) e demais legislacoes aplicaveis.""")

        # 5. Orcamento e negociacao
        prompt_parts.append(f"""
# ORCAMENTO E NEGOCIACAO
{budget}

Lembre-se: O objetivo e resolver o problema do cliente de forma justa para ambas as partes.""")

        # 6. Regras temporarias (se houver)
        if temporary_rules:
            active_rules = [r for r in temporary_rules if r.get("is_active", True)]
            if active_rules:
                rules_text = "\n".join([f"- {r.get('content', '')}" for r in active_rules])
                prompt_parts.append(f"""
# REGRAS TEMPORARIAS (ATIVAS)
{rules_text}""")

        # 7. Estrategia de negociacao
        prompt_parts.append(_build_negotiation_strategy_section())

        # 8. Instrucao sobre consulta a base de conhecimento
        # NOTA: Documentos NAO sao incluidos no prompt - sao consultados via RAG durante a conversa
        prompt_parts.append("""
# BASE DE CONHECIMENTO
Voce tem acesso a documentos da empresa (politicas, manuais, FAQs, etc).
Quando precisar de informacoes especificas sobre produtos, politicas ou procedimentos,
a base de conhecimento sera consultada automaticamente para voce.""")

        # 9. Instrucoes finais
        prompt_parts.append("""
# INSTRUCOES FINAIS
1. NUNCA invente informacoes - use apenas o que esta na base de conhecimento
2. Se nao souber algo, diga que vai verificar ou escale para um humano
3. Quando fizer uma proposta concreta, pergunte se o cliente aceita
4. Se o cliente aceitar a proposta, confirme o acordo em poucas palavras e termine com o marcador [CASO FINALIZADO] (uso interno do sistema)
5. Se o cliente escalar ou mencionar advogado/procon/reclame aqui, escale para atendimento humano

""" + COMPANY_VOICE + """""")

        # Montar prompt final
        final_prompt = "\n".join(prompt_parts)

        return {
            "prompt": final_prompt,
            "extra_info": {
                "version": self.version,
                "company_id": company_id,
                "company_name": company_name,
                "generated_at": datetime.now().isoformat(),
                "has_documents": bool(documents),
                "documents_count": len(documents) if documents else 0,
                "has_temporary_rules": bool(temporary_rules),
                "temporary_rules_count": len(temporary_rules) if temporary_rules else 0
            }
        }

    def _format_documents(self, documents: List[Dict[str, Any]]) -> str:
        """
        Formata os documentos para incluir no prompt.

        Args:
            documents: Lista de documentos

        Returns:
            String formatada com os documentos
        """
        if not documents:
            return ""

        formatted = []
        for i, doc in enumerate(documents, 1):
            title = doc.get("title", f"Documento {i}")
            content = doc.get("content", "")
            category = doc.get("category", "")

            if content:
                doc_text = f"## {title}"
                if category:
                    doc_text += f" [{category}]"
                doc_text += f"\n{content}"
                formatted.append(doc_text)

        return "\n\n".join(formatted)

    def generate_default_prompt(self, company_name: str) -> Dict[str, Any]:
        """
        Gera um prompt padrao quando nao ha instructions configuradas.

        Args:
            company_name: Nome da empresa

        Returns:
            Dict com prompt e metadados
        """
        default_instructions = {
            "persona": default_company_persona(company_name),
            "rules": "Responda de forma clara e objetiva. Nao prometa o que nao pode cumprir. Escale para humano se necessario.",
            "legal": "Siga as normas do CDC (Codigo de Defesa do Consumidor). Respeite prazos legais.",
            "budget": "Tente resolver dentro do valor estimado do ticket. Negocie de forma justa.",
            "tone": "profissional"
        }

        result = self.generate(
            company_id="default",
            company_name=company_name,
            instructions=default_instructions
        )

        result["extra_info"]["is_default"] = True

        return result


# Instancia global
prompt_generator = PromptGenerator()


# =============================================================================
# FUNCOES AUXILIARES (usadas pelo main.py)
# =============================================================================

def _build_negotiation_strategy_section() -> str:
    """Retorna a secao de estrategia de negociacao para o system prompt."""
    return """
# ESTRATEGIA DE NEGOCIACAO

Voce defende os interesses da empresa, mas sempre respeitando a lei.
Seu objetivo e encontrar a solucao mais JUSTA para AMBAS as partes, nao ceder a toda demanda.

## PRINCIPIO: OBRIGACAO LEGAL vs NEGOCIAVEL
Antes de responder qualquer demanda, CLASSIFIQUE mentalmente:

OBRIGACAO LEGAL (voce DEVE cumprir - nao negocie, resolva):
- Troca/reparo de produto com defeito dentro do prazo (30 dias nao duraveis, 90 dias duraveis - CDC Art. 18)
- Direito de arrependimento em 7 dias para compras online/fora da loja (CDC Art. 49)
- Devolucao em dobro de cobranca indevida comprovada (CDC Art. 42)
- Cumprimento de oferta/publicidade veiculada (CDC Art. 35)
- Informacao clara sobre produtos e servicos

NEGOCIAVEL (voce pode e DEVE argumentar):
- Valor de compensacao/indenizacao por danos morais
- Forma da solucao (voucher, credito, troca vs reembolso)
- Prazos alem do minimo legal
- Beneficios extras (frete gratis, desconto futuro)
- Pedidos sem comprovacao clara de dano

## COMO NEGOCIAR
1. ENTENDA antes de propor: Faca perguntas para entender o problema real. Peca detalhes, comprovantes, numero do pedido.
2. RECONHECA o problema: Demonstre empatia, mas nao assuma culpa antes de verificar.
3. VERIFIQUE a base legal: Use search_legislacao para confirmar se o pedido tem amparo legal.
4. PROPONHA com fundamento: Ofereca solucao conforme politica da empresa, citando a base legal quando aplicavel.
5. ARGUMENTE valores: Se o cliente pedir valor alto, use search_jurisprudencia para referenciar valores medios em casos similares.

## QUANDO RESISTIR
- Pedido de indenizacao com valor muito acima da jurisprudencia media
- Reclamacao sem comprovacao (peca comprovantes antes de ceder)
- Pedido fora do prazo legal (informe o prazo aplicavel com respeito)
- Demandas sem amparo legal (explique por que nao se aplica)
- Tentativa de intimidacao (mantenha o profissionalismo, nao ceda por pressao)

## QUANDO CEDER
- Obrigacao legal clara e comprovada
- Erro comprovado da empresa (assuma e resolva)
- Valor baixo onde negar custa mais que resolver
- Cliente apresentou documentacao solida

## POSTURA POR RODADA
- Rodada 1-2: Entenda o problema, peca detalhes, ofereca solucao conforme politica da empresa
- Rodada 3-4: Se necessario, melhore a oferta gradualmente COM justificativa
- Rodada 5+: Apresente oferta final firme, com fundamento legal e politicas da empresa

## USO PROATIVO DAS FERRAMENTAS JURIDICAS
ANTES de responder demandas sobre direitos ou valores:
- Use search_legislacao para verificar o que a lei realmente diz sobre o caso
- Use search_jurisprudencia para conhecer valores de referencia em decisoes judiciais similares
- Cite os artigos e referencias nas suas respostas para dar credibilidade

NAO aceite automaticamente qualquer demanda. Analise, fundamente e proponha."""


def build_legal_context_for_negotiation(category: str) -> str:
    """
    Retorna resumo compacto dos direitos aplicaveis para uma categoria juridica.

    Args:
        category: Categoria do caso (consumer, labor, civil, criminal ou equivalentes em portugues)

    Returns:
        String com resumo legal para incluir no prompt de negociacao
    """
    # Normalizar categoria
    cat = category.lower().strip() if category else ""

    # Mapeamento de termos para categorias
    consumer_terms = ["consumer", "consumidor", "consumer_law", "direito_consumidor", "cdc"]
    labor_terms = ["labor", "trabalhista", "labor_law", "direito_trabalhista", "clt"]
    civil_terms = ["civil", "direito_civil", "civil_law", "contrato", "contract"]
    criminal_terms = ["criminal", "penal", "direito_criminal", "crime", "discriminacao", "racismo"]

    if any(t in cat for t in consumer_terms):
        return """
REFERENCIA LEGAL - DIREITO DO CONSUMIDOR:
- CDC Art. 18: Produto com defeito - fornecedor tem 30 dias para reparo. Apos isso, consumidor escolhe: troca, abatimento ou devolucao do valor.
- CDC Art. 20: Servico inadequado - reexecucao, abatimento ou devolucao.
- CDC Art. 35: Oferta nao cumprida - consumidor pode exigir cumprimento, aceitar equivalente ou rescindir com devolucao.
- CDC Art. 39: Praticas abusivas proibidas (venda casada, recusa de atendimento, etc).
- CDC Art. 42: Cobranca indevida - devolucao em dobro do valor pago a mais + correcao monetaria.
- CDC Art. 49: Arrependimento - 7 dias para desistir de compra fora do estabelecimento (online, telefone). Devolucao integral incluindo frete.
- Garantia legal: 30 dias (nao duraveis) ou 90 dias (duraveis). Contratual complementa.
- Danos morais: jurisprudencia media R$ 2.000-10.000 para casos comuns de consumo (negativacao indevida, cobranca vexatoria).
- Juizados Especiais: causas ate 40 salarios minimos, sem advogado ate 20 SM."""

    elif any(t in cat for t in labor_terms):
        return """
REFERENCIA LEGAL - DIREITO TRABALHISTA:
- CLT Art. 477: Rescisao - prazo de 10 dias para pagamento das verbas rescinditórias.
- CLT Art. 487: Aviso previo - 30 dias + 3 dias por ano trabalhado (max 90 dias).
- CLT Art. 59: Horas extras - adicional minimo de 50% (100% domingos/feriados).
- CLT Art. 130: Ferias - 30 dias apos 12 meses, com 1/3 constitucional.
- FGTS: 8% do salario mensal. Multa de 40% na demissao sem justa causa.
- 13o salario: 1/12 por mes trabalhado.
- Estabilidade: gestante (confirmacao ate 5 meses apos parto), acidente de trabalho (12 meses apos alta).
- Assedio moral/sexual: indenizacao por danos morais (jurisprudencia media R$ 5.000-30.000)."""

    elif any(t in cat for t in civil_terms):
        return """
REFERENCIA LEGAL - DIREITO CIVIL:
- CC Art. 186-187: Ato ilicito e abuso de direito geram obrigacao de reparar.
- CC Art. 389-405: Inadimplemento contratual - perdas e danos, juros, correcao.
- CC Art. 421: Funcao social do contrato.
- CC Art. 422: Boa-fe objetiva nas relacoes contratuais.
- CC Art. 478: Onerosidade excessiva - possibilidade de revisao contratual.
- Prescricao: 3 anos para reparacao civil (Art. 206 §3), 5 anos para dividas (Art. 206 §5)."""

    elif any(t in cat for t in criminal_terms):
        return """
REFERENCIA LEGAL - DIREITO CRIMINAL:
- Lei 7.716/89: Crimes de racismo - inafiancavel e imprescritivel.
- Art. 140 §3 CP: Injuria racial - equiparada a racismo (Lei 14.532/2023).
- Art. 216-A CP: Assedio sexual.
- Art. 147-A CP: Perseguicao/stalking.
- CF Art. 5: Direitos fundamentais - igualdade, dignidade, proibicao de discriminacao.
NOTA: Casos criminais geralmente requerem via judicial, nao negociacao amigavel."""

    # Categoria nao identificada - retornar contexto generico
    return """
REFERENCIA LEGAL GERAL:
- Verifique a legislacao aplicavel usando search_legislacao antes de responder
- Consulte jurisprudencia com search_jurisprudencia para valores de referencia
- Diferencie obrigacoes legais (que devem ser cumpridas) de demandas negociaveis"""


def build_documents_context(documents: List[Dict[str, Any]]) -> str:
    """
    Constroi o contexto de documentos para o system prompt.

    Args:
        documents: Lista de documentos com id, title, content, category

    Returns:
        String formatada com os documentos
    """
    if not documents:
        return ""

    formatted = []
    for doc in documents:
        title = doc.get("title", "Documento")
        content = doc.get("content", "")
        category = doc.get("category", "")

        if content:
            doc_text = f"### {title}"
            if category:
                doc_text += f" [{category}]"
            doc_text += f"\n{content}"
            formatted.append(doc_text)

    if not formatted:
        return ""

    return "## BASE DE CONHECIMENTO\n\n" + "\n\n".join(formatted)


def build_system_prompt(
    instructions,  # CompanyInstructions model
    temporary_rules: Optional[List] = None,
    documents_context: Optional[str] = None,  # DEPRECATED: Ignorado - documentos sao consultados via RAG
    client_name: str = "Cliente"
) -> str:
    """
    Constroi o system prompt completo para a IA.

    Args:
        instructions: Objeto CompanyInstructions com persona, rules, legal, budget, tone
        temporary_rules: Lista de TemporaryRule (opcional)
        documents_context: DEPRECATED - Ignorado. Documentos sao consultados via RAG durante a conversa.
        client_name: Nome do cliente

    Returns:
        System prompt completo (apenas instructions + regras temporarias)
    """
    prompt_parts = []

    # 1. Identidade/Persona
    prompt_parts.append(f"""# IDENTIDADE
{instructions.persona}

Voce esta atendendo o cliente: {client_name}""")

    # 2. Tom de voz
    if instructions.tone:
        prompt_parts.append(f"""
# TOM DE VOZ
Seu tom deve ser: {instructions.tone}
Mantenha sempre a cordialidade e profissionalismo.""")

    # 3. Regras de conduta
    prompt_parts.append(f"""
# REGRAS DE CONDUTA
{instructions.rules}""")

    # 4. Consideracoes legais
    prompt_parts.append(f"""
# CONSIDERACOES LEGAIS
{instructions.legal}

IMPORTANTE: Sempre respeite o CDC (Codigo de Defesa do Consumidor) e demais legislacoes aplicaveis.""")

    # 5. Orcamento e negociacao
    prompt_parts.append(f"""
# ORCAMENTO E NEGOCIACAO
{instructions.budget}

Lembre-se: O objetivo e resolver o problema do cliente de forma justa para ambas as partes.""")

    # 6. Regras temporarias (se houver)
    if temporary_rules:
        active_rules = [r for r in temporary_rules if not getattr(r, 'is_paused', False)]
        if active_rules:
            rules_text = "\n".join([f"- {r.content}" for r in active_rules])
            prompt_parts.append(f"""
# REGRAS TEMPORARIAS (ATIVAS)
{rules_text}""")

    # 7. Instrucao sobre consulta a base de conhecimento
    # NOTA: Documentos NAO sao incluidos no prompt - sao consultados via RAG durante a conversa
    prompt_parts.append("""
# BASE DE CONHECIMENTO
Voce tem acesso a documentos da empresa (politicas, manuais, FAQs, etc).
Quando precisar de informacoes especificas sobre produtos, politicas ou procedimentos,
a base de conhecimento sera consultada automaticamente para voce.""")

    # 8. Instrucoes finais
    prompt_parts.append("""
# INSTRUCOES FINAIS
1. NUNCA invente informacoes - use apenas o que esta na base de conhecimento
2. Se nao souber algo, diga que vai verificar ou escale para um humano
3. Quando fizer uma proposta concreta, pergunte se o cliente aceita
4. Se o cliente aceitar a proposta, confirme o acordo em poucas palavras e termine com o marcador [CASO FINALIZADO] (uso interno do sistema)
5. Se o cliente escalar ou mencionar advogado/procon/reclame aqui, escale para atendimento humano

""" + COMPANY_VOICE + """""")

    # 9. Estrategia de negociacao
    prompt_parts.append(_build_negotiation_strategy_section())

    return "\n".join(prompt_parts)
