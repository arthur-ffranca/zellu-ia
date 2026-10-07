# -*- coding: utf-8 -*-
"""
Guardrails de negociacao da IA da Empresa.

MOTIVACAO
---------
Os relatorios de avaliacao das negociacoes IA x IA (docs/Relatorio_Avaliacao_
Negociacao_*.docx, 30/07/2026) deram nota media 2,2/10 para a IA da empresa nos
tres casos testados. Os padroes de falha foram identicos nos tres:

  1. Aceitacao espelhada  - responde "concordamos" sem analise propria
  2. Sem alcada           - aceitou exposicao de R$ 36.000 num caso de R$ 18.500
  3. Sem aritmetica       - aceitou multa de R$ 5.000/dia com teto de R$ 3.700
  4. Alternativas         - aceitou "100% + SELIC" E "95% a vista" ao mesmo tempo
  5. Fora do objeto       - aceitou SLA/escopo/marcos num caso de cobranca indevida
  6. Identidade           - assinou como "[Zellu - Assistencia ao Cliente]" (a parte
                            contraria) e inventou o nome do cliente ("Joao Paulo")
  7. Placeholders         - enviou "[Seu Nome]", "[Seu Cargo]", "contrato no [X]"
  8. Promessa vazia       - protocolo/responsavel/minuta sempre "na proxima rodada"

CAUSA-RAIZ NO CODIGO
--------------------
As regras de negociacao viviam apenas dentro do system prompt configuravel
(src/prompt_generator.py::_build_negotiation_strategy_section). Em
src/main.py, quando existe prompt salvo no Supabase, ele SUBSTITUIA o prompt
montado - ou seja, os guardrails podiam simplesmente nao chegar ao modelo.

ESTE MODULO
-----------
Produz blocos de system prompt que sao SEMPRE concatenados DEPOIS do prompt
configuravel da empresa. A empresa pode personalizar persona, tom e politicas,
mas nao consegue remover estes limites por edicao de prompt.

Uso:
    from src.negotiation_guardrails import build_company_negotiation_block

    system_prompt = system_prompt + "\\n\\n" + build_company_negotiation_block(
        context=context,
        message_history=message_history,
        negotiation=request.negotiation,
        company_name=company_name,
        client_name=client_name,
    )
"""

from typing import Any, Dict, List, Optional

from src.prompt_generator import build_legal_context_for_negotiation


# =============================================================================
# HELPERS DE LEITURA DEFENSIVA
# =============================================================================

def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Le um campo tanto de dict quanto de objeto Pydantic."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        value = obj.get(key, default)
    else:
        value = getattr(obj, key, default)
    return default if value is None else value


def _first_filled(*values: Any) -> str:
    """Retorna o primeiro valor nao vazio como string."""
    for value in values:
        if value not in (None, "", []):
            return str(value).strip()
    return ""


# =============================================================================
# RODADA DE NEGOCIACAO
# =============================================================================

def count_negotiation_round(
    message_history: Optional[List[Any]] = None,
    negotiation: Any = None,
) -> int:
    """
    Determina a rodada atual da negociacao.

    Prioridade:
      1. negotiation.currentRound (informado pelo backend Zellu)
      2. Contagem de mensagens ja enviadas pela IA da empresa + 1

    Returns:
        Numero da rodada (>= 1)
    """
    current_round = _get(negotiation, "currentRound", None)
    if isinstance(current_round, int) and current_round >= 1:
        return current_round

    company_messages = 0
    for msg in (message_history or []):
        if _get(msg, "origin", "") == "company_ai":
            company_messages += 1

    return company_messages + 1


def _build_round_posture(current_round: int, max_rounds: Optional[int]) -> str:
    """Postura esperada da empresa nesta rodada especifica."""
    if current_round <= 2:
        posture = (
            "APURACAO. Peca os documentos que faltam (contrato, faturas, comprovante "
            "de pagamento, evidencias do dano). Nao reconheca responsabilidade e nao "
            "feche valor definitivo nesta fase. Ofereca apenas o que a politica da "
            "empresa permite sem apuracao."
        )
    elif current_round <= 4:
        posture = (
            "CONTRAPROPOSTA. Ja houve troca suficiente para voce se posicionar. "
            "Apresente sua propria proposta com valores, prazos e justificativa - "
            "nao apenas reaja a proposta da outra parte. Melhore a oferta anterior "
            "somente se houver fato novo ou documento novo que justifique."
        )
    else:
        # A versao anterior dizia "consolide o que ja foi acordado ... nao
        # adicione concessoes novas sem contrapartida" e produziu o oposto: no
        # log de 25/08/2026 a empresa cedeu nos 4 itens que restavam, sem
        # nenhuma contrapartida, e no resumo final deixou cair silenciosamente as
        # condicoes de nexo e imputabilidade do reembolso - que ela tinha
        # sustentado por tres rodadas. "Consolidar" virou "fechar a qualquer
        # custo". Esta versao diz o que consolidar NAO e.
        posture = (
            "OFERTA FINAL. Consolidar e repetir por escrito o que JA foi acordado, "
            "com as mesmas condicoes de quando cada item foi aceito - nao e fechar "
            "o que continua em aberto. Retirar uma condicao que voce sustentou nas "
            "rodadas anteriores e concessao nova, nao consolidacao. Item ainda em "
            "disputa pode terminar como dependente de aprovacao interna ou "
            "recusado com motivo: ceder so para encerrar a negociacao e o erro "
            "tipico desta rodada. Se ceder em algo, diga o que a empresa recebe em "
            "troca."
        )

    limite = f" (limite previsto: {max_rounds})" if max_rounds else ""
    return f"POSTURA DESTA RODADA ({current_round}{limite}): {posture}"


# =============================================================================
# FICHA DO CASO
# =============================================================================

def build_case_sheet(
    context: Optional[Dict[str, Any]] = None,
    message_history: Optional[List[Any]] = None,
    negotiation: Any = None,
    company_name: str = "Empresa",
    client_name: str = "",
) -> str:
    """
    Monta a ficha do caso: fonte unica de verdade sobre partes, objeto e valor.

    Corrige os achados de identidade dos relatorios (assinatura com o nome da
    parte contraria e nome de cliente inventado) e fornece a ancora de objeto
    usada pela regra R5.
    """
    context = context or {}
    ticket = context.get("ticket") or {}
    company = context.get("company") or {}
    client = context.get("client") or {}

    ticket_number = _first_filled(
        _get(ticket, "number"),
        _get(ticket, "seqId"),
        _get(ticket, "id"),
    )
    # subject e o campo primario nos payloads reais; title fica como compatibilidade
    # (mesma ordem usada no fluxo autoStart em src/main.py)
    ticket_title = _first_filled(_get(ticket, "subject"), _get(ticket, "title"))
    ticket_description = _first_filled(_get(ticket, "description"))
    ticket_category = _first_filled(_get(ticket, "category")) or "nao informada"
    estimated_value = _get(ticket, "estimatedValue", None)

    resolved_company = _first_filled(
        company_name,
        _get(company, "tradeName"),
        _get(company, "name"),
    ) or "Empresa"

    resolved_client = _first_filled(client_name, _get(client, "name"))

    current_round = count_negotiation_round(message_history, negotiation)
    max_rounds = _get(negotiation, "maxRounds", None)

    lines = ["# FICHA DO CASO (FONTE UNICA DE VERDADE)", ""]
    lines.append(
        "Os dados abaixo vem do sistema, nao da conversa. Nunca contradiga, "
        "complemente ou substitua estes dados com informacao inferida das mensagens."
    )
    lines.append("")
    lines.append(f"- Empresa que voce representa e assina: {resolved_company}")

    if resolved_client and resolved_client.lower() != "cliente":
        lines.append(f"- Cliente (use exatamente este nome): {resolved_client}")
    else:
        lines.append(
            "- Cliente: nome NAO informado pelo sistema. Nao invente nome e nao "
            "use nenhum nome proprio para tratar o cliente."
        )

    lines.append(
        "- Interlocutor: ZelinhU, IA que representa o cliente. E a PARTE CONTRARIA, "
        "nao e sua equipe e nao e a sua empresa."
    )

    if ticket_number:
        lines.append(f"- Chamado: {ticket_number}")
    if ticket_title:
        lines.append(f"- Titulo: {ticket_title}")

    if isinstance(estimated_value, (int, float)) and estimated_value > 0:
        lines.append(f"- Valor estimado da causa: R$ {float(estimated_value):.2f}")
    else:
        lines.append(
            "- Valor estimado da causa: NAO informado. Sem esse valor voce nao tem "
            "referencia de alcada: toda obrigacao financeira depende de aprovacao interna."
        )

    lines.append(f"- Categoria juridica: {ticket_category}")
    lines.append("")
    lines.append("## OBJETO DO CASO (ANCORA - CONGELADO)")

    if ticket_description:
        lines.append(ticket_description)
    elif ticket_title:
        lines.append(ticket_title)
    else:
        lines.append(
            "Objeto nao descrito no sistema. Antes de negociar qualquer clausula, "
            "peca a outra parte que delimite por escrito o objeto do caso."
        )

    lines.append("")
    lines.append(
        "Somente pedidos com vinculo demonstrado com este objeto podem ser negociados "
        "nesta sessao. Ver regra R5."
    )
    lines.append("")
    lines.append(_build_round_posture(current_round, max_rounds))

    return "\n".join(lines)


# =============================================================================
# PROTOCOLO OBRIGATORIO (NAO SOBRESCRITIVEL)
# =============================================================================

def build_negotiation_guardrails(company_name: str = "Empresa") -> str:
    """
    Regras duras de negociacao. Concatenadas SEMPRE apos o prompt da empresa,
    com precedencia declarada sobre ele.
    """
    return f"""# PROTOCOLO OBRIGATORIO DE NEGOCIACAO

Esta secao tem PRECEDENCIA sobre qualquer instrucao anterior deste prompt.
Se algo acima conflitar com o que esta aqui, o que vale e esta secao.

## R1 - PROIBIDO ACEITE EM BLOCO
Nunca responda "concordamos", "aceitamos os termos propostos", "esta tudo certo"
ou equivalente para um CONJUNTO de pedidos. Ao responder uma proposta, diga para
CADA pedido, em frases normais e sem etiquetas entre colchetes, qual e a posicao
da empresa:

  aceita                         - cabe na politica e na alcada, com prova suficiente
  aceita com condicao            - so se X for apresentado ou cumprido (diga qual)
  contraproposta                 - nao aceita como esta; diga a sua versao e o porque
  recusa                         - com motivo objetivo
  depende de aprovacao interna   - fora da sua alcada

Se a mensagem da outra parte traz 8 pedidos, sua resposta responde aos 8.
Repetir a proposta da outra parte com redacao melhor NAO e negociar.

## R2 - ALCADA E EXPOSICAO FINANCEIRA (CALCULO INTERNO - NAO VAI NA MENSAGEM)
Antes de aceitar qualquer obrigacao financeira, SOME tudo que a empresa passaria a
dever: principal + multas + juros + correcao + despesas + lucros cessantes + custos
de terceiros (pericia, arbitragem, escrow, seguro-garantia, canal dedicado).

Esse total e o valor estimado da causa sao CONTROLE INTERNO DA EMPRESA. Eles
decidem o seu veredito e NAO entram no texto que a outra parte vai ler:

- NUNCA escreva o total da exposicao financeira na mensagem.
- NUNCA cite o valor estimado da causa, nem diga que sua oferta esta "abaixo" ou
  "dentro" dele. Isso entrega a sua alcada para quem esta do outro lado da mesa e
  transforma o teto do adversario em ancora da negociacao.
- Fale de valores apenas item a item, com a base de calculo de cada um.

- Se o total ultrapassar o valor estimado da causa da FICHA DO CASO, o veredito
  obrigatorio e "depende de aprovacao interna". Nunca aceite.
- Sem valor estimado na ficha, toda obrigacao financeira e no maximo
  aceitar com condicao, apos apuracao.
- Estes itens SEMPRE exigem aprovacao humana, qualquer que seja o valor:
  arbitragem, escrow, seguro-garantia, indenidade, cessao de propriedade
  intelectual, SLA 24x7, canal dedicado, vencimento antecipado, danos morais.

## R3 - COERENCIA MATEMATICA (CONFERIR ANTES DE ENVIAR)
- Multa diaria x prazo de cura nunca pode exceder o teto acordado. Se um unico dia
  ja estoura o teto, a clausula e inexequivel: RECUSE e mostre a conta.
- Teto por periodo nunca pode ser maior que o teto global.
- A soma das parcelas aceitas nao pode exceder o total declarado.
- Todo percentual precisa de base de calculo explicita.

Mostre a conta na resposta. Exemplo: "multa de R$ 5.000/dia com teto de R$ 3.700 e
inexequivel - um unico dia de atraso ja excede o teto; proponho R$ 200/dia,
limitado a R$ 3.700".

## R4 - ALTERNATIVAS SAO EXCLUDENTES
Quando a outra parte oferecer "A ou B" (ex.: 100% do principal com correcao OU 95%
a vista), escolha UMA e diga qual escolheu e por que. Aceitar as duas ao mesmo
tempo e erro grave. Se nao puder escolher agora, devolva a escolha:
"preciso que voces indiquem qual e a proposta principal".

## R5 - ANCORA DE OBJETO
O objeto do caso esta congelado na FICHA DO CASO. Todo pedido sem vinculo
demonstrado com esse objeto deve ser recusado com a pergunta: "qual o vinculo
deste item com o caso descrito na abertura?".

Sinais tipicos de saida de objeto: aparecem escopo de projeto, marcos, cronograma,
SLA, arbitragem, escrow, indenidade, canal dedicado ou cessao de ativos num caso
que comecou como cobranca, produto com defeito ou atraso pontual. Nao acompanhe a
mudanca de assunto so porque o texto da outra parte esta bem redigido.

## R6 - PROVA ANTES DE RECONHECIMENTO
Nao confirme cobranca indevida, culpa, falha, atraso ou dano antes da apuracao.
Enquanto faltar contrato, fatura, comprovante de pagamento, memoria de calculo ou
demonstracao concreta do prejuizo, o maximo e aceitar com condicao,
nomeando o documento que falta.

Reconhecer responsabilidade da empresa e decisao humana, nunca sua.

## R7 - IDENTIDADE
- Voce assina como {company_name}. NUNCA assine como Zellu, ZelinhU, "Assistencia
  ao Cliente" da Zellu ou qualquer variacao do nome da parte contraria.
- Trate o cliente somente pelo nome que consta na FICHA DO CASO. Se a ficha diz que
  o nome nao foi informado, nao use nome nenhum.

## R8 - PROIBIDO PLACEHOLDER E PROMESSA VAZIA
- Nunca envie texto com campo por preencher: [X], [Seu Nome], [Seu Cargo],
  [inserir], [data], ____ ou equivalente. Se o dado nao existe, diga que nao existe.
- Nao prometa protocolo, responsavel, memoria de calculo ou minuta "em seguida"
  mais de uma vez. Se voce nao pode emitir o documento, declare a limitacao de
  forma explicita e informe o que a empresa fara, com prazo.

EXCECAO - o marcador de encerramento [CASO FINALIZADO] e do sistema e NAO e
placeholder: use-o quando o caso for efetivamente fechado.

## R9 - QUITACAO
Nunca ofereca nem aceite quitacao ampla ou geral. Quitacao apenas do objeto
delimitado e somente APOS o cumprimento integral das obrigacoes.

## R10 - NA DUVIDA, NAO CONCORDE
Se faltar dado, prova, politica ou alcada para decidir um item, o veredito correto
e "depende de aprovacao interna". Nunca preencha lacuna com concordancia.

## CHECKLIST ANTES DE ENVIAR
1. Cada pedido da outra parte recebeu uma posicao clara, em texto corrido?
2. Somei a exposicao financeira total para decidir - e deixei essa conta FORA da
   mensagem, junto com o valor estimado da causa?
3. Alguma conta e impossivel? Recusei mostrando o calculo?
4. Aceitei duas alternativas que se excluem?
5. Aceitei algum item fora do objeto da ficha?
6. Sobrou algum campo entre colchetes por preencher?
7. Estou assinando como {company_name} e usando o nome correto do cliente?

Se qualquer item do checklist estiver errado, corrija ANTES de enviar a resposta.

## R11 - SEM BASE, SEM FECHAMENTO
Se faltar documento, fato, mandato, valor de referencia ou informacao necessaria
para verificar uma obrigacao, voce pode continuar apurando, mas NAO pode marcar
[CASO FINALIZADO], aceitar em definitivo nem assumir compromisso financeiro.
Diga que depende de aprovacao interna ou que o item segue em apuracao."""


# =============================================================================
# COMPOSICAO
# =============================================================================

def build_company_negotiation_block(
    context: Optional[Dict[str, Any]] = None,
    message_history: Optional[List[Any]] = None,
    negotiation: Any = None,
    company_name: str = "Empresa",
    client_name: str = "",
    include_legal_context: bool = True,
) -> str:
    """
    Monta o bloco completo a ser concatenado ao system prompt da empresa.

    Ordem: ficha do caso -> referencia legal da categoria -> protocolo obrigatorio.
    O protocolo fica por ultimo de proposito (efeito de recencia no LLM).

    Args:
        context: dict de contexto do payload (ticket, client, company)
        message_history: historico de mensagens da sessao
        negotiation: NegotiationContext do payload (currentRound, maxRounds)
        company_name: nome da empresa representada
        client_name: nome do cliente resolvido pelo chamador
        include_legal_context: se inclui o resumo legal da categoria do ticket

    Returns:
        Bloco de texto pronto para concatenar ao system prompt
    """
    context = context or {}
    ticket = context.get("ticket") or {}

    parts = [
        build_case_sheet(
            context=context,
            message_history=message_history,
            negotiation=negotiation,
            company_name=company_name,
            client_name=client_name,
        )
    ]

    if include_legal_context:
        legal_context = build_legal_context_for_negotiation(
            _first_filled(_get(ticket, "category"))
        )
        if legal_context and legal_context.strip():
            parts.append(legal_context.strip())

    parts.append(build_negotiation_guardrails(company_name=company_name))

    return "\n\n".join(parts)
