# -*- coding: utf-8 -*-
"""
Modulo centralizado de direitos do cliente.

Motivacao (achado Z-01 da auditoria de 22/08/2026)
--------------------------------------------------
O estado do Sender (ZelinhU) era montado com `client_rights: []` fixo em
api/routes/webhooks_sender.py. Como os prompts de analise e de contraproposta
interpolam `", ".join(client_rights)`, o agente que representa o cliente
negociava com a lista de direitos VAZIA - o que explica a cobertura de direitos
2/10 (danos morais) e 1/10 (restituicao em dobro) nos relatorios de avaliacao.

A funcao `generate_rights_from_description` vivia apenas em src/main.py e era
usada so pelo fluxo de analise. Foi movida para ca para que os dois fluxos
(analise e negociacao) usem a mesma fonte.

IMPORTANTE - ausencia de fallback generico
------------------------------------------
Ao contrario da versao anterior em src/main.py, esta funcao NAO devolve dois
direitos genericos do CDC quando nenhuma palavra-chave casa. Devolver CDC por
padrao e o que levou o ZelinhU a tratar um contrato B2B de desenvolvimento de
software como relacao de consumo (Caso 2 dos relatorios).

Lista vazia e um sinal honesto: "os direitos ainda nao foram identificados,
investigue antes de propor". Quem precisa de um piso garantido (o payload da
analise, que exige rights > 0) aplica o proprio fallback - e src/main.py ja faz
isso logo apos a chamada.
"""

from typing import Any, Dict, List, Optional


def generate_rights_from_description(description: str) -> List[str]:
    """
    Gera lista de direitos baseado na descricao do problema.

    Deteccao por palavra-chave, sem fallback generico: se nada casar, devolve
    lista vazia (ver docstring do modulo).

    Args:
        description: Descricao do problema relatado pelo cliente

    Returns:
        Lista de direitos identificados (pode ser vazia)
    """
    rights = []
    description_lower = (description or "").lower()

    if not description_lower:
        return rights

    # Detectar palavras-chave e adicionar direitos relevantes
    if any(word in description_lower for word in ["produto", "compra", "comprei", "defeito", "quebrado", "danificado", "quebrou"]):
        rights.append("Direito a troca ou devolucao em ate 7 dias (Art. 49 CDC)")
        rights.append("Direito a produto sem vicio ou defeito (Art. 18 CDC)")

    if any(word in description_lower for word in ["entrega", "nao chegou", "atrasou", "atraso", "prazo"]):
        rights.append("Direito ao cumprimento do prazo de entrega (Art. 35 CDC)")

    if any(word in description_lower for word in [
        "cobranca indevida", "cobrança indevida", "cobrado indevid",
        "cobraram indevid", "valor indevid", "preco errado", "preço errado",
        "fatura indevida", "negativacao indevida", "negativação indevida",
    ]):
        rights.append("Direito a informacao clara sobre precos (Art. 6 CDC)")
        rights.append("Direito a devolucao em dobro de valores cobrados indevidamente (Art. 42 CDC)")

    if any(word in description_lower for word in [
        "queda", "cai ", "caí", "cair", "acidente", "machuquei", "ferimento",
        "lesao", "lesão", "ambulancia", "ambulância", "escada rolante",
    ]):
        rights.append("Direito a seguranca na prestacao do servico ou no estabelecimento (CDC Art. 6 e Art. 14)")
        rights.append("Direito a reparacao de danos materiais comprovados (CC Art. 927)")
        rights.append("Direito a indenizacao por danos morais, conforme gravidade e provas do caso")

    if any(word in description_lower for word in ["cancelamento", "cancelar", "desistir", "desisti"]):
        rights.append("Direito de arrependimento em 7 dias para compras online (Art. 49 CDC)")

    if any(word in description_lower for word in ["garantia", "conserto", "reparo"]):
        rights.append("Direito a garantia legal de 90 dias (Art. 26 CDC)")

    if any(word in description_lower for word in ["propaganda", "anuncio", "enganosa", "falsa"]):
        rights.append("Direito a protecao contra publicidade enganosa (Art. 37 CDC)")

    if any(word in description_lower for word in ["servico", "prestacao", "atendimento"]):
        rights.append("Direito a servico adequado e eficiente (Art. 20 CDC)")

    if any(word in description_lower for word in ["reembolso", "devolucao", "dinheiro", "estorno"]):
        rights.append("Direito a restituicao imediata de valores pagos (Art. 18 CDC)")
        rights.append("Direito a devolucao corrigida monetariamente (Art. 35 CDC)")

    # Discriminacao e Racismo
    if any(word in description_lower for word in ["racismo", "racista", "discriminacao", "preconceito", "cor da pele", "raca", "injuria racial", "xenofobia", "homofobia", "intolerancia religiosa"]):
        rights.append("Crime de racismo (Lei 7.716/89)")
        rights.append("Injuria racial (Art. 140 par. 3 CP)")
        rights.append("Indenizacao por danos morais (CF Art. 5, V e X)")

    # Assedio
    if any(word in description_lower for word in ["assedio", "assedio moral", "assedio sexual", "perseguicao", "intimidacao", "stalking"]):
        rights.append("Protecao contra assedio (CLT/CP)")
        rights.append("Indenizacao por danos morais")

    # Violencia e Agressao
    if any(word in description_lower for word in ["violencia", "agressao", "agredido", "agrediu", "bateu", "espancou", "lesao corporal", "ameaca de morte"]):
        rights.append("Protecao da integridade fisica (CF Art. 5)")
        rights.append("Indenizacao por danos morais e materiais")
        rights.append("Boletim de Ocorrencia")

    # Crimes gerais
    if any(word in description_lower for word in ["crime", "golpe", "fraude", "estelionato", "roubo", "furto"]):
        rights.append("Protecao contra praticas criminosas")
        rights.append("Reparacao de danos (Art. 927 CC)")
        rights.append("Boletim de Ocorrencia")

    return rights


def _rights_from_analysis(analysis: Any) -> List[str]:
    """Le direitos de um bloco de analise, aceitando dict ou objeto Pydantic."""
    if not analysis:
        return []

    for field in ("client_rights", "clientRights", "rights"):
        if isinstance(analysis, dict):
            value = analysis.get(field)
        else:
            value = getattr(analysis, field, None)

        if isinstance(value, (list, tuple)):
            found = [str(item).strip() for item in value if str(item).strip()]
            if found:
                return found

    return []


def resolve_client_rights(
    context: Optional[Dict[str, Any]] = None,
    analysis: Any = None,
) -> List[str]:
    """
    Resolve os direitos do cliente para uma negociacao.

    Prioridade:
      1. Direitos vindos da analise juridica (o backend Zellu ja os calculou)
      2. Deteccao a partir da descricao/titulo do ticket
      3. Lista vazia - sinal de que os direitos ainda precisam ser investigados

    Args:
        context: Contexto da sessao ({ticket, client, company, analysis, ...})
        analysis: Bloco de analise avulso, quando nao esta dentro do context

    Returns:
        Lista de direitos (pode ser vazia - ver docstring do modulo)
    """
    context = context or {}

    rights = _rights_from_analysis(analysis) or _rights_from_analysis(context.get("analysis"))
    if rights:
        return rights

    ticket = context.get("ticket") or {}
    description = " ".join(
        str(part)
        for part in (
            ticket.get("description"),
            ticket.get("subject") or ticket.get("title"),
        )
        if part
    )

    return generate_rights_from_description(description)


def format_rights_for_prompt(rights: Optional[List[str]]) -> str:
    """
    Formata a lista de direitos para interpolar em prompts.

    Quando a lista esta vazia, devolve uma instrucao explicita em vez de string
    vazia: um prompt com "Direitos do cliente:" seguido de nada faz o modelo
    inventar os direitos ou simplesmente ignora-los.
    """
    if not rights:
        return (
            "NAO IDENTIFICADOS ate agora. Antes de fixar valores ou clausulas, "
            "pergunte pelos fatos que permitem identificar os direitos aplicaveis "
            "e diga expressamente que eles seguem preservados."
        )

    return ", ".join(str(item) for item in rights if str(item).strip())
