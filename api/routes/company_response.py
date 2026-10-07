# -*- coding: utf-8 -*-
"""
Rotas do Company Response AI.

Implementa o endpoint que recebe mensagens da Zellu e gera respostas
em nome da empresa, conforme especificacao company-response.

Endpoints:
- POST /api/webhooks/company/ticket-response - Endpoint principal (producao)
- POST /api/webhooks/company/ticket-response/mock - Endpoint mock (testes)

Conforme documentacao:
- webhook-spec.md: Especificacao do webhook
- payload-spec.md: Estrutura do payload
- setup-guide.md: Configuracao mock/producao
"""

from fastapi import APIRouter, Request, HTTPException, Header, Depends, BackgroundTasks
from fastapi.responses import JSONResponse
from typing import Optional, Dict, Any
from datetime import datetime
from collections import defaultdict
import asyncio
import time

from src.llm import ChatModel
from src.llm import system_message, user_message, assistant_message

from config import Settings
from src.prompt_generator import build_legal_context_for_negotiation
from api.schemas_company_response import (
    CompanyResponsePayload,
    CompanyAIResponse,
    WebhookTriggerRequest,
    WebhookSuccessResponse,
    WebhookErrorResponse
)
from api.schemas_document_generation import DocumentGenerationRequest
from src.services.document_generation_service import DocumentGenerationService
from src.services.ai_usage_tracker import track_usage, new_event_id

# Routers
router = APIRouter(prefix="/api/webhooks/company", tags=["Company Response AI"])

# Settings
settings = Settings()


def verify_api_key(x_api_key: str = Header(..., alias="x-api-key")):
    """Verify API key for protected endpoints (comunicação Backend <-> IA)."""
    if x_api_key != settings.API_KEY_ZELLU_IA:
        raise HTTPException(status_code=401, detail="Não autorizado")
    return x_api_key


# Rate limiting storage (por sessionId)
_rate_limit_cache: Dict[str, list] = defaultdict(list)

# Idempotency cache (messageId -> response)
_idempotency_cache: Dict[str, dict] = {}

# Mock storage (local - sera sincronizado com main.py via funcoes)
# Para evitar circular imports, usamos storage local que pode ser populado externamente
_mock_companies: Dict[str, dict] = {}
_mock_documents: Dict[str, list] = {}

# Document generation service (inicializado via set_document_generation_service)
_doc_gen_service: Optional[DocumentGenerationService] = None


def set_mock_storage(companies: Dict[str, dict], documents: Dict[str, list]):
    """Configura storage mock externo (chamado pelo main.py)."""
    global _mock_companies, _mock_documents
    _mock_companies = companies
    _mock_documents = documents


def get_mock_storage():
    """Retorna storage mock."""
    return _mock_companies, _mock_documents


def set_document_generation_service(service: DocumentGenerationService):
    """Configura o servico de geracao de documentos (chamado pelo main.py)."""
    global _doc_gen_service
    _doc_gen_service = service


def _clean_llm_response(content: str) -> str:
    """Remove aspas literais que o LLM pode adicionar ao redor da resposta."""
    result = content.strip()
    if (result.startswith('"') and result.endswith('"')) or \
       (result.startswith("'") and result.endswith("'")):
        result = result[1:-1]
    return result.strip()


def check_rate_limit(session_id: str) -> bool:
    """
    Verifica rate limit por sessionId.

    Limite: COMPANY_WEBHOOK_RATE_LIMIT requisicoes por minuto.

    Returns:
        True se dentro do limite, False se excedeu
    """
    now = time.time()
    window = 60  # 1 minuto
    limit = settings.COMPANY_WEBHOOK_RATE_LIMIT

    # Limpar entradas antigas
    _rate_limit_cache[session_id] = [
        t for t in _rate_limit_cache[session_id]
        if now - t < window
    ]

    # Verificar limite
    if len(_rate_limit_cache[session_id]) >= limit:
        return False

    # Registrar requisicao
    _rate_limit_cache[session_id].append(now)
    return True


def get_retry_after(session_id: str) -> int:
    """Calcula segundos para retry apos rate limit."""
    if not _rate_limit_cache[session_id]:
        return 0
    oldest = min(_rate_limit_cache[session_id])
    return max(1, int(60 - (time.time() - oldest)))


async def process_with_llm(
    payload: CompanyResponsePayload,
    company_config: dict,
    documents: list,
    session_id: Optional[str] = None,
    company_id: Optional[str] = None,
    ticket_id: Optional[str] = None,
) -> CompanyAIResponse:
    """
    Processa payload e gera resposta usando LLM.

    Args:
        payload: Payload recebido da Zellu
        company_config: Configuracao da empresa (system_prompt, etc)
        documents: Documentos da base de conhecimento

    Returns:
        CompanyAIResponse com a resposta gerada
    """
    context = payload.context or {}
    message_history = payload.messageHistory or []
    knowledge_bases = payload.knowledgeBases or []
    rules = payload.rules or []
    config = payload.config or {}

    ticket = context.ticket or {}
    client = context.client or {}
    company_info = context.company or {}

    # Pegar ultima mensagem do ZelinhU (Sender)
    last_zelinhu_message = ""
    for msg in reversed(message_history):
        if msg.origin == "zelinhu":
            last_zelinhu_message = msg.body
            break

    # Contar rodada de negociacao
    company_messages_count = sum(1 for msg in message_history if msg.origin == "company_ai")
    negotiation_round = company_messages_count + 1

    print(f"[COMPANY-RESPONSE] Rodada de negociacao: {negotiation_round}")
    if last_zelinhu_message:
        print(f"[COMPANY-RESPONSE] Ultima mensagem Sender: {last_zelinhu_message[:100]}...")

    # Montar contexto dos documentos
    doc_context = ""
    if documents:
        doc_context = "\n\nDOCUMENTOS DA BASE DE CONHECIMENTO DA EMPRESA:\n"
        for doc in documents:
            doc_context += f"\n--- {doc.get('filename', 'Documento')} ---\n{doc.get('content', '')[:2000]}\n"

    # Montar contexto das knowledge bases (se fornecidas no payload)
    if knowledge_bases:
        doc_context += "\n\nBASES DE CONHECIMENTO ATIVAS:\n"
        for kb in knowledge_bases:
            doc_context += f"- {kb.name or 'Base'}: {kb.description or ''}\n"

    # Montar regras temporarias
    rules_context = ""
    if rules:
        rules_context = "\n\nREGRAS TEMPORARIAS ATIVAS:\n"
        for rule in rules:
            rules_context += f"- {rule.name or 'Regra'}: {rule.content or ''}\n"

    # Extrair informacoes do contexto
    estimated_value = ticket.estimatedValue or 0

    # System prompt da empresa
    base_prompt = company_config.get("system_prompt", "Voce e um assistente de atendimento empresarial profissional.")

    # Buscar contexto legal baseado na categoria do ticket
    ticket_category = ticket.category or 'Geral'
    legal_context = build_legal_context_for_negotiation(ticket_category)

    # Instrucoes comportamentais para IA da empresa em negociacao
    negotiation_instructions = f"""
=== CONTEXTO DA NEGOCIACAO ===

Voce esta respondendo a uma NEGOCIACAO enviada pelo ZelinhU (representante do cliente).
Este NAO e um atendimento comum - e uma tentativa de resolucao de conflito.

INFORMACOES DO CASO:
- Cliente: {client.name or 'Cliente'}
- Ticket: {ticket.title or 'Solicitacao'}
- Descricao: {ticket.description or ''}
- Valor estimado da causa: R$ {estimated_value:.2f}
- Categoria: {ticket_category}
- Rodada de negociacao: {negotiation_round}

SUA EMPRESA:
- Nome: {company_info.name or company_config.get('name', 'Empresa')}
- Segmento: {company_info.segment or 'Geral'}

=== ESTRATEGIA DE NEGOCIACAO ===

Voce defende os interesses da empresa, mas sempre respeitando a lei.
Seu objetivo e encontrar a solucao mais JUSTA para AMBAS as partes, NAO ceder a toda demanda.

PRINCIPIO FUNDAMENTAL - OBRIGACAO LEGAL vs NEGOCIAVEL:
Antes de responder qualquer demanda, CLASSIFIQUE:

OBRIGACAO LEGAL (voce DEVE cumprir - nao negocie, resolva):
- Troca/reparo de produto com defeito dentro do prazo legal
- Direito de arrependimento em 7 dias para compras online
- Devolucao em dobro de cobranca indevida comprovada
- Cumprimento de oferta/publicidade veiculada

NEGOCIAVEL (voce pode e DEVE argumentar):
- Valor de compensacao/indenizacao por danos morais
- Forma da solucao (voucher, credito, troca vs reembolso total)
- Prazos alem do minimo legal
- Beneficios extras (frete gratis, desconto futuro)
- Pedidos sem comprovacao clara de dano

COMO NEGOCIAR POR RODADA:
- Rodada 1-2: ENTENDA o problema. Faca perguntas, peca detalhes e comprovantes. Ofereca solucao conforme politica da empresa. NAO ofereca o maximo de cara.
- Rodada 3-4: Se a primeira oferta foi recusada, melhore GRADUALMENTE com justificativa. Ofereca alternativas (ex: "Se o reembolso total nao e possivel, podemos oferecer troca + voucher de desconto").
- Rodada 5+: Apresente oferta final FIRME. Explique que e o maximo que a empresa pode oferecer dentro das politicas internas.

TATICAS DE ARGUMENTACAO:
1. Peca COMPROVANTES antes de ceder (nota fiscal, protocolo, fotos do defeito)
2. Questione valores inflados citando referencias legais e jurisprudencia
3. Ofereca ALTERNATIVAS (voucher, credito na loja, troca) antes de reembolso total
4. Reconheca o problema mas proponha solucao PROPORCIONAL ao dano real
5. Cite artigos de lei e politicas da empresa para fundamentar sua posicao

QUANDO RESISTIR:
- Pedido de indenizacao com valor muito acima do razoavel
- Reclamacao sem comprovacao (peca provas antes de ceder)
- Pedido fora do prazo legal (informe o prazo com respeito)
- Demandas sem amparo legal (explique por que nao se aplica)

QUANDO CEDER:
- Obrigacao legal clara e comprovada
- Erro comprovado da empresa
- Valor baixo onde negar custa mais que resolver
- Cliente apresentou documentacao solida

{legal_context}

OBRIGATORIO:
- Seja PROFISSIONAL e CORDIAL
- Sempre apresente valores concretos nas propostas
- Justifique suas ofertas com base nas politicas da empresa E na legislacao
- NAO use emojis
- NAO redirecione para outros canais
- NAO aceite automaticamente qualquer demanda - analise, fundamente e proponha

QUANDO FECHAR ACORDO:
Se o cliente/representante aceitar sua proposta ou fizer contra-proposta razoavel:
- Confirme o acordo de forma clara
- Liste os termos acordados
- Finalize com "[ACORDO FECHADO]"

{rules_context}
{doc_context}

=== FIM DO CONTEXTO ===
"""

    full_system_prompt = f"{base_prompt}\n{negotiation_instructions}"

    # Construir historico de mensagens para o LLM
    messages = [system_message(content=full_system_prompt)]

    for msg in message_history:
        origin = msg.origin
        body = msg.body

        if origin == "zelinhu":
            messages.append(user_message(content=body))
        elif origin in ["company_ai", "company_user", "company_manual"]:
            messages.append(assistant_message(content=body))

    # Se nao ha mensagem do Sender no historico, adicionar a atual
    if last_zelinhu_message and not any(isinstance(m, dict) and m.get('role') == 'user' for m in messages[1:]):
        messages.append(user_message(content=last_zelinhu_message))

    # Chamar LLM
    model_used = config.model or settings.COMPANY_RESPONSE_AI_MODEL
    llm = ChatModel(
        model=model_used,
        temperature=config.temperature or settings.COMPANY_RESPONSE_AI_TEMPERATURE,
        max_tokens=config.maxTokens or settings.COMPANY_RESPONSE_AI_MAX_TOKENS,
        api_key=settings.OPENAI_API_KEY
    )

    _t0 = time.perf_counter()
    response = await llm.complete(messages)
    _duration_ms = int((time.perf_counter() - _t0) * 1000)
    response_body = _clean_llm_response(response.content)

    # Observabilidade de custo (fire-and-forget) - contrato /api/ai/usage
    track_usage(
        module="company_ai",
        model=model_used,
        response=response,
        event_type="negotiation",
        profile="empresa",  # IA respondendo em nome da empresa
        session_id=session_id,
        company_id=company_id or company_config.get("company_id") or company_config.get("id"),
        ticket_id=ticket_id,
        duration_ms=_duration_ms,
    )

    print(f"[COMPANY-RESPONSE] Resposta gerada: {response_body[:150]}...")

    # Verificar se negociacao foi concluida
    negotiation_complete = "[ACORDO FECHADO]" in response_body or "[CASO FINALIZADO]" in response_body

    if negotiation_complete:
        print(f"[COMPANY-RESPONSE] *** ACORDO DETECTADO! negotiationComplete=true ***")

    # Calcular tokens (aproximacao)
    tokens_used = len(response_body.split()) * 2

    return CompanyAIResponse(
        messageBody=response_body,
        messageType="text",
        tokensUsed=tokens_used,
        modelUsed=config.model or settings.COMPANY_RESPONSE_AI_MODEL,
        negotiationComplete=negotiation_complete
    )


async def _handle_document_generation(data: dict) -> JSONResponse:
    """
    Roteia requests com action='generate_legal_document' para o serviço de geração.

    Valida o payload, inicia o processamento em background e retorna 200 imediato.
    O resultado será entregue via callback (callbackUrl).
    """
    print(f"[COMPANY-RESPONSE] Action: generate_legal_document detectado")
    print(f"[COMPANY-RESPONSE] TicketId: {data.get('ticketId')}")

    if _doc_gen_service is None:
        print(f"[COMPANY-RESPONSE] ERRO: DocumentGenerationService nao inicializado")
        return JSONResponse(
            status_code=500,
            content={"error": "Document generation service not initialized"}
        )

    # Parsear payload conforme schema de document generation
    try:
        doc_request = DocumentGenerationRequest(**data)
    except Exception as e:
        print(f"[COMPANY-RESPONSE] Erro ao parsear document generation payload: {e}")
        return JSONResponse(
            status_code=400,
            content={"error": f"Invalid document generation payload: {str(e)}"}
        )

    # Processar em background (nao bloqueia o endpoint)
    asyncio.ensure_future(_doc_gen_service.process_document_generation(doc_request))

    print(f"[COMPANY-RESPONSE] Documento em geração (background) para ticket {doc_request.ticketId}")

    return JSONResponse(
        status_code=200,
        content={
            "success": True,
            "message": "Document generation started",
            "ticketId": doc_request.ticketId,
        }
    )


@router.post(
    "/ticket-response",
    response_model=CompanyAIResponse,
    responses={
        200: {"description": "Resposta da IA gerada com sucesso"},
        400: {"description": "Payload invalido"},
        401: {"description": "Nao autorizado"},
        429: {"description": "Rate limit excedido"},
        502: {"description": "Erro no servico de IA"}
    },
    summary="Processar Mensagem e Gerar Resposta",
    description="Endpoint principal que recebe payload da Zellu e gera resposta da IA da empresa."
)
async def process_ticket_response(
    request: Request,
    api_key: str = Depends(verify_api_key)
):
    """
    Endpoint principal do Company Response AI.

    AUTENTICAÇÃO:
    - Requer header x-api-key com a chave configurada em API_KEY_ZELLU_IA

    Recebe payload completo conforme payload-spec.md e gera resposta
    usando LLM + documentos da empresa.

    Este endpoint:
    1. Valida o payload
    2. Verifica rate limit por sessionId
    3. Verifica idempotency por messageId (se fornecido)
    4. Processa com LLM usando contexto da empresa
    5. Retorna response conforme especificacao

    Returns:
        CompanyAIResponse com messageBody, tokensUsed, modelUsed, negotiationComplete
    """
    try:
        data = await request.json()
        print(f"[COMPANY-RESPONSE] ========================================")
        print(f"[COMPANY-RESPONSE] Recebendo payload")

        # ===== ROTEAMENTO POR ACTION =====
        # Se action == "generate_legal_document", delegar para geracao de documentos
        action = data.get("action")
        if action == "generate_legal_document":
            return await _handle_document_generation(data)

        # ===== FLUXO NORMAL (Company Response) =====
        # Tentar parsear como payload completo ou webhook trigger
        session_id = data.get("sessionId", "")
        message_id = data.get("lastMessageId") or data.get("messageId")

        print(f"[COMPANY-RESPONSE] SessionId: {session_id}")
        print(f"[COMPANY-RESPONSE] MessageId: {message_id}")

        if not session_id:
            return JSONResponse(
                status_code=400,
                content={"error": "Missing required field: sessionId"}
            )

        # Rate limit check
        if not check_rate_limit(session_id):
            retry_after = get_retry_after(session_id)
            print(f"[COMPANY-RESPONSE] Rate limit excedido para session {session_id}")
            return JSONResponse(
                status_code=429,
                content={
                    "error": "Rate limit exceeded",
                    "message": "Too many requests for this session",
                    "retryAfter": retry_after
                },
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(settings.COMPANY_WEBHOOK_RATE_LIMIT),
                    "X-RateLimit-Remaining": "0"
                }
            )

        # Idempotency check
        if message_id and message_id in _idempotency_cache:
            cached = _idempotency_cache[message_id]
            print(f"[COMPANY-RESPONSE] Message {message_id} ja processada (idempotency check)")
            return JSONResponse(
                status_code=200,
                content=cached
            )

        # Parsear payload
        try:
            payload = CompanyResponsePayload(**data)
        except Exception as e:
            print(f"[COMPANY-RESPONSE] Erro ao parsear payload: {e}")
            return JSONResponse(
                status_code=400,
                content={"error": f"Invalid payload format: {str(e)}"}
            )

        print(f"[COMPANY-RESPONSE] Historico: {len(payload.messageHistory or [])} mensagens")

        # Buscar configuracao da empresa
        mock_companies, mock_documents = get_mock_storage()

        # Extrair company_id do sessionId ou context
        company_id = None
        company_context = payload.context.company if payload.context else None

        # Tentar extrair do sessionId (formato: "negotiation_{company_id}_{uuid}")
        if session_id and "_" in session_id:
            parts = session_id.split("_")
            if len(parts) >= 2:
                company_id = parts[1]

        # Se nao encontrou, tentar do context
        if not company_id and company_context:
            company_id = company_context.id or ""

        # Fallback para primeira empresa mock disponivel
        if not company_id or company_id not in mock_companies:
            if mock_companies:
                company_id = list(mock_companies.keys())[0]
                print(f"[COMPANY-RESPONSE] Usando primeira empresa disponivel: {company_id}")
            else:
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": "Nenhuma empresa mock configurada",
                        "message": "Configure primeiro na aba 'Configurar Empresa' do painel de teste."
                    }
                )

        company_config = mock_companies.get(company_id, {})
        documents = mock_documents.get(company_id, [])

        print(f"[COMPANY-RESPONSE] Empresa: {company_config.get('name', company_id)}")
        print(f"[COMPANY-RESPONSE] Documentos: {len(documents)}")

        # Processar com LLM
        ticket_id = data.get("ticketId") or (
            payload.context.ticket.id if payload.context and getattr(payload.context, "ticket", None) and hasattr(payload.context.ticket, "id") else None
        )
        response = await process_with_llm(
            payload,
            company_config,
            documents,
            session_id=session_id,
            company_id=company_id,
            ticket_id=ticket_id,
        )

        # Cachear para idempotency
        if message_id:
            _idempotency_cache[message_id] = response.model_dump()

            # Limpar cache antigo (manter ultimas 100)
            if len(_idempotency_cache) > 100:
                oldest_key = next(iter(_idempotency_cache))
                del _idempotency_cache[oldest_key]

        return response

    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print(f"[COMPANY-RESPONSE] ERRO:")
        print(error_trace)
        return JSONResponse(
            status_code=502,
            content={
                "error": "Failed to get AI response",
                "details": str(e)
            }
        )


@router.post(
    "/ticket-response/mock",
    response_model=CompanyAIResponse,
    summary="Processar Mensagem (Mock)",
    description="Endpoint mock para testes - comportamento identico ao principal."
)
async def process_ticket_response_mock(
    request: Request,
    api_key: str = Depends(verify_api_key)
):
    """
    Endpoint MOCK que simula a IA da Empresa.

    AUTENTICAÇÃO:
    - Requer header x-api-key com a chave configurada em API_KEY_ZELLU_IA

    Comportamento identico ao endpoint principal, mantido para
    compatibilidade com testes existentes.
    """
    # Delegar para o endpoint principal (api_key já validada)
    return await process_ticket_response(request, api_key)


# Endpoint adicional para verificar status/saude do servico
@router.get(
    "/health",
    summary="Health Check do Company Response AI",
    description="Verifica se o servico esta funcionando."
)
async def health_check():
    """Health check do servico Company Response AI."""
    mock_companies, mock_documents = get_mock_storage()

    return {
        "status": "healthy",
        "service": "Company Response AI",
        "mode": "mock" if settings.COMPANY_RESPONSE_USE_MOCK else "production",
        "mock_companies_configured": len(mock_companies),
        "rate_limit": settings.COMPANY_WEBHOOK_RATE_LIMIT,
        "ai_model": settings.COMPANY_RESPONSE_AI_MODEL
    }
