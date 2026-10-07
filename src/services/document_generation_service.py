# -*- coding: utf-8 -*-
"""
Serviço de geração de documentos jurídicos via IA.

Orquestra o fluxo completo conforme DOCUMENT_GENERATION_API.md:
1. Recebe request com contexto do caso
2. Usa CaseDocuments para gerar PDF via IA
3. Faz upload do PDF para o backend (uploadUrl)
4. Chama callback do backend (callbackUrl) com metadados

Executado em background task para não bloquear o endpoint.
"""

import io
import traceback
from typing import Dict, Any, Optional

import httpx

from api.schemas_document_generation import (
    DocumentGenerationRequest,
    DocumentGenerationCallbackPayload,
    SourceValidation,
)
from src.services.case_documents import CaseDocuments
from src.services.document_validation_service import get_document_validation_service
from src.services.callback_service import get_callback_service
from config import get_settings


def _chave_s2s() -> str:
    """Chave do header x-api-key no upload e no callback.

    Sai da NOSSA configuração, não mais do `apiKey` do corpo do pedido (pedido da
    plataforma em 15/09, §2). É a mesma que o pós-chamada da voz já usa para o
    mesmo `/api/ai-service/upload` (`agents/conversational_agent/post_call.py`),
    para a rota não receber duas chaves diferentes dependendo de quem chama.

    ⚠️ Vale só para o host da Zellu. O download do anexo NÃO leva chave nenhuma:
    o `fileUrl` é uma URL presigned do R2, a autorização vai na própria query
    string, e mandar a nossa chave ali a deixaria nos logs de outro serviço.
    """
    return get_settings().ai_usage_api_key


class DocumentGenerationService:
    """Orquestra geração de documentos jurídicos: IA -> PDF -> Upload -> Callback."""

    def __init__(self, writer_agent: CaseDocuments):
        self.writer = writer_agent

    @staticmethod
    def _resolve_url(url: str) -> str:
        """Resolve URLs relativas para absolutas usando ZELLU_BACKEND_URL."""
        if url.startswith("http://") or url.startswith("https://"):
            return url
        settings = get_settings()
        base = settings.ZELLU_BACKEND_URL.rstrip("/")
        path = url if url.startswith("/") else f"/{url}"
        resolved = f"{base}{path}"
        print(f"[DOC-GEN] URL relativa detectada: {url} -> {resolved}")
        return resolved

    async def process_document_generation(self, request: DocumentGenerationRequest) -> None:
        """
        Processa a geração de documento em background.

        Fluxo:
        1. Validar arquivo/prompt se houver anexo
        2. Se inválido: callback com sourceValidation.valid=false
        3. Se válido: gerar PDF, upload, callback com sucesso

        Args:
            request: DocumentGenerationRequest com contexto completo
        """
        ticket_id = request.ticketId
        ticket_number = ""
        if request.context.ticket:
            ticket_number = request.context.ticket.ticketNumber or ""

        generated_by_id = request.generatedById

        print(f"[DOC-GEN] ========================================")
        print(f"[DOC-GEN] Iniciando geração de documento")
        print(f"[DOC-GEN] TicketId: {ticket_id}")
        print(f"[DOC-GEN] TicketNumber: {ticket_number}")
        print(f"[DOC-GEN] GeneratedById: {generated_by_id or 'N/A (fluxo advogado)'}")
        print(f"[DOC-GEN] DocumentType: {request.context.documentType or 'N/A (inferir do contexto)'}")
        print(f"[DOC-GEN] Attachments: {len(request.context.attachments)} arquivos")

        try:
            # Validate and extract every source once. No silent partial input.
            import json
            source_documents = []
            for attachment in request.context.attachments:
                validation_service = get_document_validation_service()
                validation_result = await validation_service.validate_attachment(
                    file_url=attachment.fileUrl, mime_type=attachment.mimeType,
                    document_type=request.context.documentType,
                    custom_prompt=request.context.customPrompt,
                )
                text = validation_result.get('extracted_text', '')
                if not validation_result['valid'] or not text.strip():
                    await self._send_validation_callback(
                        callback_url=self._resolve_url(request.callbackUrl),
                        api_key=_chave_s2s(), ticket_id=ticket_id,
                        file_id=attachment.fileId,
                        rejection_reason=validation_result.get('rejection_reason') or 'Não foi possível ler integralmente o documento de origem.',
                        analysis_details=validation_result.get('analysis_details', {}),
                    )
                    return
                source_documents.append({'fileId': attachment.fileId, 'name': attachment.fileName, 'content': text})
            attachment_content = json.dumps(source_documents, ensure_ascii=False) if source_documents else None

            # 3. Gerar documento via CaseDocuments (LLM -> HTML -> PDF WeasyPrint + DOCX python-docx)
            print(f"[DOC-GEN] Passo 2: Gerando documento via CaseDocuments...")
            doc_result = await self.writer.generate_legal_document(
                context=request.context,
                ticket_number=ticket_number,
                attachment_content=attachment_content,
            )
            pdf_bytes = doc_result["pdf_bytes"]
            file_name = doc_result["file_name"]
            docx_bytes = doc_result.get("docx_bytes")
            docx_file_name = doc_result.get("docx_file_name")
            document_summary = doc_result["document_summary"]
            message_body = doc_result["message_body"]

            print(f"[DOC-GEN] PDF gerado: {file_name} ({len(pdf_bytes)} bytes)")
            if docx_bytes:
                print(f"[DOC-GEN] DOCX gerado: {docx_file_name} ({len(docx_bytes)} bytes)")

            # 4. Upload do PDF para o backend
            upload_url = self._resolve_url(request.uploadUrl)
            print(f"[DOC-GEN] Passo 3: Upload para {upload_url}...")
            callback_service = get_callback_service()
            upload_result = await callback_service.upload_file(
                upload_url=upload_url,
                api_key=_chave_s2s(),
                file_bytes=pdf_bytes,
                filename=file_name,
                mime_type="application/pdf"
            )
            file_url = upload_result["fileUrl"]

            print(f"[DOC-GEN] Upload concluído: {file_url[:80]}...")

            # 5. Callback para o backend
            callback_url = self._resolve_url(request.callbackUrl)
            print(f"[DOC-GEN] Passo 4: Callback para {callback_url}...")
            source_file_ref = (
                {"originFileId": request.context.attachments[0].fileId}
                if request.context.attachments
                else None
            )
            case_summary = doc_result.get("case_summary") or ""
            document_preview = case_summary or document_summary
            await self._send_success_callback(
                callback_url=callback_url,
                api_key=_chave_s2s(),
                ticket_id=ticket_id,
                file_url=file_url,
                file_name=file_name,
                file_size=len(pdf_bytes),
                document_summary=document_summary,
                message_body=message_body,
                generated_by_id=generated_by_id,
                source_file_ref=source_file_ref,
                document_preview=document_preview,
            )

            print(f"[DOC-GEN] Callback de sucesso enviado")

            # 6. Upload do DOCX (editavel).
            #
            #    O upload continua: o Word segue existindo e recuperavel pela URL
            #    logada abaixo. O que saiu foi o SEGUNDO CALLBACK - cada callback
            #    cria uma mensagem no chat, e _send_success_callback leva UM
            #    arquivo por chamada, entao anunciar os dois formatos fazia o
            #    mesmo documento aparecer duas vezes para o cliente.
            #
            #    Isolado num try/except: se o DOCX falhar, o PDF ja foi entregue.
            if docx_bytes and docx_file_name:
                try:
                    print(f"[DOC-GEN] Passo 5: Upload DOCX para {upload_url}...")
                    docx_upload_result = await callback_service.upload_file(
                        upload_url=upload_url,
                        api_key=_chave_s2s(),
                        file_bytes=docx_bytes,
                        filename=docx_file_name,
                        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    )
                    docx_file_url = docx_upload_result["fileUrl"]
                    print(f"[DOC-GEN] Upload DOCX concluído: {docx_file_url}")

                    if not get_settings().DOCUMENT_DOCX_CALLBACK_ENABLED:
                        print(
                            f"[DOC-GEN] Callback do DOCX desligado - o Word esta no "
                            f"storage e nao vira segunda mensagem no chat. Para o "
                            f"backend exibir 'baixar em Word' no mesmo card, e esta "
                            f"URL que ele precisa."
                        )
                    else:
                        print(f"[DOC-GEN] Passo 6: Callback DOCX para {callback_url}...")
                        await self._send_success_callback(
                            callback_url=callback_url,
                            api_key=_chave_s2s(),
                            ticket_id=ticket_id,
                            file_url=docx_file_url,
                            file_name=docx_file_name,
                            file_size=len(docx_bytes),
                            document_summary=document_summary,
                            message_body="Documento gerado em formato Word (editável).",
                            generated_by_id=generated_by_id,
                            source_file_ref=source_file_ref,
                            document_preview=document_preview,
                            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        )
                        print(f"[DOC-GEN] Callback DOCX enviado")
                except Exception as docx_err:
                    print(f"[DOC-GEN] ERRO no fluxo DOCX (PDF ja foi entregue): {docx_err}")

            print(f"[DOC-GEN] ========================================")

        except Exception as e:
            error_trace = traceback.format_exc()
            print(f"[DOC-GEN] ERRO na geração de documento:")
            print(f"[DOC-GEN] {error_trace}")
            print(f"[DOC-GEN] ========================================")
            # A background failure must not leave the caller waiting silently.
            try:
                details = {'stage': 'document_generation', 'error_type': type(e).__name__}
                reason = 'Documento não gerado: a leitura, geração ou conferência de fidelidade não foi concluída. Revise a solicitação e tente novamente.'
                # Quem pediu precisa saber O QUE reprovou, nao so que reprovou.
                issues = getattr(e, 'missing_requirements', None)
                if issues:
                    from src.services import document_fidelity
                    reason = document_fidelity.failure_message(issues)
                    details['issues'] = [
                        {'rule': str(i.get('label', ''))[:80], 'reason': str(i.get('value', ''))[:300]}
                        for i in issues[:20]]
                elif type(e).__name__ == 'UnknownDocumentTypeError':
                    reason = ('Documento não gerado: não foi possível identificar o tipo de documento pedido. '
                              'Informe se é petição, notificação extrajudicial, acordo ou procuração e gere novamente.')
                payload = DocumentGenerationCallbackPayload(ticketId=ticket_id, messageBody=reason)
                if 'Timeout' in type(e).__name__:
                    reason = ('Documento não gerado: o modelo demorou mais que o limite para redigir. '
                              'Tente novamente em instantes.')
                    payload.messageBody = reason
                # sourceValidation so quando ha arquivo de origem. Sem anexo o backend
                # ainda nao tem formato de falha: recusa o aviso com HTTP 400 (sem
                # sourceValidation) ou quebra com HTTP 500 (fileId vazio). Fica
                # registrado aqui ate o backend aceitar falha sem arquivo.
                attachments = request.context.attachments
                if attachments:
                    payload.sourceValidation = SourceValidation(
                        fileId=attachments[0].fileId, valid=False,
                        rejectionReason=reason[:200], analysisDetails=details,
                    )
                else:
                    print('[DOC-GEN] Falha sem anexo: o backend ainda nao aceita este aviso; '
                          f'motivo que o site deveria mostrar: {reason[:300]}')
                    return
                await get_callback_service().send_callback(
                    callback_url=self._resolve_url(request.callbackUrl), api_key=_chave_s2s(),
                    payload=payload.model_dump(exclude_none=True),
                )
            except Exception as callback_error:
                print(f'[DOC-GEN] Callback de falha não entregue: {type(callback_error).__name__}')

    async def _send_validation_callback(
        self,
        callback_url: str,
        api_key: str,
        ticket_id: str,
        file_id: str,
        rejection_reason: str,
        analysis_details: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Envia callback de rejeição de validação."""
        payload = DocumentGenerationCallbackPayload(
            ticketId=ticket_id,
            sourceValidation=SourceValidation(
                fileId=file_id,
                valid=False,
                rejectionReason=rejection_reason,
                analysisDetails=analysis_details
            )
        )

        callback_service = get_callback_service()
        return await callback_service.send_callback(
            callback_url=callback_url,
            api_key=api_key,
            payload=payload.model_dump(exclude_none=True)
        )

    async def _send_success_callback(
        self,
        callback_url: str,
        api_key: str,
        ticket_id: str,
        file_url: str,
        file_name: str,
        file_size: int,
        document_summary: Optional[str],
        message_body: Optional[str],
        generated_by_id: Optional[str],
        source_file_ref: Optional[Dict[str, Any]],
        document_preview: Optional[str] = None,
        mime_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Envia callback de sucesso na geração."""
        payload = DocumentGenerationCallbackPayload(
            ticketId=ticket_id,
            fileUrl=file_url,
            fileName=file_name,
            fileSize=file_size,
            mimeType=mime_type,
            documentSummary=document_summary,
            messageBody=message_body,
            generatedById=generated_by_id,
            sourceFileRef=source_file_ref,
            documentPreview=document_preview,
        )

        callback_service = get_callback_service()
        return await callback_service.send_callback(
            callback_url=callback_url,
            api_key=api_key,
            payload=payload.model_dump(exclude_none=True)
        )
