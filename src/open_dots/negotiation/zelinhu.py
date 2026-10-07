# -*- coding: utf-8 -*-
"""Agente do ZelinhU: le a empresa, decide em codigo, redige.

Substitui o SenderAgent. Tres passos por rodada, cada um com dono:

1. ``read``   - o modelo extrai FATOS da mensagem da empresa (reading.py);
2. ``decide`` - a politica escolhe a jogada (policy.py), sem modelo;
3. ``write``  - o modelo redige a jogada decidida (writer.py), e o texto passa
   pelo lint e pela conferencia da jogada antes de sair.
"""
from __future__ import annotations

import re
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from src.llm import system_message, user_message
from src.negotiation_case_file import build_case_file
from src.negotiation_lint import (
    build_sender_correction_instruction,
    format_violations_for_log,
    lint_sender_message,
)
from src.negotiation_math import parse_brl
from src.pendencias_cliente import render_ja_solicitado, render_para_prompt as render_pendencias
from src.utils.anexos import render_anexos_para_prompt
from src.utils.client_rights import resolve_client_rights
from src.utils.veredito_documentos import render_veredito_para_prompt

from . import policy, writer
from .reading import CompanyTurnReading, ReadingError, build_reading_prompt

# Campos em que o backend pode mandar o minimo que o cliente aceita. Nenhum
# existe hoje; quando o intake passar a perguntar, basta o backend repassar.
FLOOR_KEYS = ("clientMinimumAcceptable", "minimumAcceptable", "minAcceptableValue",
              "valorMinimoAceito", "clientFloor")
TRACK_KEY = "negociacao"
# O backend espera ~120 s pela rodada inteira (leitura + redacao). Segunda
# tentativa e correcao so quando a primeira falhou rapido.
SECOND_TRY_WITHIN_S = 30


def _decimal(raw: Any) -> Optional[Decimal]:
    if raw in (None, "", 0, "0"):
        return None
    if isinstance(raw, str):
        value = parse_brl(raw.replace("R$", "").strip())
    else:
        value = policy.to_decimal(raw)
    return value if value is not None and value > 0 else None


def client_floor(context: Dict[str, Any]) -> Optional[Decimal]:
    """Minimo informado pelo cliente, se o backend mandou."""
    for holder in (context or {}, (context or {}).get("ticket") or {}, (context or {}).get("client") or {}):
        for key in FLOOR_KEYS:
            value = _decimal(holder.get(key)) if isinstance(holder, dict) else None
            if value is not None:
                return value
    return None


def _clean(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"^```\w*\s*|\s*```$", "", text).strip()
    return text.strip('"').strip()


class ZelinhuNegotiator:
    """Uma instancia por processo; o estado fica na sessao."""

    def __init__(self, llm, max_rounds: int = 6):
        self.llm = llm
        self.max_rounds = max_rounds

    # ------------------------------------------------------------------ dados
    @staticmethod
    def case(session: Dict[str, Any]):
        context = session.get("context") or {}
        return build_case_file(context, client_rights=resolve_client_rights(context))

    @staticmethod
    def track(session: Dict[str, Any]) -> policy.NegotiationTrack:
        return policy.NegotiationTrack.from_dict(session.get(TRACK_KEY))

    @staticmethod
    def save_track(session: Dict[str, Any], track: policy.NegotiationTrack) -> None:
        session[TRACK_KEY] = track.to_dict()

    # ------------------------------------------------------------------ 1. ler
    async def read(self, session: Dict[str, Any], company_message: str, *,
                   last_zelinhu_message: str = "", ledger=None) -> Dict[str, Any]:
        case = self.case(session)
        prompt = build_reading_prompt(
            company_message=company_message,
            last_zelinhu_message=last_zelinhu_message,
            case_block=case.render("cliente"),
            ledger_block=ledger.render() if ledger is not None else "",
            already_requested_block=render_ja_solicitado(session.get("solicitacoesFeitas") or []),
            documents_block="\n\n".join(b for b in (
                render_anexos_para_prompt(session.get("documentosRecebidos") or []),
                render_veredito_para_prompt(session.get("veredictosDocumentos") or []),
            ) if b),
        )
        structured = self.llm.with_structured_output(CompanyTurnReading)
        last_error: Optional[Exception] = None
        started = time.monotonic()
        for attempt in (1, 2):
            if attempt == 2 and time.monotonic() - started > SECOND_TRY_WITHIN_S:
                break  # sem tempo para outra tentativa dentro da janela do backend
            try:
                result = await structured.complete(prompt)
                return result.model_dump()
            except Exception as exc:  # noqa: BLE001
                last_error = exc
        raise ReadingError(f"leitura da mensagem da empresa indisponivel: {last_error}") from last_error

    # ---------------------------------------------------------------- 2. decidir
    def decide(self, session: Dict[str, Any], reading: Optional[Dict[str, Any]], round_number: int,
               round_cap: int, ledger=None) -> policy.Move:
        context = session.get("context") or {}
        case = self.case(session)
        track = self.track(session)
        reading = reading or {}
        target = case.case_value if case.case_value and case.case_value > 0 else None
        key = str(round_number)
        if key in track.decided:
            # Mesma rodada reprocessada: a jogada ja foi decidida e contada.
            return policy.Move.from_dict(track.decided[key])
        if not track.asks and target is not None:
            track.asks.append(target)  # a abertura pediu o valor cheio
        try:
            move = self._decide(track, context, target, reading, round_number, round_cap, ledger)
        except Exception as exc:  # noqa: BLE001 - erro aqui nunca vira concessao
            print(f"[ZELINHU] Erro na politica ({type(exc).__name__}: {exc}); mantendo o pedido")
            ask = track.last_ask if track.last_ask is not None else target
            if round_number >= round_cap:
                move = policy.Move(action=policy.TO_CLIENT, posture=policy.CLOSE_CAP,
                                   reason="erro na politica no teto", ask=ask, offer=track.best_offer())
            else:
                move = policy.Move(action=policy.COUNTER, posture=policy.HOLD,
                                   reason="erro na politica: manter pedido", ask=ask)
        track.decided[key] = move.to_dict()
        self.save_track(session, track)
        return move

    @staticmethod
    def reset_after_client_rejection(session: Dict[str, Any]) -> None:
        track = ZelinhuNegotiator.track(session)
        track.reset_after_client_rejection()
        ZelinhuNegotiator.save_track(session, track)

    def _decide(self, track, context, target, reading, round_number, round_cap, ledger) -> policy.Move:
        return policy.decide(
            target=target,
            floor=client_floor(context),
            track=track,
            offer=policy.to_decimal(reading.get("company_offer_total")),
            round_number=round_number,
            round_cap=round_cap,
            declared_final=bool(reading.get("declared_final")),
            refused_all=bool(reading.get("refused_all")),
            accepts_client_ask=bool(reading.get("accepts_client_ask")),
            open_items=len(ledger.open_items()) if ledger is not None else 0,
            settled_items=len(ledger.settled_items()) if ledger is not None else 0,
        )

    # ---------------------------------------------------------------- 3. redigir
    def _messages(self, session: Dict[str, Any], move: policy.Move, reading: Dict[str, Any],
                  extra: str = "") -> List[Dict[str, Any]]:
        context = session.get("context") or {}
        case = self.case(session)
        client = (context.get("client") or {}).get("name") or case.client_name or ""
        company = (context.get("company") or {}).get("name") or case.company_name or ""
        ledger_items = [i.description for i in self._ledger(session).open_items() if i.description]
        system = "\n\n".join(b for b in (
            writer.persona(client, company),
            case.render("cliente"),
            render_pendencias(session.get("solicitacoesFeitas") or []),
            render_anexos_para_prompt(session.get("documentosRecebidos") or []),
            render_veredito_para_prompt(session.get("veredictosDocumentos") or []),
            session.get("validation_report") or "",
        ) if b and b.strip())
        decision = writer.brief(
            move,
            offer_summary=(reading or {}).get("offer_summary") or "",
            open_items=ledger_items,
            company_arguments=(reading or {}).get("company_arguments") or [],
        )
        if extra:
            decision += "\n\n" + extra
        decision += "\n\nEscreva agora a proxima mensagem para a empresa. Responda apenas com o texto dela."
        return [system_message(system), *writer.history_turns(session.get("messages") or []),
                system_message(decision)]

    @staticmethod
    def _ledger(session: Dict[str, Any]):
        from src.negotiation_ledger import NegotiationLedger
        return NegotiationLedger.from_dict(session.get("ledger"))

    def _problems(self, text: str, move: policy.Move, session: Dict[str, Any]) -> Tuple[List[str], list]:
        context = session.get("context") or {}
        case = self.case(session)
        ticket = context.get("ticket") or {}
        lint = lint_sender_message(
            text,
            client_name=(context.get("client") or {}).get("name", ""),
            case_value=case.case_value,
            case_category=ticket.get("category", "") or "",
            case_description=ticket.get("description", "") or "",
        )
        return writer.check(text, move), lint

    async def write(self, session: Dict[str, Any], move: policy.Move,
                    reading: Optional[Dict[str, Any]] = None) -> str:
        """Texto da jogada. Uma correcao se o texto falhar na conferencia."""
        reading = reading or {}
        messages = self._messages(session, move, reading)
        started = time.monotonic()
        response = await self.llm.complete(messages)
        text = _clean(response.content)
        problems, lint = self._problems(text, move, session)
        if not problems and not lint:
            return text
        if time.monotonic() - started > SECOND_TRY_WITHIN_S:
            print(f"[ZELINHU] Sem tempo para correcao ({problems}); usando texto seguro")
            return writer.fallback(move, client_name=(session.get("context") or {}).get("client", {}).get("name", ""),
                                   offer_summary=reading.get("offer_summary") or "")
        print(f"[ZELINHU] Correcao: {problems} {format_violations_for_log(lint) if lint else ''}")
        correction = []
        if problems:
            correction.append("A mensagem anterior nao pode sair: " + "; ".join(problems) + ".")
        if lint:
            correction.append(build_sender_correction_instruction(
                lint, (session.get("context") or {}).get("client", {}).get("name", "")))
        retry = await self.llm.complete(
            messages + [{"role": "assistant", "content": text}, user_message("\n\n".join(correction))]
        )
        fixed = _clean(retry.content)
        remaining, remaining_lint = self._problems(fixed, move, session)
        if fixed and not remaining and not remaining_lint:
            return fixed
        print(f"[ZELINHU] Correcao nao resolveu {remaining}; usando texto seguro")
        return writer.fallback(move, client_name=(session.get("context") or {}).get("client", {}).get("name", ""),
                               offer_summary=reading.get("offer_summary") or "")

    async def opening(self, context: Dict[str, Any]) -> str:
        """Primeira mensagem do ZelinhU, a partir do chamado."""
        session = {"context": context, "messages": []}
        case = self.case(session)
        move = policy.opening_move(case.case_value if case.case_value and case.case_value > 0 else None,
                                   client_floor(context))
        try:
            return await self.write(session, move)
        except Exception as exc:  # noqa: BLE001
            print(f"[ZELINHU] Abertura pelo modelo indisponivel ({type(exc).__name__}); usando texto seguro")
            return writer.fallback(move, client_name=(context.get("client") or {}).get("name", ""))
