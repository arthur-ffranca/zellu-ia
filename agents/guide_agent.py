# -*- coding: utf-8 -*-
"""
Guide Agent — assistente de IA standalone que orienta usuarios via RAG.

ESCOPO
- Vive ISOLADO do native runtime (intake/classifier/analyst/writer/sender).
  Nao recebe ConversationState, nao participa do grafo, nao tem proximo node.
- Servira de assistente de FAQ/orientacao para usuarios; o contrato de
  comunicacao com o backend Zellu sera definido em momento posterior.

DESIGN
- Recupera contexto via RAGService.search (Pinecone + embeddings).
- Monta prompt grounded: system + historico + pergunta com chunks inline.
- Default usa COMPANY_RESPONSE_AI_MODEL (gpt-4o-mini) por custo - perguntas
  de orientacao nao precisam do modelo top-tier; o RAG faz o trabalho pesado.
- Tolerante a falha do RAG: se search retornar vazio ou explodir, ainda
  responde, mas avisa que nao tem fonte e sugere falar com humano.

USO ISOLADO
    from agents.guide_agent import GuideAgent
    from src.rag_service import RAGService
    from config import get_settings

    rag = RAGService(get_settings())
    agent = GuideAgent(rag_service=rag)
    result = await agent.ask(
        question="Como funciona o fluxo de notificacao extrajudicial?",
        history=[
            {"role": "user", "content": "ola"},
            {"role": "assistant", "content": "Oi! Como posso ajudar?"},
        ],
    )
    # result -> { answer, sources, rag_hits, model, tokens_used }
"""

from typing import Any, Dict, List, Optional

from src.llm import ChatModel
from src.llm import system_message, user_message, assistant_message

from config import get_settings
from src.services.ai_usage_tracker import track_usage


_DEFAULT_SYSTEM_PROMPT = """Voce e o assistente de IA da Zellu, um guia que orienta usuarios sobre a plataforma.

Funcao:
- Responder perguntas sobre como usar a plataforma, fluxos, recursos e politicas.
- Usar SEMPRE o CONTEXTO RECUPERADO como fonte de verdade. Se a resposta nao
  estiver no contexto, admita honestamente e sugira contatar o suporte humano
  ou um advogado parceiro.
- Manter respostas curtas e diretas (1-3 paragrafos), em portugues brasileiro.
- Nao inventar informacoes nem citar artigos/leis fora do contexto.
- Nao oferecer parecer juridico personalizado - esse e o papel dos advogados
  parceiros; voce orienta sobre o uso da plataforma e onde buscar essa ajuda.

Formato da resposta:
- Texto corrido, claro, sem markdown pesado.
- Se a resposta veio do contexto, mencione brevemente a fonte ao final,
  entre colchetes: [fonte: <nome>]. Se nao houver contexto util, omita.
"""


class GuideAgent:
    """Assistente standalone que orienta usuarios via RAG.

    NAO ESTA LIGADO AO native runtime. Use diretamente via metodo `ask`.
    """

    def __init__(
        self,
        llm: Optional[ChatModel] = None,
        rag_service: Optional[Any] = None,
        model: Optional[str] = None,
        temperature: float = 0.3,
        top_k: int = 4,
        max_tokens: int = 1000,
        system_prompt: Optional[str] = None,
    ):
        """Inicializa o agente.

        Args:
            llm: ChatModel ja configurado. Se None, cria com base em config.
            rag_service: instancia com metodo async search(query, top_k) ->
                [{ content, metadata, score }]. Se None, opera sem RAG.
            model: model id (default settings.COMPANY_RESPONSE_AI_MODEL).
            temperature: 0.3 - factual mas natural.
            top_k: numero de chunks recuperados por consulta.
            max_tokens: teto de tokens de saida.
            system_prompt: override do prompt padrao da persona.
        """
        self.name = "guide"
        self.rag = rag_service
        self.top_k = top_k
        self.system_prompt = system_prompt or _DEFAULT_SYSTEM_PROMPT
        self._max_tokens = max_tokens

        if llm is not None:
            self.llm = llm
        else:
            settings = get_settings()
            self.llm = ChatModel(
                model=model or settings.COMPANY_RESPONSE_AI_MODEL,
                api_key=settings.OPENAI_API_KEY,
                temperature=temperature,
                max_tokens=max_tokens,
            )

    # ------------------------------------------------------------------
    # API publica
    # ------------------------------------------------------------------

    async def ask(
        self,
        question: str,
        history: Optional[List[Dict[str, str]]] = None,
        top_k: Optional[int] = None,
        audience: Optional[str] = None,
        profile: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Responde uma pergunta orientada por RAG.

        Args:
            question: pergunta do usuario.
            history: turnos anteriores no formato
                [{"role": "user" | "assistant", "content": "..."}].
                System messages no historico sao ignoradas (a persona vem
                exclusivamente de self.system_prompt).
            top_k: override do numero de chunks RAG nesta consulta.
            audience: persona-alvo do RAG (visitor|client|lawyer|company).
                Quando definido, restringe a recuperacao apenas a chunks
                taggeados com essa audience - garante isolamento entre
                personas. None = sem filtro (admin/debug).

        Returns:
            Dict no formato:
                {
                    "answer": str,
                    "sources": List[{ "source": str, "score": float, "snippet": str }],
                    "rag_hits": int,
                    "model": str,
                    "tokens_used": Optional[int],
                }
        """
        question = (question or "").strip()
        if not question:
            return self._empty_response("Por favor, faca uma pergunta.")

        documents = await self._retrieve_context(question, top_k=top_k, audience=audience)
        context_block = self._format_rag_context(documents)

        messages: List[Any] = [system_message(content=self.system_prompt)]
        messages.extend(self._build_history_messages(history))

        user_content = question
        if context_block:
            user_content = f"{context_block}\n\nPergunta do usuario: {question}"
        messages.append(user_message(content=user_content))

        answer, tokens_used = await self._invoke_llm(messages, profile=profile)
        sources = self._build_source_summary(documents)

        model_name = self._resolve_model_name()
        self._log_decision(
            "ask",
            {
                "rag_hits": len(documents),
                "answer_len": len(answer),
                "tokens_used": tokens_used,
            },
        )

        return {
            "answer": answer,
            "sources": sources,
            "rag_hits": len(documents),
            "model": model_name,
            "tokens_used": tokens_used,
        }

    # ------------------------------------------------------------------
    # Helpers internos
    # ------------------------------------------------------------------

    async def _retrieve_context(
        self,
        query: str,
        top_k: Optional[int] = None,
        audience: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Recupera chunks via RAG. [] se RAG indisponivel ou falhar.

        Passa audience pra rag.search quando definido. Tolerante a RAGs
        antigos sem o parametro (TypeError -> retry sem audience).
        """
        if not self.rag:
            return []
        effective_top_k = top_k or self.top_k
        try:
            try:
                results = await self.rag.search(
                    query, top_k=effective_top_k, audience=audience
                )
            except TypeError:
                # RAG nao suporta audience - retrocompat (ex.: RAGService antigo)
                results = await self.rag.search(query, top_k=effective_top_k)
            return [r for r in (results or []) if isinstance(r, dict)]
        except Exception as exc:
            print(f"[GUIDE] RAG search falhou (segue sem contexto): {exc}")
            return []

    @staticmethod
    def _format_rag_context(documents: List[Dict[str, Any]]) -> str:
        """Bloco de texto com os chunks para injecao no prompt do usuario."""
        if not documents:
            return ""
        blocks: List[str] = []
        for i, doc in enumerate(documents, start=1):
            metadata = doc.get("metadata") or {}
            source = metadata.get("source") or metadata.get("title") or f"doc_{i}"
            content = (doc.get("content") or "").strip()
            if not content:
                continue
            try:
                score = float(doc.get("score") or 0.0)
            except (TypeError, ValueError):
                score = 0.0
            blocks.append(
                f"[Fonte {i}] {source} (relevancia: {score:.2f})\n{content}"
            )
        if not blocks:
            return ""
        joined = "\n\n---\n\n".join(blocks)
        return (
            "CONTEXTO RECUPERADO (use como referencia para responder):\n\n"
            f"{joined}"
        )

    @staticmethod
    def _build_history_messages(
        history: Optional[List[Dict[str, str]]],
    ) -> List[Any]:
        """Converte historico em mensagens native runtime. Ignora system items."""
        if not history:
            return []
        out: List[Any] = []
        for item in history:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").strip().lower()
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            if role in ("user", "human"):
                out.append(user_message(content=content))
            elif role in ("assistant", "ai", "bot"):
                out.append(assistant_message(content=content))
            # system intencionalmente ignorado - persona unica vem de self.system_prompt
        return out

    async def _invoke_llm(self, messages: List[Any], profile: Optional[str] = None) -> tuple:
        """Invoca o LLM e devolve (answer, tokens_used)."""
        try:
            import time as _time
            _t0 = _time.perf_counter()
            response = await self.llm.complete(messages)
            track_usage(
                module="kb_chat",
                model=self._resolve_model_name(),
                response=response,
                event_type="guide_chat",
                channel="agente_assistente",  # canal proprio (kb_chat e' compartilhado)
                profile=profile,
                duration_ms=int((_time.perf_counter() - _t0) * 1000),
            )
            answer = str(response.content or "").strip()
            tokens_used = self._extract_tokens(response)
            if not answer:
                answer = (
                    "Desculpe, nao consegui formular uma resposta agora. "
                    "Tente reformular ou contate o suporte."
                )
            return answer, tokens_used
        except Exception as exc:
            print(f"[GUIDE] LLM falhou: {exc}")
            return (
                "Desculpe, nao consegui processar sua pergunta agora. "
                "Por favor, tente novamente em instantes ou contate o suporte.",
                None,
            )

    @staticmethod
    def _extract_tokens(response: Any) -> Optional[int]:
        """Tenta extrair total_tokens do response do native runtime de forma defensiva."""
        meta = getattr(response, "response_metadata", None)
        if isinstance(meta, dict):
            usage = meta.get("token_usage") or meta.get("usage")
            if isinstance(usage, dict):
                tot = usage.get("total_tokens") or usage.get("totalTokens")
                if isinstance(tot, int):
                    return tot
        usage_metadata = getattr(response, "usage_metadata", None)
        if isinstance(usage_metadata, dict):
            tot = usage_metadata.get("total_tokens")
            if isinstance(tot, int):
                return tot
        return None

    def _resolve_model_name(self) -> str:
        """Retorna o id do modelo (nem todos os clientes expoem o mesmo atributo)."""
        for attr in ("model_name", "model"):
            value = getattr(self.llm, attr, None)
            if isinstance(value, str) and value:
                return value
        return ""

    @staticmethod
    def _build_source_summary(
        documents: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Resumo das fontes para a UI exibir."""
        out: List[Dict[str, Any]] = []
        for doc in documents:
            metadata = doc.get("metadata") or {}
            content = str(doc.get("content") or "").strip()
            snippet = content[:200] + ("..." if len(content) > 200 else "")
            try:
                score = float(doc.get("score") or 0.0)
            except (TypeError, ValueError):
                score = 0.0
            out.append(
                {
                    "source": metadata.get("source")
                    or metadata.get("title")
                    or "",
                    "score": score,
                    "snippet": snippet,
                }
            )
        return out

    def _empty_response(self, message: str) -> Dict[str, Any]:
        """Resposta-base usada quando a entrada e invalida."""
        return {
            "answer": message,
            "sources": [],
            "rag_hits": 0,
            "model": self._resolve_model_name(),
            "tokens_used": 0,
        }

    def _log_decision(self, event: str, data: Dict[str, Any]) -> None:
        """Mesmo padrao de log dos demais agentes do projeto."""
        print(f"[GUIDE] {event}: {data}")
