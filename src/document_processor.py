# -*- coding: utf-8 -*-
"""Document processor for extracting text from various file formats."""

import io
import httpx
from typing import Optional, Dict, Any
from pathlib import Path

# PDF extraction
try:
    import PyPDF2
    PDF_SUPPORT = True
except ImportError:
    PDF_SUPPORT = False
    print("[WARNING] PyPDF2 not installed - PDF support disabled")

# DOCX extraction
try:
    import docx
    DOCX_SUPPORT = True
except ImportError:
    DOCX_SUPPORT = False
    print("[WARNING] python-docx not installed - DOCX support disabled")


class DocumentProcessor:
    """Process documents from MinIO and extract text for RAG indexing."""

    def __init__(self, minio_base_url: str = ""):
        """Initialize document processor.

        Args:
            minio_base_url: Base URL for MinIO storage (e.g., http://minio.zellu.com)
        """
        self.minio_base_url = minio_base_url.rstrip("/") if minio_base_url else ""
        self.http_client = httpx.AsyncClient(timeout=60.0)

    async def download_file(self, minio_path: str) -> Optional[bytes]:
        """Download file from MinIO.

        Args:
            minio_path: Path in MinIO (e.g., company-id/file-id.pdf)

        Returns:
            File content as bytes, or None if download failed
        """
        if not self.minio_base_url:
            print(f"[DOC-PROCESSOR] MinIO base URL not configured")
            return None

        try:
            url = f"{self.minio_base_url}/{minio_path}"
            print(f"[DOC-PROCESSOR] Downloading: {url}")

            response = await self.http_client.get(url)
            response.raise_for_status()

            print(f"[DOC-PROCESSOR] Downloaded {len(response.content)} bytes")
            return response.content

        except Exception as e:
            print(f"[DOC-PROCESSOR] Error downloading {minio_path}: {e}")
            return None

    def extract_text_from_pdf(self, content: bytes) -> str:
        """Extract text from PDF content.

        Args:
            content: PDF file content as bytes

        Returns:
            Extracted text
        """
        if not PDF_SUPPORT:
            return ""

        try:
            pdf_reader = PyPDF2.PdfReader(io.BytesIO(content))
            text_parts = []

            for page in pdf_reader.pages:
                text = page.extract_text()
                if text:
                    text_parts.append(text)

            full_text = "\n\n".join(text_parts)
            print(f"[DOC-PROCESSOR] Extracted {len(full_text)} chars from PDF ({len(pdf_reader.pages)} pages)")
            return full_text

        except Exception as e:
            print(f"[DOC-PROCESSOR] Error extracting PDF text: {e}")
            return ""

    def extract_text_from_docx(self, content: bytes) -> str:
        """Extract text from DOCX content.

        Args:
            content: DOCX file content as bytes

        Returns:
            Extracted text
        """
        if not DOCX_SUPPORT:
            return ""

        try:
            doc = docx.Document(io.BytesIO(content))
            text_parts = []

            for para in doc.paragraphs:
                if para.text.strip():
                    text_parts.append(para.text)

            full_text = "\n\n".join(text_parts)
            print(f"[DOC-PROCESSOR] Extracted {len(full_text)} chars from DOCX ({len(text_parts)} paragraphs)")
            return full_text

        except Exception as e:
            print(f"[DOC-PROCESSOR] Error extracting DOCX text: {e}")
            return ""

    def extract_text_from_txt(self, content: bytes) -> str:
        """Extract text from TXT content.

        Args:
            content: TXT file content as bytes

        Returns:
            Extracted text
        """
        try:
            # Try UTF-8 first, then Latin-1
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                text = content.decode("latin-1")

            print(f"[DOC-PROCESSOR] Extracted {len(text)} chars from TXT")
            return text

        except Exception as e:
            print(f"[DOC-PROCESSOR] Error extracting TXT text: {e}")
            return ""

    def extract_text(self, content: bytes, file_type: str) -> str:
        """Extract text from file content based on type.

        Args:
            content: File content as bytes
            file_type: File type (PDF, DOCX, TXT, etc.)

        Returns:
            Extracted text
        """
        file_type_upper = file_type.upper()

        if file_type_upper == "PDF":
            return self.extract_text_from_pdf(content)
        elif file_type_upper in ("DOCX", "DOC"):
            return self.extract_text_from_docx(content)
        elif file_type_upper in ("TXT", "TEXT", "MD", "MARKDOWN"):
            return self.extract_text_from_txt(content)
        else:
            print(f"[DOC-PROCESSOR] Unsupported file type: {file_type}")
            return ""

    async def process_file(
        self,
        minio_path: str,
        file_type: str,
        metadata: Optional[Dict[str, Any]] = None
    ) -> Optional[Dict[str, Any]]:
        """Download and process a file from MinIO.

        Args:
            minio_path: Path in MinIO
            file_type: File type
            metadata: Additional metadata to include

        Returns:
            Dictionary with extracted text and metadata, or None if failed
        """
        # Download file
        content = await self.download_file(minio_path)
        if not content:
            return None

        # Extract text
        text = self.extract_text(content, file_type)
        if not text:
            print(f"[DOC-PROCESSOR] No text extracted from {minio_path}")
            return None

        result = {
            "text": text,
            "minio_path": minio_path,
            "file_type": file_type,
            "char_count": len(text),
            **(metadata or {})
        }

        return result

    async def close(self):
        """Close HTTP client."""
        await self.http_client.aclose()


# Global instance (initialized in main.py)
document_processor: Optional[DocumentProcessor] = None
