"""OpenAI Whisper Speech-to-Text.

Uses the Whisper API (multipart form upload). Returns plain transcript text.
We keep this thin — the agent handles all interpretation of the transcript.

Switched from ElevenLabs Scribe because Whisper handles Nigerian accents
(Pidgin, Yoruba, Hausa, Igbo) significantly better, especially for names
and mixed-language speech.
"""
from __future__ import annotations

import logging

import httpx

from app.config import settings

log = logging.getLogger("paywise.stt")


async def transcribe(audio_bytes: bytes, filename: str = "audio.ogg") -> str:
    """Transcribe an audio blob via OpenAI Whisper. Returns transcript text.

    Raises RuntimeError on failure so the agent can catch it and ask the
    merchant to repeat themselves rather than crash.
    """
    url = "https://api.openai.com/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {settings.openai_api_key}"}

    # Whisper auto-detects language well for Nigerian Pidgin/Yoruba/Hausa/Igbo.
    # We let it auto-detect so merchants can code-switch naturally.
    files = {
        "file": (filename, audio_bytes, "audio/ogg"),
    }
    data = {
        "model": "whisper-1",
        "response_format": "json",
    }
    try:
        async with httpx.AsyncClient(timeout=60.0) as http:
            resp = await http.post(url, headers=headers, files=files, data=data)
            resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        log.error("Whisper STT HTTP %s: %s", resp.status_code, resp.text)
        raise RuntimeError(f"Speech-to-text failed: {resp.status_code}") from e
    except httpx.HTTPError as e:
        raise RuntimeError(f"Speech-to-text network error: {e}") from e

    body = resp.json()
    text = (body.get("text") or "").strip()
    log.info("Whisper transcript (%d chars): %s", len(text), text[:120])
    return text
