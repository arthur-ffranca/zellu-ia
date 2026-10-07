# -*- coding: utf-8 -*-
"""
Normalizacao e deduplicacao da lista de empresas enviada ao front.

Contexto do problema: a busca (Firecrawl na web ou base Zellu) devolve a MESMA
empresa varias vezes, com o nome escrito de formas diferentes ("MAGAZINE LUIZA",
"magazine luiza", "Magazine Luiza S/A") e o CNPJ com pontuacao variada
("47.960.950/0001-21", "47960950000121"). Um set() sobre a lista nao resolve,
porque as strings sao diferentes caractere a caractere.

Estrategia:
  1. O CNPJ vira a chave canonica: so caracteres alfanumericos, 14 posicoes,
     com os digitos verificadores conferidos (modulo 11). CNPJ que nao fecha
     e descartado (o Firecrawl as vezes raspa numero truncado ou de outra
     empresa da pagina).
  2. Nome fantasia e razao social sao padronizados em Title Case, com siglas
     societarias em caixa alta e preposicoes em minuscula.
  3. Repetido o CNPJ, fica UM registro - o mais completo entre os repetidos,
     preenchido com os campos que so os outros tinham.

O CNPJ fica em `cnpj` sem pontuacao (valor canonico, usado para comparar e para
enviar ao backend) e formatado em `cnpj_formatted` (usado nos textos que o
usuario le).
"""

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# CNPJ
# ---------------------------------------------------------------------------

# Desde julho/2026 o CNPJ pode ser alfanumerico (12 primeiras posicoes com
# letras, 2 ultimas sempre numericas). Por isso mantemos letras na normalizacao
# em vez de aplicar um re.sub(r'\D') cego, que apagaria o numero de vez.
# O calculo do DV usa (ord(c) - 48), que da exatamente o valor do digito para
# CNPJ numerico - ou seja, o mesmo codigo serve para os dois formatos.
_CNPJ_CLEAN_RE = re.compile(r"[^0-9A-Z]")
_DV_WEIGHTS_1 = (5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)
_DV_WEIGHTS_2 = (6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)


def normalize_cnpj(raw: Any) -> str:
    """
    Devolve o CNPJ so com caracteres alfanumericos, em 14 posicoes.

    Tira pontuacao e espacos, completa zeros a esquerda quando a fonte perdeu
    (ex.: "1.234.567/0001-89"). Nao valida - use is_valid_cnpj para isso.
    Devolve "" quando nao ha nada aproveitavel.
    """
    if not raw:
        return ""
    cleaned = _CNPJ_CLEAN_RE.sub("", str(raw).upper())
    if not cleaned:
        return ""
    if len(cleaned) < 14:
        cleaned = cleaned.zfill(14)
    return cleaned


def _dv(base: str, weights: tuple) -> int:
    """Calcula um digito verificador do CNPJ (modulo 11)."""
    total = sum((ord(char) - 48) * weight for char, weight in zip(base, weights))
    rest = total % 11
    return 0 if rest < 2 else 11 - rest


def is_valid_cnpj(raw: Any) -> bool:
    """
    Confere se o CNPJ tem 14 posicoes e se os 2 digitos verificadores fecham.

    Rejeita tambem o caso degenerado de todos os caracteres iguais
    ("00000000000000"), que passa no calculo mas nao existe na Receita.
    """
    cnpj = normalize_cnpj(raw)
    if len(cnpj) != 14:
        return False
    if not cnpj[12:].isdigit():
        return False
    if len(set(cnpj)) == 1:
        return False
    return (
        _dv(cnpj[:12], _DV_WEIGHTS_1) == int(cnpj[12])
        and _dv(cnpj[:13], _DV_WEIGHTS_2) == int(cnpj[13])
    )


def format_cnpj(raw: Any) -> str:
    """Formata o CNPJ como XX.XXX.XXX/XXXX-XX para exibicao ao usuario."""
    cnpj = normalize_cnpj(raw)
    if len(cnpj) != 14:
        return str(raw or "")
    return f"{cnpj[:2]}.{cnpj[2:5]}.{cnpj[5:8]}/{cnpj[8:12]}-{cnpj[12:]}"


# ---------------------------------------------------------------------------
# Nome (razao social / nome fantasia)
# ---------------------------------------------------------------------------

# Siglas societarias que ficam em caixa alta. So valem a partir do 2o token -
# nenhuma razao social comeca com "LTDA", e assim uma empresa chamada
# "Me Leva Transportes" nao vira "ME Leva Transportes".
_SIGLAS = {
    "LTDA", "LTDA.", "LTD", "ME", "M.E.", "EPP", "EPP.", "EIRELI", "MEI",
    "SA", "S.A", "S.A.", "S/A", "CIA", "CIA.", "S/S", "SS", "SPE",
    "INC", "INC.", "LLC", "ONG", "OSCIP",
}

# Preposicoes e conectivos que ficam em minuscula no meio do nome.
_PREPOSICOES = {
    "de", "da", "do", "das", "dos", "e", "em", "na", "no", "nas", "nos",
    "a", "o", "as", "os", "ao", "aos", "com", "para", "por", "sob", "sobre",
}

_LETTERS_RE = re.compile(r"[^A-Za-zÀ-ÿ]")
_VOWELS_RE = re.compile(r"[aeiouà-ü]", re.IGNORECASE)


def _capitalize_word(word: str) -> str:
    """Capitaliza respeitando hifen e apostrofo: "coca-cola" -> "Coca-Cola"."""
    out = word
    for sep in ("-", "'", "’"):
        out = sep.join(
            part[:1].upper() + part[1:] if part else part
            for part in out.split(sep)
        )
    return out


def _normalize_token(token: str, index: int, source_all_upper: bool) -> str:
    """Normaliza uma palavra do nome da empresa."""
    bare = token.strip(".,;:()[]").upper()

    # Sigla societaria (nunca no inicio do nome)
    if index > 0 and (token.upper() in _SIGLAS or bare in _SIGLAS):
        return token.upper()

    letters = _LETTERS_RE.sub("", token)
    if not letters:
        return token  # numeros, tracos, "&", etc - preserva como veio

    # Token que ja era sigla no original ("IBM Brasil Ltda" -> mantem IBM).
    # So confiavel quando a fonte NAO estava toda em caixa alta.
    if not source_all_upper and token == token.upper() and len(letters) > 1:
        return token

    # Sigla curta sem vogal (BRF, JBS, CVC, 3M) - Title Case estragaria.
    if len(letters) <= 3 and not _VOWELS_RE.search(letters):
        return token.upper()

    lower = token.lower()
    if index > 0 and lower.strip(".,") in _PREPOSICOES:
        return lower

    return _capitalize_word(lower)


def normalize_company_name(raw: Any) -> str:
    """
    Padroniza o nome da empresa em Title Case.

    "MAGAZINE LUIZA S/A"   -> "Magazine Luiza S/A"
    "lojas americanas ltda" -> "Lojas Americanas LTDA"
    "BANCO DO BRASIL"       -> "Banco do Brasil"
    "IBM Brasil Ltda"       -> "IBM Brasil LTDA"
    """
    if not raw or not isinstance(raw, str):
        return ""
    text = re.sub(r"\s+", " ", raw).strip()
    if not text:
        return ""
    source_all_upper = text == text.upper()
    return " ".join(
        _normalize_token(token, index, source_all_upper)
        for index, token in enumerate(text.split(" "))
    )


# ---------------------------------------------------------------------------
# Empresa (dict) - normalizacao e deduplicacao
# ---------------------------------------------------------------------------

# As duas origens usam nomes de campo diferentes (Firecrawl usa snake_case,
# a base Zellu tambem devolve camelCase), entao normalizamos todas as variantes
# que existirem no dict, sem inventar chave que nao veio.
_NAME_FIELDS = (
    "razao_social", "razaoSocial", "companyName",
    "nome_fantasia", "nomeFantasia", "tradeName",
)

# Campos considerados no criterio de "registro mais completo".
_COMPLETENESS_FIELDS = _NAME_FIELDS + (
    "site", "website", "homepage", "url", "favicon_url",
    "uf", "municipio", "logradouro", "numero", "bairro", "cep",
    "situacao_cadastral", "atividade_principal",
)


@dataclass
class CompanyListResult:
    """Resultado da normalizacao/deduplicacao de uma lista de empresas."""
    companies: List[Dict[str, Any]] = field(default_factory=list)
    invalid_cnpjs: List[str] = field(default_factory=list)   # descartados no DV
    duplicates_removed: int = 0                               # mesmo CNPJ


def normalize_company(company: Dict[str, Any]) -> Dict[str, Any]:
    """
    Devolve uma copia da empresa com CNPJ e nomes normalizados.

    - `cnpj`: so alfanumerico, 14 posicoes (valor canonico)
    - `cnpj_formatted`: XX.XXX.XXX/XXXX-XX (para exibir)
    - nomes: Title Case (ver normalize_company_name)
    """
    normalized = dict(company)

    cnpj = normalize_cnpj(company.get("cnpj"))
    normalized["cnpj"] = cnpj
    normalized["cnpj_formatted"] = format_cnpj(cnpj) if cnpj else ""

    for key in _NAME_FIELDS:
        value = company.get(key)
        if isinstance(value, str) and value.strip():
            normalized[key] = normalize_company_name(value)

    return normalized


def _completeness(company: Dict[str, Any]) -> int:
    """Conta quantos campos uteis a empresa tem preenchidos."""
    score = 0
    for key in _COMPLETENESS_FIELDS:
        value = company.get(key)
        if isinstance(value, str):
            if value.strip():
                score += 1
        elif value not in (None, "", [], {}):
            score += 1

    endereco = company.get("endereco")
    if isinstance(endereco, dict):
        score += sum(
            1 for value in endereco.values()
            if isinstance(value, str) and value.strip()
        )
    return score


def _merge_into(winner: Dict[str, Any], other: Dict[str, Any]) -> None:
    """Completa os campos vazios do `winner` com o que o duplicado tinha."""
    for key, value in other.items():
        if key == "endereco":
            continue
        current = winner.get(key)
        is_empty = current is None or (isinstance(current, str) and not current.strip())
        if is_empty and value not in (None, "", [], {}):
            winner[key] = value

    other_end = other.get("endereco")
    if isinstance(other_end, dict):
        winner_end = winner.get("endereco")
        if not isinstance(winner_end, dict):
            winner["endereco"] = dict(other_end)
        else:
            for key, value in other_end.items():
                current = winner_end.get(key)
                if isinstance(value, str) and value.strip() and not (current or "").strip():
                    winner_end[key] = value


def normalize_company_list(
    companies: Optional[List[Dict[str, Any]]],
    discard_invalid: bool = True,
) -> CompanyListResult:
    """
    Normaliza, valida e deduplica a lista de empresas antes de ir ao front.

    Args:
        companies: lista crua vinda do Firecrawl ou da base Zellu
        discard_invalid: descarta CNPJ ausente ou com digito verificador errado
                         (padrao). Com False, so normaliza e deduplica.

    Returns:
        CompanyListResult com a lista final (ordem de entrada preservada),
        os CNPJs descartados e quantos duplicados foram removidos.
    """
    result = CompanyListResult()
    if not companies:
        return result

    by_cnpj: Dict[str, Dict[str, Any]] = {}

    for raw_company in companies:
        if not isinstance(raw_company, dict):
            continue

        company = normalize_company(raw_company)
        cnpj = company.get("cnpj", "")

        if discard_invalid and not is_valid_cnpj(cnpj):
            result.invalid_cnpjs.append(str(raw_company.get("cnpj") or ""))
            continue

        # Sem CNPJ nao ha chave de deduplicacao - e o form de selecao ja pula
        # essas empresas (value vazio quebra o front), entao caem fora aqui.
        if not cnpj:
            result.invalid_cnpjs.append(str(raw_company.get("cnpj") or ""))
            continue

        existing = by_cnpj.get(cnpj)
        if existing is None:
            by_cnpj[cnpj] = company
            continue

        # Mesmo CNPJ: fica o registro mais completo, completado com o outro.
        result.duplicates_removed += 1
        if _completeness(company) > _completeness(existing):
            _merge_into(company, existing)
            by_cnpj[cnpj] = company
        else:
            _merge_into(existing, company)

    result.companies = list(by_cnpj.values())
    return result
