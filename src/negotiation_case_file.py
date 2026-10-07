# -*- coding: utf-8 -*-
"""
Ficha do caso compartilhada pelas duas IAs (Fase 2).

MOTIVACAO
---------
Hoje so a IA da Empresa recebe uma ficha do caso (src/negotiation_guardrails.py
::build_case_sheet). O ZelinhU recebe uma `problem_description` solta - e por
isso, no Caso 2 dos relatorios, ele tratou um contrato de desenvolvimento de
software como relacao de consumo e falou em "logistica reversa", "coleta" e
"substituicao do sistema" (achado Z-03).

Este modulo produz UMA ficha tipada, derivada do contexto do chamado, que os
dois lados leem. Alem dos dados do sistema, ela classifica tres coisas que
mudam completamente a negociacao:

    relacao   - b2c, b2b, trabalhista ou indefinida
    regime    - CDC, Codigo Civil, CLT ou a confirmar
    objeto    - produto, servico continuado, desenvolvimento, cobranca, obra

CLASSIFICAR SO QUANDO HA SINAL
------------------------------
O relatorio do Caso 2 aponta que a IA "tratou o CDC como aplicavel sem verificar
a natureza das partes e a finalidade economica do sistema". A correcao NAO e
chutar melhor: e admitir que nao sabe. Sem sinal claro, a ficha devolve
`indefinida` e instrui as duas IAs a confirmarem antes de invocar qualquer
regime - o que e exatamente o comportamento que os relatorios cobram.
"""

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from src.negotiation_math import parse_brl


# =============================================================================
# VOCABULARIO
# =============================================================================

RELATIONSHIP_B2C = "b2c"
RELATIONSHIP_B2B = "b2b"
RELATIONSHIP_LABOR = "trabalhista"
RELATIONSHIP_UNKNOWN = "indefinida"

REGIME_BY_RELATIONSHIP = {
    RELATIONSHIP_B2C: "Codigo de Defesa do Consumidor (CDC)",
    RELATIONSHIP_B2B: "Codigo Civil (relacao entre empresas)",
    RELATIONSHIP_LABOR: "CLT",
    RELATIONSHIP_UNKNOWN: "a confirmar",
}

OBJECT_PRODUCT = "produto"
OBJECT_CONTINUOUS_SERVICE = "servico continuado"
OBJECT_DEVELOPMENT = "desenvolvimento de software"
OBJECT_BILLING = "cobranca"
OBJECT_CONSTRUCTION = "obra"
OBJECT_UNKNOWN = "indefinido"

# Sinais de relacao entre empresas
_B2B_SIGNALS = [
    "cnpj", "contrato de prestacao de servicos", "contrato de desenvolvimento",
    "nossa empresa", "minha empresa", "a contratada", "a contratante",
    "razao social", "nota fiscal de servico", "b2b", "fornecedor homologado",
]

_LABOR_SIGNALS = [
    "demissao", "rescisao do contrato de trabalho", "verbas rescisorias",
    "fgts", "aviso previo", "horas extras", "carteira assinada", "clt",
    "assedio moral no trabalho", "empregador", "empregado",
]

# Sinais de relacao de consumo. Consumo exige destinatario final: por isso a
# lista e de situacoes tipicas de varejo, nao de palavras genericas.
_B2C_SIGNALS = [
    "comprei", "compra online", "loja", "e-commerce", "produto que recebi",
    "meu pedido", "consumidor", "procon", "cartao de credito pessoal",
    "assinatura mensal", "plano de saude", "fatura do meu",
]

_OBJECT_SIGNALS = [
    (OBJECT_DEVELOPMENT, [
        "desenvolvimento", "implantacao", "sistema de gestao", "software",
        "aplicativo", "codigo-fonte", "codigo fonte", "erp", "plataforma web",
        "sprint", "homologacao do sistema",
    ]),
    (OBJECT_CONSTRUCTION, ["obra", "reforma", "imovel na planta", "construtora", "empreendimento"]),
    (OBJECT_BILLING, ["cobranca indevida", "cobranca", "fatura", "debito automatico", "negativacao", "boleto"]),
    (OBJECT_CONTINUOUS_SERVICE, ["assinatura", "mensalidade", "plano", "franquia de dados", "recorrencia"]),
    (OBJECT_PRODUCT, ["produto", "aparelho", "entrega do produto", "mercadoria", "defeito de fabricacao"]),
]

# Categorias do ticket que ja indicam o objeto sem precisar ler a descricao
_CATEGORY_OBJECT = {
    "billing": OBJECT_BILLING,
    "cobranca": OBJECT_BILLING,
    "cobranca_indevida": OBJECT_BILLING,
    "product": OBJECT_PRODUCT,
    "produto": OBJECT_PRODUCT,
    "produto_defeituoso": OBJECT_PRODUCT,
    "delivery": OBJECT_PRODUCT,
    "entrega": OBJECT_PRODUCT,
    "atraso_entrega": OBJECT_PRODUCT,
    "service": OBJECT_CONTINUOUS_SERVICE,
    "servico": OBJECT_CONTINUOUS_SERVICE,
}


def _signal_pattern(signal: str) -> re.Pattern:
    """
    Sinal como palavra inteira, nunca como pedaco de outra palavra.

    Sem isso, "cobranca" casa com o sinal "obra" e um caso de cobranca indevida
    e classificado como obra. Substring nao serve para classificar texto.
    """
    return re.compile(r"\b" + re.escape(signal) + r"\b")


_COMPILED_SIGNALS = {}


def _has_signal(haystack: str, signals: List[str]) -> bool:
    for signal in signals:
        pattern = _COMPILED_SIGNALS.get(signal)
        if pattern is None:
            pattern = _signal_pattern(signal)
            _COMPILED_SIGNALS[signal] = pattern
        if pattern.search(haystack):
            return True
    return False


def _norm(text: Any) -> str:
    stripped = "".join(
        c for c in unicodedata.normalize("NFD", str(text or ""))
        if unicodedata.category(c) != "Mn"
    )
    return re.sub(r"\s+", " ", stripped).strip().lower()


def _first_filled(*values: Any) -> str:
    for value in values:
        if value not in (None, "", []):
            return str(value).strip()
    return ""


# =============================================================================
# FICHA
# =============================================================================

@dataclass(frozen=True)
class CaseFile:
    """Fonte unica de verdade sobre o caso, lida pelas duas IAs."""

    ticket_number: str = ""
    title: str = ""
    description: str = ""
    category: str = ""
    case_value: Optional[Decimal] = None
    client_name: str = ""
    company_name: str = "Empresa"
    relationship: str = RELATIONSHIP_UNKNOWN
    legal_regime: str = REGIME_BY_RELATIONSHIP[RELATIONSHIP_UNKNOWN]
    object_kind: str = OBJECT_UNKNOWN
    client_rights: Tuple[str, ...] = ()

    @property
    def regime_confirmed(self) -> bool:
        return self.relationship != RELATIONSHIP_UNKNOWN

    def render(self, audience: str = "empresa") -> str:
        """
        Bloco de texto para o system prompt.

        Args:
            audience: "empresa" ou "cliente" - muda so a linha de quem e quem
        """
        lines = ["# FICHA DO CASO (FONTE UNICA DE VERDADE)", ""]
        lines.append(
            "Os dados abaixo vem do sistema, nao da conversa. Nunca contradiga, "
            "complemente ou substitua estes dados com informacao inferida das mensagens."
        )
        lines.append("")

        if audience == "cliente":
            lines.append(f"- Voce representa: {self.client_name or 'o cliente'}")
            lines.append(f"- Parte contraria: {self.company_name}")
        else:
            lines.append(f"- Empresa que voce representa e assina: {self.company_name}")
            if self.client_name and _norm(self.client_name) != "cliente":
                lines.append(f"- Cliente (use exatamente este nome): {self.client_name}")
            else:
                lines.append(
                    "- Cliente: nome NAO informado pelo sistema. Nao invente nome e "
                    "nao use nenhum nome proprio para tratar o cliente."
                )

        if self.ticket_number:
            lines.append(f"- Chamado: {self.ticket_number}")
        if self.title:
            lines.append(f"- Titulo: {self.title}")

        if self.case_value is not None and self.case_value > 0:
            lines.append(f"- Valor estimado da causa: R$ {self.case_value:.2f}")
        else:
            lines.append("- Valor estimado da causa: NAO informado pelo sistema.")

        lines.append(f"- Categoria: {self.category or 'nao informada'}")
        lines.append("")

        # --- natureza da relacao ------------------------------------------
        lines.append("## NATUREZA DA RELACAO E REGIME APLICAVEL")
        lines.append(f"- Relacao: {self.relationship}")
        lines.append(f"- Regime: {self.legal_regime}")
        lines.append(f"- Objeto: {self.object_kind}")

        if not self.regime_confirmed:
            lines.append("")
            lines.append(
                "ATENCAO: o sistema NAO conseguiu determinar a natureza da relacao. "
                "Nao invoque CDC, Codigo Civil ou CLT como se fosse certo. Pergunte "
                "quem sao as partes e qual a finalidade economica da contratacao "
                "antes de fundamentar qualquer pedido em um regime especifico."
            )
        elif self.relationship == RELATIONSHIP_B2B:
            lines.append("")
            lines.append(
                "Esta e uma relacao entre empresas: o CDC NAO se aplica por padrao. "
                "Use o Codigo Civil e o contrato entre as partes."
            )

        if self.object_kind == OBJECT_DEVELOPMENT:
            lines.append("")
            lines.append(
                "O objeto e desenvolvimento de software. NAO use vocabulario de "
                "produto fisico (logistica reversa, coleta, devolucao ou "
                "substituicao do item). Fale em escopo, entregas, codigo-fonte, "
                "acessos, documentacao e ambiente."
            )

        lines.append("")
        lines.append("## OBJETO DO CASO (ANCORA - CONGELADO)")
        lines.append(self.description or self.title or (
            "Objeto nao descrito no sistema. Antes de negociar qualquer clausula, "
            "peca a outra parte que delimite por escrito o objeto do caso."
        ))
        lines.append("")
        lines.append(
            "Somente pedidos com vinculo demonstrado com este objeto podem ser "
            "negociados nesta sessao."
        )

        if self.client_rights:
            lines.append("")
            lines.append("## DIREITOS IDENTIFICADOS PARA O CLIENTE")
            for right in self.client_rights:
                lines.append(f"- {right}")

        return "\n".join(lines)


# =============================================================================
# CLASSIFICACAO
# =============================================================================

def _detect_relationship(haystack: str, category: str) -> str:
    if _has_signal(haystack, _LABOR_SIGNALS):
        return RELATIONSHIP_LABOR
    if "labor" in category or "trabalhista" in category:
        return RELATIONSHIP_LABOR

    b2b = _has_signal(haystack, _B2B_SIGNALS)
    b2c = _has_signal(haystack, _B2C_SIGNALS)

    # Sinal dos dois lados ou de nenhum: nao ha como afirmar
    if b2b == b2c:
        return RELATIONSHIP_UNKNOWN
    return RELATIONSHIP_B2B if b2b else RELATIONSHIP_B2C


def _detect_object(haystack: str, category: str) -> str:
    for kind, signals in _OBJECT_SIGNALS:
        if _has_signal(haystack, signals):
            return kind

    return _CATEGORY_OBJECT.get(category, OBJECT_UNKNOWN)


def build_case_file(
    context: Optional[Dict[str, Any]] = None,
    client_rights: Optional[List[str]] = None,
    company_name: str = "",
    client_name: str = "",
) -> CaseFile:
    """
    Monta a ficha do caso a partir do contexto enviado pelo backend Zellu.

    Args:
        context: {ticket, client, company}
        client_rights: direitos ja resolvidos (src/utils/client_rights.py)
        company_name: sobrescreve o nome da empresa do contexto
        client_name: sobrescreve o nome do cliente do contexto
    """
    context = context or {}
    ticket = context.get("ticket") or {}
    company = context.get("company") or {}
    client = context.get("client") or {}

    title = _first_filled(ticket.get("subject"), ticket.get("title"))
    description = _first_filled(ticket.get("description"))
    category = _norm(ticket.get("category"))

    case_value = None
    raw_value = ticket.get("estimatedValue")
    if raw_value in (None, ""):
        raw_value = ticket.get("estimated_value")
    if raw_value not in (None, ""):
        candidate = None
        if isinstance(raw_value, str):
            candidate = parse_brl(raw_value.replace("R$", "").strip())
        if candidate is None:
            try:
                candidate = Decimal(str(raw_value))
            except Exception:  # noqa: BLE001
                candidate = None
        if candidate is not None and candidate > 0:
            case_value = candidate

    haystack = _norm(f"{title} {description} {category}")
    relationship = _detect_relationship(haystack, category)
    object_kind = _detect_object(haystack, category)

    return CaseFile(
        ticket_number=_first_filled(ticket.get("number"), ticket.get("seqId"), ticket.get("id")),
        title=title,
        description=description,
        category=_first_filled(ticket.get("category")),
        case_value=case_value,
        client_name=_first_filled(client_name, client.get("name")),
        company_name=_first_filled(company_name, company.get("tradeName"), company.get("name")) or "Empresa",
        relationship=relationship,
        legal_regime=REGIME_BY_RELATIONSHIP[relationship],
        object_kind=object_kind,
        client_rights=tuple(client_rights or ()),
    )
