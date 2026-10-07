# -*- coding: utf-8 -*-
"""Upload do chat: o arquivo enviado nao pode virar pagina executavel em /uploads."""
import asyncio
import io

import pytest
from fastapi import FastAPI, UploadFile
from fastapi.testclient import TestClient
from starlette.datastructures import Headers

from config import get_settings
from src.upload_service import SafeUploadStaticFiles, UploadService, safe_extension


@pytest.fixture
def service(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(settings, "DOCUMENTS_PATH", str(tmp_path / "documents"))
    monkeypatch.setattr(settings, "DOCUMENT_MALWARE_SCAN_ENABLED", False)
    monkeypatch.setattr(settings, "DOCUMENT_MALWARE_FAIL_CLOSED", False)
    monkeypatch.setattr(settings, "NEGOTIATION_VISION_ENABLED", False)
    return UploadService(settings)


def enviar(service, conteudo, nome, mime):
    upload = UploadFile(io.BytesIO(conteudo), filename=nome, headers=Headers({"content-type": mime}))
    return asyncio.run(service.save_file(upload))


def test_html_declarado_como_texto_e_gravado_como_txt(service):
    salvo = enviar(service, b"<html><script>alert(document.cookie)</script></html>", "x.html", "text/plain")
    assert "error" not in salvo
    assert salvo["url"].endswith(".txt")


def test_svg_nao_e_aceito(service):
    salvo = enviar(service, b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
                   "a.svg", "image/svg+xml")
    assert "error" in salvo


def test_extensao_segue_o_conteudo_real():
    assert safe_extension("pdf", "image/png") == "pdf"
    assert safe_extension("unknown", "audio/mpeg") == "mp3"
    assert safe_extension("zip", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet") == "xlsx"
    assert safe_extension("zip", "text/plain") == "zip"
    assert safe_extension("txt", "text/csv") == "csv"
    assert safe_extension("unknown", "text/html") == "bin"


def test_uploads_servidos_sem_execucao(tmp_path):
    (tmp_path / "a.txt").write_text("<script>alert(1)</script>")
    (tmp_path / "b.pdf").write_bytes(b"%PDF-1.4")
    app = FastAPI()
    app.mount("/uploads", SafeUploadStaticFiles(directory=str(tmp_path)), name="uploads")
    client = TestClient(app)
    texto = client.get("/uploads/a.txt")
    assert texto.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in texto.headers["content-security-policy"]
    documento = client.get("/uploads/b.pdf")
    assert documento.headers["x-content-type-options"] == "nosniff"
    assert "content-security-policy" not in documento.headers


# --- leitura falhar nao derruba o upload ------------------------------------

def test_upload_nao_dispara_leitura_pesada(service, monkeypatch):
    from unittest.mock import AsyncMock
    from src.services import case_evidence as ce
    reader = AsyncMock(side_effect=RuntimeError("nao deveria ser chamado"))
    monkeypatch.setattr(ce, "read_evidence", reader)
    from reportlab.pdfgen.canvas import Canvas
    buffer = io.BytesIO()
    canvas = Canvas(buffer)
    canvas.drawString(50, 800, "Recibo R$ 100,00")
    canvas.save()
    salvo = enviar(service, buffer.getvalue(), "recibo.pdf", "application/pdf")
    assert "error" not in salvo and salvo["url"].endswith(".pdf")
    assert reader.await_count == 0


def test_pdf_com_senha_de_usuario_explica_o_motivo(service):
    from PyPDF2 import PdfWriter
    writer = PdfWriter()
    writer.add_blank_page(200, 200)
    writer.encrypt(user_password="1234", owner_password="banco")
    buffer = io.BytesIO()
    writer.write(buffer)
    salvo = enviar(service, buffer.getvalue(), "extrato.pdf", "application/pdf")
    assert salvo.get("blocked") and "senha" in salvo["error"]


def test_leitura_so_acontece_no_pos_caso(service, monkeypatch):
    from unittest.mock import AsyncMock
    from src.services import case_evidence as ce
    monkeypatch.setattr(get_settings(), "SERVICE_BASE_URL", "https://api.test")
    salvo = enviar(service, b"Relato: pagamento de R$ 300,00", "relato.txt", "text/plain")
    reader = AsyncMock(return_value=("Relato: pagamento de R$ 300,00", "txt", "ok", {"pages": [{"page_number": 1, "text": "Relato: pagamento de R$ 300,00", "method": "test"}], "methods": ["test"], "status": "read"}))
    monkeypatch.setattr(ce, "read_evidence", reader)
    estado = asyncio.run(ce.ingest_case_evidence({"chat_id": "c", "user_id": "u", "received_case_files": [salvo["url"]]}, include_files=True, include_audio=False))
    assert estado["case_evidence"][0]["status"] == "read"
    assert estado["case_evidence"][0]["postCasePipeline"]["pages"][0]["method"] == "test"
    assert reader.await_count == 1


def test_audio_sem_mime_e_lido_pela_extensao(monkeypatch):
    from unittest.mock import AsyncMock
    from src.services import audio_transcription
    from src.services import case_evidence as ce
    monkeypatch.setattr(audio_transcription, "transcribe_bytes", AsyncMock(return_value="tela quebrada"))
    texto, tipo, _ = asyncio.run(ce.read_evidence(b"ID3\x04" + b"\x00" * 64, "relato.mp3",
                                                  "application/octet-stream", detected_kind="unknown"))
    assert (texto, tipo) == ("tela quebrada", "audio")


# --- auditoria nao vai para o consumidor --------------------------------------

def test_resposta_do_upload_nao_expoe_auditoria(service):
    salvo = enviar(service, b"Relato do cliente", "relato.txt", "text/plain")
    for campo in ("auditStatus", "riskLevel", "flags", "checks", "authenticity"):
        assert campo not in salvo


def test_bloqueio_traz_motivo_neutro(service, monkeypatch):
    from src.services import document_audit as da
    async def malicioso(conteudo):
        return {"status": "MALICIOUS", "engine": "clamd", "threat": "X"}
    monkeypatch.setattr(get_settings(), "DOCUMENT_MALWARE_SCAN_ENABLED", True)
    monkeypatch.setattr(da, "malware_scan", malicioso)
    salvo = enviar(service, b"qualquer", "a.txt", "text/plain")
    assert salvo["blocked"] and salvo["reason"] == "security_policy"
    assert "malware" not in salvo["error"].lower() and "auditoria" not in salvo["error"].lower()


def test_metadados_do_cliente_sem_auditoria_e_registro_interno_com():
    from src.services import case_evidence as ce
    estado = {"case_evidence": [{"id": "1", "name": "a.pdf", "kind": "pdf", "status": "read",
                                 "audit": {"status": "CONFLICTING", "riskLevel": "HIGH"}}]}
    cliente = ce.evidence_metadata(estado)["files"][0]
    interno = ce.evidence_metadata(estado, audience="internal")["files"][0]
    assert "auditStatus" not in cliente
    assert interno["auditStatus"] == "CONFLICTING"
