"""Structured client intake; the application must acknowledge ticket persistence."""
import json
import re

from src.utils.phone import is_valid_phone, normalize_phone
from src.services.case_evidence import _case_key, _connect, evidence_metadata, attachment_references

PERSONAL = ("client_name", "client_phone", "client_email", "client_cpf")
LABELS = ("Nome completo", "Telefone com DDD", "E-mail", "CPF")


def valid_cpf(value):
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) != 11 or len(set(digits)) == 1:
        return False
    for length in (9, 10):
        check = (sum(int(digits[i]) * (length + 1 - i) for i in range(length)) * 10) % 11 % 10
        if check != int(digits[length]):
            return False
    return True


def client_details_required():
    """O chat pede os dados pessoais? Por padrao nao: a conta logada ja identifica o cliente."""
    from config import get_settings
    return bool(getattr(get_settings(), "INTAKE_CLIENT_DETAILS_FORM", False))


def invalid_personal_fields(state):
    """Rotulos dos dados pessoais que nao passam na validacao."""
    checks = {
        "client_name": len(str(state.get("client_name") or "").split()) >= 2,
        "client_phone": is_valid_phone(str(state.get("client_phone") or "")),
        "client_email": bool(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", str(state.get("client_email") or ""))),
        "client_cpf": valid_cpf(state.get("client_cpf")),
    }
    return [label for key, label in zip(PERSONAL, LABELS) if not checks[key]]


def valid_personal(state):
    return (len(str(state.get("client_name") or "").split()) >= 2
            and is_valid_phone(str(state.get("client_phone") or ""))
            and bool(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", str(state.get("client_email") or "")))
            and valid_cpf(state.get("client_cpf")))


def _documents_notice(state):
    from src.services.staged_intake_adapter import unread_notice
    return unread_notice(state)


def record_payload(state):
    documents = evidence_metadata(state, audience="internal")["files"]
    stored = any(item.get("original_stored") for item in documents)
    return {
        "schemaVersion": 1, "chatId": state.get("chat_id"), "userId": state.get("user_id"),
        "requestedStatus": "open", "description": state.get("problem_description"),
        "originalDescription": state.get("problem_description_original"),
        "dateMentions": state.get("case_date_mentions") or [],
        "dateReferenceTimestamp": state.get("intake_reference_timestamp"),
        "caseConfirmed": bool(state.get("case_confirmed")),
        "legalDecision": state.get("legal_decision"),
        "location": state.get("company_location_input") or state.get("problem_location_city"),
        "userInfo": {key.removeprefix("client_"): state.get(key) for key in PERSONAL},
        "personalDataConfirmed": bool(state.get("client_details_confirmed")),
        "company": state.get("selected_company_record") or {
            "name": state.get("opposing_party_name"), "cnpj": state.get("opposing_party_cnpj")},
        "documentsStatus": "storage_error" if state.get("evidence_storage_error") else "received" if stored else "awaiting_documents",
        "documents": documents,
        # Anexos recebidos e nao lidos: o caso abre mesmo assim e leva o pedido de
        # reenvio para a tela do caso (None quando tudo foi lido).
        "documentsNotice": _documents_notice(state),
        "attachments": attachment_references(state),
        "evidenceLedger": state.get("evidence_ledger") or {},
    }


def save_record(state):
    """Idempotent local snapshot, separate from confirmation by the site backend."""
    payload = record_payload(state)
    with _connect() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS client_intake (case_key TEXT PRIMARY KEY, record TEXT NOT NULL)")
        conn.execute("INSERT OR REPLACE INTO client_intake VALUES (?, ?)",
                     (_case_key(state), json.dumps(payload, ensure_ascii=False)))
    return payload


def intake_gate(state):
    """After company confirmation, collect identity and request evidence once."""
    if not state.get("company_confirmed") or not state.get("problem_description"):
        return False
    response = (state.get("incoming_messagedata") or {}).get("dynamic_form_response") or {}
    pending = state.get("pending_form")
    invalid_fields = []
    if pending == "client_details" and response.get("form_name") == pending:
        values = response.get("values") or {}
        candidate = {key: str(values.get(key) or "").strip() for key in PERSONAL}
        invalid_fields = invalid_personal_fields(candidate)
        if valid_personal(candidate):
            candidate["client_phone"] = normalize_phone(candidate["client_phone"])
            candidate["client_cpf"] = re.sub(r"\D", "", candidate["client_cpf"])
            candidate["client_email"] = candidate["client_email"].lower()
            state.update(candidate)
            state["client_details_confirmed"] = True
            state["pending_form"] = None
            state["pending_form_event_type"] = None
            state["incoming_messagedata"] = {}
    if not state.get("client_details_confirmed") or not valid_personal(state):
        state["pending_form"] = "client_details"
        state["pending_form_event_type"] = {
            "type": "form", "mode": "editable", "form_name": "client_details",
            "submit_label": "Confirmar dados",
            "fields": [{"name": key, "type": "text", "label": label, "required": True,
                        "value": state.get(key) or ""} for key, label in zip(PERSONAL, LABELS)],
        }
        state["pending_form_snapshot"] = state["pending_form_event_type"]
        message = "Confirme seu nome completo, telefone com DDD, e-mail e CPF para registrar no chamado."
        if response.get("form_name") == "client_details":
            # Dizer qual dado nao passou: sem isso o cliente reenvia o mesmo
            # formulario sem saber o que corrigir.
            wrong = ", ".join(invalid_fields) or "os dados"
            message = (f"Não consegui validar: {wrong}. Confira e envie de novo "
                       "(nome e sobrenome, telefone com DDD, e-mail e um CPF válido).")
    else:
        payload = save_record(state)  # fail closed: do not claim persistence after a write failure
        # Anexo entregue e nao baixado (falha nossa) nao prende o cliente aqui:
        # o registro continua "awaiting_documents" e o chat avisa o que ajustar.
        from src.services.staged_intake_adapter import attachment_gate
        if payload["documentsStatus"] == "received" or attachment_gate(state):
            state["documents_requested"] = True
            return False
        state["documents_requested"] = True
        # Opening the ticket in the other backend is not yet acknowledged here.
        message = ("Aguardando documentos para complementar o relato. Use o botão de anexos "
                   "para enviar fotos, laudos, B.O. ou outros comprovantes. "
                   "Seus dados e o relato foram registrados.")
    state["ready_for_classification"] = False
    state["validated"] = False
    state["current_agent"] = "intake"
    state.setdefault("messages", []).append({"role": "assistant", "content": message, "agent": "intake"})
    state["step"] = state.get("step", 0) + 1
    return True
