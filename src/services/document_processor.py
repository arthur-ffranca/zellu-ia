"""
Document Processor - Zellu IA Empresa

Serviço responsável pelo processamento de documentos para indexação no RAG.
Realiza extração de texto e divisão em chunks otimizados para embeddings.

O que faz:
    1. Extrai texto de diferentes formatos (PDF, DOCX, TXT)
    2. Divide textos longos em chunks menores
    3. Mantém overlap entre chunks para preservar contexto
    4. Adiciona metadados para rastreabilidade

Estratégia de Chunking:
    - Chunk Size: 500 caracteres (configurável)
    - Overlap: 50 caracteres (configurável)
    - Divisão por parágrafos (preserva contexto semântico)

Por que Chunking?
    Embeddings têm limite de tokens. Dividir documentos em chunks
    permite indexar documentos longos e retornar apenas os trechos
    mais relevantes para cada query.

Classes:
    DocumentProcessor: Processa e divide documentos em chunks

Uso:
    >>> from src.services import document_processor
    >>> chunks = document_processor.chunk_text(
    ...     text="Texto longo do documento...",
    ...     document_id="doc_123",
    ...     metadata={"title": "FAQ", "category": "atendimento"}
    ... )
"""

from typing import List, Dict, Any, Optional
import uuid
import logging

from config import get_settings

logger = logging.getLogger(__name__)


class DocumentProcessor:
    """
    Processa documentos para indexação no sistema RAG.

    Responsável por extrair texto de diferentes formatos de arquivo
    e dividir em chunks otimizados para geração de embeddings.

    Attributes:
        settings: Configurações da aplicação
        chunk_size: Tamanho máximo de cada chunk em caracteres
        chunk_overlap: Sobreposição entre chunks consecutivos

    Example:
        >>> processor = DocumentProcessor()
        >>> chunks = processor.chunk_text(
        ...     text="Conteúdo do documento...",
        ...     document_id="doc_001"
        ... )
        >>> print(f"Documento dividido em {len(chunks)} chunks")
    """

    def __init__(self):
        """
        Inicializa o processador com as configurações de chunking.

        Carrega chunk_size e chunk_overlap das configurações da aplicação.
        """
        self.settings = get_settings()
        self.chunk_size = self.settings.rag_chunk_size
        self.chunk_overlap = self.settings.rag_chunk_overlap

    def chunk_text(
        self,
        text: str,
        document_id: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Divide texto em chunks com overlap para indexação.

        Estratégia:
        1. Divide o texto por parágrafos (\\n\\n)
        2. Agrupa parágrafos até atingir chunk_size
        3. Mantém overlap do final do chunk anterior no início do próximo

        Args:
            text: Texto completo do documento
            document_id: ID único do documento (para rastreabilidade)
            metadata: Metadados adicionais (título, categoria, etc)

        Returns:
            Lista de dicionários, cada um representando um chunk:
                - id: ID único do chunk (document_id_chunk_N)
                - content: Texto do chunk
                - document_id: ID do documento original
                - chunk_index: Índice do chunk (0, 1, 2...)
                - metadata: Metadados combinados

        Example:
            >>> chunks = processor.chunk_text(
            ...     text="Parágrafo 1\\n\\nParágrafo 2\\n\\nParágrafo 3",
            ...     document_id="doc_123"
            ... )
        """
        metadata = metadata or {}
        chunks = []

        # Limpar texto
        text = text.strip()
        if not text:
            return chunks

        # Dividir em parágrafos primeiro
        paragraphs = text.split("\n\n")
        current_chunk = ""
        chunk_index = 0

        for para in paragraphs:
            para = para.strip()
            if not para:
                continue

            # Se adicionar o parágrafo excede o limite
            if len(current_chunk) + len(para) + 2 > self.chunk_size:
                # Salva chunk atual se tiver conteúdo
                if current_chunk:
                    chunks.append(self._create_chunk(
                        content=current_chunk,
                        document_id=document_id,
                        chunk_index=chunk_index,
                        metadata=metadata,
                    ))
                    chunk_index += 1

                    # Overlap: pega parte do final do chunk anterior
                    if self.chunk_overlap > 0 and len(current_chunk) > self.chunk_overlap:
                        overlap_text = current_chunk[-self.chunk_overlap:]
                        current_chunk = overlap_text + "\n\n" + para
                    else:
                        current_chunk = para
                else:
                    current_chunk = para
            else:
                if current_chunk:
                    current_chunk += "\n\n" + para
                else:
                    current_chunk = para

        # Último chunk
        if current_chunk:
            chunks.append(self._create_chunk(
                content=current_chunk,
                document_id=document_id,
                chunk_index=chunk_index,
                metadata=metadata,
            ))

        logger.info(f"Documento {document_id} dividido em {len(chunks)} chunks")
        return chunks

    def _create_chunk(
        self,
        content: str,
        document_id: str,
        chunk_index: int,
        metadata: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Cria a estrutura de dados de um chunk individual.

        Args:
            content: Texto do chunk
            document_id: ID do documento original
            chunk_index: Índice sequencial do chunk
            metadata: Metadados do documento

        Returns:
            Dicionário com estrutura completa do chunk
        """
        return {
            "id": f"{document_id}_chunk_{chunk_index}",
            "content": content,
            "document_id": document_id,
            "chunk_index": chunk_index,
            "metadata": {
                **metadata,
                "document_id": document_id,
                "chunk_index": chunk_index,
                "char_count": len(content),
            },
        }

    def extract_text_from_content(
        self,
        content: bytes,
        file_type: str,
    ) -> str:
        """
        Extrai texto de diferentes tipos de arquivo.

        Suporta os seguintes formatos:
        - text/plain: Arquivos de texto simples (.txt)
        - application/pdf: Documentos PDF (requer pypdf)
        - application/vnd.openxmlformats...: Word DOCX (requer python-docx)

        Args:
            content: Conteúdo do arquivo em bytes
            file_type: Tipo MIME do arquivo (ex: "application/pdf")

        Returns:
            Texto extraído do arquivo

        Raises:
            Exception: Se houver erro na extração

        Note:
            Para PDF e DOCX, as bibliotecas correspondentes precisam
            estar instaladas (pypdf, python-docx).
        """
        try:
            if file_type == "text/plain":
                return content.decode("utf-8", errors="ignore")

            elif file_type == "application/pdf":
                return self._extract_from_pdf(content)

            elif file_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
                return self._extract_from_docx(content)

            else:
                # Tenta decodificar como texto
                return content.decode("utf-8", errors="ignore")

        except Exception as e:
            logger.error(f"Erro ao extrair texto: {e}")
            raise

    def _extract_from_pdf(self, content: bytes) -> str:
        """
        Extrai texto de arquivo PDF.

        Utiliza pypdf para ler cada página e extrair o texto.
        Páginas são separadas por duas quebras de linha.

        Args:
            content: Conteúdo do PDF em bytes

        Returns:
            Texto extraído de todas as páginas concatenadas
        """
        try:
            import io
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(content))
            text_parts = []

            for page in reader.pages:
                text = page.extract_text()
                if text:
                    text_parts.append(text)

            return "\n\n".join(text_parts)

        except ImportError:
            logger.warning("pypdf não instalado, retornando texto vazio")
            return ""
        except Exception as e:
            logger.error(f"Erro ao extrair PDF: {e}")
            return ""

    def _extract_from_docx(self, content: bytes) -> str:
        """
        Extrai texto de arquivo Word DOCX.

        Utiliza python-docx para ler cada parágrafo do documento.
        Parágrafos são separados por duas quebras de linha.

        Args:
            content: Conteúdo do DOCX em bytes

        Returns:
            Texto extraído de todos os parágrafos concatenados
        """
        try:
            import io
            from docx import Document

            doc = Document(io.BytesIO(content))
            text_parts = []

            for para in doc.paragraphs:
                if para.text.strip():
                    text_parts.append(para.text)

            return "\n\n".join(text_parts)

        except ImportError:
            logger.warning("python-docx não instalado, retornando texto vazio")
            return ""
        except Exception as e:
            logger.error(f"Erro ao extrair DOCX: {e}")
            return ""


# =============================================================================
# INSTÂNCIA GLOBAL
# =============================================================================
# Singleton para uso em toda a aplicação
# Import: from src.services import document_processor
document_processor = DocumentProcessor()
