"""ElevenLabs Speech-to-Text.

Uses the Scribe API (multipart form upload). Returns plain transcript text.
We keep this thin — the agent handles all interpretation of the transcript.
"""
from __future__ import annotations

import logging

import httpx

from app.config import settings

log = logging.getLogger("paywise.stt")


async def transcribe(audio_bytes: bytes, filename: str = "audio.ogg") -> str:
    """Transcribe an audio blob via ElevenLabs Scribe. Returns transcript text.

    Raises RuntimeError on failure so the agent can catch it and ask the
    merchant to repeat themselves rather than crash.
    """
    url = "https://api.elevenlabs.io/v1/speech-to-text"
    headers = {"xi-api-key": settings.elevenlabs_api_key}

    # model_id + language_suggested: None lets Scribe auto-detect, which works
    # well for Nigerian Pidgin/Yoruba/Hausa/Igbo mixed with English.
    files = {
        "file": (filename, audio_bytes, "audio/ogg"),
        "model_id": (None, settings.elevenlabs_stt_model),
    }
    try:
        async with httpx.AsyncClient(timeout=60.0) as http:
            resp = await http.post(url, headers=headers, files=files)
            resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        log.error("ElevenLabs STT HTTP %s: %s", resp.status_code, resp.text)
        raise RuntimeError(f"Speech-to-text failed: {resp.status_code}") from e
    except httpx.HTTPError as e:
        raise RuntimeError(f"Speech-to-text network error: {e}") from e

    body = resp.json()
    text = (body.get("text") or "").strip()
    log.info("STT transcript (%d chars): %s", len(text), text[:120])
    return text
