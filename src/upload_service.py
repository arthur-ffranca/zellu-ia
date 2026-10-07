# -*- coding: utf-8 -*-
"""Upload service for handling file uploads."""

from typing import List, Dict, Any, Optional
from pathlib import Path
import re
import mimetypes
from fastapi import UploadFile
from fastapi.staticfiles import StaticFiles
import aiofiles
import unicodedata
import uuid

from config import Settings
from src.services.case_evidence import audit_uploaded_bytes, cache_uploaded_audit


# Extensao gravada vem do tipo REAL detectado nos bytes (ou, para formatos que o
# sniff nao reconhece, do MIME ja validado contra ALLOWED_MIME_TYPES). Nunca do
# nome enviado pelo usuario: "x.html" declarado como text/plain era salvo como
# .html e servido como text/html no dominio da API (XSS armazenado).
_EXT_BY_KIND = {
    "pdf": "pdf", "docx": "docx", "txt": "txt", "jpg": "jpg", "png": "png",
    "gif": "gif", "webp": "webp", "wav": "wav",
}
_EXT_BY_MIME = {
    "audio/webm": "webm", "audio/mpeg": "mp3", "audio/ogg": "ogg", "audio/m4a": "m4a",
    "audio/mp4": "m4a", "audio/x-m4a": "m4a", "audio/flac": "flac",
    "audio/wav": "wav", "audio/x-wav": "wav",
    "video/mp4": "mp4", "video/webm": "webm", "video/avi": "avi", "video/quicktime": "mov",
    "application/msword": "doc", "application/vnd.ms-excel": "xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "text/csv": "csv", "application/json": "json", "text/plain": "txt",
    "application/zip": "zip", "application/x-rar-compressed": "rar",
    "application/x-7z-compressed": "7z",
}


def safe_extension(detected_kind: Optional[str], declared_mime: str) -> str:
    """Extensao segura para gravar/servir o arquivo."""
    declared = (declared_mime or "").split(";", 1)[0].strip().lower()
    kind = (detected_kind or "").lower()
    # Texto e zip sao containers genericos: o MIME validado diz qual dos
    # formatos permitidos e (csv/json sobre texto, xlsx/docx sobre zip).
    if kind == "txt" and declared in {"text/csv", "application/json"}:
        return _EXT_BY_MIME[declared]
    if kind == "zip":
        return _EXT_BY_MIME.get(declared) if declared in {
            "application/zip",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        } else "zip"
    if kind in _EXT_BY_KIND:
        return _EXT_BY_KIND[kind]
    return _EXT_BY_MIME.get(declared, "bin")


class SafeUploadStaticFiles(StaticFiles):
    """Serve /uploads sem permitir que o navegador execute o conteudo enviado."""

    def file_response(self, full_path, stat_result, scope, status_code=200):
        response = super().file_response(full_path, stat_result, scope, status_code)
        response.headers["X-Content-Type-Options"] = "nosniff"
        # PDF fica de fora: o visualizador embutido do Chrome nao abre sob sandbox.
        if not str(full_path).lower().endswith(".pdf"):
            response.headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
        return response


class UploadService:
    """Service for handling file uploads and storage."""

    def __init__(self, settings: Settings):
        """Initialize upload service.

        Args:
            settings: Application settings
        """
        self.settings = settings
        self.upload_dir = Path(settings.UPLOAD_DIR)
        self.upload_dir.mkdir(parents=True, exist_ok=True)

    def sanitize_filename(self, filename: str, extension: Optional[str] = None) -> str:
        """Sanitize filename removing special characters.

        Args:
            filename: Original filename
            extension: Extensao segura a usar no lugar da enviada pelo usuario

        Returns:
            Sanitized filename with timestamp
        """
        # Get file extension
        name, ext = filename.rsplit('.', 1) if '.' in filename else (filename, '')
        if extension is not None:
            ext = extension

        # Remove accents
        name = unicodedata.normalize('NFKD', name)
        name = name.encode('ascii', 'ignore').decode('ascii')

        # Convert to lowercase and replace special chars with hyphen
        name = re.sub(r'[^a-z0-9]+', '-', name.lower())

        # Remove leading/trailing hyphens
        name = name.strip('-')

        # Limit length
        name = name[:80]

        # Add timestamp for uniqueness
        timestamp = uuid.uuid4().hex

        # Reconstruct filename
        return f"{timestamp}-{name}.{ext.lower()}"

    def validate_file(self, file: UploadFile, file_content: bytes) -> Optional[str]:
        """Validate file size and MIME type.

        Args:
            file: Upload file object
            file_content: File content in bytes

        Returns:
            Error message if invalid, None if valid
        """
        # Check file size
        file_size = len(file_content)

        if file_size > self.settings.MAX_FILE_SIZE:
            size_mb = file_size / (1024 * 1024)
            max_mb = self.settings.MAX_FILE_SIZE / (1024 * 1024)
            return f"{file.filename}: Arquivo excede {max_mb}MB (tamanho: {size_mb:.2f}MB)"

        # Check MIME type
        mime_type = file.content_type or mimetypes.guess_type(file.filename)[0] or "application/octet-stream"

        if mime_type not in self.settings.ALLOWED_MIME_TYPES:
            return f"{file.filename}: Tipo de arquivo não permitido ({mime_type})"

        return None

    async def save_file(self, file: UploadFile, remaining_bytes: Optional[int] = None) -> Dict[str, Any]:
        """Recebe os bytes do chat, audita e so depois persiste localmente.

        R2/MinIO nao participam deste fluxo. O storage externo pode ser encaixado
        depois da auditoria sem mudar o contrato do front.
        """
        try:
            limit = self.settings.MAX_FILE_SIZE
            if remaining_bytes is not None:
                limit = min(limit, remaining_bytes)
            content = await file.read(limit + 1)
            if remaining_bytes is not None and len(content) > remaining_bytes:
                return {"error": "Limite total de arquivos excedido"}

            error = self.validate_file(file, content)
            if error:
                return {"error": error}

            mime_type = (
                file.content_type
                or mimetypes.guess_type(file.filename)[0]
                or "application/octet-stream"
            )

            # Auditoria trabalha nos bytes que acabaram de chegar do chat.
            audit_result = await audit_uploaded_bytes(
                content=content,
                filename=file.filename,
                mime=mime_type,
            )

            # Arquivo perigoso nao vira /uploads e nao fica disponivel para parser,
            # Vision, LLM ou usuario. O front ainda recebe o motivo estruturado.
            if audit_result.get("securityBlocked"):
                # Resposta vai para o chat do consumidor: motivo acionavel, sem
                # status/risco/flags da auditoria (esses ficam no registro interno).
                flags = (audit_result.get("audit") or {}).get("securityFlags") or []
                password = "PDF_PASSWORD_REQUIRED" in flags
                message = (
                    "PDF protegido por senha; envie uma versão sem senha"
                    if password
                    else "não foi possível aceitar este arquivo; envie outro formato ou uma nova cópia"
                )
                return {
                    "error": f"{file.filename}: {message}",
                    "filename": file.filename,
                    "size": len(content),
                    "blocked": True,
                    "reason": "password_required" if password else "security_policy",
                }

            detected_kind = ((audit_result.get("audit") or {}).get("file") or {}).get("kind")
            sanitized_name = self.sanitize_filename(
                file.filename, safe_extension(detected_kind, mime_type)
            )
            file_path = self.upload_dir / sanitized_name
            async with aiofiles.open(file_path, 'wb') as f:
                await f.write(content)

            # Cache local somente do preflight de seguranca feito no upload.
            # Parsing/OCR/Vision continuam proibidos aqui e rodam apenas pos-caso.
            cache_record = {
                **audit_result,
                "storageName": sanitized_name,
                "originalName": file.filename,
                "mimeType": mime_type,
                "size": len(content),
            }
            await cache_uploaded_audit(sanitized_name, cache_record)

            return {
                "url": self._get_file_url(sanitized_name),
                "mimeType": mime_type,
                "filename": file.filename,
                "size": len(content),
                "sha256": audit_result.get("sha256"),
            }

        except Exception as e:
            # Nao devolver detalhes internos do parser/scanner para o cliente.
            print(f"[UPLOAD-AUDIT] falha em {file.filename}: {type(e).__name__}")
            return {"error": f"{file.filename}: Erro ao processar upload"}

    def _get_file_url(self, filename: str) -> str:
        """Get public URL for uploaded file.

        Args:
            filename: Sanitized filename

        Returns:
            Public URL
        """
        # O arquivo permanece no proprio servico por enquanto. SERVICE_BASE_URL
        # evita devolver 0.0.0.0 quando a aplicacao roda em container.
        base_url = (getattr(self.settings, "SERVICE_BASE_URL", "") or "").rstrip("/")
        if not base_url:
            host = self.settings.API_HOST
            if host in {"0.0.0.0", "::"}:
                host = "localhost"
            base_url = f"http://{host}:{self.settings.API_PORT}"
        return f"{base_url}/uploads/{filename}"

    async def upload_multiple(self, files: List[UploadFile]) -> Dict[str, Any]:
        """Upload multiple files.

        Args:
            files: List of upload file objects

        Returns:
            Response with uploaded files info and warnings
        """
        if not files:
            return {"error": "Nenhum arquivo fornecido"}

        uploaded_files = []
        warnings = []
        total_size = 0
        rejected_files = []

        # Process each file
        for file in files:
            if total_size >= self.settings.MAX_TOTAL_SIZE:
                warnings.append("Limite total de arquivos excedido")
                continue
            result = await self.save_file(file, self.settings.MAX_TOTAL_SIZE - total_size)

            if "error" in result:
                warnings.append(result["error"])
                rejected_files.append(result)
            else:
                uploaded_files.append(result)
                total_size += result["size"]

        # Check if all files failed
        if not uploaded_files:
            return {
                "error": "Nenhum arquivo pôde ser enviado",
                "details": warnings,
                "rejectedFiles": rejected_files,
            }

        # Build response
        response = {
            "success": True,
            "files": uploaded_files
        }

        if warnings:
            response["uploadStatus"] = "partial"
            response["warnings"] = warnings
            response["rejectedFiles"] = rejected_files

        return response
