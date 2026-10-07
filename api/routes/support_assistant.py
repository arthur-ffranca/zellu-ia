# -*- coding: utf-8 -*-
"""
Rota do Support Assistant.

Endpoint unico que o backend Zellu chama quando o usuario manda uma
mensagem no chat do assistente.

Contrato: docs/webhook-spec.md (v1.0).

Pipeline:
1. Backend faz POST /webhook/support-assistant com a pergunta + perfil + URL de callback.
2. Validamos x-api-key, respondemos 200 imediato (a request do usuario nao espera).
3. Processamos em background:
   a) Transcrevemos audio (Whisper) se houver - mesmo helper do chat.
   b) Selecionamos o system prompt em funcao de profile_type + is_authenticated.
   c) Chamamos GuideAgent.ask (RAG via GuideRAG, namespace zellu_guide).
   d) POST no callbackUrl com x-api-key + retry com backoff em 5xx/timeout.

NAO emitimos analysis_data: este contrato eh Q&A puro, sem extracao
estruturada de caso (ver webhook-spec.md §"Sem analysis_data").
"""

import asyncio
import json
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request

from agents.guide_agent import GuideAgent
from api.schemas_support_assistant import (
    SupportAssistantCallbackPayload,
    SupportAssistantRequest,
)
from config import get_settings
from src.services.audio_transcription import (
    merge_text_with_audio,
    transcribe_urls,
)
from src.services.guide_rag import GuideRAG
from src.services.webhook_signature import build_signed_headers


router = APIRouter(prefix="/webhook", tags=["Support Assistant"])

# Settings (singleton)
_settings = get_settings()

# Dedup por messageId (memoria local; backend pode reenviar o mesmo id em
# retry de 5xx por exemplo). Cap em 500 entradas para nao crescer infinito.
_processed_messages: Dict[str, bool] = {}
_DEDUP_CAP = 500

# Instancia compartilhada do GuideAgent + GuideRAG (criada lazy na 1a chamada
# para nao quebrar o startup se o Pinecone nao estiver disponivel).
_guide_agent: Optional[GuideAgent] = None


def _verify_api_key(x_api_key: str = Header(..., alias="x-api-key")):
    """Mesma chave usada no chat de negociacao (webhook-spec.md §Configuracao)."""
    if x_api_key != _settings.API_KEY_ZELLU_IA:
        raise HTTPException(status_code=401, detail="Nao autorizado")
    return x_api_key


def _get_guide_agent() -> GuideAgent:
    """Lazy init do GuideAgent compartilhado."""
    global _guide_agent
    if _guide_agent is None:
        try:
            rag = GuideRAG()
            _guide_agent = GuideAgent(rag_service=rag)
            print("[SUPPORT-ASSIST] GuideAgent inicializado (RAG namespace=zellu_guide)")
        except Exception as exc:
            print(f"[SUPPORT-ASSIST] Falha ao iniciar GuideRAG (segue sem RAG): {exc}")
            _guide_agent = GuideAgent(rag_service=None)
    return _guide_agent


# ============================================================================
# Modulacao do system prompt por perfil (webhook-spec.md §Responsabilidade)
# ============================================================================

_BASE_PROMPT = """Voce e o assistente virtual da Zellu, plataforma juridica que ajuda clientes
a resolverem problemas com empresas atraves de chamados (amigavel, extrajudicial
ou judicial).

Funcao:
- Responder perguntas sobre como usar a plataforma, fluxos, recursos e politicas.
- USAR SEMPRE o CONTEXTO RECUPERADO abaixo como fonte de verdade. Se a resposta
  nao estiver no contexto, admita honestamente e direcione o usuario para o
  caminho certo (suporte/advogado parceiro/abrir um chamado).
- Manter respostas curtas e diretas (1-3 paragrafos), em portugues brasileiro,
  sem markdown pesado.
- Nao inventar artigos/leis fora do contexto.
- Nao oferecer parecer juridico personalizado - esse e o papel dos advogados.

REGRA DE ISOLAMENTO POR PERFIL (CRITICA):
- Voce esta atendendo um perfil especifico (veja "PERFIL DO USUARIO" abaixo).
- A base de conhecimento foi pre-filtrada para esse perfil. Voce so deve
  responder com base no CONTEXTO RECUPERADO que vier abaixo.
- NUNCA descreva ou compare funcionalidades de OUTROS perfis (ex.: para um
  advogado, nao explique recursos do painel da empresa, nem o fluxo do
  consumidor, e vice-versa). Cada persona enxerga so o que faz parte do seu
  dia a dia na plataforma.
- Se o usuario perguntar algo claramente fora do seu perfil (ex.: cliente
  perguntando como funciona o painel do advogado), responda apenas que esse
  recurso e exclusivo daquele perfil e oriente o canal correto (cadastro,
  contato com o time da Zellu, etc.). Nao detalhe o conteudo.
"""

# Apêndice por perfil. Modula tom, escopo e reforça o que NAO pode falar.
_PROFILE_APPENDIX = {
    None: """
PERFIL DO USUARIO: visitante deslogado (sem perfil definido).
- Nao assuma quem esta perguntando.
- Resposta generica e publica: explique a plataforma como funciona em alto nivel.
- Para acoes que exigem login (abrir/acompanhar chamado, painel, etc.),
  oriente a se cadastrar / fazer login. Nao acesse dados privados.
- NAO descreva o painel do cliente, do advogado ou da empresa. Esses
  detalhes sao exclusivos de cada perfil logado.
""",
    "client": """
PERFIL DO USUARIO: cliente (consumidor com algum problema contra empresa).
- Tom: empatico, simples, sem jargao juridico desnecessario.
- Foco: como abrir/acompanhar um chamado, o que esperar do fluxo amigavel ->
  extrajudicial -> judicial, prazos, documentos necessarios.
- Pode sugerir acoes praticas dentro da plataforma do cliente.
- NAO explique o painel do advogado, o painel da empresa, nem o fluxo
  interno desses perfis. Se o cliente perguntar, diga apenas que essas
  funcionalidades sao do perfil do advogado/empresa e nao detalhe.
""",
    "lawyer": """
PERFIL DO USUARIO: advogado parceiro.
- Tom: tecnico mas conciso.
- Foco: oportunidades de caso, agenda, geracao de documentos pela IA,
  assinatura digital, painel do advogado, gestao de chamados que o advogado
  pegou, OAB, creditos, perfil.
- Pode usar vocabulario tecnico (peticao, tutela, contestacao) quando o
  contexto suportar.
- NAO explique o painel do cliente nem o da empresa, nem o fluxo interno
  desses perfis. Se o advogado perguntar, diga apenas que esses recursos
  pertencem a outro perfil e nao detalhe.
""",
    "company_owner": """
PERFIL DO USUARIO: dono de empresa cadastrada na Zellu.
- Tom: profissional.
- Foco: gestao da empresa, recebimento e resposta a chamados, modos
  manual/IA, configuracao da IA da empresa, equipe, reputacao, creditos.
- NAO explique o painel do cliente nem o do advogado. Se perguntar sobre
  isso, oriente que sao recursos de outros perfis e nao detalhe.
""",
    "company_member": """
PERFIL DO USUARIO: membro de uma empresa cadastrada.
- Tom: profissional e operacional.
- Foco: como responder chamados, ferramentas do painel da empresa, modos
  de atendimento, equipe.
- NAO explique o painel do cliente nem o do advogado. Se perguntar, oriente
  que sao recursos de outros perfis e nao detalhe.
""",
    "company_admin": """
PERFIL DO USUARIO: admin de uma empresa cadastrada.
- Tom: profissional.
- Foco: configuracao da empresa, permissoes, integracoes, base de conhecimento,
  membros da equipe, configuracao da IA da empresa, regras temporarias.
- NAO explique o painel do cliente nem o do advogado. Se perguntar, oriente
  que sao recursos de outros perfis e nao detalhe.
""",
    "admin": """
PERFIL DO USUARIO: administrador da plataforma Zellu.
- Tom: tecnico e direto.
- Pode discutir aspectos internos de QUALQUER perfil (cliente/advogado/
  empresa/visitante) - admin enxerga tudo. Use o contexto recuperado como
  fonte de verdade.
""",
}


def _build_system_prompt(profile_type: Optional[str], is_authenticated: bool) -> str:
    """Monta o system prompt em funcao do perfil + autenticacao."""
    appendix = _PROFILE_APPENDIX.get(profile_type) or _PROFILE_APPENDIX[None]
    auth_note = (
        "Usuario logado (is_authenticated=true)."
        if is_authenticated
        else "Usuario NAO logado (is_authenticated=false) - nao acesse dados privados."
    )
    return f"{_BASE_PROMPT}\n{appendix}\n{auth_note}\n"


# Mapeamento profile_type -> audience usado pra filtrar o RAG.
# - Os 3 perfis de empresa colapsam em 'company' (o doc 04-empresa.md cobre os 3).
# - admin nao tem audience -> sem filtro -> ve todos os chunks (debug/observabilidade).
# - visitante deslogado (None) vai pra 'visitor'.
_PROFILE_TO_AUDIENCE: Dict[Optional[str], Optional[str]] = {
    None: "visitor",
    "client": "client",
    "lawyer": "lawyer",
    "company_owner": "company",
    "company_member": "company",
    "company_admin": "company",
    "admin": None,
}


def _profile_to_audience(profile_type: Optional[str]) -> Optional[str]:
    """Retorna o audience pra filtrar o RAG, dado o profile_type do request.

    None (visitante) -> 'visitor'. admin -> None (sem filtro - ve tudo).
    profile_type desconhecido cai em 'visitor' por seguranca.
    """
    if profile_type not in _PROFILE_TO_AUDIENCE:
        return "visitor"
    return _PROFILE_TO_AUDIENCE[profile_type]


def _to_user_profile(profile_type: Optional[str], is_authenticated: bool) -> Optional[str]:
    """Mapeia (profile_type, is_authenticated) -> userProfile do custo de IA.

    Deslogado ou sem profile_type => cliente_externo (visitante).
    lawyer => advogado; company_* => empresa; client => cliente_interno.
    admin nao e um dos 4 perfis de custo => None (fica null).
    """
    if not is_authenticated or profile_type is None:
        return "cliente_externo"
    if profile_type == "lawyer":
        return "advogado"
    if profile_type in ("company_owner", "company_member", "company_admin"):
        return "empresa"
    if profile_type == "client":
        return "cliente_interno"
    return None  # admin (ou desconhecido) -> sem atribuicao


# ============================================================================
# Callback IA -> Zellu (com retry de backoff)
# ============================================================================

# Recomendado pelo contrato: 1s, 5s, 15s (webhook-spec.md §Retry)
_CALLBACK_BACKOFF_S = (1, 5, 15)
_CALLBACK_TIMEOUT_S = 30.0


async def _send_callback(
    callback_url: str,
    payload: SupportAssistantCallbackPayload,
    request_id: str,
) -> None:
    """POST no callbackUrl da Zellu com retry em 5xx/timeout."""
    body = payload.model_dump(exclude_none=True)
    body_json = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    headers = {
        "Content-Type": "application/json",
        "x-api-key": _settings.API_KEY_ZELLU_IA,
    }
    if _settings.AI_WEBHOOK_HMAC_SECRET:
        headers.update(build_signed_headers(body_json, _settings.AI_WEBHOOK_HMAC_SECRET))

    last_exc: Optional[Exception] = None
    for attempt, _backoff in enumerate(_CALLBACK_BACKOFF_S, start=1):
        try:
            async with httpx.AsyncClient(timeout=_CALLBACK_TIMEOUT_S) as client:
                resp = await client.post(callback_url, content=body_json, headers=headers)
                if 200 <= resp.status_code < 300:
                    print(
                        f"[SUPPORT-ASSIST] Callback OK "
                        f"(attempt={attempt}, status={resp.status_code}, request_id={request_id})"
                    )
                    return
                if 400 <= resp.status_code < 500:
                    # Erro nao-retriavel (auth/validacao): nao tentar de novo
                    print(
                        f"[SUPPORT-ASSIST] Callback erro nao-retriavel "
                        f"(status={resp.status_code}, body={resp.text[:300]})"
                    )
                    return
                # 5xx -> cai no retry
                last_exc = RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
                print(
                    f"[SUPPORT-ASSIST] Callback 5xx (attempt={attempt}, "
                    f"status={resp.status_code}) - aguardando {_backoff}s"
                )
        except Exception as exc:
            last_exc = exc
            print(
                f"[SUPPORT-ASSIST] Callback erro (attempt={attempt}): {exc} - "
                f"aguardando {_backoff}s"
            )

        if attempt < len(_CALLBACK_BACKOFF_S):
            await asyncio.sleep(_backoff)

    print(f"[SUPPORT-ASSIST] Callback FALHOU apos {len(_CALLBACK_BACKOFF_S)} tentativas: {last_exc}")


# ============================================================================
# Processamento em background
# ============================================================================

async def _process_request_background(request: SupportAssistantRequest) -> None:
    """Pipeline completo: transcricao + GuideAgent + callback."""
    try:
        print(
            f"[SUPPORT-ASSIST] Processando id={request.id} room={request.room_id} "
            f"profile={request.profile_type} auth={request.is_authenticated} "
            f"type={request.message_type}"
        )

        # 1. Transcricao de audios (se houver)
        question = (request.message or "").strip()
        if request.audio:
            try:
                print(f"[SUPPORT-ASSIST] Transcrevendo {len(request.audio)} audio(s)...")
                transcriptions = await transcribe_urls(request.audio)
                question = merge_text_with_audio(question, transcriptions)
                print(f"[SUPPORT-ASSIST] Pergunta apos transcricao: {question[:120]!r}...")
            except Exception as exc:
                print(f"[SUPPORT-ASSIST] Transcricao falhou (segue com texto): {exc}")

        if not question:
            question = "[Mensagem sem texto utilizavel]"

        # 2. Selecao do system prompt por perfil
        system_prompt = _build_system_prompt(
            profile_type=request.profile_type,
            is_authenticated=request.is_authenticated,
        )

        # 3. Resposta via GuideAgent (RAG namespace zellu_guide, isolado por audience)
        audience = _profile_to_audience(request.profile_type)
        user_profile = _to_user_profile(request.profile_type, request.is_authenticated)
        print(
            f"[SUPPORT-ASSIST] Routing: profile={request.profile_type} "
            f"-> audience={audience or '(sem filtro - admin)'} "
            f"-> userProfile={user_profile or '(sem atribuicao)'}"
        )
        agent = _get_guide_agent()
        # Override do system_prompt apenas para esta consulta - sem mutar o agente
        # (assim multiplos requests com perfis diferentes nao se interferem)
        original_prompt = agent.system_prompt
        agent.system_prompt = system_prompt
        try:
            result = await agent.ask(question=question, audience=audience, profile=user_profile)
        finally:
            agent.system_prompt = original_prompt

        answer = (result.get("answer") or "").strip()
        if not answer:
            answer = (
                "Desculpe, nao consegui formular uma resposta agora. "
                "Tente reformular sua pergunta ou contate o suporte."
            )

        # 4. Montar payload do callback
        # messagedata recebe metadata leve para observabilidade no backend
        sources = result.get("sources") or []
        messagedata: Dict[str, Any] = {}
        if sources:
            messagedata["sources"] = [
                {
                    "source": s.get("source"),
                    "score": s.get("score"),
                }
                for s in sources[:5]
            ]
        if request.profile_type:
            messagedata["profile_type"] = request.profile_type
        if audience:
            messagedata["audience"] = audience
        messagedata["rag_hits"] = result.get("rag_hits", 0)

        payload = SupportAssistantCallbackPayload(
            room_id=request.room_id,
            message=answer,
            is_finished=True,
            message_type="text",
            messagedata=messagedata or None,
        )

        # 5. Disparar callback
        await _send_callback(
            callback_url=request.callbackUrl,
            payload=payload,
            request_id=request.id,
        )
    except Exception as exc:
        import traceback
        print(f"[SUPPORT-ASSIST] Erro fatal no processamento background:")
        print(traceback.format_exc())
        # Mesmo no erro, tenta avisar o usuario via callback para nao deixar
        # a sala silenciosa.
        try:
            fallback = SupportAssistantCallbackPayload(
                room_id=request.room_id,
                message=(
                    "Desculpe, tive um problema para processar sua pergunta. "
                    "Tente novamente em instantes."
                ),
                is_finished=True,
                message_type="text",
                messagedata={"error": str(exc)[:200]},
            )
            await _send_callback(
                callback_url=request.callbackUrl,
                payload=fallback,
                request_id=request.id,
            )
        except Exception as inner:
            print(f"[SUPPORT-ASSIST] Falha ao enviar fallback: {inner}")


# ============================================================================
# Endpoint
# ============================================================================

@router.post("/support-assistant")
async def support_assistant_webhook(
    request: Request,
    x_api_key: str = Depends(_verify_api_key),
):
    """Endpoint unico do Support Assistant.

    Conforme webhook-spec.md: responde 200 imediato e processa em background;
    a resposta da IA volta pelo callback (callbackUrl do request).
    """
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Payload nao e JSON valido")

    # Validacao do shape via Pydantic
    try:
        parsed = SupportAssistantRequest(**data)
    except Exception as exc:
        print(f"[SUPPORT-ASSIST] Payload invalido: {exc}")
        raise HTTPException(status_code=400, detail=f"Payload invalido: {exc}")

    # Dedup por messageId (request_id do contrato)
    if parsed.id in _processed_messages:
        print(f"[SUPPORT-ASSIST] DUPLICATA id={parsed.id} - ignorando")
        return {"status": "duplicate", "request_id": parsed.id}

    _processed_messages[parsed.id] = True
    # Cap simples para nao crescer infinito (FIFO aproximado)
    if len(_processed_messages) > _DEDUP_CAP:
        oldest = next(iter(_processed_messages))
        _processed_messages.pop(oldest, None)

    print(f"[SUPPORT-ASSIST] Recebido id={parsed.id} room={parsed.room_id} - 200 imediato")

    # Dispara o processamento sem aguardar (contrato: respond 200 rapido)
    asyncio.create_task(_process_request_background(parsed))

    return {"status": "accepted", "request_id": parsed.id}
