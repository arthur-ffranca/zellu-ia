# -*- coding: utf-8 -*-
"""
Livro-razao da negociacao (Fase 2).

MOTIVACAO
---------
Ate aqui a negociacao era uma lista de mensagens. Pedidos, vereditos, valores e
condicoes existiam apenas dissolvidos em prosa, e a cada rodada o modelo
reconstruia tudo relendo a conversa. Dai vem os dois achados mais persistentes
dos relatorios:

  - "Afirmou simultaneamente que nao haveria pagamentos adicionais e que
     existiriam pagamentos futuros por marcos"          (Caso 2, empresa)
  - "Fez desaparecer o reembolso principal de R$ 18.500,00"  (Caso 3, ZelinhU)

Nenhum dos dois e erro de redacao: e falta de memoria estruturada.

O QUE ESTE MODULO FAZ
---------------------
A negociacao passa a ser uma lista de ITENS com estado. Cada mensagem e gerada
A PARTIR do ledger, e cada resposta recebida ATUALIZA o ledger via saida
estruturada. Com isso:

  - a regra R1 ("um veredito por pedido") deixa de ser instrucao e vira
    invariante verificavel: item sem veredito aparece como pendente
  - contradicao entre rodadas vira deteccao deterministica, nao sorte
  - pedido abandonado pelo proprio representante do cliente fica visivel

PERSISTENCIA
------------
O ledger e serializavel (to_dict/from_dict) mas ainda vive em memoria, junto das
sessoes. Banco continua pendente - se o processo reiniciar no meio de uma
negociacao, o ledger recomeca vazio e volta a crescer a partir da rodada atual.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# =============================================================================
# VOCABULARIO
# =============================================================================

STATUS_ABERTO = "ABERTO"
STATUS_EM_APURACAO = "EM_APURACAO"
STATUS_ACEITO = "ACEITO"
STATUS_ACEITO_SOB_CONDICAO = "ACEITO_SOB_CONDICAO"
STATUS_CONTRAPROPOSTA = "CONTRAPROPOSTA"
STATUS_RECUSADO = "RECUSADO"
STATUS_DEPENDE_APROVACAO = "DEPENDE_DE_APROVACAO_INTERNA"

VALID_STATUSES = (
    STATUS_ABERTO,
    STATUS_EM_APURACAO,
    STATUS_ACEITO,
    STATUS_ACEITO_SOB_CONDICAO,
    STATUS_CONTRAPROPOSTA,
    STATUS_RECUSADO,
    STATUS_DEPENDE_APROVACAO,
)

# Itens fechados: reabrir sem fato novo e contradicao
SETTLED_STATUSES = (STATUS_ACEITO,)

# Quem pede uma coisa nao pode dar essa coisa por aceita (log de 25/08/2026).
#
# O livro-razao da IA da Empresa le SEMPRE mensagens da outra parte
# (actor="cliente"). O ZelinhU abriu uma mensagem com "obrigado pelo avanco:
# diagnostico sem custo, canal de escalonamento, reembolso do laudo sob condicoes
# e previsao de abatimento ja constam" - um RESUMO do que a empresa tinha
# oferecido - e o extrator marcou os 5 itens como ACEITO. O livro-razao da
# empresa saltou de 0 para 5 acordados na rodada 3, quando so 1 item estava
# aceito de fato, e nunca mais se moveu.
#
# A partir dali a IA da Empresa passou a escrever "como ja acordado" (garantia
# estendida, que estava em DEPENDE DE APROVACAO INTERNA) e "mantemos o parametro
# ja pactuado" (30 dias, que nunca foram pactuados por ninguem). A alucinacao de
# historico nao nasceu na redacao: nasceu aqui.
SETTLING_ACTOR = "empresa"

# Itens que ainda exigem alguma acao das partes
OPEN_STATUSES = (
    STATUS_ABERTO,
    STATUS_EM_APURACAO,
    STATUS_CONTRAPROPOSTA,
    STATUS_ACEITO_SOB_CONDICAO,
    STATUS_DEPENDE_APROVACAO,
)


def _slug(text: str) -> str:
    """Identificador estavel a partir da descricao do item."""
    stripped = "".join(
        c for c in unicodedata.normalize("NFD", str(text or ""))
        if unicodedata.category(c) != "Mn"
    )
    slug = re.sub(r"[^a-z0-9]+", "_", stripped.lower()).strip("_")
    return slug[:48] or "item"


# =============================================================================
# CONTRATO DE ATUALIZACAO (saida estruturada do LLM)
# =============================================================================

class LedgerItemUpdate(BaseModel):
    """Uma mudanca de estado em um item da negociacao."""

    item_id: str = Field(
        default="",
        description="Id do item existente que esta sendo atualizado. Vazio para item novo.",
    )
    description: str = Field(
        description="Descricao curta e estavel do pedido (ex: 'reembolso do principal')"
    )
    status: str = Field(
        description=(
            "Estado do item apos esta rodada. Um de: ABERTO, EM_APURACAO, ACEITO, "
            "ACEITO_SOB_CONDICAO, CONTRAPROPOSTA, RECUSADO, DEPENDE_DE_APROVACAO_INTERNA"
        )
    )
    amount: Optional[float] = Field(
        default=None, description="Valor em reais associado ao item, se houver"
    )
    conditions: List[str] = Field(
        default_factory=list, description="Condicoes para o item valer"
    )
    evidence_required: str = Field(
        default="", description="Documento ou prova que ainda falta, se houver"
    )
    justification: str = Field(
        default="",
        description=(
            "Fato novo que justifica mudar um item ja acordado. Obrigatorio para "
            "reabrir ou alterar item com status ACEITO."
        ),
    )


class LedgerUpdateBatch(BaseModel):
    """Todas as mudancas produzidas pela leitura de uma mensagem."""

    items: List[LedgerItemUpdate] = Field(default_factory=list)
    summary: str = Field(default="", description="Resumo de uma linha do que mudou")


# =============================================================================
# ITEM E LEDGER
# =============================================================================

@dataclass
class LedgerItem:
    """Um pedido da negociacao e seu estado atual."""

    id: str
    description: str
    status: str = STATUS_ABERTO
    amount: Optional[Decimal] = None
    conditions: List[str] = field(default_factory=list)
    evidence_required: str = ""
    opened_by: str = ""
    last_round: int = 0
    history: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "status": self.status,
            "amount": float(self.amount) if self.amount is not None else None,
            "conditions": list(self.conditions),
            "evidence_required": self.evidence_required,
            "opened_by": self.opened_by,
            "last_round": self.last_round,
            "history": list(self.history),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LedgerItem":
        amount = data.get("amount")
        raw_status = str(data.get("status", STATUS_ABERTO) or STATUS_ABERTO).upper()
        status = raw_status if raw_status in VALID_STATUSES else STATUS_ABERTO
        return cls(
            id=data.get("id", ""),
            description=data.get("description", ""),
            status=status,
            amount=Decimal(str(amount)) if amount is not None else None,
            conditions=list(data.get("conditions") or []),
            evidence_required=data.get("evidence_required", ""),
            opened_by=data.get("opened_by", ""),
            last_round=data.get("last_round", 0),
            history=list(data.get("history") or []),
        )


@dataclass
class NegotiationLedger:
    """Estado negocial da sessao."""

    items: Dict[str, LedgerItem] = field(default_factory=dict)
    current_round: int = 0

    # Aceites recusados na ultima chamada de apply() (ver SETTLING_ACTOR).
    #
    # NAO entra em to_dict() e NAO se mistura com as contradicoes: contradicao vai
    # para o prompt com "aponte isso na sua resposta", e aqui o resumo da outra
    # parte costuma estar CERTO - ela so nao pode fechar o item sozinha. Mandar a
    # IA contestar isso a faria negar avancos que ela mesma ofereceu. E
    # diagnostico da rodada, para o log.
    blocked_settlements: List[str] = field(default_factory=list)

    # ------------------------------------------------------------------ leitura
    def open_items(self) -> List[LedgerItem]:
        return [i for i in self.items.values() if i.status in OPEN_STATUSES]

    def settled_items(self) -> List[LedgerItem]:
        return [i for i in self.items.values() if i.status in SETTLED_STATUSES]

    def total_accepted_amount(self) -> Decimal:
        """Soma dos valores dos itens ja aceitos - alimenta o gate de alcada."""
        total = Decimal("0")
        for item in self.items.values():
            if item.status in (STATUS_ACEITO, STATUS_ACEITO_SOB_CONDICAO) and item.amount:
                total += item.amount
        return total

    def coverage_percent(self, case_value: Optional[Decimal]) -> Optional[Decimal]:
        """
        Quanto do valor da causa ja esta aceito, em percentual.

        Substitui o `acceptable_percentage` estimado pelo modelo como numero de
        referencia. Na auditoria de 23/08/2026 a analise devolveu 0% numa sessao
        com varios itens acordados: o modelo estava obedecendo a instrucao de nao
        chutar percentual quando a mensagem nao traz valor manchete (e isso e
        certo), mas 0% no prompt le como "nao ofereceram nada".

        O livro-razao sabe o numero de verdade.

        Returns:
            Percentual, ou None quando nao ha valor de causa para comparar.
        """
        if not case_value or case_value <= 0:
            return None
        return (self.total_accepted_amount() / case_value * 100).quantize(Decimal("0.1"))

    def abandoned_items(self, tolerance: int = 2) -> List[LedgerItem]:
        """
        Itens abertos que ninguem toca ha `tolerance` rodadas.

        E assim que "o reembolso principal de R$ 18.500 desapareceu" (Caso 3)
        deixa de passar despercebido.
        """
        return [
            item for item in self.items.values()
            if item.status in OPEN_STATUSES
            and self.current_round - item.last_round >= tolerance
        ]

    # ------------------------------------------------------------------ escrita
    def apply(
        self,
        batch: LedgerUpdateBatch,
        round_number: int,
        actor: str = "",
    ) -> List[str]:
        """
        Aplica as mudancas de uma rodada e devolve as contradicoes encontradas.

        Args:
            batch: mudancas produzidas pela leitura da mensagem
            round_number: rodada atual
            actor: "empresa" ou "cliente" - quem produziu a mudanca

        Returns:
            Lista de contradicoes em texto (vazia = coerente)
        """
        self.current_round = max(self.current_round, round_number)
        contradictions: List[str] = []
        self.blocked_settlements = []

        for update in batch.items:
            status = (update.status or STATUS_ABERTO).strip().upper()
            if status not in VALID_STATUSES:
                contradictions.append(
                    f"status invalido '{update.status}' para '{update.description}' "
                    f"- item mantido como {STATUS_ABERTO}"
                )
                status = STATUS_ABERTO

            item_id = (update.item_id or "").strip() or _slug(update.description)
            amount = None
            if update.amount is not None:
                candidate_amount = Decimal(str(update.amount))
                if candidate_amount < 0:
                    contradictions.append(
                        f"valor negativo invalido para '{update.description}': {candidate_amount}"
                    )
                else:
                    amount = candidate_amount

            existing = self.items.get(item_id)

            # Aceite so vale vindo de quem concede o item. Recapitulacao da outra
            # parte mantem o status que o item ja tinha (ver SETTLING_ACTOR).
            # Actor vazio passa direto: quem nao informa lado nao e classificado.
            if status == STATUS_ACEITO and actor and actor != SETTLING_ACTOR:
                anterior = existing.status if existing else STATUS_ABERTO
                self.blocked_settlements.append(
                    f"'{update.description}' aparece como ACEITO numa mensagem de "
                    f"'{actor}', que e quem PEDE o item, nao quem concede - "
                    f"mantido como {anterior}"
                )
                status = anterior

            if existing is None:
                self.items[item_id] = LedgerItem(
                    id=item_id,
                    description=update.description,
                    status=status,
                    amount=amount,
                    conditions=list(update.conditions),
                    evidence_required=update.evidence_required,
                    opened_by=actor,
                    last_round=round_number,
                    history=[{
                        "round": round_number,
                        "actor": actor,
                        "status": status,
                        "amount": float(amount) if amount is not None else None,
                    }],
                )
                continue

            contradictions.extend(
                self._check_contradiction(existing, status, amount, update.justification)
            )

            existing.description = update.description or existing.description
            existing.status = status
            if amount is not None:
                existing.amount = amount
            if update.conditions:
                existing.conditions = list(update.conditions)
            if update.evidence_required:
                existing.evidence_required = update.evidence_required
            existing.last_round = round_number
            existing.history.append({
                "round": round_number,
                "actor": actor,
                "status": status,
                "amount": float(amount) if amount is not None else None,
                "justification": update.justification,
            })

        return contradictions

    def _check_contradiction(
        self,
        existing: LedgerItem,
        new_status: str,
        new_amount: Optional[Decimal],
        justification: str,
    ) -> List[str]:
        """Um item ja acordado so muda com fato novo declarado."""
        if existing.status not in SETTLED_STATUSES:
            return []
        if justification.strip():
            return []

        problems = []

        if new_status != existing.status:
            problems.append(
                f"'{existing.description}' estava {existing.status} e voltou para "
                f"{new_status} sem fato novo declarado"
            )

        if (
            new_amount is not None
            and existing.amount is not None
            and new_amount != existing.amount
        ):
            problems.append(
                f"'{existing.description}' estava acordado em R$ {existing.amount:.2f} "
                f"e mudou para R$ {new_amount:.2f} sem fato novo declarado"
            )

        return problems

    # ------------------------------------------------------------------ prompt
    def render(self) -> str:
        """Bloco de texto do ledger para o system prompt."""
        if not self.items:
            return (
                "# LIVRO-RAZAO DA NEGOCIACAO\n\n"
                "Nenhum item registrado ainda. Esta e a primeira rodada."
            )

        lines = ["# LIVRO-RAZAO DA NEGOCIACAO (ESTADO ATUAL)", ""]
        lines.append(
            "Este e o estado acordado ate agora. Sua mensagem deve partir daqui: "
            "nao repita pedido ja resolvido e nao contradiga item fechado."
        )
        lines.append("")

        acordados = self.settled_items()
        if acordados:
            lines.append("## JA ACORDADO (nao reabrir sem fato novo)")
            for item in acordados:
                lines.append(f"- [{item.id}] {item.description}{self._amount_suffix(item)}")
            lines.append("")

        abertos = self.open_items()
        if abertos:
            lines.append("## EM ABERTO (precisa de posicao nesta rodada)")
            for item in abertos:
                linha = f"- [{item.id}] {item.description} -> {item.status}{self._amount_suffix(item)}"
                if item.conditions:
                    linha += f" | condicoes: {'; '.join(item.conditions)}"
                if item.evidence_required:
                    linha += f" | falta: {item.evidence_required}"
                lines.append(linha)
            lines.append("")

        recusados = [i for i in self.items.values() if i.status == STATUS_RECUSADO]
        if recusados:
            lines.append("## RECUSADO")
            for item in recusados:
                lines.append(f"- [{item.id}] {item.description}")
            lines.append("")

        esquecidos = self.abandoned_items()
        if esquecidos:
            lines.append("## SEM MOVIMENTO HA DUAS OU MAIS RODADAS")
            lines.append(
                "Retome cada um destes itens ou diga expressamente que esta "
                "abrindo mao dele e por que:"
            )
            for item in esquecidos:
                lines.append(f"- [{item.id}] {item.description}")

        return "\n".join(lines).strip()

    @staticmethod
    def _amount_suffix(item: LedgerItem) -> str:
        return f" (R$ {item.amount:.2f})" if item.amount is not None else ""

    # ------------------------------------------------------------ serializacao
    def to_dict(self) -> Dict[str, Any]:
        return {
            "current_round": self.current_round,
            "items": [item.to_dict() for item in self.items.values()],
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "NegotiationLedger":
        data = data or {}
        items = {}
        for raw in data.get("items") or []:
            item = LedgerItem.from_dict(raw)
            if item.id:
                items[item.id] = item
        return cls(items=items, current_round=data.get("current_round", 0))
