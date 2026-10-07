# -*- coding: utf-8 -*-
"""
O veredito sobre o documento que chegou numa solicitacao atendida.

QUEM VALIDA O QUE (spec 20260901, §5.1 - decidido pelo Zellu em 01/09)
----------------------------------------------------------------------
    Seguranca  -> backend deles: "este arquivo e seguro de abrir"
    Correcao   -> AQUI, ZelinhU:  "este arquivo e (ou nao e) o que foi pedido"

Sao perguntas diferentes e nenhum dos dois lados consegue responder a do outro.
O backend recusa .exe renomeado, PDF com script e macro em .docx; nada disso diz
se o comprovante e do mes certo, se o CPF e da pessoa certa, ou se o contrato e
o da compra em discussao.

O BURACO QUE ISTO FECHA
-----------------------
    O ZelinhU pede o CPF. O cliente sobe um arquivo. E o CPF de outra pessoa, ou
    o documento errado, ou ilegivel. Hoje nada verifica isso. A pessoa marca
    "atendi", a negociacao retoma, e o ZelinhU segue achando que recebeu o que
    pediu.

⚠️ O QUE AINDA NAO EXISTE, E NAO E NOSSO
----------------------------------------
O veredito nasce aqui, mas NAO TEM PARA ONDE IR. O canal de volta - dizer ao
backend "invalido, e por isto" - e a emenda de contrato que o §5.3 item 2 da spec
de 01/09 lista como pendente do lado deles. O contador de 3 erros e a escalacao
para humano tambem sao deles (§6).

Enquanto o canal nao existe, o veredito serve a UMA coisa, que ja e muita: o
ZelinhU para de tratar como entregue um documento que nao atende ao que ele
pediu. Ele fica registrado na sessao, pronto para ser despachado no dia em que
houver rota - sem que nada aqui precise mudar.

🔴 PII: o `motivo` vai para o estado do agente e um dia para o backend. Ele diz o
QUE esta errado ("o comprovante e de fevereiro, o pedido era marco"), nunca
transcreve o dado do documento.
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

VEREDITO_VALIDO = "valido"
VEREDITO_INVALIDO = "invalido"
VEREDITO_INDETERMINADO = "indeterminado"

# O VOCABULARIO DO CONTRATO (20260911, §3.2)
# ------------------------------------------
# Dentro de casa o veredito e em portugues, como o resto do projeto; no fio com
# o backend ele e em ingles. A traducao mora aqui, num lugar so, e vale nas duas
# direcoes: sai na resposta do `verify_request` e volta em `requests[].verdict`
# no resume.
VEREDITO_PARA_CONTRATO = {
    VEREDITO_VALIDO: "valid",
    VEREDITO_INVALIDO: "invalid",
    VEREDITO_INDETERMINADO: "undetermined",
}

VEREDITO_DO_CONTRATO = {ingles: nosso for nosso, ingles in VEREDITO_PARA_CONTRATO.items()}


def traduzir_do_contrato(valor: Optional[str]) -> Optional[str]:
    """
    O `verdict` do payload no nosso vocabulario, ou None quando nao ha veredito.

    🔴 `null` E DIFERENTE DE `undetermined` (contrato 20260911, §3.1), e a
    diferenca e a rede de seguranca inteira:

        undetermined -> eles LERAM e nao conseguiram decidir. Ler de novo daria
                        no mesmo, entao aproveitamos o veredito deles.
        null/ausente -> NINGUEM conferiu (a chamada falhou, ou a Etapa 2 ainda
                        nao existia). Aqui nos julgamos na retomada, como sempre
                        fizemos.

    Tratar os dois como a mesma coisa desligaria o julgamento justamente quando
    o documento nao foi conferido por ninguem.
    """
    if not valor:
        return None
    return VEREDITO_DO_CONTRATO.get(str(valor).strip().lower())


# Motivo de um veredito que nao foi emitido aqui. O motivo de verdade - a frase
# que diz o que o documento prova ou deixa de provar - ja foi mostrada a quem
# enviou, no momento do envio; ele nao volta no `requests[]`.
MOTIVO_CONFERIDO_NA_ENTREGA = "Conferido no momento do envio."

# Motivo padrao de quem nao pode julgar. Nao e recusa: e ausencia de leitura, e a
# diferenca importa - o contador do backend reage a "invalido", e culpar a pessoa
# pelo que NOS nao conseguimos ler seria puni-la por um limite nosso.
MOTIVO_NAO_LIDO = (
    "O arquivo chegou, mas nao foi possivel ler o conteudo dele - "
    "sem leitura nao ha como dizer se atende ao pedido."
)


class VereditoDoPedido(BaseModel):
    """O julgamento de UMA solicitacao, nao de um arquivo.

    A unidade e o pedido porque e assim que o backend conta: 'ao errar 3 vezes o
    MESMO pedido, escalar' (§6). Um pedido pode ser atendido por varios arquivos.
    """

    solicitacao: str = Field(..., description="Titulo da solicitacao julgada, copiado do pedido")
    veredito: str = Field(..., description="valido, invalido ou indeterminado")
    motivo: str = Field(
        ...,
        description=(
            "Uma frase dizendo o que esta certo ou errado. NUNCA transcreva dado "
            "pessoal do documento (CPF, RG, endereco, conta)."
        ),
    )


class VereditoDaEntrega(BaseModel):
    """Um veredito por solicitacao julgada."""

    veredictos: List[VereditoDoPedido] = Field(default_factory=list)


def _titulo(pedido: Dict[str, Any]) -> str:
    return (pedido.get("title") or pedido.get("titulo") or "").strip() or "solicitacao sem titulo"


def veredito_do_pedido(
    pedido: Dict[str, Any], veredictos: Optional[List[Dict[str, str]]]
) -> Optional[Dict[str, str]]:
    """
    O veredito que fala DESTE pedido, ou None.

    Pelo requestId quando os dois lados tem um: e a chave, e dois pedidos podem
    se chamar "Comprovante". Pelo titulo quando um dos lados nao tem id - pedido
    de antes do requestId, ou 202 de duplicata suprimida.
    """
    request_id = str(pedido.get("requestId") or "").strip()
    titulo = _titulo(pedido).casefold()

    for veredito in veredictos or []:
        id_do_veredito = str(veredito.get("requestId") or "").strip()
        if request_id and id_do_veredito:
            if id_do_veredito == request_id:
                return veredito
            continue
        if (veredito.get("solicitacao") or "").strip().casefold() == titulo:
            return veredito

    return None


def _resumo_dos_pedidos(pedidos: List[Dict[str, Any]]) -> str:
    linhas = []
    for indice, pedido in enumerate(pedidos, start=1):
        descricao = (pedido.get("description") or "").strip()
        publico = pedido.get("audience") or "?"
        linhas.append(f"[{indice}] {_titulo(pedido)} (pedido a: {publico})")
        if descricao:
            linhas.append(f"    detalhe: {descricao}")
    return "\n".join(linhas)


def _resumo_dos_documentos(documentos: List[Dict[str, str]]) -> str:
    linhas = []
    for indice, documento in enumerate(documentos, start=1):
        linhas.append(
            f"[{indice}] {documento.get('nome')} "
            f"(tipo: {documento.get('tipo')}; procedencia: {documento.get('origem')})"
        )
        texto = (documento.get("texto") or "").strip()
        linhas.append(texto if texto else "    (sem conteudo legivel)")
    return "\n".join(linhas)


def _todos_indeterminados(pedidos: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    veredictos = []
    for pedido in pedidos:
        registro = {
            "solicitacao": _titulo(pedido),
            "veredito": VEREDITO_INDETERMINADO,
            "motivo": MOTIVO_NAO_LIDO,
        }
        if pedido.get("requestId"):
            registro["requestId"] = pedido["requestId"]
        veredictos.append(registro)
    return veredictos


async def julgar_documentos(
    pedidos: Optional[List[Dict[str, Any]]],
    documentos: Optional[List[Dict[str, str]]],
    llm: Any = None,
) -> List[Dict[str, str]]:
    """
    Diz, para cada solicitacao pendente, se o que chegou atende ao que foi pedido.

    Devolve lista vazia quando nao ha o que julgar - sem pedido registrado, sem
    documento, ou sem LLM disponivel. Nunca levanta excecao: a retomada nao pode
    morrer porque o julgamento falhou, e um veredito ausente e melhor que um
    veredito inventado.
    """
    if not pedidos or not documentos:
        return []

    # Nada foi lido: nao ha julgamento a fazer, e nao se gasta uma chamada para
    # descobrir isso. Sai deterministico, com o motivo dito por extenso.
    if not any((documento.get("texto") or "").strip() for documento in documentos):
        print(f"[VEREDITO] Nenhum dos {len(documentos)} anexo(s) foi lido - {len(pedidos)} pedido(s) indeterminado(s)")
        return _todos_indeterminados(pedidos)

    if llm is None:
        print("[VEREDITO] Sem LLM disponivel - nenhum veredito emitido")
        return []

    prompt = f"""Voce confere se um documento entregue atende ao que foi pedido.

O QUE FOI PEDIDO:
{_resumo_dos_pedidos(pedidos)}

O QUE CHEGOU:
{_resumo_dos_documentos(documentos)}

Para CADA solicitacao acima, emita um veredito:

- "valido": o que chegou e o que foi pedido.
- "invalido": o que chegou claramente NAO e o que foi pedido - outro tipo de
  documento, outro assunto, periodo errado, pessoa errada, documento vencido,
  ou pagina cortada de forma que o dado pedido nao aparece.
- "indeterminado": o documento nao pode ser lido, ou o que chegou nao permite
  concluir nem uma coisa nem outra.

REGRAS:
- Documento marcado como NAO LIDO e SEMPRE "indeterminado". Nunca "invalido":
  nao conseguir ler e limitacao nossa, nao erro de quem enviou.
- Na duvida, "indeterminado". Um "invalido" errado faz uma pessoa refazer um
  documento que estava certo.
- Documento LIDO que trata de outra coisa NAO e duvida: e "invalido". Se o
  pedido descreve um pagamento (valor, data, forma), o documento tem de mostrar
  esse pagamento; se nao mostra, nao atende.
- Pedido de FOTO ou VIDEO: o que chegou tem de mostrar o que foi pedido. Se o
  conteudo lido nao tem relacao com o que a foto deveria mostrar, e "invalido".
- Um pedido pode ser atendido por mais de um arquivo, e um arquivo pode atender
  a mais de um pedido.
- O `motivo` e UMA frase, dizendo o que esta certo ou errado.
- 🔴 NUNCA escreva no motivo o dado que o documento contem (CPF, RG, endereco,
  numero de conta, valor). Diga o que o documento PROVA ou DEIXA DE PROVAR.
- Copie o titulo da solicitacao exatamente como aparece acima.
"""

    try:
        estruturado = llm.with_structured_output(VereditoDaEntrega)
        resultado = await estruturado.ainvoke(prompt)

        # O modelo devolve o TITULO; a chave e o requestId, que o backend nos deu
        # no 202 (spec 01/09, §2.4). A ligacao entre os dois e feita aqui, em
        # codigo - pedir o id ao modelo so lhe daria uma chance de inventar um.
        ids_por_titulo = {
            _titulo(pedido): pedido.get("requestId")
            for pedido in pedidos
            if pedido.get("requestId")
        }

        veredictos = []
        for item in resultado.veredictos:
            veredito = (item.veredito or "").strip().lower()
            if veredito not in (VEREDITO_VALIDO, VEREDITO_INVALIDO, VEREDITO_INDETERMINADO):
                veredito = VEREDITO_INDETERMINADO

            registro = {
                "solicitacao": item.solicitacao,
                "veredito": veredito,
                "motivo": (item.motivo or "").strip(),
            }
            request_id = ids_por_titulo.get((item.solicitacao or "").strip())
            if request_id:
                registro["requestId"] = request_id
            veredictos.append(registro)

        resumo = ", ".join(f"{v['solicitacao']}={v['veredito']}" for v in veredictos)
        print(f"[VEREDITO] {len(veredictos)} veredito(s): {resumo}")
        return veredictos

    except Exception as e:  # noqa: BLE001 - API, schema, quota
        print(f"[VEREDITO] Falha ao julgar os documentos ({type(e).__name__}: {e})")
        return []


def render_veredito_para_prompt(veredictos: Optional[List[Dict[str, str]]]) -> str:
    """
    Bloco de prompt com o veredito de cada solicitacao.

    Vazio quando nao ha veredito nenhum: um cabecalho seguido de nada faz o
    modelo inventar o que estaria ali.
    """
    if not veredictos:
        return ""

    linhas = [
        "CONFERENCIA DOS DOCUMENTOS QUE VOCE PEDIU:",
        "",
        "- Pedido marcado como `invalido` NAO foi atendido. Nao trate o que voce",
        "  pediu como recebido, e diga na sua mensagem o que falta - sem citar o",
        "  conteudo do documento.",
        "- `indeterminado` significa que nao foi possivel conferir. Nao afirme que",
        "  o documento esta errado, e nao afirme que esta certo.",
        "",
    ]

    for veredito in veredictos:
        linhas.append(
            f"- {veredito.get('solicitacao')}: {(veredito.get('veredito') or '').upper()}"
            f" - {veredito.get('motivo')}"
        )

    return "\n".join(linhas).strip()
