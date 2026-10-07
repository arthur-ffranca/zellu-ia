# -*- coding: utf-8 -*-
"""
Serviço para upload de arquivos e envio de callbacks HTTP.

Usado para integração com backend Zellu.
"""

import httpx
import json
from typing import Dict, Any, Optional
from config import get_settings
from src.services.webhook_signature import build_signed_headers

settings = get_settings()


class CallbackService:
    """
    Serviço para upload de arquivos e envio de callbacks para o backend Zellu.
    """

    def __init__(self):
        self.client = httpx.AsyncClient(timeout=60.0)  # 60s timeout
        self.hmac_secret = settings.AI_WEBHOOK_HMAC_SECRET

    async def upload_file(
        self,
        upload_url: str,
        api_key: str,
        file_bytes: bytes,
        filename: str,
        mime_type: str = "application/pdf"
    ) -> Dict[str, Any]:
        """
        Faz upload de arquivo via multipart para o backend Zellu.

        Args:
            upload_url: URL do endpoint de upload
            api_key: Chave API para autenticação
            file_bytes: Conteúdo do arquivo em bytes
            filename: Nome do arquivo
            mime_type: Tipo MIME do arquivo

        Returns:
            Resposta do backend: {"fileUrl": "...", "fileId": "...", etc.}
        """
        try:
            files = {
                'file': (filename, file_bytes, mime_type)
            }

            headers = {
                'x-api-key': api_key
            }

            response = await self.client.post(
                upload_url,
                files=files,
                headers=headers
            )
            response.raise_for_status()

            result = response.json()
            print(f"[CALLBACK] Upload successful: {filename} -> {result.get('fileUrl', 'N/A')}")
            return result

        except httpx.HTTPStatusError as e:
            print(f"[CALLBACK] Upload failed: HTTP {e.response.status_code} - {e.response.text}")
            raise
        except Exception as e:
            print(f"[CALLBACK] Upload error: {e}")
            raise

    async def send_callback(
        self,
        callback_url: str,
        api_key: str,
        payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Envia callback para o backend Zellu.

        Args:
            callback_url: URL do endpoint de callback
            api_key: Chave API para autenticação
            payload: Payload JSON a enviar

        Returns:
            Resposta do backend
        """
        try:
            payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            headers = {
                'x-api-key': api_key,
                'Content-Type': 'application/json'
            }
            if self.hmac_secret:
                headers.update(build_signed_headers(payload_json, self.hmac_secret))

            response = await self.client.post(
                callback_url,
                content=payload_json,
                headers=headers
            )
            response.raise_for_status()

            result = response.json()
            print(f"[CALLBACK] Callback successful: {callback_url}")
            return result

        except httpx.HTTPStatusError as e:
            print(f"[CALLBACK] Callback failed: HTTP {e.response.status_code} - {e.response.text}")
            raise
        except Exception as e:
            print(f"[CALLBACK] Callback error: {e}")
            raise

    async def close(self):
        """Fecha o cliente HTTP."""
        await self.client.aclose()


# Singleton
_callback_service = None

def get_callback_service() -> CallbackService:
    """Retorna instância singleton do serviço de callback."""
    global _callback_service
    if _callback_service is None:
        _callback_service = CallbackService()
    return _callback_service