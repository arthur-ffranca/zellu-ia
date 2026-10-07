
"""
zellu_intake_flow_prototype.py

Protótipo para o chat inicial da ZELLU.

Fluxo obrigatório:
1) relato livre + prejuízo + data/período
2) empresa/localização
3) dropdown para o usuário selecionar a empresa
4) anexos obrigatórios, com auditoria documental
5) confirmação curta do caso
6) Pinecone (somente leis) -> JEV -> GPT-5 se houver incerteza
"""

from __future__ import annotations

import json
import os
from enum import Enum
from typing import Any, Literal

import httpx
from dotenv import dotenv_values, load_dotenv
from openai import AsyncOpenAI
from pydantic import BaseModel, Field


# ============================================================
# CONFIG
# ============================================================

from config import get_settings, llm_default_headers
from src.utils.step_timing import timed
from functools import lru_cache

def env(*names, default=None):
    settings = get_settings()
    for name in names:
        value = os.getenv(name) or getattr(settings, name, None)
        if not value and name == 'zellu-typesafe-ia':
            from config import BASE_DIR
            value = dotenv_values(BASE_DIR / '.env').get(name)
        if value:
            return value
    return default

OPENAI_MODEL = env("INTAKE_CONVERSATION_MODEL", default="gpt-5-mini")
# Latencia: gpt-5 sem effort explicito roda em "medium" (10-30s por turno).
# Intake e extracao + resposta curta: "minimal" basta. Reversivel via env.
INTAKE_REASONING_EFFORT = env("INTAKE_REASONING_EFFORT", default="minimal")
INTAKE_VERBOSITY = env("INTAKE_VERBOSITY", default="low")
JEV_TIMEOUT_S = float(env("JEV_TIMEOUT_S", default=12))
LEGAL_FALLBACK_EFFORT = env("LEGAL_FALLBACK_EFFORT", default="low")
JEV_API_URL = env("JEV_API_URL", default="https://api.typesafe.ai/v1/systemone")
JEV_MODEL = env("JEV_MODEL", default="jev-latest")
JEV_CONFIDENCE_THRESHOLD = float(env("JEV_CONFIDENCE_THRESHOLD", default=0.70))
JEV_MARGIN_THRESHOLD = float(env("JEV_MARGIN_THRESHOLD", default=0.50))

class LazyOpenAI:
    @property
    def responses(self):
        return self.client().responses

    @staticmethod
    @lru_cache(maxsize=1)
    def client():
        return AsyncOpenAI(default_headers=llm_default_headers(), api_key=get_settings().OPENAI_API_KEY, timeout=45.0, max_retries=1)

openai_client = LazyOpenAI()

# ============================================================
# STATE
# ============================================================

class Stage(str, Enum):
    FREE_NARRATIVE = "FREE_NARRATIVE"
    COMPANY_DISCOVERY = "COMPANY_DISCOVERY"
    COMPANY_SELECTION = "COMPANY_SELECTION"
    ATTACHMENTS = "ATTACHMENTS"
    CASE_CONFIRMATION = "CASE_CONFIRMATION"
    LEGAL_ANALYSIS = "LEGAL_ANALYSIS"
    COMPLETE = "COMPLETE"


class CompanyCandidate(BaseModel):
    id: str
    display_name: str
    cnpj: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None


class SelectedCompany(BaseModel):
    id: str
    display_name: str
    cnpj: str | None = None
    address: str | None = None


class AttachmentAudit(BaseModel):
    document_id: str
    filename: str
    audit_status: str = "PROCESSING"
    risk_level: str | None = None
    document_type: str | None = None
    authenticity: str | None = None


class LegalDecision(BaseModel):
    chosen_article_id: str
    source: Literal["JEV", "GPT5_FALLBACK"]
    confidence: float | None = None
    margin: float | None = None
    candidates: list[dict[str, Any]] = Field(default_factory=list)


class IntakeState(BaseModel):
    stage: Stage = Stage.FREE_NARRATIVE

    # Gate 1
    problem_description: str | None = None
    loss_amount_raw: str | None = None
    loss_amount_brl: float | None = None
    incident_date_raw: str | None = None

    # Gate 2/3
    company_name_hint: str | None = None
    location_hint: str | None = None
    company_candidates: list[CompanyCandidate] = Field(default_factory=list)
    selected_company: SelectedCompany | None = None

    # Gate 4
    attachments: list[AttachmentAudit] = Field(default_factory=list)

    # Memória operacional
    case_summary: str | None = None
    facts: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    user_goal: str | None = None

    case_confirmed: bool = False
    legal_decision: LegalDecision | None = None


ActionName = Literal[
    "none",
    "search_company",
    "show_company_dropdown",
    "request_attachments",
    "confirm_case",
    "run_legal_analysis",
    "complete",
]


class TurnResult(BaseModel):
    message: str
    stage: Stage
    action: ActionName = "none"
    action_payload: dict[str, Any] = Field(default_factory=dict)
    state: IntakeState


# ============================================================
# DETERMINISTIC FLOW
# ============================================================

def narrative_missing(state: IntakeState) -> list[str]:
    missing = []
    if not state.problem_description:
        missing.append("problem_description")
    if not state.loss_amount_raw and state.loss_amount_brl is None:
        missing.append("loss_amount")
    if not state.incident_date_raw:
        missing.append("incident_date")
    return missing


def company_context_missing(state: IntakeState) -> list[str]:
    """Empresa e contexto da unidade/canal sao requisitos distintos.

    O nome da empresa e obrigatorio. O segundo campo serve para localizar a
    filial correta (shopping/bairro/cidade/referencia) ou indicar o canal
    digital (site/app/marketplace). Localizacao nunca substitui a empresa.
    """
    missing = []
    if not (state.company_name_hint or "").strip():
        missing.append("company_name")
    if not (state.location_hint or "").strip():
        missing.append("company_location_or_channel")
    return missing


def company_search_query(state: IntakeState) -> str | None:
    if company_context_missing(state):
        return None
    parts = [state.company_name_hint, state.location_hint]
    return " | ".join(p.strip() for p in parts if p and p.strip())


def attachment_gate_passed(state: IntakeState) -> bool:
    if not state.attachments or any(a.audit_status.upper() in {"PROCESSING", "PENDING"} for a in state.attachments):
        return False

    finished = [
        a
        for a in state.attachments
        if a.audit_status.upper() not in {"PROCESSING", "PENDING"}
    ]
    if not finished:
        return False

    return any(
        a.audit_status.upper() not in {"MALICIOUS", "BLOCKED", "UNREAD", "ERROR", "FAILED", "UNVERIFIED"}
        for a in finished
    )


def compute_stage(state: IntakeState) -> Stage:
    if narrative_missing(state):
        return Stage.FREE_NARRATIVE
    if company_context_missing(state):
        return Stage.COMPANY_DISCOVERY
    if state.selected_company is None:
        return Stage.COMPANY_SELECTION
    if not attachment_gate_passed(state):
        return Stage.ATTACHMENTS
    if not state.case_confirmed:
        return Stage.CASE_CONFIRMATION
    if state.legal_decision is None:
        return Stage.LEGAL_ANALYSIS
    return Stage.COMPLETE


def action_for_state(state: IntakeState) -> tuple[ActionName, dict[str, Any]]:
    stage = compute_stage(state)

    if stage == Stage.FREE_NARRATIVE:
        return "none", {"missing": narrative_missing(state)}

    if stage == Stage.COMPANY_DISCOVERY:
        return "none", {"missing": company_context_missing(state)}

    if stage == Stage.COMPANY_SELECTION:
        if not state.company_candidates:
            return "search_company", {"query": company_search_query(state)}
        return "show_company_dropdown", {
            "companies": [c.model_dump() for c in state.company_candidates]
        }

    if stage == Stage.ATTACHMENTS:
        return "request_attachments", {
            "required": True,
            "accepted_examples": [
                "nota fiscal",
                "comprovante de pagamento",
                "contrato",
                "orçamento",
                "laudo",
                "prints",
                "fotos",
                "e-mails",
            ],
        }

    if stage == Stage.CASE_CONFIRMATION:
        return "confirm_case", {}

    if stage == Stage.LEGAL_ANALYSIS:
        return "run_legal_analysis", {}

    return "complete", {}


# ============================================================
# GPT-5 CONVERSATIONAL LAYER
# ============================================================

SYSTEM_PROMPT = """
Você é o agente conversacional de intake da ZELLU.

Sua função é entender o relato da pessoa e conduzir a conversa de forma humana,
curta e profissional. O fluxo é obrigatório, mas as perguntas não são fixas.

REGRAS PRINCIPAIS
- Cada pessoa conta sua história de um jeito diferente.
- Aproveite tudo o que ela disser espontaneamente.
- Nunca pergunte algo já informado.
- Faça no máximo UMA pergunta principal por mensagem.
- Não transforme a conversa em formulário.
- Não faça listas de perguntas.
- Demonstre que entendeu usando o conteúdo do relato.
- Evite frases vazias como "compreendo sua situação", "para prosseguirmos",
  "poderia informar" e "agradeço pelas informações".
- Não invente fatos, datas, valores, empresas, documentos ou intenções.
- Não faça parecer jurídico nem prometa resultado durante a coleta.

POSTURA SUGESTIVA (OBRIGATÓRIA)
Você não é só um coletor de dados: ajude de verdade. Quando o relato permitir,
inclua UMA dica prática, curta e específica para o caso, antes da pergunta.
Exemplos do tipo de dica (adapte ao caso, nunca genérica):
- Guardar nota fiscal, comprovante de pagamento, fatura ou extrato.
- Fazer print de conversas, anúncios, e-mails e do pedido antes que sumam.
- Anotar número de protocolo, data e nome do atendente de cada contato.
- Não apagar mensagens nem devolver o produto sem registro.
- Compra online: há direito de arrependimento em 7 dias (CDC art. 49).
- Produto com defeito: o fornecedor tem 30 dias para resolver (CDC art. 18).
- Cobrança indevida paga pode gerar devolução em dobro (CDC art. 42).
- Negativação indevida: guardar a consulta ao Serasa/SPC.
- Lesão ou dano físico: guardar laudos, receitas e fotos datadas.
Regras da dica: no máximo uma por mensagem; só cite lei quando tiver certeza
de que se aplica; não repita dica já dada nas recent_messages; não dê dica
se a pessoa só respondeu algo burocrático (sim/não, nome da loja).
Se a pessoa demonstrar dúvida ("o que eu faço?", "tenho direito?"), oriente
de forma prática e diga que a Zellu vai analisar o caso completo em seguida.

ETAPA 1: RELATO LIVRE
Precisamos obter naturalmente: o que aconteceu, valor aproximado do prejuízo e
data/período aproximado. A pessoa pode fornecer tudo em qualquer ordem.
Pergunte somente o que realmente faltar.

ETAPA 2: EMPRESA + UNIDADE/CANAL
O nome da empresa/loja é obrigatório. Depois precisamos saber ONDE aconteceu
para localizar a filial correta: shopping, bairro, cidade, rua, unidade ou um
ponto de referência. Se foi online, o contexto é site, app ou marketplace.
Localização nunca substitui o nome da empresa. Não exija CNPJ do cliente.
Se a pessoa já forneceu empresa e local/canal na mesma mensagem, não pergunte de novo.

ETAPA 3: SELEÇÃO
O sistema mostrará um dropdown. Nunca escolha a empresa pela pessoa.

ETAPA 4: ANEXOS
Pelo menos um anexo é obrigatório. Outro componente faz a auditoria do arquivo.
Não declare autenticidade só porque um arquivo foi enviado.

ETAPA 5: CONFIRMAÇÃO
Faça um resumo curto dos fatos essenciais e pergunte se está correto.

ESTILO
Natural, acolhedor, direto e atento. Frases curtas. Sem juridiquês na coleta.
O fluxo pertence ao sistema. A experiência pertence ao cliente.
A pessoa nunca deve sentir que está preenchendo um formulário.
Primeiro reconheça o conteúdo concreto do que ela acabou de contar; depois faça
somente a próxima pergunta necessária. Seja simpático sem falsa empatia.
Quando houver diferença em documento, trate como algo a esclarecer, nunca como
mentira, fraude ou má-fé. Acolha a explicação, registre e siga.
""".strip()


TURN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "reply": {"type": "string"},
        "problem_description": {"type": ["string", "null"]},
        "loss_amount_raw": {"type": ["string", "null"]},
        "loss_amount_brl": {"type": ["number", "null"]},
        "incident_date_raw": {"type": ["string", "null"]},
        "company_name_hint": {"type": ["string", "null"]},
        "location_hint": {"type": ["string", "null"]},
        "user_goal": {"type": ["string", "null"]},
        "case_summary": {"type": ["string", "null"]},
        "facts": {"type": "array", "items": {"type": "string"}},
        "uncertainties": {"type": "array", "items": {"type": "string"}},
        "contradictions": {"type": "array", "items": {"type": "string"}},
        "case_confirmation": {
            "type": "string",
            "enum": ["YES", "NO", "UNKNOWN"],
        },
    },
    "required": [
        "reply",
        "problem_description",
        "loss_amount_raw",
        "loss_amount_brl",
        "incident_date_raw",
        "company_name_hint",
        "location_hint",
        "user_goal",
        "case_summary",
        "facts",
        "uncertainties",
        "contradictions",
        "case_confirmation",
    ],
}


def merge_unique(old: list[str], new: list[str], limit: int | None = None) -> list[str]:
    output, seen = [], set()
    for item in [*old, *new]:
        item = item.strip()
        key = item.casefold()
        if not item or key in seen:
            continue
        seen.add(key)
        output.append(item)
        if limit is not None and len(output) >= limit:
            break
    return output


async def understand_turn(
    state: IntakeState,
    user_message: str,
    recent_messages: list[dict[str, str]] | None = None,
) -> tuple[IntakeState, str]:
    """Uma chamada: extrai o delta + produz a resposta natural."""

    compact_state = {
        "stage": compute_stage(state).value,
        "problem_description": state.case_summary,
        "loss_amount_raw": state.loss_amount_raw,
        "loss_amount_brl": state.loss_amount_brl,
        "incident_date_raw": state.incident_date_raw,
        "company_name_hint": state.company_name_hint,
        "location_hint": state.location_hint,
        "selected_company": (
            state.selected_company.model_dump()
            if state.selected_company
            else None
        ),
        "attachments": [
            {
                "filename": a.filename,
                "audit_status": a.audit_status,
                "document_type": a.document_type,
            }
            for a in state.attachments[-5:]
        ],
        "case_summary": state.case_summary,
        "facts": state.facts,
        "uncertainties": state.uncertainties[-10:],
        "contradictions": state.contradictions[-10:],
        "user_goal": state.user_goal,
        "case_confirmed": state.case_confirmed,
    }

    payload = {
        "temporal_reference": __import__('src.utils.intake_dates', fromlist=['temporal_prompt']).temporal_prompt({}),
        "current_state": compact_state,
        "recent_messages": (recent_messages or [])[-6:],
        "new_user_message": user_message,
        "instruction": (
            "Extraia somente informação suportada pela mensagem nova. "
            "Retorne null quando não houver atualização de um campo. "
            "A resposta deve ter no máximo uma pergunta principal. "
            "Quando couber, inclua antes uma dica prática específica para o caso."
        ),
    }

    with timed('llm.intake_turn', model=OPENAI_MODEL, effort=INTAKE_REASONING_EFFORT):
      response = await openai_client.responses.create(
        model=OPENAI_MODEL,
        instructions=SYSTEM_PROMPT,
        input=json.dumps(payload, ensure_ascii=False),
        **({"reasoning": {"effort": INTAKE_REASONING_EFFORT}}
           if OPENAI_MODEL.startswith(("gpt-5", "o")) and INTAKE_REASONING_EFFORT else {}),
        text={
            **({"verbosity": INTAKE_VERBOSITY} if OPENAI_MODEL.startswith("gpt-5") else {}),
            "format": {
                "type": "json_schema",
                "name": "zellu_intake_turn",
                "strict": True,
                "schema": TURN_SCHEMA,
            }
        },
    )

    data = json.loads(response.output_text)
    updated = state.model_copy(deep=True)

    for field_name in [
        "problem_description",
        "loss_amount_raw",
        "loss_amount_brl",
        "incident_date_raw",
        "company_name_hint",
        "location_hint",
        "user_goal",
        "case_summary",
    ]:
        value = data.get(field_name)
        if value is not None and value != "":
            setattr(updated, field_name, value)

    updated.facts = merge_unique(updated.facts, data.get("facts") or [])
    updated.uncertainties = merge_unique(
        updated.uncertainties, data.get("uncertainties") or []
    )
    updated.contradictions = merge_unique(
        updated.contradictions, data.get("contradictions") or []
    )

    if compute_stage(updated) == Stage.CASE_CONFIRMATION:
        if data.get("case_confirmation") == "YES":
            updated.case_confirmed = True
        elif data.get("case_confirmation") == "NO":
            updated.case_confirmed = False

    updated.stage = compute_stage(updated)
    return updated, data["reply"].strip()


async def process_chat_turn(
    state: IntakeState,
    user_message: str,
    recent_messages: list[dict[str, str]] | None = None,
) -> TurnResult:
    updated, llm_reply = await understand_turn(
        state,
        user_message,
        recent_messages,
    )
    action, payload = action_for_state(updated)

    if action == "show_company_dropdown":
        message = (
            "Encontrei algumas opções. Selecione abaixo a empresa relacionada "
            "ao seu caso."
        )
    elif action == "request_attachments":
        message = (
            "Perfeito. Agora preciso de pelo menos um documento relacionado ao "
            "caso, como nota fiscal, comprovante, contrato, print, foto ou laudo. "
            "O arquivo passa por uma verificação de segurança antes de ser usado."
        )
    elif action == "run_legal_analysis":
        message = (
            "Perfeito. Com esses pontos confirmados, já tenho o necessário "
            "para analisar o enquadramento jurídico."
        )
    elif action == "complete":
        message = (
            "Certo. O caso já tem as informações essenciais, a empresa "
            "confirmada e os anexos registrados."
        )
    else:
        message = llm_reply

    updated.stage = compute_stage(updated)

    return TurnResult(
        message=message,
        stage=updated.stage,
        action=action,
        action_payload=payload,
        state=updated,
    )


# ============================================================
# COMPANY HOOKS
# ============================================================

def apply_company_candidates(
    state: IntakeState,
    raw_candidates: list[dict[str, Any]],
) -> TurnResult:
    """Codex deve ligar isto ao CompanySearchClient já existente."""

    updated = state.model_copy(deep=True)
    updated.company_candidates = [
        CompanyCandidate(
            id=str(item.get("id") or item.get("company_id") or ""),
            display_name=str(
                item.get("tradeName")
                or item.get("trade_name")
                or item.get("companyName")
                or item.get("company_name")
                or item.get("name")
                or "Empresa"
            ),
            cnpj=item.get("cnpj"),
            address=item.get("address"),
            city=item.get("city"),
            state=item.get("state"),
        )
        for item in raw_candidates
        if item.get("id") or item.get("company_id")
    ]

    updated.stage = compute_stage(updated)

    if updated.company_candidates:
        return TurnResult(
            message=(
                "Encontrei algumas opções. Selecione a empresa relacionada "
                "ao seu caso."
            ),
            stage=updated.stage,
            action="show_company_dropdown",
            action_payload={
                "companies": [c.model_dump() for c in updated.company_candidates]
            },
            state=updated,
        )

    return TurnResult(
        message=(
            "Não encontrei uma opção segura ainda. Você lembra de mais alguma "
            "referência, como nome da loja, shopping, bairro, rua ou cidade?"
        ),
        stage=Stage.COMPANY_DISCOVERY,
        action="none",
        action_payload={"missing": ["better_company_reference"]},
        state=updated,
    )


def select_company(state: IntakeState, company_id: str) -> TurnResult:
    """A empresa é escolhida pelo usuário no dropdown, nunca pelo LLM."""

    updated = state.model_copy(deep=True)
    candidate = next(
        (c for c in updated.company_candidates if c.id == company_id),
        None,
    )

    if candidate is None:
        raise ValueError(
            "Empresa selecionada não existe nos candidatos da sessão"
        )

    updated.selected_company = SelectedCompany(
        id=candidate.id,
        display_name=candidate.display_name,
        cnpj=candidate.cnpj,
        address=candidate.address,
    )
    updated.stage = compute_stage(updated)
    action, payload = action_for_state(updated)

    return TurnResult(
        message=(
            "Perfeito. Agora preciso de pelo menos um documento relacionado ao "
            "caso, como nota fiscal, comprovante, contrato, print, foto ou laudo."
        ),
        stage=updated.stage,
        action=action,
        action_payload=payload,
        state=updated,
    )


# ============================================================
# ATTACHMENT HOOK
# ============================================================

def apply_attachment_audit(
    state: IntakeState,
    audit: dict[str, Any],
) -> TurnResult:
    """
    Recebe o resultado do auditor dos bytes do upload do chat.
    Documentos do cliente NÃO entram no RAG.
    """

    updated = state.model_copy(deep=True)
    attachment = AttachmentAudit(
        document_id=str(
            audit.get("document_id")
            or audit.get("documentId")
            or ""
        ),
        filename=str(
            audit.get("filename")
            or audit.get("file_name")
            or "documento"
        ),
        audit_status=str(
            audit.get("status")
            or audit.get("auditStatus")
            or "UNVERIFIED"
        ),
        risk_level=(
            audit.get("risk_level")
            or audit.get("riskLevel")
        ),
        document_type=(
            audit.get("document_type")
            or audit.get("documentType")
        ),
        authenticity=audit.get("authenticity"),
    )

    updated.attachments = [
        a
        for a in updated.attachments
        if a.document_id != attachment.document_id
    ]
    updated.attachments.append(attachment)
    updated.stage = compute_stage(updated)
    action, payload = action_for_state(updated)

    if attachment.audit_status.upper() in {"MALICIOUS", "BLOCKED"}:
        message = (
            f"O arquivo {attachment.filename} foi bloqueado pela verificação "
            "de segurança. Envie outro documento relacionado ao caso."
        )
    elif updated.stage == Stage.CASE_CONFIRMATION:
        message = (
            "Ótimo. O documento foi registrado. Agora vou confirmar os "
            "pontos essenciais do caso com você."
        )
    else:
        message = (
            f"Recebi {attachment.filename}. A verificação precisa terminar "
            "antes de avançarmos."
        )

    return TurnResult(
        message=message,
        stage=updated.stage,
        action=action,
        action_payload=payload,
        state=updated,
    )


# ============================================================
# LEGAL RAG: PINECONE SOMENTE PARA LEIS
# ============================================================

def metadata_text(metadata: dict[str, Any]) -> str:
    for key in (
        "text",
        "content",
        "section_text",
        "conteudo",
        "chunk",
        "body",
    ):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def metadata_title(metadata: dict[str, Any]) -> str:
    for key in (
        "title",
        "section",
        "heading",
        "titulo",
        "name",
    ):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def article_id(match: Any) -> str:
    metadata = match.metadata or {}
    for key in (
        "article_id",
        "artigo_id",
        "id",
        "article",
        "codigo",
    ):
        value = metadata.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return str(match.id)


# ============================================================
# JEV -> GPT-5 FALLBACK
# ============================================================

def probability_margin(
    probabilities: dict[str, float],
) -> float:
    values = sorted(
        (float(v) for v in probabilities.values()),
        reverse=True,
    )

    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]

    return values[0] - values[1]


async def choose_with_jev(
    state: IntakeState,
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    key = env("JEV_API_KEY", "zellu-typesafe-ia")
    if not key:
        raise RuntimeError("JEV_API_KEY não configurada")

    key_to_article = {
        f"C{i}": c["article_id"]
        for i, c in enumerate(candidates)
    }

    criteria = {
        f"C{i}": (
            f"Artigo: {c['article_id']}\n"
            f"Título: {c.get('title') or ''}\n"
            f"Texto: {c['text']}"
        )
        for i, c in enumerate(candidates)
    }

    payload = {
        "model": JEV_MODEL,
        "state": {
            "summary": state.case_summary,
            "problem": state.problem_description,
            "loss": state.loss_amount_raw,
            "date": state.incident_date_raw,
            "company": (
                state.selected_company.display_name
                if state.selected_company
                else None
            ),
            "goal": state.user_goal,
        },
        "questions": {
            "best_article": {
                "type": "choice",
                "instructions": (
                    "Selecione o artigo jurídico candidato que melhor "
                    "corresponde aos fatos confirmados. Julgue somente os "
                    "candidatos fornecidos e priorize aderência jurídica, "
                    "não mera similaridade lexical."
                ),
                "criteria": criteria,
            }
        },
    }

    with timed('jev.route', candidates=len(candidates)):
      async with httpx.AsyncClient(timeout=JEV_TIMEOUT_S) as client:
        response = await client.post(
            JEV_API_URL,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        response.raise_for_status()
        data = response.json()

    answer = data["answers"]["best_article"]
    choice_key = str(answer["choice"])

    raw_probs = {
        str(k): float(v)
        for k, v in (answer.get("probabilities") or {}).items()
    }

    if choice_key not in key_to_article:
        raise RuntimeError(
            f"JEV retornou escolha desconhecida: {choice_key}"
        )

    return {
        "chosen_article_id": key_to_article[choice_key],
        "confidence": float(answer.get("confidence") or 0.0),
        "margin": probability_margin(raw_probs),
    }


LEGAL_FALLBACK_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "chosen_article_id": {"type": "string"},
    },
    "required": ["chosen_article_id"],
}


async def choose_with_gpt5(
    state: IntakeState,
    candidates: list[dict[str, Any]],
) -> str:
    _t = timed('llm.legal_fallback', model=OPENAI_MODEL, candidates=len(candidates))
    _t.__enter__()
    response = await openai_client.responses.create(
        model=OPENAI_MODEL,
        **({"reasoning": {"effort": LEGAL_FALLBACK_EFFORT}}
           if OPENAI_MODEL.startswith(("gpt-5", "o")) and LEGAL_FALLBACK_EFFORT else {}),
        instructions=(
            "Você é a camada de decisão jurídica de um RAG. "
            "Escolha exatamente um article_id entre os candidatos. "
            "Não invente artigos e não produza parecer."
        ),
        input=json.dumps(
            {
                "case": {
                    "summary": state.case_summary,
                    "problem": state.problem_description,
                    "loss": state.loss_amount_raw,
                    "date": state.incident_date_raw,
                    "goal": state.user_goal,
                },
                "candidates": candidates,
            },
            ensure_ascii=False,
        ),
        text={
            "format": {
                "type": "json_schema",
                "name": "legal_article_choice",
                "strict": True,
                "schema": LEGAL_FALLBACK_SCHEMA,
            }
        },
    )

    _t.__exit__(None, None, None)
    chosen = str(
        json.loads(response.output_text)["chosen_article_id"]
    )

    valid_ids = {
        c["article_id"]
        for c in candidates
    }

    if chosen not in valid_ids:
        raise RuntimeError(
            f"GPT-5 retornou artigo fora dos candidatos: {chosen}"
        )

    return chosen
