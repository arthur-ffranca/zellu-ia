# -*- coding: utf-8 -*-
"""Validacao de idade na ligacao (tool `validar_idade` do agente ElevenLabs).

Ate agora a triagem perguntava "voce tem 18 anos ou mais?" e acreditava na
resposta. Duas fragilidades nisso: a barreira dependia de o modelo interpretar
certo uma frase solta ("acabei de fazer 18", "faco 18 mes que vem"), e a decisao
de atender ou nao um menor de idade ficava dentro do LLM, sem registro.

Agora o agente pergunta a DATA DE NASCIMENTO e chama esta rota. A conta e feita
aqui, em codigo: mesma entrada, mesma saida, sempre. O modelo so le o `isAdult` e
segue o roteiro.

MUDANCA DE 2026-09-08
---------------------
Um cliente de 2003 foi tratado como menor de idade na ligacao. A conta estava
certa; o que chegou aqui e que estava errado. Dois buracos fechados:

1. Data na ordem americana (07/31/2003) caia como invalida. Agora e aceita como
   ultimo recurso, e so quando nao ha ambiguidade nenhuma.
2. Idade absurdamente baixa era devolvida como isAdult false, e a ligacao de um
   adulto era encerrada sem ninguem perceber. Agora volta como invalidDate, para
   o agente perguntar de novo.

Contrato: 20260823-contrato-atendimento-telefonico-elevenlabs.md (ETAPA 1).
"""

from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from config import get_settings

# Maioridade civil no Brasil.
IDADE_MINIMA = 18

# O Brasil nao tem mais horario de verao, entao UTC-3 vale o ano inteiro. Usamos
# a data de Sao Paulo, e nao a do servidor: entre 21h e 0h no Brasil o UTC ja
# virou o dia seguinte, e quem faz aniversario nesse dia seria contado com um ano
# a mais - justo na fronteira dos 18, que e a unica que importa aqui.
FUSO_BRASIL = timezone(timedelta(hours=-3))

# Antes disso nao e data de nascimento de quem esta ao telefone, e sim engano de
# transcricao (a pessoa viva mais velha do mundo nunca passou de 122 anos).
ANO_MINIMO = 1900

# E do outro lado a mesma coisa: quem liga para um escritorio de advocacia para
# relatar um problema de consumo nao tem cinco anos. Uma idade dessas nao e um
# menor de idade de verdade, e sim ano trocado - tipicamente o modelo preenchendo
# o ano corrente quando nao entendeu o que o cliente falou.
IDADE_IMPROVAVEL = 5

router = APIRouter(tags=["Pre-atendimento telefonico"])


class AgeCheckRequest(BaseModel):
    """A data que o cliente falou, como o agente entendeu."""

    birthDate: str


def verify_api_key(x_api_key: str = Header(..., alias="x-api-key")):
    """Mesma chave das outras integracoes do servico."""
    if x_api_key != get_settings().API_KEY_ZELLU_IA:
        raise HTTPException(status_code=401, detail="Nao autorizado")
    return x_api_key


def hoje_no_brasil() -> date:
    return datetime.now(FUSO_BRASIL).date()


def parse_data_de_nascimento(bruto: str) -> Optional[date]:
    """Data de nascimento a partir do que o agente mandou, ou None.

    Aceita AAAA-MM-DD (o formato que pedimos no prompt) e DD/MM/AAAA, que e como
    o brasileiro dita a data e o modelo as vezes repassa cru.

    MM/DD/AAAA fica por ultimo de proposito. Nesse ponto a data ja falhou no
    DD/MM/AAAA, o que so acontece quando o numero do meio passa de doze - e um
    numero maior que doze nao pode ser mes, so pode ser dia. Ou seja: nao ha
    palpite aqui, so a leitura que sobrou. Data ambigua como 05/11/2003 continua
    sendo lida como dia 5 de novembro, do jeito brasileiro, porque essa passa no
    DD/MM/AAAA e nunca chega no ultimo formato.

    Nao tentamos adivinhar mais que isso: data que nao casa com nenhum deles
    volta como invalida e o agente pergunta de novo, que e mais barato que errar
    a conta.
    """
    texto = (bruto or "").strip()
    if not texto:
        return None

    for formato in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(texto, formato).date()
        except ValueError:
            continue

    return _parse_flexivel(texto)


def _ano_quatro_digitos(ano: int, digitos: int) -> int:
    """Ano de dois digitos: 00..(ano corrente yy) -> 20xx, senao 19xx."""
    if digitos > 2:
        return ano
    corrente = hoje_no_brasil().year % 100
    return 2000 + ano if ano <= corrente else 1900 + ano


def _parse_flexivel(texto: str) -> Optional[date]:
    """D/M/AA, D-M-AA, DDMMAAAA e DDMMAA (sempre ordem brasileira)."""
    import re

    m = re.fullmatch(r"(\d{1,2})\s*[/.\- ]\s*(\d{1,2})\s*[/.\- ]\s*(\d{2}|\d{4})", texto)
    if m:
        d, mes, a = m.groups()
    else:
        so = re.sub(r"\D", "", texto)
        if len(so) == 8 and so == texto:
            d, mes, a = so[:2], so[2:4], so[4:]
        elif len(so) == 6 and so == texto:
            d, mes, a = so[:2], so[2:4], so[4:]
        else:
            return None
    try:
        return date(_ano_quatro_digitos(int(a), len(a)), int(mes), int(d))
    except ValueError:
        return None


def calcular_idade(nascimento: date, hoje: date) -> int:
    """Idade em anos completos."""
    anos = hoje.year - nascimento.year
    # Ainda nao fez aniversario este ano
    if (hoje.month, hoje.day) < (nascimento.month, nascimento.day):
        anos -= 1
    return anos


@router.post(
    "/phone/age-check",
    responses={
        200: {"description": "Idade avaliada (ou data recusada, em invalidDate)"},
        401: {"description": "Nao autorizado"},
    },
    summary="Valida a idade do cliente pela data de nascimento",
    description=(
        "Chamado pelo agente de voz na triagem. Devolve isAdult para o agente "
        "decidir se segue com o atendimento ou encerra a ligacao."
    ),
)
async def verificar_idade(
    pedido: AgeCheckRequest,
    api_key: str = Depends(verify_api_key),
):
    """Diz se quem esta na linha tem 18 anos ou mais.

    Data irreconhecivel NAO e erro HTTP: volta 200 com invalidDate=true. Um 4xx
    chegaria ao agente como falha de ferramenta, e o roteiro trata falha caindo
    para a pergunta direta - o oposto do que queremos quando o problema e so uma
    data mal entendida, que se resolve perguntando de novo.

    Resposta com invalidDate NAO tem isAdult, e isso e proposital: ausencia de
    isAdult nunca deve ser lida como menor de idade. O prompt do agente diz isso
    com essas palavras.
    """

    nascimento = parse_data_de_nascimento(pedido.birthDate)
    hoje = hoje_no_brasil()

    if nascimento is None:
        return {
            "invalidDate": True,
            "error": "Data de nascimento nao reconhecida. Use o formato AAAA-MM-DD.",
        }

    if nascimento > hoje:
        return {"invalidDate": True, "error": "Data de nascimento no futuro."}

    if nascimento.year < ANO_MINIMO:
        return {"invalidDate": True, "error": "Data de nascimento improvavel."}

    idade = calcular_idade(nascimento, hoje)

    if idade < IDADE_IMPROVAVEL:
        # Nao devolvemos isAdult false aqui. Encerraria a ligacao de um adulto
        # por causa de um ano mal entendido, e o erro seria indistinguivel de um
        # menor de idade de verdade - ninguem olharia duas vezes.
        print(
            f"[IDADE] {pedido.birthDate!r} -> {idade} anos: improvavel, "
            "pedindo a data de novo"
        )
        return {
            "invalidDate": True,
            "error": (
                "Data de nascimento improvavel. Confirme o ano de nascimento "
                "com o cliente e chame a ferramenta de novo."
            ),
        }

    maior = idade >= IDADE_MINIMA

    print(f"[IDADE] {pedido.birthDate!r} -> {idade} anos, maior de idade: {maior}")

    return {
        "isAdult": maior,
        "age": idade,
        "birthDate": nascimento.isoformat(),
    }
