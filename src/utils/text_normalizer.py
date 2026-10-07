"""Normalização conservadora de texto informal antes da interpretação da IA."""

from __future__ import annotations

import re
import unicodedata


# Expansões comuns em respostas curtas. Aplicadas por palavra para não alterar
# nomes, e-mails, CNPJ ou o conteúdo original que fica registrado no chamado.
_REPLACEMENTS = {
    "vc": "voce",
    "vcs": "voces",
    "tb": "tambem",
    "tbm": "tambem",
    "s": "sim",
    "ss": "sim",
    "n": "nao",
    "nao": "nao",
    "vose": "voce",
    "vosse": "voce",
    "voçe": "voce",
    "nois": "nos",
    "nos": "nos",
    "pq": "porque",
    "p q": "porque",
    "q": "que",
    "blz": "beleza",
    "obg": "obrigado",
    "vlw": "valeu",
    "eh": "e",
    "ta": "esta",
    "to": "estou",
    "tô": "estou",
    "dps": "depois",
    "agr": "agora",
    "hj": "hoje",
    "amanha": "amanha",
    "msg": "mensagem",
    "p": "para",
    "pra": "para",
    "pro": "para o",
    "aí": "ai",
    "ai": "ai",
    "mt": "muito",
    "mto": "muito",
    "muita": "muita",
    "qdo": "quando",
    "kd": "cadê",
    "cadê": "cade",
    "ctz": "certeza",
    "certeza": "certeza",
    "tb": "tambem",
    "tipo": "tipo",
    "mano": "amigo",
    "man": "amigo",
    "irmao": "amigo",
    "caralho": "",
    "porra": "",
}


def normalize_user_text(text: str | None) -> str:
    """Expande abreviações e erros previsíveis sem reescrever a mensagem toda."""
    if not text:
        return ""
    value = str(text)
    # Preserva pontuação e espaços; só substitui tokens isolados.
    value = re.sub(
        r"(?<![\w@.])([A-Za-zÀ-ÿ]+)(?![\w@.])",
        lambda match: _REPLACEMENTS.get(match.group(1).lower(), match.group(1)),
        value,
        flags=re.IGNORECASE,
    )
    value = unicodedata.normalize("NFKD", value)
    value = "".join(char for char in value if not unicodedata.combining(char))
    return value.lower()
