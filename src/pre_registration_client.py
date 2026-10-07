# -*- coding: utf-8 -*-
"""
Cliente para criar pre-registro de usuario no backend Zellu.

Usa o endpoint POST /api/ai/create-pre-registered-user para criar
um usuario pre-registrado durante o fluxo de conversa.

Payload:
{
    "email": "joao@email.com",
    "firstName": "Joao",
    "lastName": "Silva",
    "phone": "(11) 99999-1111",
    "sessionId": "chat-session-id"  // opcional mas recomendado
}

EMENDA 2026-09-04 (user-exists-webhook-spec.md): o payload NAO mudou, mas a
rota ficou mais estrita no `phone`, que virou identidade (unico por conta e
chave de login):

    phone irreconhecivel  -> 400 (antes: 201 com a conta nascendo sem telefone)
    phone de outra conta  -> 409 com phoneInUse: true (antes: 500 generico)

Nos dois casos NAO ha conta criada e quem chama deve pedir o numero de novo na
conversa. Por isso o 409 deixou de ser tratado como sucesso cego - ver o
comentario no bloco do 409 abaixo.
"""

import httpx
from typing import Optional
from dataclasses import dataclass
from config import get_settings
from src.utils.phone import PHONE_FORMAT_HINT, is_valid_phone

settings = get_settings()


@dataclass
class PreRegistrationResult:
    """Resultado da criacao de pre-registro.

    Campos conforme documentacao /api/ai/create-pre-registered-user:
    - success: True se o usuario foi criado com sucesso
    - user_id: ID do usuario criado
    - email: Email do usuario
    - username: Username gerado
    - error: Mensagem de erro (se houver)

    Emenda 2026-09-04 (identidade por telefone):
    - phone_invalid: 400 - numero irreconhecivel, pedir o numero de novo
    - phone_in_use: 409 phoneInUse - numero e de outra conta, pedir OUTRO numero

    Nos dois casos a conta NAO foi criada (success=False).
    """
    success: bool
    user_id: Optional[str] = None
    email: Optional[str] = None
    username: Optional[str] = None
    error: Optional[str] = None
    phone_invalid: bool = False
    phone_in_use: bool = False


class PreRegistrationClient:
    """
    Cliente para criar pre-registro de usuario no backend Zellu.

    Endpoint: POST /api/ai/create-pre-registered-user
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: float = 15.0
    ):
        """
        Inicializa o cliente de pre-registro.

        Args:
            base_url: URL base do backend Zellu (ex: https://api.zellu.com.br)
            api_key: API Key para autenticacao
            timeout: Timeout em segundos para a requisicao
        """
        self.base_url = (base_url or settings.ZELLU_BACKEND_URL).rstrip('/')
        self.api_key = api_key or settings.API_KEY_ZELLU_IA
        self.timeout = timeout
        self.endpoint = "/api/ai/create-pre-registered-user"

        print(f"[PRE-REGISTRATION] Inicializado - URL: {self.base_url}{self.endpoint}")

    @staticmethod
    def _is_phone_error(error_msg: Optional[str]) -> bool:
        """
        Diz se a mensagem de erro do 400 fala do telefone.

        A emenda de 04/09 nao definiu flag para o 400 - so diz que o `error`
        traz o formato esperado. Como o e-mail ja foi validado antes, um 400
        aqui e quase sempre o telefone; mesmo assim conferimos o texto para
        nao pedir o numero de novo quando o problema for outro campo.
        """
        if not error_msg:
            return False
        texto = error_msg.lower()
        return "phone" in texto or "telefone" in texto or "celular" in texto

    async def create(
        self,
        email: str,
        first_name: str,
        last_name: str,
        phone: str,
        session_id: Optional[str] = None
    ) -> PreRegistrationResult:
        """
        Cria um usuario pre-registrado.

        Args:
            email: Email do usuario
            first_name: Primeiro nome
            last_name: Sobrenome
            phone: Telefone com DDD
            session_id: ID da sessao de chat (opcional mas recomendado)

        Returns:
            PreRegistrationResult com o resultado da criacao
        """
        if not email or not first_name or not last_name or not phone:
            return PreRegistrationResult(
                success=False,
                error="Campos obrigatorios faltando (email, firstName, lastName, phone)"
            )

        # Emenda 04/09: telefone irreconhecivel agora e 400. Conferimos aqui
        # antes de gastar a chamada - o resultado para quem chama e o mesmo
        # (phone_invalid=True), so que sem round-trip e sem erro no backend.
        if not is_valid_phone(phone):
            print(f"[PRE-REGISTRATION] Telefone recusado antes do envio: '{phone}'")
            return PreRegistrationResult(
                success=False,
                phone_invalid=True,
                error=f"Telefone em formato invalido. {PHONE_FORMAT_HINT}"
            )

        url = f"{self.base_url}{self.endpoint}"

        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key
        }

        payload = {
            "email": email.strip().lower(),
            "firstName": first_name.strip(),
            "lastName": last_name.strip(),
            "phone": phone.strip()
        }

        # Adicionar sessionId se fornecido
        if session_id:
            payload["sessionId"] = session_id

        print(f"[PRE-REGISTRATION] Criando pre-registro para: {email}")
        print(f"[PRE-REGISTRATION] Payload: {payload}")

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(url, json=payload, headers=headers)

                print(f"[PRE-REGISTRATION] Status: {response.status_code}")

                if response.status_code == 201 or response.status_code == 200:
                    data = response.json()

                    if data.get("success"):
                        user_data = data.get("data", {})
                        user_id = user_data.get("id")
                        username = user_data.get("username")

                        print(f"[PRE-REGISTRATION] Usuario criado: id={user_id}, username={username}")

                        return PreRegistrationResult(
                            success=True,
                            user_id=user_id,
                            email=email,
                            username=username
                        )
                    else:
                        error_msg = data.get("error", "Erro desconhecido")
                        print(f"[PRE-REGISTRATION] Erro: {error_msg}")
                        return PreRegistrationResult(
                            success=False,
                            error=error_msg
                        )

                elif response.status_code == 400:
                    data = response.json()
                    error_msg = data.get("error", "Dados invalidos")
                    # Emenda 04/09: 400 no pre-registro e, na pratica, telefone
                    # irreconhecivel - o e-mail ja passou pelo validate-user-email
                    # antes de chegarmos aqui. O backend nao manda flag, entao
                    # olhamos o texto; sem pista, tratamos como erro comum para
                    # nao pedir telefone a toa.
                    phone_invalid = bool(data.get("phoneInvalid")) or self._is_phone_error(error_msg)
                    print(f"[PRE-REGISTRATION] Erro (400): {error_msg} (phone_invalid={phone_invalid})")
                    return PreRegistrationResult(
                        success=False,
                        phone_invalid=phone_invalid,
                        error=error_msg
                    )

                elif response.status_code == 409:
                    data = response.json()

                    # Emenda 04/09: 409 deixou de significar so "usuario ja
                    # existe". Com phoneInUse=true o numero e de OUTRA conta e
                    # nada foi criado - tratar como sucesso aqui faria o fluxo
                    # seguir achando que tem conta, e o cadastro nunca sairia.
                    if data.get("phoneInUse"):
                        error_msg = data.get("error") or "Este telefone ja esta cadastrado em outra conta"
                        print(f"[PRE-REGISTRATION] Conflito (409): telefone ja pertence a outra conta")
                        return PreRegistrationResult(
                            success=False,
                            phone_in_use=True,
                            error=error_msg
                        )

                    # Conflito de e-mail - usuario ja existe (tratar como sucesso)
                    existing_user_id = data.get("userId") or data.get("user_id") or data.get("id")
                    print(f"[PRE-REGISTRATION] Conflito (409): usuario ja existe (user_id={existing_user_id})")
                    return PreRegistrationResult(
                        success=True,
                        user_id=existing_user_id,
                        error=None
                    )

                elif response.status_code == 401:
                    print(f"[PRE-REGISTRATION] Erro de autenticacao (401)")
                    return PreRegistrationResult(
                        success=False,
                        error="Erro de autenticacao com o backend"
                    )

                else:
                    print(f"[PRE-REGISTRATION] Erro HTTP: {response.status_code}")
                    return PreRegistrationResult(
                        success=False,
                        error=f"Erro HTTP {response.status_code}"
                    )

        except httpx.TimeoutException:
            print(f"[PRE-REGISTRATION] Timeout ao criar pre-registro")
            return PreRegistrationResult(
                success=False,
                error="Timeout na criacao do pre-registro"
            )
        except httpx.RequestError as e:
            print(f"[PRE-REGISTRATION] Erro de conexao: {e}")
            return PreRegistrationResult(
                success=False,
                error=f"Erro de conexao: {str(e)}"
            )
        except Exception as e:
            print(f"[PRE-REGISTRATION] Erro inesperado: {e}")
            return PreRegistrationResult(
                success=False,
                error=f"Erro inesperado: {str(e)}"
            )


# Instancia global (sera inicializada quando necessario)
_pre_registration_client: Optional[PreRegistrationClient] = None


def get_pre_registration_client() -> PreRegistrationClient:
    """Retorna a instancia global do cliente de pre-registro."""
    global _pre_registration_client
    if _pre_registration_client is None:
        _pre_registration_client = PreRegistrationClient()
    return _pre_registration_client
