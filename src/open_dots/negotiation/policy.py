# -*- coding: utf-8 -*-
"""Decisao do ZelinhU em cada rodada, em codigo e sem modelo.

POR QUE EM CODIGO
-----------------
Ate 05/10/2026 quem decidia ceder era o proprio modelo que redigia: ele dava
uma nota de 0 a 100 para a oferta da empresa e, acima de 80, a proposta ia ao
cliente. A nota era calculada sobre o ULTIMO pedido do ZelinhU - se ele ja tinha
baixado o pedido, a regua baixava junto -, e o prompt mandava "propor solucao
intermediaria" em toda rodada. Resultado: o ZelinhU cedia a qualquer coisa.

Aqui a jogada sai de numeros:

- ``target``: o que o cliente pede (valor da causa). Nunca muda.
- ``floor``: o minimo que o CLIENTE disse aceitar. Sem essa informacao, o piso e
  o proprio ``target``: o ZelinhU nao abre mao de nada sozinho e leva a melhor
  proposta ao cliente decidir.
- o historico de pedidos do ZelinhU e de ofertas da empresa, rodada a rodada.

Regras:

1. Empresa cobriu o pedido atual -> leva ao cliente (quem aceita e ele).
2. Teto de rodadas -> leva a melhor proposta ao cliente, sem aceitar nada.
3. Empresa diz que e a ultima proposta: se cobre o piso, leva ao cliente; se nao
   cobre, contesta UMA vez e, se ela repetir, leva ao cliente dizendo que fica
   abaixo do que ele pode aceitar.
4. Empresa recusa negociar duas vezes seguidas sem oferta -> leva ao cliente.
5. Fora isso, contrapropoe. So reduz o pedido quando a empresa MELHOROU a oferta
   desde a rodada anterior, em passos limitados, e nunca abaixo do piso.

Codigo puro: sem LLM, sem rede, sem estado global. O estado vive na sessao
(``session['negociacao']``), junto do resto do Sender.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

# Fracao do espaco restante (pedido atual ate o maior entre piso e oferta) que o
# ZelinhU pode ceder numa rodada em que a empresa melhorou.
CONCESSION_SHARE = Decimal("0.3")

COUNTER = "counter_proposal"
TO_CLIENT = "send_to_client"

# Posturas: o que a mensagem precisa fazer. O redator recebe isto, nao numeros
# soltos para interpretar.
OPENING = "abertura"
HOLD = "manter"
CONCEDE = "ajustar"
CHALLENGE_FINAL = "contestar_ultima"
PRESS = "cobrar_resposta"
CLOSE_MET = "levar_ao_cliente_atendido"
CLOSE_FINAL = "levar_ao_cliente_ultima"
CLOSE_CAP = "levar_ao_cliente_limite"
CLOSE_REFUSAL = "levar_ao_cliente_recusa"


@dataclass
class Move:
    """Jogada da rodada."""

    action: str
    posture: str
    reason: str
    ask: Optional[Decimal] = None
    previous_ask: Optional[Decimal] = None
    offer: Optional[Decimal] = None
    floor: Optional[Decimal] = None

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        for key in ("ask", "previous_ask", "offer", "floor"):
            if data[key] is not None:
                data[key] = str(data[key])
        return data

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> Optional["Move"]:
        if not data:
            return None
        values = dict(data)
        for key in ("ask", "previous_ask", "offer", "floor"):
            values[key] = to_decimal(values.get(key))
        return cls(**{k: values.get(k) for k in cls.__dataclass_fields__})


@dataclass
class NegotiationTrack:
    """O que a politica precisa lembrar entre rodadas."""

    asks: List[Optional[Decimal]] = field(default_factory=list)
    offers: List[Optional[Decimal]] = field(default_factory=list)
    final_challenged: bool = False
    refusals_in_a_row: int = 0
    # Jogada ja decidida por rodada: a mesma mensagem da empresa reprocessada
    # (reenvio, retry apos timeout) nao pode empurrar a negociacao duas vezes.
    decided: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    @property
    def last_ask(self) -> Optional[Decimal]:
        return next((a for a in reversed(self.asks) if a is not None), None)

    def best_offer(self) -> Optional[Decimal]:
        known = [o for o in self.offers if o is not None]
        return max(known) if known else None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "asks": [str(a) if a is not None else None for a in self.asks],
            "offers": [str(o) if o is not None else None for o in self.offers],
            "final_challenged": self.final_challenged,
            "refusals_in_a_row": self.refusals_in_a_row,
            "decided": dict(self.decided),
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "NegotiationTrack":
        data = data or {}
        return cls(
            asks=[to_decimal(a) for a in data.get("asks") or []],
            offers=[to_decimal(o) for o in data.get("offers") or []],
            final_challenged=bool(data.get("final_challenged")),
            refusals_in_a_row=int(data.get("refusals_in_a_row") or 0),
            decided=dict(data.get("decided") or {}),
        )

    def reset_after_client_rejection(self) -> None:
        """O cliente recusou: a proxima "ultima proposta" volta a ser contestada."""
        self.final_challenged = False
        self.refusals_in_a_row = 0


def to_decimal(value: Any) -> Optional[Decimal]:
    if value in (None, ""):
        return None
    if isinstance(value, Decimal):
        return value
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result >= 0 else None


def _round_money(value: Decimal) -> Decimal:
    """Valor redondo para negociar: dezena acima de R$ 1.000, real abaixo."""
    step = Decimal("10") if value >= Decimal("1000") else Decimal("1")
    return (value / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * step


def opening_move(target: Optional[Decimal], floor: Optional[Decimal] = None) -> Move:
    """Primeira mensagem: o pedido cheio do cliente."""
    return Move(
        action=COUNTER, posture=OPENING, ask=target, floor=floor,
        reason="abertura: pedido integral do cliente",
    )


def decide(
    *,
    target: Optional[Decimal],
    floor: Optional[Decimal],
    track: NegotiationTrack,
    offer: Optional[Decimal],
    round_number: int,
    round_cap: int,
    declared_final: bool = False,
    refused_all: bool = False,
    accepts_client_ask: bool = False,
    open_items: int = 0,
    settled_items: int = 0,
) -> Move:
    """Jogada desta rodada. Atualiza ``track`` com a oferta e o pedido."""
    floor = floor if floor is not None else target
    if target is not None and floor is not None and floor > target:
        floor = target

    previous_best = track.best_offer()
    track.offers.append(offer)
    current_ask = track.last_ask if track.last_ask is not None else target

    refused_now = refused_all and offer is None and not accepts_client_ask
    track.refusals_in_a_row = track.refusals_in_a_row + 1 if refused_now else 0

    def close(posture: str, reason: str) -> Move:
        track.asks.append(current_ask)
        # O cliente decide sobre a MELHOR proposta da negociacao, nao so sobre o
        # que veio nesta rodada (a empresa pode ter so repetido "e o que temos").
        best = offer if offer is not None else track.best_offer()
        return Move(action=TO_CLIENT, posture=posture, reason=reason, ask=current_ask,
                    previous_ask=current_ask, offer=best, floor=floor)

    def counter(posture: str, reason: str, ask: Optional[Decimal]) -> Move:
        track.asks.append(ask)
        return Move(action=COUNTER, posture=posture, reason=reason, ask=ask,
                    previous_ask=current_ask, offer=offer, floor=floor)

    # 1. A empresa atendeu o que o cliente pede agora.
    if accepts_client_ask:
        return close(CLOSE_MET, "empresa aceitou o pedido do cliente")
    if current_ask is not None and offer is not None and offer >= current_ask:
        return close(CLOSE_MET, f"oferta {offer} cobre o pedido {current_ask}")
    if target is None and settled_items > 0 and open_items == 0:
        return close(CLOSE_MET, "todos os itens pedidos foram aceitos")

    # 2. Teto de rodadas: o cliente decide sobre a melhor proposta.
    if round_number >= round_cap:
        return close(CLOSE_CAP, f"rodada {round_number} atingiu o teto {round_cap}")

    # 3. "Ultima proposta".
    if declared_final:
        if offer is not None and floor is not None and offer >= floor:
            return close(CLOSE_FINAL, f"ultima proposta {offer} cobre o piso {floor}")
        if track.final_challenged:
            return close(CLOSE_FINAL, "empresa repetiu a ultima proposta abaixo do piso")
        track.final_challenged = True
        return counter(CHALLENGE_FINAL, "ultima proposta abaixo do piso: contestar uma vez", current_ask)

    # 4. Recusa sem oferta, duas vezes seguidas.
    if track.refusals_in_a_row >= 2:
        return close(CLOSE_REFUSAL, "empresa recusou negociar em duas rodadas seguidas")
    if refused_now:
        return counter(PRESS, "empresa recusou sem oferta: cobrar posicao concreta", current_ask)

    # 5. Contraproposta. Reduzir so quando a empresa MELHOROU uma oferta que ja
    # existia: a primeira oferta, ou a mesma oferta repetida, nao compra concessao.
    improved = offer is not None and previous_best is not None and offer > previous_best
    if (
        improved
        and current_ask is not None
        and floor is not None
        and current_ask > floor
    ):
        reference = max(floor, offer)
        gap = current_ask - reference
        if gap > 0:
            new_ask = _round_money(current_ask - gap * CONCESSION_SHARE)
            new_ask = max(new_ask, floor)
            if offer is not None and new_ask <= offer:
                new_ask = current_ask  # nunca encostar na oferta: isso e aceitar
            if new_ask < current_ask:
                return counter(CONCEDE, f"empresa subiu para {offer}: ajuste de {current_ask} para {new_ask}", new_ask)

    if offer is None:
        return counter(HOLD, "empresa nao apresentou valor: manter pedido e pedir proposta concreta", current_ask)
    return counter(HOLD, f"oferta {offer} abaixo do pedido {current_ask} sem melhora suficiente: manter", current_ask)
