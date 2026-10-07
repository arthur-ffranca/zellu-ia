# -*- coding: utf-8 -*-
"""
Mandato da empresa e gate de alcada (Fase 1).

MOTIVACAO
---------
Hoje o limite financeiro da empresa chega a IA como texto livre no campo
`instructions.budget` ("Tente resolver dentro do valor estimado do ticket.
Negocie de forma justa."). A regra R2 dos guardrails manda somar a exposicao e
parar quando ela estoura - mas quem decide se o total cabe e o proprio modelo,
na mesma passada em que redige. Resultado nos relatorios: exposicao de
R$ 36.000 aceita num caso de R$ 18.500, no mesmo minuto, sem nenhuma trava.

ESTE MODULO
-----------
Transforma esse limite em politica verificavel por codigo:

  - teto global (por padrao, o valor estimado da causa na ficha do caso)
  - lista de instrumentos que SEMPRE exigem aprovacao humana, qualquer que
    seja o valor (a mesma lista da regra R2)

E aplica o gate sobre o texto que a IA acabou de escrever, olhando apenas para
FRASES DE COMPROMISSO - "aceitamos", "pagaremos", "reembolsaremos". Valores que
a IA apenas cita ao repetir a proposta da outra parte nao entram na conta, que e
o que evita transformar toda mensagem em pendencia humana.

ESCOPO DA FASE 1
----------------
O mandato aqui e o minimo util: teto global + itens que exigem humano. Teto por
categoria, teto por rodada e catalogo de concessoes com custo dependem do ledger
da negociacao (Fase 2) e de configuracao por empresa.
"""

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from src.negotiation_math import find_money, format_brl, parse_brl


# =============================================================================
# INSTRUMENTOS QUE EXIGEM APROVACAO HUMANA
# =============================================================================

# Mesma lista da regra R2 dos guardrails. Cada entrada: chave -> (rotulo, regex)
SENSITIVE_INSTRUMENTS: Dict[str, Tuple[str, re.Pattern]] = {
    "arbitragem": (
        "arbitragem ou camara arbitral",
        re.compile(r"\barbitragem\b|\bc[âa]mara\s+arbitral\b|\bju[íi]zo\s+arbitral\b", re.IGNORECASE),
    ),
    "escrow": (
        "escrow / conta garantia",
        re.compile(r"\bescrow\b|\bconta\s+garantia\b|\bconta\s+cau[çc][ãa]o\b", re.IGNORECASE),
    ),
    "seguro_garantia": (
        "seguro-garantia",
        re.compile(r"\bseguro[-\s]?garantia\b|\bfian[çc]a\s+banc[áa]ria\b", re.IGNORECASE),
    ),
    "indenidade": (
        "clausula de indenidade",
        re.compile(r"\bindenidade\b|\bindenizar\s+e\s+isentar\b|\bhold\s+harmless\b", re.IGNORECASE),
    ),
    "propriedade_intelectual": (
        "cessao de propriedade intelectual",
        re.compile(
            r"\bcess[ãa]o\s+(?:de\s+|dos\s+|da\s+)?(?:propriedade\s+intelectual|c[óo]digo|direitos\s+autorais)\b"
            r"|\bpropriedade\s+intelectual\b",
            re.IGNORECASE,
        ),
    ),
    "sla_24x7": (
        "SLA 24x7 ou canal dedicado",
        re.compile(r"\b24\s*x\s*7\b|\b24/7\b|\bcanal\s+dedicado\b|\batendimento\s+dedicado\b", re.IGNORECASE),
    ),
    "vencimento_antecipado": (
        "vencimento antecipado",
        re.compile(r"\bvencimento\s+antecipado\b|\bantecipa[çc][ãa]o\s+do\s+vencimento\b", re.IGNORECASE),
    ),
    "danos_morais": (
        "indenizacao por danos morais",
        re.compile(r"\bdanos?\s+morais\b", re.IGNORECASE),
    ),
}

# Marcadores que indicam que o item NAO foi assumido nesta mensagem
# Com ou sem colchetes: a IA da empresa passou a escrever a posicao em texto
# corrido (05/10/2026), e "depende de aprovacao interna" sem colchete continua
# sendo ressalva.
DEFERRAL_MARKERS = re.compile(
    r"(?<!n[ãa]o )\[?depende de aprova[çc][ãa]o(?: interna)?\]?|\[recusado\]|\brecusamos\b"
    r"|\bn[ãa]o (?:aceitamos|podemos (?:aceitar|assumir|oferecer|conceder))\b"
    r"|\bsujeit[oa] (?:a|à) aprova[çc][ãa]o\b",
    re.IGNORECASE,
)

# Frases em que a empresa assume obrigacao. So o dinheiro que aparece aqui conta
# para a exposicao - o resto costuma ser repeticao da proposta da outra parte.
#
# A lista e de VERBOS conjugados de propósito. Substantivos como "reembolso",
# "ressarcimento" e "acordo" aparecem o tempo todo ao repetir o pedido do cliente
# ("recebemos a solicitacao de reembolso no valor de R$ 18.500") e transformariam
# toda mensagem em pendencia humana.
COMMITMENT_RE = re.compile(
    r"\baceit\w+\b"                                    # aceitamos, aceito, aceita
    r"|\bconcord\w+\b"                                 # concordamos, concordo
    r"|\bassumimos\b|\bassumiremos\b"
    r"|\bacordamos\b|\bestamos\s+de\s+acordo\b"
    r"|\bpagaremos\b|\bpagamos\b|\bpagar[áa]\b|\biremos\s+pagar\b"
    r"|\breembolsaremos\b|\breembolsamos\b"
    r"|\bressarciremos\b|\bressarcimos\b"
    r"|\bindenizaremos\b|\bindenizamos\b"
    r"|\barcaremos\b|\barcamos\b"
    r"|\boferecemos\b|\bofereceremos\b"
    r"|\bconstituiremos\b|\bconstitu[íi]mos\b"
    r"|\bcomprometemo\w*\b"
    r"|\[aceito\]",
    re.IGNORECASE,
)

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?;])\s+|\n+")


# =============================================================================
# MANDATO
# =============================================================================

@dataclass(frozen=True)
class CompanyMandate:
    """Limites dentro dos quais a IA da empresa pode fechar sozinha."""

    # Teto global de exposicao financeira. None = sem teto conhecido, e entao
    # toda obrigacao financeira depende de aprovacao humana (regra R2).
    global_cap: Optional[Decimal] = None
    # Instrumentos que exigem humano qualquer que seja o valor
    requires_human: Tuple[str, ...] = tuple(SENSITIVE_INSTRUMENTS.keys())
    # De onde veio o teto, para aparecer no log
    cap_source: str = ""

    def label_for(self, key: str) -> str:
        entry = SENSITIVE_INSTRUMENTS.get(key)
        return entry[0] if entry else key


def build_company_mandate(context: Optional[Dict[str, Any]] = None) -> CompanyMandate:
    """
    Monta o mandato minimo a partir do contexto do chamado.

    Teto global = valor estimado da causa. E o mesmo criterio que a regra R2 ja
    enuncia em texto: acima do valor da causa, o veredito obrigatorio e
    [DEPENDE DE APROVACAO INTERNA].
    """
    ticket = (context or {}).get("ticket") or {}
    raw_value = ticket.get("estimatedValue")
    if raw_value in (None, ""):
        raw_value = ticket.get("estimated_value")

    cap = None
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
            cap = candidate

    return CompanyMandate(
        global_cap=cap,
        cap_source="valor estimado da causa" if cap is not None else "nao informado",
    )


# =============================================================================
# GATE
# =============================================================================

@dataclass
class MandateDecision:
    """Resultado da conferencia de uma mensagem contra o mandato."""
    committed_amount: Decimal = Decimal("0")
    exceeds_cap: bool = False
    sensitive_without_approval: List[str] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)
    committed_sentences: List[str] = field(default_factory=list)

    @property
    def requires_human(self) -> bool:
        """Se True, a mensagem nao pode fechar o caso automaticamente."""
        return self.exceeds_cap or bool(self.sensitive_without_approval)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "committed_amount": float(self.committed_amount),
            "exceeds_cap": self.exceeds_cap,
            "sensitive_without_approval": list(self.sensitive_without_approval),
            "requires_human": self.requires_human,
            "reasons": list(self.reasons),
        }


def _commitment_sentences(text: str) -> List[str]:
    """Frases em que a empresa assume obrigacao."""
    return [
        sentence.strip()
        for sentence in _SENTENCE_SPLIT_RE.split(text or "")
        if sentence.strip() and COMMITMENT_RE.search(sentence)
    ]


def check_mandate(text: str, mandate: Optional[CompanyMandate] = None) -> MandateDecision:
    """
    Confere a resposta da IA da empresa contra o mandato.

    Args:
        text: resposta que a IA pretende enviar
        mandate: limites da empresa (None = nada a verificar)

    Returns:
        MandateDecision. requires_human=True significa que a mensagem assume algo
        fora da alcada e o caso nao pode ser fechado automaticamente.
    """
    decision = MandateDecision()

    if not text or not text.strip() or mandate is None:
        return decision

    sentences = _commitment_sentences(text)
    decision.committed_sentences = sentences

    # --- exposicao financeira assumida -------------------------------------
    total = Decimal("0")
    for sentence in sentences:
        for value in find_money(sentence):
            total += value
    decision.committed_amount = total

    if mandate.global_cap is not None:
        if total > mandate.global_cap:
            decision.exceeds_cap = True
            decision.reasons.append(
                f"exposicao assumida {format_brl(total)} acima do teto de "
                f"{format_brl(mandate.global_cap)} ({mandate.cap_source})"
            )
    elif total > 0:
        # Sem teto conhecido, qualquer obrigacao financeira depende de humano
        decision.exceeds_cap = True
        decision.reasons.append(
            f"obrigacao financeira de {format_brl(total)} sem teto de alcada definido "
            f"para este caso"
        )

    # --- instrumentos sensiveis assumidos sem ressalva ----------------------
    for key in mandate.requires_human:
        entry = SENSITIVE_INSTRUMENTS.get(key)
        if not entry:
            continue
        label, pattern = entry

        for sentence in sentences:
            if not pattern.search(sentence):
                continue
            if DEFERRAL_MARKERS.search(sentence):
                continue  # ja marcado como pendente ou recusado
            decision.sensitive_without_approval.append(key)
            decision.reasons.append(
                f"{label} assumido sem ressalvar que depende de aprovacao interna"
            )
            break

    return decision
