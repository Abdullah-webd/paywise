"""WhatsApp outbound messaging via Twilio.

Two methods:
  - send_text:  short replies, statuses, acks.
  - send_audio: longer/richer messages — synthesised voice notes.

Twilio's WhatsApp API accepts media by public URL, so to send a voice note we
must first publish the generated WAV somewhere Twilio can fetch it. In dev we
expose a temporary endpoint on our own FastAPI server; in prod this would be S3.
"""
from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import uuid
from typing import Optional

import asyncio

from twilio.rest import Client

from app.config import settings

log = logging.getLogger("paywise.whatsapp")


def _get_ffmpeg() -> str:
    """Return the ffmpeg executable path (bundled via imageio-ffmpeg)."""
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


class WhatsAppSender:
    def __init__(self) -> None:
        # Use the SYNCHRONOUS Twilio client and offload each call to a thread.
        # The async Twilio http client is unreliable in this version, so we keep
        # it simple and blocking-friendly.
        self._client = Client(
            settings.twilio_account_sid,
            settings.twilio_auth_token,
        )
        self._from = settings.twilio_whatsapp_from
        # In-memory map of media_id -> file path, served by our /media endpoint.
        # Fine for a single-process demo; use object storage in prod.
        self._media: dict[str, str] = {}
        # Public base URL for serving media. Set per-request from the inbound
        # webhook (so it picks up the live ngrok URL). Falls back to .env.
        self.media_base_url: str = settings.base_url

    @staticmethod
    def _to(to_phone: str) -> str:
        """Normalize to the WhatsApp address form: whatsapp:+234..."""
        p = (to_phone or "").strip()
        if not p.startswith("whatsapp:"):
            p = f"whatsapp:{p}" if p.startswith("+") else f"whatsapp:+{p.lstrip('+')}"
        return p

    async def send_text(self, to_phone: str, body: str) -> None:
        # WhatsApp has a 1600 character limit per message.
        # Split long messages into chunks.
        max_len = 1550  # leave a bit of headroom
        if len(body) <= max_len:
            await asyncio.to_thread(
                self._client.messages.create,
                from_=self._from, to=self._to(to_phone), body=body
            )
            log.info("WA text -> %s: %s", to_phone, body[:80])
            return

        # Split into chunks, try to break at sentence boundaries
        chunks = []
        remaining = body
        while len(remaining) > max_len:
            # Find last sentence break within limit
            cut = remaining.rfind(". ", 0, max_len)
            if cut == -1:
                cut = remaining.rfind(" ", 0, max_len)
            if cut == -1:
                cut = max_len
            chunks.append(remaining[:cut+1].strip())
            remaining = remaining[cut+1:].strip()
        if remaining:
            chunks.append(remaining)

        for i, chunk in enumerate(chunks):
            await asyncio.to_thread(
                self._client.messages.create,
                from_=self._from, to=self._to(to_phone), body=chunk
            )
            log.info("WA text -> %s [%d/%d]: %s", to_phone, i+1, len(chunks), chunk[:60])
            if i < len(chunks) - 1:
                await asyncio.sleep(0.5)

    async def send_sms(self, to_phone: str, body: str) -> None:
        """Send a plain SMS via Twilio — no sandbox restrictions like WhatsApp."""
        if not settings.twilio_sms_from:
            log.error("twilio_sms_from not configured, cannot send SMS")
            return
        p = (to_phone or "").strip()
        if not p.startswith("+"):
            p = f"+{p.lstrip('+')}"
        await asyncio.to_thread(
            self._client.messages.create,
            from_=settings.twilio_sms_from, to=p, body=body
        )
        log.info("SMS → %s: %s", p, body[:80])

    async def send_audio(self, to_phone: str, wav_bytes: bytes, ext: str = ".wav") -> None:
        """Publish the WAV and send it as a WhatsApp voice note.

        WhatsApp only accepts audio in OGG/Opus (or MP3/AAC). Gemini TTS
        produces WAV, so we transcode to .ogg/Opus via ffmpeg first.
        """
        media_id = uuid.uuid4().hex
        tmp_dir = tempfile.gettempdir()

        # write the WAV to a temp file
        wav_path = os.path.join(tmp_dir, f"pw_{media_id}{ext}")
        with open(wav_path, "wb") as f:
            f.write(wav_bytes)

        # convert WAV → OGG/Opus (WhatsApp's native voice-note format)
        ogg_path = os.path.join(tmp_dir, f"pw_{media_id}.ogg")
        ffmpeg_exe = _get_ffmpeg()
        result = subprocess.run(
            [ffmpeg_exe, "-y", "-i", wav_path, "-c:a", "libopus",
             "-b:a", "32k", "-ac", "1", "-ar", "16000", ogg_path],
            capture_output=True, timeout=30,
        )
        if result.returncode != 0 or not os.path.exists(ogg_path):
            raise RuntimeError(f"ffmpeg failed: {result.stderr.decode(errors='replace')[:300]}")

        with open(ogg_path, "rb") as f:
            ogg_bytes = f.read()
        # cleanup the intermediate wav
        try:
            os.remove(wav_path)
        except OSError:
            pass

        self._media[media_id] = ogg_path
        media_url = f"{self.media_base_url.rstrip('/')}/media/{media_id}.ogg"
        await asyncio.to_thread(
            self._client.messages.create,
            from_=self._from, to=self._to(to_phone), media_url=[media_url]
        )
        log.info("WA audio → %s (%d bytes via %s)", to_phone, len(ogg_bytes), media_url)

    def cleanup_media(self, media_id: str) -> None:
        path = self._media.pop(media_id, None)
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass


# shared singleton — instantiated at startup (see app/main.py)
whatsapp: Optional[WhatsAppSender] = None


def init_whatsapp() -> WhatsAppSender:
    global whatsapp
    whatsapp = WhatsAppSender()
    return whatsapp


def get_whatsapp() -> WhatsAppSender:
    if whatsapp is None:
        raise RuntimeError("WhatsAppSender not initialised — call init_whatsapp() at startup.")
    return whatsapp
