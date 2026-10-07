# -*- coding: utf-8 -*-
"""
Deteccao de aceite na fala do ZelinhU (lado da IA da Empresa).

MOTIVACAO
---------
Quando o ZelinhU aceita a proposta, src/main.py injeta a instrucao de fechar o
caso com [CASO FINALIZADO] - e isso vira negotiationComplete=true. A deteccao
antiga procurava as palavras como SUBSTRING em qualquer ponto do texto:

  - "Nao aceito essa proposta."          -> "aceito"
  - "Assim nao da, quero reembolso."     -> "sim"
  - "Nao pode ser, o valor e R$ 1.200."  -> "pode ser"
  - "Tudo bem, mas preciso do estorno."  -> "tudo bem"

Todas contavam como aceite e fechavam o caso com um acordo que ninguem fez.

REGRAS
------
  1. Palavra inteira, sem acento, sem diferenciar maiuscula.
  2. Termo negado na mesma oracao ("nao aceito", "isso nao esta fechado") nao
     conta.
  3. Recusa ou contraproposta em qualquer ponto da mensagem anula o aceite.
  4. Ressalva DEPOIS do aceite ("tudo bem, mas...", "aceito, desde que...")
     anula o aceite - o que vem depois e condicao ou contraproposta.
  5. Termos fracos ("sim", "ok", "perfeito", "tudo bem", "pode ser", "pode
     fazer", "combinado", "fechado") so contam em resposta curta. Numa mensagem
     longa do ZelinhU eles sao cortesia ("Ok, entendemos...") ou descricao
     ("o prazo combinado") e nao decisao.

Na duvida, NAO e aceite: um falso negativo custa uma rodada a mais (a empresa
pergunta de novo); um falso positivo fecha o caso errado.

Codigo puro: sem LLM, sem rede, sem estado.
"""

import re
import unicodedata
from typing import List, Optional, Sequence, Tuple

# Aceite explicito: conta em mensagem de qualquer tamanho.
STRONG_TERMS: Tuple[str, ...] = (
    "aceito",
    "aceitamos",
    "cliente aceita",
    "concordo",
    "concordamos",
    # "de acordo" sozinho nao: "de acordo com o art. 18 do CDC" e argumento
    "estou de acordo",
    "estamos de acordo",
    "negocio fechado",
    "acordo fechado",
    "fechamos",
)

# Aceite de cortesia: so conta em resposta curta. "combinado" e "fechado"
# entram aqui porque numa mensagem longa sao descricao ("o prazo combinado").
WEAK_TERMS: Tuple[str, ...] = (
    "combinado",
    "fechado",
    "sim",
    "ok",
    "okay",
    "perfeito",
    "tudo bem",
    "pode ser",
    "pode fazer",
)

# Resposta com ate este numero de palavras e "curta" para os termos fracos.
SHORT_REPLY_MAX_WORDS = 8

# Negacao que anula o termo seguinte na mesma oracao.
NEGATIONS = frozenset({"nao", "nunca", "jamais", "nem"})
# Distancia maxima (em palavras) entre a negacao e o termo.
NEGATION_WINDOW = 4

# Recusa ou contraproposta: anula o aceite em qualquer ponto.
REFUSAL_TERMS: Tuple[str, ...] = (
    "recuso",
    "recusamos",
    "recusa",
    "rejeito",
    "rejeitamos",
    "discordo",
    "discordamos",
    "contraproposta",
    "contrapropor",
    "contrapropomos",
    "contrapropoe",
    "insuficiente",
    "inaceitavel",
)

# Ressalva: anula o aceite quando aparece depois dele.
CONTRAST_TERMS: Tuple[str, ...] = (
    "mas",
    "porem",
    "contudo",
    "entretanto",
    "todavia",
    "no entanto",
    "desde que",
    "exceto",
    "salvo",
    "condicionado",
    "parcialmente",
    "em parte",
    "com ressalva",
    "sob condicao",
    "sob condição",
)

_CLAUSE_BREAK = re.compile(r"[.,;:!?\n]+")
_WORD = re.compile(r"\w+")


def _tokenize(text: str) -> List[str]:
    plain = "".join(
        c for c in unicodedata.normalize("NFD", text)
        if unicodedata.category(c) != "Mn"
    )
    return _WORD.findall(plain.lower())


def _phrase_positions(tokens: Sequence[str], phrase: str) -> List[int]:
    """Posicoes em que a frase (uma ou mais palavras) comeca em `tokens`."""
    words = phrase.split()
    size = len(words)
    return [
        i for i in range(len(tokens) - size + 1)
        if list(tokens[i:i + size]) == words
    ]


def _contains(tokens: Sequence[str], phrases: Sequence[str]) -> bool:
    return any(_phrase_positions(tokens, phrase) for phrase in phrases)


def _is_negated(clause: Sequence[str], position: int) -> bool:
    start = max(0, position - NEGATION_WINDOW)
    return any(token in NEGATIONS for token in clause[start:position])


def _first_acceptance(
    clauses: List[List[str]], terms: Sequence[str]
) -> Optional[Tuple[int, int]]:
    """(oracao, posicao) do primeiro termo de aceite nao negado."""
    for clause_index, clause in enumerate(clauses):
        positions = sorted(
            position
            for term in terms
            for position in _phrase_positions(clause, term)
            if not _is_negated(clause, position)
        )
        if positions:
            return clause_index, positions[0]
    return None


def is_acceptance_message(text: str) -> bool:
    """True se a fala do ZelinhU aceita a proposta sem ressalva."""
    if not text or not text.strip():
        return False

    all_tokens = _tokenize(text)
    if not all_tokens or _contains(all_tokens, REFUSAL_TERMS):
        return False

    clauses = [c for c in (_tokenize(part) for part in _CLAUSE_BREAK.split(text)) if c]

    terms = list(STRONG_TERMS)
    if len(all_tokens) <= SHORT_REPLY_MAX_WORDS:
        terms.extend(WEAK_TERMS)

    found = _first_acceptance(clauses, terms)
    if found is None:
        return False

    # Ressalva depois do aceite anula: "tudo bem, mas...", "aceito, desde que..."
    clause_index, position = found
    after = list(clauses[clause_index][position:])
    for clause in clauses[clause_index + 1:]:
        after.extend(clause)
    return not _contains(after, CONTRAST_TERMS)
