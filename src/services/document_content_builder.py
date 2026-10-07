# -*- coding: utf-8 -*-
"""
Builder dos documentos para analysisData (modelo TEMPLATE-DRIVEN).

Contrato vigente:
- docs/20260618-contrato-documentos-template-fiel.md

O QUE MUDOU (vs. contratos 20260611/20260612):
Os documentos agora sao fieis aos modelos dos advogados. O texto fixo e da
plataforma (templates em src/shared/document/templates/*.ts do backend Zellu);
a IA NAO escreve mais corpo livre (facts/legalBasis/requests). Em vez disso,
cada documento e um LegalDocumentPayload:

    {
      "type": "agreement | extrajudicial | judicial",
      "templateVersion": "v1",
      "slots":    { ... },   # variaveis pontuais (comarca, valores, datas...)
      "sections": { ... },   # trechos de livre descricao (string ou string[])
      "flags":    { ... },   # liga/desliga clausulas opcionais
    }

Sao 3 tipos (1 documento cada):
    amigavel      -> Solicitacao de Resolucao Amigavel (Modelo 1, desde 06/07)
    extrajudicial -> Notificacao Extrajudicial (Modelo 2)
    judicial      -> Peticao Inicial (Consumidor)

Divisao de responsabilidades:
- A IA e dona do CONTEUDO das lacunas (slots/sections) e das flags.
- A plataforma (front + PDF) e dona do TEXTO FIXO e do LAYOUT/marca.

Regras do contrato respeitadas aqui:
- Sem markdown/HTML nas secoes (texto corrido; paragrafos via string[]).
- Qualificacao das partes NAO vai no payload (segue em userInfo/opposingParty).
- Flags conservadoras: judicial.gratuidade=true; todo o resto=false. As
  secoes/slots atras de flag desligada sao OMITIDOS (advogado liga depois).
- Notificacao NAO envia prazo (fixo 3 dias uteis pelo template), nem a
  amigavel (24 horas).
- Amigavel e notificacao: relato em no minimo 2 paragrafos.
- Lacuna que CONTINUA uma frase do modelo sai em minuscula e sem ponto final
  (plano de 22/09, secao 3.1).
- Peticao: a v2 devolve 3.1 a 3.4 ao texto do modelo e troca as quatro secoes
  de fatos por catorze lacunas. Ligada desde 23/09, quando a plataforma
  confirmou o template v2 em producao (PETICAO_TEMPLATE_VERSION=v2).

Custo: 3 chamadas LLM em paralelo (uma por tipo) usando COMPANY_RESPONSE_AI_MODEL
(gpt-4o-mini por padrao). Wall-clock equivalente a uma unica chamada. Em caso de
falha do LLM em qualquer tipo, ha fallback deterministico (a partir de
problem_description + client_rights) para o documento nao sair vazio.
"""

import asyncio
import json
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from src.llm import ChatModel
from src.llm import system_message, user_message

from config import get_settings
from src.services.ai_usage_tracker import track_usage, new_event_id


_TEMPLATE_VERSION = "v1"
_VALID_TYPES = ("agreement", "extrajudicial", "judicial")

try:  # Fecho com data em America/Sao_Paulo (contrato exige TZ correta).
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo("America/Sao_Paulo")
except Exception:  # pragma: no cover - ambiente sem tzdata
    _TZ = None

_MESES_PT = [
    "janeiro", "fevereiro", "marco", "abril", "maio", "junho",
    "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
]


# ---------------------------------------------------------------------------
# Helpers deterministicos (sem LLM)
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(_TZ) if _TZ else datetime.now()


def _data_extenso(dt: datetime) -> str:
    """'18 de junho de 2026'."""
    return f"{dt.day} de {_MESES_PT[dt.month - 1]} de {dt.year}"


def _data_curta(dt: datetime) -> str:
    """'18/06/2026'."""
    return dt.strftime("%d/%m/%Y")


def _brl(value: Any) -> str:
    """Formata numero como moeda BR: 10000 -> 'R$ 10.000,00'."""
    try:
        s = f"{float(value):,.2f}"  # '10,000.00'
        s = s.replace(",", "X").replace(".", ",").replace("X", ".")
        return f"R$ {s}"
    except Exception:
        return "R$ 0,00"


def _strip_code_fences(content: str) -> str:
    """Remove cercas tipo ```json ... ``` que o LLM as vezes adiciona."""
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
    return content.strip()


def _extract_city_from_address(address: Optional[str]) -> Optional[str]:
    """Tenta extrair 'Cidade' de string livre tipo 'Rua X, 123, Sao Paulo - SP'."""
    if not address:
        return None
    m = re.search(r"([^,]+?)\s*[-/]\s*[A-Z]{2}\b", address)
    if m:
        return m.group(1).strip()
    return None


def _city_uf(state: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """Cidade e UF do cliente/caso a partir dos varios campos possiveis."""
    user_info = state.get("userInfo") if isinstance(state.get("userInfo"), dict) else None
    city = (
        state.get("problem_location_city")
        or state.get("client_city")
        or _extract_city_from_address(state.get("client_address"))
        or _extract_city_from_address(user_info.get("address") if user_info else None)
    )
    uf = state.get("problem_location_uf") or state.get("client_uf")
    return (city, uf)


def _cidade_uf_str(state: Dict[str, Any]) -> str:
    """'Sao Paulo/SP', ou so a cidade, ou '' (placeholder do template)."""
    city, uf = _city_uf(state)
    if city and uf:
        return f"{city}/{uf}"
    return city or ""


def _as_str(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "\n\n".join(str(x).strip() for x in value if str(x).strip())
    return ""


def _as_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str) and value.strip():
        # paragrafos separados por linha em branco
        parts = [p.strip() for p in re.split(r"\n\s*\n", value) if p.strip()]
        return parts or [value.strip()]
    return []


def _paragrafos_do_relato(texto: str) -> List[str]:
    """Quebra o relato do cliente em 2 paragrafos, para o minimo do template.

    Rede de seguranca do `oQueAconteceu`: se a LLM devolver um paragrafo so (ou
    nenhum), o relato cru do cliente ja esta na voz certa - primeira pessoa,
    escrita por ele. Quebramos na metade das frases, sem reescrever nada.
    """

    texto = (texto or "").strip()
    if not texto:
        return []

    paragrafos = _as_list(texto)
    if len(paragrafos) >= 2:
        return paragrafos

    frases = [f.strip() for f in re.split(r"(?<=[.!?])\s+", texto) if f.strip()]
    if len(frases) < 2:
        return [texto]

    meio = (len(frases) + 1) // 2
    return [" ".join(frases[:meio]), " ".join(frases[meio:])]


def _cap(text: str, limit: int) -> str:
    """Trunca respeitando o limite de caracteres do contrato (sem cortar palavra)."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(",;:. ")
    return cut or text[:limit]


def _pick_type(recommendations: Optional[List[Dict[str, Any]]]) -> str:
    """Escolhe o tipo recomendado pelo maior score (para o fallback singular)."""
    if not recommendations:
        return "agreement"
    sorted_recs = sorted(recommendations, key=lambda r: r.get("score") or 0, reverse=True)
    top = (sorted_recs[0].get("type") or "agreement").strip().lower()
    if top == "amigavel":
        top = "agreement"
    return top if top in _VALID_TYPES else "agreement"


# ---------------------------------------------------------------------------
# Valor por extenso (art. 292, VI do CPC)
# ---------------------------------------------------------------------------

_UNIDADES = ["", "um", "dois", "tres", "quatro", "cinco", "seis", "sete", "oito", "nove"]
_DEZ_A_DEZENOVE = [
    "dez", "onze", "doze", "treze", "quatorze",
    "quinze", "dezesseis", "dezessete", "dezoito", "dezenove",
]
_DEZENAS = [
    "", "", "vinte", "trinta", "quarenta", "cinquenta",
    "sessenta", "setenta", "oitenta", "noventa",
]
_CENTENAS = [
    "", "cento", "duzentos", "trezentos", "quatrocentos", "quinhentos",
    "seiscentos", "setecentos", "oitocentos", "novecentos",
]


def _ate_999(n: int) -> str:
    if n == 100:
        return "cem"
    centena, resto = divmod(n, 100)
    partes = []
    if centena:
        partes.append(_CENTENAS[centena])
    if resto:
        dezena, unidade = divmod(resto, 10)
        if dezena == 1:
            partes.append(_DEZ_A_DEZENOVE[unidade])
        else:
            if dezena:
                partes.append(_DEZENAS[dezena])
            if unidade:
                partes.append(_UNIDADES[unidade])
    return " e ".join(partes)


def _extenso_int(n: int) -> str:
    if n == 0:
        return "zero"
    milhoes, resto = divmod(n, 1000000)
    milhares, unidades = divmod(resto, 1000)
    partes = []
    if milhoes:
        partes.append(f"{_ate_999(milhoes)} {'milhao' if milhoes == 1 else 'milhoes'}")
    if milhares:
        partes.append("mil" if milhares == 1 else f"{_ate_999(milhares)} mil")
    if unidades:
        partes.append(_ate_999(unidades))
    return " e ".join(partes)


def _extenso_reais(valor: float) -> str:
    """'cinco mil reais'. Usado no fallback deterministico do valor da causa."""
    centavos_totais = int(round(float(valor) * 100))
    inteiro, centavos = divmod(centavos_totais, 100)
    texto = f"{_extenso_int(inteiro)} {'real' if inteiro == 1 else 'reais'}"
    if centavos:
        texto += f" e {_extenso_int(centavos)} {'centavo' if centavos == 1 else 'centavos'}"
    return texto


_RE_VALOR_BRL = re.compile(r"R\$\s*([\d.]+,\d{2})")


def _parse_brl(texto: Any) -> float:
    """Extrai o numeral de 'cinco mil reais (R$ 5.000,00)' -> 5000.0. 0.0 se nao houver."""
    match = _RE_VALOR_BRL.search(str(texto or ""))
    if not match:
        return 0.0
    try:
        return float(match.group(1).replace(".", "").replace(",", "."))
    except ValueError:
        return 0.0


# ---------------------------------------------------------------------------
# Higiene do texto que a LLM devolve (achados 3.2 e 6.2 da auditoria)
# ---------------------------------------------------------------------------

# Alguns slots o template usa como COMPLEMENTO de uma frase ja iniciada. Se a
# LLM devolver oracao com sujeito, o PDF sai com "o dever de A re violou...".
_SUJEITO_NO_INICIO = re.compile(
    r"^\s*(?:"
    # "A ré violou...", "O autor sofreu..."
    r"(?:a|o|as|os|ao|aos|ele|ela|eles|elas)\s+"
    r"(?:re|ré|reu|réu|autor|autora|empresa|assistencia|assistência|"
    r"consumidor|cliente|notificad[oa]|notificante|fornecedor|requerid[oa]|parte)"
    # "Nao ha excludente...", "Houve falha...", "Trata-se de..." (achado 22/09)
    r"|n[ãa]o\s+(?:h[áa]|existe|houve|ha)"
    r"|houve|trata-se|verifica-se|constata-se|resta\s+claro|e\s+certo\s+que"
    r")\b",
    re.IGNORECASE,
)

# Se a LLM escorregar, um complemento generico correto vale mais que uma frase
# quebrada. Na pratica os hints ja evitam cair aqui.
_FALLBACK_COMPLEMENTO = {
    "deverViolado": "prestar o servico contratado com a qualidade, a seguranca e a informacao devidas",
    "descricaoSofrimento": "sucessivas tentativas de solucao sem resposta efetiva da parte contraria",
    "sinteseConduta": "prestar servico defeituoso e nao corrigi-lo apos a reclamacao",
    "justificativaExcludente": (
        "o defeito decorreu exclusivamente do servico prestado pela re, sem culpa "
        "do consumidor nem fato de terceiro"
    ),
}

# Comeco do periodo que o TEMPLATE ja escreveu. Quando a LLM repete a deixa, o
# PDF sai com "exposicao do(a) Autor(a) a exposicao do autor a ..." (prova 5.2
# da auditoria de 22/09). Aqui a repeticao e cortada em vez de descartada.
_DEIXAS_DO_TEMPLATE = (
    "exposicao do autor a", "exposicao do(a) autor(a) a", "exposicao da autora a",
    "consistente em", "o dever de", "defeito consistente em", "na medida em que",
)


def _sem_deixa_do_template(texto: str) -> str:
    """Tira do inicio a repeticao da frase que o template ja abriu."""

    limpo = texto.lstrip()

    for deixa in _DEIXAS_DO_TEMPLATE:
        if limpo.lower().startswith(deixa):
            limpo = limpo[len(deixa):].lstrip()
            break

    return limpo


def _formatar_complemento(texto: str) -> str:
    """Deixa o texto no formato de complemento: minuscula, sem ponto final.

    A lacuna continua uma frase que o modelo comecou, entao ela nao abre com
    maiuscula nem fecha com ponto. Nome proprio e sigla ficam como estao - so a
    primeira letra cai, e apenas quando a palavra inteira nao e maiuscula.
    """

    texto = _sem_deixa_do_template(texto).rstrip(" .;")

    if not texto:
        return ""

    primeira = texto.split(" ", 1)[0]

    # SIGLA (CDC) ou nome com maiuscula no meio (McDonald's): fica como veio.
    # Um "A" solto e artigo, nao sigla: "A Re garantiu" vira "a Re garantiu".
    if (len(primeira) > 1 and primeira.isupper()) or any(c.isupper() for c in primeira[1:]):
        return texto

    return texto[0].lower() + texto[1:]


def _complemento(chave: str, valor: Any) -> str:
    """Devolve o slot so se ele COMPLETAR a frase do template; senao, o fallback."""
    texto = _as_str(valor)
    if texto and not _SUJEITO_NO_INICIO.match(_sem_deixa_do_template(texto)):
        return _formatar_complemento(texto)
    if texto:
        print(
            f"[DOC-CONTENT] slot {chave!r} veio como oracao com sujeito - "
            f"trocado pelo fallback: {texto[:80]!r}"
        )
    return _FALLBACK_COMPLEMENTO.get(chave, "")


def _complementos_lista(chave: str, valores: Any) -> List[str]:
    """Mesma regra de _complemento, item a item. Itens invalidos saem da lista."""
    limpos = []
    for item in _as_list(valores):
        if _SUJEITO_NO_INICIO.match(_sem_deixa_do_template(item)):
            print(
                f"[DOC-CONTENT] item de {chave!r} descartado "
                f"(oracao com sujeito): {item[:80]!r}"
            )
            continue
        formatado = _formatar_complemento(item)
        if formatado:
            limpos.append(formatado)
    return limpos


def _tres_elementos(itens: List[str]) -> List[str]:
    """Os elementos do defeito em no maximo 3 itens (a, b, c do modelo).

    Sobrando, ficam os tres primeiros. Faltando, vai o que houver: o template
    letra o que chegar, e a plataforma prefere 2 alineas a cair no gerador
    antigo (resposta 2 de 23/09, §3). So a lista vazia reprova - pelo campo
    obrigatorio do gate.
    """

    return itens[:3]


def _oracao(valor: Any) -> str:
    """Lacuna que o modelo recebe como ORACAO inteira, com sujeito proprio.

    circunstanciasContratacao, agravantesConduta e justificativaUrgencia vem
    depois de uma conjuncao do template ("...sendo que {x}"), entao "a Re
    garantiu..." e o formato certo. Por isso nao passam pelo filtro de sujeito
    de _complemento - ele as esvaziaria. So o formato: minuscula, sem ponto.
    """

    return _formatar_complemento(_as_str(valor))


def _com_ponto_e_virgula(valor: Any) -> str:
    """Item da tutela: o modelo encadeia as obrigacoes com ';' no fim."""

    texto = _formatar_complemento(_as_str(valor))
    return f"{texto};" if texto else ""


def _judicial_negativation_requested(state: Dict[str, Any], e: Dict[str, Any]) -> bool:
    parts = [
        state.get("document_generation_prompt"),
        state.get("custom_prompt"),
        state.get("problem_description"),
        state.get("case_summary"),
        e.get("tutelaObrigacao"),
        e.get("tutelaDetalhamento"),
    ]
    text = " ".join(_as_str(part) for part in parts if part)
    return bool(re.search(r"negativ|restri[cç][aã]o|restri[cç][oõ]es", text, flags=re.IGNORECASE))


def _ensure_judicial_negativation_tutela(tutela: str, state: Dict[str, Any], e: Dict[str, Any]) -> str:
    if not _judicial_negativation_requested(state, e):
        return tutela
    if re.search(r"negativ|restri[cç][aã]o|restri[cç][oõ]es", tutela or "", flags=re.IGNORECASE):
        return tutela
    negativation_item = (
        "abster-se de negativar o Autor ou manter restrição em seu nome por valores relacionados aos fatos narrados;"
    )
    if not tutela:
        return negativation_item
    return f"{tutela} {negativation_item}"


def _numeros_de_documento(valor: Any, total: int) -> str:
    """Numeros do rol citados no texto ('02 e 03'), conferidos contra o rol.

    O plano de 22/09 exige que os numeros batam com a ordem de
    `sections.documentos`. A LLM erra esse tipo de referencia com facilidade,
    entao aqui qualquer numero fora do rol e descartado - um "Documento 07" num
    rol de cinco e um documento que nao existe.
    """

    if total <= 0:
        return ""

    numeros = []
    for bruto in re.findall(r"\d+", _as_str(valor)):
        numero = int(bruto)
        if 1 <= numero <= total and numero not in numeros:
            numeros.append(numero)

    if not numeros:
        return ""

    formatados = [f"{n:02d}" for n in numeros]

    if len(formatados) == 1:
        return formatados[0]

    return ", ".join(formatados[:-1]) + f" e {formatados[-1]}"


def _tipo_acao(flags: Dict[str, bool]) -> str:
    """Nome da acao derivado dos pedidos que o template REALMENTE formula.

    Achado 6.3 da auditoria: o titulo anunciava "OBRIGACAO DE FAZER C/C
    INDENIZACAO POR DANOS MORAIS" (default fixo) enquanto a secao de pedidos
    nao continha nenhuma obrigacao de fazer. No modelo, a obrigacao de fazer so
    existe dentro da tutela de urgencia (item 5.2) - entao ela so pode ser
    anunciada no titulo quando essa flag estiver ligada.
    """
    nome = "INDENIZACAO POR DANOS MORAIS"
    if flags.get("danoMaterial"):
        nome = "INDENIZACAO POR DANOS MORAIS E MATERIAIS"
    if flags.get("tutelaUrgencia"):
        nome = f"OBRIGACAO DE FAZER C/C {nome}, COM PEDIDO DE TUTELA DE URGENCIA"
    return nome


# Missing monetary requests remain pending; estimates are not moral damages.
_VALOR_PENDENTE = "[VALOR A DEFINIR PELO ADVOGADO]"


def _valores_judicial(e: Dict[str, Any], estimated_value: Any) -> Tuple[str, str]:
    """Keep monetary roles separate; the fidelity review checks source support."""
    dano_moral = _as_str(e.get("valorDanoMoral"))
    if not _parse_brl(dano_moral):
        dano_moral = _VALOR_PENDENTE
    valor_causa = _as_str(e.get("valorCausa"))
    if not _parse_brl(valor_causa):
        valor_causa = _VALOR_PENDENTE
    return dano_moral, valor_causa


# ---------------------------------------------------------------------------
# Gate de validacao do payload montado (achado 3.1 / P0 da auditoria)
# ---------------------------------------------------------------------------

# Nenhum destes pode chegar ao PDF. O "None" desta lista e o achado 4.1.
_MARCADORES_PROIBIDOS = re.compile(r"\b(?:None|null|undefined|NaN)\b|\[PREENCHER|\[preencher")

# Campos sem os quais o documento sai capenga. Ficam DE FORA de proposito os
# que o contrato deixa para o advogado preencher depois (comarca, cidade/UF,
# advogado, OAB) e os que a IA ainda nao coleta (protocolo, canal de contato).
_CAMPOS_OBRIGATORIOS: Dict[str, Dict[str, Tuple[str, ...]]] = {
    "agreement": {
        "sections": ("consideracoes", "objeto", "obrigacoesPrimeiraParte", "obrigacoesSegundaParte", "quitacao"),
        "slots": ("dataExtenso",),
    },
    "amigavel": {
        "sections": ("oQueAconteceu", "oQueProponhe"),
        # legado fora do caminho principal do Writer.
        "slots": (),
    },
    "extrajudicial": {
        "sections": ("objeto", "fundamentos", "pedidos"),
        "slots": ("data",),
    },
    "judicial": {
        "sections": ("fatosContratacao", "elementosDefeito", "documentos"),
        "slots": ("tipoAcao", "valorDanoMoral", "valorCausa", "dataExtenso"),
    },
}

# Na v2 as quatro secoes de fatos sumiram: quem conta a historia e o texto do
# modelo, e o que nao pode faltar sao as lacunas que o completam.
_CAMPOS_OBRIGATORIOS_JUDICIAL_V2: Dict[str, Tuple[str, ...]] = {
    "sections": ("elementosDefeito", "documentos"),
    "slots": (
        "tipoAcao", "valorDanoMoral", "valorCausa", "dataExtenso",
        "descricaoContratacao", "descricaoConduta",
    ),
}


def _textos_do_payload(doc: Dict[str, Any]):
    """Itera (caminho, texto) por todo slot/section do payload."""
    for bloco in ("slots", "sections"):
        for chave, valor in (doc.get(bloco) or {}).items():
            if isinstance(valor, list):
                for i, item in enumerate(valor):
                    yield f"{bloco}.{chave}[{i}]", str(item)
            else:
                yield f"{bloco}.{chave}", str(valor)


def validar_payload(doc: Dict[str, Any]) -> List[str]:
    """Le o payload MONTADO e devolve a lista de problemas que impedem o PDF.

    A auditoria de 18/09/2026 mostrou que a validacao existia, quando muito,
    por campo - nunca sobre a versao final. Esta funcao roda sobre o documento
    pronto, imediatamente antes de ele entrar em documents[].
    """
    problemas: List[str] = []
    doc_type = doc.get("type") or "?"

    for caminho, texto in _textos_do_payload(doc):
        if _MARCADORES_PROIBIDOS.search(texto):
            problemas.append(f"{caminho}: marcador proibido em {texto[:60]!r}")

    exigidos = _CAMPOS_OBRIGATORIOS.get(doc_type, {})

    if doc_type == "judicial" and _peticao_v2():
        exigidos = _CAMPOS_OBRIGATORIOS_JUDICIAL_V2

    for bloco, chaves in exigidos.items():
        conteudo = doc.get(bloco) or {}
        for chave in chaves:
            valor = conteudo.get(chave)
            preenchido = _as_list(valor) if isinstance(valor, list) else _as_str(valor)
            if not preenchido:
                problemas.append(f"{bloco}.{chave}: obrigatorio e veio vazio")

    # Os dois modelos de notificacao exigem relato em no minimo 2 paragrafos.
    if doc_type == "extrajudicial":
        if len(_as_list((doc.get("sections") or {}).get("objeto"))) < 2:
            problemas.append("sections.objeto: o modelo exige no minimo 2 paragrafos")

    if doc_type == "amigavel":
        if len(_as_list((doc.get("sections") or {}).get("oQueAconteceu"))) < 2:
            problemas.append("sections.oQueAconteceu: o modelo exige no minimo 2 paragrafos")

    if doc_type == "agreement":
        consideracoes = _as_list((doc.get("sections") or {}).get("consideracoes"))
        if len(consideracoes) < 3:
            problemas.append("sections.consideracoes: o modelo exige considerandos do acordo")

    # Coerencia global da peca (achados 6.3 e 6.4).
    if doc_type == "judicial":
        slots = doc.get("slots") or {}
        flags = doc.get("flags") or {}
        tipo_acao = _as_str(slots.get("tipoAcao")).upper()
        if "OBRIGACAO DE FAZER" in tipo_acao and not flags.get("tutelaUrgencia"):
            problemas.append("slots.tipoAcao: anuncia obrigacao de fazer sem pedido correspondente")
        valor_causa = _parse_brl(slots.get("valorCausa"))
        if valor_causa <= 0 and slots.get("valorCausa") != _VALOR_PENDENTE:
            problemas.append("slots.valorCausa: sem valor (art. 292, VI do CPC)")
        elif valor_causa > 0 and valor_causa < _parse_brl(slots.get("valorDanoMoral")):
            problemas.append("slots.valorCausa: menor que o dano moral pedido")

    return problemas


# ---------------------------------------------------------------------------
# Enriquecimento via LLM (lacunas case-specific)
# ---------------------------------------------------------------------------

# Por tipo: chaves que o LLM deve preencher e se sao lista (string[]) ou texto.
_LLM_KEYS: Dict[str, Dict[str, str]] = {
    # Acordo extrajudicial usa o Modelo_Acordo_Extrajudicial.docx. O LLM
    # preenche apenas as lacunas das clausulas; a estrutura fixa vem do modelo.
    "agreement": {
        "consideracaoSituacao": "str",
        "consideracaoConflito": "str",
        "objetoControversia": "str",
        "obrigacoesPrimeiraParte": "list",
        "prazosPrimeiraParte": "list",
        "consequenciasPrimeiraParte": "list",
        "obrigacoesSegundaParte": "list",
        "prazosSegundaParte": "list",
        "consequenciasSegundaParte": "list",
        "tipoQuitacao": "str",
        "confidencialidade": "str",
        "homologacaoJudicial": "str",
    },
    "amigavel": {
        "oQueAconteceu": "list",
        "oQueProponhe": "str",
    },
    "extrajudicial": {
        "objeto": "list",
        "fundamentos": "list",
        "pedidos": "list",
    },
    "judicial": {
        # "tipoAcao" NAO esta aqui de proposito: o nome da acao passou a ser
        # derivado das flags por _tipo_acao() (achado 6.3 da auditoria).
        "deverViolado": "str",
        "sinteseConduta": "str",
        "justificativaExcludente": "str",
        "descricaoSofrimento": "str",
        "valorDanoMoral": "str",
        "valorCausa": "str",
        "justificativaSemAudiencia": "str",
        "fatosContratacao": "str",
        "fatosCondutaLesiva": "str",
        "fatosTentativasAdmin": "str",
        "fatosDano": "str",
        "elementosDefeito": "list",
        "documentos": "list",
    },
}

# Chaves que a PETICAO v2 acrescenta e as que ela aposenta (plano de 22/09,
# §4). Na v2 as subsecoes 3.1 a 3.4 voltam ao texto do modelo - sao 8
# paragrafos que a IA deixa de escrever -, e cada lacuna vira um pedaco
# pontual. So valem quando PETICAO_TEMPLATE_VERSION = "v2".
_LLM_KEYS_JUDICIAL_V2 = {
    "dataContratacao": "str",
    "descricaoContratacao": "str",
    "docContratacao": "str",
    "circunstanciasContratacao": "str",
    "dataConduta": "str",
    "descricaoConduta": "str",
    "docsConduta": "str",
    "agravantesConduta": "str",
    "tentativasAdministrativas": "str",
    "descricaoDano": "str",
    "docsDano": "str",
    "justificativaUrgencia": "str",
    "tutelaObrigacao": "str",
    "tutelaDetalhamento": "str",
}

_CHAVES_APOSENTADAS_NA_V2 = (
    "fatosContratacao", "fatosCondutaLesiva", "fatosTentativasAdmin", "fatosDano",
)


def _peticao_v2() -> bool:
    """True quando a plataforma ja subiu o template v2 da peticao.

    Ligado por PETICAO_TEMPLATE_VERSION=v2 (config.py). Enquanto a v2 nao
    estiver no ar do lado deles, mandar as chaves novas sairia como lacuna que
    o template antigo nao conhece.
    """

    return (get_settings().PETICAO_TEMPLATE_VERSION or "v1").strip().lower() == "v2"


def _chaves_do_tipo(doc_type: str) -> Dict[str, str]:
    """Chaves que a LLM preenche para o tipo, ja na versao em vigor."""

    chaves = dict(_LLM_KEYS[doc_type])

    if doc_type == "judicial" and _peticao_v2():
        for chave in _CHAVES_APOSENTADAS_NA_V2:
            chaves.pop(chave, None)
        chaves.update(_LLM_KEYS_JUDICIAL_V2)

    return chaves

# Instrucoes por chave (compoem o "esquema" pedido ao LLM).
_KEY_HINTS: Dict[str, str] = {
    # acordo extrajudicial - Modelo_Acordo_Extrajudicial.docx
    "consideracaoSituacao": (
        "CONSIDERANDO 1 do acordo, maximo 150 caracteres: descreva objetivamente a relacao juridica ou situacao subjacente"
    ),
    "consideracaoConflito": (
        "CONSIDERANDO 2 do acordo, maximo 250 caracteres: resuma os fatos ou posicoes conflitantes que motivam a composicao"
    ),
    "objetoControversia": (
        "completa a Clausula 1.1: descreva de forma imparcial a controversia que sera encerrada pelo acordo"
    ),
    "obrigacoesPrimeiraParte": (
        "obrigacoes concretas da PRIMEIRA PARTE, 1 por item; incluir valor, forma, prazo e condicao quando o advogado pedir"
    ),
    "prazosPrimeiraParte": "prazos de cumprimento das obrigacoes da PRIMEIRA PARTE, 1 por item",
    "consequenciasPrimeiraParte": "consequencias do descumprimento pela PRIMEIRA PARTE, 1 por item",
    "obrigacoesSegundaParte": (
        "obrigacoes concretas da SEGUNDA PARTE, 1 por item; incluir quitacao, retirada de reclamacao, entrega ou cooperacao quando couber"
    ),
    "prazosSegundaParte": "prazos de cumprimento das obrigacoes da SEGUNDA PARTE, 1 por item",
    "consequenciasSegundaParte": "consequencias do descumprimento pela SEGUNDA PARTE, 1 por item",
    "tipoQuitacao": (
        "redacao da quitacao: ampla apenas se o advogado pedir; caso contrario, limitada ao objeto do acordo e aos fatos descritos"
    ),
    "confidencialidade": "redacao objetiva da clausula de confidencialidade, ou deixe vazio se nao aplicavel",
    "homologacaoJudicial": "informe se as partes reservam homologacao judicial ou a dispensam; se nao houver instrucao, dispensa homologacao",

    # amigavel (Modelo 1) - quem FALA aqui e o proprio cliente, escrevendo
    # para a empresa. Terceira pessoa ("o notificante", "o solicitante") e o
    # defeito que a auditoria de 22/09 apontou na prova 3.3.
    "oQueAconteceu": (
        "relato do caso em NO MINIMO 2 paragrafos (lista com 2+ itens, cada item = 1 "
        "paragrafo), escrito na PRIMEIRA PESSOA pelo proprio cliente, como quem conta o "
        "que viveu: 'Comprei...', 'Paguei...', 'Entrei em contato...'. NUNCA escreva 'o "
        "notificante', 'o solicitante', 'o consumidor' nem 'a parte' - este texto e uma "
        "mensagem do cliente para a empresa, nao uma peca sobre ele"
    ),
    "oQueProponhe": (
        "o que o cliente PROPOE para resolver, na primeira pessoa e de forma concreta "
        "(ex.: 'Proponho a devolucao dos R$ 480,00 pagos pelo reparo, em ate dez dias'). "
        "Sem ameaca, sem prazo legal e sem citar processo"
    ),
    # extrajudicial
    "objeto": "relato do caso em NO MINIMO 2 paragrafos (lista com 2+ itens, cada item = 1 paragrafo)",
    "fundamentos": (
        "dispositivos legais aplicaveis, 1 por item, SEMPRE no formato "
        "'Art. <numero> do <Lei/Codigo> — <breve explicacao do direito que o dispositivo garante>'. "
        "A explicacao deve ter 1 frase curta e objetiva. "
        "Exemplos: 'Art. 475 do Codigo Civil — Direito de rescindir o contrato quando uma das partes "
        "descumpre suas obrigacoes.'; 'Art. 18 do CDC — Direito a troca, abatimento ou devolucao em caso "
        "de vicio do produto.'"
    ),
    "pedidos": "requerimentos do notificante, 1 por item",
    # judicial
    # COMPLEMENTOS GRAMATICAIS (achado 6.2 da auditoria de 18/09/2026).
    # O texto fixo do template ja abre a frase e o slot apenas a completa
    # ("...violou o dever de {deverViolado}"). Pedir "1 frase" fazia a LLM
    # devolver oracao com sujeito e o PDF saia com "o dever de A re violou...".
    # Por isso estes hints exigem complemento, nao frase.
    "deverViolado": (
        "COMPLETA a frase 'a re violou o dever de ...'. Comece por VERBO NO "
        "INFINITIVO, sem sujeito e sem ponto final. "
        "Ex.: 'informar previamente o custo do reparo'"
    ),
    "sinteseConduta": (
        "COMPLETA a frase 'conduta ilicita consistente em ...'. Comece por VERBO NO "
        "INFINITIVO, em minuscula, sem sujeito e sem ponto final. "
        "Ex.: 'prestar servico de reparo ineficaz e recusar-se a corrigi-lo'"
    ),
    "justificativaExcludente": (
        "COMPLETA a frase 'na medida em que ...'. Oracao em minuscula, sem ponto final "
        "e SEM comecar por 'nao ha'. "
        "Ex.: 'a falha decorreu exclusivamente do servico prestado pela re'"
    ),
    "descricaoSofrimento": (
        "COMPLETA a frase 'exposicao do autor a ...'. Use sintagma NOMINAL, sem "
        "sujeito e sem verbo conjugado, e sem ponto final. "
        "Ex.: 'sucessivas idas a assistencia tecnica sem qualquer solucao'"
    ),
    "valorDanoMoral": "valor de dano moral expressamente solicitado nas fontes (ausente: string vazia), por extenso seguido do numeral (ex.: 'cinco mil reais (R$ 5.000,00)')",
    "valorCausa": "valor da causa por extenso seguido do numeral (ex.: 'cinco mil reais (R$ 5.000,00)')",
    "justificativaSemAudiencia": "justificativa do desinteresse na audiencia de conciliacao (1 frase)",
    "fatosContratacao": "narrativa da contratacao/relacao de consumo (1 paragrafo)",
    "fatosCondutaLesiva": "narrativa da conduta lesiva da re (1 paragrafo)",
    "fatosTentativasAdmin": "narrativa das tentativas de solucao administrativa pelo autor (1 paragrafo)",
    "fatosDano": "narrativa do dano sofrido pelo autor (1 paragrafo)",
    "elementosDefeito": (
        "cada item COMPLETA a frase 'defeito consistente em ...'. Sintagma "
        "NOMINAL, sem sujeito e sem verbo conjugado. "
        "Ex.: 'reparo executado sem a troca da peca defeituosa'"
    ),
    "documentos": "rol de documentos que instruem a peca, 1 por item (ex.: 'comprovante de compra')",
    # judicial v2 - as lacunas de 3.1 a 3.4, que na v1 eram paragrafos
    # inteiros. Cada uma COMPLETA uma frase que o modelo ja escreveu, entao
    # vale a mesma regra: minuscula, sem ponto final.
    "dataContratacao": "data da contratacao por extenso (ex.: '12 de marco de 2026'). Se o caso nao disser a data, deixe vazio",
    "descricaoContratacao": (
        "COMPLETA 'o(a) Autor(a) ... '. Comece por VERBO NO PASSADO, sem ponto final. "
        "Ex.: 'contratou a assistencia tecnica da Re para o conserto de sua geladeira, "
        "mediante o pagamento de R$ 480,00 (quatrocentos e oitenta reais)'"
    ),
    "docContratacao": "numero do documento que prova a contratacao, no rol de 'documentos' (ex.: '01')",
    "circunstanciasContratacao": (
        "oracao completa sobre o que foi prometido/combinado na contratacao, sem ponto "
        "final. Ex.: 'a Re garantiu que a troca do compressor resolveria a falha'"
    ),
    "dataConduta": "data da conduta lesiva por extenso (ex.: '2 de abril de 2026'). Vazio se o caso nao disser",
    "descricaoConduta": (
        "COMPLETA 'a Re ... '. Comece por VERBO NO PASSADO, sem ponto final. "
        "Ex.: 'deixou de solucionar o defeito, que voltou a se manifestar dias depois'"
    ),
    "docsConduta": "numeros dos documentos que provam a conduta, no rol (ex.: '02 e 03')",
    "agravantesConduta": (
        "oracao com o que agrava a conduta, sem ponto final. "
        "Ex.: 'o reparo foi cobrado como definitivo e a Re nao informou as pecas trocadas'"
    ),
    "tentativasAdministrativas": (
        "COMPLETA 'tendo ... '. Comece por VERBO NO PARTICIPIO, sem ponto final. "
        "Ex.: 'registrado dois chamados na central e enviado solicitacao de resolucao "
        "amigavel pela plataforma Zellu em 10 de abril de 2026'"
    ),
    "descricaoDano": (
        "COMPLETA o que o autor sofreu, sem ponto final. Ex.: 'prejuizo de R$ 480,00 "
        "(quatrocentos e oitenta reais), alem da perda de alimentos'"
    ),
    "docsDano": "numeros dos documentos que provam o dano, no rol (ex.: '04 e 05')",
    "justificativaUrgencia": (
        "oracao que mostra a urgencia atual, sem ponto final. Ex.: 'a Autora segue sem "
        "refrigeracao para alimentos e medicamentos de uso continuo'"
    ),
    "tutelaObrigacao": (
        "obrigacao especifica pedida em tutela de urgencia, terminando em ';'. "
        "Ex.: 'consertar a geladeira da Autora ou substitui-la por outra equivalente, nova;'"
    ),
    "tutelaDetalhamento": (
        "o que assegura o cumprimento da obrigacao acima, terminando em ';'. "
        "Ex.: 'comprovar nos autos a execucao do servico, com a indicacao das pecas empregadas;'"
    ),
}

_DOC_KIND = {
    "agreement": (
        "Acordo Extrajudicial (instrumento particular com considerandos, objeto, "
        "obrigacoes reciprocas, prazos, consequencias, quitacao, confidencialidade "
        "quando cabivel, homologacao e disposicoes gerais)"
    ),
    "amigavel": (
        "Solicitacao de Resolucao Amigavel (uma MENSAGEM do proprio cliente para a "
        "empresa, em formato de e-mail; ele conta o que aconteceu e propoe a solucao, "
        "na primeira pessoa, antes de qualquer medida judicial)"
    ),
    "extrajudicial": (
        "Notificacao Extrajudicial (tom formal e tecnico; relata o caso, cita "
        "dispositivos legais e formula requerimentos ao notificado)"
    ),
    "judicial": (
        "Peticao Inicial - Relacao de Consumo (tom tecnico-juridico; narrativa "
        "dos fatos, caracterizacao do defeito, dano moral e valor da causa)"
    ),
}


def _template_contract(doc_type: str) -> str:
    if doc_type == "agreement":
        return """Contrato do modelo DOCX: Modelo_Acordo_Extrajudicial.docx
- Titulo obrigatorio: ACORDO EXTRAJUDICIAL.
- O documento e instrumento particular entre PRIMEIRA PARTE e SEGUNDA PARTE.
- Deve conter consideracoes: situacao subjacente em ate 150 caracteres, conflito em ate 250 caracteres, intencao de encerrar a controversia, boa-fe e igualdade negocial.
- Clausula 1: objeto da composicao definitiva, de forma imparcial.
- Clausulas 2 e 3: obrigacoes especificas de cada parte, prazos e consequencias do descumprimento.
- Clausula 4: quitacao e renuncia; quitacao ampla somente se a orientacao do advogado pedir. Sem instrucao expressa, limitar ao objeto do acordo e aos fatos descritos.
- Clausula 5: confidencialidade e LGPD somente quando adequada ou pedida.
- Clausula 6: homologacao judicial reservada ou dispensada conforme orientacao; sem instrucao, dispensar homologacao.
- Clausula 7: disposicoes gerais do modelo: validade, aditivo por escrito, sucessores, integralidade, tolerancia sem renuncia e assinatura digital.
- Testemunhas sao necessarias para titulo executivo extrajudicial; se nao houver dados das testemunhas, nao invente nomes nem CPFs."""
    if doc_type == "extrajudicial":
        return """Contrato do modelo DOCX: Modelo_Notificacao_Extrajudicial.docx
- Use o MODELO 2 - NOTIFICACAO EXTRAJUDICIAL.
- Identifique quem notifica e quem e notificado pelos dados estruturados, nao pelo texto livre.
- Objeto da notificacao deve ter no minimo 2 paragrafos completos.
- Fundamentos legais em topicos, com artigo e explicacao curta.
- Pedidos em topicos claros, com prazo fixo de 3 dias uteis pelo modelo.
- Se houve tentativa amigavel previa, mencionar no objeto quando constar do caso ou da orientacao do advogado."""
    if doc_type == "judicial":
        return """Contrato do modelo DOCX: Modelo_Peticao_Inicial_Consumidor.docx
- Titulo obrigatorio: PETICAO INICIAL - RELACAO DE CONSUMO.
- Estrutura base: legitimidade/competencia, gratuidade/prioridade quando cabivel, fatos 3.1 a 3.4, direito 4.1 a 4.6, tutela de urgencia quando pedida/cabivel, pedidos, valor da causa e rol de documentos.
- As lacunas devem completar o texto fixo do modelo, sem repetir a deixa do template.
- O autor e o cliente; a re e a empresa/parte contraria. Nunca inverter as partes.
- Valor da causa deve seguir o valor estimado do caso quando informado, salvo orientacao expressa em contrario no dado estruturado.
- Pedidos e fundamentos do advogado devem aparecer nas secoes juridicas correspondentes, sem criar uma secao propria para o prompt."""
    return ""


def _build_user_prompt(state: Dict[str, Any], doc_type: str) -> str:
    from src.services.case_evidence import evidence_context
    evidence = evidence_context(state)
    legal_category = state.get("legal_category") or "Geral"
    problem = (state.get("problem_description") or "").strip()
    rights = state.get("client_rights") or []
    urgency = state.get("urgency") or "Media"
    client_name = state.get("client_name") or "Cliente"
    opposing = state.get("opposing_party_name") or "parte contraria"
    estimated_value = state.get("potential_gain") or state.get("estimated_value") or 0
    lawyer_prompt = (state.get("document_generation_prompt") or "").strip()
    template_contract = _template_contract(doc_type)
    lawyer_block = ""
    if lawyer_prompt:
        lawyer_block = (
            "\nOrientacao do advogado para este documento (obrigatoria, mas NAO e relato do caso):\n"
            "<<<ORIENTACAO_DO_ADVOGADO>>>\n"
            f"{lawyer_prompt}\n"
            "<<<FIM_DA_ORIENTACAO_DO_ADVOGADO>>>\n"
            "Como aplicar:\n"
            "- Incorpore tudo que o advogado pediu nas lacunas corretas do modelo DOCX.\n"
            "- Nao copie este bloco literalmente para o documento.\n"
            "- Nao crie secao propria para estas orientacoes.\n"
            "- Se houver conflito entre orientacao e estrutura, a orientacao explicita prevalece; nao acrescente clausula proibida para preencher o modelo.\n"
        )
    rights_block = "\n".join(f"- {r}" for r in rights) if rights else "(nenhum direito mapeado)"

    keys = _chaves_do_tipo(doc_type)
    schema_lines = []
    for key, kind in keys.items():
        hint = _KEY_HINTS.get(key, "")
        if kind == "list":
            schema_lines.append(f'  "{key}": ["{hint}"]')
        else:
            schema_lines.append(f'  "{key}": "{hint}"')
    schema = "{\n" + ",\n".join(schema_lines) + "\n}"

    return f"""Tipo de documento alvo: {_DOC_KIND[doc_type]}

Regras do modelo aprovado pelo advogado:
{template_contract}

Dados do caso (use apenas o que estiver presente; NAO invente dados que faltarem):
- Categoria juridica: {legal_category}
- Urgencia: {urgency}
- Cliente (autor/notificante/1a parte): {client_name}
- Parte contraria (re/notificado/2a parte): {opposing}
- Valor estimado do dano/causa: {_brl(estimated_value)}
- Relato do cliente:
\"\"\"
{problem}
\"\"\"
- Direitos identificados na analise:
{rights_block}

{evidence}
{lawyer_block}

Responda APENAS com este JSON (preencha TODAS as chaves, sem markdown):
{schema}

Regras:
- Texto corrido, SEM markdown/HTML. Listas como arrays JSON de strings.
- NAO qualifique partes (nome/CPF/CNPJ/endereco) - isso vem separado.
- Fundamentacao ESPECIFICA do caso (cite artigos do CDC/CC/CPC quando couber).
- Em "fundamentos", cada item DEVE seguir o formato 'Art. <numero> do <Lei/Codigo> — <breve explicacao>'
  (numero do artigo + uma frase curta explicando o direito que ele garante). NUNCA enviar so o codigo.
- Respeite limites de caracteres indicados nas chaves.
- O prompt do advogado prevalece como conteudo obrigatorio, mas sempre dentro das clausulas/secoes do modelo DOCX.
- Nunca inclua no documento frases como "orientacao do advogado", "prompt", "instrucoes recebidas" ou o texto bruto do pedido."""


async def _enrich_with_llm(
    state: Dict[str, Any], doc_type: str, event_id: Optional[str] = None
) -> Dict[str, Any]:
    """Gera as lacunas case-specific do tipo via uma chamada LLM.

    Retorna dict (possivelmente parcial). Em erro, retorna {}.
    """
    try:
        settings = get_settings()
        llm = ChatModel(
            model=settings.COMPANY_RESPONSE_AI_MODEL,
            api_key=settings.OPENAI_API_KEY,
            temperature=0.2,
            max_tokens=2000,
            model_kwargs={"response_format": {"type": "json_object"}},
        )

        system_msg = (
            "Voce e um(a) advogado(a) que preenche AS LACUNAS de um documento "
            "juridico cujo texto fixo ja existe (modelo da plataforma). Saida "
            "exclusivamente em JSON valido, sem markdown e sem prosa fora do JSON. "
            "Nao qualifique as partes. Nao use HTML/markup. Linguagem formal e "
            "tecnica. Nao invente datas, valores ou dados que nao constem do caso."
        )
        user_msg = _build_user_prompt(state, doc_type)

        _t0 = time.perf_counter()
        response = await llm.complete([
            system_message(content=system_msg),
            user_message(content=user_msg),
        ])
        track_usage(
            module="writer",
            model=settings.COMPANY_RESPONSE_AI_MODEL,
            response=response,
            event_id=event_id,
            event_type="document_generation",
            company_id=state.get("company_id"),
            session_id=state.get("session_id"),
            ticket_id=state.get("ticket_id") or state.get("ticketId"),
            duration_ms=int((time.perf_counter() - _t0) * 1000),
        )
        content = _strip_code_fences(response.content or "")
        if not content:
            return {}
        parsed = json.loads(content)
        if not isinstance(parsed, dict):
            return {}

        # Coercao por tipo declarado em _LLM_KEYS.
        result: Dict[str, Any] = {}
        for key, kind in _chaves_do_tipo(doc_type).items():
            if key not in parsed:
                continue
            if kind == "list":
                val = _as_list(parsed[key])
                if val:
                    result[key] = val
            else:
                val = _as_str(parsed[key])
                if val:
                    result[key] = val
        return result
    except Exception as exc:
        print(f"[DOC-CONTENT] Falha no enriquecimento LLM type={doc_type} (fallback): {exc}")
        return {}


# ---------------------------------------------------------------------------
# Montagem dos payloads (puro - sem LLM)
# ---------------------------------------------------------------------------


def _strip_agreement_object_intro(texto: str) -> str:
    texto = _as_str(texto)
    if not texto:
        return ""
    texto = re.sub(
        r"^\s*o\s+presente\s+acordo\s+(?:tem\s+por\s+objeto|visa)\s+(?:a\s+)?(?:composi[cç][aã]o\s+definitiva\s+)?(?:decorrente\s+de\s+)?",
        "",
        texto,
        flags=re.IGNORECASE,
    )
    texto = re.sub(
        r"^\s*entre\s+as\s+partes\s+em\s+rela[cç][aã]o\s+(?:a|às|as|ao|aos)?\s*",
        "",
        texto,
        flags=re.IGNORECASE,
    )
    texto = re.sub(
        r"^\s*d(?:a|e|o|as|os)\s+(?=controv[eé]rsia|cobran[cç]as?|valores?|fatos?|rela[cç][aã]o|situa[cç][aã]o)",
        "",
        texto,
        flags=re.IGNORECASE,
    ).strip(" .")
    texto = re.sub(
        r"\bcobran[cç]as\s+indevidas\s+realizadas\s+pela\s+SEGUNDA\s+PARTE\b",
        "cobranças indevidas realizadas pela PRIMEIRA PARTE",
        texto,
        flags=re.IGNORECASE,
    )
    return texto


def _is_first_party_agreement_duty(value: str) -> bool:
    first_party_patterns = [
        r"abster-se\s+de\s+negativ",
        r"manter\s+restri[cç][aã]o",
        r"negativar",
        r"cobrad[oa]s?\s+indevidamente",
        r"responsabilidade\s+civil\s+por\s+danos\s+materiais\s+e\s+morais",
        r"obriga[cç][aã]o\s+de\s+absten[cç][aã]o",
        r"descumprimento\s+da\s+absten[cç][aã]o",
    ]
    return any(re.search(pattern, value, flags=re.IGNORECASE) for pattern in first_party_patterns)


def _agreement_second_party_obligations(items: List[str]) -> List[str]:
    """Mantem a quitacao limitada e remove renuncias amplas geradas pelo LLM."""
    cleaned: List[str] = []
    for item in items:
        value = _as_str(item)
        if not value:
            continue
        broad_patterns = [
            r"n[aã]o\s+(?:realizar|apresentar|formular|fazer)\s+(?:novas?\s+)?reclama",
            r"ren[uú]ncia\s+(?:ampla|geral|irrestrita)",
            r"quaisquer\s+(?:direitos|pretens[oõ]es|reclama[cç][oõ]es)",
        ]
        if _is_first_party_agreement_duty(value) or any(re.search(pattern, value, flags=re.IGNORECASE) for pattern in broad_patterns):
            continue
        cleaned.append(value)
    return cleaned or [
        "Dar quitação limitada ao objeto deste acordo após o cumprimento integral das obrigações assumidas pela PRIMEIRA PARTE."
    ]


def _agreement_second_party_ancillary(items: List[str], fallback: List[str]) -> List[str]:
    cleaned: List[str] = []
    for item in items:
        value = _as_str(item)
        if not value or _is_first_party_agreement_duty(value):
            continue
        cleaned.append(value)
    return cleaned or fallback


def _ensure_no_negativation_obligation(obligations: List[str], prompt: str) -> List[str]:
    prompt_text = _as_str(prompt)
    if not re.search(r"negativ", prompt_text, flags=re.IGNORECASE):
        return obligations
    if any(re.search(r"negativ", item, flags=re.IGNORECASE) for item in obligations):
        return obligations
    return obligations + [
        "Abster-se de negativar a SEGUNDA PARTE por valores relacionados aos fatos objeto deste acordo."
    ]


def _agreement_text(texto: str) -> str:
    replacements = {
        "composicao": "composição",
        "controversia": "controvérsia",
        "condicoes": "condições",
        "concordancia": "concordância",
        "Alteracoes": "Alterações",
        "serao": "serão",
        "validas": "válidas",
        "clausula": "cláusula",
        "circunstancia": "circunstância",
        "afetaria": "afetaria",
        "afetada": "afetada",
        "titulo": "título",
        "novacao": "novação",
        "ate": "até",
        "renuncia": "renúncia",
        "obrigacoes": "obrigações",
        "prejuizo": "prejuízo",
    }
    for old, new in replacements.items():
        texto = re.sub(rf"\b{old}\b", new, texto)
    return texto


def _assemble_agreement(state: Dict[str, Any], e: Dict[str, Any], dt: datetime) -> Dict[str, Any]:
    """Modelo_Acordo_Extrajudicial.docx.

    O acordo e uma das tres opcoes principais do Writer. Ele nao usa o modelo
    de resolucao amigavel: preenche as clausulas do instrumento particular.
    """

    problem = (state.get("problem_description") or "").strip()
    estimated_value = state.get("potential_gain") or state.get("estimated_value") or 0
    valor = _brl(estimated_value) if estimated_value else ""
    cidade_uf = _cidade_uf_str(state)

    consideracao_situacao = _cap(
        _as_str(e.get("consideracaoSituacao"))
        or (" ".join(problem.split()) if problem else "existe controversia entre as Partes decorrente da relacao juridica descrita no chamado"),
        150,
    )
    consideracao_conflito = _cap(
        _as_str(e.get("consideracaoConflito"))
        or (f"as Partes buscam compor a controvérsia relacionada ao valor de {valor}" if valor else "as Partes buscam compor a controvérsia de forma consensual"),
        250,
    )
    objeto = _strip_agreement_object_intro(e.get("objetoControversia")) or problem or "a controvérsia descrita no chamado"

    obrigacoes_primeira = _as_list(e.get("obrigacoesPrimeiraParte")) or [
        f"Pagar à SEGUNDA PARTE a quantia de {valor}, na forma e prazo ajustados entre as Partes." if valor else
        "Cumprir integralmente a proposta de composição aceita pelas Partes."
    ]
    obrigacoes_primeira = _ensure_no_negativation_obligation(
        obrigacoes_primeira,
        state.get("document_generation_prompt") or state.get("custom_prompt") or "",
    )
    prazos_primeira = _as_list(e.get("prazosPrimeiraParte")) or [
        "As obrigações deverão ser cumpridas no prazo indicado na proposta ou, se omisso, em até 5 dias úteis."
    ]
    consequencias_primeira = _as_list(e.get("consequenciasPrimeiraParte")) or [
        "O descumprimento autoriza a parte prejudicada a exigir o cumprimento específico, sem prejuízo de perdas, danos e demais medidas cabíveis."
    ]

    obrigacoes_segunda = _agreement_second_party_obligations(_as_list(e.get("obrigacoesSegundaParte")))
    prazos_segunda = _agreement_second_party_ancillary(
        _as_list(e.get("prazosSegundaParte")),
        ["A quitação será eficaz somente após a comprovação do cumprimento integral das obrigações assumidas."],
    )
    consequencias_segunda = _agreement_second_party_ancillary(
        _as_list(e.get("consequenciasSegundaParte")),
        ["Antes do cumprimento integral, a SEGUNDA PARTE preserva os direitos relacionados aos fatos e valores não satisfeitos."],
    )

    quitacao = _as_str(e.get("tipoQuitacao")) or (
        "Com o integral cumprimento das obrigações assumidas, as Partes darão quitação recíproca limitada exclusivamente ao objeto deste acordo e aos fatos descritos nas considerações, preservadas pretensões não abrangidas expressamente."
    )
    # Absence is not consent to these optional clauses.
    confidencialidade = _as_str(e.get("confidencialidade"))
    homologacao = _as_str(e.get("homologacaoJudicial"))

    sections = {
        "consideracoes": [
            _agreement_text(f"CONSIDERANDO que {consideracao_situacao};"),
            _agreement_text(f"CONSIDERANDO que {consideracao_conflito};"),
            "CONSIDERANDO que as Partes pretendem encerrar a controvérsia de forma definitiva, evitando desgaste e custos inerentes a uma demanda judicial;",
            "CONSIDERANDO que o presente acordo é celebrado de boa-fé, em condições de plena igualdade negocial entre as Partes, com expressa concordância de seus termos;",
        ],
        "objeto": _agreement_text(f"O presente acordo tem por objeto a composição definitiva decorrente de {objeto}."),
        "obrigacoesPrimeiraParte": [_agreement_text(item) for item in (obrigacoes_primeira + prazos_primeira + consequencias_primeira)],
        "obrigacoesSegundaParte": [_agreement_text(item) for item in (obrigacoes_segunda + prazos_segunda + consequencias_segunda)],
        "quitacao": _agreement_text(quitacao),
        "confidencialidade": _agreement_text(confidencialidade),
        "homologacaoJudicial": _agreement_text(homologacao),
        "disposicoesGerais": [
            "Alterações ao presente acordo somente serão válidas se formalizadas por escrito e assinadas pelas Partes.",
            "Caso qualquer cláusula seja declarada nula ou inexequível, tal circunstância não afetará a validade das demais.",
            "O presente acordo obriga as Partes, seus herdeiros e sucessores a qualquer título.",
            "Este acordo reflete a integralidade dos entendimentos entre as Partes sobre seu objeto, sem novação até o integral cumprimento.",
            "A eventual tolerância a descumprimento ou atraso não implicará renúncia de direitos.",
        ],
    }
    slots = {
        "cidadeUF": cidade_uf,
        "dataExtenso": _data_extenso(dt),
    }
    return {
        "type": "agreement",
        "templateVersion": _TEMPLATE_VERSION,
        "slots": slots,
        "sections": sections,
        "flags": {},
    }

def _assemble_amigavel(state: Dict[str, Any], e: Dict[str, Any], dt: datetime) -> Dict[str, Any]:
    """MODELO 1 - Solicitacao de Resolucao Amigavel (formato e-mail).

    Duas secoes e dois slots, e so. Sem cidade, sem data, sem assinatura e sem
    prazo (fixo em 24 horas pelo template). "COMO RESPONDER" e o fecho
    institucional sao da plataforma - a IA nao os envia.
    """

    problem = (state.get("problem_description") or "").strip()

    o_que_aconteceu = _as_list(e.get("oQueAconteceu"))

    if len(o_que_aconteceu) < 2 and problem:
        # O template exige 2 paragrafos. O relato cru do cliente ja e a voz
        # certa (primeira pessoa), entao ele serve de rede quando a LLM falha.
        o_que_aconteceu = _paragrafos_do_relato(problem)

    sections = {
        "oQueAconteceu": o_que_aconteceu,
        "oQueProponhe": _as_str(e.get("oQueProponhe")),
    }
    # Sem `protocolo`: a plataforma sobrescreve com o numero do chamado
    # (resposta 2 da auditoria dos 3 documentos, 21/09, §3).
    slots = {
        "canalContato": state.get("opposing_party_email") or "",
        "referenciaInterna": state.get("opposing_party_reference") or "",
    }
    # Sem flags: confidencialidade, homologacao e temTestemunhas eram do Acordo
    # Extrajudicial, substituido pelo Modelo 1 em 06/07. Nada na plataforma as
    # le (resposta 2 da auditoria dos 3 documentos, 21/09, §2).
    return {
        "type": "amigavel",
        "templateVersion": _TEMPLATE_VERSION,
        "slots": slots,
        "sections": sections,
        "flags": {},
    }


def _assemble_extrajudicial(state: Dict[str, Any], e: Dict[str, Any], dt: datetime) -> Dict[str, Any]:
    problem = (state.get("problem_description") or "").strip()
    rights = state.get("client_rights") or []

    objeto = e.get("objeto") or ([problem] if problem else [])
    if len(objeto) < 2 and problem:
        # template exige no minimo 2 paragrafos
        objeto = objeto + ["O notificante requer a solucao do problema no prazo desta notificacao."]

    sections = {
        "objeto": objeto,
        "fundamentos": e.get("fundamentos") or list(rights),
        "pedidos": e.get("pedidos")
        or ["Cumprimento integral das obrigacoes assumidas, no prazo desta notificacao."],
    }
    slots = {
        # Sem `protocolo`: a plataforma sobrescreve com o numero do chamado
        # (resposta 2 da auditoria dos 3 documentos, 21/09, §3).
        # canalContato tem fallback para opposingParty.email no backend.
        # referenciaInterna so aparece quando o cliente deu o numero do pedido,
        # do contrato ou do protocolo da empresa (coletado no intake).
        "referenciaInterna": state.get("opposing_party_reference") or "",
        "canalContato": state.get("opposing_party_email") or "",
        "cidadeUF": _cidade_uf_str(state),
        "data": _data_curta(dt),
    }
    # Notificacao NAO envia prazo (fixo 3 dias uteis pelo template). Sem flags.
    return {
        "type": "extrajudicial",
        "templateVersion": _TEMPLATE_VERSION,
        "slots": slots,
        "sections": sections,
        "flags": {},
    }


def _assemble_judicial(state: Dict[str, Any], e: Dict[str, Any], dt: datetime) -> Dict[str, Any]:
    problem = (state.get("problem_description") or "").strip()
    rights = state.get("client_rights") or []
    estimated_value = state.get("potential_gain") or state.get("estimated_value") or 0
    cidade_uf = _cidade_uf_str(state)

    # As flags vem antes dos slots porque o nome da acao e derivado delas.
    flags = {
        "gratuidade": True,      # default true (contrato)
        "prioridade": False,
        "danoMaterial": False,
        "tutelaUrgencia": False,
    }

    # elementosDefeito completa "defeito consistente em ..." - itens que vierem
    # como oracao com sujeito sao descartados (achado 6.2).
    elementos_defeito = _complementos_lista("elementosDefeito", e.get("elementosDefeito"))

    valor_dano_moral, valor_causa = _valores_judicial(e, estimated_value)

    documentos = e.get("documentos") or [
        "Documentos pessoais do autor.",
        "Comprovantes da relacao de consumo.",
    ]

    sections = {
        "elementosDefeito": _tres_elementos(elementos_defeito or list(rights)),
        "documentos": documentos,
    }

    if _peticao_v2():
        # 3.1 a 3.4 voltam ao texto do modelo: a IA preenche so as lacunas.
        sections.update({
            "tentativasAdministrativas": _complemento(
                "tentativasAdministrativas", e.get("tentativasAdministrativas")
            ),
            "descricaoDano": _complemento("descricaoDano", e.get("descricaoDano")),
            "tutelaObrigacao": _ensure_judicial_negativation_tutela(
                _com_ponto_e_virgula(e.get("tutelaObrigacao")), state, e
            ),
            "tutelaDetalhamento": _com_ponto_e_virgula(e.get("tutelaDetalhamento")),
        })
    else:
        sections.update({
            "fatosContratacao": e.get("fatosContratacao") or problem or "",
            "fatosCondutaLesiva": e.get("fatosCondutaLesiva") or "",
            "fatosTentativasAdmin": e.get("fatosTentativasAdmin") or "",
            "fatosDano": e.get("fatosDano") or "",
        })

    slots = {
        "vara": "Vara Civel",
        "comarcaUF": cidade_uf,
        "comarcaDomicilioAutor": cidade_uf,
        "tipoAcao": _tipo_acao(flags),
        "deverViolado": _complemento("deverViolado", e.get("deverViolado")),
        # Os quatro passam pelo mesmo filtro: o template ja abriu a frase, e o
        # slot so a completa (achado 6.2 de 18/09, ainda visto em 22/09).
        "sinteseConduta": _complemento("sinteseConduta", e.get("sinteseConduta")),
        "justificativaExcludente": _complemento(
            "justificativaExcludente", e.get("justificativaExcludente")
        ),
        "descricaoSofrimento": _complemento("descricaoSofrimento", e.get("descricaoSofrimento")),
        "valorDanoMoral": valor_dano_moral,
        "justificativaSemAudiencia": e.get("justificativaSemAudiencia")
        or "O autor nao tem interesse na autocomposicao diante da resistencia da re.",
        # Sem advogado coletado pela IA -> placeholder do template.
        "advogadoNome": state.get("lawyer_name") or "",
        "advogadoOAB": state.get("lawyer_oab") or "",
        "advogadoNomeOAB": (
            f"{state.get('lawyer_name')} - {state.get('lawyer_oab')}"
            if state.get("lawyer_name") and state.get("lawyer_oab")
            else ""
        ),
        "valorCausa": valor_causa,
        "cidadeUF": cidade_uf,
        "dataExtenso": _data_extenso(dt),
    }

    if _peticao_v2():
        total_documentos = len(documentos)
        slots.update({
            "dataContratacao": _as_str(e.get("dataContratacao")),
            "descricaoContratacao": _complemento(
                "descricaoContratacao", e.get("descricaoContratacao")
            ),
            "docContratacao": _numeros_de_documento(e.get("docContratacao"), total_documentos),
            "circunstanciasContratacao": _oracao(e.get("circunstanciasContratacao")),
            "dataConduta": _as_str(e.get("dataConduta")),
            "descricaoConduta": _complemento("descricaoConduta", e.get("descricaoConduta")),
            "docsConduta": _numeros_de_documento(e.get("docsConduta"), total_documentos),
            "agravantesConduta": _oracao(e.get("agravantesConduta")),
            "docsDano": _numeros_de_documento(e.get("docsDano"), total_documentos),
            "justificativaUrgencia": _oracao(e.get("justificativaUrgencia")),
        })

    return {
        "type": "judicial",
        "templateVersion": "v2" if _peticao_v2() else _TEMPLATE_VERSION,
        "slots": slots,
        "sections": sections,
        "flags": flags,
    }


_ASSEMBLERS = {
    "agreement": _assemble_agreement,
    "amigavel": _assemble_amigavel,
    "extrajudicial": _assemble_extrajudicial,
    "judicial": _assemble_judicial,
}


# ---------------------------------------------------------------------------
# API publica
# ---------------------------------------------------------------------------

async def build_all_documents(
    state: Dict[str, Any],
    recommendations: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Gera UM documento por tipo (agreement + extrajudicial + judicial) no
    shape LegalDocumentPayload, conforme contrato 20260618.

    Retorna a tupla (documents, recommended):
        - documents: lista de ate 3 payloads (1 por tipo). Tipos cuja montagem
          falhe sao omitidos sem derrubar os demais.
        - recommended: o payload do tipo com maior score (campo singular
          `document`, mantido para retrocompatibilidade do backend).
    """
    recommended_type = _pick_type(recommendations)
    dt = _now()

    # eventId unico p/ correlacionar as 3 chamadas LLM deste fluxo de geracao.
    usage_event_id = new_event_id()

    # 3 enriquecimentos em paralelo - mesmo wall-clock que uma chamada unica.
    enrich_results = await asyncio.gather(
        *[_enrich_with_llm(state, t, event_id=usage_event_id) for t in _VALID_TYPES],
        return_exceptions=True,
    )

    documents: List[Dict[str, Any]] = []
    for doc_type, result in zip(_VALID_TYPES, enrich_results):
        enriched = result if isinstance(result, dict) else {}
        try:
            doc = _ASSEMBLERS[doc_type](state, enriched, dt)
        except Exception as exc:
            print(f"[DOC-CONTENT] Falha ao montar payload type={doc_type}: {exc}")
            continue

        # Gate final (achado 3.1 da auditoria de 18/09): o documento so entra
        # em documents[] depois de lido inteiro. Reprovado, ele e omitido. Isso
        # NAO e entregar menos: tipo ausente cai no gerador legado da
        # plataforma, o PDF generico fora do modelo do escritorio (que desde
        # 21/09 tambem recusa marcador proibido). Ainda assim e melhor do que
        # expedir o nosso com marcador ou com titulo que contradiz os pedidos.
        problemas = validar_payload(doc)
        if problemas:
            print(
                f"[DOC-CONTENT] payload type={doc_type} REPROVADO no gate e omitido: "
                + "; ".join(problemas)
            )
            continue

        documents.append(doc)

    recommended = next(
        (d for d in documents if d.get("type") == recommended_type), None
    )
    if recommended is None and documents:
        recommended = documents[0]

    return documents, recommended


def _proposta_de_pedidos(pedidos: List[str]) -> str:
    """Transforma os requerimentos da notificacao em uma proposta em prosa."""
    limpos = [p.rstrip(" .;") for p in pedidos if p.strip()]
    if not limpos:
        return ""
    if len(limpos) == 1:
        return f"{limpos[0]}."
    return (
        "O solicitante propoe, para encerrar a questao de forma amigavel: "
        + "; ".join(limpos[:-1])
        + f"; e {limpos[-1]}."
    )


def refine_case_details(
    case_details: Dict[str, Any], documents: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """Preenche o relato e a proposta do Modelo 1 com o que a LLM ja escreveu.

    O PDF "Solicitacao de Resolucao Amigavel" (Modelo 1 do LZL-006-C) NAO sai
    de documents[] - a plataforma o monta a partir de caseDetails: a secao
    "O QUE ACONTECEU" vem de `description` e "O QUE O SOLICITANTE PROPOE" vem
    de `expectedSolution`. Ate a auditoria de 18/09/2026 os dois saiam crus:
    o relato em um unico bloco corrido (achado 4.2) e a proposta como frase
    generica, que ainda vazava um valor nulo (achados 4.1 e 4.4).

    A fonte agora e o payload da PROPRIA amigavel, que desde a correcao de
    22/09 traz `oQueAconteceu` e `oQueProponhe` escritos na voz do cliente. A
    notificacao fica como reserva: ela e a mesma historia, mas contada em
    terceira pessoa ("o notificante"), que foi exatamente o que a auditoria de
    22/09 apontou na prova 3.3.

    Nao substitui nada por vazio: se nenhum dos dois veio, caseDetails fica
    como estava.
    """
    por_tipo = {d.get("type"): d for d in documents}

    amigavel_sections = (por_tipo.get("amigavel") or {}).get("sections") or {}
    extrajudicial_sections = (por_tipo.get("extrajudicial") or {}).get("sections") or {}

    relato = _as_list(amigavel_sections.get("oQueAconteceu"))
    if len(relato) < 2:
        relato = _as_list(extrajudicial_sections.get("objeto"))

    if len(relato) >= 2:
        case_details["description"] = "\n\n".join(relato)

    proposta = _as_str(amigavel_sections.get("oQueProponhe"))
    if not proposta:
        proposta = _proposta_de_pedidos(_as_list(extrajudicial_sections.get("pedidos")))

    if proposta:
        case_details["expectedSolution"] = proposta

    return case_details


async def build_document_content(
    state: Dict[str, Any],
    recommendations: Optional[List[Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    """[Retrocompatibilidade] Devolve apenas o documento do tipo recomendado."""
    _, recommended = await build_all_documents(state, recommendations)
    return recommended
