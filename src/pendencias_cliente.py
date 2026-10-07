# -*- coding: utf-8 -*-
"""
O que o ZelinhU ja SOLICITOU pelo sistema - e o que depende do cliente.

HISTORICO
---------
08/09/2026 (QA de reparo automotivo): o ZelinhU prometia em nome do cliente
("Enviaremos hoje as NFs, comprovantes e janelas"). Este modulo nasceu como
remendo: guardava em TEXTO o que a empresa pedia ao cliente e despejava a lista
no fechamento, porque a flag das solicitacoes estava desligada e nao havia como
PEDIR nada a ele.

10/09/2026 (QA de notebook): o remendo virou o defeito. A flag foi ligada em
08/09, mas a lista em texto nunca foi ligada ao `action: "request"`. O ZelinhU
identificava o item, dizia "levarei o pedido a ele" - frase que este modulo
mandava dizer - e nada acontecia. O fechamento despejou 20 itens, varios
repetidos com outras palavras, e um deles a propria empresa ja tinha resolvido.

O QUE ELE FAZ AGORA
-------------------
A fonte da verdade e a SOLICITACAO. O que a empresa pede e so o cliente pode dar
sai como `request` com audience "client", no turno em que aparece. Este modulo
guarda o registro do que ja foi solicitado - para nao pedir de novo e para dar
baixa quando a resposta volta - e rende esse registro para os prompts.

Nao existe mais lista paralela em texto. Sem ela, nao ha onde um item repetido
ou obsoleto se acumular.
"""

import re
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

from src.utils.veredito_documentos import VEREDITO_INVALIDO, veredito_do_pedido

# Registro por sessao. O backend corta em 10 pendentes ABERTAS; o historico
# (abertas + respondidas) cresce devagar, e o teto e so a rede.
MAX_SOLICITACOES_NA_SESSAO = 30

STATUS_ABERTA = "aberta"
STATUS_ATENDIDA = "atendida"
STATUS_RECUSADA = "recusada"
# O backend fechou como atendido, mas o documento nao era o que foi pedido.
STATUS_INVALIDA = "documento invalido"

# Quantas vezes o mesmo pedido pode voltar com documento invalido. Na terceira o
# ZelinhU para de pedir de novo - e o "3 erros no mesmo pedido" do backend (spec
# 01/09, §6), contado aqui porque cada pedido refeito ganha um requestId novo, e
# o contador deles nao o reconhece como o mesmo.
MAX_TENTATIVAS_POR_PEDIDO = 3

_ROTULO_DO_PUBLICO = {"client": "cliente", "company": "empresa"}


def _normalizar(texto: str) -> str:
    """Chave de comparacao: sem acento, sem caixa, sem pontuacao solta."""
    limpo = unicodedata.normalize("NFD", (texto or "").strip().lower())
    limpo = "".join(c for c in limpo if unicodedata.category(c) != "Mn")
    limpo = re.sub(r"[^a-z0-9]+", " ", limpo)
    return limpo.strip()


def _campo(pedido: Any, nome: str) -> str:
    """Le um campo do pedido, venha ele como dict ou como modelo Pydantic."""
    valor = pedido.get(nome) if isinstance(pedido, dict) else getattr(pedido, nome, None)
    return str(valor or "").strip()


def _chave(pedido: Any) -> Tuple[str, str]:
    return _campo(pedido, "audience"), _normalizar(_campo(pedido, "title"))


# Palavras que nao individualizam um pedido. Sem tira-las, "comprovante de
# pagamento" e "comprovante do pagamento" ja seriam pedidos diferentes.
_PALAVRAS_VAZIAS = frozenset({
    "de", "da", "do", "das", "dos", "e", "o", "a", "os", "as", "um", "uma",
    "para", "por", "com", "em", "no", "na", "nos", "nas", "ao", "aos", "que",
    "seu", "sua", "seus", "suas", "the", "of", "completo", "completa",
})

_MESES = frozenset({
    "janeiro", "fevereiro", "marco", "abril", "maio", "junho", "julho",
    "agosto", "setembro", "outubro", "novembro", "dezembro",
})


def _termos(texto: str) -> frozenset:
    """As palavras que carregam o significado do titulo."""
    return frozenset(
        palavra
        for palavra in _normalizar(texto).split()
        if len(palavra) > 2 and palavra not in _PALAVRAS_VAZIAS
    )


def _distintivos(termos: frozenset) -> frozenset:
    """O que individualiza UMA instancia do pedido: numero, valor ou mes.

    E o freio do casamento por significado. "Comprovante de marco" e
    "comprovante de abril" tem um termo em comum e um distintivo diferente:
    sao dois pedidos, e engolir o segundo deixaria a empresa sem o documento.
    """
    return frozenset(
        termo for termo in termos
        if termo in _MESES or any(caractere.isdigit() for caractere in termo)
    )


def _e_o_mesmo_pedido(novo: Any, feito: Any) -> bool:
    """Se os dois pedidos sao a mesma coisa dita de dois jeitos.

    O QUE O TITULO NORMALIZADO NAO PEGAVA (QA de 10/09, achado 4)
    -------------------------------------------------------------
    A empresa repete o pedido a cada rodada e o modelo reescreve o rotulo. O
    checklist final saiu com 20 itens: "uso pessoal ou corporativo" tres vezes,
    endereco e janelas de coleta em varias formulacoes. Comparar o texto
    normalizado so pega a repeticao literal - e a repeticao literal e
    justamente a que nao acontecia.

    A regra aqui: mesmo publico, e o conjunto de termos de um contido no do
    outro. "Endereco para coleta" esta contido em "Endereco, contato e janelas
    de coleta", entao e o mesmo pedido.

    Dois freios, para o filtro nao engolir pedido legitimo:

    1. o menor conjunto precisa de DOIS termos. Um pedido de uma palavra so
       ("Comprovante") casaria com todos os outros que a contenham;
    2. os distintivos - numero, valor, mes - tem que ser os MESMOS. E o que
       separa "comprovante de marco" de "comprovante de abril".
    """
    publico_novo, titulo_novo = _chave(novo)
    publico_feito, titulo_feito = _chave(feito)

    if publico_novo != publico_feito:
        return False
    if titulo_novo == titulo_feito:
        return True

    termos_novo = _termos(titulo_novo)
    termos_feito = _termos(titulo_feito)
    if not termos_novo or not termos_feito:
        return False

    if not (termos_novo <= termos_feito or termos_feito <= termos_novo):
        return False

    if min(len(termos_novo), len(termos_feito)) < 2:
        return False

    return _distintivos(termos_novo) == _distintivos(termos_feito)


def filtrar_ja_solicitados(
    pedidos: Optional[List[Any]], feitas: Optional[List[Dict[str, Any]]]
) -> Tuple[List[Any], List[Any]]:
    """
    Separa os pedidos novos dos que ja foram feitos.

    A empresa repete o mesmo pedido a cada rodada. O prompt da analise recebe a
    lista do que ja foi solicitado e a regra de nao pedir de novo; isto e a rede
    em codigo para quando o modelo reescreve o rotulo. Tambem descarta repeticao
    DENTRO da mesma rajada, e pedido sem titulo, que o backend recusaria.

    A comparacao e por SIGNIFICADO, nao por texto (QA de 10/09, achado 4): ver
    _e_o_mesmo_pedido. O titulo normalizado sozinho so pegava a repeticao
    literal, que e justamente a que nao acontecia.

    O pedido com documento invalido tambem conta como feito, de proposito: quem
    o refaz e a retomada, com o teto de MAX_TENTATIVAS_POR_PEDIDO. Liberado aqui,
    o modelo poderia repedi-lo sem limite.

    Returns:
        (novos, descartados)
    """
    vistas: List[Any] = list(feitas or [])
    novos: List[Any] = []
    descartados: List[Any] = []

    for pedido in pedidos or []:
        if not _chave(pedido)[1]:
            descartados.append(pedido)
            continue
        if any(_e_o_mesmo_pedido(pedido, vista) for vista in vistas):
            descartados.append(pedido)
            continue
        vistas.append(pedido)
        novos.append(pedido)

    return novos, descartados


def registrar_solicitacoes(session: dict, aceitas: List[Dict[str, Any]]) -> None:
    """Guarda o que o backend ACEITOU como pendencia, com o requestId quando veio."""
    feitas = session.setdefault("solicitacoesFeitas", [])

    for pedido in aceitas or []:
        title = _campo(pedido, "title")
        audience = _campo(pedido, "audience")
        if not title or audience not in {"client", "company"}:
            continue
        candidate = {"title": title, "audience": audience}
        # O pedido refeito (documento invalido) volta como NOVO registro aberto:
        # o antigo fica como historico e conta para o teto de tentativas. Antes
        # ele casava com o registro invalido e sumia - a retomada seguinte nao
        # tinha o que fechar e o teto nunca disparava.
        if any(
            _e_o_mesmo_pedido(candidate, feita) and feita.get("status") != STATUS_INVALIDA
            for feita in feitas
        ):
            continue
        registro = {
            "title": title,
            "audience": audience,
            "status": STATUS_ABERTA,
        }
        request_id = _campo(pedido, "requestId")
        if request_id:
            registro["requestId"] = request_id
        feitas.append(registro)

    del feitas[:-MAX_SOLICITACOES_NA_SESSAO]


# O desfecho de um pedido no vocabulario do backend (contrato 20260911, §2.1),
# traduzido para o nosso registro.
_STATUS_DO_CONTRATO = {"fulfilled": STATUS_ATENDIDA, "declined": STATUS_RECUSADA}


def dar_baixa(
    session: dict,
    recusadas: bool,
    status_por_pedido: Optional[Dict[str, str]] = None,
    veredictos: Optional[List[Dict[str, str]]] = None,
) -> List[Dict[str, Any]]:
    """
    Fecha as solicitacoes abertas quando o resume chega.

    O resume so sai quando a ULTIMA pendencia da rajada foi resolvida (spec
    01/09, secao 3.1), entao todas as abertas fecham juntas.

    O DESFECHO E POR PEDIDO (contrato 20260911, §2.2)
    -------------------------------------------------
    O `resumeReason` e UM so para a rajada, e a regra do backend passou a ser
    "basta uma atendida para vir request_fulfilled". Fechar a rajada inteira por
    ele marcaria como ATENDIDA a solicitacao que a pessoa recusou - e o registro
    de status e o que o proximo turno le para saber o que ainda falta.

    Com `status_por_pedido` (requestId -> fulfilled/declined), cada um fecha com
    o desfecho dele. Quem nao estiver no mapa - pedido de antes da Etapa 1, ou
    sem requestId - cai no comportamento de sempre, pelo `recusadas`.

    O VEREDITO VENCE O "ATENDIDA" (QA de 14/09/2026)
    ------------------------------------------------
    O backend fecha como atendido todo pedido que recebeu arquivo: ele confere
    seguranca, nao conteudo. Gravar "atendida" num pedido cujo documento foi
    julgado invalido dizia ao proximo turno que o comprovante tinha chegado.
    """
    padrao = STATUS_RECUSADA if recusadas else STATUS_ATENDIDA
    por_pedido = status_por_pedido or {}
    baixadas = []

    for registro in session.get("solicitacoesFeitas") or []:
        if registro.get("status") != STATUS_ABERTA:
            continue

        do_contrato = por_pedido.get(_campo(registro, "requestId"))
        status = _STATUS_DO_CONTRATO.get(do_contrato, padrao)

        veredito = veredito_do_pedido(registro, veredictos)
        if status == STATUS_ATENDIDA and veredito and veredito.get("veredito") == VEREDITO_INVALIDO:
            status = STATUS_INVALIDA

        registro["status"] = status
        baixadas.append(registro)

    return baixadas


def tentativas_invalidas(session: dict, pedido: Any) -> int:
    """Quantas vezes este pedido ja voltou com documento invalido.

    Conta pelo mesmo par (publico, titulo) do filtro de repeticao: o pedido
    refeito sai com o mesmo titulo, e cada tentativa ganha um requestId novo.
    """
    chave = _chave(pedido)
    return sum(
        1
        for feita in session.get("solicitacoesFeitas") or []
        if feita.get("status") == STATUS_INVALIDA and _chave(feita) == chave
    )


def titulos_ao_cliente(pedidos: Optional[List[Any]]) -> List[str]:
    """Os titulos dos pedidos dirigidos ao cliente."""
    return [
        _campo(pedido, "title")
        for pedido in pedidos or []
        if _campo(pedido, "audience") == "client" and _campo(pedido, "title")
    ]


def render_ja_solicitado(feitas: Optional[List[Dict[str, Any]]]) -> str:
    """
    Bloco do prompt da ANALISE: o que ja foi pedido, a quem, e em que pe esta.

    E o que faltava para o modelo parar de reescrever o mesmo pedido a cada
    rodada: ele extraia do zero, sem ver a lista, e cada rodada saia com outras
    palavras. Vazio quando nada foi pedido - cabecalho seguido de nada faz o
    modelo inventar itens.
    """
    if not feitas:
        return ""

    linhas = ["JA SOLICITADO (nao peca de novo, nem com outras palavras):"]
    for feita in feitas:
        publico = _ROTULO_DO_PUBLICO.get(feita.get("audience"), feita.get("audience") or "?")
        linhas.append(f"- [{publico}] {feita.get('title')} - {feita.get('status')}")

    return "\n".join(linhas)


def render_para_prompt(feitas: Optional[List[Dict[str, Any]]]) -> str:
    """
    Bloco do prompt de REDACAO: o que depende do cliente, e o que nao dizer.

    As tres proibicoes de 08/09 continuam. A conduta que este bloco mandava
    seguir - "diga que voce vai leva-los a ele" - saiu: foi ela que produziu, em
    10/09, quatro promessas que nenhum codigo cumpria.
    """
    ao_cliente = [feita for feita in (feitas or []) if feita.get("audience") == "client"]
    if not ao_cliente:
        return ""

    linhas = [
        "# O QUE DEPENDE DO CLIENTE, NAO DE VOCE",
        "",
        "Estes itens foram SOLICITADOS ao cliente pelo sistema:",
        "",
    ]
    linhas.extend(f"- {feita.get('title')} ({feita.get('status')})" for feita in ao_cliente)
    linhas.extend([
        "",
        "REGRAS, e elas tem precedencia sobre o resto do prompt:",
        "- NAO prometa entregar nenhum item do cliente. Voce nao os tem, e nao pode",
        "  saber se ele os tem.",
        "- NAO responda por ele o que a empresa perguntou a ele (se o uso e",
        "  pessoal ou corporativo, se autoriza a desmontagem, qual a agencia).",
        "- NAO assuma prazo em nome dele.",
        "- NAO diga que vai \"levar\" nada ao cliente: quem leva e o sistema, pela",
        "  solicitacao. Diga so a verdade - que o item depende do cliente e, para",
        "  os marcados como aberta acima, que a solicitacao ja esta com ele.",
    ])

    return "\n".join(linhas)


def render_para_cliente(pendencias: Optional[List[str]]) -> str:
    """
    Bloco da mensagem de fechamento, enderecado ao cliente.

    So recebe o que a empresa pediu ao cliente NO PROPRIO turno do fechamento,
    quando houve acordo. Nesse caso o pedido nao pode sair como solicitacao - o
    backend recusa pedido e fechamento juntos (REQUEST_AND_TERMINAL_CONFLICT) -, e
    listar e o que impede o item de sumir. Todo o resto ja saiu como solicitacao
    antes e voltou respondido.
    """
    if not pendencias:
        return ""

    linhas = [
        "Alem da decisao sobre a proposta, a empresa aguarda os itens abaixo, que",
        "dependem de voce:",
        "",
    ]
    linhas.extend(f"- {item}" for item in pendencias)
    linhas.append("")
    linhas.append(
        "Estes itens continuam pendentes: eles nao foram enviados durante a "
        "negociacao."
    )

    return "\n".join(linhas)
