"""Normalize dynamic-form submissions from the ZELLU chat transport.

The production frontend has historically used two payload contracts:
1. structured ``messagedata.dynamic_form_response``; and
2. a plain chat message such as ``"Qual dessas unidades é a correta?: cmp_x"``.

This adapter accepts both while only consuming values that belong to the
currently pending form. It deliberately refuses arbitrary candidate ids so a
stale/forged chat message cannot select a company that was never offered.
"""
from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, Dict, Optional

from src.utils.company_normalizer import normalize_cnpj


_RESPONSE_META = {"form_name", "formName", "name", "values", "data", "fields", "type", "mode", "submit_label"}
_CANDIDATE_RE = re.compile(r"\bcmp_[A-Za-z0-9_-]+\b")
_CNPJ_RE = re.compile(r"(?:\d{2}[.\s-]?\d{3}[.\s-]?\d{3}[/\s-]?\d{4}[-\s]?\d{2}|\b\d{14}\b)")


def normalize_messagedata(raw: Any) -> Dict[str, Any]:
    """Return a canonical messagedata dict, accepting camel/snake aliases."""
    if not isinstance(raw, dict):
        return {}
    data = dict(raw)
    response = data.get("dynamic_form_response")
    if response is None:
        response = data.get("dynamicFormResponse") or data.get("form_response") or data.get("formResponse")
    if isinstance(response, str) and response.strip()[:1] == "{":
        try:
            response = json.loads(response)  # some clients stringify the form
        except ValueError:
            pass
    if isinstance(response, dict):
        normalized = dict(response)
        if not normalized.get("form_name"):
            normalized["form_name"] = normalized.get("formName") or normalized.get("name")
        values = normalized.get("values")
        if not isinstance(values, dict):
            values = normalized.get("data") if isinstance(normalized.get("data"), dict) else normalized.get("fields")
        if not isinstance(values, dict):
            # Field values sent beside form_name instead of under "values".
            values = {key: value for key, value in normalized.items()
                      if key not in _RESPONSE_META and isinstance(value, (str, int, float, bool))}
        normalized["values"] = values
        data["dynamic_form_response"] = normalized
    return data


def option_value_from_label(state: Dict[str, Any], field_name: str, text: Any) -> Optional[str]:
    """Value of the pending form's option whose visible label is `text`."""
    return _option_from_label(state, field_name, str(text or ""))


def _pending_options(state: Dict[str, Any]) -> Dict[str, set[str]]:
    snapshot = state.get("pending_form_snapshot") or state.get("pending_form_event_type") or {}
    out: Dict[str, set[str]] = {}
    if not isinstance(snapshot, dict):
        return out
    for field in snapshot.get("fields") or []:
        if not isinstance(field, dict) or not field.get("name"):
            continue
        values = set()
        for option in field.get("options") or []:
            if isinstance(option, dict) and option.get("value") is not None:
                values.add(str(option["value"]))
        out[str(field["name"])] = values
    return out


def _plain(text: Any) -> str:
    text = unicodedata.normalize("NFKD", str(text or ""))
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text)).strip()


def _option_from_label(state: Dict[str, Any], field_name: str, text: str) -> Optional[str]:
    """Resolve a submit that carries the option's visible label instead of its value.

    Only labels of the form that is pending right now are considered, and the
    match must point to exactly one option (the longest label wins when one
    label contains another).
    """
    snapshot = state.get("pending_form_snapshot") or state.get("pending_form_event_type") or {}
    if not isinstance(snapshot, dict):
        return None
    said = _plain(text)
    # "<pergunta>?: <resposta>" do renderer legado: a resposta e o que vem depois.
    answer = _plain(str(text or "").rsplit("?:", 1)[-1])
    hits = []
    for field in snapshot.get("fields") or []:
        if not isinstance(field, dict) or field.get("name") != field_name:
            continue
        for option in field.get("options") or []:
            if not isinstance(option, dict) or option.get("value") is None:
                continue
            label = _plain(option.get("label"))
            if not label:
                continue
            # Rotulo curto ("Sim", "Quero corrigir") so vale como a resposta inteira:
            # "ele disse que sim e depois negou" e relato, nao confirmacao.
            if answer == label or (len(label.split()) >= 4
                                   and re.search(r"(?:^| )" + re.escape(label) + r"(?:$| )", said)):
                hits.append((len(label), str(option["value"])))
    if not hits:
        return None
    longest = max(size for size, _ in hits)
    values = {value for size, value in hits if size == longest}
    return values.pop() if len(values) == 1 else None


def _pending_fields(state: Dict[str, Any], pending: str) -> list:
    """(name, label) dos campos do formulario pendente."""
    snapshot = state.get("pending_form_snapshot") or state.get("pending_form_event_type") or {}
    fields = [(str(f["name"]), str(f.get("label") or "")) for f in (snapshot.get("fields") or [])
              if isinstance(f, dict) and f.get("name")] if isinstance(snapshot, dict) else []
    if not fields and pending == "client_details":
        # O form de dados pessoais nao guarda snapshot em sessoes antigas.
        from src.services.client_intake_record import PERSONAL, LABELS
        fields = list(zip(PERSONAL, LABELS))
    return fields


def _values_from_labelled_lines(state: Dict[str, Any], pending: str, text: str) -> Optional[Dict[str, str]]:
    """Renderer legado de formulario com varios campos: uma linha "Rotulo: valor" por campo."""
    fields = [(name, _plain(label)) for name, label in _pending_fields(state, pending) if label]
    if len(fields) < 2:
        return None
    values: Dict[str, str] = {}
    for line in re.split(r"[\r\n]+", text):
        label, separator, value = line.partition(":")
        if not separator:
            continue
        label = _plain(label)
        for name, expected in fields:
            if label == expected and name not in values:
                values[name] = value.strip()
    # Duas linhas reconhecidas ja mostram que e o envio do formulario, nao conversa.
    return values if len(values) >= 2 else None


def _offered_company_ids(state: Dict[str, Any]) -> set[str]:
    offered: set[str] = {"none"}
    for company in state.get("company_search_results") or []:
        if not isinstance(company, dict):
            continue
        for key in ("candidate_id", "id", "company_id", "companyId"):
            if company.get(key):
                offered.add(str(company[key]))
        cnpj = normalize_cnpj(company.get("cnpj"))
        if cnpj:
            offered.add(cnpj)
    return offered


def _extract_single_option(text: str, values: set[str]) -> Optional[str]:
    if not text or not values:
        return None
    lowered = text.strip().lower()
    exact = [v for v in values if lowered == v.lower()]
    if len(exact) == 1:
        return exact[0]
    # Frontend legacy renderer usually serializes "<label>: <value>".
    # Short values ("no", "yes") are also ordinary words - "loja no shopping" -
    # so they only count at the very end of the message.
    def found(value: str) -> bool:
        if len(value) < 4:
            return bool(re.search(r"(?:^|:\s*)" + re.escape(value) + r"\s*$", text, re.I))
        return bool(re.search(r"(?<![A-Za-z0-9_-])" + re.escape(value) + r"(?![A-Za-z0-9_-])", text, re.I))
    hits = [v for v in values if found(v)]
    return hits[0] if len(hits) == 1 else None


def _infer_from_plain_text(state: Dict[str, Any], body_message: str) -> Optional[Dict[str, Any]]:
    pending = str(state.get("pending_form") or "").strip()
    text = str(body_message or "").strip()
    if not pending or not text:
        return None

    # A JSON string is also accepted for older clients that stringified the form.
    if text[:1] in "[{":
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                nested = normalize_messagedata(parsed)
                response = nested.get("dynamic_form_response")
                if isinstance(response, dict):
                    return response
        except Exception:
            pass

    if pending == "company_select":
        offered = _offered_company_ids(state)
        match = _CANDIDATE_RE.search(text)
        if match and match.group(0) in offered:
            return {"form_name": pending, "values": {"selected_company_id": match.group(0)}}
        cnpj_match = _CNPJ_RE.search(text)
        if cnpj_match:
            cnpj = normalize_cnpj(cnpj_match.group(0))
            if cnpj in offered:
                return {"form_name": pending, "values": {"selected_company_id": cnpj}}
        # Renderers that submit the visible label ("Riachuelo - Centro - Campinas/SP").
        by_label = _option_from_label(state, "selected_company_id", text)
        if by_label in offered:
            return {"form_name": pending, "values": {"selected_company_id": by_label}}
        # "nenhuma dessas" como resposta; "não tenho nenhuma nota fiscal" e relato.
        answer = _plain(text.rsplit("?:", 1)[-1])
        if re.match(r"(nenhuma|nenhum)( dessas| desses| delas| deles| das opcoes| das lojas)?$", answer):
            return {"form_name": pending, "values": {"selected_company_id": "none"}}
        return None

    if pending == "company_refine":
        # The form asks one thing - where the unit is. A typed answer is that answer.
        if text.startswith("[") or len(text) > 120:
            return None
        location = text.rsplit("?:", 1)[-1].strip()  # "<pergunta>?: <resposta>" do renderer legado
        return {"form_name": pending, "values": {"company_location": location}} if location else None

    labelled = _values_from_labelled_lines(state, pending, text)
    if labelled is not None:
        return {"form_name": pending, "values": labelled}

    options = _pending_options(state)
    if len(options) == 1:
        field_name, values = next(iter(options.items()))
        # The visible label is the strongest evidence; raw values come second.
        selected = _option_from_label(state, field_name, text) or _extract_single_option(text, values)
        if selected is not None:
            return {"form_name": pending, "values": {field_name: selected}}

    # Explicit compatibility for yes/no forms even if the UI schema snapshot
    # came from a session created before this bridge existed.
    normalized = text.lower().strip()
    answer = normalized.rsplit("?:", 1)[-1].strip()  # drop the question of "<pergunta>?: <resposta>"
    # Only a bare yes/no counts: "sim, mas é outra unidade" is not a confirmation.
    yes = bool(re.fullmatch(r"(yes|sim)[\s.,!]*", answer))
    no = bool(re.fullmatch(r"(no|nao|não)[\s.,!]*", answer))
    if pending == "company_confirm" and yes != no:
        return {"form_name": pending, "values": {"company_confirmed": "yes" if yes else "no"}}
    if pending == "case_confirmation" and yes != no:
        return {"form_name": pending, "values": {"case_confirmed": "yes" if yes else "no"}}

    if pending == "cnpj_input":
        match = _CNPJ_RE.search(text)
        if match:
            return {"form_name": pending, "values": {"opposing_party_cnpj": match.group(0)}}

    return None


def extract_dynamic_form_response(
    state: Dict[str, Any],
    messagedata: Any,
    body_message: str,
) -> Optional[Dict[str, Any]]:
    """Resolve the current form response from structured or legacy transport."""
    data = normalize_messagedata(messagedata)
    response = data.get("dynamic_form_response")
    pending = str(state.get("pending_form") or "").strip()
    if isinstance(response, dict):
        form_name = response.get("form_name")
        values = response.get("values") if isinstance(response.get("values"), dict) else {}
        # A submit without form_name can only be for the form on screen; one that
        # names another form is stale and is returned as-is so the caller rejects it.
        if pending and (not form_name or form_name == pending):
            return {"form_name": pending, "values": values}
        return {**response, "values": values}
    return _infer_from_plain_text(state, body_message)
