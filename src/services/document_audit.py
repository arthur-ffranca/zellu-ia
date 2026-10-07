# -*- coding: utf-8 -*-
"""Auditoria documental defensiva para anexos de casos.

Esta camada NAO decide validade juridica e NAO substitui verificacao oficial.
Ela faz quatro coisas antes/depois da leitura do documento:

1. preflight de seguranca: tipo real, MIME, malware, PDF/DOCX ativo;
2. extracao deterministica de identificadores e campos auditaveis;
3. classificacao e sinais de coerencia/inconsistencia;
4. cruzamento entre documentos do mesmo caso.

Regra de produto: ``CONSISTENT`` significa apenas que nao foram encontrados
conflitos relevantes nas verificacoes executadas. Autenticidade externa fica
``UNVERIFIED`` ate existir um verificador oficial/criptografico apropriado.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import re
import socket
import struct
import unicodedata
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from config import get_settings


# Nomes que, por si so, indicam conteudo ativo. /OpenAction e /AA NAO entram:
# sao apenas gatilhos (o Word/Acrobat usam /OpenAction para "abrir na pagina 1")
# e o perigo real e a acao que disparam, detectada pelo /S abaixo ou pelo /JS.
_PDF_MARKERS = {
    b"/JavaScript": "PDF_JAVASCRIPT",
    b"/JS": "PDF_JAVASCRIPT",
    b"/Launch": "PDF_LAUNCH_ACTION",
    b"/EmbeddedFile": "PDF_EMBEDDED_FILE",
    b"/EmbeddedFiles": "PDF_EMBEDDED_FILE",
}
_PDF_DANGEROUS_ACTIONS = {"/JavaScript": "PDF_JAVASCRIPT", "/Launch": "PDF_LAUNCH_ACTION"}
# Fim de token de nome em PDF: espaco em branco ou delimitador (ISO 32000, 7.2.2).
# Sem isso, "/AA" casava com a fonte subset "/AAAAAA+Calibri" e "/JS" com "/JSxyz".
_PDF_NAME_END = rb"(?=[\s()<>\[\]{}/%]|$)"
_PDF_RAW_MARKER_RE = re.compile(
    rb"(/JavaScript|/JS|/Launch|/EmbeddedFiles?)" + _PDF_NAME_END
)

_SIGNATURES: Tuple[Tuple[bytes, str, str], ...] = (
    (b"%PDF", "pdf", "application/pdf"),
    (b"PK\x03\x04", "zip", "application/zip"),
    (b"\xff\xd8\xff", "jpg", "image/jpeg"),
    (b"\x89PNG", "png", "image/png"),
    (b"GIF8", "gif", "image/gif"),
)

_MIME_BY_KIND = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "txt": "text/plain",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
}

# CPF/CNPJ so contam quando formatados ou rotulados. Onze digitos soltos sao
# frequentemente telefone com DDD, protocolo ou trecho de codigo de barras, e
# checar digito verificador neles gerava "CPF invalido" em documento legitimo.
_CPF_RE = re.compile(r"(?<![\d.])(\d{3}\.\d{3}\.\d{3}-\d{2})(?![\d-])")
_CNPJ_RE = re.compile(r"(?<![\d.])(\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2})(?![\d-])")
_CPF_LABELED_RE = re.compile(
    r"\bCPF\b[^0-9\n]{0,15}(\d{3}[.\s]?\d{3}[.\s]?\d{3}[-\s.]?\d{2})(?!\d)", re.I
)
_CNPJ_LABELED_RE = re.compile(
    r"\bCNPJ\b[^0-9\n]{0,15}(\d{2}[.\s]?\d{3}[.\s]?\d{3}[/\s]?\d{4}[-\s.]?\d{2})(?!\d)", re.I
)
_MONEY_RE = re.compile(r"R\$\s*([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{2})|[0-9]+(?:,[0-9]{2})?)", re.I)
_DATE_RE = re.compile(r"(?<!\d)(\d{2}[/-]\d{2}[/-]\d{4})(?!\d)")
_NFE_KEY_RE = re.compile(r"(?<!\d)(\d{44})(?!\d)")
# Chave impressa no DANFE em blocos de 4 digitos.
_NFE_KEY_SPACED_RE = re.compile(r"(?<!\d)((?:\d{4}[ .]){10}\d{4})(?!\d)")
# PIX end-to-end id (BCB): "E" + ISPB do pagador (8) + AAAAMMDDHHMM em UTC (12)
# + 11 alfanumericos. Identifica a transacao no SPI; o recebedor confere no extrato.
_PIX_E2E_RE = re.compile(r"\b(E\d{8}\d{12}[A-Za-z0-9]{11})\b")
_NFSE_VERIFY_RE = re.compile(r"(?:codigo|c[oó]digo)\s+(?:de\s+)?verifica(?:cao|ção)\s*[:\-]?\s*([A-Z0-9-]{5,32})", re.I)
_REGISTRY_RE = re.compile(
    r"\b(CRM|CREA|CRO|CRP|CRMV|CAU|OAB)\s*(?:[/\-]\s*([A-Z]{2}))?\s*[:nº°.#-]*\s*([0-9.\-]{3,20})\b",
    re.I,
)

# Ferramentas de edicao/conversao que nao emitem comprovante, nota ou CNH. Um
# documento "do banco" ou "da prefeitura" produzido por elas foi, no minimo,
# reprocessado depois de emitido. Nao prova fraude; pede revisao humana.
_EDITOR_TOOLS = (
    "ilovepdf", "smallpdf", "sejda", "pdfescape", "pdffiller", "pdf-xchange editor",
    "foxit phantompdf", "foxit pdf editor", "nitro pro", "pdfelement", "docfly",
    "photoshop", "gimp", "canva", "pixlr", "photopea", "picsart", "snapseed",
)
# Tipos que deveriam sair do sistema emissor sem retoque.
_ISSUER_GENERATED = {"comprovante_pagamento", "nota_fiscal", "nota_fiscal_servico", "cnh"}
_PDF_DATE_RE = re.compile(r"D?:?(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?")

_scan_semaphore: Optional[asyncio.Semaphore] = None

def security_blocked(audit: Dict[str, Any]) -> bool:
    blocking = {
        "MALWARE_DETECTED", "MALWARE_SCAN_REQUIRED", "PDF_JAVASCRIPT",
        "PDF_OPEN_ACTION", "PDF_LAUNCH_ACTION", "PDF_EMBEDDED_FILE",
        "PDF_ADDITIONAL_ACTION", "PDF_INSPECTION_INCOMPLETE",
        "DOCX_MACRO_PRESENT", "DOCX_EXTERNAL_RELATIONSHIP", "DOCX_PATH_TRAVERSAL",
        "DOCX_EXPANSION_LIMIT_EXCEEDED", "DOCX_PARSE_ERROR", "DOCX_EMBEDDED_OBJECT",
    }
    return audit.get("status") == "MALICIOUS" or bool(blocking.intersection(audit.get("securityFlags") or []))


def _scan_gate() -> asyncio.Semaphore:
    global _scan_semaphore
    limit = max(int(getattr(get_settings(), "DOCUMENT_AUDIT_MAX_CONCURRENCY", 2)), 1)
    if _scan_semaphore is None:
        _scan_semaphore = asyncio.Semaphore(limit)
    return _scan_semaphore


def _norm(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    return "".join(ch for ch in normalized if not unicodedata.combining(ch)).lower()


def _digits(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def _unique(values: Iterable[Any]) -> List[Any]:
    out = []
    seen = set()
    for value in values:
        marker = repr(value)
        if marker in seen:
            continue
        seen.add(marker)
        out.append(value)
    return out


def validate_cpf(value: str) -> bool:
    number = _digits(value)
    if len(number) != 11 or len(set(number)) == 1:
        return False
    for pos in (9, 10):
        weight = pos + 1
        total = sum(int(number[i]) * (weight - i) for i in range(pos))
        digit = (total * 10 % 11) % 10
        if digit != int(number[pos]):
            return False
    return True


def validate_cnpj(value: str) -> bool:
    number = _digits(value)
    if len(number) != 14 or len(set(number)) == 1:
        return False
    nums = [int(ch) for ch in number]
    weights1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    rest = sum(a * b for a, b in zip(nums[:12], weights1)) % 11
    d1 = 0 if rest < 2 else 11 - rest
    weights2 = [6] + weights1
    rest = sum(a * b for a, b in zip(nums[:13], weights2)) % 11
    d2 = 0 if rest < 2 else 11 - rest
    return nums[12] == d1 and nums[13] == d2


def validate_nfe_key(value: str) -> bool:
    key = _digits(value)
    if len(key) != 44:
        return False
    body = [int(ch) for ch in key[:43]]
    weights = []
    weight = 2
    for _ in range(43):
        weights.append(weight)
        weight += 1
        if weight > 9:
            weight = 2
    total = sum(n * w for n, w in zip(reversed(body), weights))
    rest = total % 11
    check = 0 if rest in (0, 1) else 11 - rest
    return check == int(key[43])


def parse_nfe_key(value: str) -> Dict[str, Any]:
    """Decompoe a chave de acesso NF-e/NFC-e (leiaute 4.00, 44 digitos)."""
    key = _digits(value)
    return {
        "uf": key[0:2], "yearMonth": (2000 + int(key[2:4]), int(key[4:6])),
        "issuerId": key[6:20], "model": key[20:22], "series": key[22:25],
        "number": int(key[25:34]),
    }


def parse_pix_e2e(value: str) -> Dict[str, Any]:
    """ISPB do pagador e instante (UTC) codificados no end-to-end id do PIX."""
    from datetime import datetime

    entry: Dict[str, Any] = {"value": value, "ispb": value[1:9], "formatValid": False}
    try:
        moment = datetime.strptime(value[9:21], "%Y%m%d%H%M")
        entry.update(formatValid=True, utc=moment.isoformat(timespec="minutes"))
    except ValueError:
        pass
    return entry


def _parse_br_date(raw: str):
    from datetime import date

    try:
        day, month, year = (int(part) for part in re.split(r"[/-]", raw))
        return date(year, month, day)
    except ValueError:
        return None


def structural_findings(fields: Dict[str, Any]) -> List[str]:
    """Cruza o que esta codificado nos identificadores com o resto do documento.

    Chave NF-e carrega emitente, mes de emissao e numero; o E2E do PIX carrega
    o instante da transacao. Um documento editado costuma trocar o campo visivel
    e esquecer o codificado.
    """
    from datetime import datetime, timedelta

    findings: List[str] = []
    dates = [d for d in (_parse_br_date(raw) for raw in fields.get("dates") or []) if d]
    cnpjs = {item["value"] for item in fields.get("cnpjs") or []}
    for item in fields.get("nfeKeys") or []:
        if not item.get("checksumValid"):
            continue
        key = parse_nfe_key(item["value"])
        issuer = key["issuerId"]
        # Produtor rural emite com CPF no campo do CNPJ (zeros a esquerda).
        if cnpjs and not issuer.startswith("000") and issuer not in cnpjs:
            findings.append("NFE_KEY_ISSUER_MISMATCH")
        if dates and not any((d.year, d.month) == key["yearMonth"] for d in dates):
            findings.append("NFE_KEY_DATE_MISMATCH")
        number = str(fields.get("documentNumber") or "").strip()
        if number.isdigit() and int(number) != key["number"]:
            findings.append("NFE_KEY_NUMBER_MISMATCH")
    for item in fields.get("pixEndToEndIds") or []:
        if not item.get("formatValid"):
            findings.append("PIX_E2E_INVALID_FORMAT")
            continue
        # E2E em UTC e comprovante em horario de Brasilia: tolera um dia.
        moment = datetime.fromisoformat(item["utc"]).date()
        if dates and not any(abs(d - moment) <= timedelta(days=1) for d in dates):
            findings.append("PIX_E2E_DATE_MISMATCH")
    return _unique(findings)


def external_checks(fields: Dict[str, Any]) -> List[Dict[str, str]]:
    """O que uma pessoa (empresa/mediador) pode conferir fora daqui."""
    checks: List[Dict[str, str]] = []
    for item in fields.get("pixEndToEndIds") or []:
        if item.get("formatValid"):
            checks.append({
                "type": "pix_e2e", "reference": item["value"],
                "how": "Recebedor confirma no extrato/internet banking a transação com este E2E e o valor declarado.",
            })
    for item in fields.get("nfeKeys") or []:
        if item.get("checksumValid"):
            checks.append({
                "type": "nfe_key", "reference": item["value"],
                "how": "Consultar a chave de acesso no Portal da NF-e (SEFAZ) e conferir emitente, valor e situação.",
            })
    if fields.get("verificationCode"):
        checks.append({
            "type": "nfse_verification_code", "reference": fields["verificationCode"],
            "how": "Consultar o código de verificação no portal de NFS-e do município emissor (ou no portal nacional).",
        })
    return checks


def _money_to_float(raw: str) -> Optional[float]:
    try:
        return float(raw.replace(".", "").replace(",", "."))
    except (TypeError, ValueError):
        return None


def sniff_file_type(content: bytes, filename: str = "", declared_mime: str = "") -> Dict[str, Any]:
    declared = (declared_mime or "").split(";", 1)[0].strip().lower()
    extension = Path(filename or "").suffix.lower().lstrip(".")
    kind = ""
    mime = "application/octet-stream"
    for signature, candidate, candidate_mime in _SIGNATURES:
        if content.startswith(signature):
            kind, mime = candidate, candidate_mime
            break
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        kind, mime = "webp", "image/webp"
    elif content.startswith(b"RIFF") and content[8:12] == b"WAVE":
        kind, mime = "wav", "audio/wav"

    if kind == "zip":
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                names = set(archive.namelist())
            if "[Content_Types].xml" in names and "word/document.xml" in names:
                kind = "docx"
                mime = _MIME_BY_KIND["docx"]
        except Exception:
            pass

    if not kind and content:
        # TXT conservador: nao chamar binario arbitrario de texto.
        try:
            sample = content[:4096].decode("utf-8")
            printable = sum(ch.isprintable() or ch in "\r\n\t" for ch in sample)
            if sample and printable / len(sample) > 0.95:
                kind, mime = "txt", "text/plain"
        except UnicodeDecodeError:
            pass

    extension_normalized = "jpg" if extension == "jpeg" else extension
    actual_normalized = "jpg" if kind == "jpeg" else kind
    mismatch = bool(extension_normalized and actual_normalized and extension_normalized != actual_normalized)
    mime_mismatch = bool(declared and mime != "application/octet-stream" and declared != mime)
    # Alguns storages devolvem octet-stream legitimamente.
    if declared in {"", "application/octet-stream", "binary/octet-stream"}:
        mime_mismatch = False

    return {
        "kind": kind or "unknown",
        "mime": mime,
        "declaredMime": declared or None,
        "extension": extension or None,
        "extensionMismatch": mismatch,
        "mimeMismatch": mime_mismatch,
    }


def _clamd_scan_sync(content: bytes) -> Dict[str, Any]:
    settings = get_settings()
    host = getattr(settings, "CLAMAV_HOST", "clamav")
    port = int(getattr(settings, "CLAMAV_PORT", 3310))
    timeout = float(getattr(settings, "CLAMAV_TIMEOUT_S", 8.0))
    chunk_size = 64 * 1024
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(b"zINSTREAM\0")
            for offset in range(0, len(content), chunk_size):
                chunk = content[offset: offset + chunk_size]
                sock.sendall(struct.pack("!I", len(chunk)))
                sock.sendall(chunk)
            sock.sendall(struct.pack("!I", 0))
            response = bytearray()
            while True:
                part = sock.recv(4096)
                if not part:
                    break
                response.extend(part)
                if b"\0" in part or b"\n" in part:
                    break
        message = bytes(response).decode("utf-8", errors="replace").strip("\x00\r\n ")
        if " FOUND" in message:
            threat = message.rsplit(":", 1)[-1].replace("FOUND", "").strip()
            return {"status": "MALICIOUS", "engine": "clamd", "threat": threat or "detected"}
        if message.endswith("OK"):
            return {"status": "PASS", "engine": "clamd"}
        return {"status": "UNAVAILABLE", "engine": "clamd", "reason": "unexpected_response"}
    except (OSError, socket.timeout):
        return {"status": "UNAVAILABLE", "engine": "clamd", "reason": "scanner_unavailable"}


async def malware_scan(content: bytes) -> Dict[str, Any]:
    settings = get_settings()
    if not getattr(settings, "DOCUMENT_MALWARE_SCAN_ENABLED", True):
        return {"status": "DISABLED", "engine": "clamd"}
    async with _scan_gate():
        try:
            timeout = float(getattr(settings, "CLAMAV_TIMEOUT_S", 8.0)) + 1.0
            return await asyncio.wait_for(asyncio.to_thread(_clamd_scan_sync, content), timeout=timeout)
        except asyncio.TimeoutError:
            return {"status": "UNAVAILABLE", "engine": "clamd", "reason": "scan_timeout"}


def _inspect_pdf(content: bytes) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "pages": None,
        "encrypted": None,
        "digitalSignaturePresent": bool(b"/ByteRange" in content and b"/Contents" in content),
        "activeContent": [],
        "metadata": {},
    }
    for match in _PDF_RAW_MARKER_RE.finditer(content):
        result["activeContent"].append(_PDF_MARKERS[match.group(1)])
    try:
        import PyPDF2

        reader = PyPDF2.PdfReader(io.BytesIO(content))
        result["encrypted"] = bool(reader.is_encrypted)
        if reader.is_encrypted:
            # Extrato/comprovante de banco costuma vir so com senha de proprietario
            # (restricao de impressao/copia): abre com senha de usuario vazia.
            try:
                result["passwordRequired"] = not reader.decrypt("")
            except Exception:
                result["passwordRequired"] = True
            if result["passwordRequired"]:
                result["activeContent"] = _unique(result["activeContent"])
                return result
        result["pages"] = len(reader.pages)
        # Inspeciona objetos resolvidos: marcadores podem estar comprimidos ou
        # escapados e nao aparecer literalmente nos bytes do arquivo.
        visited = set()
        settings = get_settings()
        remaining = [max(int(getattr(settings, "DOCUMENT_PDF_MAX_OBJECTS", 200_000)), 1)]
        def inspect_object(obj, depth=0):
            if depth > 64:
                raise ValueError("PDF inspection limit")
            identity = (getattr(obj, "idnum", None), getattr(obj, "generation", None))
            if identity[0] is not None:
                if identity in visited:
                    return
                visited.add(identity)
                obj = obj.get_object()
            if not isinstance(obj, (dict, list, tuple)):
                return
            # Conta so containers: numeros e nomes nao custam nada e, contados,
            # faziam um extrato de 40 paginas estourar o limite.
            remaining[0] -= 1
            if remaining[0] <= 0:
                raise ValueError("PDF inspection limit")
            if isinstance(obj, dict):
                for key, value in obj.items():
                    key = str(key)
                    if key == "/Parent":
                        continue  # aponta para cima na arvore; ja visitado
                    marker = _PDF_MARKERS.get(key.encode("ascii", errors="ignore"))
                    if marker:
                        result["activeContent"].append(marker)
                    if key == "/S" and str(value) in _PDF_DANGEROUS_ACTIONS:
                        result["activeContent"].append(_PDF_DANGEROUS_ACTIONS[str(value)])
                    inspect_object(value, depth + 1)
            else:
                for value in obj:
                    inspect_object(value, depth + 1)
        inspect_object(reader.trailer)
        result["activeContent"] = _unique(result["activeContent"])
        metadata = reader.metadata or {}
        for key in ("/Title", "/Author", "/Creator", "/Producer", "/CreationDate", "/ModDate"):
            value = metadata.get(key)
            if value is not None:
                result["metadata"][key.lstrip("/")] = str(value)[:300]
    except Exception as exc:
        result["parseError"] = type(exc).__name__
    return result


# Alteracoes posteriores a assinatura que o proprio padrao permite (preencher
# formulario, anexar carimbo de tempo/LTV, assinar de novo). Qualquer outra coisa
# numa revisao posterior e alteracao do documento depois de assinado.
_ALLOWED_POST_SIGNATURE = {"NONE", "LTA_UPDATES", "FORM_FILLING"}


def _load_trust_roots(directory: str) -> List[Any]:
    """Certificados raiz confiaveis (ex.: ACs-Raiz ICP-Brasil) em PEM ou DER."""
    from asn1crypto import pem, x509 as asn1_x509

    roots: List[Any] = []
    folder = Path(directory or "")
    if not directory or not folder.is_dir():
        return roots
    for path in sorted(folder.iterdir()):
        if path.suffix.lower() not in {".crt", ".cer", ".pem", ".der"}:
            continue
        try:
            raw = path.read_bytes()
            blobs = [der for _, _, der in pem.unarmor(raw, multiple=True)] if pem.detect(raw) else [raw]
            roots.extend(asn1_x509.Certificate.load(blob) for blob in blobs)
        except Exception:
            continue
    return roots


def verify_pdf_signatures(content: bytes) -> Dict[str, Any]:
    """Verifica criptograficamente as assinaturas digitais embutidas no PDF.

    Integridade (o arquivo nao mudou depois de assinado) independe de cadeia de
    confianca. Autoria so e "confiavel" se a cadeia fechar numa raiz do
    diretorio DOCUMENT_SIGNATURE_TRUST_DIR (ex.: ACs-Raiz ICP-Brasil).
    Revogacao (CRL/OCSP) so e consultada com DOCUMENT_SIGNATURE_ALLOW_FETCHING.
    """
    import logging

    from pyhanko.pdf_utils.reader import PdfFileReader
    from pyhanko.sign.validation import validate_pdf_signature
    from pyhanko_certvalidator import ValidationContext

    settings = get_settings()
    # Cadeia nao confiavel e resultado esperado, nao erro: silencia o log do validador.
    logging.getLogger("pyhanko_certvalidator").setLevel(logging.CRITICAL)
    logging.getLogger("pyhanko").setLevel(logging.CRITICAL)
    roots = _load_trust_roots(str(getattr(settings, "DOCUMENT_SIGNATURE_TRUST_DIR", "") or ""))
    context_kwargs = {
        "trust_roots": roots,
        "allow_fetching": bool(getattr(settings, "DOCUMENT_SIGNATURE_ALLOW_FETCHING", False)),
    }
    result: Dict[str, Any] = {"trustRootsLoaded": len(roots), "signatures": []}
    try:
        reader = PdfFileReader(io.BytesIO(content), strict=False)
        if reader.encrypted:
            reader.decrypt("")
        for embedded in reader.embedded_signatures[:10]:
            entry: Dict[str, Any] = {"field": embedded.field_name}
            try:
                status = validate_pdf_signature(embedded, ValidationContext(**context_kwargs))
                modification = getattr(status.modification_level, "name", None)
                entry.update(
                    intact=bool(status.intact),
                    valid=bool(status.valid),
                    trusted=bool(status.trusted),
                    coverage=getattr(status.coverage, "name", None),
                    modificationLevel=modification,
                    modifiedAfterSigning=bool(modification and modification not in _ALLOWED_POST_SIGNATURE),
                    signer=status.signing_cert.subject.human_friendly[:200] if status.signing_cert else None,
                    signingTime=status.signer_reported_dt.isoformat() if status.signer_reported_dt else None,
                )
            except Exception as exc:
                entry["error"] = type(exc).__name__
            result["signatures"].append(entry)
    except Exception as exc:
        result["error"] = type(exc).__name__
    return result


def signature_findings(audit: Dict[str, Any]) -> Tuple[List[str], Optional[Dict[str, Any]]]:
    """Achados da verificacao de assinatura e a assinatura que prova autenticidade."""
    findings: List[str] = []
    verified = None
    for entry in ((audit.get("pdf") or {}).get("signatureVerification") or {}).get("signatures") or []:
        if entry.get("error"):
            findings.append("DIGITAL_SIGNATURE_UNVERIFIABLE")
        elif not entry.get("intact") or not entry.get("valid"):
            findings.append("DIGITAL_SIGNATURE_BROKEN")
        elif entry.get("modifiedAfterSigning"):
            findings.append("MODIFIED_AFTER_SIGNATURE")
        elif entry.get("trusted"):
            verified = verified or entry
        else:
            findings.append("DIGITAL_SIGNATURE_UNTRUSTED_CHAIN")
    return _unique(findings), verified


def _inspect_image(content: bytes) -> Dict[str, Any]:
    """Software de edicao gravado no EXIF (tag 0x0131), quando existir."""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(content)) as image:
            software = image.getexif().get(0x0131)
        return {"software": str(software)[:200]} if software else {}
    except Exception as exc:
        return {"parseError": type(exc).__name__}


def _pdf_date(value: Any) -> Optional[Tuple[int, ...]]:
    match = _PDF_DATE_RE.match(str(value or "").strip())
    if not match:
        return None
    parts = [int(part) if part else default for part, default in zip(match.groups(), (0, 1, 1, 0, 0, 0))]
    return tuple(parts)


def editing_findings(audit: Dict[str, Any]) -> List[str]:
    """Sinais de reprocessamento a partir de metadados ja coletados."""
    findings: List[str] = []
    metadata = (audit.get("pdf") or {}).get("metadata") or {}
    tools = " ".join(
        str(value) for value in (
            metadata.get("Producer"), metadata.get("Creator"), (audit.get("image") or {}).get("software"),
        ) if value
    ).lower()
    if any(tool in tools for tool in _EDITOR_TOOLS):
        findings.append("PRODUCED_BY_EDITING_TOOL")
    created, modified = _pdf_date(metadata.get("CreationDate")), _pdf_date(metadata.get("ModDate"))
    if created and modified and modified[:3] != created[:3] and modified > created:
        findings.append("PDF_MODIFIED_AFTER_CREATION")
    return findings


def _inspect_docx(content: bytes) -> Dict[str, Any]:
    result = {"macroPresent": False, "embeddedObjects": False, "externalRelationships": False,
              "pathTraversal": False, "expandedBytes": 0, "entries": 0}
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            infos = archive.infolist()
            result["entries"] = len(infos)
            result["expandedBytes"] = sum(i.file_size for i in infos)
            limit = int(getattr(get_settings(), "DOCUMENT_MAX_EXPANDED_BYTES", 150 * 1024 * 1024))
            if result["expandedBytes"] > limit or len(infos) > 10000:
                result["expansionLimitExceeded"] = True
                return result
            names = [i.filename for i in infos]
            result["pathTraversal"] = any(
                name.startswith(("/", "\\")) or ".." in name.replace("\\", "/").split("/") for name in names
            )
            lowered = [name.lower() for name in names]
            result["macroPresent"] = any(name.endswith("vbaproject.bin") for name in lowered)
            result["embeddedObjects"] = any("/embeddings/" in name for name in lowered)
            for name in names:
                if not name.lower().endswith(".rels"):
                    continue
                try:
                    if archive.getinfo(name).file_size > 1024 * 1024:
                        result["expansionLimitExceeded"] = True
                        continue
                    raw = archive.read(name)
                    root = ET.fromstring(raw)
                except Exception:
                    result["relationshipParseError"] = True
                    continue
                for relationship in root:
                    if relationship.get("TargetMode", "").lower() != "external":
                        continue
                    target = relationship.get("Target", "")
                    rel_type = relationship.get("Type", "")
                    from urllib.parse import urlsplit
                    safe_link = rel_type.endswith("/hyperlink") and urlsplit(target).scheme.lower() in {"http", "https", "mailto"}
                    if not safe_link:
                        result["externalRelationships"] = True
    except Exception as exc:
        result["parseError"] = type(exc).__name__
    return result


async def preflight_document(content: bytes, filename: str, declared_mime: str = "") -> Dict[str, Any]:
    settings = get_settings()
    file_info = sniff_file_type(content, filename, declared_mime)
    audit: Dict[str, Any] = {
        "schemaVersion": 1,
        "sha256": hashlib.sha256(content).hexdigest(),
        "file": file_info,
        "malware": {"status": "NOT_RUN"},
        "securityFlags": [],
        "status": "UNVERIFIED",
        "riskLevel": "LOW",
        "authenticity": "UNVERIFIED",
    }
    if not getattr(settings, "DOCUMENT_AUDIT_ENABLED", True):
        audit["status"] = "UNVERIFIED"
        audit["securityFlags"].append("DOCUMENT_AUDIT_DISABLED")
        return audit

    audit["malware"] = await malware_scan(content)
    if audit["malware"]["status"] == "MALICIOUS":
        audit["status"] = "MALICIOUS"
        audit["riskLevel"] = "CRITICAL"
        audit["securityFlags"].append("MALWARE_DETECTED")
        return audit
    if audit["malware"]["status"] == "UNAVAILABLE":
        audit["securityFlags"].append("MALWARE_SCAN_UNAVAILABLE")

    if file_info["extensionMismatch"]:
        audit["securityFlags"].append("FILE_EXTENSION_MISMATCH")
    if file_info["mimeMismatch"]:
        audit["securityFlags"].append("MIME_MISMATCH")

    kind = file_info["kind"]
    if kind == "pdf":
        # Parse de PDF/DOCX e CPU-bound: fora do event loop (1000 paginas ~3 s).
        audit["pdf"] = await asyncio.to_thread(_inspect_pdf, content)
        active = audit["pdf"].get("activeContent") or []
        audit["securityFlags"].extend(active)
        if audit["pdf"].get("passwordRequired"):
            audit["securityFlags"].append("PDF_PASSWORD_REQUIRED")
        if audit["pdf"].get("parseError") or audit["pdf"].get("passwordRequired"):
            audit["securityFlags"].append("PDF_INSPECTION_INCOMPLETE")
        if active:
            audit["status"] = "SUSPICIOUS"
            audit["riskLevel"] = "HIGH"
        if (audit["pdf"].get("digitalSignaturePresent") and not audit["pdf"].get("passwordRequired")
                and getattr(settings, "DOCUMENT_SIGNATURE_VERIFY_ENABLED", True)):
            try:
                audit["pdf"]["signatureVerification"] = await asyncio.wait_for(
                    asyncio.to_thread(verify_pdf_signatures, content),
                    timeout=float(getattr(settings, "DOCUMENT_SIGNATURE_TIMEOUT_S", 20.0)),
                )
            except asyncio.TimeoutError:
                audit["pdf"]["signatureVerification"] = {"error": "timeout", "signatures": []}
    elif kind == "docx":
        audit["docx"] = await asyncio.to_thread(_inspect_docx, content)
        info = audit["docx"]
        if info.get("macroPresent"):
            audit["securityFlags"].append("DOCX_MACRO_PRESENT")
        if info.get("embeddedObjects"):
            audit["securityFlags"].append("DOCX_EMBEDDED_OBJECT")
        if info.get("externalRelationships"):
            audit["securityFlags"].append("DOCX_EXTERNAL_RELATIONSHIP")
        if info.get("pathTraversal"):
            audit["securityFlags"].append("DOCX_PATH_TRAVERSAL")
        max_expand = int(getattr(settings, "DOCUMENT_MAX_EXPANDED_BYTES", 150 * 1024 * 1024))
        if info.get("expandedBytes", 0) > max_expand or info.get("expansionLimitExceeded"):
            audit["securityFlags"].append("DOCX_EXPANSION_LIMIT_EXCEEDED")
        if info.get("parseError") or info.get("relationshipParseError"):
            audit["securityFlags"].append("DOCX_PARSE_ERROR")
        if any(flag.startswith("DOCX_") for flag in audit["securityFlags"]):
            audit["status"] = "SUSPICIOUS"
            audit["riskLevel"] = "HIGH"
    elif kind in {"jpg", "png", "webp", "gif"}:
        audit["image"] = await asyncio.to_thread(_inspect_image, content)

    fail_closed = bool(getattr(settings, "DOCUMENT_MALWARE_FAIL_CLOSED", False))
    if fail_closed and audit["malware"]["status"] != "PASS":
        audit["status"] = "SUSPICIOUS"
        audit["riskLevel"] = "HIGH"
        audit["securityFlags"].append("MALWARE_SCAN_REQUIRED")

    return audit


def classify_document(text: str, filename: str = "") -> str:
    body = _norm((filename or "") + "\n" + (text or ""))
    # A source memorial can mention missing reports/invoices. Its role wins
    # over vocabulary referring to documents that have not been delivered.
    if 'memorial' in _norm(filename) or re.search(r'^\s*memorial\b', _norm(text or '')):
        return 'memorial'
    # Identidade primeiro: uma CNH frequentemente menciona outros termos em rodape.
    if any(term in body for term in ("carteira nacional de habilitacao", "permissao para dirigir", "senatran", "cnh")):
        return "cnh"
    if "nota fiscal eletronica de servicos" in body or "nfs-e" in body or "nfse" in body:
        return "nota_fiscal_servico"
    if any(term in body for term in ("danfe", "nota fiscal eletronica", "chave de acesso")):
        return "nota_fiscal"
    if any(term in body for term in ("laudo tecnico", "laudo de avaria", "relatorio tecnico", "parecer tecnico")) or "laudo" in _norm(filename):
        return "laudo"
    if "orcamento" in body or "proposta comercial" in body:
        return "orcamento"
    if any(term in body for term in ("comprovante de pagamento", "comprovante pix", "pix realizado", "transferencia realizada")):
        return "comprovante_pagamento"
    if "recibo" in body:
        return "recibo"
    if "contrato" in body:
        return "contrato"
    return "outro"


def extract_fields(text: str, document_type: str) -> Dict[str, Any]:
    text = text or ""
    cpfs = _unique(
        _digits(match) for match in [*_CPF_RE.findall(text), *_CPF_LABELED_RE.findall(text)]
    )
    cnpjs = _unique(
        _digits(match) for match in [*_CNPJ_RE.findall(text), *_CNPJ_LABELED_RE.findall(text)]
    )
    cpfs = [value for value in cpfs if len(value) == 11]
    cnpjs = [value for value in cnpjs if len(value) == 14]
    money = _unique(value for value in (_money_to_float(raw) for raw in _MONEY_RE.findall(text)) if value is not None)
    dates = _unique(_DATE_RE.findall(text))
    nfe_keys = _unique(
        _digits(raw) for raw in [*_NFE_KEY_RE.findall(text), *_NFE_KEY_SPACED_RE.findall(text)]
    )
    registries = []
    for council, uf, number in _REGISTRY_RE.findall(text):
        registries.append({"council": council.upper(), "uf": uf.upper() if uf else None, "number": number})

    fields: Dict[str, Any] = {
        "cpfs": [{"value": value, "checksumValid": validate_cpf(value)} for value in cpfs],
        "cnpjs": [{"value": value, "checksumValid": validate_cnpj(value)} for value in cnpjs],
        "moneyValues": money,
        "dates": dates,
        "professionalRegistries": registries,
        "nfeKeys": [{"value": value, "checksumValid": validate_nfe_key(value)} for value in nfe_keys],
        "pixEndToEndIds": [parse_pix_e2e(value) for value in _unique(_PIX_E2E_RE.findall(text))],
    }
    verification = _NFSE_VERIFY_RE.search(text)
    if verification:
        fields["verificationCode"] = verification.group(1).upper()

    if document_type in {"nota_fiscal", "nota_fiscal_servico"}:
        invoice_patterns = [
            r"(?:numero|n[uú]mero)\s+(?:da\s+)?nota\s*[:\-]?\s*0*([0-9]{1,20})",
            r"nfs-e\s*(?:n[ºo°.]*)?\s*[:\-]?\s*0*([0-9]{1,20})",
        ]
        for pattern in invoice_patterns:
            match = re.search(pattern, text, re.I)
            if match:
                fields["documentNumber"] = match.group(1)
                break
    elif document_type == "laudo":
        match = re.search(r"(?:laudo|relatorio)\s*(?:n[ºo°.]*)?\s*[:\-]?\s*([A-Z0-9./-]{2,30})", text, re.I)
        if match:
            fields["documentNumber"] = match.group(1)
    elif document_type == "cnh":
        match = re.search(r"(?:n[ºo°.]?\s*)?(?:registro|registro cnh|cnh)\s*[:\-]?\s*([0-9]{8,15})", text, re.I)
        if match:
            fields["documentNumber"] = match.group(1)
        category = re.search(r"(?:categoria|cat\.?)\s*[:\-]?\s*([A-E](?:[A-E])?)\b", text, re.I)
        if category:
            fields["category"] = category.group(1).upper()

    return fields


def _content_findings(text: str, document_type: str, fields: Dict[str, Any]) -> List[str]:
    normalized = _norm(text)
    findings: List[str] = []
    if document_type == "laudo":
        if any(term in normalized for term in (
            "exemplo ficticio", "modelo ficticio", "sem validacao tecnica",
            "nao comprova dano real", "nao constitui laudo emitido por profissional",
        )):
            findings.append("SELF_DECLARED_NON_EVIDENTIARY_DOCUMENT")
        if "pendente de inspecao" in normalized or "nao houve inspecao" in normalized:
            findings.append("TECHNICAL_INSPECTION_NOT_COMPLETED")
        if "preencher somente apos avaliacao real" in normalized or "responsavel tecnico" in normalized and not fields.get("professionalRegistries"):
            findings.append("TECHNICAL_SIGNATORY_UNVERIFIED")
    invalid_ids = [x for x in fields.get("cpfs", []) + fields.get("cnpjs", []) if not x.get("checksumValid")]
    if invalid_ids:
        findings.append("INVALID_TAX_ID_CHECKSUM")
    invalid_keys = [x for x in fields.get("nfeKeys", []) if not x.get("checksumValid")]
    if invalid_keys:
        findings.append("INVALID_NFE_KEY_CHECKSUM")
    return _unique(findings)


def _required_field_findings(document_type: str, fields: Dict[str, Any], text_chars: int) -> List[str]:
    findings: List[str] = []
    threshold = max(int(getattr(get_settings(), "DOCUMENT_VISION_MIN_TEXT_CHARS", 800)), 0)
    if document_type in {"cnh", "nota_fiscal", "nota_fiscal_servico"} and threshold and text_chars < threshold:
        findings.append("INSUFFICIENT_TEXT_COVERAGE")

    if document_type == "cnh":
        if not fields.get("cpfs") or not fields.get("dates"):
            findings.append("REQUIRED_FIELDS_INCOMPLETE")
    elif document_type in {"nota_fiscal", "nota_fiscal_servico"}:
        if not fields.get("cnpjs") or not fields.get("moneyValues") or not fields.get("dates"):
            findings.append("REQUIRED_FIELDS_INCOMPLETE")
    elif document_type == "laudo":
        # Laudo pode nao ter CPF, mas precisa trazer algum conteudo tecnico e, quando
        # aplicavel, responsavel/registro. A ausencia nao prova fraude: apenas reduz
        # a confianca automatica.
        if text_chars < 200:
            findings.append("REQUIRED_FIELDS_INCOMPLETE")
    return findings


def finalize_document_audit(
    preflight: Dict[str, Any],
    text: str,
    filename: str,
    *,
    case_context: Optional[Dict[str, Any]] = None,
    text_source: str = "text_layer",
) -> Dict[str, Any]:
    """Consolida a auditoria.

    ``text_source``: "text_layer" (texto do proprio arquivo), "vision" (texto
    descrito por modelo de visao) ou "transcript" (audio). Campos lidos por
    visao/transcricao podem ter erro de leitura, entao digito verificador
    invalido neles pede revisao em vez de marcar conflito.
    """
    audit = dict(preflight or {})
    if audit.get("status") == "MALICIOUS":
        return audit

    document_type = classify_document(text, filename)
    fields = extract_fields(text, document_type)
    findings = _content_findings(text, document_type, fields)
    findings.extend(_required_field_findings(document_type, fields, len(text or "")))
    editing = editing_findings(audit)
    findings.extend(editing)
    signature, verified_signature = signature_findings(audit)
    findings.extend(signature)
    structural = structural_findings(fields)
    findings.extend(structural)
    audit["externalChecks"] = external_checks(fields)
    audit["documentType"] = document_type
    audit["textSource"] = text_source
    audit["fields"] = fields
    audit["findings"] = findings
    audit["textChars"] = len(text or "")

    context = case_context or {}
    client_name = str(context.get("client_name") or context.get("clientName") or "").strip()
    client_cpf = _digits(str(context.get("client_cpf") or context.get("clientCpf") or ""))
    consistency: Dict[str, Any] = {}
    if client_cpf:
        doc_cpfs = [item["value"] for item in fields.get("cpfs", [])]
        if doc_cpfs:
            # Somente documentos explicitamente identificados como pertencentes
            # ao cliente podem produzir conflito de identidade.
            identity_required = document_type == "cnh" or context.get("documentOwner") == "client"
            consistency["clientCpf"] = "PASS" if client_cpf in doc_cpfs else "CONFLICT" if identity_required else "NOT_CONFIRMED"
            if client_cpf not in doc_cpfs and identity_required:
                findings.append("CLIENT_CPF_MISMATCH")
    if client_name:
        normalized_client = _norm(client_name)
        normalized_text = _norm(text)
        consistency["clientName"] = "PASS" if normalized_client and normalized_client in normalized_text else "NOT_FOUND"
    audit["caseConsistency"] = consistency

    serious = set(findings) & {
        "DIGITAL_SIGNATURE_BROKEN",
        "MODIFIED_AFTER_SIGNATURE",
        "SELF_DECLARED_NON_EVIDENTIARY_DOCUMENT",
        "INVALID_TAX_ID_CHECKSUM",
        "INVALID_NFE_KEY_CHECKSUM",
        "CLIENT_CPF_MISMATCH",
    }
    reading_sensitive = {
        "INVALID_TAX_ID_CHECKSUM", "INVALID_NFE_KEY_CHECKSUM", "CLIENT_CPF_MISMATCH",
        "NFE_KEY_ISSUER_MISMATCH", "NFE_KEY_DATE_MISMATCH", "NFE_KEY_NUMBER_MISMATCH",
        "PIX_E2E_DATE_MISMATCH",
    }
    serious |= set(structural) - {"PIX_E2E_INVALID_FORMAT"}
    if text_source != "text_layer":
        # Lido por visao/transcricao: divergencia pode ser erro de leitura do modelo.
        serious -= reading_sensitive
    needs_review = bool(editing and document_type in _ISSUER_GENERATED) or (
        text_source != "text_layer" and bool(reading_sensitive & set(findings))
    ) or "PIX_E2E_INVALID_FORMAT" in findings
    if audit.get("securityFlags"):
        dangerous_flags = {"PDF_JAVASCRIPT", "PDF_OPEN_ACTION", "PDF_LAUNCH_ACTION", "PDF_EMBEDDED_FILE",
                           "DOCX_MACRO_PRESENT", "DOCX_EXTERNAL_RELATIONSHIP", "DOCX_PATH_TRAVERSAL",
                           "MALWARE_SCAN_REQUIRED"}
        if dangerous_flags.intersection(audit["securityFlags"]):
            audit["status"] = "SUSPICIOUS"
            audit["riskLevel"] = "HIGH"
    if serious:
        audit["status"] = "SUSPICIOUS" if "SELF_DECLARED_NON_EVIDENTIARY_DOCUMENT" in serious else "CONFLICTING"
        audit["riskLevel"] = "HIGH"
    elif audit.get("status") not in {"SUSPICIOUS", "CONFLICTING"}:
        incomplete = any(flag in findings for flag in ("INSUFFICIENT_TEXT_COVERAGE", "REQUIRED_FIELDS_INCOMPLETE"))
        if incomplete or needs_review:
            audit["status"] = "UNVERIFIED"
            audit["riskLevel"] = "MEDIUM"
        else:
            # Houve leitura e checagens deterministicas, mas autenticidade externa continua separada.
            scanner_ok = (audit.get("malware") or {}).get("status") == "PASS"
            coverage_ok = not audit.get("securityFlags") and scanner_ok
            audit["status"] = "CONSISTENT" if (text or "").strip() and coverage_ok else "UNVERIFIED"
            audit["riskLevel"] = "LOW" if audit["status"] == "CONSISTENT" else "MEDIUM"

    # Unico caminho para autenticidade diferente de UNVERIFIED: assinatura digital
    # integra, sem alteracao posterior e com cadeia ate uma raiz confiavel.
    if verified_signature and not serious:
        audit["authenticity"] = "SIGNATURE_VERIFIED"
        audit["verifiedSigner"] = verified_signature.get("signer")
    else:
        audit["authenticity"] = "UNVERIFIED"
    audit["findings"] = _unique(findings)
    return audit


def apply_cross_document_audit(records: List[Dict[str, Any]]) -> None:
    """Marca conflitos fortes sem tentar escolher qual documento e verdadeiro."""
    seen_invoice: Dict[Tuple[str, str], Tuple[str, Tuple[float, ...]]] = {}
    for record in records:
        audit = record.get("audit") or {}
        dtype = audit.get("documentType")
        fields = audit.get("fields") or {}
        if dtype not in {"nota_fiscal", "nota_fiscal_servico"}:
            continue
        number = str(fields.get("documentNumber") or "").strip()
        cnpjs = tuple(sorted(item.get("value") for item in fields.get("cnpjs", []) if item.get("value")))
        values = tuple(sorted(round(float(v), 2) for v in fields.get("moneyValues", []) if isinstance(v, (int, float))))
        if not number or not cnpjs:
            continue
        key = (number, cnpjs[0])
        if key not in seen_invoice:
            seen_invoice[key] = (record.get("id", ""), values)
            continue
        other_id, other_values = seen_invoice[key]
        if values != other_values:
            for candidate in records:
                if candidate.get("id") not in {record.get("id"), other_id}:
                    continue
                candidate_audit = candidate.setdefault("audit", {})
                findings = candidate_audit.setdefault("findings", [])
                if "DUPLICATE_DOCUMENT_NUMBER_CONFLICT" not in findings:
                    findings.append("DUPLICATE_DOCUMENT_NUMBER_CONFLICT")
                candidate_audit["status"] = "CONFLICTING"
                candidate_audit["riskLevel"] = "HIGH"


def public_audit_summary(audit: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Resumo seguro para UI, sem expor texto nem identificadores pessoais."""
    audit = audit or {}
    malware = audit.get("malware") or {}
    file_info = audit.get("file") or {}
    fields = audit.get("fields") or {}
    flags = list(_unique([*(audit.get("securityFlags") or []), *(audit.get("findings") or [])]))[:20]

    checks: List[Dict[str, str]] = []
    malware_status = malware.get("status", "NOT_RUN")
    checks.append({
        "code": "MALWARE_SCAN",
        "status": "PASS" if malware_status == "PASS" else "FAIL" if malware_status == "MALICIOUS" else "WARN",
        "label": "Varredura antimalware",
    })

    if file_info:
        mismatch = bool(file_info.get("extensionMismatch") or file_info.get("mimeMismatch"))
        checks.append({
            "code": "FILE_TYPE",
            "status": "WARN" if mismatch else "PASS",
            "label": "Tipo real e MIME do arquivo",
        })

    ids = [*(fields.get("cpfs") or []), *(fields.get("cnpjs") or [])]
    if ids:
        checks.append({
            "code": "TAX_ID_CHECKSUM",
            "status": "PASS" if all(item.get("checksumValid") for item in ids) else "FAIL",
            "label": "Dígitos verificadores de CPF/CNPJ",
        })

    nfe_keys = fields.get("nfeKeys") or []
    if nfe_keys:
        checks.append({
            "code": "NFE_KEY_CHECKSUM",
            "status": "PASS" if all(item.get("checksumValid") for item in nfe_keys) else "FAIL",
            "label": "Chave de acesso da NF-e",
        })

    if fields.get("verificationCode"):
        checks.append({"code": "VERIFICATION_CODE", "status": "PASS", "label": "Código de verificação encontrado"})

    pdf = audit.get("pdf") or {}
    if pdf.get("digitalSignaturePresent"):
        if audit.get("authenticity") == "SIGNATURE_VERIFIED":
            signature_status, label = "PASS", "Assinatura digital íntegra e de cadeia confiável"
        elif any(flag in flags for flag in ("DIGITAL_SIGNATURE_BROKEN", "MODIFIED_AFTER_SIGNATURE")):
            signature_status, label = "FAIL", "Documento alterado depois de assinado"
        elif "DIGITAL_SIGNATURE_UNTRUSTED_CHAIN" in flags:
            signature_status, label = "INFO", "Assinatura íntegra; emissor não confirmado"
        else:
            signature_status, label = "INFO", "Assinatura digital presente no PDF"
        checks.append({"code": "DIGITAL_SIGNATURE", "status": signature_status, "label": label})

    structural_flags = [flag for flag in flags if flag.startswith(("NFE_KEY_", "PIX_E2E_"))
                        and flag != "PIX_E2E_INVALID_FORMAT"]
    if fields.get("pixEndToEndIds") or any(i.get("checksumValid") for i in nfe_keys):
        checks.append({
            "code": "ENCODED_IDENTIFIERS",
            "status": "FAIL" if structural_flags else "PASS",
            "label": "Dados codificados na chave NF-e / E2E PIX batem com o documento",
        })

    if any(flag in flags for flag in ("PRODUCED_BY_EDITING_TOOL", "PDF_MODIFIED_AFTER_CREATION")):
        checks.append({"code": "EDITING_SIGNS", "status": "WARN", "label": "Sinais de edição nos metadados"})

    if audit.get("caseConsistency"):
        values = set((audit.get("caseConsistency") or {}).values())
        checks.append({
            "code": "CASE_CONSISTENCY",
            "status": "FAIL" if "CONFLICT" in values else "PASS" if values == {"PASS"} else "WARN",
            "label": "Consistência com os dados do caso",
        })

    return {
        "auditStatus": audit.get("status", "UNVERIFIED"),
        "riskLevel": audit.get("riskLevel", "MEDIUM"),
        "authenticity": audit.get("authenticity", "UNVERIFIED"),
        "documentType": audit.get("documentType", "outro"),
        "malware": malware_status,
        "flags": flags,
        "checks": checks[:12],
    }
