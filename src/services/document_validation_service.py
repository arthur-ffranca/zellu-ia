# -*- coding: utf-8 -*-
"""
Serviço de validação de documentos para geração jurídica.

Valida se arquivo de origem + prompt são compatíveis antes de gastar tokens.
"""

import httpx
import time
from typing import Optional, Dict, Any
from src.llm import ChatModel
from config import get_settings
from src.services.ai_usage_tracker import track_usage

settings = get_settings()


class DocumentValidationService:
    """
    Serviço para validar arquivos e prompts antes de gerar documentos jurídicos.
    """

    def __init__(self):
        self.llm = ChatModel(
            model=settings.COMPANY_RESPONSE_AI_MODEL,
            temperature=0.1,  # Baixa temperatura para validação consistente
            max_tokens=500,
            request_timeout=30.0,  # Timeout de 30 segundos para evitar travamentos
            model_kwargs={"response_format": {"type": "json_object"}}  # Forçar saída JSON estruturada
        )

    async def validate_attachment(
        self,
        file_url: str,
        mime_type: str,
        document_type: Optional[str],
        custom_prompt: Optional[str],
    ) -> Dict[str, Any]:
        """
        Valida se o arquivo de origem é compatível com o tipo de documento e prompt.

        Args:
            file_url: URL para download do arquivo
            mime_type: Tipo MIME do arquivo
            document_type: Tipo de documento solicitado
            custom_prompt: Prompt customizado do usuário

        Returns:
            {
                "valid": bool,
                "rejection_reason": str (se invalid),
                "analysis_details": dict (detalhes técnicos)
            }
        """
        try:
            # 1. Validar tipo MIME básico
            allowed_mimes = [
                "application/pdf",
                "application/msword",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "text/plain"
            ]

            if mime_type not in allowed_mimes:
                return {
                    "valid": False,
                    "rejection_reason": f"Tipo de arquivo não suportado: {mime_type}. Use PDF, DOC, DOCX ou TXT.",
                    "analysis_details": {"mime_type": mime_type, "allowed": allowed_mimes}
                }

            # 2. Baixar arquivo (com timeout)
            #
            # Sem header nenhum. O `fileUrl` e uma URL presigned do R2: a
            # autorizacao vai na propria query string, o storage ignora a
            # x-api-key, e mandar a nossa chave deixava um segredo nosso nos logs
            # de um host que nao e o da Zellu (pedido da plataforma, 15/09 §2).
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                response = await client.get(file_url)
                response.raise_for_status()
                file_content = response.content

            # 3. Extrair texto do arquivo
            text_content = await self._extract_text_from_file(file_content, mime_type)
            if not text_content:
                return {
                    "valid": False,
                    "rejection_reason": "Não foi possível extrair texto do arquivo. Verifique se o arquivo contém conteúdo legível.",
                    "analysis_details": {"extracted_length": 0}
                }

            # 4. Usar LLM para validar compatibilidade
            validation_result = await self._validate_with_llm(
                text_content,
                document_type,
                custom_prompt
            )

            validation_result['extracted_text'] = text_content
            return validation_result

        except httpx.TimeoutException:
            return {
                "valid": False,
                "rejection_reason": "Timeout ao baixar o arquivo de origem.",
                "analysis_details": {"error": "timeout"}
            }
        except httpx.HTTPStatusError as e:
            response_text = e.response.text[:500] if e.response is not None else ""
            return {
                "valid": False,
                "rejection_reason": f"Erro ao acessar arquivo de origem: HTTP {e.response.status_code}",
                # `analysisDetails` viaja no callback ate a plataforma e fica no
                # log deles. Os headers da requisicao saiam aqui dentro - e
                # enquanto o download levou a nossa x-api-key, era ela que ia
                # junto a cada falha de download. Nao voltem a por headers aqui.
                "analysis_details": {
                    "http_status": e.response.status_code,
                    "http_body": response_text,
                    "file_url": file_url,
                }
            }
        except Exception as e:
            return {
                "valid": False,
                "rejection_reason": "Erro interno na validação do arquivo.",
                "analysis_details": {"error": str(e)}
            }

    async def _extract_text_from_file(self, file_content: bytes, mime_type: str) -> Optional[str]:
        """Extrai texto do arquivo baseado no MIME type."""
        try:
            if mime_type == "application/pdf":
                from PyPDF2 import PdfReader
                from io import BytesIO
                pdf_file = BytesIO(file_content)
                pdf_reader = PdfReader(pdf_file)
                text = ""
                for page in pdf_reader.pages:
                    page_text = page.extract_text() or ''
                    if not page_text.strip():
                        # Never claim a scanned/unread page was considered.
                        return None
                    text += page_text + "\n"
                    if len(text) > 200000:
                        return None
                return text.strip()

            elif mime_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
                from docx import Document
                from docx.text.paragraph import Paragraph
                from docx.table import Table
                from io import BytesIO
                doc = Document(BytesIO(file_content))
                parts = []
                for block in doc.iter_inner_content():
                    if isinstance(block, Paragraph):
                        parts.append(block.text)
                    elif isinstance(block, Table):
                        parts.extend(' | '.join(cell.text for cell in row.cells) for row in block.rows)
                text = '\n'.join(parts).strip()
                return text if len(text) <= 200000 else None

            elif mime_type == "application/msword":
                return None  # Legacy binary DOC requires conversion; do not fabricate text.

            elif mime_type == "text/plain":
                text = file_content.decode('utf-8-sig')
                return text if len(text) <= 200000 else None

            else:
                return None

        except Exception as e:
            print(f"[VALIDATION] Erro extraindo texto: {e}")
            return None

    async def _validate_with_llm(
        self,
        text_content: str,
        document_type: Optional[str],
        custom_prompt: Optional[str]
    ) -> Dict[str, Any]:
        """Usa LLM para validar se o conteúdo é compatível com o documento solicitado."""

        prompt_template = """
        Você é um assistente jurídico especializado em validar documentos de origem para geração de peças processuais.

        ANALISE o seguinte conteúdo de arquivo de origem e determine se ele é COMPATÍVEL para gerar o tipo de documento solicitado.

        CONTEÚDO DO ARQUIVO:
        {text_content}

        TIPO DE DOCUMENTO SOLICITADO: {document_type}
        PROMPT DO USUÁRIO: {custom_prompt}

        INSTRUÇÕES:
        - Seja rigoroso: o arquivo deve conter informações relevantes para o documento solicitado
        - Considere contexto jurídico brasileiro
        - Exemplos de incompatibilidade:
          * Pedir "petição inicial" mas arquivo é foto de perfil
          * Pedir "contrato" mas arquivo é notícia de jornal
          * Prompt vazio ou muito genérico sem especificar documento

        RETORNE APENAS um objeto JSON válido com estes campos:
        {{
            "valid": true/false,
            "reason": "explicação curta se inválido",
            "confidence": 0.0-1.0
        }}
        """

        try:
            _t0 = time.perf_counter()
            response = await self.llm.complete(prompt_template.format(**{
                "text_content": text_content,
                "document_type": document_type or "não especificado",
                "custom_prompt": custom_prompt or "não fornecido"
            }))
            track_usage(
                module="doc_validation",
                model=settings.COMPANY_RESPONSE_AI_MODEL,
                response=response,
                event_type="document_validation",
                duration_ms=int((time.perf_counter() - _t0) * 1000),
            )

            # Log raw output para depuração
            raw_content = response.content.strip()
            print(f"[VALIDATION] Raw LLM output: '{raw_content}'")

            # Verificar se resposta está vazia (possível timeout ou content policy)
            if not raw_content:
                print(f"[VALIDATION] LLM returned empty response - possible timeout or content policy")
                return {
                    "valid": False,
                    "rejection_reason": "LLM não retornou resposta válida (possível timeout ou política de conteúdo).",
                    "analysis_details": {
                        "llm_confidence": 0.0,
                        "text_length": len(text_content),
                        "raw_llm_output": "",
                        "error_type": "empty_response"
                    }
                }

            # Parse JSON da resposta com fallback defensivo
            import json
            import re

            result = None
            try:
                result = json.loads(raw_content)
            except json.JSONDecodeError:
                # Fallback: tentar extrair JSON de markdown wrapper
                json_match = re.search(r'```json\s*(\{.*?\})\s*```', raw_content, re.DOTALL)
                if json_match:
                    try:
                        result = json.loads(json_match.group(1))
                        print(f"[VALIDATION] Extracted JSON from markdown: {result}")
                    except json.JSONDecodeError:
                        pass

                if not result:
                    # Último fallback: assumir inválido se não conseguir parsear
                    print(f"[VALIDATION] Failed to parse JSON, assuming invalid")
                    result = {"valid": False, "reason": "Resposta do LLM não é JSON válido", "confidence": 0.0}

            return {
                "valid": result.get("valid", False),
                "rejection_reason": result.get("reason", "Validação indeterminada") if not result.get("valid") else None,
                "analysis_details": {
                    "llm_confidence": result.get("confidence", 0.0),
                    "text_length": len(text_content),
                    "raw_llm_output": raw_content[:200]  # Log limitado para análise
                }
            }

        except Exception as e:
            print(f"[VALIDATION] Erro no LLM: {e}")
            return {
                "valid": False,
                "rejection_reason": "Erro na análise automática do arquivo.",
                "analysis_details": {"llm_error": str(e), "error_type": type(e).__name__}
            }


# Singleton
_validation_service = None

def get_document_validation_service() -> DocumentValidationService:
    """Retorna instância singleton do serviço de validação."""
    global _validation_service
    if _validation_service is None:
        _validation_service = DocumentValidationService()
    return _validation_service
