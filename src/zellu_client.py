# -*- coding: utf-8 -*-
"""Cliente HTTP para integração com backend Zellu."""

import asyncio
import httpx
from typing import Dict, Any, Optional, List
import logging
import json

from src.services.webhook_signature import build_signed_headers, redact_headers
from src.utils.hand_type import normalize_hand_type

logger = logging.getLogger(__name__)


class ZelluClient:
    """Cliente HTTP para enviar respostas da IA para o backend Zellu."""

    def __init__(self, settings):
        """Inicializa o cliente Zellu.

        Args:
            settings: Configurações da aplicação
        """
        self.webhook_url = settings.ZELLU_WEBHOOK_URL
        self.api_key = settings.API_KEY_ZELLU_IA
        self.hmac_secret = settings.AI_WEBHOOK_HMAC_SECRET

        # Entrega garantida do callback (contrato 20260805). getattr com default
        # para nao quebrar com objetos de settings antigos/mockados nos testes.
        self.timeout = float(getattr(settings, "CHAT_CALLBACK_TIMEOUT_S", 30.0))
        self.max_attempts = int(getattr(settings, "CHAT_CALLBACK_MAX_ATTEMPTS", 3))
        self.retry_backoff = float(getattr(settings, "CHAT_CALLBACK_BACKOFF_S", 1.0))

        logger.info(
            f"[ZELLU] Cliente inicializado - URL: {self.webhook_url} "
            f"(timeout={self.timeout}s, tentativas={self.max_attempts})"
        )

    async def _post_with_retry(
        self,
        url: str,
        content: str,
        headers: Dict[str, str],
    ) -> httpx.Response:
        """POST com retry e backoff exponencial.

        Contrato docs/20260805-contrato-entrega-garantida-callback-webhook.md
        (ponto 2): se /api/chat/webhook responder 5xx, a resposta da IA ainda NAO
        foi persistida do lado do app - sem retry aqui, ela se perde.

        Retenta em erro de transporte/timeout e em 5xx.
        NAO retenta em 4xx (erro permanente: payload ou credencial invalida) nem
        em 200 com erro no corpo (decisao do app, ja processada por ele).

        Mesma semantica de ZelluAPIClient._post_with_retry (src/clients/api_client.py:35),
        reimplementada aqui para nao alterar aquele caminho, que ja esta em producao.

        Args:
            url: destino do POST
            content: corpo cru (string ja serializada - necessario para a
                assinatura HMAC bater byte a byte)
            headers: cabecalhos, incluindo x-api-key e assinatura

        Returns:
            httpx.Response da ultima tentativa

        Raises:
            httpx.RequestError: se todas as tentativas falharem no transporte
        """
        backoff = self.retry_backoff
        last_exc: Optional[Exception] = None

        for attempt in range(1, self.max_attempts + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.post(url, content=content, headers=headers)

                if response.status_code >= 500 and attempt < self.max_attempts:
                    logger.warning(
                        f"[ZELLU] {url} -> HTTP {response.status_code} (resposta NAO persistida); "
                        f"retry {attempt}/{self.max_attempts - 1} em {backoff:.0f}s"
                    )
                    print(
                        f"[ZELLU] HTTP {response.status_code}; "
                        f"retry {attempt}/{self.max_attempts - 1} em {backoff:.0f}s"
                    )
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    continue

                return response

            except httpx.RequestError as e:
                # TimeoutException herda de RequestError - timeout tambem retenta
                last_exc = e
                if attempt < self.max_attempts:
                    logger.warning(
                        f"[ZELLU] {url} falhou ({type(e).__name__}: {e}); "
                        f"retry {attempt}/{self.max_attempts - 1} em {backoff:.0f}s"
                    )
                    print(
                        f"[ZELLU] Falha de transporte ({type(e).__name__}); "
                        f"retry {attempt}/{self.max_attempts - 1} em {backoff:.0f}s"
                    )
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    continue
                raise

        # Salvaguarda: inalcancavel enquanto max_attempts >= 1
        if last_exc:
            raise last_exc
        raise RuntimeError("_post_with_retry sem resposta")

    async def send_message(
        self,
        chat_id: str,
        message: str,
        is_finished: bool = False,
        analysis_data: Optional[Dict[str, Any]] = None,
        message_type: str = "text",
        group_messages: Optional[List[Dict[str, Any]]] = None,
        messagedata: Optional[Dict[str, Any]] = None,
        dispatch_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Envia mensagem de volta para o backend Zellu.

        Args:
            chat_id: ID da sessão de chat
            message: Mensagem de texto da IA (ou mensagem consolidada se usando group_messages)
            is_finished: Se a análise foi concluída
            analysis_data: Dados da análise completa (obrigatório se is_finished=True)
            message_type: Tipo da mensagem (default: "text")
            group_messages: Array de mensagens sequenciais com delay/awaiting_continuation (opcional)
            dispatch_id: Eco do despacho recebido do app (contrato 20260807).
                Copiado para a RAIZ do callback. Quando ausente, o app cai no
                comportamento antigo (correlacao por sessao) e so loga.

        Returns:
            Resposta do webhook Zellu

        Raises:
            httpx.HTTPError: Se houver erro na requisição HTTP
        """
        from src.services.chat_completion import prepare_completion
        message, is_finished, group_messages = prepare_completion(
            message, is_finished, analysis_data, group_messages
        )
        # Monta payload
        payload = {
            "chat_id": chat_id,
            "message": message,
            "is_finished": is_finished,
            "message_type": message_type
        }

        # Eco do dispatch_id na RAIZ do callback, ao lado do chat_id
        # (contrato docs/20260807-contrato-fila-despacho-eco-dispatch-id.md, item 1).
        # - Ecoamos o campo recebido; NUNCA derivamos do `id` da mensagem.
        # - NUNCA vai dentro de messagedata.
        # - Todas as bolhas/continuacoes deste turno saem neste mesmo POST
        #   (group_messages), entao carregam o mesmo dispatch_id.
        # - Entra no corpo antes da serializacao assinada: HMAC inalterado.
        if dispatch_id:
            payload["dispatch_id"] = dispatch_id
            logger.info(f"[ZELLU] dispatch_id ecoado no callback: {dispatch_id}")
        else:
            logger.warning(
                f"[ZELLU] Callback SEM dispatch_id para chat_id={chat_id} - "
                f"app cai na correlacao por sessao (contrato 20260807)"
            )

        # Garante que o handType saia sempre no slug canonico. Valor que nao
        # esta no enum e removido: o front cai na mao padrao de qualquer jeito,
        # e assim nao poluimos o payload com lixo.
        if messagedata and "handType" in messagedata:
            hand = normalize_hand_type(messagedata.get("handType"))
            messagedata = dict(messagedata)
            if hand:
                messagedata["handType"] = hand
            else:
                logger.warning(f"[ZELLU] handType invalido descartado: {messagedata.get('handType')!r}")
                messagedata.pop("handType", None)

        # group_messages é OBRIGATÓRIO no backend Zellu
        # Se não fornecido, cria automaticamente com a mensagem única
        if group_messages:
            payload["group_messages"] = group_messages
            logger.info(f"[ZELLU] Enviando {len(group_messages)} mensagens em grupo para chat_id={chat_id}")
        else:
            # Cria group_messages padrão com mensagem única
            single_msg = {
                "message": message,
                "delay": 0,
                "awaiting_continuation": False,
                "is_finished": is_finished
            }
            # Incluir messagedata dentro do group_message também
            if messagedata:
                single_msg["messagedata"] = messagedata
            payload["group_messages"] = [single_msg]
            logger.info(f"[ZELLU] Enviando mensagem única (auto group_messages) para chat_id={chat_id}")

        # Adiciona analysis_data se a análise estiver completa
        if is_finished and analysis_data:
            payload["analysis_data"] = analysis_data
            logger.info(f"[ZELLU] Enviando análise completa para chat_id={chat_id}")
        else:
            logger.info(f"[ZELLU] Enviando mensagem intermediária para chat_id={chat_id}")

        # Adiciona messagedata se fornecido (ex: userExists para modal de login)
        if messagedata:
            payload["messagedata"] = messagedata
            logger.info(f"[ZELLU] messagedata incluído: {messagedata}")

        # Headers obrigatórios (x-api-key minúsculo conforme AI_SERVICE_WEBHOOK_FORMAT.md)
        payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key
        }
        if self.hmac_secret:
            headers.update(build_signed_headers(payload_json, self.hmac_secret))

        try:
            # Log detalhado do payload para debug
            import os
            from datetime import datetime
            url = f"{self.webhook_url}/api/chat/webhook"
            print(f"[ZELLU] URL: {url}")
            print(f"[ZELLU] Headers: {redact_headers(headers)}")
            # ensure_ascii=False para manter acentos corretos
            payload_json_pretty = json.dumps(payload, indent=2, ensure_ascii=False)
            print(f"[ZELLU] Payload (truncado): {payload_json_pretty[:900]}")

            # Salvar payload completo em arquivo para debug
            if is_finished:
                debug_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "debug_payloads")
                os.makedirs(debug_dir, exist_ok=True)
                debug_file = os.path.join(debug_dir, f"{chat_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_payload.json")
                with open(debug_file, 'w', encoding='utf-8') as f:
                    f.write(payload_json_pretty)
                print(f"[ZELLU] Payload completo salvo em: {debug_file}")

            # Envia requisicao para Zellu, com retry em 5xx/timeout (contrato 20260805)
            response = await self._post_with_retry(url, payload_json, headers)

            # Log da resposta. Corpo pode nao ser JSON (ex: 502 HTML de proxy),
            # entao o parse nao pode derrubar o tratamento de status abaixo.
            try:
                response_data = response.json()
            except Exception:
                response_data = {"raw": response.text[:500]}

            print(f"[ZELLU] Response Status: {response.status_code}")
            print(f"[ZELLU] Response Body: {response_data}")

            if response.status_code == 200:
                # Verificar se o backend Zellu retornou sucesso no corpo
                if response_data.get('success') == False or response_data.get('error'):
                    error_msg = response_data.get('error', 'Erro desconhecido')
                    print(f"[ZELLU] ERRO no corpo da resposta: {error_msg}")
                    raise Exception(f"Backend Zellu retornou erro: {error_msg}")

                logger.info(f"[ZELLU] OK Mensagem enviada com sucesso - chat_id={chat_id}")
                return response_data
            else:
                logger.error(
                    f"[ZELLU] ERRO ao enviar mensagem apos {self.max_attempts} tentativa(s) - "
                    f"Status: {response.status_code}, "
                    f"Response: {response.text}"
                )
                response.raise_for_status()

        except httpx.TimeoutException as e:
            logger.error(f"[ZELLU] TIMEOUT ao enviar para Zellu: {e}")
            raise
        except httpx.HTTPError as e:
            logger.error(f"[ZELLU] ERRO HTTP ao enviar para Zellu: {e}")
            raise
        except Exception as e:
            logger.error(f"[ZELLU] ERRO inesperado ao enviar para Zellu: {e}")
            raise

    def build_group_messages(
        self,
        messages: List[str],
        delays: Optional[List[int]] = None,
        analysis_data: Optional[Dict[str, Any]] = None,
        hand_types: Optional[List[Optional[str]]] = None
    ) -> List[Dict[str, Any]]:
        """Constrói array de group_messages no formato esperado pelo Zellu.

        Conforme especificação AI_SERVICE_WEBHOOK_FORMAT.md:
        - Cada mensagem pode ter delay (ms antes de exibir)
        - awaiting_continuation=true mantém "Digitando..." após mensagem
        - Última mensagem tem awaiting_continuation=false e pode ter analysis_data

        Args:
            messages: Lista de mensagens de texto
            delays: Lista de delays em ms (opcional, default 0 para todas)
            analysis_data: Dados da análise (incluído apenas na última mensagem se fornecido)
            hand_types: Maozinha animada de cada bolha (opcional, mesma ordem de
                `messages`). Posicao com None nao recebe o campo - o front usa a
                mao padrao. Ver docs/20260728-contrato-handtype-avatar-animado.md

        Returns:
            Lista de objetos formatados para group_messages

        Example:
            >>> messages = [
            ...     "Analisando seu caso...",
            ...     "Identifiquei alguns direitos...",
            ...     "Vou apresentar as soluções!"
            ... ]
            >>> delays = [0, 1500, 2000]
            >>> group = client.build_group_messages(messages, delays, analysis_data)
        """
        if not messages:
            return []

        # Se delays não foi fornecido, usa 0 para todas as mensagens
        if delays is None:
            delays = [0] * len(messages)
        elif len(delays) < len(messages):
            # Completa com zeros se necessário
            delays = delays + [0] * (len(messages) - len(delays))

        group_messages = []

        for idx, message in enumerate(messages):
            is_last = (idx == len(messages) - 1)

            msg_obj = {
                "message": message,
                "delay": delays[idx],
                "awaiting_continuation": not is_last,  # True para todas exceto última
                "is_finished": is_last  # True apenas na última, conforme especificação
            }

            # Incluir analysis_data apenas na última mensagem se fornecido
            # (conforme GUIA_TESTE_FRONTEND.md linha 357-364)
            if is_last and analysis_data:
                msg_obj["analysis_data"] = analysis_data

            # handType vai DENTRO do messagedata da propria bolha - o
            # messagedata por mensagem tem precedencia sobre o do topo.
            if hand_types and idx < len(hand_types):
                hand = normalize_hand_type(hand_types[idx])
                if hand:
                    msg_obj["messagedata"] = {"handType": hand}

            group_messages.append(msg_obj)

        return group_messages

    def build_analysis_data(
        self,
        problem: str,
        rights: List[str],
        estimated_value: float,
        recommendations: List[Dict[str, Any]],
        user_info: Optional[Dict[str, str]] = None,
        opposing_party: Optional[Dict[str, str]] = None,
        case_details: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Constrói o objeto analysis_data no formato esperado pelo Zellu.

        Args:
            problem: Descrição do problema identificado
            rights: Lista de direitos do cliente
            estimated_value: Valor estimado em reais
            recommendations: Lista com 3 recomendações (amigavel, extrajudicial, judicial)
            user_info: Informações do usuário (opcional)
            opposing_party: Informações da parte contrária (opcional)
            case_details: Detalhes do caso (opcional)

        Returns:
            Objeto analysis_data formatado
        """
        analysis = {
            "problem": problem,
            "rights": rights,
            "estimatedValue": estimated_value,
            "recommendations": recommendations
        }

        # Adiciona campos opcionais se fornecidos
        if user_info:
            analysis["userInfo"] = user_info

        if opposing_party:
            analysis["opposingParty"] = opposing_party

        if case_details:
            analysis["caseDetails"] = case_details

        return analysis
