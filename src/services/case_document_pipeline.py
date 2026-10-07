# -*- coding: utf-8 -*-
"""Pipeline pós-criação do caso para leitura robusta, indexação e retrieval.

Princípio de produto:
- durante o chat, anexos são apenas RECEBIDOS e preservados;
- depois que o caso é criado com sucesso, roda a auditoria pesada;
- leitura principal: LlamaParse (agentic_plus);
- fallback por página: PaddleOCR local;
- fallback final: Vision já existente na Zellu;
- resultado normalizado é indexado no Pinecone por caso;
- consultas futuras usam retrieval vetorial + rerank JEV.

Nenhum componente desta camada afirma autenticidade jurídica. Segurança/malware
continua sendo responsabilidade do preflight documental antes do parser.
"""

from __future__ import annotations

import asyncio
import io
import math
import os
import tempfile
import threading
from functools import lru_cache
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import httpx

from config import get_settings


@dataclass
class ParsedPage:
    page_number: int
    text: str
    confidence: Optional[float] = None
    method: str = "llamaparse"
    printed_page_number: Optional[str] = None
    warning: Optional[str] = None


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("\x00", " ").strip()


def _page_quality_ok(page: ParsedPage) -> bool:
    settings = get_settings()
    minimum_chars = int(getattr(settings, "DOCUMENT_PARSE_MIN_PAGE_CHARS", 40))
    minimum_confidence = float(getattr(settings, "DOCUMENT_PARSE_MIN_CONFIDENCE", 0.72))
    if len(page.text.strip()) < minimum_chars:
        return False
    if page.confidence is not None and page.confidence < minimum_confidence:
        return False
    return True


class LlamaParseClient:
    """Cliente REST mínimo para LlamaParse V2, sem trazer o framework inteiro."""

    def __init__(self) -> None:
        settings = get_settings()
        self.api_key = getattr(settings, "LLAMA_CLOUD_API_KEY", "") or os.getenv("LLAMA_CLOUD_API_KEY", "")
        self.base_url = getattr(settings, "LLAMAPARSE_BASE_URL", "https://api.cloud.llamaindex.ai").rstrip("/")
        self.tier = getattr(settings, "LLAMAPARSE_TIER", "agentic_plus")
        self.timeout_s = float(getattr(settings, "LLAMAPARSE_TIMEOUT_S", 180.0))
        self.poll_interval_s = float(getattr(settings, "LLAMAPARSE_POLL_INTERVAL_S", 1.5))

    @property
    def enabled(self) -> bool:
        return bool(self.api_key and getattr(get_settings(), "LLAMAPARSE_ENABLED", True))

    async def parse(self, content: bytes, filename: str, mime: str) -> Dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("llamaparse_not_configured")

        headers = {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}
        limits = httpx.Limits(max_keepalive_connections=5, max_connections=10)
        timeout = httpx.Timeout(self.timeout_s, connect=20.0)
        async with httpx.AsyncClient(timeout=timeout, limits=limits, follow_redirects=False) as client:
            upload = await client.post(
                f"{self.base_url}/api/v1/beta/files",
                headers=headers,
                files={"file": (filename, content, mime or "application/octet-stream")},
                data={"purpose": "parse"},
            )
            upload.raise_for_status()
            file_id = upload.json().get("id")
            if not file_id:
                raise RuntimeError("llamaparse_upload_without_file_id")

            payload: Dict[str, Any] = {
                "file_id": file_id,
                "tier": self.tier,
                "version": "latest",
                "confidence_score_effort": "high",
                "processing_options": {
                    "ocr_parameters": {"languages": ["pt", "en"]},
                },
                "output_options": {
                    "extract_printed_page_number": True,
                    "markdown": {"tables": {"merge_continued_tables": True}},
                },
            }
            create = await client.post(
                f"{self.base_url}/api/v2/parse",
                headers={**headers, "Content-Type": "application/json"},
                json=payload,
            )
            create.raise_for_status()
            job = create.json()
            job_id = (job.get("job") or job).get("id")
            if not job_id:
                raise RuntimeError("llamaparse_create_without_job_id")

            deadline = asyncio.get_running_loop().time() + self.timeout_s
            while True:
                result = await client.get(
                    f"{self.base_url}/api/v2/parse/{job_id}",
                    headers=headers,
                    params=[
                        ("expand", "markdown"),
                        ("expand", "text"),
                        ("expand", "metadata"),
                    ],
                )
                result.raise_for_status()
                data = result.json()
                job_obj = data.get("job") or data
                status = str(job_obj.get("status") or "").upper()
                if status == "COMPLETED":
                    return data
                if status in {"FAILED", "CANCELLED"}:
                    raise RuntimeError(f"llamaparse_{status.lower()}")
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError("llamaparse_timeout")
                await asyncio.sleep(self.poll_interval_s)


def _llamaparse_pages(data: Dict[str, Any]) -> List[ParsedPage]:
    markdown_pages = ((data.get("markdown") or {}).get("pages") or [])
    text_pages = ((data.get("text") or {}).get("pages") or [])
    metadata_pages = ((data.get("metadata") or {}).get("pages") or [])
    by_text = {int(p.get("page_number") or 0): p for p in text_pages}
    by_meta = {int(p.get("page_number") or 0): p for p in metadata_pages}
    pages: List[ParsedPage] = []

    for md in markdown_pages:
        number = int(md.get("page_number") or 0)
        if number <= 0:
            continue
        meta = by_meta.get(number, {})
        text = _clean_text(md.get("markdown")) or _clean_text(by_text.get(number, {}).get("text"))
        success = md.get("success", True)
        pages.append(
            ParsedPage(
                page_number=number,
                text=text if success else "",
                confidence=(float(meta["confidence"]) if meta.get("confidence") is not None else None),
                method="llamaparse",
                printed_page_number=(str(meta.get("printed_page_number")) if meta.get("printed_page_number") else None),
                warning=(None if success else _clean_text(md.get("error")) or "llamaparse_page_failed"),
            )
        )

    if not pages:
        for entry in text_pages:
            number = int(entry.get("page_number") or 0)
            if number <= 0:
                continue
            meta = by_meta.get(number, {})
            pages.append(
                ParsedPage(
                    page_number=number,
                    text=_clean_text(entry.get("text")),
                    confidence=(float(meta["confidence"]) if meta.get("confidence") is not None else None),
                    method="llamaparse",
                    printed_page_number=(str(meta.get("printed_page_number")) if meta.get("printed_page_number") else None),
                )
            )
    return pages


def _render_pdf_page(content: bytes, page_number: int) -> bytes:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(content)
    try:
        if page_number < 1 or page_number > len(pdf):
            raise ValueError("page_out_of_range")
        page = pdf[page_number - 1]
        bitmap = page.render(scale=3.0)
        image = bitmap.to_pil().convert("RGB")
        output = io.BytesIO()
        image.save(output, format="PNG", optimize=True)
        return output.getvalue()
    finally:
        pdf.close()


_PADDLE_LOCK = threading.Lock()


@lru_cache(maxsize=1)
def _paddle_engine():
    """Carrega o modelo OCR uma vez por processo; inicializacao e muito pesada."""
    try:
        from paddleocr import PaddleOCR
    except Exception as exc:  # pragma: no cover - depende do runtime pesado
        raise RuntimeError("paddleocr_not_available") from exc
    try:
        return PaddleOCR(
            lang="pt",
            use_doc_orientation_classify=True,
            use_doc_unwarping=True,
            use_textline_orientation=True,
        )
    except TypeError:
        return PaddleOCR(lang="pt", use_angle_cls=True)


def _paddle_extract_text_sync(image_bytes: bytes) -> str:
    """Aceita PaddleOCR 2.x/3.x sem recarregar o modelo a cada pagina."""
    try:
        import numpy as np
        from PIL import Image
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("paddleocr_runtime_not_available") from exc

    image = np.array(Image.open(io.BytesIO(image_bytes)).convert("RGB"))
    ocr = _paddle_engine()
    # Paddle nao documenta a mesma garantia de thread-safety entre releases.
    # O lock evita duas paginas concorrentes corromperem o estado do modelo.
    with _PADDLE_LOCK:
        try:
            result = ocr.predict(image)
        except Exception:
            result = ocr.ocr(image, cls=True)

    texts: List[str] = []

    def walk(value: Any) -> None:
        if value is None:
            return
        if isinstance(value, dict):
            for key in ("rec_texts", "texts"):
                items = value.get(key)
                if isinstance(items, list):
                    texts.extend(_clean_text(x) for x in items if _clean_text(x))
            for key in ("text", "rec_text"):
                if isinstance(value.get(key), str) and _clean_text(value[key]):
                    texts.append(_clean_text(value[key]))
            for child in value.values():
                if isinstance(child, (dict, list, tuple)):
                    walk(child)
            return
        if isinstance(value, (list, tuple)):
            # PaddleOCR 2.x: [box, (text, score)]
            if len(value) == 2 and isinstance(value[1], (list, tuple)) and value[1]:
                if isinstance(value[1][0], str):
                    candidate = _clean_text(value[1][0])
                    if candidate:
                        texts.append(candidate)
                    return
            for child in value:
                walk(child)
            return
        json_value = getattr(value, "json", None)
        if callable(json_value):
            try:
                walk(json_value())
            except Exception:
                pass
        elif isinstance(json_value, dict):
            walk(json_value)

    walk(result)
    # dedupe adjacente sem embaralhar ordem
    ordered: List[str] = []
    for text in texts:
        if not ordered or ordered[-1] != text:
            ordered.append(text)
    return "\n".join(ordered).strip()


async def _vision_page(image_bytes: bytes, filename: str, page_number: int) -> str:
    from src.utils import anexos

    settings = get_settings()
    if not getattr(settings, "NEGOTIATION_VISION_ENABLED", True):
        return ""
    timeout = float(getattr(settings, "DOCUMENT_VISION_TIMEOUT_S", 25.0))
    try:
        return await asyncio.wait_for(
            anexos._ler_imagem(image_bytes, "image/png", f"{filename}#pagina-{page_number}"),
            timeout=timeout,
        )
    except Exception:
        return ""


async def _fallback_page(content: bytes, filename: str, mime: str, page_number: int) -> ParsedPage:
    suffix = Path(filename).suffix.lower()
    image_bytes: Optional[bytes] = None
    if suffix == ".pdf" or mime.split(";", 1)[0].lower() == "application/pdf":
        try:
            image_bytes = await asyncio.to_thread(_render_pdf_page, content, page_number)
        except Exception:
            image_bytes = None
    elif mime.startswith("image/") or suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        image_bytes = content

    if image_bytes:
        if getattr(get_settings(), "PADDLEOCR_ENABLED", True):
            try:
                paddle_text = await asyncio.to_thread(_paddle_extract_text_sync, image_bytes)
                if len(paddle_text.strip()) >= int(getattr(get_settings(), "DOCUMENT_PARSE_MIN_PAGE_CHARS", 40)):
                    return ParsedPage(page_number, paddle_text, method="paddleocr")
            except Exception:
                pass
        vision_text = await _vision_page(image_bytes, filename, page_number)
        if vision_text.strip():
            return ParsedPage(page_number, vision_text.strip(), method="vision")

    return ParsedPage(page_number, "", method="failed", warning="all_page_readers_failed")


async def parse_document_robust(content: bytes, filename: str, mime: str) -> Dict[str, Any]:
    """Lê o documento com redundância e devolve páginas normalizadas."""
    pages: List[ParsedPage] = []
    errors: List[str] = []

    try:
        data = await LlamaParseClient().parse(content, filename, mime)
        pages = _llamaparse_pages(data)
    except Exception as exc:
        errors.append(type(exc).__name__ + ":" + str(exc)[:160])

    # Se o provedor principal não devolveu páginas, cria alvos de fallback.
    if not pages:
        suffix = Path(filename).suffix.lower()
        if suffix == ".pdf" or mime.split(";", 1)[0].lower() == "application/pdf":
            try:
                import pypdfium2 as pdfium
                pdf = pdfium.PdfDocument(content)
                try:
                    pages = [ParsedPage(i + 1, "", method="pending") for i in range(len(pdf))]
                finally:
                    pdf.close()
            except Exception:
                pages = [ParsedPage(1, "", method="pending")]
        else:
            pages = [ParsedPage(1, "", method="pending")]

    repaired: List[ParsedPage] = []
    for page in pages:
        if _page_quality_ok(page):
            repaired.append(page)
            continue
        fallback = await _fallback_page(content, filename, mime, page.page_number)
        if fallback.text.strip():
            # Mantém o melhor texto. Em conflito, fallback forte substitui a página ruim.
            repaired.append(fallback)
        else:
            repaired.append(page)

    # Última rede de segurança para formatos textuais. Para DOCX/TXT/MD, se a
    # leitura principal ficou vazia OU de baixa qualidade, compara com o extrator
    # textual local e mantém a versão mais completa. Isso evita que um parse curto
    # e aparentemente "válido" impeça a recuperação por uma segunda implementação.
    suffix_kind = Path(filename).suffix.lower().lstrip(".") or "txt"
    needs_textual_crosscheck = suffix_kind in {"docx", "doc", "txt", "md"} and (
        not any(p.text.strip() for p in repaired) or any(not _page_quality_ok(p) for p in repaired)
    )
    if needs_textual_crosscheck:
        try:
            from src.services.document_extractor import DocumentExtractor
            extractor = DocumentExtractor()
            legacy_text, _ = await asyncio.to_thread(extractor.extract, content, suffix_kind.upper())
            legacy_text = (legacy_text or "").strip()
            current_text = "\n\n".join(p.text.strip() for p in repaired if p.text.strip()).strip()
            if legacy_text and (not current_text or len(legacy_text) > len(current_text) * 1.10):
                repaired = [ParsedPage(1, legacy_text, method="legacy_text_crosscheck")]
        except Exception as exc:
            errors.append(type(exc).__name__ + ":" + str(exc)[:160])

    repaired.sort(key=lambda p: p.page_number)
    full_text = "\n\n".join(
        f"[Página {p.page_number}]\n{p.text.strip()}" for p in repaired if p.text.strip()
    ).strip()
    failed_pages = [p.page_number for p in repaired if not _page_quality_ok(p)]
    return {
        "text": full_text,
        "pages": [
            {
                "page_number": p.page_number,
                "printed_page_number": p.printed_page_number,
                "text": p.text,
                "confidence": p.confidence,
                "method": p.method,
                "warning": p.warning,
                "quality_ok": _page_quality_ok(p),
            }
            for p in repaired
        ],
        "status": "read" if full_text and not failed_pages else "partial" if full_text else "unread",
        "failed_pages": failed_pages,
        "methods": sorted({p.method for p in repaired}),
        "errors": errors,
    }


def _chunk_page(text: str, max_chars: int, overlap: int) -> Iterable[str]:
    value = text.strip()
    if not value:
        return []
    if len(value) <= max_chars:
        return [value]
    chunks: List[str] = []
    start = 0
    while start < len(value):
        end = min(start + max_chars, len(value))
        # tenta fechar em quebra de linha/frase para não serrar contexto no meio
        if end < len(value):
            candidates = [value.rfind("\n", start, end), value.rfind(". ", start, end)]
            boundary = max(candidates)
            if boundary > start + max_chars // 2:
                end = boundary + 1
        chunk = value[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(value):
            break
        start = max(end - overlap, start + 1)
    return chunks


def case_namespace(case_key: str) -> str:
    return f"case-docs-{case_key[:48]}"


async def index_case_documents(case_key: str, records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Indexa páginas/chunks já auditados. Idempotente pelo id do vetor."""
    settings = get_settings()
    if not getattr(settings, "CASE_DOCUMENT_INDEX_ENABLED", True):
        return {"status": "disabled", "vectors": 0}
    if not settings.PINECONE_API_KEY:
        return {"status": "skipped", "reason": "pinecone_not_configured", "vectors": 0}

    from src.services.embedding_service import embedding_service
    from src.services.pinecone_service import pinecone_service

    chunk_size = int(getattr(settings, "CASE_DOCUMENT_CHUNK_SIZE", 1800))
    overlap = int(getattr(settings, "CASE_DOCUMENT_CHUNK_OVERLAP", 250))
    texts: List[str] = []
    metas: List[Dict[str, Any]] = []
    ids: List[str] = []

    for record in records:
        document_id = record.get("sha256") or record.get("id")
        audit = record.get("audit") or {}
        pipeline = record.get("postCasePipeline") or {}
        pages = pipeline.get("pages") or []
        if not pages and record.get("text"):
            import re
            raw_text = str(record.get("text") or "")
            matches = list(re.finditer(r"(?m)^\[Página (\d+)\]\s*$", raw_text))
            if matches:
                pages = []
                for pos, match in enumerate(matches):
                    start = match.end()
                    end = matches[pos + 1].start() if pos + 1 < len(matches) else len(raw_text)
                    pages.append({
                        "page_number": int(match.group(1)),
                        "text": raw_text[start:end].strip(),
                        "method": "normalized_post_case",
                    })
            else:
                pages = [{"page_number": 1, "text": raw_text, "method": "stored"}]
        for page in pages:
            page_number = int(page.get("page_number") or 1)
            page_text = _clean_text(page.get("text"))
            for chunk_index, chunk in enumerate(_chunk_page(page_text, chunk_size, overlap)):
                vector_id = f"{document_id}:p{page_number}:c{chunk_index}"
                ids.append(vector_id[:500])
                texts.append(chunk)
                metas.append({
                    "case_key": case_key,
                    "document_id": str(document_id),
                    "evidence_id": str(record.get("id") or ""),
                    "filename": str(record.get("name") or "arquivo")[:300],
                    "kind": str(record.get("kind") or "desconhecido"),
                    "page": page_number,
                    "printed_page": str(page.get("printed_page_number") or "")[:50],
                    "read_method": str(page.get("method") or "")[:50],
                    "audit_status": str(audit.get("auditStatus") or "UNVERIFIED")[:50],
                    "text": chunk,
                })

    if not texts:
        return {"status": "empty", "vectors": 0}

    vectors = await asyncio.to_thread(embedding_service.generate_embeddings_batch, texts)
    payload = [
        {"id": ids[i], "values": vectors[i], "metadata": metas[i]}
        for i in range(len(ids))
    ]
    count = await asyncio.to_thread(
        pinecone_service.upsert_vectors,
        payload,
        case_namespace(case_key),
    )
    return {"status": "indexed", "vectors": count, "namespace": case_namespace(case_key)}


def _probability_margin(probabilities: Dict[str, float]) -> float:
    values = sorted((float(v) for v in probabilities.values()), reverse=True)
    if not values:
        return 0.0
    return values[0] if len(values) == 1 else values[0] - values[1]


async def _jev_rerank(query: str, candidates: List[Dict[str, Any]], final_k: int) -> Dict[str, Any]:
    settings = get_settings()
    if not settings.JEV_API_KEY:
        raise RuntimeError("jev_not_configured")
    criteria = {
        f"C{i}": (
            f"Arquivo: {c['metadata'].get('filename', '')}\n"
            f"Página: {c['metadata'].get('page', '')}\n"
            f"Trecho: {c['metadata'].get('text', '')}"
        )
        for i, c in enumerate(candidates)
    }
    payload = {
        "model": settings.JEV_MODEL,
        "state": {"query": query},
        "questions": {
            "best_chunks": {
                "type": "choice",
                "instructions": (
                    "Selecione o trecho que melhor responde à consulta usando somente os candidatos fornecidos. "
                    "Priorize aderência factual ao documento, página e contexto, não mera similaridade lexical."
                ),
                "criteria": criteria,
            }
        },
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            settings.JEV_API_URL,
            headers={"Authorization": f"Bearer {settings.JEV_API_KEY}", "Content-Type": "application/json"},
            json=payload,
        )
        response.raise_for_status()
        answer = response.json()["answers"]["best_chunks"]
    choice = str(answer.get("choice"))
    probabilities = {str(k): float(v) for k, v in (answer.get("probabilities") or {}).items()}
    if choice not in criteria:
        raise RuntimeError("jev_choice_outside_candidates")
    confidence = float(answer.get("confidence") or 0.0)
    margin = _probability_margin(probabilities)
    if not all(math.isfinite(v) and 0 <= v <= 1 for v in (confidence, margin)):
        raise RuntimeError("jev_invalid_metrics")

    # Ordena pelos próprios scores quando disponíveis e força o escolhido na frente.
    ranking = sorted(
        range(len(candidates)),
        key=lambda i: probabilities.get(f"C{i}", 0.0),
        reverse=True,
    )
    chosen_idx = int(choice[1:])
    ranking = [chosen_idx] + [i for i in ranking if i != chosen_idx]
    return {
        "items": [candidates[i] for i in ranking[:max(1, final_k)]],
        "confidence": confidence,
        "margin": margin,
    }


async def retrieve_case_context(case_key: str, query: str, *, top_k: int = 10, final_k: int = 3) -> Dict[str, Any]:
    """Busca por índice e usa JEV para reduzir o contexto entregue ao agente."""
    settings = get_settings()
    from src.services.embedding_service import embedding_service
    from src.services.pinecone_service import pinecone_service

    vector = await asyncio.to_thread(embedding_service.generate_embedding, query)
    candidates = await asyncio.to_thread(
        pinecone_service.query,
        vector,
        case_namespace(case_key),
        top_k,
        None,
    )
    if not candidates:
        return {"items": [], "source": "EMPTY", "confidence": 0.0, "margin": 0.0}

    if not settings.JEV_API_KEY:
        return {"items": candidates[:final_k], "source": "VECTOR_ONLY", "confidence": 0.0, "margin": 0.0}

    try:
        reranked = await _jev_rerank(query, candidates, final_k)
        source = "JEV"
        if (
            reranked["confidence"] < settings.JEV_CONFIDENCE_THRESHOLD
            or reranked["margin"] < settings.JEV_MARGIN_THRESHOLD
        ):
            # Não inventa outro candidato. Em baixa certeza, entrega mais contexto vetorial
            # para o modelo forte decidir usando os mesmos candidatos recuperados.
            return {
                "items": candidates[:max(final_k, min(top_k, 6))],
                "source": "JEV_LOW_CONFIDENCE_EXPANDED",
                "confidence": reranked["confidence"],
                "margin": reranked["margin"],
            }
        return {**reranked, "source": source}
    except Exception as exc:
        return {
            "items": candidates[:final_k],
            "source": "JEV_FAILED_VECTOR_FALLBACK",
            "confidence": 0.0,
            "margin": 0.0,
            "error": type(exc).__name__,
        }
