# -*- coding: utf-8 -*-
"""
handType - maozinha animada do avatar do Assistente Zellu.

Contrato: docs/20260728-contrato-handtype-avatar-animado.md

O campo vai DENTRO de `messagedata` (nunca solto na raiz) e escolhe, por
mensagem, qual das 4 maos animadas acompanha aquela bolha no chat. E opcional:
sem o campo, o front usa a mao padrao (`tchau`), entao omitir nunca quebra nada.

A escolha aqui e DETERMINISTICA - sai de sinais que o fluxo ja conhece
(formulario pendente, analise concluida, problema recem-relatado), sem custo de
token e sem risco de o LLM inventar um valor fora do enum.

Regra do contrato que guia o resolver: nao mandar a mesma mao em toda mensagem.
Quando nao ha motivo claro para variar, devolvemos None e o campo e omitido.
"""

import re
import unicodedata
from typing import Optional

# ---------------------------------------------------------------------------
# Valores canonicos (enum do contrato)
# ---------------------------------------------------------------------------

HEART = "heart"    # acolhimento, empatia, agradecimento
SHAKE = "shake"    # acordo fechado, "combinado", boas-vindas formais
SNAP = "snap"      # problema resolvido, entrega do resultado, "pronto!"
TCHAU = "tchau"    # padrao: saudacao, abertura, despedida

VALID_HAND_TYPES = frozenset({HEART, SHAKE, SNAP, TCHAU})


def normalize_hand_type(value: Optional[str]) -> Optional[str]:
    """
    Normaliza um handType para o slug canonico, ou None se nao reconhecer.

    Aceita as mesmas variacoes que o app tolera (prefixo `hand-`/`hand_`, caixa
    livre, espaco sobrando) para que o valor saia sempre no formato canonico -
    que o contrato pede que a gente envie.

    "HAND_HEART" -> "heart" | "  Snap " -> "snap" | "abraco" -> None
    """
    if not value or not isinstance(value, str):
        return None
    slug = unicodedata.normalize("NFKD", value.strip().lower())
    slug = "".join(c for c in slug if not unicodedata.combining(c))
    slug = re.sub(r"^hand[-_]", "", slug)
    return slug if slug in VALID_HAND_TYPES else None


# ---------------------------------------------------------------------------
# Deteccao de agradecimento na mensagem do usuario
# ---------------------------------------------------------------------------

_GRATITUDE_TERMS = (
    "obrigado", "obrigada", "obrigadao", "obg", "vlw", "valeu",
    "agradeco", "agradecido", "agradecida", "grato", "grata", "gratidao",
    "muito obrigado", "muito obrigada", "voces sao demais", "ajudou muito",
)


def _strip_accents(text: str) -> str:
    """Remove acentos para comparar sem depender de como o usuario digitou."""
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def is_gratitude_message(text: Optional[str]) -> bool:
    """Detecta agradecimento na mensagem do usuario."""
    if not text:
        return False
    normalized = _strip_accents(text.lower())
    return any(term in normalized for term in _GRATITUDE_TERMS)


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------

def resolve_hand_type(
    is_finished: bool = False,
    has_form: bool = False,
    agreement_reached: bool = False,
    problem_just_described: bool = False,
    user_message: Optional[str] = None,
) -> Optional[str]:
    """
    Decide a maozinha da resposta a partir dos sinais do turno.

    Precedencia (do mais especifico para o mais generico):
      1. is_finished             -> snap   (entrega do resultado, "pronto!")
      2. has_form                -> shake  ("combinado, preciso destes dados")
      3. agreement_reached       -> shake  (empresa confirmada, acordo fechado)
      4. problem_just_described  -> heart  (o usuario acabou de relatar o caso)
      5. agradecimento           -> heart  (acolhimento)
      6. nada disso              -> None   (omite; o front usa `tchau`)

    Args:
        is_finished: a analise foi concluida neste turno
        has_form: a resposta leva um formulario dinamico (event_type)
        agreement_reached: fechou acordo / confirmou a empresa neste turno
        problem_just_described: o problema foi preenchido agora (era vazio antes)
        user_message: texto do usuario, para detectar agradecimento

    Returns:
        Slug canonico ou None quando o caso e neutro.
    """
    if is_finished:
        return SNAP
    if has_form:
        return SHAKE
    if agreement_reached:
        return SHAKE
    if problem_just_described:
        return HEART
    if is_gratitude_message(user_message):
        return HEART
    return None
