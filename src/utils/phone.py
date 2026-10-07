# -*- coding: utf-8 -*-
"""
Normalizacao e validacao de telefone brasileiro.

Contrato: emenda de 2026-09-04 em user-exists-webhook-spec.md.

Ate 03/09 o POST /api/ai/create-pre-registered-user aceitava qualquer coisa no
campo `phone`: numero irreconhecivel virava 201 e a conta nascia SEM telefone,
calado. A partir da emenda o telefone e identidade (unico por conta, chave de
login), entao a rota devolve 400 quando nao reconhece o numero. Validamos aqui
ANTES de enviar, para pedir o numero de novo na conversa em vez de descobrir o
problema como erro HTTP no meio do fluxo.

Formato aceito pelo backend:
    11 digitos => DDD (11-99) + '9' + 8 digitos   (celular)
    10 digitos => DDD (11-99) + [2-8] + 7 digitos (fixo)

O mascarado continua valendo - "(11) 99999-9999", "+5511999999999" e
"11999999999" sao a mesma coisa depois de normalizar. O que passa a ser
recusado e o celular SEM o 9 ("(11) 9999-9999"), numero curto, DDD invalido
e texto.
"""

import re
from typing import Any

# Texto unico para pedir o numero de novo - usado nas mensagens e no tooltip
# do formulario, para o usuario ler sempre a mesma instrucao.
PHONE_FORMAT_HINT = "Use DDD + numero, como (11) 99999-9999. Celular tem 9 digitos apos o DDD."

# Regex do formulario dinamico (validado no front antes de chegar aqui).
# Aceita o mascarado, o +55 e os digitos crus; recusa celular sem o 9.
PHONE_FORM_PATTERN = r"^(?:\+?55\s?)?\(?(?:1[1-9]|[2-9][0-9])\)?\s?(?:9\d{4}|[2-8]\d{3})-?\d{4}$"

_NON_DIGITS_RE = re.compile(r"\D")


def normalize_phone(raw: Any) -> str:
    """
    Devolve so os digitos do telefone, sem o codigo do pais.

    Tira mascara, espaco e o +55 (que o usuario as vezes digita). Devolve ""
    quando nao sobra nada aproveitavel. Nao valida - use is_valid_phone.
    """
    if not raw:
        return ""
    digits = _NON_DIGITS_RE.sub("", str(raw))
    # +55 so e codigo de pais quando sobra um telefone inteiro depois dele
    # (12 = fixo com DDI, 13 = celular com DDI). Um numero de 11 digitos que
    # comeca com 55 e o DDD 55 (RS), nao um DDI - por isso a checagem de tamanho.
    if len(digits) in (12, 13) and digits.startswith("55"):
        digits = digits[2:]
    return digits


def is_valid_phone(raw: Any) -> bool:
    """
    True quando o numero esta no formato que o backend aceita.

    Vale o contrato da emenda de 04/09: 11 digitos com o 9 do celular, ou 10
    digitos com o primeiro digito do fixo entre 2 e 8. DDD sempre de 11 a 99.
    """
    digits = normalize_phone(raw)
    if len(digits) not in (10, 11):
        return False

    if not 11 <= int(digits[:2]) <= 99:
        return False

    if len(digits) == 11:
        return digits[2] == "9"

    return digits[2] in "2345678"
