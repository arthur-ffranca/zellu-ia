"""
Main FastAPI application - Zellu IA Empresa.

Endpoints principais:
- POST /api/company/base-dados - Processa mensagens da Zellu
- POST /company_notify_knowledgebase_to_ia - Webhook: docs criados/deletados
- POST /company_notify_instructions_to_ia - Webhook: instructions atualizadas
- POST /company_notify_temporary_rules_to_ia - Webhook: regras temporarias

Clientes HTTP em src/clients/:
- ZelluClient: Envia respostas via webhook
- ZelluAPIClient: Busca config da API Zellu
"""

from fastapi import FastAPI, HTTPException, Request, BackgroundTasks, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from contextlib import asynccontextmanager
from uuid import uuid4
from datetime import datetime
from typing import Any, Dict, List, Optional
import os
import asyncio
import re
import hmac
import time
import unicodedata
from collections import defaultdict

from config import Settings
from api.schemas import HealthCheckResponse, MessageRequest, MessageResponse
from src.state_manager import StateManager
from src.upload_service import SafeUploadStaticFiles, UploadService
from src.rag_service import RAGService
from src.clients import ZelluClient, ZelluAPIClient
from src.minio_service import MinioService
from src.company_search_client import CompanySearchClient
from src.utils.company_normalizer import normalize_cnpj, is_valid_cnpj
from src.email_validation_client import get_email_validation_client
from src.pre_registration_client import get_pre_registration_client
from src.open_dots.agent import DotAgent
from src.llm import ChatModel

# Rotas de webhook do Sender (negociação)
from api.routes.negotiation import router as negotiation_router
# Rotas do Company Response AI
from api.routes.company_response import router as company_response_router, set_mock_storage as set_company_response_mock, set_document_generation_service
# Rotas do External Chat
from api.routes.external_chat import router as external_chat_router
# Rotas dos Webhooks do Sender (ZelinhU)
from api.routes.webhooks_sender import router as webhooks_sender_router
# Contexto da sessao: o que o backend nao entrega pela porta do Sender, a IA da
# Empresa recebe no proprio payload e repassa (ver absorb_session_context).
from api.routes.webhooks_sender import absorb_session_context
# Rotas de análise de reabertura
from api.routes.ticket_reopen import router as ticket_reopen_router
from api.routes.support_assistant import router as support_assistant_router
from agents.conversational_agent.post_call import router as post_call_router
from agents.conversational_agent.web_call import router as web_call_router
from agents.conversational_agent.age_check import router as age_check_router
from agents.conversational_agent.phone_identity import router as phone_identity_router
# O singleton `prompt_generator` saiu do import em 17/09: os unicos usos dele
# eram as rotas company-update e generate-prompt, removidas na mesma mudanca. A
# classe continua em src/prompt_generator.py, sem chamador. Quem gera prompt de
# verdade importa build_system_prompt direto, onde usa.
#Knowledge base (Function Calling)
from src.knowledge_bases import (ChatWithToolsHandler, get_tools_description_for_prompt)
# Mensagens do ZelinhU (modulo centralizado)
from src.utils.zelinhu_messages import create_zelinhu_initial_message
from src.negotiation_acceptance import is_acceptance_message
from src.utils.hand_type import SNAP, resolve_hand_type
# Direitos do cliente (modulo centralizado - usado tambem pelo Sender/ZelinhU)
from src.utils.client_rights import generate_rights_from_description



# Instância de Settings para verificação de API key
settings = Settings()


def _api_key_matches(provided: Optional[str], expected: Optional[str]) -> bool:
    """Constant-time API key comparison; fail closed on empty values."""

    if not provided or not expected:
        return False
    return hmac.compare_digest(str(provided), str(expected))

# Global locks and cache for duplicate request protection
chat_locks = defaultdict(asyncio.Lock)
processed_messages = {}  # Cache: message_id -> response

# Resultado da ultima tentativa de entrega do callback, por message_id.
# Contrato: docs/20260805-contrato-entrega-garantida-callback-webhook.md
# O dedupe por message_id so pode memoizar mensagens EFETIVAMENTE entregues.
# Antes, uma falha de envio era marcada como processada e a reentrega do app
# caia em "duplicate" - a resposta se perdia de vez, sem rede de seguranca.
_callback_delivery = {}  # message_key -> bool


def _record_callback_delivery(message_key: str, delivered: bool) -> None:
    """Registra se o callback daquela mensagem chegou ao app."""
    if not message_key:
        return
    _callback_delivery[message_key] = delivered
    if len(_callback_delivery) > 100:
        del _callback_delivery[next(iter(_callback_delivery))]


def _callback_was_delivered(message_key: str) -> bool:
    """True somente se houve confirmacao de entrega para essa mensagem."""
    return _callback_delivery.get(message_key, False)


def _dispatch_dedupe_key(
    message_id: Optional[str],
    group_metadata: Optional[dict],
    chat_id: Optional[str] = None,
) -> str:
    """Chave de dedupe/idempotencia de um despacho recebido.

    Contrato docs/20260807-contrato-fila-despacho-eco-dispatch-id.md (item 2):
    o app reenvia o MESMO turno com payload identico em cenarios de recuperacao
    (instancia caiu no meio do envio, timeout do POST com a mensagem ja
    entregue). Sem chave estavel, o redespacho vira resposta duplicada.

    - Batch: `group_metadata.messageIds`. O `id` da raiz e um UUID NOVO a cada
      tentativa - dedupe por ele NUNCA casa. Os messageIds nao mudam.
    - Single: o proprio `id` (id da mensagem, estavel entre tentativas).
    - Sem nenhum dos dois: cai no chat_id, preservando o comportamento antigo
      de quem chama sem `id` (ex: POST /message direto).

    Ordenamos os messageIds para a chave nao depender da ordem do array.
    """
    ids = None
    if isinstance(group_metadata, dict):
        ids = group_metadata.get("messageIds") or group_metadata.get("message_ids")

    if isinstance(ids, list):
        stable = sorted(str(i) for i in ids if i)
        if stable:
            return "batch:" + ",".join(stable)

    return message_id or chat_id or ""


def _log_ack(endpoint: str, started: float, chat_id: Optional[str], status: str) -> None:
    """Loga a duracao do ack do webhook (contrato 20260807, item 3).

    O POST do app tem timeout de 30s (AI_SERVICE_WEBHOOK_TIMEOUT_MS); ack acima
    disso e tratado como falha de entrega e vira redespacho. Este log e apenas
    observabilidade - permite medir o p99 do nosso ack sem mudar o fluxo.
    """
    elapsed_ms = (time.perf_counter() - started) * 1000
    print(f"[ACK] {endpoint} status={status} chat_id={chat_id} em {elapsed_ms:.0f}ms")


# Track CNPJs with contact search in progress to avoid duplicate searches
_contact_search_in_progress: set = set()


def _clean_llm_response(content: str) -> str:
    """Remove aspas literais que o LLM pode adicionar ao redor da resposta."""
    result = content.strip()
    # Remove aspas duplas ou simples no início e fim
    if (result.startswith('"') and result.endswith('"')) or \
       (result.startswith("'") and result.endswith("'")):
        result = result[1:-1]
    return result.strip()


def _normalize_negotiation_text(text: str) -> str:
    """Normaliza texto para regras deterministicas da negociacao."""
    normalized = unicodedata.normalize("NFKD", text or "")
    return "".join(
        c for c in normalized if not unicodedata.combining(c)
    ).casefold().strip()


def _is_explicit_acceptance_message(text: str) -> bool:
    """
    True somente para aceite explicito e nao condicionado.

    Evita o falso positivo historico em que ``"nao aceito"`` continha
    ``"aceito"`` e podia encerrar uma negociacao. Em caso ambiguo, a regra
    prefere NAO encerrar automaticamente.
    """
    normalized = _normalize_negotiation_text(text)
    if not normalized:
        return False

    rejection_patterns = (
        r"\bnao\s+(?:aceito|concordo|confirmo|aprovo|autorizo|quero)\b",
        r"\b(?:recuso|discordo)\b",
        r"\bnao\s+(?:pode\s+ser|pode\s+fazer|pode\s+prosseguir)\b",
        r"\b(?:ainda\s+nao|nao\s+sei\s+se)\b.*\b(?:aceito|concordo|fechado|combinado)\b",
    )
    if any(re.search(pattern, normalized) for pattern in rejection_patterns):
        return False

    # Aceites condicionais nao fecham o caso automaticamente. O modelo pode
    # continuar negociando, e um novo turno explicito confirma o fechamento.
    conditional_patterns = (
        r"\bdesde\s+que\b",
        r"\bcontanto\s+que\b",
        r"\bcaso\b",
        r"\btalvez\b",
    )
    if any(re.search(pattern, normalized) for pattern in conditional_patterns):
        return False

    acceptance_patterns = (
        r"\b(?:aceito|concordo|confirmo|aprovo)\b",
        r"\b(?:combinado|fechado|perfeito)\b",
        r"\b(?:pode\s+fazer|pode\s+ser|pode\s+prosseguir|tudo\s+bem|de\s+acordo)\b",
        r"^\s*(?:sim|ok|okay)\b",
    )
    return any(re.search(pattern, normalized) for pattern in acceptance_patterns)

# Global state manager
state_manager = StateManager()

# Global services (inicializados no lifespan)
orchestrator: DotAgent = None  # type: ignore
upload_service: UploadService = None  # type: ignore
rag_service: RAGService = None  # type: ignore
zellu_client: ZelluClient = None  # type: ignore
minio_service: MinioService = None  # type: ignore
zellu_api_client: ZelluAPIClient = None  # type: ignore

# Cache de prompts por empresa (em memoria)
company_prompts_cache: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    global orchestrator, upload_service, rag_service, zellu_client, minio_service, zellu_api_client
    global rag_indexer_instance, doc_extractor_instance

    # Startup: Initialize services
    upload_service = UploadService(settings)
    rag_service = RAGService(settings)
    zellu_client = ZelluClient(settings)
    minio_service = MinioService(settings)

    # Initialize RAG Indexer (novo sistema)
    # Habilitado em 10/12/2025 - Integração com API de download do Zellu
    try:
        from src.services.rag_indexer import RAGIndexer
        global rag_indexer_instance
        rag_indexer_instance = RAGIndexer(settings)
        print("[STARTUP] RAG Indexer inicializado com sucesso")
    except Exception as e:
        print(f"[STARTUP] AVISO: RAG Indexer nao inicializado - {e}")
        rag_indexer_instance = None

    # Processador dos documentos ESPECIFICOS do caso. Diferente do RAG da
    # empresa: aqui entram laudos, peticoes, contratos, comprovantes e anexos
    # recebidos com o chamado. Se um arquivo existir mas nao puder ser lido, a
    # negociacao automatica deve parar em vez de fingir que o documento nao existe.
    try:
        from src.document_processor import DocumentProcessor
        doc_extractor_instance = DocumentProcessor(
            minio_base_url=os.environ.get("ZELLU_MINIO_BASE_URL", "")
        )
        print("[STARTUP] DocumentProcessor do dossie do caso inicializado")
    except Exception as e:
        print(f"[STARTUP] AVISO: DocumentProcessor nao inicializado - {e}")
        doc_extractor_instance = None

    # Initialize Zellu API Client (para buscar instructions e salvar prompts)
    zellu_api_client = ZelluAPIClient(
        base_url=settings.ZELLU_WEBHOOK_URL,
        api_key=settings.API_KEY_ZELLU_IA
    )

    # Initialize AI usage tracker (observabilidade de custos -> POST /api/ai/usage)
    # Singleton lazy: inicializa aqui para logar a config no boot.
    from src.services.ai_usage_tracker import get_tracker
    get_tracker()

    # Initialize LLM
    # llm: modelo forte (OPENAI_MODEL) para analyst/writer.
    # llm_fast: modelo barato (OPENAI_MODEL_FAST) para intake/classifier -
    #   tarefas de extracao/classificacao (contrato 20260629 item 2).
    llm = ChatModel(
        model=settings.OPENAI_MODEL,
        api_key=settings.OPENAI_API_KEY,
        temperature=0.7
    )
    llm_fast = ChatModel(
        model=settings.OPENAI_MODEL_FAST,
        api_key=settings.OPENAI_API_KEY,
        temperature=0.7
    )

    # Initialize company search client (busca empresas no backend Zellu)
    company_search_client = CompanySearchClient()

    # Initialize email validation client (valida emails no backend Zellu)
    email_validation_client = get_email_validation_client()

    # Initialize pre-registration client (cria pré-registro de visitantes)
    pre_registration_client = get_pre_registration_client()

    # Initialize the Open Dots principal
    orchestrator = DotAgent(
        llm=llm,
        llm_fast=llm_fast,
        rag_system=rag_service,
        minio_service=minio_service,
        company_search_client=company_search_client,
        email_validation_client=email_validation_client,
        pre_registration_client=pre_registration_client
    )

    # Initialize independent document generation service
    from src.services.document_generation_service import DocumentGenerationService
    from src.services.case_documents import CaseDocuments as WriterAgentClass
    writer_for_docgen = WriterAgentClass(llm=llm, minio_service=minio_service)
    doc_gen_service = DocumentGenerationService(writer_agent=writer_for_docgen)
    set_document_generation_service(doc_gen_service)

    print(f"[OK] Server {settings.API_TITLE} started successfully!")
    print(f"[OK] Listening on {settings.API_HOST}:{settings.API_PORT}")
    print(f"[OK] RAG System: {'Enabled' if settings.RAG_ENABLED else 'Disabled'}")
    print(f"[OK] RAG Indexer: Initialized")
    print(f"[OK] MinIO Storage: {'Enabled' if minio_service.enabled else 'Disabled'}")
    print(f"[OK] Zellu Integration: {settings.ZELLU_WEBHOOK_URL}")
    print(f"[OK] Zellu Backend Webhook: {settings.ZELLU_BACKEND_WEBHOOK_URL or 'Not configured'}")
    print(f"[OK] Service Callback URL: {settings.SERVICE_BASE_URL}/api/webhooks/company/ticket-response")
    print(f"[OK] Zellu API Client: Initialized")
    print(f"[OK] AI Usage Tracker: {'Enabled' if settings.AI_USAGE_ENABLED else 'Disabled'} -> {settings.ZELLU_WEBHOOK_URL}/api/ai/usage")
    print(f"[OK] Open Dots principal initialized: case intake, analysis and documents")
    print(f"[OK] Sender webhook: POST /negotiation/start (acionado pelo Backend Zellu)")
    print(f"[OK] Document Generation Service: Initialized")

    yield

    # Shutdown: Cleanup
    await llm.close()
    await llm_fast.close()
    print("Shutting down...")
    if doc_extractor_instance:
        await doc_extractor_instance.close()


# Rotas que NAO passam pelo gate global de x-api-key.
#
# `/` e `/health` porque sao publicas por definicao.
#
# `/webhooks/elevenlabs/post-call` porque quem chama e a ElevenLabs, de fora, e
# ela nao tem como mandar a nossa chave: o webhook dela autentica por HMAC, no
# header `elevenlabs-signature`, conferido dentro do proprio handler
# (agents/conversational_agent/post_call.py:assinatura_valida).
#
# Ate 17/09 essa rota estava no gate e o POST da ElevenLabs morria aqui, com 401,
# antes de chegar ao handler. Como e o handler que dispara o upload do audio, o
# POST /api/phone/calls e o POST /api/ai/usage, o efeito foi 41 sessoes de voz
# abertas em HMG sem NENHUMA chegar na Zellu: relato, gravacao e transcricao
# ficaram paradas do nosso lado. O gate e de 22/07 e o pos-chamada de 02/09 -
# passou batido porque os testes chamavam as funcoes direto, sem o app real.
#
# 🔴 Quem entrar nesta lista PRECISA autenticar por conta propria, dentro do
# handler. Sem isso, e uma rota aberta na internet.
ROTAS_SEM_GATE_DE_CHAVE = {"/", "/health", "/webhooks/elevenlabs/post-call"}


async def require_api_key(
    request: Request,
    x_api_key: Optional[str] = Header(default=None, alias="x-api-key")
):
    """Fail-closed global API key gate for all routes except healthcheck.

    A API key da produção deve bater com o valor enviado pelo app ou com
    API_KEY_ZELLU_IA como fallback, preservando compatibilidade com o fluxo
    atual sem abrir a superfície pública.
    """
    if request.url.path in ROTAS_SEM_GATE_DE_CHAVE:
        return

    valid_keys = [key for key in (settings.API_KEY_IA_PRODUCTION, settings.API_KEY_ZELLU_IA) if key]
    if not valid_keys:
        raise HTTPException(status_code=401, detail="Unauthorized")

    if not x_api_key:
        raise HTTPException(status_code=401, detail="Header x-api-key é obrigatório")

    if not any(hmac.compare_digest(x_api_key, key) for key in valid_keys):
        raise HTTPException(status_code=401, detail="API Key inválida")


# Create FastAPI app
app = FastAPI(
    title=settings.API_TITLE,
    description="Intelligent conversation API for information collection",
    version="2.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
    dependencies=[Depends(require_api_key)]
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_credentials=False,
    allow_methods=["POST", "GET"],
    allow_headers=["x-api-key", "content-type", "authorization"],
)

# Mount uploads directory for serving files
try:
    app.mount("/uploads", SafeUploadStaticFiles(directory=settings.UPLOAD_DIR), name="uploads")
except RuntimeError:
    pass  # Directory might not exist yet

# Registrar rotas de webhook do Sender (negociação IA-to-IA)
# Endpoints: /negotiation/start, /negotiation/company-response, /negotiation/cancel, /negotiation/{id}
app.include_router(negotiation_router)
# Registrar rotas do Company Response AI
app.include_router(company_response_router)
# Registrar rotas do External Chat
app.include_router(external_chat_router)
# Registrar rotas dos Webhooks Sender (recebe mensagens do ZelinhU via Zellu Backend)
# Endpoints: /api/webhooks/sender/message, /api/webhooks/sender/status
app.include_router(webhooks_sender_router)

app.include_router(ticket_reopen_router)

# Support Assistant (assistente standalone, RAG namespace zellu_guide).
# Endpoint unico: POST /webhook/support-assistant (contrato docs/webhook-spec.md).
app.include_router(support_assistant_router)

# Pos-chamada do agente telefonico (ElevenLabs Agents).
# Endpoint unico: POST /webhooks/elevenlabs/post-call
# (contrato 20260823-contrato-atendimento-telefonico-elevenlabs.md).
app.include_router(post_call_router)

# Chamada de voz no app (ElevenLabs Agents, WebRTC): o cliente clica no icone
# e fala com o MESMO agente do telefone, sem o telefone tocar.
# Endpoint unico: POST /phone/web-call/token
# (emenda 20260903-emenda-chamada-de-voz-no-app.md).
app.include_router(web_call_router)

# Triagem por idade na ligacao: o agente pergunta a data de nascimento e esta
# rota devolve se a pessoa tem 18 anos ou mais. A conta sai do LLM e vira codigo.
# Endpoint unico: POST /phone/age-check
app.include_router(age_check_router)

# Identidade por telefone na ligacao: a pessoa dita o numero, a Zellu manda um
# codigo por SMS e ela dita o codigo de volta. E o que prova que o numero e dela.
# Endpoints: POST /phone/identity/lookup e POST /phone/identity/verify
# (emenda 20260917-emenda-6-identidade-por-telefone-no-canal-de-voz.md).
app.include_router(phone_identity_router)


# =============================================================================
# INCLUDE ROUTERS
# =============================================================================

# Mock/Test routes (/api/test/*) - REMOVIDO
# app.include_router(mock_router)


# =============================================================================
# BASIC ENDPOINTS
# =============================================================================

@app.get("/", response_model=HealthCheckResponse)
async def root():
    """Root endpoint - health check."""
    return HealthCheckResponse(
        status="healthy",
    )


@app.get("/health", response_model=HealthCheckResponse)
async def health_check():
    """Health check endpoint."""
    return HealthCheckResponse(
        status="healthy",
    )


@app.get("/panel")
async def serve_panel():
    """Serve the test panel HTML."""
    static_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "static",
        "index.html"
    )
    if not os.path.exists(static_path):
        raise HTTPException(status_code=404, detail="Panel not found")
    return FileResponse(static_path, media_type="text/html")


# =============================================================================
# FUNÇÕES AUXILIARES PARA ZELLU
# =============================================================================

from agents.state import ConversationState


def _normalize_case_text(text: str) -> str:
    import unicodedata

    normalized = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in normalized if not unicodedata.combining(c)).lower()


def _amount_context_is_supported_damage(context: str) -> bool:
    """Diferencia valor do dano de preco do bem comprado."""
    damage_terms = (
        "prejuizo", "dano", "danos", "gasto", "gastei", "gastos", "perdi",
        "ressarcimento", "ressarcir", "indenizacao", "indenizar", "reembolso",
        "devolucao", "cobranca", "cobrado", "cobraram", "multa", "taxa",
        "conserto", "reparo", "orcamento", "orcado", "funilaria", "pintura",
        "franquia", "tratamento", "consulta", "ambulancia", "medicamento",
        "exame", "laudo", "desvalorizacao", "abatimento", "desconto",
    )
    purchase_terms = (
        "paguei", "pagou", "comprei", "comprou", "custou", "custa",
        "preco", "valor do produto", "valor do carro", "valor do veiculo",
        "financiei", "financiou",
    )

    has_damage_context = any(term in context for term in damage_terms)
    has_purchase_context = any(term in context for term in purchase_terms)
    return has_damage_context or not has_purchase_context


def _extract_brl_amount_from_text(text: str) -> float:
    """Extrai valor estimado do dano quando o relato fornece base objetiva.

    Regra de produto: preco do bem comprado nao vira valor do caso sozinho.
    Ex.: "Porsche 911 riscado, paguei 1 milhao de reais" descreve o valor do
    bem; o valor do caso depende de conserto, orcamento, laudo, desvalorizacao,
    gasto comprovavel ou pedido de ressarcimento.
    """
    if not text:
        return 0.0

    values = []
    normalized_text = _normalize_case_text(text)
    patterns = [
        (r"r\$\s*([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{2})?|[0-9]+(?:,[0-9]{2})?)", 1),
        (r"([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{2})?|[0-9]+(?:,[0-9]{2})?)\s*reais", 1),
        (r"([0-9]+(?:[,.][0-9]+)?)\s*(?:milhao|milhoes|milha|mi)\s*(?:de\s*)?reais", 1_000_000),
        (r"([0-9]+(?:[,.][0-9]+)?)\s*mil\s*(?:de\s*)?reais", 1_000),
    ]
    for pattern, multiplier in patterns:
        for match in re.finditer(pattern, normalized_text, flags=re.IGNORECASE):
            context = normalized_text[max(0, match.start() - 90):match.end() + 90]
            if not _amount_context_is_supported_damage(context):
                continue

            if multiplier == 1:
                raw = match.group(1).replace(".", "").replace(",", ".")
            else:
                raw = match.group(1).replace(",", ".")
            try:
                values.append(float(raw) * multiplier)
            except ValueError:
                continue

    # "8k", "8 mil", "oito mil", "oitocentos reais": formas que os padroes acima nao cobrem.
    try:
        from src.utils.brl_amount import iter_brl_amounts
        for amount, start, end in iter_brl_amounts(normalized_text):
            context = normalized_text[max(0, start - 90):end + 90]
            if _amount_context_is_supported_damage(context):
                values.append(amount)
    except Exception:
        pass

    return max(values) if values else 0.0


def _case_evidence_review(problem_description: str, estimated_value: float) -> dict:
    """Monta bloco para a UI revisar provas antes das opcoes de acordo."""
    normalized = _normalize_case_text(problem_description)
    has_amount_mention = bool(
        re.search(r"r\$\s*\d|\d[\d\.,]*\s*(?:reais|milhao|milhoes|milha|mi|mil)", normalized)
    )
    has_supported_value = estimated_value > 0
    has_vehicle_damage = any(
        term in normalized
        for term in ("carro", "veiculo", "automovel", "porsche", "bmw", "mercedes", "audi")
    ) and any(term in normalized for term in ("risc", "arranh", "amass", "avari", "danific", "defeito"))
    has_health_expense = any(
        term in normalized
        for term in ("hospital", "ambulancia", "tratamento", "consulta", "medicamento", "exame", "lesao")
    )

    checklist = [
        "Comprovante de compra, contrato, pedido ou nota fiscal.",
        "Fotos, videos, conversas, protocolos e testemunhas do ocorrido.",
    ]
    if has_vehicle_damage:
        checklist.extend([
            "Fotos detalhadas da avaria no veiculo.",
            "Orcamento de conserto, laudo tecnico ou avaliacao de desvalorizacao.",
        ])
    if has_health_expense:
        checklist.extend([
            "Notas fiscais e recibos dos gastos medicos.",
            "Laudo, atestado, prontuario ou comprovante de atendimento.",
        ])
    if has_amount_mention and not has_supported_value:
        checklist.append(
            "Documento que transforme o valor narrado em prejuizo comprovavel, como orcamento, recibo ou laudo."
        )

    status = "supported" if has_supported_value else "needs_evidence"
    display_value = (
        f"R$ {estimated_value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        if has_supported_value
        else "A apurar com documentos"
    )

    return {
        "status": status,
        "displayValue": display_value,
        "estimatedValueIsPlaceholder": not has_supported_value,
        "summary": problem_description,
        "notice": (
            "Valores narrados pelo cliente precisam de comprovantes antes de virar proposta de acordo."
            if not has_supported_value
            else "Valor identificado no relato; ainda deve ser validado por comprovantes antes da negociacao."
        ),
        "checklist": checklist,
        "nextStep": (
            "Anexar comprovantes, orcamentos e laudos antes de escolher uma estrategia de acordo."
            if not has_supported_value
            else "Conferir os comprovantes anexados e seguir para as opcoes de acordo."
        ),
    }


def _police_report_info_from_description(description: str) -> dict:
    """Deriva metadados para o front exibir o campo 'Deseja abrir BO?'."""
    normalized = _normalize_case_text(description)
    has_bo_context = any(
        term in normalized
        for term in (
            "boletim", " b.o", " bo ", "policia", "ameaca", "agressao",
            "violencia", "acidente", "queda", "cai ", "lesao", "ferimento",
            "roubo", "furto", "golpe", "fraude",
        )
    )
    if not has_bo_context:
        return {
            "applicable": False,
            "status": "not_applicable",
            "wantsToFile": None,
            "question": None,
        }

    filed = any(
        term in normalized
        for term in (
            "registrei boletim", "fiz boletim", "abri boletim",
            "tenho boletim", "boletim registrado", "b.o registrado",
        )
    )
    not_filed = any(
        term in normalized
        for term in (
            "nao registrei boletim", "não registrei boletim",
            "nao fiz boletim", "não fiz boletim",
            "nao abri boletim", "não abri boletim",
            "sem boletim",
        )
    )
    wants = any(
        term in normalized
        for term in (
            "quero abrir boletim", "desejo abrir boletim",
            "quero fazer boletim", "desejo fazer boletim",
            "quero registrar boletim",
        )
    )
    does_not_want = any(
        term in normalized
        for term in (
            "nao quero abrir boletim", "não quero abrir boletim",
            "nao desejo abrir boletim", "não desejo abrir boletim",
            "nao quero fazer boletim", "não quero fazer boletim",
        )
    )

    if not_filed:
        status = "not_filed"
    elif filed:
        status = "filed"
    else:
        status = "unknown"

    wants_to_file = True if wants else False if does_not_want else None

    return {
        "applicable": True,
        "status": status,
        "wantsToFile": wants_to_file,
        "question": "Deseja abrir Boletim de Ocorrencia?",
    }


def _build_analysis_data_from_state(state: ConversationState) -> dict:
    """Constrói analysis_data no formato Zellu a partir do ConversationState.

    Args:
        state: Estado da conversa

    Returns:
        Objeto analysis_data formatado para Zellu
    """
    # Monta userInfo
    user_info = {}
    if state.get('client_name'):
        user_info['name'] = state['client_name']

    # CPF: Sempre adicionar o campo (mesmo que vazio) para compatibilidade com backend Zellu
    user_info['cpf'] = state.get('client_cpf') or ""

    # Email e Phone: campos obrigatórios coletados pelo Intake
    user_info['email'] = state.get('client_email') or ""
    user_info['phone'] = state.get('client_phone') or ""

    # Endereço (usado na qualificação das partes pelos templates)
    user_info['address'] = state.get('client_address') or ""

    # Campos de qualificação exigidos pela petição (contrato 20260618 §3).
    # Ainda NÃO coletados pelo Intake -> enviados vazios (template usa placeholder
    # [...] sem quebrar; coleta desses dados fica como evolução futura do Intake).
    user_info['rg'] = state.get('client_rg') or ""
    user_info['nacionalidade'] = state.get('client_nacionalidade') or ""
    user_info['estadoCivil'] = state.get('client_estado_civil') or ""
    user_info['profissao'] = state.get('client_profissao') or ""

    print(f"[MAIN] userInfo montado: name={user_info.get('name')}, email={user_info.get('email')}, phone={user_info.get('phone')}")

    # Monta caseDetails
    # `or` e nao default do .get(): a chave EXISTE no state com valor None
    # (src/state_manager.py e o state inicial do webhook), entao o default do
    # .get() nunca entrava e o f-string abaixo imprimia "None" dentro do PDF
    # do Modelo 1 - achado 4.1 (CRITICA) da auditoria de 18/09/2026.
    legal_category = state.get('legal_category') or 'Caso Jurídico'
    problem_description = state.get('problem_description', '')
    case_details = {
        "title": legal_category,
        "description": problem_description,
        # Ambos sao substituidos por refine_case_details() assim que a
        # notificacao fica pronta (relato em 2+ paragrafos + pedidos reais).
        # O que fica aqui e o ultimo recurso, para o caso de a montagem dos
        # documentos falhar inteira.
        "expectedSolution": f"Resolucao do problema de {legal_category} conforme analise juridica",
        "documents": []
    }
    case_details["policeReport"] = _police_report_info_from_description(problem_description)
    from src.services.client_intake_record import record_payload
    case_details["intakeRecord"] = record_payload(state)
    case_details["documents"] = case_details["intakeRecord"]["documents"]

    # Nome da empresa: usar o coletado pelo Intake, ou extrair da descrição como fallback
    opposing_party_name = state.get('opposing_party_name') or ""
    opposing_trade_name = state.get('opposing_party_trade_name') or ""
    opposing_razao_social = state.get('opposing_party_razao_social') or ""
    opposing_website = state.get('opposing_party_website') or ""

    if opposing_party_name:
        print(f"[MAIN] Empresa coletada pelo Intake: {opposing_party_name}")
        print(f"[MAIN] Trade name: {opposing_trade_name or 'N/A'}, Razao social: {opposing_razao_social or 'N/A'}")
    else:
        # Fallback: tentar extrair da descrição
        opposing_party_name = _extract_company_name_from_description(problem_description)
        if opposing_party_name:
            print(f"[MAIN] Empresa extraida da descricao: {opposing_party_name}")
        else:
            print(f"[MAIN] Nenhuma empresa identificada - usando placeholder")

    # CNPJ da parte contrária (coletado pelo Intake ou preenchido automaticamente pela busca)
    opposing_cnpj = state.get('opposing_party_cnpj') or ""
    # Preservar também letras do CNPJ alfanumérico e rejeitar placeholders.
    cnpj_digits = normalize_cnpj(opposing_cnpj)
    if not is_valid_cnpj(cnpj_digits):
        raise ValueError('CNPJ válido da empresa é obrigatório para abrir o chamado')

    # Construir faviconUrl a partir do website usando API do Google (gratuita)
    favicon_url = ""
    if opposing_website:
        from urllib.parse import urlparse
        try:
            domain = urlparse(opposing_website if "://" in opposing_website else f"https://{opposing_website}").netloc
            if domain:
                favicon_url = f"https://www.google.com/s2/favicons?domain={domain}&sz=64"
        except Exception:
            pass

    # Monta opposingParty (obrigatório pelo backend Zellu)
    # tradeName = nome fantasia (display name), companyName = razão social (nome jurídico)
    opposing_party = {
        "type": "pj",
        "tradeName": opposing_party_name or "Parte contraria a ser identificada",
        "companyName": opposing_razao_social or None,
        "document": cnpj_digits,
        # Canal de contato da empresa (achados 4.3/5.1). O backend usa este
        # campo como fallback do slot canalContato dos modelos 1 e 2.
        "email": state.get('opposing_party_email') or "",
        "phone": "",
        "address": "",
        "website": opposing_website or None,
        "faviconUrl": favicon_url or None,
    }

    print(f"[MAIN] opposingParty montado: tradeName={opposing_party['tradeName']}, companyName={opposing_party.get('companyName')}, document={opposing_party['document']}, website={opposing_party.get('website')}")

    # =================================================================
    # RIGHTS - Gerar direitos baseado no problema (OBRIGATÓRIO > 0)
    # =================================================================
    rights = state.get('client_rights') or []

    # Se não há direitos do state, gerar baseado na descrição do problema
    if not rights and problem_description:
        rights = generate_rights_from_description(problem_description)

    # Garantir que sempre tenha pelo menos um direito (exigido pelo backend)
    if not rights:
        rights = [
            "Direito a protecao e defesa do consumidor (Art. 6 CDC)",
            "Direito a reparacao de danos patrimoniais e morais (Art. 6 CDC)"
        ]

    # =================================================================
    # ESTIMATED VALUE - Garantir valor > 0 (OBRIGATÓRIO)
    # =================================================================
    from src.services.recommendation_scorer import parse_estimated_value

    estimated_value = parse_estimated_value(state.get('potential_gain') or state.get('estimated_value') or 0.0)
    if estimated_value <= 0:
        described_value = _extract_brl_amount_from_text(problem_description)
        if described_value > 0:
            estimated_value = described_value
            print(f"[MAIN] estimatedValue extraido do relato: {estimated_value}")

    supported_estimated_value = estimated_value
    evidence_review = _case_evidence_review(problem_description, supported_estimated_value)

    # Sem base monetaria, manter a apuracao aberta; nunca fabricar um valor.

    # Monta recommendations com scores dinamicos. Usa valor sustentado por
    # contexto/provas, nao o fallback tecnico de R$ 100,00.
    from src.services.recommendation_scorer import score_recommendations
    scoring_state = dict(state)
    scoring_state['potential_gain'] = supported_estimated_value
    scoring_state['estimated_value'] = supported_estimated_value
    recommendations = score_recommendations(scoring_state)

    # Monta analysis_data completo
    analysis_data = {
        "problem": problem_description or 'Problema a ser detalhado',
        "rights": rights,
        "valueAssessment": evidence_review,
        "preNegotiationReview": {
            "title": "Revise o caso e anexe os comprovantes",
            "summary": problem_description,
            "claimedValue": evidence_review["displayValue"],
            "requiredEvidence": evidence_review["checklist"],
            "notice": evidence_review["notice"],
            "nextStep": evidence_review["nextStep"],
        },
        "legal_category": legal_category if legal_category != 'Caso Jurídico' else 'Geral',
        "recommendations": recommendations,
        "userInfo": user_info,
        "opposingParty": opposing_party,
        "caseDetails": case_details
    }

    if supported_estimated_value > 0:
        analysis_data["estimatedValue"] = supported_estimated_value
    return analysis_data


def _extract_company_name_from_description(description: str) -> str:
    """Tenta extrair o nome da empresa/parte contrária da descrição do problema."""
    if not description:
        return None

    import re

    patterns = [
        r'(?:empresa|loja|site|operadora|banco|companhia)\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)*)',
        r'na\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)*)',
        r'pela\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)*)',
        r'da\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)*)',
        r'do\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)*)'
    ]

    for pattern in patterns:
        match = re.search(pattern, description)
        if match:
            company_name = match.group(1).strip()
            common_words = ['internet', 'online', 'whatsapp', 'email', 'site', 'Comprei', 'Recebi']
            if company_name not in common_words:
                return company_name

    return None


# =============================================================================
# WEBHOOKS ZELLU - Recebimento e Envio de Mensagens
# =============================================================================

async def process_message_background(request, dedupe_key: str):
    """Processa mensagem em background e envia resposta via webhook.

    `dedupe_key` vem de _dispatch_dedupe_key (contrato 20260807): e o `id` no
    formato single e os group_metadata.messageIds no batch.
    """
    try:
        async with chat_locks[request.chat_id]:
            print(f"[WEBHOOK] Processando mensagem em background - chat_id={request.chat_id}, dedupe_key={dedupe_key}")

            response = await process_message(request)

            if dedupe_key:
                # So memoiza se o callback chegou ao app (contrato 20260805).
                # Memoizar uma entrega falha faria a reentrega do app cair em
                # "duplicate" e a resposta se perderia em definitivo.
                if _callback_was_delivered(dedupe_key):
                    processed_messages[dedupe_key] = response

                    if len(processed_messages) > 100:
                        oldest_key = next(iter(processed_messages))
                        del processed_messages[oldest_key]
                else:
                    print(
                        f"[WEBHOOK] Callback NAO entregue - dedupe_key={dedupe_key} "
                        f"nao memoizado; reentrega do app volta a processar"
                    )

            print(f"[WEBHOOK] Processamento background concluido - chat_id={request.chat_id}")

    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print(f"[WEBHOOK] ERRO no processamento background:")
        print(error_trace)


@app.post("/webhook/chat")
async def webhook_chat(request: Request, background_tasks: BackgroundTasks):
    """Webhook endpoint para receber mensagens do Zellu.

    Este é o endpoint que o Zellu chama para enviar mensagens do usuário.
    Conforme especificado em AI_SERVICE_WEBHOOK_FORMAT.md.

    FLUXO ASSÍNCRONO:
    1. Retorna 200 OK imediatamente (confirmando recebimento)
    2. Processa a mensagem em background
    3. Envia resposta via POST /api/chat/webhook do Zellu
    """
    from api.schemas import MessageRequest

    ack_started = time.perf_counter()
    data = await request.json()

    # Anexos sem texto são aceitos pelo schema. Leitura/transcrição ocorre
    # em background no orchestrator, uma vez, junto ao registro do original.

    # Eco do despacho (contrato 20260807): aceita snake_case e camelCase.
    if not data.get("dispatch_id") and data.get("dispatchId"):
        data["dispatch_id"] = data.get("dispatchId")

    msg_request = MessageRequest(**data)
    message_id = msg_request.id

    # Dedupe do redespacho: `id` no single, messageIds no batch (contrato 20260807)
    dedupe_key = _dispatch_dedupe_key(message_id, msg_request.group_metadata, msg_request.chat_id)

    if dedupe_key and dedupe_key in processed_messages:
        print(f"[WEBHOOK] DUPLICATA DETECTADA - dedupe_key={dedupe_key}, chat_id={msg_request.chat_id}")
        _log_ack("/webhook/chat", ack_started, msg_request.chat_id, "duplicate")
        return {"status": "duplicate", "message": "Mensagem já processada"}

    print(f"[WEBHOOK] Recebido - chat_id={msg_request.chat_id}, message_id={message_id}, dispatch_id={msg_request.dispatch_id}")
    print(f"[WEBHOOK] Retornando 200 OK e processando em background...")

    background_tasks.add_task(process_message_background, msg_request, dedupe_key)

    _log_ack("/webhook/chat", ack_started, msg_request.chat_id, "accepted")
    return {"status": "accepted", "message": "Mensagem recebida, processando..."}


# =============================================================================
# ENDPOINT /api/chat/webhook - Alternativo ao /webhook/chat
# =============================================================================
# Conforme AI_SERVICE_WEBHOOK_FORMAT.md
# Este endpoint é chamado pelo Backend Zellu para enviar mensagens à IA
# =============================================================================

@app.post("/api/chat/webhook")
async def api_chat_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_api_key: str = Header(None, alias="x-api-key")
):
    """
    Endpoint unificado para receber mensagens do Backend Zellu.

    Detecta automaticamente o tipo de payload e roteia para o fluxo correto:
    - External Chat (visitantes): se tem sessionId + context.company.id == "zellu_chat"
    - Chat Interno (native runtime): se tem chat_id + body_message

    FLUXO:
    1. Valida x-api-key
    2. Detecta tipo de payload
    3. Roteia para fluxo apropriado
    """
    from api.schemas import ChatWebhookRequest, ChatWebhookResponse, MessageRequest
    from api.routes.external_chat import external_chat_webhook

    ack_started = time.perf_counter()

    # 1. Validar API Key
    if not x_api_key:
        print("[API/CHAT/WEBHOOK] ERRO: Header x-api-key ausente")
        raise HTTPException(
            status_code=401,
            detail="Header x-api-key é obrigatório"
        )

    if not _api_key_matches(x_api_key, settings.API_KEY_ZELLU_IA):
        print(f"[API/CHAT/WEBHOOK] ERRO: API Key inválida")
        raise HTTPException(
            status_code=401,
            detail="API Key inválida"
        )

    # 2. Parsear payload
    try:
        data = await request.json()
        print(f"[API/CHAT/WEBHOOK] Payload recebido: {list(data.keys())}")

        # =====================================================================
        # 3. DETECTAR TIPO DE PAYLOAD E ROTEAR
        # =====================================================================

        # Verificar se é formato External Chat (tem sessionId e context)
        if "sessionId" in data or "session_id" in data:
            context = data.get("context", {})
            company = context.get("company", {})
            company_id = company.get("id", "")

            # Se company.id é "zellu_chat" ou não está definido, é External Chat
            is_external_chat = company_id == "zellu_chat" or company_id == "" or not company_id

            if is_external_chat:
                print(f"[API/CHAT/WEBHOOK] >>> Detectado EXTERNAL CHAT (company.id={company_id})")
                print(f"[API/CHAT/WEBHOOK] >>> Roteando para /api/external/chat/webhook")

                # Chamar diretamente o handler do external_chat
                # Criar um novo request com o mesmo body
                from starlette.requests import Request as StarletteRequest
                from starlette.datastructures import Headers
                import json

                # Chamar o endpoint diretamente passando os dados
                external_response = await _handle_external_chat(data, x_api_key)
                # Fluxo sincrono: a duracao aqui e resposta completa, nao ack.
                _log_ack("/api/chat/webhook", ack_started, data.get("sessionId") or data.get("session_id"), "external_sync")
                return external_response

        # =====================================================================
        # 4. FLUXO NORMAL (Chat Interno / native runtime)
        # =====================================================================

        # Validar campos obrigatórios para fluxo interno
        chat_id = data.get("chat_id") or data.get("chatId")
        if not chat_id:
            raise HTTPException(
                status_code=400,
                detail="Campo chat_id é obrigatório"
            )

        # Extrair campos do payload (aceita snake_case e camelCase)
        message_id = data.get("id")
        # Identificador do despacho, na raiz do payload (contrato 20260807).
        # Ecoado depois na raiz do callback - nunca derivado do `id`.
        dispatch_id = data.get("dispatch_id") or data.get("dispatchId")
        nome = data.get("nome")
        message_type = data.get("message_type") or data.get("messageType", "text")
        # Aceita: body_message, bodyMessage, ou message (formato Zellu)
        body_message = data.get("body_message") or data.get("bodyMessage") or data.get("message", "")
        audio = data.get("audio")
        files = data.get("files", [])
        group_metadata = data.get("group_metadata") or data.get("groupMetadata")
        is_finished = data.get("is_finished", data.get("isFinished"))
        messagedata_from_request = (data.get("messagedata") or data.get("messageData")
                                    or data.get("message_data"))
        root_form_response = data.get("dynamic_form_response") or data.get("dynamicFormResponse")
        if root_form_response:
            messagedata_from_request = dict(messagedata_from_request or {})
            messagedata_from_request["dynamic_form_response"] = root_form_response
        if messagedata_from_request:
            from src.services.dynamic_form_bridge import normalize_messagedata
            messagedata_from_request = normalize_messagedata(messagedata_from_request)
        company = data.get("company")  # Dados da empresa (quando chamado via "Busca Empresa")
        # Qualificacao do cliente (context.client) p/ preencher os documentos do chamado
        # (contrato 20260624). Pode nao vir hoje; quando vier, populamos o state.
        client_context = (data.get("context") or {}).get("client") or None

        if company:
            print(f"[API/CHAT/WEBHOOK] Empresa no payload: {company}")
        print(f"[API/CHAT/WEBHOOK] Fluxo INTERNO - chat_id={chat_id}, message_id={message_id}, dispatch_id={dispatch_id or '(ausente)'}")
        print(f"[API/CHAT/WEBHOOK] Tipo: {message_type}, Texto: {body_message[:100] if body_message else '(vazio)'}...")
        if is_finished is not None:
            print(f"[API/CHAT/WEBHOOK] is_finished={is_finished} (Continuar Conversando)")

    except HTTPException:
        raise
    except Exception as e:
        print(f"[API/CHAT/WEBHOOK] ERRO ao parsear payload: {e}")
        raise HTTPException(
            status_code=400,
            detail=f"Payload inválido: {str(e)}"
        )

    # 5. Verificar duplicatas (redespacho do app - contrato 20260807, item 2)
    #    Batch: chave pelos group_metadata.messageIds, pois o `id` da raiz e um
    #    UUID novo a cada tentativa. Single: o proprio `id`.
    dedupe_key = _dispatch_dedupe_key(message_id, group_metadata, chat_id)

    if dedupe_key and dedupe_key in processed_messages:
        print(f"[API/CHAT/WEBHOOK] DUPLICATA DETECTADA - dedupe_key={dedupe_key}")
        _log_ack("/api/chat/webhook", ack_started, chat_id, "duplicate")
        return ChatWebhookResponse(
            success=True,
            message="Mensagem já processada (duplicata)",
            chat_id=chat_id,
            message_id=message_id
        )

    # Leitura de arquivos e áudio centralizada no processamento em background.

    # 7. Garantir que body_message não seja vazio (exigido pelo MessageRequest)
    if not body_message or body_message.strip() == "":
        if audio:
            body_message = "[Mensagem de áudio]"
        elif files:
            body_message = "[Mensagem com arquivo(s)]"
        else:
            body_message = "[Mensagem vazia]"
        print(f"[API/CHAT/WEBHOOK] body_message vazio, usando placeholder: {body_message}")

    # 8. Converter para MessageRequest (formato interno)
    msg_request = MessageRequest(
        chat_id=chat_id,
        message_type=message_type,
        body_message=body_message,
        audio=audio,
        files=files,
        id=message_id,
        nome=nome,
        company=company,
        client=client_context,
        group_metadata=group_metadata,
        is_finished=is_finished,
        messagedata=messagedata_from_request,
        dispatch_id=dispatch_id
    )

    # 8. Processar em background
    print(f"[API/CHAT/WEBHOOK] Retornando 200 OK e processando em background...")
    background_tasks.add_task(process_message_background, msg_request, dedupe_key)

    # 9. Retornar confirmação
    _log_ack("/api/chat/webhook", ack_started, chat_id, "accepted")
    return ChatWebhookResponse(
        success=True,
        message="Mensagem recebida, processando...",
        chat_id=chat_id,
        message_id=message_id
    )


async def _handle_external_chat(data: dict, api_key: str):
    """
    Handler interno para processar requisições do External Chat.

    Chama diretamente as funções do módulo external_chat.
    """
    from api.routes.external_chat import (
        get_or_create_session,
        extract_data_from_conversation,
        generate_response
    )
    from api.schemas_external_chat import (
        ExternalChatRequest,
        ExternalChatResponse,
        ExternalChatResponseMessage
    )
    from api.schemas_company_response import MessageHistoryItem
    import json

    try:
        # Parsear request
        chat_request = ExternalChatRequest(**data)

        session_id = chat_request.sessionId
        message_history = chat_request.messageHistory or []
        context = chat_request.context

        # Extrair dados do cliente do context
        client_name = None
        client_email = None
        client_phone = None

        if context and context.client:
            client_name = context.client.name
            client_email = context.client.email
            client_phone = context.client.phone

        print(f"[EXTERNAL-CHAT-ROUTE] SessionId: {session_id}")
        print(f"[EXTERNAL-CHAT-ROUTE] Mensagens: {len(message_history)}")
        print(f"[EXTERNAL-CHAT-ROUTE] Cliente: name={client_name}, email={client_email}")

        # Obter ou criar sessao
        session = get_or_create_session(
            session_id,
            client_name=client_name,
            client_email=client_email,
            client_phone=client_phone
        )

        # Pegar ultima mensagem do usuario
        last_user_message = ""
        for msg in reversed(message_history):
            if msg.origin == "zelinhu":
                last_user_message = msg.body
                break

        print(f"[EXTERNAL-CHAT-ROUTE] Ultima mensagem: {last_user_message[:100] if last_user_message else 'N/A'}...")

        # Extrair dados da conversa
        session = await extract_data_from_conversation(session, message_history)
        last_user_message = next((m.body for m in reversed(message_history) if m.origin == "zelinhu"), last_user_message)

        # Log dados coletados
        print(f"[EXTERNAL-CHAT-ROUTE] Dados coletados:")
        print(f"  - Nome: {session.client_name}")
        print(f"  - Email: {session.client_email}")
        print(f"  - Problema: {session.problem_description[:50] if session.problem_description else 'N/A'}...")
        print(f"  - Campos faltando: {session.get_missing_fields()}")

        # Gerar resposta
        response_text, is_finished = await generate_response(session, last_user_message)
        if session.evidence_notice:
            response_text += "\n\n" + session.evidence_notice

        print(f"[EXTERNAL-CHAT-ROUTE] Resposta: {response_text[:100]}...")
        print(f"[EXTERNAL-CHAT-ROUTE] isFinished: {is_finished}")

        # Montar response
        response_message = ExternalChatResponseMessage(
            role="assistant",
            content=response_text,
            isFinished=is_finished,
            awaitingContinuation=False
        )

        # Se finalizado, incluir analysisData
        if is_finished:
            session.is_finished = True
            analysis_data = session.to_analysis_data()
            print(f"[EXTERNAL-CHAT-ROUTE] *** COLETA FINALIZADA ***")
            response_message.messagedata = {"analysisData": analysis_data}

        from src.services.case_evidence import evidence_metadata
        response_message.messagedata = response_message.messagedata or {}
        response_message.messagedata["evidenceUpload"] = evidence_metadata(session.model_dump())

        return ExternalChatResponse(messages=[response_message])

    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print(f"[EXTERNAL-CHAT-ROUTE] ERRO:")
        print(error_trace)
        raise HTTPException(
            status_code=500,
            detail=f"Erro ao processar external chat: {str(e)}"
        )


def _extract_cnpj_raiz(cnpj: str) -> str:
    """Extrai os primeiros 8 digitos (raiz) de um CNPJ."""
    digits = ''.join(c for c in cnpj if c.isdigit())
    return digits[:8]


# Valores permitidos para a classificacao de origem do contato (enviado ao backend).
# Mantido em ingles por ser o padrao de comunicacao com o backend.
CONTACT_SOURCE_VALUES = ("official_website", "government", "external", "unknown")


def _classify_contact_source(raw: str) -> str:
    """
    Classifica a fonte crua devolvida pelo Firecrawl em uma das categorias
    pre-definidas. O Firecrawl devolve a fonte como texto livre em linguagem
    natural (ex: "site oficial", "Reclame Aqui", "https://www.empresa.com.br"),
    entao normalizamos para um valor estavel.

    Categorias:
    - official_website: site/portal oficial da propria empresa
    - government: orgao/site governamental (consumidor.gov, procon, .gov)
    - external: plataforma de terceiros (Reclame Aqui, Google, redes sociais)
    - unknown: vazio ou nao reconhecido (fallback seguro)
    """
    if not raw or not raw.strip():
        return "unknown"

    s = raw.strip().lower()

    # 1. Governo primeiro (consumidor.gov.br contem ".gov", precisa vir antes da URL generica)
    if ".gov" in s or "consumidor.gov" in s or "procon" in s or "governo" in s:
        return "government"

    # 2. Plataformas externas de terceiros
    external_markers = (
        "reclame aqui", "reclameaqui", "google", "bing", "jusbrasil",
        "econodata", "serasa", "linkedin", "facebook", "instagram",
        "twitter", "busca", "internet", "diretorio", "diretório",
    )
    if any(m in s for m in external_markers):
        return "external"

    # 3. Site oficial declarado explicitamente
    official_markers = (
        "site oficial", "website", "web site", "site da empresa",
        "pagina oficial", "página oficial", "homepage", "home page",
        "site institucional", "portal da empresa", "portal oficial",
    )
    if any(m in s for m in official_markers):
        return "official_website"

    # 4. URL "pelada" sem marcador externo/gov: assume dominio da propria empresa
    if s.startswith("http://") or s.startswith("https://") or "www." in s:
        return "official_website"

    return "unknown"


def _melhor_canal_de_contato(contact_points: List[Dict[str, Any]]) -> str:
    """Escolhe o contato que vai no campo "Canal de contato" dos documentos.

    Os Modelos 1 e 2 (LZL-006-C) preveem "Canal de contato:
    [E-MAIL/SETOR DE ATENDIMENTO]" na identificacao de quem e notificado. Ate a
    auditoria de 18/09/2026 esse campo saia sempre vazio (achados 4.3 e 5.1),
    porque os contatos encontrados iam direto para a base da Zellu e nunca
    voltavam para a sessao.

    Preferencia: melhor contato marcado E e-mail > e-mail > melhor contato >
    o que houver. E-mail vem antes de telefone porque o documento pede um canal
    onde a empresa possa responder por escrito, dentro do prazo do modelo.
    """
    ativos = [
        c for c in (contact_points or [])
        if c.get("isActive", True) and str(c.get("content") or "").strip()
    ]
    if not ativos:
        return ""

    def preferencia(contato: Dict[str, Any]) -> int:
        e_email = str(contato.get("type") or "").lower() == "email"
        melhor = bool(contato.get("isBestContact"))
        if melhor and e_email:
            return 0
        if e_email:
            return 1
        if melhor:
            return 2
        return 3

    return str(min(ativos, key=preferencia)["content"]).strip()


async def _background_search_company_contacts(
    cnpj: str, company_name: str, chat_id: Optional[str] = None
):
    """
    Background task: busca contatos da empresa via Firecrawl e salva no backend.

    Fluxo:
    1. Verifica se ja existem contatos no backend (por cnpjRaiz para cobrir matriz+filiais)
    2. Se nao existem, busca via Firecrawl Agent
    3. Transforma resultado para formato da API (normaliza tipos, monta notes)
    4. Guarda o melhor canal na sessao, para os documentos (achados 4.3/5.1)
    5. Salva via upsert no backend (sanitizacao feita pelo client)

    Este task e fire-and-forget - nao afeta a conversa. O passo 4 e a unica
    escrita no state, e ela e aditiva: so grava quando achou um canal.
    """

    def guardar_canal(contact_points, origem: str) -> None:
        """Grava o canal na sessao, para o documento nao sair com o campo vazio."""
        if not chat_id:
            return
        canal = _melhor_canal_de_contato(contact_points)
        if not canal:
            return
        state_manager.update_state(chat_id, {"opposing_party_email": canal})
        print(f"[CONTACT-BG] Canal de contato da sessao {chat_id} ({origem}): {canal}")
    from src.services.firecrawl_service import get_firecrawl_service
    from src.company_search_client import get_company_search_client

    try:
        print(f"[CONTACT-BG] Iniciando busca de contatos para {company_name} (CNPJ={cnpj})")

        client = get_company_search_client()

        # Passo 1: Verificar se ja existem contatos no backend
        # Usa cnpjRaiz (8 primeiros digitos) para cobrir matriz + todas as filiais
        cnpj_raiz = _extract_cnpj_raiz(cnpj)
        search_result = await client.search_contact_points(cnpj_raiz=cnpj_raiz)
        if search_result.success and search_result.has_contacts:
            print(f"[CONTACT-BG] Grupo {cnpj_raiz} ja tem {search_result.total} contatos. Pulando busca.")
            # A busca para aqui, mas a sessao ainda precisa do canal.
            guardar_canal(search_result.contacts, "base da Zellu")
            return

        # Passo 2: Buscar contatos via Firecrawl (sync - roda em thread)
        firecrawl = get_firecrawl_service()
        if not firecrawl.api_key:
            print(f"[CONTACT-BG] Firecrawl nao configurado. Pulando busca de contatos.")
            return

        # Firecrawl agent() e sync, rodar em thread para nao bloquear event loop
        loop = asyncio.get_event_loop()
        crawl_result = await loop.run_in_executor(
            None,
            lambda: firecrawl.search_company_contacts(
                company_name=company_name,
                cnpj=cnpj,
            )
        )

        if not crawl_result.success or (not crawl_result.contacts and not crawl_result.website):
            print(f"[CONTACT-BG] Nenhum contato encontrado para {company_name}: {crawl_result.error}")
            return

        print(f"[CONTACT-BG] Firecrawl encontrou {len(crawl_result.contacts)} contatos "
              f"(credits={crawl_result.credits_used}, website={crawl_result.website or 'N/A'})")

        # Salvar website no state se encontrado (para chamados futuros com mesmo CNPJ)
        if crawl_result.website:
            print(f"[CONTACT-BG] Website encontrado para {company_name}: {crawl_result.website}")

        # Passo 3: Transformar para formato da API de upsert
        api_contacts = []
        for c in crawl_result.contacts:
            contact_type = c.get("type", "email").lower()
            # Normalizar tipo para os aceitos pela API
            if contact_type not in ("email", "phone", "cellphone"):
                if "celular" in contact_type or "whatsapp" in contact_type:
                    contact_type = "cellphone"
                elif "telefone" in contact_type or "fone" in contact_type:
                    contact_type = "phone"
                else:
                    contact_type = "email"

            dept = c.get("department", "")
            source_raw = c.get("source", "")
            source = _classify_contact_source(source_raw)
            notes_parts = []
            if dept:
                notes_parts.append(f"Departamento: {dept}")
            if source_raw:
                notes_parts.append(f"Fonte: {source_raw}")
            notes_parts.append("Encontrado automaticamente via Firecrawl")

            api_contacts.append({
                "type": contact_type,
                "content": c["content"].strip(),
                "source": source,
                "isValid": True,
                "isBestContact": False,
                "isActive": True,
                "notes": ". ".join(notes_parts),
            })

        if not api_contacts:
            print(f"[CONTACT-BG] Nenhum contato valido para salvar para {company_name}")
            return

        # Passo 4: guardar o canal ANTES do upsert - se o backend recusar o
        # upsert, o documento ainda sai com o canal preenchido.
        guardar_canal(api_contacts, "Firecrawl")

        # Passo 5: Salvar no backend (sanitizacao de limites feita pelo client)
        upsert_result = await client.upsert_contact_points(cnpj, api_contacts)
        if upsert_result.success:
            print(f"[CONTACT-BG] Contatos salvos para {company_name} (companyId={upsert_result.company_id}): "
                  f"created={upsert_result.created}, updated={upsert_result.updated}, total={upsert_result.total}")
        else:
            print(f"[CONTACT-BG] Erro ao salvar contatos: {upsert_result.error}")

    except Exception as e:
        import traceback
        print(f"[CONTACT-BG] ERRO na busca de contatos para {company_name}:")
        print(traceback.format_exc())

    finally:
        _contact_search_in_progress.discard(cnpj)


@app.post("/message", response_model=MessageResponse)
async def process_message(request):
    """Process an incoming message.

    Compatível com formato Zellu (aceita campos id e nome).
    """
    from api.schemas import MessageRequest, MessageResponse

    try:
        user_id = request.user_id or request.chat_id

        state = state_manager.get_state(request.chat_id)

        if not state:
            state = state_manager.create_state(
                chat_id=request.chat_id,
                user_id=user_id,
                message_type=request.message_type,
                body_message=request.body_message,
                audio=request.audio,
                files=request.files
            )

            if request.nome:
                state['client_name'] = request.nome
                state_manager.update_state(request.chat_id, {'client_name': request.nome})

        else:
            state_manager.update_state(
                request.chat_id,
                {
                    "message_type": request.message_type,
                    "body_message": request.body_message,
                    "audio": request.audio,
                    "files": request.files
                }
            )
            state = state_manager.get_state(request.chat_id)

            # =========================================================
            # CONTINUAR CONVERSANDO: is_finished=False em state completo
            # Reseta campos de fluxo mas MANTEM dados pessoais coletados
            # =========================================================
            is_finished_flag = getattr(request, 'is_finished', None)
            state_completed = state.get('completed', False)

            if is_finished_flag is False and state_completed:
                print(f"[PROCESS_MESSAGE] >>> CONTINUAR CONVERSANDO detectado - chat_id={request.chat_id}")
                print(f"[PROCESS_MESSAGE] Resetando fluxo, mantendo dados pessoais...")

                continuation_reset = {
                    # Resetar controle de fluxo
                    "completed": False,
                    "current_agent": "intake",
                    "validated": False,
                    "ready_for_classification": False,
                    # Modo continuacao
                    "continuation_mode": True,
                    "continuation_rounds": 0,
                    "max_continuation_rounds": 15,
                    # Limpar classificacao/analise (serao refeitas)
                    "legal_category": None,
                    "urgency": None,
                    "client_rights": [],
                    "potential_gain": None,
                    "relevant_documents": [],
                    "suggested_next_steps": [],
                    "required_documents": [],
                    # Limpar documentos gerados
                    "generated_documents": None,
                    "document_generation_date": None,
                    "document_generation_error": None,
                    "minio_urls": None,
                    # Resetar loop classifier
                    "description_complete": False,
                    "missing_context": None,
                    "follow_up_attempts": 0,
                    "previous_descriptions": [],
                    # Limpar descricao anterior (vai coletar nova)
                    "problem_description": None,
                    "problem_type": None,
                }

                state_manager.update_state(request.chat_id, continuation_reset)
                state = state_manager.get_state(request.chat_id)

                kept_fields = {
                    "client_name": state.get('client_name'),
                    "client_email": state.get('client_email'),
                    "client_phone": state.get('client_phone'),
                    "first_name": state.get('first_name'),
                    "last_name": state.get('last_name'),
                    "opposing_party_name": state.get('opposing_party_name'),
                    "opposing_party_cnpj": state.get('opposing_party_cnpj'),
                }
                print(f"[PROCESS_MESSAGE] Dados mantidos: {kept_fields}")
                print(f"[PROCESS_MESSAGE] continuation_mode=True, max_rounds=15")

        # Se empresa veio no payload (chamado via "Busca Empresa") e ainda nao esta no state
        if request.company and not state.get('opposing_party_name'):
            comp = request.company
            comp_name = comp.get('trade_name') or comp.get('company_name')
            comp_cnpj = comp.get('cnpj')
            comp_id = comp.get('id')

            if comp_name:
                company_updates = {
                    'opposing_party_found_in_db': True,
                    'company_confirmed': True,
                    'opposing_party_name': comp_name,
                }
                if comp_cnpj:
                    company_updates['opposing_party_cnpj'] = comp_cnpj
                if comp_id:
                    company_updates['opposing_party_company_id'] = comp_id

                state.update(company_updates)
                state_manager.update_state(request.chat_id, company_updates)
                print(f"[PROCESS_MESSAGE] Empresa pre-populada do payload: {comp_name} (CNPJ: {comp_cnpj})")
            else:
                print(f"[PROCESS_MESSAGE] company no payload mas sem nome: {request.company}")

        # Injetar messagedata do request no state (dynamic_form_response).
        # A resposta de formulario vale so para o turno em que chegou: sobra de
        # turno anterior faria o intake reler um envio antigo a cada mensagem.
        state['incoming_messagedata'] = None
        if hasattr(request, 'messagedata') and request.messagedata:
            from src.services.dynamic_form_bridge import normalize_messagedata
            normalized_messagedata = normalize_messagedata(request.messagedata)
            if 'dynamic_form_response' in normalized_messagedata:
                state['incoming_messagedata'] = normalized_messagedata
                print(f"[PROCESS_MESSAGE] messagedata com dynamic_form_response injetado no state")

        state['messages'].append({
            'role': 'user',
            'content': request.body_message
        })

        if state.get('voice_review_pending'):
            from src.services.voice_chat_handoff import review_reply
            review_text = review_reply(state, request.body_message or '')
            state_manager.update_state(request.chat_id, state)
            if review_text:
                await zellu_client.send_message(
                    chat_id=request.chat_id, message=review_text,
                    is_finished=False, dispatch_id=getattr(request, 'dispatch_id', None),
                )
                _record_callback_delivery(_dispatch_dedupe_key(
                    getattr(request, 'id', None), getattr(request, 'group_metadata', None), request.chat_id
                ), True)
                return MessageResponse(
                    id=str(uuid4()), chat_id=request.chat_id, message=review_text,
                    agent='intake', completed=False, timestamp=datetime.now(),
                )

        # Snapshot ANTES do orchestrator: o process() pode mutar o state no
        # lugar, entao guardamos os sinais que o handType compara depois.
        had_problem_before = bool(state.get('problem_description'))
        had_company_confirmed_before = bool(state.get('company_confirmed'))

        updated_state = await orchestrator.process(state)

        state_manager.update_state(request.chat_id, updated_state)

        # ===================================================================
        # BACKGROUND: Busca de contatos da empresa via Firecrawl
        # Disparado quando intake_agent sinaliza contact_search_pending=True
        # ===================================================================
        if updated_state.get('contact_search_pending'):
            _cnpj = updated_state.get('opposing_party_cnpj')
            _company_name = updated_state.get('opposing_party_name', '')
            if _cnpj and _cnpj not in _contact_search_in_progress:
                _contact_search_in_progress.add(_cnpj)
                asyncio.create_task(
                    _background_search_company_contacts(_cnpj, _company_name, request.chat_id)
                )
                print(f"[MAIN] Background contact search disparado para {_company_name} (CNPJ={_cnpj})")
            # Limpar flag do state
            updated_state['contact_search_pending'] = False
            state_manager.update_state(request.chat_id, {'contact_search_pending': False})

        is_finished = updated_state.get('completed', False)
        analysis_data = None
        group_messages = None

        if is_finished:
            print(f"[MAIN] FLUXO COMPLETO! completed=True detectado")
            analysis_data = _build_analysis_data_from_state(updated_state)
            rights_count = len(analysis_data.get('rights') or [])
            print(f"[MAIN] analysis_data montado: {rights_count} direitos identificados")

            # Documentos (contrato vigente:
            #   docs/20260618-contrato-documentos-template-fiel.md)
            #
            # Modelo TEMPLATE-DRIVEN: cada doc e um LegalDocumentPayload
            # (type + templateVersion + slots + sections + flags). A IA preenche
            # as lacunas; o texto fixo e o layout sao da plataforma.
            #
            # IA entrega 3 documentos (amigavel + extrajudicial + judicial)
            # para o cliente poder escolher qualquer um na tela de analise.
            # Mantemos tambem analysis_data['document'] singular como fallback
            # retrocompatibilidade que o backend ainda le.
            try:
                from src.services.document_content_builder import (
                    build_all_documents,
                    refine_case_details,
                )
                documents_list, recommended_doc = await build_all_documents(
                    updated_state,
                    recommendations=analysis_data.get('recommendations') or [],
                )
                if documents_list:
                    analysis_data['documents'] = documents_list
                    # O Modelo 1 (Solicitacao de Resolucao Amigavel) e montado
                    # pela plataforma a partir de caseDetails, nao de
                    # documents[]. Aproveita o relato e os pedidos que a LLM ja
                    # escreveu para a notificacao - achados 4.2 e 4.4.
                    refine_case_details(analysis_data['caseDetails'], documents_list)
                    print(
                        f"[MAIN] documents[] montado: {len(documents_list)} tipo(s) - "
                        + ", ".join(d.get('type', '?') for d in documents_list)
                    )
                if recommended_doc:
                    analysis_data['document'] = recommended_doc
                    print(
                        f"[MAIN] document singular (fallback): type={recommended_doc.get('type')}, "
                        f"legalBasis={len(recommended_doc.get('legalBasis') or [])}, "
                        f"requests={len(recommended_doc.get('requests') or [])}"
                    )
                if not documents_list and not recommended_doc:
                    print(f"[MAIN] documents NAO montados (backend aplicara fallback)")
            except Exception as exc:
                # Nao derruba o fluxo: backend tem fallback hardcoded
                print(f"[MAIN] erro ao montar documents (segue sem): {exc}")

            client_name = updated_state.get('client_name', 'Cliente')
            first_name = client_name.split()[0] if client_name else 'Cliente'

            # "Estou analisando..." ja foi enviado cedo (antes de classify/analyze).
            messages = [
                f"Perfeito, {first_name}! Recebi todas as informacoes.",
                "Pronto! Seu chamado foi criado com sucesso. Nossa equipe jurídica entrará em contato em até 48 horas."
            ]
            delays = [0, 1500]

            # handType por bolha: snap so na ultima (entrega do resultado).
            # Ver docs/20260728-contrato-handtype-avatar-animado.md
            hand_types = [None, SNAP]

            group_messages = zellu_client.build_group_messages(
                messages, delays, analysis_data, hand_types=hand_types
            )
            last_message = " ".join(messages)

            print(f"[MAIN] Payload final montado:")
            print(f"  - group_messages: {len(group_messages)} mensagens")
            print(f"  - analysis_data.rights: {rights_count}")
            print(f"  - analysis_data.estimatedValue: {analysis_data.get('estimatedValue', 0)}")
        else:
            last_message = ""
            for msg in reversed(updated_state['messages']):
                if msg['role'] == 'assistant':
                    last_message = msg['content']
                    break

        # Montar messagedata se email já existe (userExists para modal de login)
        messagedata = None
        if updated_state.get('email_exists') and updated_state.get('client_email'):
            messagedata = {
                "userExists": True,
                "userEmail": updated_state['client_email'],
                "hasPreRegistration": False
            }
            print(f"[MAIN] Email já existe na base! Enviando messagedata.userExists=True para email={updated_state['client_email']}")

        # Incluir event_type no messagedata se agente gerou formulário dinâmico
        has_form = bool(updated_state.get('pending_form_event_type'))
        if has_form:
            messagedata = messagedata or {}
            messagedata['event_type'] = updated_state['pending_form_event_type']
            # Limpar do state após capturar para envio
            updated_state['pending_form_event_type'] = None
            state_manager.update_state(request.chat_id, {'pending_form_event_type': None})
            print(f"[MAIN] event_type incluido no messagedata: form={messagedata['event_type'].get('form_name', 'unknown')}")

        # Maozinha animada do avatar (opcional - omitir cai na mao padrao).
        # No fluxo de fechamento o handType ja foi definido por bolha em
        # group_messages, entao aqui so resolvemos o caso de mensagem unica.
        if not group_messages:
            hand_type = resolve_hand_type(
                is_finished=is_finished,
                has_form=has_form,
                agreement_reached=(
                    bool(updated_state.get('company_confirmed'))
                    and not had_company_confirmed_before
                ),
                problem_just_described=(
                    bool(updated_state.get('problem_description'))
                    and not had_problem_before
                ),
                user_message=request.body_message,
            )
            if hand_type:
                messagedata = messagedata or {}
                messagedata['handType'] = hand_type
                print(f"[MAIN] handType incluido no messagedata: {hand_type}")

        # Contrato aditivo: o front pode abrir seu picker existente e mostrar
        # status por arquivo. Nunca enviar texto de laudo ou URL privada aqui.
        from src.services.case_evidence import evidence_metadata, evidence_receipt
        messagedata = messagedata or {}
        messagedata["evidenceUpload"] = evidence_metadata(updated_state)
        from src.services.client_intake_record import client_details_required
        if updated_state.get("client_details_confirmed") or (
                not client_details_required() and updated_state.get("company_confirmed")
                and updated_state.get("case_evidence")):
            from src.services.client_intake_record import save_record
            messagedata["intakeRecord"] = save_record(updated_state)
        receipt = evidence_receipt(updated_state)
        if receipt:
            last_message = (last_message + "\n\n" + receipt).strip()
            if group_messages:
                group_messages[-1]["message"] = (group_messages[-1].get("message", "") + "\n\n" + receipt).strip()
        if group_messages:
            for group_message in group_messages:
                group_message.setdefault("messagedata", {})["evidenceUpload"] = messagedata["evidenceUpload"]
                if "intakeRecord" in messagedata:
                    group_message["messagedata"]["intakeRecord"] = messagedata["intakeRecord"]

        from src.services.chat_completion import prepare_completion
        last_message, is_finished, group_messages = prepare_completion(
            last_message, is_finished, analysis_data, group_messages
        )
        if not is_finished and updated_state.get('completed'):
            state_manager.update_state(request.chat_id, {
                'completed': False, 'current_agent': 'intake',
                'ready_for_classification': False, 'validated': False,
            })

        # Chave de entrega: a MESMA chave de dedupe do despacho que originou
        # esta resposta (contrato 20260807) - single pelo `id`, batch pelos
        # group_metadata.messageIds. Cai no chat_id quando a chamada nao tem
        # nenhum dos dois (ex: POST /message direto).
        message_key = _dispatch_dedupe_key(
            getattr(request, "id", None),
            getattr(request, "group_metadata", None),
            request.chat_id,
        )

        # Eco do despacho no callback (contrato 20260807, item 1)
        dispatch_id = getattr(request, "dispatch_id", None)

        if last_message or group_messages:
            try:
                print(f"[ZELLU] Enviando mensagem - chat_id={request.chat_id}, is_finished={is_finished}")

                await zellu_client.send_message(
                    chat_id=request.chat_id,
                    message=last_message,
                    is_finished=is_finished,
                    analysis_data=analysis_data,
                    group_messages=group_messages,
                    messagedata=messagedata,
                    dispatch_id=dispatch_id
                )

                _record_callback_delivery(message_key, True)

                if is_finished:
                    print(f"[ZELLU] Chamado criado! is_finished=true enviado com sucesso")
                else:
                    print(f"[ZELLU] Mensagem enviada - chat_id={request.chat_id}")

            except Exception as e:
                # Retries ja foram esgotados dentro do ZelluClient. Marcamos a
                # entrega como falha para que o dedupe NAO memoize esta mensagem
                # e uma eventual reentrega do app possa reproduzir a resposta.
                _record_callback_delivery(message_key, False)
                print(f"[ZELLU] ERRO ao enviar para frontend (apos retries): {e}")
        else:
            # Nada a entregar: nao ha callback pendente, entao segue como entregue
            # para o dedupe funcionar normalmente.
            _record_callback_delivery(message_key, True)
            print(f"[ZELLU] SKIP - Nenhuma mensagem para enviar - chat_id={request.chat_id}")

        response = MessageResponse(
            id=str(uuid4()),
            chat_id=request.chat_id,
            message=last_message,
            agent=updated_state.get('current_agent', 'intake'),
            completed=updated_state.get('completed', False),
            timestamp=datetime.now()
        )

        return response

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error processing message: {str(e)}"
        )


# =============================================================================
# API ENDPOINTS (inline - producao)
# =============================================================================

from fastapi import Header, Depends, UploadFile, File
from fastapi.responses import JSONResponse
from api.schemas import (
    MessageRequest,
    MessageResponse,
    ConversationStateResponse,
    EmpresaBaseDadosRequest,
    EmpresaBaseDadosResponse
)


def verify_api_key(x_api_key: str = Header(..., alias="x-api-key")):
    """Verify API key for protected endpoints."""
    if not _api_key_matches(x_api_key, settings.API_KEY_ZELLU_IA):
        raise HTTPException(status_code=401, detail="Nao autorizado")
    return x_api_key


# -----------------------------------------------------------------------------
# ENDPOINT PRINCIPAL v2 - /api/company/base-dados e /api/webhook/process
# -----------------------------------------------------------------------------
# NOTA: A funcao de criacao de mensagem do ZelinhU foi movida para
# src/utils/zelinhu_messages.py (modulo centralizado)
# -----------------------------------------------------------------------------


# =============================================================================
# IDEMPOTENCIA DO TURNO DA IA DA EMPRESA (diagnostico de homolog, 23/08/2026)
# =============================================================================
# O backend retenta /api/company/base-dados ate 3x quando estoura o timeout de
# 60s (COMPANY_RESPONSE_WEBHOOK_MAX_RETRIES). Sem dedupe, cada retry refazia o
# turno inteiro de LLM e ainda reaplicava o livro-razao, o que gerava
# contradicao falsa - a IA chegou a acusar a outra parte de voltar atras com
# base num estado que nos mesmos corrompemos.
#
# O fluxo de chat deste mesmo arquivo ja resolvia isso (chat_locks +
# processed_messages + _dispatch_dedupe_key); o de negociacao nunca teve.
#
# Efeito colateral desejado: com a resposta em cache, o retry do backend recebe
# o resultado NA HORA em vez de disparar outro turno - o retry vira o mecanismo
# de entrega, nao mais carga.
_empresa_inflight = {}    # dedupe_key -> Future com (status_code, body)
_empresa_responses = {}   # dedupe_key -> (status_code, body) ja respondidos
_EMPRESA_RESPONSE_CACHE_MAX = 200


def _empresa_dedupe_key(raw_body: dict) -> str:
    """
    Chave estavel do turno: sessao + mensagem que estamos respondendo.

    `lastMessageId`/`triggeredByMessageId` identificam a mensagem do ZelinhU a
    que a resposta se refere e nao mudam entre as tentativas do backend.
    Sem eles nao ha como deduplicar com seguranca - melhor processar.
    """
    session_id = raw_body.get("sessionId") or raw_body.get("session_id") or ""
    message_id = raw_body.get("lastMessageId") or raw_body.get("triggeredByMessageId") or ""
    if not session_id or not message_id:
        return ""
    return f"{session_id}:{message_id}"


def _remember_empresa_response(key: str, payload) -> None:
    """Guarda a resposta do turno, com o dicionario limitado."""
    _empresa_responses[key] = payload
    while len(_empresa_responses) > _EMPRESA_RESPONSE_CACHE_MAX:
        _empresa_responses.pop(next(iter(_empresa_responses)))


def _is_cacheable_empresa_response(body: bytes) -> bool:
    """
    So memoriza turno de verdade.

    Respostas de curto-circuito (modo manual, marcador terminal do Sender)
    dependem de estado que pode mudar entre uma tentativa e outra - memorizar
    faria o retry receber um "pulei" que ja nao vale mais.
    """
    try:
        import json as _json
        data = _json.loads(body)
    except Exception:  # noqa: BLE001
        return False

    if data.get("skipped") or data.get("awaitingContext"):
        return False
    return bool((data.get("messageBody") or "").strip())


async def _process_empresa_request_deduped(
    empresa_request: "EmpresaBaseDadosRequest",
    endpoint_name: str,
    dedupe_key: str,
):
    """
    Executa o turno UMA unica vez por mensagem.

    - resposta ja pronta  -> devolve na hora, sem LLM
    - turno em andamento  -> espera o mesmo turno (single-flight) e devolve o
                             mesmo resultado, em vez de comecar outro
    - nenhum dos dois     -> processa e memoriza
    """
    from starlette.responses import Response as _RawResponse

    if not dedupe_key:
        return await _process_empresa_request(empresa_request, endpoint_name)

    cached = _empresa_responses.get(dedupe_key)
    if cached is not None:
        print(f"[EMPRESA-API] Turno ja respondido para {dedupe_key} - devolvendo resposta memorizada")
        return _RawResponse(content=cached[1], status_code=cached[0], media_type="application/json")

    inflight = _empresa_inflight.get(dedupe_key)
    if inflight is not None:
        print(f"[EMPRESA-API] Turno de {dedupe_key} ja em andamento - aguardando o mesmo resultado")
        status_code, body = await inflight
        return _RawResponse(content=body, status_code=status_code, media_type="application/json")

    future = asyncio.get_running_loop().create_future()
    # Evita "Future exception was never retrieved" quando ninguem esta esperando
    future.add_done_callback(lambda f: f.cancelled() or f.exception())
    _empresa_inflight[dedupe_key] = future

    try:
        response = await _process_empresa_request(empresa_request, endpoint_name)
        payload = (getattr(response, "status_code", 200), bytes(getattr(response, "body", b"")))

        if _is_cacheable_empresa_response(payload[1]):
            _remember_empresa_response(dedupe_key, payload)

        if not future.done():
            future.set_result(payload)
        return response

    except Exception as e:
        if not future.done():
            future.set_exception(e)
        raise
    finally:
        _empresa_inflight.pop(dedupe_key, None)


# =============================================================================
# LIVRO-RAZAO DA NEGOCIACAO - LADO DA EMPRESA (Fase 2)
# =============================================================================
# Em memoria, como o resto do estado de negociacao do servico. Persistencia em
# banco continua pendente: se o processo reiniciar no meio de uma negociacao, o
# livro-razao recomeca vazio e volta a crescer a partir da rodada atual.
_negotiation_ledgers = {}  # sessionId -> ledger serializado

# Serializa o turno do livro-razao por sessao. Sem isso, duas requisicoes
# concorrentes faziam leitura-modificacao-escrita em cima do mesmo estado e a
# segunda sobrescrevia a primeira - foi o que produziu a contradicao falsa no
# log de homolog de 23/08/2026.
_ledger_locks = defaultdict(asyncio.Lock)

# Mensagens ja aplicadas ao livro-razao, por sessao. Segunda trava: mesmo que a
# mesma mensagem chegue duas vezes, ela so muda o estado uma vez.
_ledger_applied = {}  # sessionId -> lista dos ultimos messageIds aplicados
_LEDGER_APPLIED_MAX = 20


def _ledger_already_applied(session_id: str, message_id: str) -> bool:
    if not message_id:
        return False
    return message_id in _ledger_applied.get(session_id, [])


def _mark_ledger_applied(session_id: str, message_id: str) -> None:
    if not message_id:
        return
    aplicadas = _ledger_applied.setdefault(session_id, [])
    aplicadas.append(message_id)
    del aplicadas[:-_LEDGER_APPLIED_MAX]


def plano_de_consulta_a_base(
    tem_base: bool,
    busca_previa_trouxe_algo: bool,
    orcamento_padrao: int,
) -> tuple:
    """
    Decide se a tool de busca vai para a mao do modelo, e com que orcamento.

    Cada rodada de consulta e um roundtrip inteiro de LLM. No log de 25/08/2026 a
    IA da Empresa gastou 5 `search_knowledge_base` num turno de 50s, todos contra
    "[RAG-INDEXER] Busca retornou 0 resultados".

    - Sem base indexada: a tool nao pode achar nada. Nao e oferecida. Antes so
      pediamos no prompt que nao chamasse - e o modelo chamava mesmo assim.
    - Base declarada, mas a busca previa (mesmo indice, texto da propria mensagem
      como query) voltou vazia: a tool CONTINUA disponivel, porque o indice pode
      ter conteudo de outro assunto e tirar a ferramenta seria decidir pelo
      modelo. O que muda e o orcamento: uma rodada, nao duas.
    - Base com resposta para o caso: orcamento normal.

    Returns:
        (oferecer_tools, orcamento_de_rodadas)
    """
    if not tem_base:
        return False, 0

    if not busca_previa_trouxe_algo:
        return True, min(1, orcamento_padrao)

    return True, orcamento_padrao


async def _run_ledger_turn(
    session_id: str,
    last_client_message: str,
    message_history: list,
    context: dict,
    model: str,
    temperature: float,
    negotiation=None,
    last_message_id: str = "",
) -> str:
    """
    Passos 1 e 2 do turno da IA da Empresa.

    Passo 1: uma chamada com saida estruturada le a mensagem do ZelinhU e diz
             como cada item da negociacao muda. Nao redige nada.
    Passo 2: codigo puro compara com o que ja estava acordado e acusa
             contradicao.

    Returns:
        Bloco de texto (livro-razao + validacao) para somar ao system prompt.
        String vazia se nao houver nada a dizer.
    """
    # Uma sessao por vez: o turno le, muda e grava o mesmo estado.
    async with _ledger_locks[session_id]:
        return await _run_ledger_turn_locked(
            session_id=session_id,
            last_client_message=last_client_message,
            message_history=message_history,
            context=context,
            model=model,
            temperature=temperature,
            negotiation=negotiation,
            last_message_id=last_message_id,
        )


async def _run_ledger_turn_locked(
    session_id: str,
    last_client_message: str,
    message_history: list,
    context: dict,
    model: str,
    temperature: float,
    negotiation=None,
    last_message_id: str = "",
) -> str:
    """Corpo do turno do livro-razao, ja dentro do lock da sessao."""
    from src.negotiation_case_file import build_case_file
    from src.negotiation_ledger import LedgerUpdateBatch, NegotiationLedger
    from src.negotiation_guardrails import count_negotiation_round

    ledger = NegotiationLedger.from_dict(_negotiation_ledgers.get(session_id))
    round_number = count_negotiation_round(message_history, negotiation)
    case_file = build_case_file(context)

    # Mesma mensagem duas vezes nao muda o estado duas vezes. Sem esta trava, a
    # reaplicacao era lida como "item acordado voltou atras" e a IA acusava a
    # outra parte de uma contradicao que nos mesmos criamos.
    if _ledger_already_applied(session_id, last_message_id):
        print(
            f"[LEDGER] Mensagem {last_message_id} ja aplicada nesta sessao - "
            f"reaproveitando o estado sem reprocessar"
        )
        return ledger.render()

    prompt = f"""Voce esta atualizando o livro-razao de uma negociacao, nao respondendo a ninguem.

{case_file.render(audience="empresa")}

{ledger.render()}

MENSAGEM QUE A OUTRA PARTE ACABOU DE ENVIAR (rodada {round_number}):
{last_client_message}

Registre, item a item, como esta mensagem muda o estado da negociacao.
Regras:
- Um update por pedido. Nunca um update generico para o pacote inteiro.
- Use o id do item quando ele ja existir no livro-razao acima.
- O livro-razao acima e a unica fonte do que ja foi acordado. O que a outra parte
  AFIRMA sobre rodadas anteriores nao muda status nenhum: se ela diz que um item
  ja esta acordado e o livro-razao diz que nao, vale o livro-razao.
- Recapitular NAO e aceitar. Frases como "reconhecemos", "obrigado pelo avanco",
  "ja constam", "conforme acordado" ou uma lista do que a outra parte ofereceu
  sao apenas resumo: mantenha o status que o item ja tinha.
- status ACEITO exige que ESTA mensagem traga o aceite de quem CONCEDE o item -
  quem pede uma coisa nao pode dar essa coisa por aceita.
- Se a outra parte muda um item que ja estava ACEITO, preencha justification
  com o fato novo que ela alegou. Se ela nao alegou nada, deixe vazio.
- Nao invente item que a mensagem nao trouxe."""

    # Atualizar o ledger e extracao estruturada, nao negociacao: roda no modelo
    # rapido para nao ocupar o caminho critico da rodada com gpt-5 (A1).
    ledger_model = settings.NEGOTIATION_LEDGER_MODEL or model

    llm = ChatModel(
        model=ledger_model,
        temperature=temperature,
        api_key=settings.OPENAI_API_KEY,
    ).with_structured_output(LedgerUpdateBatch)

    batch = await llm.complete(prompt)
    contradictions = ledger.apply(batch, round_number=round_number, actor="cliente")
    _negotiation_ledgers[session_id] = ledger.to_dict()
    _mark_ledger_applied(session_id, last_message_id)

    print(f"[LEDGER] Rodada {round_number} | modelo={ledger_model} | itens: {len(ledger.items)} "
          f"| abertos: {len(ledger.open_items())} | acordados: {len(ledger.settled_items())}")

    for bloqueado in ledger.blocked_settlements:
        print(f"[LEDGER] Aceite recusado: {bloqueado}")

    blocks = [ledger.render()]

    if contradictions:
        print(f"[LEDGER] Contradicoes detectadas: {contradictions}")
        linhas = ["# RELATORIO DE VALIDACAO (PASSO 2)", ""]
        linhas.append(
            "A mensagem da outra parte contradiz o que ja estava acordado. "
            "Aponte isso na sua resposta em vez de acompanhar a mudanca:"
        )
        linhas.extend(f"- {c}" for c in contradictions)
        blocks.append("\n".join(linhas))

    return "\n\n".join(blocks)




# =============================================================================
# BASE DOCUMENTAL OBRIGATORIA DA NEGOCIACAO
# =============================================================================
# A negociacao automatica so pode ocorrer quando existem DUAS bases verificadas:
#   1. documentos especificos do caso (laudo, peticao, contrato, NF, anexos etc.);
#   2. politicas da empresa recuperadas pela base de conhecimento/RAG.
#
# Ticket, descricao e conhecimento geral do modelo sao contexto auxiliar. Eles
# nunca substituem um arquivo do caso que existe mas nao foi lido.

_CASE_DOCUMENT_KEYS = (
    "caseDocuments", "case_documents", "caseFiles", "case_files",
    "attachments", "files", "documents", "evidence", "evidenceFiles",
    "evidence_files", "judicialDocuments", "judicial_documents",
)


def _document_obj_to_dict(value: Any) -> dict:
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump(exclude_none=True)
        except Exception:
            pass
    if hasattr(value, "dict"):
        try:
            return value.dict(exclude_none=True)
        except Exception:
            pass
    try:
        return {k: v for k, v in vars(value).items() if not k.startswith("_")}
    except Exception:
        return {}


def _looks_like_case_file(value: Any) -> bool:
    """Evita confundir lista de documentos *necessarios* com arquivos reais."""
    if isinstance(value, str):
        return value.startswith(("http://", "https://"))
    data = _document_obj_to_dict(value)
    if not data:
        return False
    file_signals = (
        "fileName", "filename", "fileType", "file_type", "mimeType", "mime_type",
        "minioPath", "minio_path", "url", "downloadUrl", "download_url",
        "presignedUrl", "presigned_url", "text", "extractedText", "extracted_text",
        "ocrText", "ocr_text", "content",
    )
    return any(data.get(key) not in (None, "", [], {}) for key in file_signals)


def _flatten_file_values(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        out = []
        for item in value:
            out.extend(_flatten_file_values(item))
        return out
    if isinstance(value, str):
        if value.startswith(("http://", "https://")):
            name = value.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1] or "Documento do caso"
            return [{"url": value, "fileName": name}]
        return []

    data = _document_obj_to_dict(value)
    if not data:
        return []

    # Alguns contratos embrulham a lista em items/files/attachments/documents.
    for wrapper in ("items", "attachments", "files", "documents"):
        wrapped = data.get(wrapper)
        if isinstance(wrapped, list) and not _looks_like_case_file(data):
            return _flatten_file_values(wrapped)

    return [data] if _looks_like_case_file(data) else []


def _collect_case_document_candidates(request: Any, context: dict, message_history: list) -> list:
    """Coleta anexos do caso sem misturar os knowledgeBases/politicas da empresa."""
    candidates = []

    # Campos de primeira classe do schema, quando existirem.
    for key in _CASE_DOCUMENT_KEYS:
        candidates.extend(_flatten_file_values(getattr(request, key, None)))

    # Contexto do chamado. O backend pode colocar anexos na raiz, no ticket ou
    # em caseDetails dependendo da versao do contrato.
    containers = [context, context.get("ticket") or {}]
    ticket = context.get("ticket") or {}
    if isinstance(ticket, dict):
        containers.append(ticket.get("caseDetails") or {})
        containers.append(ticket.get("case_details") or {})

    for container in containers:
        if not isinstance(container, dict):
            continue
        for key in _CASE_DOCUMENT_KEYS:
            candidates.extend(_flatten_file_values(container.get(key)))

    # Arquivos anexados a mensagens durante a propria negociacao tambem passam a
    # fazer parte do dossie a partir daquele turno.
    for msg in message_history or []:
        data = _document_obj_to_dict(msg)
        for key in ("attachments", "files", "documents"):
            candidates.extend(_flatten_file_values(data.get(key)))

    # Dedup estavel por id/caminho/url/nome. Mantemos a primeira representacao.
    deduped = []
    seen = set()
    for item in candidates:
        data = _document_obj_to_dict(item)
        identity = str(
            data.get("id")
            or data.get("fileId")
            or data.get("file_id")
            or data.get("minioPath")
            or data.get("minio_path")
            or data.get("url")
            or data.get("downloadUrl")
            or data.get("presignedUrl")
            or data.get("fileName")
            or data.get("filename")
            or data.get("title")
            or repr(sorted(data.items()))
        )
        if identity in seen:
            continue
        seen.add(identity)
        deduped.append(data)
    return deduped


async def _build_case_document_context(request: Any, context: dict, message_history: list) -> tuple[str, list, list]:
    """Le TODOS os anexos detectados. Um arquivo ilegivel deixa a base incompleta."""
    candidates = _collect_case_document_candidates(request, context, message_history)
    if not candidates:
        return "", [], [{"title": "Dossie do caso", "error": "Nenhum arquivo do caso foi recebido"}]

    if not doc_extractor_instance:
        return "", [], [{"title": "Dossie do caso", "error": "Processador de documentos indisponivel"}]

    processed = []
    failures = []
    for attachment in candidates:
        result = await doc_extractor_instance.process_attachment(attachment)
        if result.get("ok") and (result.get("text") or "").strip():
            processed.append(result)
        else:
            failures.append(result)

    if failures:
        return "", processed, failures

    blocks = [
        "# DOCUMENTOS OBRIGATORIOS DO CASO",
        "Os arquivos abaixo pertencem a ESTE caso e sao evidencia primaria. "
        "Toda proposta, aceite, contraproposta, recusa, valor, prazo ou condicao "
        "deve ser compativel com eles. Nao invente informacao ausente e nao "
        "substitua estes documentos pela descricao do ticket ou por conhecimento geral.",
    ]
    for index, doc in enumerate(processed, start=1):
        title = doc.get("title") or f"Documento {index}"
        doc_id = doc.get("id") or "sem-id"
        file_type = doc.get("file_type") or "arquivo"
        blocks.append(
            f"## DOCUMENTO {index}: {title} [{file_type}] [id={doc_id}]\n"
            f"{(doc.get('text') or '').strip()}"
        )

    return "\n\n".join(blocks), processed, []


def _document_basis_block_response(request: Any, document_failures: list, policy_error: str = ""):
    """Fail closed: sem dossie legivel + politicas recuperadas nao ha negociacao automatica."""
    errors = []
    for failure in document_failures or []:
        title = failure.get("title") or "Documento"
        detail = failure.get("error") or "nao foi possivel ler"
        if failure.get("needs_ocr"):
            detail += " (OCR necessario)"
        errors.append(f"{title}: {detail}")
    if policy_error:
        errors.append(policy_error)

    last_message_id = request.lastMessageId or request.triggeredByMessageId
    text = (
        "A negociacao automatica foi pausada porque a base documental obrigatoria "
        "do caso ou as politicas da empresa nao puderam ser verificadas. "
        "E necessario disponibilizar/processar os arquivos pendentes antes de "
        "formular ou aceitar qualquer proposta."
    )
    return JSONResponse(
        status_code=200,
        content={
            "messageBody": text,
            "messageType": "text",
            "tokensUsed": 0,
            "modelUsed": "none",
            "negotiationComplete": False,
            "requiresHumanApproval": True,
            "documentBasisReady": not bool(document_failures),
            "policyContextReady": not bool(policy_error),
            "basisErrors": errors,
            "messagedata": {"replyToMessageId": last_message_id} if last_message_id else {},
        },
    )


async def _process_empresa_request(request: EmpresaBaseDadosRequest, endpoint_name: str) -> EmpresaBaseDadosResponse:
    """
    Logica principal para processar mensagens da Zellu.

    Recebe o payload completo com:
    - context (ticket, client, company)
    - instructions (5 campos estruturados)
    - messageHistory
    - knowledgeBases + documents
    - rules (temporarias)
    - config

    Retorna a resposta da IA.
    """
    from src.llm import system_message, user_message, assistant_message
    from src.prompt_generator import build_system_prompt
    from models.company import CompanyInstructions, TemporaryRule
    from datetime import datetime as dt
    import unicodedata

    # Compatibilidade: aceita tanto camelCase quanto snake_case
    session_id = request.sessionId
    message_history = request.messageHistory
    knowledge_bases = request.knowledgeBases

    print(f"[API] POST {endpoint_name} - sessionId={session_id}")

    # =========================================================================
    # VERIFICACAO: Modo manual ativo (humano assumiu o controle)
    # - Chat Empresa: manualModeActive=true → IA NAO responde
    # - Teste IA (ai_tester): manualModeActive=true → IA CONTINUA respondendo
    #   (no Teste IA, o manual controla o ZelinhU, nao a IA da Empresa)
    # =========================================================================
    if request.negotiation and request.negotiation.manualModeActive:
        ticket_status = (request.context or {}).get("ticket", {}).get("status", "")
        is_ai_tester = ticket_status == "ai_tester"

        if not is_ai_tester:
            print(f"[API] MODO MANUAL ATIVO - sessionId={session_id}. IA nao deve responder.")
            return JSONResponse(
                status_code=200,
                content={
                    "messageBody": "",
                    "messageType": "text",
                    "tokensUsed": 0,
                    "modelUsed": "none",
                    "negotiationComplete": False,
                    "manualModeActive": True,
                    "skipped": True,
                    "reason": "Modo manual ativo. IA aguardando humano devolver controle."
                }
            )
        else:
            print(f"[API] Modo manual ativo mas ai_tester=true - IA continua respondendo.")

    # =========================================================================
    # CURTO-CIRCUITO: marcadores terminais do Sender (ZelinhU)
    # Quando o ZelinhU emite [PROPOSTA PARA APROVACAO DO CLIENTE] ou
    # [NEGOCIACAO ENCERRADA - ESCALADO], a negociacao entre as IAs acabou -
    # cabe ao cliente (ou via escalonamento) decidir. Se respondessemos com
    # negotiationComplete=false, o backend reencaminharia a mensagem para a IA
    # Empresa e cairiamos em loop infinito (sender re-analisa -> send_to_client
    # de novo -> ...). Devolvemos uma confirmacao curta com
    # negotiationComplete=true para o backend disparar markSessionAsAgreement.
    # =========================================================================
    SENDER_TERMINAL_MARKERS = {
        "[PROPOSTA PARA APROVACAO DO CLIENTE]": (
            "Entendido. Aguardamos o retorno do cliente sobre a proposta apresentada."
        ),
        "[NEGOCIACAO ENCERRADA - ESCALADO]": (
            "Compreendemos a decisao. Permanecemos a disposicao nos canais formais."
        ),
    }

    last_zelinhu_body = ""
    for _msg in reversed(message_history or []):
        if _msg.origin == "zelinhu":
            last_zelinhu_body = (_msg.body or "").upper()
            break

    for marker, reply_body in SENDER_TERMINAL_MARKERS.items():
        if marker in last_zelinhu_body:
            print(f"[API] Marcador terminal do Sender detectado: {marker} - encerrando com negotiationComplete=true")
            last_message_id = request.lastMessageId or request.triggeredByMessageId
            short_circuit = {
                "messageBody": reply_body,
                "messageType": "text",
                "tokensUsed": 0,
                "modelUsed": "none",
                "negotiationComplete": True,
                "messagedata": {"replyToMessageId": last_message_id} if last_message_id else {},
            }
            return JSONResponse(content=short_circuit)

    # 1. Extrair contexto
    context = request.context or {}
    if not isinstance(context, dict):
        context = _document_obj_to_dict(context)

    # 1.1 Ler o dossie especifico do caso ANTES de qualquer decisao de
    # negociacao. Laudo/peticao/contrato/NF/anexos nao sao opcionais: se existe
    # arquivo e ele nao pode ser lido, o turno entra em fail-closed.
    case_document_context, case_documents, case_document_failures = await _build_case_document_context(
        request, context, message_history
    )
    if case_documents:
        context = dict(context)
        context["caseDocumentsExtracted"] = case_documents
        print(
            f"[DOCUMENT-BASIS] {len(case_documents)} documento(s) do caso lido(s); "
            f"falhas={len(case_document_failures)}"
        )
    else:
        print(f"[DOCUMENT-BASIS] Nenhum documento do caso ficou disponivel para o modelo")

    # O contexto que o Sender nao consegue buscar (404 do backend) chega inteiro
    # aqui. Incluimos tambem os documentos ja extraidos, para as duas pontas
    # partirem do mesmo dossie factual.
    try:
        absorb_session_context(request.sessionId, context)
    except Exception as e:  # noqa: BLE001
        print(f"[API] Nao foi possivel repassar o contexto para a sessao do Sender: {e}")

    client_name = context.get("client", {}).get("name", "Cliente")
    company_id = context.get("company", {}).get("id") or request.companyId or "unknown"
    company_name = context.get("company", {}).get("tradeName") or context.get("company", {}).get("name", "Empresa")

    print(f"[API] Empresa: {company_name} (ID: {company_id})")
    print(f"[API] Cliente: {client_name}")

    # DEBUG: Ver o que chegou em instructions
    print(f"[DEBUG] request.instructions = {request.instructions}")
    print(f"[DEBUG] company_id do chat = {company_id}")

    # =========================================================================
    # 2. BUSCAR SYSTEM PROMPT DO SUPABASE (PRIORIDADE 1)
    # =========================================================================
    system_prompt_from_supabase = None
    if zellu_api_client:
        try:
            prompt_data = await zellu_api_client.get_system_prompt(company_id)
            if prompt_data and prompt_data.get("prompt"):
                system_prompt_from_supabase = prompt_data.get("prompt")
                print(f"[API] System prompt obtido do SUPABASE ({len(system_prompt_from_supabase)} chars)")
        except Exception as e:
            print(f"[API] Erro ao buscar prompt do Supabase: {e}")

    # =========================================================================
    # 3. Se nao tem no Supabase, montar em tempo real (fallback)
    # =========================================================================
    # Prioridade: 1) Supabase, 2) payload, 3) store (webhook), 4) defaults
    if request.instructions:
        # Usar instructions do payload
        instructions = CompanyInstructions(
            company_id=company_id,
            persona=request.instructions.persona,
            rules=request.instructions.rules,
            legal=request.instructions.legal,
            budget=request.instructions.budget,
            tone=request.instructions.tone
        )
        print(f"[API] Instructions do PAYLOAD disponivel para {company_name}")

    elif company_id in company_instructions_store:
        # Buscar instructions armazenadas (recebidas via webhook)
        stored = company_instructions_store[company_id]
        instructions = CompanyInstructions(
            company_id=company_id,
            persona=stored.get("persona", ""),
            rules=stored.get("rules", ""),
            legal=stored.get("legal", ""),
            budget=stored.get("budget", ""),
            tone=stored.get("tone", "profissional")
        )
        print(f"[API] Instructions do WEBHOOK STORE disponivel para {company_name}")

    else:
        # Tentar buscar instructions via API da Zellu (fallback)
        instructions = None
        if zellu_api_client:
            try:
                api_instructions = await zellu_api_client.get_company_instructions(company_id)
                if api_instructions and api_instructions.get("instructions"):
                    instr_data = api_instructions.get("instructions")
                    instructions = CompanyInstructions(
                        company_id=company_id,
                        persona=instr_data.get("persona", ""),
                        rules=instr_data.get("rules", ""),
                        legal=instr_data.get("legal", ""),
                        budget=instr_data.get("budget", ""),
                        tone=instr_data.get("tone", "profissional")
                    )
                    # Armazenar no store para proximas requisicoes
                    company_instructions_store[company_id] = instr_data
                    print(f"[API] Instructions obtidas via API ZELLU para {company_name}")
            except Exception as e:
                print(f"[API] Erro ao buscar instructions via API: {e}")

        # Se ainda nao tem, usar defaults
        if not instructions:
            instructions = CompanyInstructions(
                company_id=company_id,
                persona=f"Voce e o assistente virtual da {company_name}. Seja cordial, profissional e objetivo.",
                rules="Responda de forma clara e objetiva. Nao prometa o que nao pode cumprir. Escale para humano se necessario.",
                legal="Siga as normas do CDC (Codigo de Defesa do Consumidor). Respeite prazos legais.",
                budget="Tente resolver dentro do valor estimado do ticket. Negocie de forma justa.",
                tone="profissional"
            )
            print(f"[API] AVISO: Usando DEFAULTS para {company_name}")

    # 3. Converter regras temporarias
    # Prioridade: 1) payload, 2) store (webhook), 3) API Zellu
    temp_rules = []
    rules_source = "PAYLOAD" if request.temporaryRules else None
    # Falha ao BUSCAR regras nao pode ser confundida com "a empresa nao tem
    # regras": no log de homolog o 401 virou "Nenhuma regra temporaria ativa" e
    # a IA negociou sem as regras da empresa sem ninguem perceber (QA-07).
    rules_fetch_error = None

    # Se nao tem regras no payload, tentar buscar do store ou API
    if not request.temporaryRules:
        # Verificar store primeiro
        if company_id in company_rules_store:
            # company_rules_store[company_id] e um dict {rule_id: rule_data}
            stored_rules = company_rules_store[company_id].values() if isinstance(company_rules_store[company_id], dict) else company_rules_store[company_id]
            for stored_rule in stored_rules:
                try:
                    valid_from = None
                    valid_until = None
                    if stored_rule.get("validFrom"):
                        valid_from = dt.fromisoformat(stored_rule["validFrom"].replace("Z", "+00:00"))
                    if stored_rule.get("validUntil"):
                        valid_until = dt.fromisoformat(stored_rule["validUntil"].replace("Z", "+00:00"))

                    temp_rules.append(TemporaryRule(
                        id=stored_rule.get("id", ""),
                        company_id=company_id,
                        content=stored_rule.get("content", ""),
                        description=stored_rule.get("description"),
                        valid_from=valid_from,
                        valid_until=valid_until,
                        is_paused=stored_rule.get("isPaused", False)
                    ))
                except Exception as e:
                    print(f"[API] Erro ao parsear regra do store: {e}")
            if temp_rules:
                rules_source = "WEBHOOK STORE"

        # Se store vazio, tentar API
        if not temp_rules and zellu_api_client:
            try:
                api_rules = await zellu_api_client.get_temporary_rules(company_id)
                if isinstance(api_rules, dict) and api_rules.get("error"):
                    rules_fetch_error = api_rules
                if api_rules and api_rules.get("rules"):
                    for api_rule in api_rules.get("rules", []):
                        try:
                            valid_from = None
                            valid_until = None
                            if api_rule.get("validFrom"):
                                valid_from = dt.fromisoformat(api_rule["validFrom"].replace("Z", "+00:00"))
                            if api_rule.get("validUntil"):
                                valid_until = dt.fromisoformat(api_rule["validUntil"].replace("Z", "+00:00"))

                            temp_rules.append(TemporaryRule(
                                id=api_rule.get("id", ""),
                                company_id=company_id,
                                content=api_rule.get("content", ""),
                                description=api_rule.get("description"),
                                valid_from=valid_from,
                                valid_until=valid_until,
                                is_paused=False  # API ja filtra as nao-pausadas
                            ))
                        except Exception as e:
                            print(f"[API] Erro ao parsear regra da API: {e}")
                    if temp_rules:
                        # Armazenar no store para proximas requisicoes
                        # Formato: {rule_id: rule_data}
                        company_rules_store[company_id] = {
                            rule.get("id", str(i)): rule
                            for i, rule in enumerate(api_rules.get("rules", []))
                        }
                        rules_source = "API ZELLU"
            except Exception as e:
                print(f"[API] Erro ao buscar regras temporarias via API: {e}")
    else:
        # Usar regras do payload
        for rule in request.temporaryRules:
            try:
                # Compatibilidade: aceita validFrom/validUntil ou valid_from/valid_until
                valid_from_str = rule.validFrom
                valid_until_str = rule.validUntil
                is_active = rule.isActive

                # Parsear datas (opcionais)
                valid_from = None
                valid_until = None
                if valid_from_str:
                    valid_from = dt.fromisoformat(valid_from_str.replace("Z", "+00:00"))
                if valid_until_str:
                    valid_until = dt.fromisoformat(valid_until_str.replace("Z", "+00:00"))

                temp_rules.append(TemporaryRule(
                    id=rule.id,
                    company_id=instructions.company_id,
                    content=rule.content,
                    description=rule.description,
                    valid_from=valid_from,
                    valid_until=valid_until,
                    is_paused=not is_active
                ))
            except Exception as e:
                print(f"[API] Erro ao parsear regra temporaria {rule.id}: {e}")

    if temp_rules:
        print(f"[API] {len(temp_rules)} regras temporarias carregadas ({rules_source})")
    elif rules_fetch_error:
        print(
            f"[API] ALERTA: negociando SEM as regras temporarias de {company_name}. "
            f"A busca FALHOU ({rules_fetch_error.get('error')}"
            f"{'/' + str(rules_fetch_error.get('status')) if rules_fetch_error.get('status') else ''})"
            f" - se a empresa tiver regras ativas, elas NAO foram aplicadas nesta resposta."
        )
    else:
        print(f"[API] Nenhuma regra temporaria ativa para {company_name}")

    # 4. Buscar ultima mensagem do cliente para consulta RAG
    last_user_message = ""
    for msg in reversed(message_history):
        if msg.origin == "zelinhu":
            last_user_message = msg.body
            break

    # 5. Consultar RAG das POLITICAS DA EMPRESA. Esta camada e obrigatoria na
    # negociacao: documento do caso sem politica, ou politica sem documento do
    # caso, nao autoriza proposta automatica.
    rag_context = ""
    policy_error = ""
    ticket_for_query = context.get("ticket") or {}
    rag_query = (
        last_user_message
        or ticket_for_query.get("description")
        or ticket_for_query.get("subject")
        or ticket_for_query.get("title")
        or ""
    )

    if not knowledge_bases:
        policy_error = "A empresa nao possui base de conhecimento/politicas disponivel para esta negociacao"
    elif not rag_indexer_instance:
        policy_error = "O indexador RAG das politicas da empresa esta indisponivel"
    elif not rag_query.strip():
        policy_error = "Nao existe texto do caso suficiente para consultar as politicas da empresa"
    else:
        try:
            rag_results = await rag_indexer_instance.search(
                company_id=company_id,
                query=rag_query,
                top_k=3
            )
            rag_chunks = []
            for doc in rag_results or []:
                title = doc.get("title") or doc.get("file_name", "Politica da empresa")
                content = (doc.get("content") or "").strip()
                if content:
                    rag_chunks.append(f"[{title}]: {content}")

            if rag_chunks:
                rag_context = (
                    "# POLITICAS DA EMPRESA RECUPERADAS PELO RAG\n"
                    "As politicas abaixo sao obrigatorias para esta rodada. A resposta nao pode "
                    "contradize-las nem substitui-las por conhecimento geral do modelo.\n\n"
                    + "\n\n".join(rag_chunks)
                )
                print(f"[RAG] Politicas recuperadas: {len(rag_chunks)} trecho(s) relevante(s)")
            else:
                policy_error = (
                    "A base de politicas foi consultada, mas nenhum trecho relevante foi recuperado "
                    "para este caso"
                )
        except Exception as e:
            policy_error = f"Falha ao consultar as politicas da empresa no RAG: {type(e).__name__}"
            print(f"[RAG] Erro na busca: {e}")

    # FAIL CLOSED DOCUMENTAL: a IA nao recebe permissao para negociar quando
    # faltar qualquer arquivo do caso ou quando as politicas nao forem recuperadas.
    if case_document_failures or policy_error:
        print(
            f"[DOCUMENT-BASIS] NEGOCIACAO PAUSADA: "
            f"document_failures={len(case_document_failures)} policy_error={bool(policy_error)}"
        )
        return _document_basis_block_response(request, case_document_failures, policy_error)

    # 6. Montar system prompt
    # PRIORIDADE: 1) Supabase (persistido), 2) Montar em tempo real (fallback)
    # NOTA: System prompt contem APENAS instructions + regras temporarias (NAO documentos)
    if system_prompt_from_supabase:
        # Usar prompt do Supabase
        system_prompt = system_prompt_from_supabase
        print(f"[API] USANDO PROMPT DO SUPABASE")
    else:
        # Fallback: montar em tempo real (apenas instructions + regras)
        system_prompt = build_system_prompt(
            instructions=instructions,
            temporary_rules=temp_rules if temp_rules else None,
            client_name=client_name
        )
        print(f"[API] USANDO PROMPT MONTADO EM TEMPO REAL (fallback)")

    # Se uma camada critica de seguranca falhar, a conversa pode continuar,
    # mas um fechamento automatico fica proibido ate revisao humana.
    negotiation_safety_failure = False
    negotiation_safety_reasons = []

    # 6.1 GUARDRAILS DE NEGOCIACAO (sempre aplicados, NAO sobrescritiveis)
    # O prompt da empresa (Supabase/payload) define persona, tom e politicas.
    # Este bloco define os LIMITES: ficha do caso, contexto legal e o protocolo
    # obrigatorio de negociacao. Vai depois do prompt da empresa de proposito -
    # tem precedencia declarada e se beneficia do efeito de recencia.
    # Motivacao: relatorios de avaliacao 30/07/2026 (nota 2,2/10 da IA da empresa).
    try:
        from src.negotiation_guardrails import build_company_negotiation_block

        negotiation_block = build_company_negotiation_block(
            context=context,
            message_history=message_history,
            negotiation=request.negotiation,
            company_name=company_name,
            client_name=client_name,
        )
        system_prompt = f"{system_prompt}\n\n{negotiation_block}"
        print(f"[API] Guardrails de negociacao aplicados (+{len(negotiation_block)} chars)")
    except Exception as e:
        # A conversa continua, mas nao pode finalizar automaticamente sem a
        # camada de guardrails que define os limites da negociacao.
        negotiation_safety_failure = True
        negotiation_safety_reasons.append(f"guardrails: {e}")
        print(f"[API] ERRO ao aplicar guardrails de negociacao: {e}")

    # Modelo/temperatura resolvidos aqui porque o passo 1 do turno (livro-razao)
    # ja precisa deles. NEGOTIATION_MODEL tem precedencia sobre o config.model do
    # payload: o backend Zellu envia gpt-4o-mini por default, modelo fraco demais
    # para negociar (auditoria 22/08/2026). Com NEGOTIATION_MODEL="" vale o payload.
    config = request.config
    payload_model = config.model if config and config.model else "gpt-4o-mini"
    model = settings.NEGOTIATION_MODEL or payload_model
    temperature = settings.NEGOTIATION_TEMPERATURE

    if model != payload_model:
        print(f"[API] Modelo da negociacao: {model} (payload pedia {payload_model})")
    print(f"[API] Modelo={model} temperature={temperature}")

    # 6.2 TURNO EM TRES PASSOS - PASSOS 1 E 2 (Fase 2)
    # Passo 1: ler a mensagem do ZelinhU e atualizar o livro-razao (saida
    #          estruturada, sem redigir nada)
    # Passo 2: validar em codigo puro - contradicoes com o que ja foi acordado
    # O passo 3 (redigir) e a chamada que ja existia, agora recebendo o estado
    # ja atualizado e conferido.
    ledger_block = ""
    if settings.NEGOTIATION_LEDGER_ENABLED and last_user_message:
        try:
            ledger_block = await _run_ledger_turn(
                session_id=session_id,
                last_client_message=last_user_message,
                message_history=message_history,
                context=context,
                model=model,
                temperature=temperature,
                negotiation=request.negotiation,
                last_message_id=request.lastMessageId or request.triggeredByMessageId or "",
            )
            if ledger_block:
                system_prompt = f"{system_prompt}\n\n{ledger_block}"
        except Exception as e:
            # O turno continua, mas sem livro-razao nao ha base segura para
            # encerrar automaticamente uma obrigacao financeira/juridica.
            negotiation_safety_failure = True
            negotiation_safety_reasons.append(f"ledger: {e}")
            print(f"[LEDGER] ERRO no turno do livro-razao: {e}")

    # 7. Montar mensagens para o LLM. A base documental vem ANTES do historico:
    # isso reforca a precedencia das evidencias e mantem um prefixo mais estavel
    # para reutilizacao de KV cache nas rodadas seguintes.
    messages = [system_message(content=system_prompt)]
    messages.append(system_message(content=case_document_context))
    messages.append(system_message(content=rag_context))
    print(
        f"[DOCUMENT-BASIS] Contexto obrigatorio adicionado: "
        f"caso={len(case_document_context)} chars, politicas={len(rag_context)} chars"
    )

    for msg in message_history:
        if msg.origin == "zelinhu":
            messages.append(user_message(content=msg.body))
        elif msg.origin in ["company_ai", "company_user", "company_manual"]:
            messages.append(assistant_message(content=msg.body))

    # 9. Detectar se cliente esta aceitando proposta
    last_user_msg = ""
    last_ai_msg = ""

    for msg in reversed(message_history):
        if msg.origin == "zelinhu" and not last_user_msg:
            last_user_msg = msg.body.lower().strip()
        elif msg.origin == "company_ai" and not last_ai_msg:
            last_ai_msg = msg.body.lower()
        if last_user_msg and last_ai_msg:
            break

    # Aceite e fechamento sao regras deterministicas. Substring pura e insegura:
    # "nao aceito" contem "aceito" e "nao concordo" contem "concordo".
    # Em ambiguidade, nao fechamos automaticamente.
    is_acceptance = is_acceptance_message(last_user_msg)

    last_ai_normalized = _normalize_negotiation_text(last_ai_msg)
    was_proposal = any(
        marker in last_ai_normalized
        for marker in (
            "voce aceita",
            "posso prosseguir",
            "podemos prosseguir",
            "confirma o aceite",
            "confirma a proposta",
        )
    )

    if is_acceptance and was_proposal:
        closure_instruction = """

ATENCAO: O cliente ACEITOU a proposta que voce fez. Voce DEVE:
1. Confirmar brevemente o acordo
2. OBRIGATORIAMENTE encerrar com: "[CASO FINALIZADO] - Sua solicitacao foi registrada com sucesso."
NAO faca mais perguntas. NAO peca mais informacoes. FECHE O CASO AGORA.
"""
        messages[0] = system_message(content=system_prompt + closure_instruction)

    # 10. PASSO 3 DO TURNO - redigir. O modelo aqui verbaliza uma posicao que ja
    # passou pelo livro-razao (passo 1) e pela validacao (passo 2).
    # model e temperature foram resolvidos antes do passo 1.

    # Adicionar descricao das tools ao system prompt, com orientacao de uso.
    # Diagnostico A1 (22/08/2026): cada tool call e um roundtrip inteiro de LLM.
    # O "search_knowledge_base xN" do log e o modelo cacando documento em base
    # vazia - dizer o que ja foi buscado e o que nem existe corta iteracao.
    tools_description = get_tools_description_for_prompt()

    # Base sem nada para este caso: a busca previa ja rodou sobre O MESMO indice,
    # com o texto da propria mensagem como query, e voltou vazia. Uma parafrase do
    # modelo vai bater no mesmo lugar. Enquanto isso, cada tentativa e um roundtrip
    # inteiro de LLM - o log de 25/08 tem 5 search_knowledge_base num turno de 50s,
    # todos contra "[RAG-INDEXER] Busca retornou 0 resultados".
    base_sem_conteudo = not knowledge_bases
    base_sem_resposta_para_o_caso = bool(knowledge_bases) and not rag_context

    oferecer_tools, orcamento_de_consulta = plano_de_consulta_a_base(
        tem_base=bool(knowledge_bases),
        busca_previa_trouxe_algo=bool(rag_context),
        orcamento_padrao=settings.NEGOTIATION_MAX_TOOL_CALLS,
    )

    orientacao = []
    if base_sem_conteudo:
        orientacao.append(
            "- Esta empresa NAO tem base de conhecimento indexada. NAO chame "
            "search_knowledge_base: nao ha o que encontrar."
        )
    if base_sem_resposta_para_o_caso:
        orientacao.append(
            "- A base desta empresa ja foi consultada com o texto desta mensagem e "
            "NAO retornou nenhum documento. Buscar de novo com outras palavras bate "
            "no mesmo indice e volta vazio. Responda com o que voce ja tem e diga "
            "que a informacao nao consta na base."
        )
    if rag_context:
        orientacao.append(
            "- As politicas da empresa relevantes para a ULTIMA mensagem ja foram "
            "recuperadas e estao neste contexto. So chame search_knowledge_base se "
            "precisar de um assunto diferente do que ja esta aqui."
        )
    if oferecer_tools:
        # O numero aqui e o orcamento REAL desta sessao, nao o padrao: prometer
        # duas rodadas e cortar na primeira e instruir o modelo com informacao
        # falsa.
        orientacao.append(
            "- Voce tem no maximo "
            f"{orcamento_de_consulta} rodada(s) de consulta antes de responder. "
            "Se a busca nao trouxer o que precisa, diga que a informacao nao consta na "
            "base em vez de repetir a busca."
        )

    tools_description = tools_description + "\n\nORIENTACAO DESTA SESSAO:\n" + "\n".join(orientacao)

    if messages and isinstance(messages[0], dict) and messages[0].get('role') == 'system':
        original_content = messages[0]['content']
        messages[0] = system_message(content=original_content + "\n\n" + tools_description)

    print(
        f"[API] Consulta a base: tools={'sim' if oferecer_tools else 'nao'}, "
        f"orcamento={orcamento_de_consulta} rodada(s)"
    )

    # Usar ChatWithToolsHandler para suportar function calling
    # NOTA: api_key e obtida via OPENAI_API_KEY env var dentro do handler
    chat_handler = ChatWithToolsHandler(
        company_id=company_id,
        model=model,
        temperature=temperature,
        max_tool_calls=orcamento_de_consulta
    )

    # Chamar LLM com tools
    chat_result = await chat_handler.chat(messages, include_tools=oferecer_tools)
    ai_response = chat_result["content"]
    tokens_used = chat_result.get("tokens_used")
    tool_calls_made = chat_result.get("tool_calls_made", [])

    if tool_calls_made:
        print(f"[API] Tools usadas: {[t['name'] for t in tool_calls_made]}")

    # 10.1 LINT DETERMINISTICO DA RESPOSTA (Fase 2)
    # A Fase 1 age por instrucao, e os relatorios mostram que o modelo desobedece:
    # no Caso 2 ele assinou como "[Zellu - Assistencia ao Cliente]" nas 4 rodadas,
    # mesmo apos recusas explicitas apontando o erro. Aqui o texto JA GERADO e
    # conferido: 1 tentativa de correcao e, em ultimo caso, sanitizacao.
    # A resposta NUNCA e bloqueada - messageBody vazio quebraria o backend.
    # Dados do caso usados pelas regras numericas/estrategicas (L6-L10) e pelo
    # mandato. Vem da mesma ficha do caso que alimenta os guardrails.
    _ticket = context.get("ticket") or {}
    case_category = _ticket.get("category") or ""
    case_description = _ticket.get("description") or _ticket.get("subject") or _ticket.get("title") or ""
    case_value = None
    company_mandate = None
    requires_human_approval = False

    try:
        from decimal import Decimal as _Decimal
        from src.negotiation_mandate import build_company_mandate

        _raw_value = _ticket.get("estimatedValue") or _ticket.get("estimated_value")
        if _raw_value not in (None, ""):
            case_value = _Decimal(str(_raw_value))
        company_mandate = build_company_mandate(context)
        print(f"[MANDATO] Teto global: {company_mandate.global_cap} ({company_mandate.cap_source})")
    except Exception as e:
        negotiation_safety_failure = True
        negotiation_safety_reasons.append(f"mandato: {e}")
        print(f"[MANDATO] Nao foi possivel montar o mandato: {e}")

    try:
        from src.negotiation_lint import (
            build_correction_instruction,
            format_violations_for_log,
            lint_company_response,
            sanitize_company_response,
        )

        def _lint(resposta):
            return lint_company_response(
                resposta,
                company_name,
                client_name,
                case_value=case_value,
                case_category=case_category,
                case_description=case_description,
                mandate=company_mandate,
            )

        violations = _lint(ai_response)

        if violations:
            print(f"[LINT] Violacoes na 1a resposta: {format_violations_for_log(violations)}")

            correction = build_correction_instruction(violations, company_name, client_name)
            retry_messages = messages + [
                assistant_message(content=ai_response),
                system_message(content=correction),
            ]
            # include_tools=False: o retry e reescrita, nao precisa consultar base
            retry_result = await chat_handler.chat(retry_messages, include_tools=False)
            retry_response = (retry_result.get("content") or "").strip()

            if retry_response:
                tokens_used = (tokens_used or 0) + (retry_result.get("tokens_used") or 0)
                remaining = _lint(retry_response)

                if not remaining:
                    ai_response = retry_response
                    print(f"[LINT] Resposta corrigida no retry")
                else:
                    print(f"[LINT] Violacoes persistem apos retry: {format_violations_for_log(remaining)}")
                    ai_response = sanitize_company_response(retry_response, company_name)
                    ainda = _lint(ai_response)
                    if ainda:
                        negotiation_safety_failure = True
                        print(f"[LINT] ALERTA: resposta enviada com violacoes nao sanitizaveis: {format_violations_for_log(ainda)}")
            else:
                print(f"[LINT] Retry retornou vazio - sanitizando a resposta original")
                ai_response = sanitize_company_response(ai_response, company_name)

        # GATE DE ALCADA: uma resposta que ainda assume obrigacao fora do mandato
        # nao pode fechar o caso sozinha. Nao bloqueamos o envio (messageBody
        # vazio quebraria o backend) - bloqueamos o FECHAMENTO AUTOMATICO e
        # sinalizamos a pendencia para o backend Zellu encaminhar a um humano.
        alcada = [v for v in _lint(ai_response) if v.rule == "L7_ALCADA"]
        if alcada:
            requires_human_approval = True
            print(f"[MANDATO] GATE ACIONADO - resposta fora da alcada: {format_violations_for_log(alcada)}")
    except Exception as e:
        # O atendimento continua, mas nao finaliza automaticamente sem o lint.
        negotiation_safety_failure = True
        print(f"[LINT] ERRO no lint de saida: {e}")

    # 11. Verificar se caso foi finalizado (case-insensitive)
    negotiation_complete = "[caso finalizado]" in ai_response.lower()

    if negotiation_complete and (requires_human_approval or negotiation_safety_failure):
        if negotiation_safety_failure:
            print(f"[NEGOTIATION-SAFETY] Fechamento bloqueado: {negotiation_safety_reasons}")
        requires_human_approval = True
        negotiation_complete = False
        from src.negotiation_lint import strip_case_closed_marker
        ai_response = strip_case_closed_marker(ai_response)
        reason = (
            "o acordo excede a alcada"
            if not negotiation_safety_failure
            else "uma camada critica de seguranca falhou"
        )
        print(f"[MANDATO] [CASO FINALIZADO] ignorado: {reason}; depende de aprovacao humana")

    print(f"[API] Resposta gerada - negotiationComplete={negotiation_complete}")
    print(f"[API] Resposta IA: {ai_response[:200]}..." if len(ai_response) > 200 else f"[API] Resposta IA: {ai_response}")

    last_message_id = request.lastMessageId or request.triggeredByMessageId

    result = {
        "messageBody": ai_response,
        "messageType": "text",
        "tokensUsed": tokens_used,
        "modelUsed": model,
        "negotiationComplete": negotiation_complete,
        # Campo novo (aditivo): o backend Zellu pode ignorar sem quebrar nada.
        # true = a resposta assume obrigacao fora da alcada da IA e precisa de
        # aprovacao humana antes de virar acordo.
        "requiresHumanApproval": requires_human_approval,
        # Auditoria da base usada neste turno. Se chegamos ate aqui, ambos sao
        # verdadeiros por construcao: qualquer falha teria retornado antes do LLM.
        "documentBasisReady": True,
        "policyContextReady": True,
        "documentsRead": len(case_documents),
        "messagedata": {
            "replyToMessageId": last_message_id
        } if last_message_id else {}
    }

    print(f"[API] Response JSON: {str(result)[:300]}...")
    print(f"[API] messagedata enviado: {result.get('messagedata', {})}")
    return JSONResponse(content=result)


# =============================================================================
# TURNO ASSINCRONO DA IA DA EMPRESA
# Contrato: 20260911-turno-assincrono-ia-da-empresa.md
# =============================================================================
# Tarefas em voo. A referencia forte e necessaria: o asyncio so guarda
# referencia fraca para a task, e uma coleta de lixo no meio do turno o mataria
# sem callback nenhum - justamente o silencio que o contrato veio fechar.
_turnos_assincronos = set()


def _classificar_falha_do_turno(erro: Exception):
    """
    (code, retryable) do callback de falha (secao 3.2).

    `code` e livre - serve ao log e ao painel deles. `retryable` responde a
    unica pergunta que muda o comportamento do backend: vale redespachar este
    mesmo turno? Corpo invalido, nao: o redespacho traria o mesmo corpo.
    """
    nome = type(erro).__name__.lower()
    texto = str(erro).lower()
    if "validation" in nome:
        return "INVALID_PAYLOAD", False
    if "timeout" in nome or "timeout" in texto:
        return "LLM_TIMEOUT", True
    if "ratelimit" in nome or "rate limit" in texto:
        return "LLM_RATE_LIMIT", True
    return "TURN_FAILED", True


async def _executar_turno_assincrono(raw_body: dict, callback_url: str, turn_id, session_id, last_message_id):
    """
    Roda o turno sem pressa e entrega o resultado no `callbackUrl`.

    REDESPACHO: se o cron do backend redespachar o mesmo turno com outro
    `turnId`, a chave de dedupe continua sendo `sessionId:lastMessageId`, entao
    o single-flight de _process_empresa_request_deduped faz UM turno de LLM so -
    o segundo despacho espera o mesmo resultado. Cada despacho manda o callback
    dele, com o `turnId` dele; o segundo o backend responde como `duplicate`.

    Nunca levanta: e background task, e um traceback perdido aqui seria o mesmo
    silencio de antes. Falha vira callback com `status: "failed"`.
    """
    import json as _json
    from src.services.company_turn_callback import (
        build_turn_callback_payload,
        build_turn_failure_payload,
        send_turn_callback,
    )

    api_key = raw_body.get("apiKey")

    try:
        response = await _resolver_e_processar_turno(raw_body)
        status_code = getattr(response, "status_code", 200)
        corpo = _json.loads(bytes(getattr(response, "body", b"") or b"{}") or b"{}")

        if status_code >= 400:
            payload = build_turn_failure_payload(
                turn_id, session_id, last_message_id,
                error=str(corpo)[:300],
                code=f"TURN_HTTP_{status_code}",
                retryable=status_code >= 500,
            )
        elif not (corpo.get("messageBody") or "").strip() and not corpo.get("skipped"):
            # Turno sem texto e sem `skipped` e FALHA, nao turno concluido
            # (resposta do backend em 14/09/2026): la, corpo vazio sem o
            # marcador ja era tratado como resposta invalida. Dizer "completed"
            # esconderia isso num turno que nao produziu nada; dizer "failed"
            # devolve o turno para a fila de redespacho do cron deles.
            #
            # `skipped: true` NAO passa por aqui: e turno pulado de proposito
            # (a empresa assumiu a conversa), e eles respondem
            # `discarded: "skipped"` sem gravar nada.
            print(f"[EMPRESA-API] Turno sem messageBody e sem 'skipped' - turnId={turn_id}")
            payload = build_turn_failure_payload(
                turn_id, session_id, last_message_id,
                error="o turno terminou sem texto de resposta",
                code="EMPTY_RESPONSE",
                retryable=True,
            )
        else:
            payload = build_turn_callback_payload(turn_id, session_id, last_message_id, corpo)

    except Exception as e:
        import traceback
        print(f"[EMPRESA-API] Turno assincrono falhou depois do 202 - turnId={turn_id}")
        print(traceback.format_exc())
        code, retryable = _classificar_falha_do_turno(e)
        payload = build_turn_failure_payload(
            turn_id, session_id, last_message_id,
            error=f"{type(e).__name__}: {e}",
            code=code,
            retryable=retryable,
        )

    await send_turn_callback(callback_url, payload, api_key)


async def _aceitar_turno_assincrono(raw_body: dict, callback_url: str):
    """
    Aceita o despacho e responde 202 na hora (secao 3.1).

    O payload e validado ANTES do 202: corpo fora do contrato continua levando
    400 sincrono. Depois do 202 nao ha mais como responder erro de contrato, e
    um callback de falha por algo que o backend corrige no proprio despacho so
    atrasaria a correcao.
    """
    turn_id = raw_body.get("turnId")
    session_id = raw_body.get("sessionId") or raw_body.get("session_id") or ""
    last_message_id = raw_body.get("lastMessageId") or raw_body.get("triggeredByMessageId")

    try:
        EmpresaBaseDadosRequest.model_validate(raw_body)
    except Exception as e:
        print(f"[EMPRESA-API] Despacho assincrono com payload invalido: {e}")
        return JSONResponse(
            status_code=400,
            content={"error": f"Invalid payload format: {str(e)}"}
        )

    print(f"[EMPRESA-API] ========== DESPACHO ASSINCRONO ACEITO ==========")
    print(f"[EMPRESA-API] turnId={turn_id} lastMessageId={last_message_id}")
    print(f"[EMPRESA-API] callbackUrl={callback_url}")

    tarefa = asyncio.ensure_future(
        _executar_turno_assincrono(raw_body, callback_url, turn_id, session_id, last_message_id)
    )
    _turnos_assincronos.add(tarefa)
    tarefa.add_done_callback(_turnos_assincronos.discard)

    return JSONResponse(
        status_code=202,
        content={"accepted": True, "turnId": turn_id}
    )


async def _resolver_e_processar_turno(raw_body: dict):
    """
    Resolve o contexto e executa o turno da IA da empresa.

    E o corpo de sempre de /api/company/base-dados, so que extraido para uma
    funcao: assim o caminho sincrono (200 na hora) e o assincrono (202 +
    callback, contrato de 11/09/2026) rodam exatamente o mesmo codigo, e uma
    correcao no turno vale para os dois sem ninguem lembrar de copiar.
    """
    # Parsear e processar

    # Tentar parsear manualmente para ter mais controle
    empresa_request = EmpresaBaseDadosRequest.model_validate(raw_body)

    # Chave do turno: o backend retenta esta chamada quando estoura o
    # timeout, e sem isso cada retry refaz o turno inteiro de LLM.
    dedupe_key = _empresa_dedupe_key(raw_body)

    # =========================================================================
    # FALLBACK: autoStart mode
    # =========================================================================
    # Em situações normais, o Zellu Backend envia o payload completo.
    # O autoStart é um fallback para quando o contexto não está no payload.
    # Neste caso, buscamos os dados do Zellu Backend via API.
    # =========================================================================
    if empresa_request.autoStart or not empresa_request.context:
        session_id = empresa_request.sessionId
        ticket_id = empresa_request.ticketId or raw_body.get('ticketId')

        print(f"[EMPRESA-API] Modo autoStart (fallback) - sessionId={session_id}")
        print(f"[EMPRESA-API] autoStart={empresa_request.autoStart}, context={empresa_request.context is not None}")

        # Buscar contexto do Zellu Backend quando autoStart
        if session_id:
            print(f"[EMPRESA-API] Buscando contexto da sessao no Zellu Backend...")
            zellu_api_client = ZelluAPIClient(
                base_url=settings.ZELLU_BACKEND_URL,
                api_key=settings.API_KEY_ZELLU_IA
            )
            company_id = empresa_request.companyId or raw_body.get('companyId')
            session_payload = await zellu_api_client.build_payload_from_session(
                session_id=session_id,
                ticket_id=ticket_id,
                company_id=company_id
            )
            if session_payload:
                print(f"[EMPRESA-API] Contexto obtido com sucesso! Processando...")

                # Obter messageHistory do payload
                message_history = session_payload.get("messageHistory", [])
                context = session_payload.get("context", {})

                # =========================================================================
                # FALLBACK: Se messageHistory está vazio, criar mensagem inicial do ZelinhU
                # Isso acontece quando o Sender ainda não enviou a primeira mensagem
                # =========================================================================
                if not message_history or len(message_history) == 0:
                    print(f"[EMPRESA-API] messageHistory vazio! Criando mensagem inicial do ZelinhU...")

                    # Extrair dados do ticket para criar a mensagem
                    ticket = context.get("ticket", {})
                    client = context.get("client", {})

                    # Campos do ticket: subject (assunto), description, category
                    # Fallback para title se subject não existir (compatibilidade)
                    ticket_title = ticket.get("subject") or ticket.get("title", "")
                    ticket_description = ticket.get("description", "")
                    ticket_category = ticket.get("category", "")
                    # estimatedValue pode estar em diferentes formatos
                    estimated_value = ticket.get("estimatedValue") or ticket.get("estimated_value", 0)
                    client_name = client.get("name", "Cliente")

                    print(f"[EMPRESA-API] Dados do ticket: subject='{ticket_title[:50]}...', description={len(ticket_description)} chars, category='{ticket_category}', value={estimated_value}")

                    # Criar mensagem inicial do ZelinhU representando o cliente
                    # Usando modulo centralizado src/utils/zelinhu_messages.py
                    zelinhu_message = create_zelinhu_initial_message(
                        client_name=client_name,
                        ticket_title=ticket_title,
                        ticket_description=ticket_description,
                        ticket_category=ticket_category,
                        estimated_value=estimated_value
                    )

                    # Adicionar mensagem ao messageHistory
                    from datetime import datetime as dt
                    message_history = [{
                        "id": f"msg_zelinhu_initial_{dt.now().timestamp()}",
                        "origin": "zelinhu",
                        "type": "text",
                        "body": zelinhu_message,
                        "data": None,
                        "hasAttachments": False,
                        "timestamp": dt.now().isoformat()
                    }]

                    print(f"[EMPRESA-API] Mensagem inicial do ZelinhU criada ({len(zelinhu_message)} chars)")
                    print(f"[EMPRESA-API] Mensagem: {zelinhu_message[:200]}...")
                else:
                    print(f"[EMPRESA-API] messageHistory já possui {len(message_history)} mensagem(ns)")

                full_payload = {
                    **raw_body,
                    "context": context,
                    "instructions": session_payload.get("instructions", {}),
                    "messageHistory": message_history,
                    "knowledgeBases": session_payload.get("knowledgeBases", []),
                    "rules": session_payload.get("temporaryRules", session_payload.get("rules", [])),
                    "negotiation": session_payload.get("negotiation", {}),
                    "autoStart": False
                }
                empresa_request = EmpresaBaseDadosRequest.model_validate(full_payload)
                response = await _process_empresa_request_deduped(
                    empresa_request, "/api/company/base-dados", dedupe_key
                )

                # A resposta é retornada no HTTP response - o chamador (Zellu Backend)
                # é responsável por salvar a mensagem na UI
                print(f"[EMPRESA-API] ========== RESPOSTA ENVIADA (autoStart processado) ==========")
                return response
            else:
                print(f"[EMPRESA-API] AVISO: Nao foi possivel obter contexto do Zellu Backend.")

        # Fallback: retornar mensagem inicial
        return JSONResponse(
            content={
                "messageBody": "Ola! Sou o assistente virtual da empresa. Estou pronto para iniciar a negociacao.",
                "messageType": "text",
                "tokensUsed": 0,
                "modelUsed": "system",
                "negotiationComplete": False,
                "messagedata": {},
                "awaitingContext": True
            },
            status_code=200
        )

    response = await _process_empresa_request_deduped(
        empresa_request, "/api/company/base-dados", dedupe_key
    )

    print(f"[EMPRESA-API] ========== RESPOSTA ENVIADA ==========")
    return response


@app.post("/api/company/base-dados")
async def empresa_base_dados(
    request: Request,
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key")
):
    """
    Endpoint da IA da Empresa para processar mensagens de negociação.

    Conforme documentação: automatic-negotiation/api-base-dados.md

    Este endpoint é chamado pelo Zellu Backend quando o Sender Agent (ZelinhU)
    envia uma mensagem. O Zellu Backend é responsável por:
    1. Salvar a mensagem do ZelinhU
    2. Montar o payload completo (context, instructions, messageHistory, etc.)
    3. Chamar este endpoint
    4. Salvar a resposta e retornar para o Sender

    Headers:
        X-API-Key: chave de autenticacao (opcional por compatibilidade)

    Request:
        sessionId: ID da sessão
        context: { ticket, client, company }
        instructions: { persona, rules, legal, budget, tone }
        messageHistory: Lista de mensagens da negociação
        knowledgeBases: Lista de bases de conhecimento
        temporaryRules: Regras temporárias ativas
        config: { model, maxTokens, temperature }
        autoStart: Se deve iniciar automaticamente (fallback)

    Response:
        messageBody: Resposta da IA
        negotiationComplete: Se a negociação foi concluída

    Aceita payload em camelCase (padrão Zellu) ou snake_case.
    """
    # Validar API Key se fornecida
    if x_api_key:
        if not _api_key_matches(x_api_key, settings.API_KEY_ZELLU_IA):
            raise HTTPException(status_code=401, detail="API Key invalida")

    try:
        # Capturar payload raw para debug detalhado
        raw_body = await request.json()
        
        import json
        print(f"[EMPRESA-API] ========== REQUISICAO RECEBIDA ==========")
        print(f"[EMPRESA-API] PAYLOAD COMPLETO:")
        print(json.dumps(raw_body, indent=2, ensure_ascii=False, default=str))
        print(f"[EMPRESA-API] Campos no payload: {list(raw_body.keys())}")
        print(f"[EMPRESA-API] sessionId: {raw_body.get('sessionId', 'N/A')}")
        print(f"[EMPRESA-API] lastMessageId: {raw_body.get('lastMessageId', 'N/A')}")

        # Log do context
        context = raw_body.get('context', {})
        print(f"[EMPRESA-API] context.company.name: {context.get('company', {}).get('name', 'N/A')}")
        print(f"[EMPRESA-API] context.client.name: {context.get('client', {}).get('name', 'N/A')}")
        print(f"[EMPRESA-API] messageHistory count: {len(raw_body.get('messageHistory', []))}")
        print(f"[EMPRESA-API] knowledgeBases count: {len(raw_body.get('knowledgeBases', []))}")
        print(f"[EMPRESA-API] rules count: {len(raw_body.get('rules', []))}")

        # =====================================================================
        # DETECÇÃO DE PAYLOAD DE TESTE (formato do simulador Zellu)
        # =====================================================================
        # O simulador envia: { test, company, instructions, temporaryRules, knowledgeFiles, ticket }
        # Precisamos transformar para o formato EmpresaBaseDadosRequest
        # =====================================================================
        if "test" in raw_body and "sessionId" not in raw_body and "session_id" not in raw_body:
            print(f"[EMPRESA-API] >>> Detectado PAYLOAD DE TESTE - transformando para formato padrão...")
            test_data = raw_body.get("test", {})
            company_data = raw_body.get("company", {})
            ticket_data = raw_body.get("ticket", {})
            instructions_data = raw_body.get("instructions", {})
            temp_rules = raw_body.get("temporaryRules", [])
            knowledge_files = raw_body.get("knowledgeFiles", [])

            # Montar messageHistory com a mensagem inicial (motivo do teste)
            motivo = test_data.get("motivo", "")
            causa = test_data.get("causa", "")
            message_text = motivo if motivo else causa
            from datetime import datetime as dt
            message_history = []
            if message_text:
                message_history = [{
                    "id": f"msg_test_{dt.now().timestamp()}",
                    "origin": "zelinhu",
                    "type": "text",
                    "body": message_text,
                    "hasAttachments": False,
                    "timestamp": dt.now().isoformat()
                }]

            # Montar knowledgeBases a partir de knowledgeFiles
            knowledge_bases = []
            for kf in knowledge_files:
                knowledge_bases.append({
                    "id": kf.get("id"),
                    "name": kf.get("fileName", ""),
                    "description": kf.get("description"),
                    "isDefault": False
                })

            # Valor estimado
            valor_pedido = test_data.get("valorPedido", 0)
            try:
                valor_pedido = float(valor_pedido) if valor_pedido else 0
            except (ValueError, TypeError):
                valor_pedido = 0

            # Transformar para formato padrão
            raw_body = {
                "sessionId": test_data.get("id", f"test_{dt.now().timestamp()}"),
                "context": {
                    "ticket": {
                        "id": ticket_data.get("id", ""),
                        "number": ticket_data.get("ticketNumber", ""),
                        "title": ticket_data.get("title", test_data.get("name", "")),
                        "description": ticket_data.get("description", motivo),
                        "category": causa,
                        "estimatedValue": valor_pedido,
                        "status": "in_progress",
                        "resolutionType": test_data.get("tipoServico", "amigavel")
                    },
                    "client": {
                        "name": "Cliente Teste"
                    },
                    "company": {
                        "id": company_data.get("id", ""),
                        "name": company_data.get("name", ""),
                        "cnpj": company_data.get("cnpj", "")
                    }
                },
                "instructions": instructions_data,
                "messageHistory": message_history,
                "knowledgeBases": knowledge_bases,
                "temporaryRules": temp_rules,
                "autoStart": False
            }
            print(f"[EMPRESA-API] Payload transformado: sessionId={raw_body['sessionId']}")
            print(f"[EMPRESA-API] Company: {raw_body['context']['company']['name']}")
            print(f"[EMPRESA-API] Mensagem: {message_text[:100]}...")

        # =====================================================================
        # ROTEAMENTO POR ACTION
        # =====================================================================
        # Se action == "generate_legal_document", delegar para geracao de docs
        # O payload é diferente do EmpresaBaseDadosRequest (não tem sessionId)
        # =====================================================================
        action = raw_body.get("action")
        if action == "generate_legal_document":
            from api.routes.company_response import _handle_document_generation
            return await _handle_document_generation(raw_body)

        # =====================================================================
        # TURNO ASSINCRONO (20260911-turno-assincrono-ia-da-empresa.md)
        # =====================================================================
        # A Cloudflare corta esta rota em ~125s com 524. Turno mais longo que
        # isso morria no caminho e a negociacao so voltava a andar quando o cron
        # do backend a resgatava, 10 min depois.
        #
        # O interruptor e a presenca do `callbackUrl` no payload de TURNO (o que
        # nao traz `action`, ou traz `action: "company_turn"`): com ele,
        # respondemos 202 na hora e o resultado vai por callback. SEM ELE, NADA
        # MUDA - 200 sincrono, mesma resposta de sempre.
        #
        # `generate_legal_document` ja retornou acima e nao passa por aqui: ele
        # tem o callback dele desde sempre.
        callback_url = raw_body.get("callbackUrl")
        if callback_url and action in (None, "", "company_turn") and settings.COMPANY_TURN_ASYNC_ENABLED:
            return await _aceitar_turno_assincrono(raw_body, callback_url)

        return await _resolver_e_processar_turno(raw_body)
    
    except Exception as e:
        import traceback
        print(f"[EMPRESA-API] ========== ERRO ==========")
        print(f"[EMPRESA-API] Tipo: {type(e).__name__}")
        print(f"[EMPRESA-API] Mensagem: {str(e)}")
        print(traceback.format_exc())
        raise HTTPException(
            status_code=500,
            detail=f"Erro ao processar mensagem: {str(e)}"
        )


@app.post("/states/clear-completed")
async def clear_completed_states():
    """Clear all completed conversation states.

    Returns:
        Number of states cleared
    """
    count = state_manager.clear_completed()
    return {
        "message": f"Cleared {count} completed conversation(s)",
        "count": count
    }


# ============================================================================
# UPLOAD ENDPOINTS
# ============================================================================

@app.post("/api/ai-service/upload")
async def upload_files(
    files: List[UploadFile] = File(...),
    api_key: str = Depends(verify_api_key)
):
    """Upload files endpoint (conforme especificação AI_SERVICE_UPLOAD_API.md).

    Args:
        files: List of files to upload
        api_key: API key for authentication

    Returns:
        Upload response with file URLs
    """
    try:
        result = await upload_service.upload_multiple(files)

        if "error" in result and "details" in result:
            # All files failed
            return JSONResponse(
                status_code=400,
                content=result
            )

        return result

    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": "Erro ao processar upload"}
        )


# -----------------------------------------------------------------------------
# RAG ENDPOINTS
# -----------------------------------------------------------------------------

@app.post("/rag/load-documents")
async def load_rag_documents():
    """Load legal documents into Pinecone vector store."""
    try:
        if not rag_service or not rag_service.vector_store:
            return JSONResponse(
                status_code=503,
                content={"error": "Sistema RAG nao disponivel. Verifique PINECONE_API_KEY."}
            )

        rag_service.load_documents_from_directory()

        return {
            "message": "Documentos carregados com sucesso no Pinecone",
            "stats": rag_service.get_stats()
        }

    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Erro ao carregar documentos: {str(e)}"}
        )


@app.get("/rag/stats")
async def get_rag_stats():
    """Get RAG system statistics."""
    if not rag_service:
        return JSONResponse(
            status_code=503,
            content={"error": "Sistema RAG nao disponivel"}
        )

    return rag_service.get_stats()


@app.post("/rag/query")
async def query_rag(
    question: str,
    category: Optional[str] = None
):
    """Query the RAG system."""
    if not rag_service:
        return JSONResponse(
            status_code=503,
            content={"error": "Sistema RAG nao disponivel"}
        )

    try:
        result = rag_service.query(question, legal_category=category)
        return result
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Erro ao consultar RAG: {str(e)}"}
        )

# -----------------------------------------------------------------------------
# KNOWLEDGE BASES ENDPOINTS
# -----------------------------------------------------------------------------

@app.post("/knowledge-bases/populate-shared")
async def populate_shared_knowledge_bases():
    """
    Popula as bases de conhecimento compartilhadas (jurisprudencia e legislacao).
    Endpoint para inicializar as bases com dados de exemplo.
    Em producao, os dados devem vir de fontes oficiais.
    """
    try:
        from src.knowledge_bases import knowledge_base_indexer

        results = knowledge_base_indexer.populate_all_shared_bases()

        return {
            "message": "Bases compartilhadas populadas com sucesso",
            "indexed": results,
        }
    except Exception as e:
        import traceback
        print(f"[KB] Erro ao popular bases: {traceback.format_exc()}")
        return JSONResponse(
            status_code=500,
            content={"error": f"Erro ao popular bases: {str(e)}"}
        )


@app.post("/knowledge-bases/search")
async def search_knowledge_base(
    query: str,
    base_id: str,
    company_id: Optional[str] = None,
    top_k: int = 5,
    min_score: float = 0.5,
):
    """
    Busca em uma base de conhecimento especifica.
    Args:
        query: Texto da busca
        base_id: ID da base (politicas_gerais, jurisprudencia, etc)
        company_id: ID da empresa (obrigatorio para bases de empresa)
        top_k: Numero maximo de resultados
        min_score: Score minimo de similaridade (0.0 a 1.0)
    """
    try:
        from src.knowledge_bases import knowledge_base_service

        results = knowledge_base_service.search(
            query=query,
            base_id=base_id,
            company_id=company_id,
            top_k=top_k,
            min_score=min_score,
        )

        return {
            "query": query,
            "base_id": base_id,
            "results": results,
            "count": len(results),
        }
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Erro na busca: {str(e)}"}
        )


@app.get("/knowledge-bases/list")
async def list_knowledge_bases():
    """Lista todas as bases de conhecimento disponiveis."""
    from src.knowledge_bases import COMPANY_KNOWLEDGE_BASES, SHARED_KNOWLEDGE_BASES

    return {
        "company_bases": [
            {"id": b.id, "name": b.name, "description": b.description}
            for b in COMPANY_KNOWLEDGE_BASES
        ],
        "shared_bases": [
            {"id": b.id, "name": b.name, "description": b.description}
            for b in SHARED_KNOWLEDGE_BASES
        ],
    }

# -----------------------------------------------------------------------------
# STATE MANAGEMENT
# -----------------------------------------------------------------------------

@app.get("/state/{chat_id}", response_model=ConversationStateResponse)
async def get_conversation_state(chat_id: str):
    """Get the current conversation state."""
    state = state_manager.get_state(chat_id)

    if not state:
        raise HTTPException(
            status_code=404,
            detail=f"Conversation not found: {chat_id}"
        )

    return ConversationStateResponse(**state)


@app.delete("/state/{chat_id}")
async def delete_conversation_state(chat_id: str):
    """Delete a conversation state."""
    deleted = state_manager.delete_state(chat_id)

    if not deleted:
        raise HTTPException(
            status_code=404,
            detail=f"Conversation not found: {chat_id}"
        )

    return {"message": f"Conversation {chat_id} deleted successfully"}


@app.get("/states")
async def list_conversation_states():
    """List all conversation states."""
    states = state_manager.list_states()
    return {
        "count": len(states),
        "states": states
    }




# =============================================================================
# WEBHOOK - Notificacao de mudanca na empresa (REMOVIDO EM 17/09)
# =============================================================================
# Aqui viviam POST /api/webhook/company-update e POST /api/webhook/generate-prompt.
#
# A plataforma confirmou em 15/09 que NUNCA chamou nenhuma das duas - nem hoje
# nem no historico do repositorio deles - e pediu que removessemos os caminhos
# inteiros. Eram tambem as duas unicas portas de entrada do
# fetch_all_company_data, que por sua vez chamava uma rota
# (GET /api/companies/{id}/documents) que nunca existiu e respondia 404 em
# silencio. Os tres sairam juntos.
#
# Quem notifica mudanca de empresa e mantem o system prompt em dia sao os
# /company_notify_*_to_ia logo abaixo, que chamam generate_and_save_system_prompt.
# =============================================================================


# =============================================================================
# WEBHOOKS DE NOTIFICACAO - Company Config Notifications
# =============================================================================
# Esses endpoints sao chamados pela Zellu quando a empresa atualiza algo
# =============================================================================

from api.schemas import (
    KnowledgeBaseWebhookRequest,
    InstructionsWebhookRequest,
    TemporaryRulesWebhookRequest,
    WebhookNotificationResponse
)

# Storage em memoria por empresa (TODO: migrar para banco de dados)
company_instructions_store: dict = {}  # {company_id: InstructionPayload}
company_rules_store: dict = {}  # {company_id: {rule_id: TemporaryRuleWebhookPayload}}
company_knowledge_store: dict = {}  # {company_id: {file_id: FileMetadata}}


# =============================================================================
# FUNCAO PRINCIPAL: Gerar e Salvar System Prompt
# =============================================================================

async def generate_and_save_system_prompt(
    company_id: str,
    company_name: str = None,
    save_to_zellu: bool = True
) -> dict:
    """
    Gera o system prompt completo para uma empresa e salva no Supabase.

    Esta funcao e chamada quando:
    1. Instructions sao atualizadas (webhook)
    2. Knowledge base e atualizada (webhook)
    3. Manualmente via endpoint de teste

    Args:
        company_id: UUID da empresa
        company_name: Nome da empresa (opcional, usa do store se nao fornecido)
        save_to_zellu: Se True, salva no Supabase via API

    Returns:
        Dict com prompt gerado e resultado do salvamento
    """
    from src.prompt_generator import build_system_prompt, build_documents_context
    from models.company import CompanyInstructions, TemporaryRule

    print(f"[PROMPT-GEN] Gerando system prompt para empresa {company_id}")

    # 1. Buscar instructions do store (recebidas via webhook)
    if company_id in company_instructions_store:
        stored = company_instructions_store[company_id]
        instructions = CompanyInstructions(
            company_id=company_id,
            persona=stored.get("persona", ""),
            rules=stored.get("rules", ""),
            legal=stored.get("legal", ""),
            budget=stored.get("budget", ""),
            tone=stored.get("tone", "profissional")
        )
        # Usar company_name do argumento ou extrair do store se disponivel
        if not company_name:
            company_name = stored.get("company_name", "Empresa")
        print(f"[PROMPT-GEN] Instructions encontradas no store")
    else:
        # Usar defaults
        if not company_name:
            company_name = "Empresa"
        instructions = CompanyInstructions(
            company_id=company_id,
            persona=f"Voce e o assistente virtual da {company_name}. Seja cordial, profissional e objetivo.",
            rules="Responda de forma clara e objetiva. Nao prometa o que nao pode cumprir. Escale para humano se necessario.",
            legal="Siga as normas do CDC (Codigo de Defesa do Consumidor). Respeite prazos legais.",
            budget="Tente resolver dentro do valor estimado do ticket. Negocie de forma justa.",
            tone="profissional"
        )
        print(f"[PROMPT-GEN] AVISO: Instructions nao encontradas, usando defaults")

    # 2. Buscar regras temporarias ativas do store
    temp_rules = []
    if company_id in company_rules_store:
        for rule_data in company_rules_store[company_id].values():
            if not rule_data.get("is_paused", False):
                temp_rules.append(TemporaryRule(
                    id=rule_data.get("id", ""),
                    company_id=company_id,
                    content=rule_data.get("rule_content", ""),
                    description=rule_data.get("description", ""),
                    valid_from=None,
                    valid_until=None,
                    is_paused=False
                ))
        print(f"[PROMPT-GEN] {len(temp_rules)} regras temporarias ativas encontradas")

    # 3. Buscar documentos do RAG (Pinecone) - resumo dos docs indexados
    documents_context = None
    if company_id in company_knowledge_store:
        # Montar contexto com metadados dos documentos indexados
        docs_info = []
        for file_data in company_knowledge_store[company_id].values():
            if file_data.get("status") == "indexed":
                docs_info.append({
                    "title": file_data.get("fileName", "Documento"),
                    "content": f"[Documento indexado: {file_data.get('fileName')}]",
                    "category": file_data.get("knowledge_base_name", "")
                })
        if docs_info:
            documents_context = build_documents_context(docs_info)
            print(f"[PROMPT-GEN] {len(docs_info)} documento(s) indexados incluidos no contexto")

    # 4. Gerar system prompt
    system_prompt = build_system_prompt(
        instructions=instructions,
        temporary_rules=temp_rules if temp_rules else None,
        documents_context=documents_context,
        client_name="Cliente"  # Sera substituido em runtime
    )

    print(f"[PROMPT-GEN] System prompt gerado ({len(system_prompt)} caracteres)")

    result = {
        "company_id": company_id,
        "company_name": company_name,
        "prompt": system_prompt,
        "prompt_length": len(system_prompt),
        "has_instructions": company_id in company_instructions_store,
        "has_temporary_rules": len(temp_rules) > 0,
        "has_documents": documents_context is not None,
        "generated_at": datetime.now().isoformat()
    }

    # 5. Salvar no Supabase via API Zellu
    if save_to_zellu and zellu_api_client:
        try:
            extra_info = {
                "version": "1.0.0",
                "generated_at": result["generated_at"],
                "has_instructions": result["has_instructions"],
                "has_temporary_rules": result["has_temporary_rules"],
                "has_documents": result["has_documents"],
                "prompt_length": result["prompt_length"]
            }

            save_result = await zellu_api_client.save_system_prompt(
                company_id=company_id,
                prompt=system_prompt,
                extra_info=extra_info
            )

            result["saved_to_zellu"] = save_result.get("success", False)
            result["save_result"] = save_result

            if save_result.get("success"):
                print(f"[PROMPT-GEN] System prompt salvo no Supabase com sucesso")
                if save_result.get("previousPromptDeactivated"):
                    print(f"[PROMPT-GEN] Prompt anterior foi desativado")
            else:
                print(f"[PROMPT-GEN] ERRO ao salvar no Supabase: {save_result.get('error')}")

        except Exception as e:
            print(f"[PROMPT-GEN] Excecao ao salvar no Supabase: {e}")
            result["saved_to_zellu"] = False
            result["save_error"] = str(e)
    else:
        result["saved_to_zellu"] = False
        result["save_reason"] = "save_to_zellu=False ou zellu_api_client indisponivel"

    # 6. Atualizar cache local
    company_prompts_cache[company_id] = {
        "prompt": system_prompt,
        "generated_at": result["generated_at"],
        "has_instructions": result["has_instructions"],
        "has_temporary_rules": result["has_temporary_rules"],
        "has_documents": result["has_documents"]
    }

    return result

# RAG Indexer global (inicializado abaixo)
# from src.services.rag_indexer import RAGIndexer  # TODO: fix service compatibility
# from src.services.document_extractor import DocumentExtractor  # TODO: fix service compatibility

rag_indexer_instance = None  # RAGIndexer desabilitado temporariamente
doc_extractor_instance = None  # DocumentExtractor desabilitado temporariamente

# URL base do MinIO da Zellu (configurar quando disponivel)
ZELLU_MINIO_BASE_URL = os.environ.get("ZELLU_MINIO_BASE_URL", "")


@app.post("/company_notify_knowledgebase_to_ia", response_model=WebhookNotificationResponse)
async def notify_knowledgebase(
    request: KnowledgeBaseWebhookRequest,
    x_api_key: str = Header(..., alias="x-api-key")
):
    """
    Webhook: Base de Conhecimento.

    Recebido quando empresa faz upload ou deleta arquivos.
    Atualizado em 10/12/2025 para usar API de download do Zellu.

    Actions:
    - create: Indexar arquivos no Pinecone (RAG) via API download
    - delete: Remover arquivos do indice

    Novos campos (10/12/2025):
    - knowledge_base_id: UUID da base
    - knowledge_base_name: Nome da base
    - is_default: Se e a base padrao

    Headers:
        x-api-key: chave de autenticacao
    """
    # Validar API Key
    if not _api_key_matches(x_api_key, settings.API_KEY_ZELLU_IA):
        raise HTTPException(status_code=401, detail="API Key invalida")

    company_id = request.company_id
    action = request.action
    knowledge_base_id = request.knowledge_base_id
    knowledge_base_name = request.knowledge_base_name
    is_default = request.is_default
    files = request.files

    print(f"[AI-NOTIFY] Knowledge base: company={company_id}, kb={knowledge_base_name} ({knowledge_base_id})")
    print(f"[AI-NOTIFY] Action={action}, files={len(files)}, is_default={is_default}")

    # Inicializar store da empresa se nao existir
    if company_id not in company_knowledge_store:
        company_knowledge_store[company_id] = {}

    indexed_count = 0
    deleted_count = 0
    errors = []

    try:
        if action == "create":
            # Verificar se RAG Indexer e API Client estao disponiveis
            if rag_indexer_instance and zellu_api_client:
                # Usar novo metodo process_webhook_files
                files_data = [
                    {
                        "id": f.id,
                        "fileName": f.fileName,
                        "fileSize": f.fileSize,
                        "fileType": f.fileType,
                        "folder": f.folder,
                        "title": f.title,
                        "description": f.description,
                        "minioPath": f.minioPath
                    }
                    for f in files
                ]

                result = await rag_indexer_instance.process_webhook_files(
                    company_id=company_id,
                    knowledge_base_id=knowledge_base_id,
                    knowledge_base_name=knowledge_base_name,
                    files=files_data,
                    api_client=zellu_api_client,
                    action="create"
                )

                indexed_count = len(result.get("processed", []))
                errors = [f"{e['file_name']}: {e['error']}" for e in result.get("failed", [])]

                # Atualizar store local
                for f in files:
                    company_knowledge_store[company_id][f.id] = {
                        "id": f.id,
                        "fileName": f.fileName,
                        "fileType": f.fileType,
                        "knowledge_base_id": knowledge_base_id,
                        "knowledge_base_name": knowledge_base_name,
                        "status": "indexed" if f.id in [p["file_id"] for p in result.get("processed", [])] else "error",
                        "indexed_at": datetime.now().isoformat()
                    }

                print(f"[AI-NOTIFY] Resultado RAG: {indexed_count} indexados, {len(errors)} erros")

            else:
                # RAG Indexer ou API Client nao disponivel - armazenar como pendente
                for f in files:
                    company_knowledge_store[company_id][f.id] = {
                        "id": f.id,
                        "fileName": f.fileName,
                        "fileType": f.fileType,
                        "knowledge_base_id": knowledge_base_id,
                        "status": "pending",
                        "indexed_at": None
                    }
                print(f"[AI-NOTIFY] RAG Indexer/API Client nao disponivel - {len(files)} arquivo(s) pendentes")

        elif action == "delete":
            for file in files:
                print(f"[AI-NOTIFY] Removendo arquivo: {file.id} da kb={knowledge_base_id}")

                # Remover do Pinecone
                if rag_indexer_instance:
                    result = await rag_indexer_instance.delete_document(
                        company_id=company_id,
                        file_id=file.id,
                        knowledge_base_id=knowledge_base_id
                    )
                    if result.get("success"):
                        deleted_count += 1
                        print(f"[AI-NOTIFY] Arquivo removido do indice: {file.fileName}")
                    else:
                        errors.append(f"{file.fileName}: {result.get('error')}")
                else:
                    deleted_count += 1  # Considerar sucesso se nao ha indexador

                # Remover do store local
                if file.id in company_knowledge_store[company_id]:
                    del company_knowledge_store[company_id][file.id]

            print(f"[AI-NOTIFY] {deleted_count} arquivo(s) removidos do indice")

        # Construir mensagem de resposta
        if action == "create":
            msg = f"KB '{knowledge_base_name}': {len(files)} arquivo(s) - {indexed_count} indexados"
            if errors:
                msg += f", {len(errors)} erros"
        else:
            msg = f"KB '{knowledge_base_name}': {deleted_count}/{len(files)} arquivo(s) removidos"

        # Regenerar e salvar system prompt (documentos mudaram)
        try:
            prompt_result = await generate_and_save_system_prompt(
                company_id=company_id,
                save_to_zellu=True
            )
            if prompt_result.get("saved_to_zellu"):
                msg += f" | Prompt atualizado ({prompt_result.get('prompt_length', 0)} chars)"
                print(f"[AI-NOTIFY] System prompt regenerado apos mudanca de KB")
            else:
                msg += " | Prompt NAO atualizado"
        except Exception as e:
            print(f"[AI-NOTIFY] Erro ao regenerar prompt apos KB: {e}")

        return WebhookNotificationResponse(success=True, message=msg)

    except Exception as e:
        import traceback
        print(f"[AI-NOTIFY] ERRO em knowledgebase:")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/company_notify_instructions_to_ia", response_model=WebhookNotificationResponse)
async def notify_instructions(
    request: InstructionsWebhookRequest,
    x_api_key: str = Header(..., alias="x-api-key")
):
    """
    Webhook: Instrucoes de IA.

    Recebido quando empresa atualiza as 5 instrucoes (persona, rules, legal, budget, tone).

    Fluxo:
    1. Armazena instructions no store local
    2. Gera novo system prompt
    3. Salva no Supabase via POST /api/ai/system-prompt

    Actions:
    - update: Atualizar system prompt da empresa

    Headers:
        x-api-key: chave de autenticacao
    """
    # Validar API Key
    if not _api_key_matches(x_api_key, settings.API_KEY_ZELLU_IA):
        raise HTTPException(status_code=401, detail="API Key invalida")

    company_id = request.company_id
    action = request.action
    instruction = request.instruction

    print(f"[AI-NOTIFY] Instructions: company={company_id}, action={action}, instruction_id={instruction.id}")

    try:
        # 1. Armazenar instructions da empresa
        company_instructions_store[company_id] = {
            "id": instruction.id,
            "persona": instruction.persona,
            "rules": instruction.rules,
            "legal": instruction.legal,
            "budget": instruction.budget,
            "tone": instruction.tone,
            "updated_at": datetime.now().isoformat()
        }

        print(f"[AI-NOTIFY] Instructions atualizadas para empresa {company_id}")
        print(f"[AI-NOTIFY] - Persona: {instruction.persona[:50]}...")
        print(f"[AI-NOTIFY] - Tone: {instruction.tone}")

        # 2. Gerar e salvar system prompt no Supabase
        prompt_result = await generate_and_save_system_prompt(
            company_id=company_id,
            save_to_zellu=True
        )

        saved = prompt_result.get("saved_to_zellu", False)
        prompt_length = prompt_result.get("prompt_length", 0)

        if saved:
            msg = f"Instructions updated + System prompt saved ({prompt_length} chars)"
        else:
            save_error = prompt_result.get("save_error") or prompt_result.get("save_reason", "unknown")
            msg = f"Instructions updated but prompt NOT saved: {save_error}"

        print(f"[AI-NOTIFY] Resultado: {msg}")

        return WebhookNotificationResponse(
            success=True,
            message=msg
        )

    except Exception as e:
        import traceback
        print(f"[AI-NOTIFY] ERRO em instructions:")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/company_notify_temporary_rules_to_ia", response_model=WebhookNotificationResponse)
async def notify_temporary_rules(
    request: TemporaryRulesWebhookRequest,
    x_api_key: str = Header(..., alias="x-api-key")
):
    """
    Webhook: Regras Temporarias.

    Recebido quando empresa cria/atualiza/deleta/pausa regras temporarias.

    Actions:
    - create: Adicionar regra ao contexto
    - update: Atualizar regra existente
    - delete: Remover regra
    - active: Ativar regra (is_paused=false)
    - inactive: Pausar regra (is_paused=true)

    Headers:
        x-api-key: chave de autenticacao
    """
    # Validar API Key
    if not _api_key_matches(x_api_key, settings.API_KEY_ZELLU_IA):
        raise HTTPException(status_code=401, detail="API Key invalida")

    company_id = request.company_id
    action = request.action
    rule = request.rule

    print(f"[AI-NOTIFY] Temporary rules: company={company_id}, action={action}, rule_id={rule.id}")

    try:
        # Inicializar store da empresa se nao existir
        if company_id not in company_rules_store:
            company_rules_store[company_id] = {}

        if action == "create" or action == "update":
            # Adicionar/atualizar regra
            company_rules_store[company_id][rule.id] = {
                "id": rule.id,
                "title": rule.title,
                "rule_content": rule.rule_content,
                "description": rule.description,
                "valid_from": rule.valid_from,
                "valid_until": rule.valid_until,
                "is_paused": rule.is_paused,
                "updated_at": datetime.now().isoformat()
            }
            print(f"[AI-NOTIFY] Regra {action}: {rule.title}")

        elif action == "delete":
            # Remover regra
            if rule.id in company_rules_store[company_id]:
                del company_rules_store[company_id][rule.id]
                print(f"[AI-NOTIFY] Regra removida: {rule.title}")
            else:
                print(f"[AI-NOTIFY] Regra nao encontrada para remover: {rule.id}")

        elif action == "active":
            # Ativar regra
            if rule.id in company_rules_store[company_id]:
                company_rules_store[company_id][rule.id]["is_paused"] = False
                print(f"[AI-NOTIFY] Regra ativada: {rule.title}")
            else:
                # Se nao existe, criar
                company_rules_store[company_id][rule.id] = {
                    "id": rule.id,
                    "title": rule.title,
                    "rule_content": rule.rule_content,
                    "description": rule.description,
                    "valid_from": rule.valid_from,
                    "valid_until": rule.valid_until,
                    "is_paused": False,
                    "updated_at": datetime.now().isoformat()
                }
                print(f"[AI-NOTIFY] Regra criada e ativada: {rule.title}")

        elif action == "inactive":
            # Pausar regra
            if rule.id in company_rules_store[company_id]:
                company_rules_store[company_id][rule.id]["is_paused"] = True
                print(f"[AI-NOTIFY] Regra pausada: {rule.title}")
            else:
                print(f"[AI-NOTIFY] Regra nao encontrada para pausar: {rule.id}")

        # Log estado atual
        active_rules = sum(1 for r in company_rules_store[company_id].values() if not r.get("is_paused", False))
        total_rules = len(company_rules_store[company_id])
        print(f"[AI-NOTIFY] Estado: {active_rules}/{total_rules} regras ativas para empresa {company_id}")

        return WebhookNotificationResponse(
            success=True,
            message=f"Temporary rule {action}: {rule.title}"
        )

    except Exception as e:
        import traceback
        print(f"[AI-NOTIFY] ERRO em temporary_rules:")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# ENDPOINTS AUXILIARES - Consultar dados armazenados
# =============================================================================

@app.get("/api/company/{company_id}/instructions")
async def get_company_instructions(
    company_id: str,
    x_api_key: str = Header(..., alias="x-api-key")
):
    """Retorna as instructions armazenadas para uma empresa."""
    if not _api_key_matches(x_api_key, settings.API_KEY_ZELLU_IA):
        raise HTTPException(status_code=401, detail="API Key invalida")

    if company_id not in company_instructions_store:
        return {"company_id": company_id, "instructions": None, "message": "Nenhuma instruction armazenada"}

    return {
        "company_id": company_id,
        "instructions": company_instructions_store[company_id]
    }


@app.get("/api/company/{company_id}/rules")
async def get_company_rules(
    company_id: str,
    x_api_key: str = Header(..., alias="x-api-key")
):
    """Retorna as regras temporarias armazenadas para uma empresa."""
    if not _api_key_matches(x_api_key, settings.API_KEY_ZELLU_IA):
        raise HTTPException(status_code=401, detail="API Key invalida")

    if company_id not in company_rules_store:
        return {"company_id": company_id, "rules": [], "message": "Nenhuma regra armazenada"}

    rules = list(company_rules_store[company_id].values())
    active_rules = [r for r in rules if not r.get("is_paused", False)]

    return {
        "company_id": company_id,
        "total_rules": len(rules),
        "active_rules": len(active_rules),
        "rules": rules
    }


# =============================================================================
# ENDPOINT: POST /api/ai/validate-company
# =============================================================================
# Busca empresa na base de dados Zellu por CNPJ, Nome ou Nome Fantasia
# Conforme documentacao company-management/webhook-spec.md
# =============================================================================

from api.schemas import ValidateCompanyRequest, ValidateCompanyResponse, ValidateCompanyData

@app.post("/api/ai/validate-company", response_model=ValidateCompanyResponse)
async def validate_company(
    request: ValidateCompanyRequest,
    x_api_key: str = Header(..., alias="x-api-key")
):
    """
    Valida se uma empresa existe na base de dados Zellu.

    Busca por:
    - CNPJ (formatado XX.XXX.XXX/XXXX-XX ou apenas digitos)
    - companyName (razao social, case-insensitive)
    - tradeName (nome fantasia, case-insensitive)

    Retorna apenas empresas com status = 'active'.

    Headers:
        x-api-key: Chave de autenticacao

    Request Body:
        identifier: CNPJ, Nome da Empresa ou Nome Fantasia

    Response:
        - 200 OK: { success: true, data: {...} } ou { success: false, error: "..." }
        - 400 Bad Request: Identifier ausente
        - 401 Unauthorized: API Key invalida
    """
    # Validar API Key
    if not _api_key_matches(x_api_key, settings.API_KEY_ZELLU_IA):
        raise HTTPException(status_code=401, detail="API Key invalida")

    identifier = request.identifier.strip()
    if not identifier:
        raise HTTPException(status_code=400, detail="Campo 'identifier' e obrigatorio")

    print(f"[VALIDATE-COMPANY] Buscando: '{identifier}'")

    try:
        # Usar o CompanySearchClient para buscar no backend Zellu
        from src.company_search_client import CompanySearchClient

        # Inicializar cliente (usa config do settings)
        client = CompanySearchClient()
        result = await client.search(identifier)

        if result.found:
            # Montar response com dados da empresa
            company_data = ValidateCompanyData(
                id=result.company_id or "",
                cnpj=result.cnpj or "",
                companyName=result.company_name or "",
                tradeName=result.trade_name,
                segmentName=result.segment_name,
                status=result.status or "active"
            )

            print(f"[VALIDATE-COMPANY] Empresa encontrada: {result.display_name}")
            return ValidateCompanyResponse(
                success=True,
                data=company_data
            )
        else:
            # Empresa nao encontrada
            error_msg = result.error or "Empresa nao encontrada na base de dados"
            print(f"[VALIDATE-COMPANY] Nao encontrada: {error_msg}")
            return ValidateCompanyResponse(
                success=False,
                error=error_msg
            )

    except Exception as e:
        import traceback
        print(f"[VALIDATE-COMPANY] ERRO:")
        print(traceback.format_exc())
        raise HTTPException(
            status_code=500,
            detail=f"Erro ao validar empresa: {str(e)}"
        )


# =============================================================================
# ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "src.main:app",
        host=settings.API_HOST,
        port=settings.API_PORT,
        reload=False
    )
