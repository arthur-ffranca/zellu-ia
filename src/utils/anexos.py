# -*- coding: utf-8 -*-
"""
Ingestao dos anexos que chegam numa solicitacao atendida.

O PROBLEMA QUE ISTO RESOLVE
---------------------------
O ZelinhU pede um comprovante, uma pessoa anexa o arquivo, a negociacao retoma - e
ate agora o que chegava ate o modelo era a frase 'Solicitacao "X" atendida.
2 arquivo(s) anexado(s).' O agente nao via nem o nome do arquivo, quanto mais o
conteudo. Ele pedia um documento e seguia negociando como se nada tivesse chegado.

O modelo por tras do ZelinhU le texto sem dificuldade nenhuma. O que faltava nao
era capacidade de leitura: era alguem colocar o documento na frente dele.

O QUE ENTRA
-----------
Dois formatos, na mesma lista:
- URL em string - e o que o backend manda de verdade, no campo `files`
  (contrato 09/09). Presigned, valida por 1h a partir do despacho.
- Objeto com nome/tipo/url ou com o texto ja extraido - formato da spec de 01/09.
  So ele consegue trazer texto pronto, por isso continua aceito.

O QUE ESTE MODULO FAZ, E O QUE NAO FAZ
--------------------------------------
Faz: transforma cada anexo em TEXTO com procedencia declarada, para entrar no
contexto de raciocinio do ZelinhU.

Faz tambem, desde 10/09: le IMAGEM por visao computacional (GPT-4o Vision, pelo
DocumentAnalyzer que ja servia o lado da empresa). Comprovante fotografado e RG
sao o formato mais comum do mundo real - enquanto nao liamos imagem, pedir
documento era teatro.

Faz tambem, desde 18/09: le o PDF DIGITALIZADO - aquele que e so foto por
dentro. Nao por OCR e nem convertendo pagina em imagem (isso exigiria
poppler/pdf2image, que o projeto nao tem), mas tirando de dentro do PDF as
imagens que ja estao la, com o PyPDF2 que o projeto ja usa, e mandando-as para a
mesma visao. O QA de 14/09 mostrou o custo de nao fazer isso: um PDF sem texto
voltava "NAO LIDO", o veredito era obrigado a sair "indeterminado" e o arquivo
errado seguia como se tivesse atendido ao pedido.

Nao faz: OCR de PDF que nao tem nem texto nem imagem embutida aproveitavel. Ele
diz isso em voz alta, no lugar de sumir em silencio.

🔴 A REGRA QUE NAO PODE SER QUEBRADA
------------------------------------
O conteudo de um anexo do CLIENTE (RG, comprovante de residencia, holerite) NUNCA
entra na mensagem que o ZelinhU manda para a EMPRESA. A empresa e a outra parte da
negociacao. Por isso o texto extraido vai para o estado do agente - o que ele usa
para PENSAR -, nunca para o corpo da retomada, e o prompt manda explicitamente nao
transcrever dado pessoal na mensagem.
"""

from typing import Any, Dict, List, Optional, Tuple
import asyncio
import io
from urllib.parse import unquote

import httpx

from config import get_settings

# Teto por anexo e no total. Um PDF de 40 paginas nao pode empurrar o resto do
# prompt para fora da janela - o caso, o livro-razao e o mandato importam mais que
# a pagina 37 de um contrato.
CAP_POR_ANEXO = 4000
CAP_TOTAL = 12000

# Quantos anexos olhamos numa retomada. O teto do backend e de 10 solicitacoes
# pendentes, e cada uma pode trazer mais de um arquivo.
MAX_ANEXOS = 12

# Teto da imagem que mandamos para a visao. Acima disso a chamada fica cara e
# lenta sem ganho: um comprovante de celular tem 1-3 MB.
CAP_BYTES_IMAGEM = 8_000_000

# Timeout do download. A URL e presigned e expira em 1h (preferencia da §5.4);
# se ela nao responder rapido, seguimos sem o conteudo em vez de segurar a
# retomada inteira.
TIMEOUT_DOWNLOAD = 10.0

# Extensoes que viram texto pelo extrator que ja existe no projeto, mapeadas para
# o vocabulario que ele entende. CSV vira TXT de proposito: o extrator nao conhece
# "CSV" e devolveria vazio, mas um CSV e texto puro e le perfeitamente por ali.
TIPOS_LEGIVEIS = {
    "pdf": "PDF",
    "docx": "DOCX",
    "doc": "DOCX",
    "txt": "TXT",
    "md": "TXT",
    "csv": "TXT",
}

# Extensoes que sao imagem: reconhecidas para poder dizer com precisao POR QUE
# nao foram lidas, em vez de cair no balde de "tipo desconhecido".
TIPOS_IMAGEM = {"jpg", "jpeg", "png", "webp", "gif", "bmp", "heic"}

# Os que a Vision da OpenAI aceita de verdade (DocumentAnalyzer._is_supported_image).
# BMP e HEIC sao imagem, e sao reconhecidos como tal para poder dizer com precisao
# por que nao foram lidos - em vez de irem para a API e voltarem com uma
# "observacao" que pareceria leitura.
TIPOS_IMAGEM_VISAO = {"jpg", "jpeg", "png", "webp", "gif"}

# Procedencia do texto. O agente precisa saber a diferenca entre "li o documento"
# e "me disseram o que tem nele".
ORIGEM_BACKEND = "texto enviado pelo backend"
ORIGEM_EXTRAIDO = "extraido do arquivo"
ORIGEM_VISAO = "lido por visao computacional (imagem)"
ORIGEM_VISAO_PDF = "lido por visao computacional (PDF digitalizado)"
NAO_LIDO_IMAGEM = "NAO LIDO - imagem, e ainda nao fazemos OCR"
NAO_LIDO_TIPO = "NAO LIDO - tipo de arquivo sem extrator"
NAO_LIDO_SEM_FONTE = "NAO LIDO - nem texto nem link no payload"
NAO_LIDO_FALHA = "NAO LIDO - falha ao baixar ou extrair"
# Antes este caso reusava NAO_LIDO_TIPO e dizia ao agente que o arquivo era de um
# "tipo sem extrator" - mentira sobre um PDF que apenas nao coube. O agente
# repetia a mentira para a empresa.
NAO_LIDO_SEM_ESPACO = "NAO LIDO - nao coube no espaco reservado a documentos"
NAO_LIDO_VISAO_DESLIGADA = "NAO LIDO - imagem, e a leitura por visao esta desligada"
NAO_LIDO_IMAGEM_GRANDE = "NAO LIDO - imagem grande demais para a leitura por visao"
NAO_LIDO_IMAGEM_ILEGIVEL = "NAO LIDO - imagem recebida, mas a visao nao conseguiu interpreta-la"
# PDF que e so foto por dentro: o extrator devolve vazio e ate agora isso virava
# "falha ao baixar ou extrair", como se fosse defeito de rede. Nao e - o arquivo
# chegou inteiro, so nao tem texto dentro. Desde 18/09 este motivo sobra so para
# o PDF de onde nao saiu nem texto nem imagem legivel.
NAO_LIDO_PDF_DIGITALIZADO = "NAO LIDO - PDF sem texto e sem imagem que a visao consiga ler"

# Quantas imagens tiramos de dentro de um PDF digitalizado. Um comprovante ou um
# laudo cabe nas primeiras; ler as 40 paginas de um contrato fotografado custaria
# uma chamada de visao por pagina sem mudar o veredito.
MAX_IMAGENS_DO_PDF = 3

# Imagem embutida menor que isto e logotipo, assinatura digitalizada ou icone de
# rodape - nao e a pagina. Mandar cada uma para a visao gastaria a cota lendo
# carimbo.
MIN_BYTES_IMAGEM_DO_PDF = 20_000

# Assinatura dos formatos, para o arquivo cujo nome nao diz o que ele e. Uma URL
# presigned pode apontar para uma chave UUID sem extensao nenhuma - e a lista
# crua de `files` nao traz `fileName` para consultar.
ASSINATURAS = (
    (b"%PDF", "pdf"),
    (b"PK\x03\x04", "docx"),  # DOCX e um ZIP por dentro
    (b"\xff\xd8\xff", "jpg"),
    (b"\x89PNG", "png"),
    (b"GIF8", "gif"),
    (b"RIFF", "webp"),
)


def _primeiro_preenchido(dados: Dict[str, Any], *chaves: str) -> str:
    for chave in chaves:
        valor = dados.get(chave)
        if isinstance(valor, str) and valor.strip():
            return valor.strip()
    return ""


def _nome_do_anexo(anexo: Dict[str, Any]) -> str:
    declarado = _primeiro_preenchido(
        anexo, "fileName", "filename", "name", "title", "originalName"
    )
    if declarado:
        return declarado

    # Lista crua de URLs (contrato 09/09): nao ha campo de nome nenhum. O que
    # sobra e o fim do caminho, antes da query string da assinatura. Sem isto
    # todo anexo vira "arquivo sem nome" e o agente nao consegue nem dizer a qual
    # documento esta se referindo quando chega mais de um.
    url = _url_do_anexo(anexo)
    if url:
        ultimo = url.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
        if ultimo and "." in ultimo:
            return unquote(ultimo)

    return "arquivo sem nome"


def _tipo_do_anexo(anexo: Dict[str, Any]) -> str:
    """Extensao normalizada, do campo declarado ou do fim do nome/URL."""
    declarado = _primeiro_preenchido(anexo, "fileType", "type", "extension")
    if declarado:
        limpo = declarado.lower().strip().lstrip(".")
        # "application/pdf" e "image/jpeg" viram "pdf" e "jpeg"
        if "/" in limpo:
            limpo = limpo.split("/")[-1]
        if limpo:
            return limpo

    for campo in ("fileName", "filename", "name", "url", "downloadUrl", "fileUrl"):
        valor = anexo.get(campo)
        if isinstance(valor, str) and "." in valor:
            # A query string de uma URL presigned nao faz parte da extensao.
            candidato = valor.split("?")[0].rsplit(".", 1)[-1].lower()
            if candidato and len(candidato) <= 5:
                return candidato

    return ""


def _url_do_anexo(anexo: Dict[str, Any]) -> str:
    return _primeiro_preenchido(anexo, "url", "downloadUrl", "fileUrl", "signedUrl", "href")


def _texto_embutido(anexo: Dict[str, Any]) -> str:
    return _primeiro_preenchido(anexo, "extractedText", "text", "content", "ocrText")


def _normalizar(anexo: Any) -> Optional[Dict[str, Any]]:
    """
    Um item da lista vira sempre dict - ou None quando nao da para usar.

    O backend manda `files` como lista CRUA DE URLs (contrato 09/09); o formato
    em dict continua aceito porque so ele traz texto ja extraido. String que nao
    e URL nao e anexo: e texto solto que entrou na lista por engano, e inventar
    um arquivo a partir dela seria pior do que ignorar.
    """
    if isinstance(anexo, dict):
        return anexo
    if isinstance(anexo, str):
        limpo = anexo.strip()
        if limpo.lower().startswith(("http://", "https://")):
            return {"url": limpo}
    return None


def nomes_dos_anexos(anexos: Optional[List[Any]]) -> List[str]:
    """Nome de cada anexo utilizavel, sem baixar nada e sem expor a URL."""
    nomes = []
    for bruto in anexos or []:
        anexo = _normalizar(bruto)
        if anexo is not None:
            nomes.append(_nome_do_anexo(anexo))
    return nomes


async def _baixar(url: str) -> Tuple[bytes, str]:
    """Baixa o arquivo e devolve (bytes, content-type declarado pelo storage)."""
    async with httpx.AsyncClient(timeout=TIMEOUT_DOWNLOAD, follow_redirects=True) as cliente:
        resposta = await cliente.get(url)
        resposta.raise_for_status()
        return resposta.content, (resposta.headers.get("content-type") or "")


def _tipo_por_conteudo(content_type: str, inicio: bytes) -> str:
    """
    Tipo do arquivo quando a URL nao diz qual e.

    Primeiro o Content-Type do storage, que e a declaracao de quem guardou o
    arquivo; depois a assinatura dos primeiros bytes, que e o proprio arquivo
    dizendo o que e. A assinatura e mais confiavel, mas nao cobre os formatos que
    nao tem uma (TXT, CSV) - por isso as duas.
    """
    declarado = (content_type or "").split(";")[0].strip().lower()
    if "/" in declarado:
        sufixo = declarado.split("/")[-1]
        if "wordprocessingml" in sufixo or sufixo == "msword":
            return "docx"
        if sufixo in ("plain", "markdown", "x-markdown"):
            return "txt"
        if sufixo in TIPOS_LEGIVEIS or sufixo in TIPOS_IMAGEM:
            return sufixo

    for assinatura, tipo in ASSINATURAS:
        if inicio.startswith(assinatura):
            return tipo

    return ""


async def _ler_imagem(conteudo: bytes, content_type: str, nome: str) -> str:
    """
    Le uma imagem de documento e devolve em texto o que ela mostra.

    Reusa o DocumentAnalyzer (GPT-4o Vision), que ja fazia exatamente isto no
    lado da empresa desde antes - a capacidade existia no projeto, faltava
    liga-la neste caminho.

    A chamada e sincrona e vai para uma thread: segurar o event loop aqui
    travaria a retomada inteira, que e uma requisicao HTTP do backend esperando.
    """
    import asyncio
    import base64

    from src.services.document_analyzer import document_analyzer

    analise = await asyncio.to_thread(
        document_analyzer.analyze_document,
        base64.b64encode(conteudo).decode("ascii"),
        content_type,
        nome,
        "Documento anexado por uma pessoa para atender a uma solicitacao feita "
        "durante uma negociacao. Descreva o que o documento mostra e extraia "
        "datas, valores, nomes e numeros visiveis.",
    )

    # analyze_document NAO levanta excecao: falha de API, quota estourada e
    # formato nao suportado voltam como um dict de confianca 0 com o erro dentro
    # de `observations`. Sem esta checagem, "Erro na analise: 401" entraria no
    # prompt marcado como "lido por visao" e o agente raciocinaria sobre isso.
    informacoes = analise.get("extracted_info") or {}
    try:
        confianca = float(analise.get("confidence") or 0)
    except (TypeError, ValueError):
        confianca = 0.0

    if confianca <= 0 and not informacoes:
        print(f"[ANEXOS] A visao nao leu '{nome}': {(analise.get('observations') or '')[:140]}")
        return ""

    partes = [
        f"Tipo aparente: {document_analyzer.get_document_description(analise.get('document_type', 'outro'))}"
    ]

    for chave, valor in informacoes.items():
        if valor:
            partes.append(f"{chave}: {valor}")

    observacoes = (analise.get("observations") or "").strip()
    if observacoes:
        partes.append(f"Observacoes da leitura: {observacoes}")

    # A confianca entra no texto de proposito: uma foto tremida lida com 0.3 nao
    # pode sustentar afirmacao categorica na negociacao.
    partes.append(f"Confianca da leitura: {analise.get('confidence', 0)}")

    return "\n".join(partes)


def _imagens_do_pdf(conteudo: bytes) -> List[Tuple[bytes, str]]:
    """As imagens que estao dentro do PDF, em ordem de pagina.

    O PDF digitalizado guarda cada pagina como UMA imagem embutida - e ela ja
    esta la, em JPEG ou PNG, sem precisar rasterizar nada. O PyPDF2 que o projeto
    ja usa para extrair texto sabe entrega-la.

    Nunca levanta excecao: PDF cifrado, objeto de imagem que o PyPDF2 nao decodifica
    e lib ausente devolvem lista vazia, e quem chama diz que nao conseguiu ler.

    Returns:
        [(bytes, mime)], no maximo MAX_IMAGENS_DO_PDF.
    """
    try:
        import io

        import PyPDF2
    except ImportError:
        print("[ANEXOS] PyPDF2 ausente - PDF digitalizado segue sem leitura")
        return []

    imagens: List[Tuple[bytes, str]] = []

    try:
        leitor = PyPDF2.PdfReader(io.BytesIO(conteudo))

        for pagina in leitor.pages:
            for embutida in getattr(pagina, "images", []) or []:
                dados = getattr(embutida, "data", b"") or b""
                if len(dados) < MIN_BYTES_IMAGEM_DO_PDF or len(dados) > CAP_BYTES_IMAGEM:
                    continue

                # O nome interno traz a extensao que o PyPDF2 deduziu do filtro
                # do objeto (DCTDecode vira .jpg, FlateDecode vira .png).
                tipo = (getattr(embutida, "name", "") or "").rsplit(".", 1)[-1].lower()
                if tipo not in TIPOS_IMAGEM_VISAO:
                    tipo = _tipo_por_conteudo("", dados[:16]) or "png"
                if tipo not in TIPOS_IMAGEM_VISAO:
                    continue

                imagens.append((dados, f"image/{'jpeg' if tipo in ('jpg', 'jpeg') else tipo}"))
                if len(imagens) >= MAX_IMAGENS_DO_PDF:
                    return imagens
    except Exception as e:  # noqa: BLE001 - PDF cifrado, objeto invalido, Pillow ausente
        print(f"[ANEXOS] Nao foi possivel tirar imagens do PDF ({type(e).__name__}: {e})")

    return imagens


# Teto de pixels por pagina rasterizada: uma pagina declarada com 14400 pt de
# lado viraria uma imagem de centenas de MB a 1.5x.
MAX_PIXELS_PAGINA = 3000 * 3000


def _paginas_renderizadas_pdf(conteudo: bytes) -> List[Tuple[bytes, str]]:
    """Rasteriza ate N paginas quando o PDF tem camada textual insuficiente.

    Usa pypdfium2 (PDFium; BSD-3/Apache-2.0) com wheels autocontidos, sem poppler.
    PyMuPDF faria o mesmo, mas e AGPL: em SaaS isso pode obrigar a abrir o codigo
    do servico. O caminho so e acionado quando a leitura textual nao representa
    bem o documento, portanto CNH/NFS-e image-based deixam de escapar apenas
    porque havia um cabecalho de poucas linhas na camada de texto.
    """
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return []

    limite = max(int(getattr(get_settings(), "DOCUMENT_VISION_MAX_PAGES", MAX_IMAGENS_DO_PDF)), 1)
    imagens: List[Tuple[bytes, str]] = []
    documento = None
    try:
        documento = pdfium.PdfDocument(conteudo)
        for indice in range(min(len(documento), limite)):
            pagina = documento[indice]
            try:
                largura, altura = pagina.get_size()
                # 1.5x e suficiente para leitura de formulario sem explodir custo/memoria.
                escala = 1.5
                pixels = largura * altura * escala * escala
                if pixels > MAX_PIXELS_PAGINA:
                    escala *= (MAX_PIXELS_PAGINA / pixels) ** 0.5
                bitmap = pagina.render(scale=escala)
                imagem = bitmap.to_pil().convert("RGB")
                buffer = io.BytesIO()
                imagem.save(buffer, format="PNG", optimize=True)
                dados = buffer.getvalue()
            finally:
                pagina.close()
            if len(dados) <= CAP_BYTES_IMAGEM:
                imagens.append((dados, "image/png"))
    except Exception as e:  # noqa: BLE001
        print(f"[ANEXOS] Nao foi possivel rasterizar PDF ({type(e).__name__}: {e})")
    finally:
        if documento is not None:
            documento.close()
    return imagens


async def _ler_pdf_digitalizado(conteudo: bytes, nome: str) -> str:
    """Le por visao o PDF de onde o extrator nao tirou texto nenhum.

    O FURO QUE ISTO FECHA (QA de 14/09/2026)
    ----------------------------------------
    Dois PDFs deliberadamente errados foram enviados no teste. Um PDF sem texto
    voltava daqui como NAO LIDO, e a regra do julgamento - "documento nao lido e
    SEMPRE indeterminado", que existe para nao punir a pessoa por um limite
    nosso - transformava o arquivo errado em "recebido, nao conferido". A
    solicitacao seguia como atendida e a negociacao andava.

    Lendo a imagem que esta dentro do PDF, o mesmo arquivo passa a ser conferido
    como qualquer foto: se nao e o que foi pedido, o veredito sai "invalido" e o
    pedido volta para quem deveria atende-lo.
    """
    if not get_settings().NEGOTIATION_VISION_ENABLED:
        return ""

    # Pagina inteira conserva posicao, texto e contexto. Imagens isoladas podem
    # ser somente logos ou fragmentos, omitindo o restante do documento.
    imagens = await asyncio.to_thread(_paginas_renderizadas_pdf, conteudo)
    origem = "pagina(s) rasterizada(s)"
    if not imagens:
        imagens = await asyncio.to_thread(_imagens_do_pdf, conteudo)
        origem = "imagem(ns) embutida(s)"
    if not imagens:
        return ""

    print(f"[ANEXOS] '{nome}' precisa de leitura visual - lendo {len(imagens)} {origem}")

    partes = []
    for indice, (dados, mime) in enumerate(imagens, start=1):
        try:
            texto = await _ler_imagem(dados, mime, f"{nome} (pagina {indice})")
        except Exception as e:  # noqa: BLE001 - API de visao, quota
            print(f"[ANEXOS] Falha ao ler a pagina {indice} de '{nome}' ({type(e).__name__}: {e})")
            continue
        if texto.strip():
            partes.append(f"[pagina {indice}]\n{texto.strip()}")

    return "\n\n".join(partes)


def _cortar(texto: str, teto: int) -> str:
    texto = (texto or "").strip()
    if len(texto) <= teto:
        return texto
    return texto[:teto].rstrip() + "\n[... documento truncado ...]"


async def ingerir_anexos(
    anexos: Optional[List[Any]],
    teto_total: int = CAP_TOTAL,
    max_anexos: int = MAX_ANEXOS,
) -> List[Dict[str, str]]:
    """
    Transforma os anexos do payload em texto, um registro por arquivo.

    Cada registro tem `nome`, `tipo`, `origem` e `texto`. `origem` e o que
    permite ao agente distinguir o que ele leu do que ele nao leu - um anexo que
    falhou continua aparecendo na lista, com o motivo, porque desaparecer em
    silencio faria o agente concluir que o cliente nao mandou nada.

    Nunca levanta excecao: a retomada nao pode morrer por causa de um anexo.

    ORCAMENTO COMPARTILHADO (contrato 20260911, Etapa 1)
    ----------------------------------------------------
    Desde que a rajada passou a ser ingerida PEDIDO A PEDIDO - para cada arquivo
    ser julgado contra o pedido dele -, esta funcao e chamada varias vezes na
    mesma retomada. Com o teto fixo por chamada, seis pedidos renderiam seis
    vezes o orcamento de texto e empurrariam o caso, o livro-razao e o mandato
    para fora da janela do modelo. Quem chama em serie passa o que sobrou.

    Os defaults sao os tetos de sempre: uma chamada unica se comporta como antes.
    """
    if not anexos:
        return []

    registros: List[Dict[str, str]] = []
    orcamento = max(int(teto_total), 0)
    extrator = None

    for bruto in anexos[:max(int(max_anexos), 0)]:
        anexo = _normalizar(bruto)
        if anexo is None:
            continue

        nome = _nome_do_anexo(anexo)
        tipo = _tipo_do_anexo(anexo)
        registro = {"nome": nome, "tipo": tipo or "desconhecido", "texto": ""}

        embutido = _texto_embutido(anexo)
        if embutido:
            registro["origem"] = ORIGEM_BACKEND
            registro["texto"] = _cortar(embutido, min(CAP_POR_ANEXO, max(orcamento, 0)))
            orcamento -= len(registro["texto"])
            registros.append(registro)
            continue

        url = _url_do_anexo(anexo)
        if not url:
            registro["origem"] = NAO_LIDO_SEM_FONTE
            registros.append(registro)
            continue

        # Antes do download: o que ja sabemos ser ilegivel nao vale uma conexao,
        # e o que nao cabe no orcamento nao vale ser baixado para ser descartado.
        # Imagem NAO entra aqui: ela tem caminho proprio, o da visao.
        if tipo and tipo not in TIPOS_LEGIVEIS and tipo not in TIPOS_IMAGEM:
            registro["origem"] = NAO_LIDO_TIPO
            registros.append(registro)
            continue

        if orcamento <= 0:
            registro["origem"] = NAO_LIDO_SEM_ESPACO
            registros.append(registro)
            continue

        # ARQUIVO SEM EXTENSAO NO NOME
        # ----------------------------
        # Uma URL presigned pode terminar numa chave UUID, e a lista crua de
        # `files` nao traz campo de tipo nenhum. Sem olhar dentro, todo anexo
        # assim viraria "tipo sem extrator": o pedido foi atendido, o arquivo
        # chegou, e o ZelinhU nunca saberia. Baixamos UMA vez e reaproveitamos os
        # mesmos bytes na extracao.
        conteudo: Optional[bytes] = None
        if not tipo:
            try:
                conteudo, content_type = await _baixar(url)
                tipo = _tipo_por_conteudo(content_type, conteudo[:16])
                registro["tipo"] = tipo or "desconhecido"
            except Exception as e:  # noqa: BLE001 - rede, 403 de URL expirada
                print(f"[ANEXOS] Falha ao baixar '{nome}' ({type(e).__name__}: {e})")
                registro["origem"] = NAO_LIDO_FALHA
                registros.append(registro)
                continue

            if tipo not in TIPOS_LEGIVEIS and tipo not in TIPOS_IMAGEM:
                registro["origem"] = NAO_LIDO_TIPO
                registros.append(registro)
                continue

        # IMAGEM: o caminho da visao (fase 2, 10/09/2026)
        # ----------------------------------------------
        # Comprovante fotografado, RG, print de conversa. Ate 10/09 tudo isto
        # parava aqui como "nao lido" - o ZelinhU pedia o documento, ele chegava,
        # e ninguem abria.
        if tipo in TIPOS_IMAGEM:
            if tipo not in TIPOS_IMAGEM_VISAO:
                registro["origem"] = NAO_LIDO_IMAGEM
                registros.append(registro)
                continue

            if not get_settings().NEGOTIATION_VISION_ENABLED:
                registro["origem"] = NAO_LIDO_VISAO_DESLIGADA
                registros.append(registro)
                continue

            try:
                if conteudo is None:
                    conteudo, content_type = await _baixar(url)
                if len(conteudo) > CAP_BYTES_IMAGEM:
                    registro["origem"] = NAO_LIDO_IMAGEM_GRANDE
                    registros.append(registro)
                    continue

                mime = f"image/{'jpeg' if tipo in ('jpg', 'jpeg') else tipo}"
                texto = await _ler_imagem(conteudo, mime, nome)
                if texto.strip():
                    registro["origem"] = ORIGEM_VISAO
                    registro["texto"] = _cortar(texto, min(CAP_POR_ANEXO, orcamento))
                    orcamento -= len(registro["texto"])
                else:
                    registro["origem"] = NAO_LIDO_IMAGEM_ILEGIVEL
            except Exception as e:  # noqa: BLE001 - rede, API de visao, quota
                print(f"[ANEXOS] Falha ao ler a imagem '{nome}' ({type(e).__name__}: {e})")
                registro["origem"] = NAO_LIDO_FALHA

            registros.append(registro)
            continue

        try:
            if extrator is None:
                from src.services.document_extractor import DocumentExtractor

                extrator = DocumentExtractor(timeout=TIMEOUT_DOWNLOAD)

            if conteudo is not None:
                texto, _meta = extrator.extract(conteudo, TIPOS_LEGIVEIS[tipo])
            else:
                texto, _meta = await extrator.extract_from_url(url, TIPOS_LEGIVEIS[tipo])
            if (texto or "").strip():
                registro["origem"] = ORIGEM_EXTRAIDO
                registro["texto"] = _cortar(texto, min(CAP_POR_ANEXO, orcamento))
                orcamento -= len(registro["texto"])
            elif tipo == "pdf":
                # O arquivo chegou inteiro; e que nao ha TEXTO la dentro. Mas
                # pode haver a imagem da pagina, e a visao le imagem (QA 14/09).
                # Os bytes so sao baixados agora: no caminho comum quem le e o
                # extrator, direto da URL.
                if conteudo is None:
                    try:
                        conteudo, _ = await _baixar(url)
                    except Exception as e:  # noqa: BLE001 - rede, URL expirada
                        print(f"[ANEXOS] Falha ao baixar '{nome}' para a visao ({type(e).__name__}: {e})")

                da_visao = await _ler_pdf_digitalizado(conteudo, nome) if conteudo else ""
                if da_visao.strip():
                    registro["origem"] = ORIGEM_VISAO_PDF
                    registro["texto"] = _cortar(da_visao, min(CAP_POR_ANEXO, orcamento))
                    orcamento -= len(registro["texto"])
                else:
                    registro["origem"] = NAO_LIDO_PDF_DIGITALIZADO
            elif tipo in ("doc", "docx"):
                registro["origem"] = NAO_LIDO_PDF_DIGITALIZADO
            else:
                registro["origem"] = NAO_LIDO_FALHA
        except Exception as e:  # noqa: BLE001 - download, parse, lib ausente
            print(f"[ANEXOS] Falha ao ler '{nome}' ({type(e).__name__}: {e})")
            registro["origem"] = NAO_LIDO_FALHA

        registros.append(registro)

    if extrator is not None:
        try:
            await extrator.close()
        except Exception:  # noqa: BLE001
            pass

    lidos = sum(1 for r in registros if r.get("texto"))
    print(f"[ANEXOS] {len(registros)} anexo(s) na retomada, {lidos} com texto aproveitado")
    return registros


def render_anexos_para_prompt(registros: Optional[List[Dict[str, str]]]) -> str:
    """
    Bloco de prompt com os documentos recebidos.

    Vazio quando nao ha anexo nenhum - que e o caso de toda negociacao ate o
    backend passar a mandar arquivos. Um cabecalho "DOCUMENTOS:" seguido de nada
    faz o modelo inventar o que estaria ali.
    """
    if not registros:
        return ""

    linhas = [
        "DOCUMENTOS RECEBIDOS DO CLIENTE OU DA EMPRESA:",
        "",
        "Use o conteudo abaixo para sustentar sua posicao. Regras:",
        "- NAO transcreva dado pessoal (CPF, RG, endereco, conta bancaria) na",
        "  mensagem que voce escreve: ela vai para a EMPRESA, que e a outra parte.",
        "  Cite o que o documento PROVA, nunca os dados que ele contem.",
        "- Documento marcado como NAO LIDO nao foi lido. Nao afirme nada sobre o",
        "  conteudo dele, e nao o trate como entregue para efeito de prova.",
        "",
    ]

    for indice, registro in enumerate(registros, start=1):
        remetente = registro.get("remetente")
        linhas.append(
            f"[{indice}] {registro.get('nome')} "
            f"({'enviado por: ' + remetente + '; ' if remetente else ''}"
            f"tipo: {registro.get('tipo')}; procedencia: {registro.get('origem')})"
        )
        texto = (registro.get("texto") or "").strip()
        if texto:
            linhas.append(texto)
        linhas.append("")

    return "\n".join(linhas).strip()
