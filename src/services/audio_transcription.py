# -*- coding: utf-8 -*-
"""
Servico de transcricao de audio via OpenAI Whisper.

Contrato (do backend Zellu, contrato 20260612 de audio):
- Backend envia URLs presigned (validas por 3600s) apontando para o audio
  no MinIO/S3 privado.
- A IA baixa o audio antes da expiracao, transcreve via Whisper e usa o
  texto transcrito como conteudo da mensagem do usuario.

Uso:
    from src.services.audio_transcription import transcribe_urls
    transcriptions = await transcribe_urls(["https://...presigned.webm"])
    # -> ["Ola, gostaria de saber sobre..."]

Custo aproximado (whisper-1): $0.006 / minuto de audio. Audios do chat sao
limitados a 60s no front, entao ~$0.006/audio.

Robustez: falhas (timeout no download, expiracao da URL, erro do Whisper)
sao logadas e retornam string vazia naquela posicao - o caller decide o
fallback ("[Mensagem de audio nao transcrita]" ou ignorar).
"""

import asyncio
import time
from typing import List, Optional, Tuple

import httpx
from openai import AsyncOpenAI

from config import get_settings, llm_default_headers
from src.services.ai_usage_tracker import track_usage


# Custo estimado por audio do Whisper (whisper-1: $0.006/min; audios do chat
# sao limitados a 60s no front, entao ~1 min => ~$0.006). Estimativa flat
# (a API nao retorna a duracao); o painel agrega como custo de STT.
_WHISPER_COST_PER_AUDIO_USD = 0.006


# Modelo de transcricao do Whisper. whisper-1 e o estavel e amplamente suportado.
_WHISPER_MODEL = "whisper-1"

# Idioma default das transcricoes. PT-BR e o publico da Zellu.
_DEFAULT_LANGUAGE = "pt"

# Timeout total de download de cada audio. 30s e folgado para webm <= 60s.
_DOWNLOAD_TIMEOUT_S = 30.0

# Limite de tamanho aceito (50MB - bate com o limite do Whisper API).
_MAX_AUDIO_BYTES = 25 * 1024 * 1024


def _filename_from_url(url: str, default: str = "audio.webm") -> str:
    """Tenta deduzir o nome do arquivo a partir da URL presigned (so para
    o parametro filename do Whisper, que so usa a extensao para sniff).
    """
    try:
        from urllib.parse import urlparse
        path = urlparse(url).path or ""
        name = path.rsplit("/", 1)[-1] if path else ""
        if name and "." in name:
            return name
    except Exception:
        pass
    return default


async def _download_audio(url: str) -> Optional[Tuple[bytes, str]]:
    """Baixa o audio de uma URL presigned.

    Retorna (bytes, filename) em caso de sucesso ou None se algo falhar.
    """
    try:
        async with httpx.AsyncClient(timeout=_DOWNLOAD_TIMEOUT_S, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
            audio_bytes = response.content
            if not audio_bytes:
                print(f"[AUDIO-TRANSCRIBE] Download vazio: {url[:80]}...")
                return None
            if len(audio_bytes) > _MAX_AUDIO_BYTES:
                print(
                    f"[AUDIO-TRANSCRIBE] Audio excede limite de "
                    f"{_MAX_AUDIO_BYTES // (1024*1024)}MB (got {len(audio_bytes)} bytes)"
                )
                return None
            filename = _filename_from_url(url)
            return audio_bytes, filename
    except httpx.HTTPStatusError as exc:
        # 403 normalmente significa presigned expirada (TTL 3600s estourado)
        print(f"[AUDIO-TRANSCRIBE] Erro HTTP {exc.response.status_code} ao baixar audio: {url[:80]}...")
        return None
    except Exception as exc:
        print(f"[AUDIO-TRANSCRIBE] Falha no download: {exc}")
        return None


async def transcribe_url(
    url: str,
    language: str = _DEFAULT_LANGUAGE,
    module: str = "external_chat",
    event_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Transcreve um unico audio via Whisper.

    Args:
        url: URL presigned (HTTP/S) do arquivo de audio.
        language: codigo ISO-639-1 (default 'pt').
        module: modulo de negocio para atribuicao de custo (default external_chat).
        event_id: eventId p/ correlacionar com as demais chamadas do mesmo fluxo.
        session_id: sessao associada (opcional).

    Returns:
        Texto transcrito. Em caso de falha, retorna "" (caller decide fallback).
    """
    download = await _download_audio(url)
    if not download:
        return ""

    audio_bytes, filename = download
    return await transcribe_bytes(
        audio_bytes, filename, language=language, module=module,
        event_id=event_id, session_id=session_id,
    )


async def transcribe_bytes(
    audio_bytes: bytes, filename: str, language: str = _DEFAULT_LANGUAGE,
    module: str = "external_chat", event_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Transcreve bytes já armazenados, sem novo download ou conteúdo nos logs."""
    import mimetypes

    if not audio_bytes or len(audio_bytes) > _MAX_AUDIO_BYTES:
        return ""
    try:
        settings = get_settings()
        client = AsyncOpenAI(default_headers=llm_default_headers(), api_key=settings.OPENAI_API_KEY, timeout=90.0, max_retries=1)
        # OpenAI SDK aceita tuple (filename, bytes, mime) ou bytes; tuple e
        # mais explicito sobre a extensao para o sniffer da API.
        _t0 = time.perf_counter()
        async with client:
            transcription = await client.audio.transcriptions.create(
                model=_WHISPER_MODEL,
                file=(filename, audio_bytes, mimetypes.guess_type(filename)[0] or "application/octet-stream"),
                language=language,
                response_format="text",
            )
        # Modalidade nao-token (STT): custo flat estimado, tokens=0.
        track_usage(
            module=module,
            model=_WHISPER_MODEL,
            tokens_in=0,
            tokens_out=0,
            cost_usd=_WHISPER_COST_PER_AUDIO_USD,
            provider="openai",
            event_id=event_id,
            event_type="audio_transcription",
            unit="requests",
            quantity_in=1,
            session_id=session_id,
            duration_ms=int((time.perf_counter() - _t0) * 1000),
        )
        # response_format='text' devolve string direta; outras formas devolvem
        # objeto com .text. Cobre os dois casos por robustez.
        if isinstance(transcription, str):
            text = transcription
        else:
            text = getattr(transcription, "text", "") or ""
        text = text.strip()
        if text:
            print(f"[AUDIO-TRANSCRIBE] OK ({len(audio_bytes)} bytes, {len(text)} caracteres)")
        else:
            print("[AUDIO-TRANSCRIBE] Transcricao vazia")
        return text
    except Exception as exc:
        print(f"[AUDIO-TRANSCRIBE] Falha no Whisper: {type(exc).__name__}")
        return ""


async def transcribe_urls(
    urls: List[str],
    language: str = _DEFAULT_LANGUAGE,
    module: str = "external_chat",
    event_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> List[str]:
    """Transcreve varias URLs em paralelo via asyncio.gather.

    Mantem a ordem das URLs. Falhas individuais viram string vazia naquela
    posicao - o caller pode filtrar ou usar placeholder.
    """
    if not urls:
        return []
    tasks = [
        transcribe_url(
            u, language=language, module=module, event_id=event_id, session_id=session_id
        )
        for u in urls
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    out: List[str] = []
    for r in results:
        if isinstance(r, str):
            out.append(r)
        else:
            print(f"[AUDIO-TRANSCRIBE] Excecao em transcribe_urls: {r}")
            out.append("")
    return out


def merge_text_with_audio(text: Optional[str], transcriptions: List[str]) -> str:
    """Mescla texto da mensagem (se houver) com transcricoes de audios.

    Padrao de merge:
      - so texto      -> texto
      - so audio      -> "[audio] transcricao"
      - texto + audio -> "texto\n\n[audio] transcricao"
      - audios multiplos -> juntados com \n\n
      - placeholder se TODAS as transcricoes vieram vazias

    Retorna string sempre (nunca vazia se houver entrada minima).
    """
    text_clean = (text or "").strip()
    # Filtra placeholders que o backend manda quando audio existe mas o
    # texto vai vazio ("[Mensagem de audio]" etc.)
    if text_clean.startswith("[Mensagem de áudio") or text_clean.startswith("[Mensagem de audio"):
        text_clean = ""

    valid_transcriptions = [t.strip() for t in transcriptions if t and t.strip()]

    if not valid_transcriptions:
        # Nada transcrito: devolve texto original (ou placeholder se nao tem texto)
        return text_clean or "[Mensagem de áudio - transcrição indisponível]"

    audio_block = "\n\n".join(f"[áudio] {t}" for t in valid_transcriptions)

    if text_clean:
        return f"{text_clean}\n\n{audio_block}"
    return audio_block
