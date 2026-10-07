# -*- coding: utf-8 -*-
"""
Lint deterministico da resposta da IA da Empresa (Fase 2).

MOTIVACAO
---------
A Fase 1 (src/negotiation_guardrails.py) age por INSTRUCAO: coloca os limites no
system prompt e confia que o modelo obedeca. Os relatorios de avaliacao mostram
que isso nao basta - no Caso 2 a IA assinou como "[Zellu - Assistencia ao
Cliente]" nas QUATRO rodadas, mesmo depois de recusas explicitas apontando o
erro. Instrucao sozinha nao trava comportamento.

Este modulo verifica o texto JA GERADO, antes de devolver ao backend Zellu:

  L1 PLACEHOLDER   - "[X]", "[Seu Nome]", "[Seu Cargo]", "____"   (Casos 1 e 2)
  L2 IDENTIDADE    - assinatura como Zellu/ZelinhU (parte contraria)  (Caso 2)
  L3 NOME INVENTADO- "Prezado Joao Paulo" sem o nome vir do sistema   (Caso 2)
  L4 ACEITE EM BLOCO - "concordamos com todos os termos" sem veredito item a item
  L5 EMOJI         - proibido pelas regras fixas de comportamento

REGRAS NUMERICAS E ESTRATEGICAS (Fase 1 - achados C-04 e Z-07)
--------------------------------------------------------------
As cinco regras acima olham a superficie do texto. As falhas que custam dinheiro
nos relatorios sao de conta e de estrategia, e passavam inteiras:

  L6 MATEMATICA    - multa x prazo acima do teto, teto do periodo maior que o
                     global, parcelas que nao somam o total    (Casos 2 e 3)
  L7 ALCADA        - exposicao acima do teto do mandato, ou instrumento que
                     exige humano assumido sem ressalva        (Caso 3: R$ 36k)
  L8 ALTERNATIVAS  - aceita "A ou B" ao mesmo tempo            (Caso 1: 100% e 95%)
  L9 FORA DE OBJETO- clausula de projeto (SLA, marcos, escopo) num caso de
                     cobranca, produto ou entrega              (Caso 1, rodada 4)
  L10 DESPROPORCAO - arbitragem para causa pequena             (Caso 3: FGV x R$ 18,5k)

L6, L8, L9 e L10 valem para as DUAS IAs: a multa impossivel e os tetos
incompativeis foram propostos pelo ZelinhU, nao pela empresa. Use
lint_company_response para a IA da Empresa e lint_sender_message para o ZelinhU.
L7 depende do mandato e so se aplica a empresa.

FLUXO DE APLICACAO (src/main.py)
--------------------------------
  1. lint na resposta do LLM
  2. se houver violacao -> UMA nova tentativa, com instrucao corretiva especifica
  3. se ainda houver violacao -> sanitiza o que da e registra alerta

A resposta NUNCA e bloqueada: devolver messageBody vazio quebraria o fluxo do
backend. O objetivo e corrigir, nao censurar.
"""

import re
import unicodedata
from decimal import Decimal
from typing import Any, Dict, List, Optional

from src.negotiation_math import check_text_coherence, format_brl
from src.negotiation_mandate import CompanyMandate, check_mandate


# =============================================================================
# NORMALIZACAO
# =============================================================================

def _strip_accents(text: str) -> str:
    """Remove acentos para comparacao (mesma tecnica ja usada em src/main.py)."""
    return "".join(
        c for c in unicodedata.normalize("NFD", text)
        if unicodedata.category(c) != "Mn"
    )


def _norm(text: str) -> str:
    """Normaliza para comparacao: sem acento, minusculo, espacos colapsados."""
    return re.sub(r"\s+", " ", _strip_accents(text or "")).strip().lower()


# =============================================================================
# MARCADORES OFICIAIS DO SISTEMA (colchetes permitidos)
# =============================================================================

SYSTEM_MARKERS = {
    # fechamento - src/main.py e api/routes/company_response.py dependem destes
    "caso finalizado",
    "acordo fechado",
    # vereditos da regra R1 dos guardrails
    "aceito",
    "aceito sob condicao",
    "contraproposta",
    "recusado",
    "depende de aprovacao interna",
    # marcadores emitidos pelo Sender (ZelinhU)
    "proposta para aprovacao do cliente",
    "negociacao encerrada - escalado",
    "negociacao encerrada - decisao do cliente",
}

VERDICT_MARKERS = {
    "aceito",
    "aceito sob condicao",
    "contraproposta",
    "recusado",
    "depende de aprovacao interna",
}

# Termos genericos que podem seguir uma saudacao sem configurar nome proprio
GENERIC_ADDRESSEES = {
    "cliente", "clientes", "senhor", "senhora", "senhores", "prezado", "prezada",
    "equipe", "representante", "zelinhu", "zellu", "usuario", "consumidor",
    "sr", "sra", "time", "todos",
}

# Conteudo entre colchetes que caracteriza campo por preencher
PLACEHOLDER_CONTENT = re.compile(
    r"^(?:"
    r"x+|n/?a|\.{2,}|_+|-+|\?+"                       # [X], [XX], [___], [...]
    r"|(?:seu|sua|seus|suas)\b.*"                     # [Seu Nome], [Sua Empresa]
    r"|(?:nome|cargo|data|valor|numero|no|n|cpf|cnpj|email|telefone|endereco)\b.*"
    r"|(?:inserir|preencher|informar|completar|definir|incluir)\b.*"
    r"|(?:contrato|protocolo|empresa|cliente|razao social|representante)\s*(?:n?o?\.?\s*)?[x\d_\.]*"
    r")$",
    re.IGNORECASE,
)

# Placeholders fora de colchetes
LOOSE_PLACEHOLDERS = [
    (re.compile(r"_{3,}"), "linha de preenchimento (___)"),
    (re.compile(r"\{\{[^}]{1,60}\}\}"), "template nao preenchido ({{...}})"),
    (re.compile(r"<\s*(?:inserir|preencher|nome|cargo|data|valor)[^>]{0,40}>", re.IGNORECASE),
     "campo por preencher (<inserir ...>)"),
    (re.compile(r"\bX{3,}\b"), "mascara XXX"),
]

# Saudacoes que costumam preceder o nome do destinatario.
# A saudacao e case-insensitive (?i:...), mas o nome capturado NAO: exigir
# inicial maiuscula e o que distingue nome proprio de palavra comum
# ("Prezado Joao Paulo" vira violacao; "prezado cliente" nao).
GREETING = re.compile(
    r"\b(?i:prezad[oa]s?|car[oa]s?|ol[aá]|sr\.?|sra\.?|senhor|senhora)\s*,?\s+"
    r"([A-ZÀ-Ú][\wÀ-ú]{1,20}(?:\s+[A-ZÀ-Ú][\wÀ-ú]{1,20})?)",
)

# Aceite generico em bloco
BULK_ACCEPTANCE = [
    r"concordamos com (?:todos|todas)\b",
    r"concordamos integralmente\b",
    r"aceitamos (?:todos|todas|integralmente|na integra)\b",
    r"aceitamos a proposta (?:integralmente|na integra|como apresentada)",
    r"estamos de acordo com (?:todos|todas|a totalidade)\b",
    r"(?:todos|todas) (?:os termos|as condicoes|as clausulas)[^.]{0,40}(?:acei|acord|de acordo)",
    r"de acordo com (?:todos|todas) (?:os|as)\b",
]

# Emoji - faixas conservadoras, para nao pegar simbolos matematicos comuns
EMOJI = re.compile(
    "["
    "\U0001F000-\U0001FAFF"   # pictogramas, emoticons, suplementares
    "\U00002600-\U000027BF"   # simbolos diversos e dingbats
    "\U00002B00-\U00002BFF"   # estrelas e setas decorativas
    "️"                  # variation selector (emoji presentation)
    "]"
)

SIGNATURE_ZONE_CHARS = 300
COUNTERPARTY = re.compile(r"\b(zellu|zelinhu|zellinhu)\b", re.IGNORECASE)


# =============================================================================
# RESULTADO
# =============================================================================

class LintViolation:
    """Uma violacao encontrada na resposta da IA."""

    def __init__(self, rule: str, detail: str, excerpt: str = "", sanitizable: bool = False):
        self.rule = rule
        self.detail = detail
        self.excerpt = excerpt
        self.sanitizable = sanitizable

    def __repr__(self) -> str:
        return f"<LintViolation {self.rule}: {self.detail}>"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule": self.rule,
            "detail": self.detail,
            "excerpt": self.excerpt,
            "sanitizable": self.sanitizable,
        }


# =============================================================================
# REGRAS
# =============================================================================

def _check_placeholders(text: str) -> List[LintViolation]:
    """L1 - campos por preencher que vazaram para a resposta final."""
    violations = []

    for content in re.findall(r"\[([^\[\]]{1,80})\]", text):
        normalized = _norm(content)
        if normalized in SYSTEM_MARKERS:
            continue
        # marcador do sistema com sufixo, ex: "[CASO FINALIZADO] - registrado"
        if any(normalized.startswith(marker) for marker in SYSTEM_MARKERS):
            continue
        if PLACEHOLDER_CONTENT.match(normalized) or len(normalized) <= 3:
            violations.append(LintViolation(
                rule="L1_PLACEHOLDER",
                detail=f"campo por preencher na resposta: [{content}]",
                excerpt=f"[{content}]",
            ))

    for pattern, description in LOOSE_PLACEHOLDERS:
        match = pattern.search(text)
        if match:
            violations.append(LintViolation(
                rule="L1_PLACEHOLDER",
                detail=description,
                excerpt=match.group(0)[:60],
            ))

    return violations


# Verbos de entrega em primeira pessoa: "enviaremos", "vamos encaminhar",
# "providenciaremos". A conjugacao no plural e a forma que o ZelinhU usa para
# falar de si ("nos, a representacao do cliente").
_VERBO_DE_ENTREGA = (
    r"(?:enviar|encaminhar|mandar|entregar|providenciar|disponibilizar|"
    r"apresentar|fornecer|remeter|anexar|juntar|indicar|informar|confirmar)"
)
_PROMESSA = re.compile(
    r"\b(?:"
    r"(?:vamos|iremos|podemos|vou)\s+" + _VERBO_DE_ENTREGA + r"|"
    + _VERBO_DE_ENTREGA + r"(?:emos|ei|amos)"
    r")\b",
    re.IGNORECASE,
)

# Itens que pertencem ao CLIENTE. A lista e curta e concreta de proposito: e o
# que separa "enviaremos nossa contraproposta" (legitimo) de "enviaremos as
# notas fiscais" (fala por quem nao esta na conversa).
_ITEM_DO_CLIENTE = re.compile(
    r"\b(?:"
    r"nota[s]?\s+fiscal|nota[s]?\s+fiscais|nfs?e?\b|nf/os|ordem\s+de\s+servico|"
    r"comprovante|recibo|laudo|boleto|extrato|"
    r"janela[s]?\s+de\s+(?:data|horario)|tres\s+janelas|disponibilidade\s+de\s+(?:data|horario)|"
    r"autorizacao|dados\s+bancarios|pix\b|agencia\s+e\s+conta|titularidade|"
    r"foto[s]?\s+do|video[s]?\s+do|codigo[s]?\s+obd|"
    r"documento[s]?\s+solicitado[s]?|documentacao\s+solicitada"
    r")\b",
    re.IGNORECASE,
)


# "Levarei o pedido a ele", "vou levar os pedidos a ele", "serao levados a ele"
# (auditoria QA de 10/09/2026). O ZelinhU prometendo LEVAR o item ao cliente -
# e nenhum codigo faz isso: quem leva e a solicitacao do sistema. So vale com o
# destino na mesma frase, porque "levaremos em conta a proposta" e legitimo.
_LEVAR = re.compile(
    r"\b(?:(?:vou|vamos|iremos)\s+leva(?:r|-l[oa]s?)|levarei|levaremos|"
    r"ser(?:a|ao)\s+levad[oa]s?)\b",
    re.IGNORECASE,
)
_ATE_O_CLIENTE = re.compile(
    r"\b(?:a|ao|ate|para)\s+(?:ele|ela|o\s+cliente|cliente)\b",
    re.IGNORECASE,
)


def _check_promessa_pelo_cliente(text: str) -> List[LintViolation]:
    """L11 - o ZelinhU prometendo, confirmando ou datando acao do cliente.

    AUDITORIA QA DE 08/09/2026
    --------------------------
    A empresa pediu NF/OS, comprovantes, janelas de horario, autorizacao para
    desmontagem e dados bancarios. O ZelinhU respondeu "Enviaremos hoje as NFs,
    comprovantes e janelas" e "Encaminharemos ... em ate 24h" - documentos que
    estao com o CLIENTE, prazo que so o CLIENTE pode assumir. A empresa seguiu
    pedindo os mesmos itens ate a ultima rodada, o que prova que nada foi
    entregue.

    A regra e por FRASE, e nao pelo texto inteiro: "enviaremos nossa
    contraproposta ate sexta" e legitimo, e so vira violacao quando o objeto
    prometido pertence ao cliente.
    """
    violations = []

    for frase in re.split(r"[.;!?\n]+", text):
        if not frase.strip():
            continue
        # Compara sem acento: o texto real vem com "autorizacao", "horario" e
        # "documentacao" acentuados, e um padrao sem acento nunca casaria.
        frase_norm = _strip_accents(frase)

        if _LEVAR.search(frase_norm) and _ATE_O_CLIENTE.search(frase_norm):
            violations.append(LintViolation(
                rule="L11_FALA_PELO_CLIENTE",
                detail=(
                    "promessa de levar o item ao cliente: quem leva e a solicitacao "
                    "do sistema, e a frase nao faz nada acontecer. Diga apenas que "
                    "o item depende do cliente"
                ),
                excerpt=frase.strip()[:120],
            ))
            continue

        if not _PROMESSA.search(frase_norm):
            continue
        achado = _ITEM_DO_CLIENTE.search(frase_norm)
        if not achado:
            continue

        violations.append(LintViolation(
            rule="L11_FALA_PELO_CLIENTE",
            detail=(
                f"promessa de entregar item que e do cliente ('{achado.group(0)}'): "
                f"os documentos estao com ele, e voce nao sabe se ele os tem. "
                f"Diga apenas que o item depende do cliente"
            ),
            excerpt=frase.strip()[:120],
        ))

    return violations


def _check_identity(text: str, company_name: str) -> List[LintViolation]:
    """L2 - assinatura com a identidade da parte contraria."""
    violations = []

    # colchete com o nome da contraparte, ex: "[Zellu - Assistencia ao Cliente]"
    for content in re.findall(r"\[([^\[\]]{1,80})\]", text):
        if COUNTERPARTY.search(content):
            violations.append(LintViolation(
                rule="L2_IDENTIDADE",
                detail=f"assinatura com identidade da parte contraria: [{content}]",
                excerpt=f"[{content}]",
                sanitizable=True,
            ))

    # zona de assinatura: final da mensagem
    signature_zone = text[-SIGNATURE_ZONE_CHARS:]
    match = COUNTERPARTY.search(signature_zone)
    if match and not any(v.rule == "L2_IDENTIDADE" for v in violations):
        violations.append(LintViolation(
            rule="L2_IDENTIDADE",
            detail=(
                f"mencao a '{match.group(0)}' na zona de assinatura; "
                f"a resposta deve ser assinada por {company_name}"
            ),
            excerpt=signature_zone[max(0, match.start() - 40):match.end() + 40].strip(),
            sanitizable=True,
        ))

    return violations


def _check_client_name(text: str, client_name: str) -> List[LintViolation]:
    """L3 - trata o cliente por um nome que nao veio do sistema."""
    violations = []

    known = _norm(client_name)
    if known in ("", "cliente"):
        known_tokens = set()
    else:
        known_tokens = set(known.split())

    for match in GREETING.finditer(text):
        addressed = match.group(1).strip()
        normalized = _norm(addressed)
        first_token = normalized.split()[0] if normalized.split() else ""

        if not first_token or first_token in GENERIC_ADDRESSEES:
            continue
        if known_tokens and first_token in known_tokens:
            continue

        if known_tokens:
            detail = (
                f"trata o cliente como '{addressed}', mas o nome informado pelo "
                f"sistema e '{client_name}'"
            )
        else:
            detail = (
                f"usa o nome '{addressed}' sem que o sistema tenha informado o "
                f"nome do cliente"
            )

        violations.append(LintViolation(
            rule="L3_NOME_INVENTADO",
            detail=detail,
            excerpt=match.group(0),
        ))

    return violations


def _check_bulk_acceptance(text: str) -> List[LintViolation]:
    """L4 - aceite generico do pacote inteiro, sem veredito item a item."""
    normalized = _norm(text)

    # se classificou item a item, uma frase de resumo nao e aceite em bloco
    if any(f"[{marker}]" in normalized for marker in VERDICT_MARKERS):
        return []

    for pattern in BULK_ACCEPTANCE:
        match = re.search(pattern, normalized)
        if match:
            return [LintViolation(
                rule="L4_ACEITE_EM_BLOCO",
                detail=(
                    "aceite generico do conjunto de pedidos, sem classificar "
                    "cada item (regra R1 dos guardrails)"
                ),
                excerpt=match.group(0),
            )]

    return []


def _check_emoji(text: str) -> List[LintViolation]:
    """L5 - emojis sao proibidos nas respostas."""
    found = EMOJI.findall(text)
    if not found:
        return []
    return [LintViolation(
        rule="L5_EMOJI",
        detail=f"resposta contem {len(found)} emoji(s), proibidos nas regras fixas",
        excerpt="".join(found[:5]),
        sanitizable=True,
    )]


# =============================================================================
# REGRAS NUMERICAS E ESTRATEGICAS (L6 - L10)
# =============================================================================

# Codigo devolvido por check_coherence -> regra do lint
_COHERENCE_RULE = {
    "MULTA_ACIMA_DO_TETO": "L6_MATEMATICA",
    "TETO_PERIODO_MAIOR_QUE_GLOBAL": "L6_MATEMATICA",
    "PARCELAS_DIFEREM_DO_TOTAL": "L6_MATEMATICA",
    "PERCENTUAL_SEM_BASE": "L6_MATEMATICA",
    "ALTERNATIVAS_EXCLUDENTES": "L8_ALTERNATIVAS",
    "DISPUTA_DESPROPORCIONAL": "L10_DESPROPORCAO",
}

# Instrumentos tipicos de contrato de projeto. Num caso de cobranca indevida ou
# produto com defeito eles sao sinal de saida de objeto (regra R5).
PROJECT_INSTRUMENTS = [
    (re.compile(r"\bSLA\b", re.IGNORECASE), "SLA"),
    (re.compile(r"\bmarcos?\s+(?:de\s+)?(?:entrega|projeto)\b|\bmarco\s+\d", re.IGNORECASE), "marcos de projeto"),
    (re.compile(r"\bcronograma\b", re.IGNORECASE), "cronograma"),
    (re.compile(r"\bescopo\s+(?:do\s+)?(?:projeto|t[ée]cnico)\b", re.IGNORECASE), "escopo de projeto"),
    (re.compile(r"\bhomologa[çc][ãa]o\b", re.IGNORECASE), "homologacao"),
    (re.compile(r"\bgo-?live\b", re.IGNORECASE), "go-live"),
    (re.compile(r"\baceite\s+t[áa]cito\b", re.IGNORECASE), "aceite tacito"),
]

# Categorias em que clausula de projeto nao tem vinculo com o objeto
SIMPLE_CASE_CATEGORIES = {
    "billing", "cobranca", "cobranca indevida", "cobranca_indevida",
    "product", "produto", "produto defeituoso", "produto_defeituoso",
    "delivery", "entrega", "atraso entrega", "atraso_entrega",
    "payment", "pagamento", "negativacao indevida", "negativacao_indevida",
}


def _check_math(text: str, case_value: Optional[Decimal]) -> List[LintViolation]:
    """L6, L8 e L10 - contas impossiveis, alternativas somadas e desproporcao."""
    violations = []

    for issue in check_text_coherence(text, case_value=case_value):
        rule = _COHERENCE_RULE.get(issue.code, "L6_MATEMATICA")
        detail = issue.detail
        if issue.math:
            detail = f"{detail} | conta: {issue.math}"

        violations.append(LintViolation(
            rule=rule,
            detail=detail,
            excerpt=issue.math or issue.code,
        ))

    return violations


def _check_out_of_scope(
    text: str,
    case_category: str,
    case_description: str,
) -> List[LintViolation]:
    """
    L9 - clausula de projeto num caso que nao e de projeto.

    So dispara quando a categoria do caso e claramente simples E o termo nao
    aparece na descricao do proprio caso: se o cliente ja falou em cronograma na
    abertura, negociar cronograma esta dentro do objeto.
    """
    if _norm(case_category) not in SIMPLE_CASE_CATEGORIES:
        return []

    description = _norm(case_description)
    violations = []

    for pattern, label in PROJECT_INSTRUMENTS:
        match = pattern.search(text)
        if not match:
            continue
        if _norm(label) in description:
            continue

        violations.append(LintViolation(
            rule="L9_FORA_DE_OBJETO",
            detail=(
                f"introduz '{label}', que nao tem vinculo demonstrado com o objeto "
                f"do caso (categoria: {case_category})"
            ),
            excerpt=match.group(0),
        ))

    return violations


def _check_mandate(text: str, mandate: Optional[CompanyMandate]) -> List[LintViolation]:
    """L7 - alcada: exposicao acima do teto ou instrumento sensivel sem ressalva."""
    if mandate is None:
        return []

    decision = check_mandate(text, mandate)
    if not decision.requires_human:
        return []

    return [
        LintViolation(
            rule="L7_ALCADA",
            detail=reason,
            excerpt=format_brl(decision.committed_amount),
        )
        for reason in decision.reasons
    ]


# =============================================================================
# API PUBLICA
# =============================================================================

def lint_company_response(
    text: str,
    company_name: str = "Empresa",
    client_name: str = "",
    case_value: Optional[Decimal] = None,
    case_category: str = "",
    case_description: str = "",
    mandate: Optional[CompanyMandate] = None,
) -> List[LintViolation]:
    """
    Verifica a resposta gerada pela IA da empresa.

    Args:
        text: resposta do LLM
        company_name: empresa que deveria assinar
        client_name: nome do cliente vindo do sistema ("" ou "Cliente" = ausente)
        case_value: valor estimado da causa (habilita L10)
        case_category: categoria do chamado (habilita L9)
        case_description: descricao do caso (evita falso positivo em L9)
        mandate: mandato da empresa (habilita L7)

    Returns:
        Lista de violacoes (vazia = resposta limpa)
    """
    if not text or not text.strip():
        return [LintViolation(
            rule="L0_EMPTY_RESPONSE",
            detail="resposta vazia nao pode ser enviada",
            excerpt="",
        )]

    violations: List[LintViolation] = []
    violations.extend(_check_placeholders(text))
    violations.extend(_check_identity(text, company_name))
    violations.extend(_check_client_name(text, client_name))
    violations.extend(_check_bulk_acceptance(text))
    violations.extend(_check_emoji(text))
    violations.extend(_check_math(text, case_value))
    violations.extend(_check_mandate(text, mandate))
    violations.extend(_check_out_of_scope(text, case_category, case_description))

    return violations


def lint_sender_message(
    text: str,
    client_name: str = "",
    case_value: Optional[Decimal] = None,
    case_category: str = "",
    case_description: str = "",
) -> List[LintViolation]:
    """
    Verifica a mensagem gerada pelo ZelinhU antes de enviar a empresa.

    A multa de R$ 5.000/dia com teto de R$ 3.700, os tetos de 15% ao mes contra
    10% global e a arbitragem para uma causa de R$ 18.500 foram TODOS propostos
    pelo ZelinhU. Nao havia nenhuma verificacao desse lado (achado Z-07).

    L7 (alcada) fica de fora: quem tem mandato financeiro e a empresa.
    L2/L4 tambem: identidade e aceite em bloco sao especificos da empresa.

    Returns:
        Lista de violacoes (vazia = mensagem limpa)
    """
    if not text or not text.strip():
        return [LintViolation(
            rule="L0_EMPTY_RESPONSE",
            detail="resposta vazia nao pode ser enviada",
            excerpt="",
        )]

    violations: List[LintViolation] = []
    violations.extend(_check_placeholders(text))
    violations.extend(_check_client_name(text, client_name))
    violations.extend(_check_promessa_pelo_cliente(text))
    violations.extend(_check_emoji(text))
    violations.extend(_check_math(text, case_value))
    violations.extend(_check_out_of_scope(text, case_category, case_description))

    return violations


def build_correction_instruction(
    violations: List[LintViolation],
    company_name: str = "Empresa",
    client_name: str = "",
) -> str:
    """
    Monta a instrucao corretiva para a segunda tentativa.

    Aponta o defeito concreto encontrado, em vez de repetir a regra generica -
    os relatorios mostram que repetir a regra nao muda o comportamento.
    """
    lines = [
        "CORRECAO OBRIGATORIA DA SUA ULTIMA RESPOSTA",
        "",
        "A resposta que voce acabou de escrever nao pode ser enviada. Foram "
        "encontrados os seguintes defeitos:",
        "",
    ]

    for i, violation in enumerate(violations, 1):
        lines.append(f"{i}. [{violation.rule}] {violation.detail}")
        if violation.excerpt:
            lines.append(f"   trecho: {violation.excerpt}")

    lines.extend([
        "",
        "Reescreva a resposta inteira corrigindo TODOS os defeitos acima e "
        "mantendo o conteudo negocial que ja estava correto.",
        "",
        "Lembretes para a reescrita:",
        f"- Assine como {company_name}. Nunca como Zellu ou ZelinhU.",
    ])

    if _norm(client_name) not in ("", "cliente"):
        lines.append(f"- O nome do cliente e exatamente: {client_name}")
    else:
        lines.append("- O sistema nao informou o nome do cliente: nao use nome nenhum.")

    lines.extend([
        "- Nenhum campo entre colchetes por preencher. Se o dado nao existe, "
        "escreva que ele nao existe.",
        "- Se voce esta respondendo a varios pedidos, diga para cada um, em frase "
        "normal e sem etiqueta entre colchetes, se a empresa aceita, aceita com "
        "condicao, contrapropoe, recusa ou depende de aprovacao interna.",
        "- Se a sua resposta anterior encerrava o caso com [CASO FINALIZADO], "
        "MANTENHA esse marcador na reescrita - ele fecha o atendimento no sistema.",
        "- Sem emojis.",
        "- Se algum defeito acima for de conta (multa, teto, percentual, soma), "
        "refaca o calculo e mostre a conta na resposta.",
        "- Se algum item estiver fora da sua alcada, diga que ele depende de "
        "aprovacao interna em vez de assumi-lo.",
        "",
        "Responda APENAS com o texto corrigido, pronto para enviar ao cliente.",
    ])

    return "\n".join(lines)


def build_sender_correction_instruction(
    violations: List[LintViolation],
    client_name: str = "",
) -> str:
    """
    Instrucao corretiva para a mensagem do ZelinhU.

    Mesma logica de build_correction_instruction, com os lembretes do lado de
    quem representa o cliente: nao propor conta impossivel, nao abandonar
    direito, nao trazer clausula fora do objeto.
    """
    lines = [
        "CORRECAO OBRIGATORIA DA SUA ULTIMA MENSAGEM",
        "",
        "A mensagem que voce acabou de escrever nao pode ser enviada a empresa. "
        "Foram encontrados os seguintes defeitos:",
        "",
    ]

    for i, violation in enumerate(violations, 1):
        lines.append(f"{i}. [{violation.rule}] {violation.detail}")
        if violation.excerpt:
            lines.append(f"   trecho: {violation.excerpt}")

    lines.extend([
        "",
        "Reescreva a mensagem inteira corrigindo TODOS os defeitos acima e "
        "mantendo os pedidos do cliente que ja estavam corretos.",
        "",
        "Lembretes para a reescrita:",
        "- Toda multa precisa fechar a conta: valor por dia vezes o prazo nao "
        "pode ultrapassar o teto que voce mesmo propoe.",
        "- Teto por periodo nunca pode ser maior que o teto global.",
        "- Apresente UMA proposta principal. Se houver alternativa, deixe claro "
        "que e alternativa e que a empresa precisa escolher uma.",
        "- Nao proponha clausula sem relacao com o problema que o cliente relatou.",
        "- Nao renuncie a direito do cliente para simplificar o acordo.",
        "- Nenhum campo entre colchetes por preencher.",
        "- Sem emojis.",
    ])

    if _norm(client_name) not in ("", "cliente"):
        lines.append(f"- O nome do cliente e exatamente: {client_name}")

    lines.extend([
        "",
        "Responda APENAS com o texto corrigido da mensagem.",
    ])

    return "\n".join(lines)


def sanitize_company_response(text: str, company_name: str = "Empresa") -> str:
    """
    Ultimo recurso, aplicado somente se a segunda tentativa ainda violar.

    Corrige o que da para corrigir deterministicamente sem inventar conteudo:
    remove emojis e troca a assinatura invertida pelo nome da empresa.
    NAO tenta consertar placeholder ou aceite em bloco - isso exigiria reescrever
    o conteudo negocial, o que este modulo nao faz.
    """
    if not text:
        return text

    sanitized = EMOJI.sub("", text)

    # assinatura em colchete com a parte contraria -> nome da empresa
    def _replace_bracket(match: "re.Match") -> str:
        if COUNTERPARTY.search(match.group(1)):
            return company_name
        return match.group(0)

    sanitized = re.sub(r"\[([^\[\]]{1,80})\]", _replace_bracket, sanitized)

    # mencao a contraparte na zona de assinatura
    head, tail = sanitized[:-SIGNATURE_ZONE_CHARS], sanitized[-SIGNATURE_ZONE_CHARS:]
    tail = COUNTERPARTY.sub(company_name, tail)

    return re.sub(r"[ \t]{2,}", " ", head + tail).strip()


_CASE_CLOSED_MARKER = re.compile(
    r"\[\s*caso\s+finalizado\s*\]"
    r"(?:\s*[-–—:]?\s*sua\s+solicita[cç][aã]o\s+foi\s+registrada[^.\n]*\.?)?",
    re.IGNORECASE,
)

PENDING_APPROVAL_FALLBACK = (
    "A proposta depende de aprovacao interna da empresa. "
    "Retornaremos com a confirmacao."
)


def strip_case_closed_marker(text: str) -> str:
    """
    Remove [CASO FINALIZADO] (e o sufixo padrao "Sua solicitacao foi
    registrada...") de uma resposta cujo fechamento foi barrado.

    Usado quando o gate de alcada devolve negotiationComplete=false: sem isso o
    ZelinhU recebia "caso finalizado" numa negociacao que continua aberta.
    """
    if not text:
        return text

    stripped = _CASE_CLOSED_MARKER.sub("", text)
    stripped = re.sub(r"[ \t]{2,}", " ", stripped)
    stripped = re.sub(r"\n{3,}", "\n\n", stripped).strip()
    return stripped or PENDING_APPROVAL_FALLBACK


def format_violations_for_log(violations: List[LintViolation]) -> str:
    """Resumo em uma linha para o log."""
    if not violations:
        return "nenhuma"
    return "; ".join(f"{v.rule}({v.excerpt[:30]})" if v.excerpt else v.rule
                     for v in violations)
