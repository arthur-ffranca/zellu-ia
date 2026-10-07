# -*- coding: utf-8 -*-
"""
Cliente HTTP para comunicacao com a API da Zellu.

Responsavel por:
1. Buscar instructions da empresa
2. Buscar documentos da empresa
3. Salvar system prompt gerado
"""

import asyncio
import httpx
from typing import Optional, Dict, Any
from datetime import datetime


class ZelluAPIClient:
    """Cliente para comunicacao com a API da Zellu."""

    def __init__(self, base_url: str, api_key: str):
        """
        Inicializa o cliente.

        Args:
            base_url: URL base da API Zellu (ex: https://zellu.com)
            api_key: API key para autenticacao
        """
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.headers = {
            "Content-Type": "application/json",
            "X-API-Key": api_key
        }

    async def _post_with_retry(
        self,
        url: str,
        payload: Dict[str, Any],
        timeout: float = 30.0,
        max_attempts: int = 3,
        retry_on_5xx: bool = True,
    ) -> "httpx.Response":
        """POST com retry e backoff exponencial para falhas transitorias.

        Conforme contrato 20260626 (§3.2): os callbacks do Sender precisam ser
        confiaveis (retry no nosso lado). Faz retry em erros de transporte/timeout
        e, opcionalmente, em respostas 5xx. Retorna a Response da ultima tentativa
        ou levanta a excecao apos esgotar as tentativas (o chamador trata o status).

        Args:
            url: URL do POST.
            payload: corpo JSON.
            timeout: timeout por tentativa (s).
            max_attempts: numero maximo de tentativas (inclui a primeira).
            retry_on_5xx: se True, faz retry quando status >= 500.
        """
        backoff = 1.0
        last_exc: Optional[Exception] = None
        for attempt in range(1, max_attempts + 1):
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.post(url, headers=self.headers, json=payload)
                if retry_on_5xx and resp.status_code >= 500 and attempt < max_attempts:
                    print(f"[ZELLU-API] {url} -> HTTP {resp.status_code}; retry {attempt}/{max_attempts - 1} em {backoff:.0f}s")
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    continue
                return resp
            except httpx.RequestError as e:
                last_exc = e
                if attempt < max_attempts:
                    print(f"[ZELLU-API] {url} falhou ({type(e).__name__}: {e}); retry {attempt}/{max_attempts - 1} em {backoff:.0f}s")
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    continue
                raise
        # Inalcancavel em teoria; salvaguarda
        if last_exc:
            raise last_exc
        raise RuntimeError("post_with_retry sem resposta")

    async def get_company_instructions(self, company_id: str) -> Optional[Dict[str, Any]]:
        """
        Busca as instructions (persona, rules, legal, budget, tone) de uma empresa.

        Endpoint: GET /api/ai/instructions/{companyId}
        Conforme documentacao company-response/api-endpoints.md

        Args:
            company_id: UUID da empresa

        Returns:
            Dict com instructions ou None se erro
            {
                "success": true,
                "data": {
                    "companyId": "abc123",
                    "instructions": {
                        "persona": "...",
                        "rules": "...",
                        "legal": "...",
                        "budget": "...",
                        "tone": "..."
                    },
                    "lastUpdated": "2024-11-15T10:30:00.000Z"
                }
            }
        """
        url = f"{self.base_url}/api/ai/instructions/{company_id}"

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(url, headers=self.headers)

                if response.status_code == 200:
                    data = response.json()
                    print(f"[ZELLU-API] Instructions obtidas para empresa {company_id}")
                    # Retorna o objeto data.instructions ou data diretamente
                    if data.get("success") and data.get("data"):
                        return data.get("data")
                    return data
                elif response.status_code == 404:
                    print(f"[ZELLU-API] Instructions nao encontradas para empresa {company_id}")
                    return None
                elif response.status_code == 401:
                    print(f"[ZELLU-API] Erro 401: API key invalida ao buscar instructions")
                    return None
                else:
                    print(f"[ZELLU-API] Erro ao buscar instructions: {response.status_code} - {response.text}")
                    return None

        except Exception as e:
            print(f"[ZELLU-API] Excecao ao buscar instructions: {e}")
            return None

    async def get_temporary_rules(self, company_id: str) -> Optional[Dict[str, Any]]:
        """
        Busca as regras temporarias ativas de uma empresa.

        Endpoint: GET /api/ai/temporary-rules/{companyId}
        Conforme documentacao company-response/api-endpoints.md

        Retorna apenas regras que:
        - validationStatus = 'approved'
        - isPaused = false
        - validFrom <= NOW()
        - validUntil >= NOW()

        Args:
            company_id: UUID da empresa

        Returns:
            Dict com regras ou None se erro
            {
                "success": true,
                "data": {
                    "companyId": "abc123",
                    "rules": [
                        {
                            "id": "rule001",
                            "title": "Black Friday",
                            "content": "...",
                            "description": "...",
                            "validFrom": "...",
                            "validUntil": "..."
                        }
                    ],
                    "totalActive": 1
                }
            }
        """
        url = f"{self.base_url}/api/ai/temporary-rules/{company_id}"

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(url, headers=self.headers)

                if response.status_code == 200:
                    data = response.json()
                    print(f"[ZELLU-API] Regras temporarias obtidas para empresa {company_id}")
                    # Retorna o objeto data ou data.data
                    if data.get("success") and data.get("data"):
                        return data.get("data")
                    return data
                elif response.status_code == 404:
                    # A empresa realmente nao tem regras. Nao e falha.
                    print(f"[ZELLU-API] Nenhuma regra temporaria encontrada para empresa {company_id}")
                    return None

                # Falhas devolvem um marcador de erro em vez de None. Sem isso,
                # "a empresa nao tem regras" e "nao consegui buscar as regras"
                # chegavam ao chamador do mesmo jeito - e a IA negociava sem as
                # regras da empresa sem ninguem perceber (auditoria QA-07).
                elif response.status_code == 401:
                    print(f"[ZELLU-API] Erro 401: API key invalida ao buscar regras temporarias")
                    return {"error": "unauthorized", "status": 401, "rules": []}
                else:
                    print(f"[ZELLU-API] Erro ao buscar regras temporarias: {response.status_code} - {response.text}")
                    return {"error": "http_error", "status": response.status_code, "rules": []}

        except Exception as e:
            print(f"[ZELLU-API] Excecao ao buscar regras temporarias: {e}")
            return {"error": "exception", "detail": str(e), "rules": []}

    # get_company_documents saiu em 17/09.
    #
    # Ele chamava GET /api/companies/{id}/documents, uma rota que nasceu de um
    # TODO ("Marcelo vai criar esse endpoint") e NUNCA foi criada: respondia 404,
    # o except devolvia None e ninguem percebia. A plataforma pediu em 15/09 para
    # nao cria-la - a lista de documentos da base vem do Pinecone, alimentado
    # pelo /company_notify_knowledgebase_to_ia.

    async def save_system_prompt(
        self,
        company_id: str,
        prompt: str,
        extra_info: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Salva o system prompt gerado no banco da Zellu.

        Endpoint documentado pelo Marcelo: POST /api/ai/system-prompt

        Args:
            company_id: UUID da empresa
            prompt: System prompt completo gerado
            extra_info: Metadados opcionais (versao, modelo, etc)

        Returns:
            Dict com resultado da operacao
        """
        url = f"{self.base_url}/api/ai/system-prompt"

        payload = {
            "companyId": company_id,
            "prompt": prompt,
        }

        if extra_info:
            payload["extraInfo"] = extra_info

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(url, headers=self.headers, json=payload)

                if response.status_code in [200, 201]:
                    data = response.json()
                    print(f"[ZELLU-API] System prompt salvo para empresa {company_id}")
                    print(f"[ZELLU-API] previousPromptDeactivated: {data.get('previousPromptDeactivated', False)}")
                    return {
                        "success": True,
                        "data": data.get("data"),
                        "previousPromptDeactivated": data.get("previousPromptDeactivated", False)
                    }
                else:
                    error_msg = response.text
                    print(f"[ZELLU-API] Erro ao salvar prompt: {response.status_code} - {error_msg}")
                    return {
                        "success": False,
                        "error": f"HTTP {response.status_code}: {error_msg}"
                    }

        except Exception as e:
            print(f"[ZELLU-API] Excecao ao salvar prompt: {e}")
            return {
                "success": False,
                "error": str(e)
            }

    async def get_system_prompt(self, company_id: str) -> Optional[Dict[str, Any]]:
        """
        Busca o system prompt ativo de uma empresa no Supabase.

        Endpoint: GET /api/ai/system-prompt/{companyId}
        (Marcelo precisa criar este endpoint)

        Args:
            company_id: UUID da empresa

        Returns:
            Dict com prompt ou None se nao encontrado/erro
            {
                "id": "uuid",
                "companyId": "uuid",
                "prompt": "texto do prompt...",
                "status": "active",
                "extraInfo": {...},
                "createdAt": "...",
                "updatedAt": "..."
            }
        """
        url = f"{self.base_url}/api/ai/system-prompt/{company_id}"

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(url, headers=self.headers)

                if response.status_code == 200:
                    data = response.json()
                    print(f"[ZELLU-API] System prompt obtido para empresa {company_id}")
                    # Retorna o prompt ativo
                    return data.get("data") or data
                elif response.status_code == 404:
                    print(f"[ZELLU-API] Nenhum prompt encontrado para empresa {company_id}")
                    return None
                else:
                    print(f"[ZELLU-API] Erro ao buscar prompt: {response.status_code} - {response.text}")
                    return None

        except Exception as e:
            print(f"[ZELLU-API] Excecao ao buscar prompt: {e}")
            return None

    # fetch_all_company_data saiu em 17/09, junto do get_company_documents que
    # ele chamava. Os dois unicos caminhos que chegavam aqui eram as rotas
    # /api/webhook/company-update e /api/webhook/generate-prompt, que a
    # plataforma confirmou nunca ter chamado e que sairam na mesma mudanca.
    #
    # get_company_instructions e get_temporary_rules continuam vivos: quem os usa
    # agora e o build_payload_from_session (:580) e o proprio src/main.py.

    # =========================================================================
    # NOVOS ENDPOINTS (10/12/2025) - Download e Status de Processamento
    # =========================================================================

    async def download_knowledge_file(
        self,
        file_id: str,
        force_download: bool = False
    ) -> Optional[bytes]:
        """
        Baixa um arquivo da base de conhecimento para processamento RAG.

        Endpoint: GET /api/ai/knowledge-files/{fileId}/download

        Args:
            file_id: UUID do arquivo
            force_download: Se True, adiciona ?download=true

        Returns:
            Bytes do arquivo ou None se erro
        """
        url = f"{self.base_url}/api/ai/knowledge-files/{file_id}/download"
        if force_download:
            url += "?download=true"

        try:
            async with httpx.AsyncClient(timeout=120.0) as client:  # Timeout maior para arquivos grandes
                response = await client.get(url, headers={"X-API-Key": self.api_key})

                if response.status_code == 200:
                    content_length = response.headers.get("content-length", "unknown")
                    content_type = response.headers.get("content-type", "unknown")
                    print(f"[ZELLU-API] Arquivo {file_id} baixado: {content_length} bytes, tipo: {content_type}")
                    return response.content
                elif response.status_code == 401:
                    print(f"[ZELLU-API] Erro 401: API key invalida ou ausente")
                    return None
                elif response.status_code == 404:
                    print(f"[ZELLU-API] Erro 404: Arquivo {file_id} nao encontrado")
                    return None
                else:
                    print(f"[ZELLU-API] Erro ao baixar arquivo: {response.status_code} - {response.text}")
                    return None

        except Exception as e:
            print(f"[ZELLU-API] Excecao ao baixar arquivo {file_id}: {e}")
            return None

    async def update_file_processing_status(
        self,
        file_id: str,
        status: str,
        error: Optional[str] = None,
        total_chunks: Optional[int] = None,
        chunks_completed: Optional[int] = None,
        chunks_failed: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Atualiza o status de processamento de um arquivo na Zellu.

        Endpoint: POST /api/ai/knowledge-files/{fileId}/status

        Args:
            file_id: UUID do arquivo
            status: pending | in_progress | completed | failed | cancelled
            error: Mensagem de erro (obrigatorio se status=failed ou cancelled)
            total_chunks: Total de chunks do documento
            chunks_completed: Chunks processados com sucesso
            chunks_failed: Chunks que falharam

        Returns:
            Dict com resultado da operacao
        """
        url = f"{self.base_url}/api/ai/knowledge-files/{file_id}/status"

        payload: Dict[str, Any] = {"status": status}

        if error:
            payload["error"] = error
        if total_chunks is not None:
            payload["totalChunks"] = total_chunks
        if chunks_completed is not None:
            payload["chunksCompleted"] = chunks_completed
        if chunks_failed is not None:
            payload["chunksFailed"] = chunks_failed

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(url, headers=self.headers, json=payload)

                if response.status_code == 200:
                    data = response.json()
                    print(f"[ZELLU-API] Status do arquivo {file_id} atualizado para '{status}'")
                    return {
                        "success": True,
                        "file": data.get("file")
                    }
                elif response.status_code == 401:
                    print(f"[ZELLU-API] Erro 401: API key invalida")
                    return {"success": False, "error": "API key invalida"}
                elif response.status_code == 404:
                    print(f"[ZELLU-API] Erro 404: Arquivo {file_id} nao encontrado")
                    return {"success": False, "error": "Arquivo nao encontrado"}
                else:
                    error_msg = response.text
                    print(f"[ZELLU-API] Erro ao atualizar status: {response.status_code} - {error_msg}")
                    return {"success": False, "error": f"HTTP {response.status_code}: {error_msg}"}

        except Exception as e:
            print(f"[ZELLU-API] Excecao ao atualizar status: {e}")
            return {"success": False, "error": str(e)}

    async def get_file_processing_status(self, file_id: str) -> Optional[Dict[str, Any]]:
        """
        Consulta o status de processamento de um arquivo.

        Endpoint: GET /api/ai/knowledge-files/{fileId}/status

        Args:
            file_id: UUID do arquivo

        Returns:
            Dict com status do arquivo ou None se erro
        """
        url = f"{self.base_url}/api/ai/knowledge-files/{file_id}/status"

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(url, headers=self.headers)

                if response.status_code == 200:
                    data = response.json()
                    return data.get("file")
                else:
                    print(f"[ZELLU-API] Erro ao consultar status: {response.status_code}")
                    return None

        except Exception as e:
            print(f"[ZELLU-API] Excecao ao consultar status: {e}")
            return None

    # =========================================================================
    # ENDPOINTS PARA AUTOSTART - Buscar contexto da sessão
    # =========================================================================

    async def get_empresa_base_dados(
        self,
        session_id: str
    ) -> Optional[Dict[str, Any]]:
        """
        Busca dados completos para IA da empresa via endpoint consolidado.

        Endpoint: POST /api/company/base-dados
        Conforme documentacao automatic-negotiation/api-base-dados.md

        Args:
            session_id: UUID da sessão

        Returns:
            Dict com context, instructions, messageHistory, knowledgeBases, temporaryRules, config
            ou None se erro
        """
        url = f"{self.base_url}/api/company/base-dados"

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    url,
                    headers=self.headers,
                    json={"sessionId": session_id}
                )

                if response.status_code == 200:
                    data = response.json()
                    print(f"[ZELLU-API] Dados da sessao {session_id} obtidos via /api/company/base-dados")
                    return data
                elif response.status_code == 404:
                    print(f"[ZELLU-API] Sessao {session_id} nao encontrada em /api/company/base-dados")
                    return None
                elif response.status_code == 401:
                    print(f"[ZELLU-API] Erro 401: API key invalida ao chamar /api/company/base-dados")
                    return None
                else:
                    print(f"[ZELLU-API] Erro ao chamar /api/company/base-dados: {response.status_code} - {response.text}")
                    return None

        except Exception as e:
            print(f"[ZELLU-API] Excecao ao chamar /api/company/base-dados: {e}")
            return None

    async def get_session_context(
        self,
        session_id: str,
        ticket_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Busca o contexto completo de uma sessão para iniciar negociação.

        Chamado quando autoStart=True chega sem contexto.
        Endpoint: GET /api/ai/sessions/{sessionId}/context

        Args:
            session_id: UUID da sessão
            ticket_id: UUID do ticket (opcional, para fallback)

        Returns:
            Dict com context, instructions, knowledgeBases, rules ou None se erro
        """
        url = f"{self.base_url}/api/ai/sessions/{session_id}/context"

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(url, headers=self.headers)

                if response.status_code == 200:
                    data = response.json()
                    print(f"[ZELLU-API] Contexto da sessao {session_id} obtido com sucesso")
                    return data.get("data") or data
                elif response.status_code == 404:
                    print(f"[ZELLU-API] Sessao {session_id} nao encontrada")
                    if ticket_id:
                        return await self._get_context_by_ticket(ticket_id)
                    return None
                elif response.status_code == 401:
                    print(f"[ZELLU-API] Erro 401: API key invalida ao buscar contexto")
                    return None
                else:
                    print(f"[ZELLU-API] Erro ao buscar contexto: {response.status_code} - {response.text}")
                    return None

        except Exception as e:
            print(f"[ZELLU-API] Excecao ao buscar contexto da sessao: {e}")
            return None

    async def _get_context_by_ticket(self, ticket_id: str) -> Optional[Dict[str, Any]]:
        """
        Busca contexto pelo ticketId como fallback.

        Endpoint: GET /api/ai/tickets/{ticketId}/context
        """
        url = f"{self.base_url}/api/ai/tickets/{ticket_id}/context"

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(url, headers=self.headers)

                if response.status_code == 200:
                    data = response.json()
                    print(f"[ZELLU-API] Contexto do ticket {ticket_id} obtido com sucesso")
                    return data.get("data") or data
                else:
                    print(f"[ZELLU-API] Erro ao buscar contexto por ticket: {response.status_code}")
                    return None

        except Exception as e:
            print(f"[ZELLU-API] Excecao ao buscar contexto do ticket: {e}")
            return None

    async def build_payload_from_session(
        self,
        session_id: str,
        ticket_id: Optional[str] = None,
        company_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Constrói payload completo para IA a partir do sessionId.

        Usa o endpoint consolidado POST /api/company/base-dados conforme documentacao.
        Fallback para endpoints individuais se o consolidado falhar.

        Args:
            session_id: UUID da sessão
            ticket_id: UUID do ticket (opcional)
            company_id: UUID da empresa (opcional)

        Returns:
            Payload completo ou None se erro
        """
        print(f"[ZELLU-API] Construindo payload para sessao {session_id}")

        # Tentar endpoint consolidado primeiro (conforme documentacao)
        session_context = await self.get_empresa_base_dados(session_id)

        if session_context:
            print(f"[ZELLU-API] Payload obtido via endpoint consolidado /api/company/base-dados")
            return session_context

        # Fallback: endpoints individuais
        print(f"[ZELLU-API] Fallback para endpoints individuais...")
        session_context = await self.get_session_context(session_id, ticket_id)

        if not session_context:
            print(f"[ZELLU-API] Nao foi possivel obter contexto da sessao")
            return None

        if not company_id:
            company_id = session_context.get("context", {}).get("company", {}).get("id")

        if company_id:
            if not session_context.get("instructions"):
                instructions = await self.get_company_instructions(company_id)
                if instructions:
                    session_context["instructions"] = instructions.get("instructions", instructions)

            if not session_context.get("rules") and not session_context.get("temporaryRules"):
                rules = await self.get_temporary_rules(company_id)
                if rules and not rules.get("error"):
                    session_context["temporaryRules"] = rules.get("rules", [])

        return session_context

    # =========================================================================
    # WEBHOOKS DO SENDER - Conforme documentação automatic-negotiation
    # =========================================================================

    async def send_zelinhu_message(
        self,
        session_id: str,
        message_body: str,
        round: int,
        negotiation_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Envia mensagem do ZelinhU para o Zellu Backend.

        Conforme documentação webhooks-sender.md:
        POST /api/webhooks/sender/message

        O Zellu Backend irá:
        1. Salvar a mensagem na sessão
        2. Montar payload completo
        3. Encaminhar para IA da empresa
        4. Retornar resposta da IA para o Sender

        Args:
            session_id: ID da sessão no Zellu
            message_body: Conteúdo da mensagem do ZelinhU
            round: Rodada atual da negociação
            negotiation_id: ID da negociação no Sender (opcional na primeira msg)

        Returns:
            Dict com resposta da IA da empresa:
            {
                "success": true,
                "zelinhuMessageId": "msg_001",
                "companyMessageId": "msg_002",
                "response": {
                    "messageBody": "...",
                    "negotiationComplete": false
                },
                "round": 2
            }
        """
        url = f"{self.base_url}/api/webhooks/sender/message"

        payload = {
            "sessionId": session_id,
            "messageBody": message_body,
            "round": round
        }

        if negotiation_id:
            payload["negotiationId"] = negotiation_id

        try:
            print(f"[SENDER-WEBHOOK] Enviando mensagem do ZelinhU - sessionId={session_id}, round={round}")
            # Retry/backoff em falhas de transporte/timeout (contrato 20260626 §3.2).
            # retry_on_5xx=False: 502 = "IA da empresa falhou" tem tratamento proprio
            # abaixo (o negotiation.py decide o retry) - evita reenviar a mensagem.
            # Mesmo motivo do teto de 120s em webhooks_sender (plano de
            # 25/08/2026, §2.6): timeout curto aqui mata o rollback da flag, e
            # nao evita o corte do Cloudflare, que vem antes.
            response = await self._post_with_retry(url, payload, timeout=60.0, retry_on_5xx=False)

            if response.status_code == 202:
                # Ack do turno assincrono: sem corpo util. NAO fazemos json()
                # aqui - corpo de ack pode vir vazio e o parse viraria excecao,
                # transformando um despacho aceito em falha.
                print(f"[SENDER-WEBHOOK] Backend aceitou a mensagem; aguardando callback")
                return {"success": True, "accepted": True}

            if response.status_code == 200:
                data = response.json()
                print(f"[SENDER-WEBHOOK] Mensagem enviada com sucesso!")
                print(f"[SENDER-WEBHOOK] zelinhuMessageId={data.get('zelinhuMessageId')}")
                print(f"[SENDER-WEBHOOK] companyMessageId={data.get('companyMessageId')}")
                print(f"[SENDER-WEBHOOK] negotiationComplete={data.get('response', {}).get('negotiationComplete', False)}")
                return data
            elif response.status_code == 400:
                error_data = response.json()
                print(f"[SENDER-WEBHOOK] Erro 400: {error_data.get('error', 'Bad request')}")
                return {"success": False, "error": error_data.get("error")}
            elif response.status_code == 404:
                print(f"[SENDER-WEBHOOK] Erro 404: Sessão não encontrada")
                return {"success": False, "error": "Session not found"}
            elif response.status_code == 502:
                error_data = response.json()
                print(f"[SENDER-WEBHOOK] Erro 502: Falha na IA da empresa - {error_data.get('error')}")
                return {
                    "success": False,
                    "error": error_data.get("error"),
                    "code": error_data.get("code"),
                    "retryable": error_data.get("retryable", False),
                    "zelinhuMessageId": error_data.get("zelinhuMessageId")
                }
            else:
                print(f"[SENDER-WEBHOOK] Erro {response.status_code}: {response.text}")
                return {"success": False, "error": f"HTTP {response.status_code}"}

        except Exception as e:
            print(f"[SENDER-WEBHOOK] Exceção ao enviar mensagem: {e}")
            return {"success": False, "error": str(e)}

    async def send_negotiation_status(
        self,
        session_id: str,
        status: str,
        negotiation_id: Optional[str] = None,
        final_round: Optional[int] = None,
        reason: Optional[str] = None,
        agreement_details: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
        final_message: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Notifica o Zellu Backend sobre mudança de status da negociação.

        Conforme documentação webhooks-sender.md:
        POST /api/webhooks/sender/status

        Chamado quando:
        - Acordo alcançado (agreement)
        - Sem acordo após todas tentativas (no_agreement)
        - Prazo expirado (timeout)
        - Escalado para judicial (escalated)
        - Cancelado pelo cliente (cancelled)

        Args:
            session_id: ID da sessão no Zellu
            status: started | negotiating | agreement | no_agreement | timeout | escalated | cancelled
            negotiation_id: ID da negociação no Sender
            final_round: Rodada em que terminou
            reason: Motivo do status (para no_agreement, escalated, cancelled)
            agreement_details: Detalhes do acordo (para status=agreement)
                {
                    "value": 500.00,
                    "terms": "Desconto aplicado na próxima fatura",
                    "deadline": "2025-01-31"
                }
            error: Mensagem de erro (para timeout, failed)

        Returns:
            Dict com resultado:
            {
                "success": true,
                "sessionId": "sess_xyz789",
                "previousStatus": "negotiating",
                "newStatus": "agreement",
                "isTerminal": true
            }
        """
        url = f"{self.base_url}/api/webhooks/sender/status"

        payload: Dict[str, Any] = {
            "sessionId": session_id,
            "status": status
        }

        if negotiation_id:
            payload["negotiationId"] = negotiation_id
        if final_round is not None:
            payload["finalRound"] = final_round
        if reason:
            payload["reason"] = reason
        if agreement_details:
            payload["agreementDetails"] = agreement_details
        if error:
            payload["error"] = error
        if final_message:
            payload["finalMessage"] = final_message

        try:
            print(f"[SENDER-WEBHOOK] Notificando status - sessionId={session_id}, status={status}")
            # Status callback e a fonte da verdade do progresso para o Zellu
            # (contrato 20260626 §3.2): retry com backoff inclusive em 5xx, pois
            # o backend trata a notificacao de status de forma idempotente.
            response = await self._post_with_retry(url, payload, timeout=30.0, retry_on_5xx=True)

            if response.status_code == 200:
                data = response.json()
                print(f"[SENDER-WEBHOOK] Status notificado com sucesso!")
                print(f"[SENDER-WEBHOOK] previousStatus={data.get('previousStatus')}")
                print(f"[SENDER-WEBHOOK] newStatus={data.get('newStatus')}")
                print(f"[SENDER-WEBHOOK] isTerminal={data.get('isTerminal')}")
                return data
            elif response.status_code == 400:
                error_data = response.json()
                print(f"[SENDER-WEBHOOK] Erro 400: {error_data.get('error', 'Bad request')}")
                return {"success": False, "error": error_data.get("error")}
            elif response.status_code == 404:
                print(f"[SENDER-WEBHOOK] Erro 404: Sessão não encontrada")
                return {"success": False, "error": "Session not found"}
            else:
                print(f"[SENDER-WEBHOOK] Erro {response.status_code}: {response.text}")
                return {"success": False, "error": f"HTTP {response.status_code}"}

        except Exception as e:
            print(f"[SENDER-WEBHOOK] Exceção ao notificar status: {e}")
            return {"success": False, "error": str(e)}
