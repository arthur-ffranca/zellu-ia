# -*- coding: utf-8 -*-
"""Leitura da mensagem da empresa: so fatos, nenhuma avaliacao.

O antigo SenderAgent pedia ao modelo uma "recomendacao" (aceitar, negociar,
recusar) e um percentual estimado. Era o modelo decidindo ceder. Aqui ele so
extrai o que a empresa disse - quanto ofereceu, se chamou de ultima proposta, o
que pediu ao cliente - e a decisao fica com ``policy.decide``.
"""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from src.negotiation_ledger import LedgerItemUpdate


class AgreementDetails(BaseModel):
    """Termos da proposta da empresa, para o cliente decidir."""

    summary: str = Field(default="", description="O que a empresa propoe, em uma ou duas frases")
    value: Optional[float] = Field(default=None, description="Valor total em reais da proposta, se houver")
    conditions: List[str] = Field(default_factory=list, description="Condicoes da proposta")


class NegotiationRequest(BaseModel):
    """Um pedido do ZelinhU no meio da negociacao (spec de 01/09).

    Vocabularios fechados e titulo com teto de 200: sao as recusas que o backend
    devolve em 400 (REQUEST_MALFORMED e REQUEST_TITLE_TOO_LONG).
    """

    audience: Literal["client", "company"] = Field(
        description="Para QUEM e o pedido: client (o cliente) ou company (a empresa)"
    )
    type: Literal["document", "receipt", "meeting", "information", "other"] = Field(
        description=(
            "O QUE se pede. receipt e comprovante de pagamento; document e qualquer "
            "outro documento; meeting e reuniao; information e dado textual sem arquivo"
        )
    )
    response_type: Literal["file", "confirmation", "text"] = Field(
        description=(
            "COMO se responde: file (arquivo), confirmation (sim/nao) ou text. Um "
            "pedido tem UM formato"
        )
    )
    title: str = Field(max_length=200, description="Titulo do card, no maximo 200 caracteres")
    description: str = Field(default="", description="Detalhe do pedido. Opcional")


class CompanyTurnReading(BaseModel):
    """O que a empresa disse nesta mensagem. Fatos, nao julgamento."""

    company_offer_total: Optional[float] = Field(
        default=None,
        description=(
            "Valor total em reais que a EMPRESA se compromete a pagar, devolver, "
            "estornar ou abater nesta proposta, somando os itens em dinheiro. null se "
            "ela nao ofereceu dinheiro, se so repetiu o pedido do cliente ou se o valor "
            "depende de algo ainda nao definido."
        ),
    )
    offer_summary: str = Field(
        default="",
        description="O que a empresa ofereceu, em uma frase, inclusive o que nao e dinheiro. Vazio se nada.",
    )
    accepts_client_ask: bool = Field(
        default=False,
        description="True somente se a empresa aceitou INTEGRALMENTE o ultimo pedido do cliente, sem condicao nova.",
    )
    declared_final: bool = Field(
        default=False,
        description="True se a empresa disse que esta e a proposta final, a ultima ou o maximo que pode fazer.",
    )
    refused_all: bool = Field(
        default=False,
        description="True se a empresa se recusou a oferecer qualquer coisa (nega responsabilidade, encerra) sem proposta.",
    )
    company_arguments: List[str] = Field(
        default_factory=list,
        description="Argumentos e objecoes que a empresa usou, um por item, nas palavras dela resumidas.",
    )
    agreement_details: Optional[AgreementDetails] = Field(
        default=None,
        description="Termos da proposta da empresa quando ha proposta concreta (valor ou providencia).",
    )
    ledger_updates: List[LedgerItemUpdate] = Field(
        default_factory=list,
        description="Como a mensagem muda cada item da negociacao. Um update por item tocado.",
    )
    requests: List[NegotiationRequest] = Field(
        default_factory=list,
        description=(
            "Solicitacoes a emitir. SEMPRE inclua, com audience client, o que a empresa "
            "pediu e so o cliente pode fornecer. Por iniciativa propria, raramente."
        ),
    )


class ReadingError(RuntimeError):
    """A leitura nao pode ser feita. Quem chama nao inventa uma no lugar."""


READING_INSTRUCTIONS = """Voce le a mensagem que a empresa enviou numa negociacao com o
representante de um cliente. Extraia so o que esta escrito. Nao avalie se a proposta
e boa, nao recomende nada.

Valor oferecido (company_offer_total): some apenas o que a EMPRESA se compromete a
pagar, devolver, estornar ou abater. Valor que ela so cita ao repetir o pedido do
cliente nao e oferta. Proposta condicionada ("podemos devolver R$ 500 se...") conta,
e a condicao vai em agreement_details.conditions. Credito, voucher e desconto em
compra futura contam pelo valor nominal; descreva-os em offer_summary.

accepts_client_ask so e verdadeiro quando a empresa aceita tudo o que o cliente pediu
na ultima mensagem, sem acrescentar condicao nem reduzir nada.

LIVRO-RAZAO (ledger_updates): registre item a item como a mensagem mudou o estado
da negociacao. Use o id do item quando ele ja existir no livro-razao; deixe item_id
vazio para item novo. Nunca um update generico para o pacote todo.

SOLICITACOES (requests): o que a empresa pediu e so o cliente pode fornecer
(documento, comprovante, nota fiscal, foto, endereco, dados bancarios, horarios, uma
resposta dele) vira SEMPRE solicitacao com audience "client". Agrupe o que se
responde de uma vez. Nao e acao do cliente o que a propria empresa faz. Nao repita o
que esta em JA SOLICITADO. Por iniciativa propria, so peca o que a negociacao nao
consegue avancar sem. Um formato por pedido; titulo de ate 200 caracteres; no maximo
6 pedidos."""


def build_reading_prompt(
    *,
    company_message: str,
    last_zelinhu_message: str,
    case_block: str,
    ledger_block: str,
    already_requested_block: str,
    documents_block: str,
) -> str:
    parts = [
        READING_INSTRUCTIONS,
        case_block,
        "ULTIMA MENSAGEM DO REPRESENTANTE DO CLIENTE:\n" + (last_zelinhu_message or "(nenhuma)"),
        "MENSAGEM DA EMPRESA A LER:\n" + (company_message or ""),
        ledger_block,
        already_requested_block,
        documents_block,
    ]
    return "\n\n".join(p for p in parts if p and p.strip())
