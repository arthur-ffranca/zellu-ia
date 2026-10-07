# -*- coding: utf-8 -*-
"""Redacao das mensagens do ZelinhU.

O redator recebe uma jogada ja decidida (``policy.Move``) e escreve a mensagem.
Nao decide valor, nao decide aceitar, nao escolhe ceder.

Em relacao ao SenderAgent antigo:
- sem roteiro fixo ("1. agradecer, 2. reconhecer pontos positivos..."), que
  deixava todas as mensagens com o mesmo esqueleto;
- o historico vai como conversa (turnos), inteiro dentro de um limite generoso,
  e nao como 8 trechos de 800 caracteres colados num bloco;
- uma persona de quem representa o cliente, e nao um checklist.
"""
from __future__ import annotations

import re
import unicodedata
from decimal import Decimal
from typing import Any, Dict, List, Optional

from src.llm import assistant_message, user_message
from src.negotiation_acceptance import is_acceptance_message
from src.negotiation_math import find_money, format_brl

from . import policy

HISTORY_MESSAGES = 30
HISTORY_CHARS = 2500

# So termos que nao aparecem em conversa comum: "piso", "rodada" e "mandato" sao
# portugues corrente (piso laminado, rodada de reunioes, mandato do advogado).
_INTERNAL_TERMS = re.compile(
    r"livro[- ]?razao|inteligencia artificial|\bpercentual (?:estimado|da proposta)\b|"
    r"\bteto de rodadas\b|\bpiso do cliente\b|\bmandato do cliente\b",
)
# Pergunta sobre a decisao do cliente nao e aceite: "para saber se o cliente aceita".
_ASKING_CLIENT = re.compile(r"\bse (?:o cliente|a cliente|ele|ela|[A-Z][a-z]+) (?:aceita|concorda)\w*", re.IGNORECASE)
# Frase em que um valor e PROPOSTO (nao citado como fato do caso).
_PROPOSING = re.compile(
    r"propo|podemos|poderiamos|aceit|fechar|fecha|reduz|abaix|baixar|alternativ|acordo|"
    r"consideramos|topamos|seria possivel",
)
_SENTENCES = re.compile(r"(?<=[.!?;])\s+|\n+")
_AI_WORD = re.compile(r"\bIA\b")


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in text if not unicodedata.combining(c)).lower()


def persona(client_name: str, company_name: str) -> str:
    client = client_name if client_name and _norm(client_name) != "cliente" else "o cliente"
    company = company_name or "a empresa"
    return f"""Voce e o ZelinhU e representa {client} numa negociacao por escrito com {company}.
Voce escreve como um negociador experiente escreve num chat profissional: frases
diretas, educadas e firmes, em portugues do Brasil, sem formalidade de carta.

Como voce escreve:
- Responda ao que a empresa acabou de dizer. Se ela usou um argumento, enfrente-o
  com o fato do caso que o contradiz.
- Nao abra toda mensagem agradecendo, nao feche com formula pronta, nao use lista
  numerada a nao ser que esteja respondendo varios itens separados.
- Entre 50 e 170 palavras. Um assunto por paragrafo.
- Valores sempre no formato R$ 10.000,00.
- Sem emojis e sem campos entre colchetes.

Limites que voce nunca ultrapassa:
- Voce nao aceita proposta e nao diz que o cliente aceita: quem decide e o cliente.
- Voce so pede o que a DECISAO DESTA MENSAGEM manda pedir. Nao ofereca valor menor,
  alternativa, desconto ou prazo que ela nao traga.
- Nao invente fato, documento, data, protocolo ou valor que nao esteja no caso ou
  na conversa.
- Nao fale de rodadas, percentuais, piso, sistema, regras internas ou de ser uma IA.
- Nao transcreva dado pessoal do cliente (CPF, endereco, conta bancaria) para a empresa.
- Nao prometa nada em nome do cliente: o que depende dele vira solicitacao, nao promessa."""


def history_turns(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Mensagens da sessao como conversa: ZelinhU = assistant, empresa = user."""
    turns = []
    for message in (messages or [])[-HISTORY_MESSAGES:]:
        body = (message.get("body") or "").strip()
        if not body:
            continue
        if len(body) > HISTORY_CHARS:
            body = body[:HISTORY_CHARS].rstrip() + " [...]"
        if message.get("origin") == "zelinhu":
            turns.append(assistant_message(body))
        else:
            turns.append(user_message(body))
    return turns


def _money(value: Optional[Decimal]) -> str:
    return format_brl(value) if value is not None else ""


def brief(move: policy.Move, *, offer_summary: str = "", open_items: Optional[List[str]] = None,
          company_arguments: Optional[List[str]] = None) -> str:
    """O que esta mensagem precisa fazer, em linguagem de instrucao."""
    ask = _money(move.ask)
    offer = _money(move.offer)
    items = "; ".join(i for i in (open_items or []) if i)
    pedido = f"o valor de {ask}" if ask else (f"os itens: {items}" if items else "a reparacao do problema relatado")
    proposta = offer_summary or (f"{offer}" if offer else "")

    lines = ["DECISAO DESTA MENSAGEM (instrucao interna, nao a reproduza):"]
    if move.posture == policy.OPENING:
        lines.append(
            f"Abra a negociacao. Em poucas linhas, diga o que aconteceu com o cliente e peca {pedido}. "
            "Termine pedindo uma proposta concreta."
        )
    elif move.posture == policy.CONCEDE:
        lines.append(
            f"A empresa melhorou a proposta ({proposta}). Ajuste o pedido para {ask}, e nao menos. "
            "Apresente o ajuste como movimento para resolver agora, condicionado a que a empresa "
            "aceite esse valor; nao diga que existe margem alem dele."
        )
    elif move.posture == policy.CHALLENGE_FINAL:
        lines.append(
            f"A empresa disse que esta e a ultima proposta ({proposta}). Diga com clareza que ela fica "
            f"abaixo do que o cliente pode aceitar, mantenha {pedido} e pergunte se ha margem para "
            "chegar la. Se nao houver, a proposta sera levada ao cliente."
        )
    elif move.posture == policy.PRESS:
        lines.append(
            f"A empresa nao apresentou proposta. Mantenha {pedido}, responda ao argumento dela com os "
            "fatos do caso e peca uma posicao concreta."
        )
    elif move.action == policy.TO_CLIENT:
        motivo = {
            policy.CLOSE_MET: "a empresa atendeu o pedido",
            policy.CLOSE_FINAL: "a empresa apresentou a proposta final",
            policy.CLOSE_CAP: "a negociacao chegou ao ponto de ouvir o cliente",
            policy.CLOSE_REFUSAL: "a empresa nao apresentou proposta",
        }.get(move.posture, "e hora de o cliente decidir")
        lines.append(
            f"Encerre esta etapa: {motivo}. Diga em poucas linhas que vai levar "
            f"{'a proposta (' + proposta + ')' if proposta else 'a posicao da empresa'} ao cliente, "
            "que e quem decide. Nao aceite, nao comente se a proposta e boa, nao peca mais nada."
        )
        if move.posture in (policy.CLOSE_FINAL, policy.CLOSE_CAP) and move.offer is not None and move.ask is not None \
                and move.offer < move.ask:
            lines.append(f"Registre, sem drama, que a proposta fica abaixo do pedido de {ask}.")
    else:  # HOLD
        lines.append(
            f"Mantenha {pedido}. "
            + (f"A proposta atual da empresa ({proposta}) nao resolve: diga por que, com os fatos do caso. "
               if proposta else "A empresa ainda nao apresentou valor: peca uma proposta concreta. ")
            + "Nao reduza o pedido nem ofereca alternativa."
        )
    if company_arguments and move.action != policy.TO_CLIENT:
        lines.append("Argumentos da empresa a enfrentar: " + "; ".join(company_arguments[:4]))
    return "\n".join(lines)


def check(text: str, move: policy.Move) -> List[str]:
    """Defeitos que impedem o envio. Vazio = pode sair."""
    problems = []
    if not text or not text.strip():
        return ["mensagem vazia"]
    if is_acceptance_message(_ASKING_CLIENT.sub("", text)):
        problems.append("a mensagem aceita a proposta; quem decide e o cliente")
    plain = _norm(text)
    leak = _INTERNAL_TERMS.search(plain)
    if leak or _AI_WORD.search(text):
        problems.append(f"a mensagem fala de regra interna ({(leak.group(0) if leak else 'IA')})")
    if move.action == policy.COUNTER and move.ask is not None:
        values = find_money(text)
        if not any(abs(v - move.ask) < Decimal("1") for v in values):
            problems.append(f"a mensagem precisa pedir exatamente {format_brl(move.ask)}")
        # Valor abaixo do pedido so e defeito quando aparece PROPOSTO ("podemos
        # fechar em..."). Citar o que o cliente pagou ou o que a empresa ofereceu
        # e argumento, e e justamente o que a persona pede.
        for sentence in _SENTENCES.split(text):
            if not _PROPOSING.search(_norm(sentence)):
                continue
            for v in find_money(sentence):
                if v >= move.ask or (move.offer is not None and abs(v - move.offer) < Decimal("1")):
                    continue
                problems.append(
                    f"a mensagem propoe {format_brl(v)}, abaixo do pedido de {format_brl(move.ask)}"
                )
                break
    return problems


def fallback(move: policy.Move, *, client_name: str = "", offer_summary: str = "") -> str:
    """Texto curto quando o modelo falha. Natural, sem marcador e sem roteiro."""
    first = (client_name or "").split()[0] if client_name and _norm(client_name) != "cliente" else "o cliente"
    ask = _money(move.ask)
    if move.action == policy.TO_CLIENT:
        what = f"a proposta de voces ({offer_summary})" if offer_summary else "a posicao de voces"
        return f"Vou levar {what} para {first}, que e quem decide. Retorno assim que tiver a resposta."
    if move.posture == policy.OPENING:
        pedido = f"o ressarcimento de {ask}" if ask else "a solucao do problema"
        return (f"Represento {first} neste chamado. O caso esta descrito no atendimento, e o que ele busca e "
                f"{pedido}. Qual proposta voces conseguem apresentar?")
    if ask:
        return (f"Com o que esta no caso, o pedido de {ask} se mantem. Se houver uma proposta "
                f"concreta nesse sentido, levo para {first} decidir.")
    return ("O pedido da abertura se mantem. Preciso de uma proposta concreta para levar a "
            f"{first}.")
