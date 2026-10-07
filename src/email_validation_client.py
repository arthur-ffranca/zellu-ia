# -*- coding: utf-8 -*-
"""
Cliente para validacao de email no backend Zellu.

Usa o endpoint POST /api/ai/validate-user-email para verificar
se um email ja existe na base de dados.
"""

import httpx
from typing import Optional
from dataclasses import dataclass
from config import get_settings

settings = get_settings()


@dataclass
class EmailValidationResult:
    """Resultado da validacao de email.

    Campos conforme documentacao /api/ai/validate-user-email:
    - success: True se a requisicao foi bem sucedida
    - exists: True se o email ja existe na base (usuario cadastrado)
    - email_invalid: True se o email foi rejeitado pela validacao externa
    - verification_status: Status da verificacao ('safe', 'risky', 'invalid', 'unknown')
    - error: Mensagem de erro para exibir ao usuario
    """
    success: bool
    exists: bool = False
    email_invalid: bool = False
    verification_status: Optional[str] = None
    error: Optional[str] = None


class EmailValidationClient:
    """
    Cliente para validar emails no backend Zellu.

    Endpoint: POST /api/ai/validate-user-email
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: float = 30.0  # 30s para cobrir os ~24s do backend (2 tentativas de 10s + delays)
    ):
        """
        Inicializa o cliente de validacao de email.

        Args:
            base_url: URL base do backend Zellu (ex: https://api.zellu.com.br)
            api_key: API Key para autenticacao
            timeout: Timeout em segundos para a requisicao
        """
        self.base_url = (base_url or settings.ZELLU_BACKEND_URL).rstrip('/')
        self.api_key = api_key or settings.API_KEY_ZELLU_IA
        self.timeout = timeout
        self.endpoint = "/api/ai/validate-user-email"

        print(f"[EMAIL-VALIDATION] Inicializado - URL: {self.base_url}{self.endpoint}")

    async def validate(self, email: str) -> EmailValidationResult:
        """
        Valida se um email ja existe na base de dados.

        Args:
            email: Email a ser verificado

        Returns:
            EmailValidationResult com o resultado da validacao
        """
        if not email or not email.strip():
            return EmailValidationResult(
                success=False,
                error="Email vazio"
            )

        email = email.strip().lower()
        url = f"{self.base_url}{self.endpoint}"

        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key
        }

        payload = {
            "email": email
        }

        print(f"[EMAIL-VALIDATION] Validando: '{email}'")

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(url, json=payload, headers=headers)

                print(f"[EMAIL-VALIDATION] Status: {response.status_code}")

                if response.status_code == 200:
                    data = response.json()

                    if data.get("success"):
                        exists = data.get("exists", False)
                        print(f"[EMAIL-VALIDATION] Email {'existe' if exists else 'nao existe'} na base")
                        return EmailValidationResult(
                            success=True,
                            exists=exists
                        )
                    else:
                        error_msg = data.get("error", "Erro desconhecido")
                        print(f"[EMAIL-VALIDATION] Erro: {error_msg}")
                        return EmailValidationResult(
                            success=False,
                            error=error_msg
                        )

                elif response.status_code == 400:
                    data = response.json()
                    error_msg = data.get("error", "Formato de email invalido")
                    email_invalid = data.get("emailInvalid", False)
                    verification_status = data.get("verificationStatus")

                    print(f"[EMAIL-VALIDATION] Erro (400): {error_msg}")
                    print(f"[EMAIL-VALIDATION] emailInvalid={email_invalid}, verificationStatus={verification_status}")

                    return EmailValidationResult(
                        success=False,
                        exists=False,
                        email_invalid=email_invalid,
                        verification_status=verification_status,
                        error=error_msg
                    )

                elif response.status_code == 401:
                    print(f"[EMAIL-VALIDATION] Erro de autenticacao (401)")
                    return EmailValidationResult(
                        success=False,
                        error="Erro de autenticacao com o backend"
                    )

                else:
                    print(f"[EMAIL-VALIDATION] Erro HTTP: {response.status_code}")
                    return EmailValidationResult(
                        success=False,
                        error=f"Erro HTTP {response.status_code}"
                    )

        except httpx.TimeoutException:
            print(f"[EMAIL-VALIDATION] Timeout ao validar email")
            return EmailValidationResult(
                success=False,
                error="Timeout na validacao"
            )
        except httpx.RequestError as e:
            print(f"[EMAIL-VALIDATION] Erro de conexao: {e}")
            return EmailValidationResult(
                success=False,
                error=f"Erro de conexao: {str(e)}"
            )
        except Exception as e:
            print(f"[EMAIL-VALIDATION] Erro inesperado: {e}")
            return EmailValidationResult(
                success=False,
                error=f"Erro inesperado: {str(e)}"
            )


# Instancia global (sera inicializada quando necessario)
_email_validation_client: Optional[EmailValidationClient] = None


def get_email_validation_client() -> EmailValidationClient:
    """Retorna a instancia global do cliente de validacao de email."""
    global _email_validation_client
    if _email_validation_client is None:
        _email_validation_client = EmailValidationClient()
    return _email_validation_client
