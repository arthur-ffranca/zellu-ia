# -*- coding: utf-8 -*-
"""
Aritmetica e coerencia de propostas de negociacao (Fase 1).

MOTIVACAO
---------
Os relatorios de avaliacao (30/07/2026) mostram que as falhas mais caras das
duas IAs nao sao de redacao, e sim de conta:

  - multa de R$ 5.000/dia com teto de R$ 3.700 (um unico dia ja estoura o teto)
  - teto de 15% ao mes convivendo com teto global de 10%
  - exposicao aceita de R$ 36.000 num caso de R$ 18.500
  - "100% + SELIC" e "95% a vista" aceitos na mesma mensagem

Os guardrails (Fase 1 por instrucao) mandam o modelo conferir a conta; o lint
(Fase 2) confere o texto. Faltava quem confere o NUMERO. Este modulo faz isso em
codigo puro: sem LLM, sem rede, sem estado.

ORGANIZACAO
-----------
  1. Dinheiro e percentual - parse de valores em pt-BR
  2. Estruturas            - OfferItem, NegotiationTerms, resultados
  3. evaluate_offer()      - soma a exposicao financeira total
  4. check_coherence()     - acha contas impossiveis
  5. extract_terms()       - le os termos de uma mensagem em texto livre

A separacao importa: evaluate_offer e check_coherence trabalham sobre dados
ESTRUTURADOS e sao o alvo dos testes. extract_terms e a ponte para o texto de
hoje - quando existir o ledger da negociacao (Fase 2), ele alimenta as mesmas
funcoes sem passar por regex.

LIMITE CONHECIDO DA EXTRACAO
----------------------------
extract_terms so extrai o que da para extrair com precisao alta: multa diaria,
teto, tetos percentuais por periodo/global, valores em reais, percentuais em
frases de aceite e mencao a arbitragem. Parcelamento e base de calculo de
percentual NAO sao extraidos do texto (dariam falso positivo demais) - as
verificacoes existem na API e ficam disponiveis para o ledger.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional


# =============================================================================
# 1. DINHEIRO E PERCENTUAL
# =============================================================================

# "R$ 5.000,00", "R$ 1.850", "R$ 18.500,00", "R$5000"
MONEY_RE = re.compile(r"R\$\s*(\d[\d.\s]*(?:,\d{1,2})?)", re.IGNORECASE)

PERCENT_RE = re.compile(r"(\d{1,3}(?:[.,]\d{1,2})?)\s*%")


def parse_brl(raw: str) -> Optional[Decimal]:
    """
    Converte um valor monetario em pt-BR para Decimal.

    >>> parse_brl("5.000,00")
    Decimal('5000.00')
    >>> parse_brl("1.850")
    Decimal('1850')
    >>> parse_brl("1850.50")
    Decimal('1850.50')
    """
    if not raw:
        return None

    cleaned = raw.strip().replace(" ", "")
    if "," in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d+\.\d{1,2}", cleaned):
        # "1850.50" (valor numerico serializado, ponto decimal). Antes os pontos
        # eram todos removidos e virava 185050 - teto da empresa 100x maior.
        pass
    else:
        cleaned = cleaned.replace(".", "")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def find_money(text: str) -> List[Decimal]:
    """Todos os valores em reais citados no texto, na ordem em que aparecem."""
    values = []
    for raw in MONEY_RE.findall(text or ""):
        value = parse_brl(raw)
        if value is not None:
            values.append(value)
    return values


def parse_percent(raw: str) -> Optional[Decimal]:
    """Converte '15' ou '15,5' (de '15%') para Decimal."""
    if not raw:
        return None
    try:
        value = Decimal(raw.strip().replace(",", "."))
    except InvalidOperation:
        return None
    return value if Decimal("0") <= value <= Decimal("100") else None


def _norm(text: str) -> str:
    """Sem acento, minusculo, espacos colapsados - mesma tecnica do lint."""
    stripped = "".join(
        c for c in unicodedata.normalize("NFD", text or "")
        if unicodedata.category(c) != "Mn"
    )
    return re.sub(r"\s+", " ", stripped).strip().lower()


def format_brl(value: Decimal) -> str:
    """Formata Decimal como R$ 1.234,56 (para mostrar a conta na resposta)."""
    quantized = value.quantize(Decimal("0.01"))
    inteiro, _, centavos = str(abs(quantized)).partition(".")
    grupos = []
    while len(inteiro) > 3:
        grupos.insert(0, inteiro[-3:])
        inteiro = inteiro[:-3]
    grupos.insert(0, inteiro)
    sinal = "-" if quantized < 0 else ""
    return f"{sinal}R$ {'.'.join(grupos)},{centavos or '00'}"


# =============================================================================
# 2. ESTRUTURAS
# =============================================================================

# Itens que somam exposicao financeira para a empresa
EXPOSURE_KINDS = (
    "principal",
    "multa",
    "juros",
    "correcao",
    "despesa",
    "lucro_cessante",
    "custo_terceiro",
)


@dataclass(frozen=True)
class OfferItem:
    """Uma obrigacao financeira dentro de uma proposta."""
    label: str
    amount: Decimal
    kind: str = "principal"

    def __post_init__(self):
        if self.kind not in EXPOSURE_KINDS:
            raise ValueError(
                f"kind invalido: {self.kind!r}. Use um de {EXPOSURE_KINDS}"
            )
        if self.amount < 0:
            raise ValueError("amount de OfferItem nao pode ser negativo")


@dataclass(frozen=True)
class PenaltyTerms:
    """Termos de uma clausula penal."""
    daily_amount: Optional[Decimal] = None
    cap: Optional[Decimal] = None
    # Dias de tolerancia antes da multa incidir. Sem informacao, a verificacao
    # usa 1 dia: se um unico dia ja estoura o teto, a clausula e inexequivel.
    cure_days: Optional[int] = None
    period_cap_percent: Optional[Decimal] = None
    global_cap_percent: Optional[Decimal] = None


@dataclass
class NegotiationTerms:
    """Tudo que uma mensagem afirma em numeros."""
    penalty: PenaltyTerms = field(default_factory=PenaltyTerms)
    installments: List[Decimal] = field(default_factory=list)
    declared_total: Optional[Decimal] = None
    accepted_percentages: List[Decimal] = field(default_factory=list)
    percentages_without_base: List[str] = field(default_factory=list)
    mentions_arbitration: bool = False
    money_values: List[Decimal] = field(default_factory=list)


@dataclass(frozen=True)
class CoherenceIssue:
    """Uma conta que nao fecha."""
    code: str
    detail: str
    math: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "detail": self.detail, "math": self.math}


@dataclass
class OfferEvaluation:
    """Resultado de evaluate_offer."""
    total_exposure: Decimal
    by_kind: Dict[str, Decimal]
    case_value: Optional[Decimal]
    exceeds_case_value: bool
    items: List[OfferItem]

    def summary(self) -> str:
        """Uma linha com a conta, para colocar na resposta ou no log."""
        partes = [f"{kind}={format_brl(total)}" for kind, total in sorted(self.by_kind.items())]
        linha = f"exposicao total {format_brl(self.total_exposure)}"
        if partes:
            linha += f" ({', '.join(partes)})"
        if self.case_value is not None:
            linha += f" | valor da causa {format_brl(self.case_value)}"
            if self.exceeds_case_value:
                linha += " | ACIMA DO VALOR DA CAUSA"
        return linha

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_exposure": float(self.total_exposure),
            "by_kind": {k: float(v) for k, v in self.by_kind.items()},
            "case_value": float(self.case_value) if self.case_value is not None else None,
            "exceeds_case_value": self.exceeds_case_value,
            "summary": self.summary(),
        }


# =============================================================================
# 3. EXPOSICAO FINANCEIRA
# =============================================================================

def evaluate_offer(
    items: List[OfferItem],
    case_value: Optional[Decimal] = None,
) -> OfferEvaluation:
    """
    Soma tudo que a empresa passaria a dever se aceitasse a proposta.

    E a regra R2 dos guardrails executada em codigo: principal + multas + juros
    + correcao + despesas + lucros cessantes + custos de terceiros.

    Args:
        items: obrigacoes financeiras da proposta
        case_value: valor estimado da causa (ficha do caso), se conhecido

    Returns:
        OfferEvaluation com o total, a quebra por tipo e se estourou a causa
    """
    by_kind: Dict[str, Decimal] = {}
    total = Decimal("0")

    for item in items:
        by_kind[item.kind] = by_kind.get(item.kind, Decimal("0")) + item.amount
        total += item.amount

    exceeds = case_value is not None and case_value > 0 and total > case_value

    return OfferEvaluation(
        total_exposure=total,
        by_kind=by_kind,
        case_value=case_value,
        exceeds_case_value=exceeds,
        items=list(items),
    )


# =============================================================================
# 4. COERENCIA
# =============================================================================

# Acima deste valor de causa a arbitragem deixa de ser automaticamente
# desproporcional. Referencia: taxa de abertura da Camara FGV citada no
# relatorio do Caso 3 (R$ 5.000, sem honorarios arbitrais) contra uma causa de
# R$ 18.500.
ARBITRATION_MIN_CASE_VALUE = Decimal("50000")


def check_coherence(
    terms: NegotiationTerms,
    case_value: Optional[Decimal] = None,
) -> List[CoherenceIssue]:
    """
    Verifica se os numeros de uma proposta sao possiveis entre si.

    Cada problema devolvido traz a conta pronta em `math`, para o texto da
    resposta poder mostrar o calculo em vez de so recusar.
    """
    issues: List[CoherenceIssue] = []
    penalty = terms.penalty

    # --- multa diaria x prazo de cura contra o teto -------------------------
    if penalty.daily_amount is not None and penalty.cap is not None:
        dias = penalty.cure_days if penalty.cure_days and penalty.cure_days > 0 else 1
        acumulado = penalty.daily_amount * dias

        if acumulado > penalty.cap:
            janela = f"{dias} dia" if dias == 1 else f"{dias} dias"
            issues.append(CoherenceIssue(
                code="MULTA_ACIMA_DO_TETO",
                detail=(
                    f"multa de {format_brl(penalty.daily_amount)}/dia com teto de "
                    f"{format_brl(penalty.cap)} e inexequivel: {janela} de atraso "
                    f"ja excede o teto"
                ),
                math=(
                    f"{format_brl(penalty.daily_amount)} x {dias} = "
                    f"{format_brl(acumulado)} > teto {format_brl(penalty.cap)}"
                ),
            ))

    # --- teto por periodo maior que o teto global ---------------------------
    if penalty.period_cap_percent is not None and penalty.global_cap_percent is not None:
        if penalty.period_cap_percent > penalty.global_cap_percent:
            issues.append(CoherenceIssue(
                code="TETO_PERIODO_MAIOR_QUE_GLOBAL",
                detail=(
                    f"teto por periodo ({penalty.period_cap_percent}%) e maior que o "
                    f"teto global ({penalty.global_cap_percent}%): o limite global "
                    f"impede que o limite do periodo seja atingido"
                ),
                math=f"{penalty.period_cap_percent}% por periodo > {penalty.global_cap_percent}% global",
            ))

    # --- soma das parcelas contra o total declarado ------------------------
    if terms.installments and terms.declared_total is not None:
        soma = sum(terms.installments, Decimal("0"))
        if soma != terms.declared_total:
            issues.append(CoherenceIssue(
                code="PARCELAS_DIFEREM_DO_TOTAL",
                detail=(
                    f"a soma das parcelas ({format_brl(soma)}) nao confere com o "
                    f"total declarado ({format_brl(terms.declared_total)})"
                ),
                math=(
                    f"{' + '.join(format_brl(p) for p in terms.installments)} = "
                    f"{format_brl(soma)} != {format_brl(terms.declared_total)}"
                ),
            ))

    # --- percentual sem base de calculo ------------------------------------
    for trecho in terms.percentages_without_base:
        issues.append(CoherenceIssue(
            code="PERCENTUAL_SEM_BASE",
            detail=f"percentual sem base de calculo explicita: {trecho}",
        ))

    # --- alternativas excludentes aceitas juntas ---------------------------
    distintos = sorted(set(terms.accepted_percentages))
    if len(distintos) > 1:
        issues.append(CoherenceIssue(
            code="ALTERNATIVAS_EXCLUDENTES",
            detail=(
                "a mensagem aceita mais de um percentual para a mesma obrigacao "
                f"({', '.join(f'{p}%' for p in distintos)}); alternativas se excluem "
                f"e uma delas precisa ser escolhida"
            ),
            math=" x ".join(f"{p}%" for p in distintos),
        ))

    # --- mecanismo de disputa desproporcional ------------------------------
    if terms.mentions_arbitration and case_value is not None and 0 < case_value < ARBITRATION_MIN_CASE_VALUE:
        issues.append(CoherenceIssue(
            code="DISPUTA_DESPROPORCIONAL",
            detail=(
                f"arbitragem proposta para uma causa de {format_brl(case_value)}: "
                f"as custas de abertura de camara arbitral costumam consumir parte "
                f"relevante do proprio valor discutido"
            ),
            math=f"valor da causa {format_brl(case_value)} < referencia {format_brl(ARBITRATION_MIN_CASE_VALUE)}",
        ))

    return issues


# =============================================================================
# 5. EXTRACAO A PARTIR DO TEXTO
# =============================================================================

_DAILY_PENALTY_PATTERNS = [
    # R$ 5.000,00/dia | R$ 5.000 por dia | R$ 200 ao dia
    re.compile(r"R\$\s*(\d[\d.\s]*(?:,\d{1,2})?)\s*(?:/|\s*por\s+|\s*ao\s+)\s*dia", re.IGNORECASE),
    # multa diaria de R$ 5.000
    re.compile(r"di[áa]ri[ao][^.]{0,40}?R\$\s*(\d[\d.\s]*(?:,\d{1,2})?)", re.IGNORECASE),
]

_CAP_PATTERNS = [
    re.compile(r"teto\s*(?:de\s*|global\s*de\s*)?R\$\s*(\d[\d.\s]*(?:,\d{1,2})?)", re.IGNORECASE),
    re.compile(r"limitad[oa]s?\s*a\s*R\$\s*(\d[\d.\s]*(?:,\d{1,2})?)", re.IGNORECASE),
    re.compile(r"limite\s*(?:de\s*|global\s*de\s*)?R\$\s*(\d[\d.\s]*(?:,\d{1,2})?)", re.IGNORECASE),
]

_PERIOD_CAP_RE = re.compile(
    r"(\d{1,3}(?:[.,]\d{1,2})?)\s*%\s*(?:ao|por)\s*m[eê]s|(\d{1,3}(?:[.,]\d{1,2})?)\s*%\s*mensa(?:l|is)",
    re.IGNORECASE,
)

_GLOBAL_CAP_RE = re.compile(
    r"(\d{1,3}(?:[.,]\d{1,2})?)\s*%\s*(?:global|do contrato|do valor do contrato)",
    re.IGNORECASE,
)

_ARBITRATION_RE = re.compile(
    r"\barbitragem\b|\bc[âa]mara\s+arbitral\b|\bju[íi]zo\s+arbitral\b",
    re.IGNORECASE,
)

# Verbos que caracterizam aceite - usados para achar percentuais aceitos
_ACCEPT_RE = re.compile(
    r"\b(?:aceit\w+|acord\w+|concord\w+|est[áa]\s+aceit\w+|homolog\w+)\b",
    re.IGNORECASE,
)

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?;])\s+|\n+")


def _first_match(patterns: List[re.Pattern], text: str) -> Optional[Decimal]:
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            return parse_brl(match.group(1))
    return None


def extract_terms(text: str) -> NegotiationTerms:
    """
    Le de uma mensagem os termos numericos que dao para extrair com seguranca.

    Ver "LIMITE CONHECIDO DA EXTRACAO" no topo do modulo: parcelamento e base de
    calculo de percentual ficam de fora de proposito.
    """
    text = text or ""

    period_cap = None
    period_match = _PERIOD_CAP_RE.search(text)
    if period_match:
        period_cap = parse_percent(period_match.group(1) or period_match.group(2))

    global_cap = None
    global_match = _GLOBAL_CAP_RE.search(text)
    if global_match:
        global_cap = parse_percent(global_match.group(1))

    penalty = PenaltyTerms(
        daily_amount=_first_match(_DAILY_PENALTY_PATTERNS, text),
        cap=_first_match(_CAP_PATTERNS, text),
        period_cap_percent=period_cap,
        global_cap_percent=global_cap,
    )

    # Percentuais em frases de aceite: e assim que "100% + SELIC" e "95% a vista"
    # aceitos na mesma mensagem viram detectaveis (Caso 1, rodada 4).
    accepted: List[Decimal] = []
    for sentence in _SENTENCE_SPLIT_RE.split(text):
        if not _ACCEPT_RE.search(sentence):
            continue
        for raw in PERCENT_RE.findall(sentence):
            value = parse_percent(raw)
            if value is not None:
                accepted.append(value)

    return NegotiationTerms(
        penalty=penalty,
        accepted_percentages=accepted,
        mentions_arbitration=bool(_ARBITRATION_RE.search(text)),
        money_values=find_money(text),
    )


def check_text_coherence(
    text: str,
    case_value: Optional[Decimal] = None,
) -> List[CoherenceIssue]:
    """Atalho: extrai os termos de uma mensagem e verifica a coerencia deles."""
    return check_coherence(extract_terms(text), case_value=case_value)
