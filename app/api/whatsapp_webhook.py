"""WhatsApp inbound webhook (Twilio).

This is the merchant's entire entry point. Flow:
  1. GET  /webhooks/whatsapp  — Twilio's verification handshake.
  2. POST /webhooks/whatsapp  — an inbound message arrives.
       a. If it's a voice note → reply instantly "I'm listening…" then
          download + transcribe + feed transcript as text to the agent.
       b. If it's text → feed straight to the agent.
       c. The agent runs the graph, decides reply, and we ship the reply
          (as text or voice note depending on length).

We ACK Twilio with a 200 within milliseconds; all the heavy lifting happens
in a background task so Twilio never times out.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Request, BackgroundTasks
from fastapi.responses import PlainTextResponse, JSONResponse

from app.config import settings
from app.services.whatsapp import get_whatsapp
from app.services.stt import transcribe
from app.services.tts import synthesise
from app.agent.graph import get_graph
from langchain_core.messages import HumanMessage

router = APIRouter()
log = logging.getLogger("paywise.wa_webhook")


# ===================================================================== GET
@router.get("/webhooks/whatsapp")
async def verify() -> PlainTextResponse:
    """Some WhatsApp providers do a GET challenge; Twilio doesn't strictly
    require this but it's harmless and handy for manual health pings."""
    return PlainTextResponse("ok")


# ==================================================================== POST
@router.post("/webhooks/whatsapp")
async def inbound(request: Request, bg: BackgroundTasks):
    form = await request.form()
    fields = {k: str(v) for k, v in form.items()}
    log.info("WA inbound: %s", {k: v for k, v in fields.items() if k != "MediaUrl0"})

    sender = _normalize_sender(fields.get("From", ""))
    if not sender:
        return JSONResponse({"status": "ignored", "reason": "no_sender"})

    # ---- public base URL (ngrok forwards the real host/proto in these headers) ----
    # Twilio needs an HTTPS URL it can fetch to download the voice notes we
    # generate. localhost/http won't work, so we derive the public base from the
    # incoming request instead of hard-coding it in .env.
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    base_url = f"{scheme}://{host.rstrip('/')}" if host else settings.app_base_url
    get_whatsapp().media_base_url = base_url

    # ---- branch: voice note vs text ----
    num_media = int(fields.get("NumMedia", "0") or "0")
    media_url = fields.get("MediaUrl0")
    media_type = (fields.get("MediaContentType0") or "").lower()

    is_audio = num_media > 0 and ("audio" in media_type)

    if is_audio:
        # Instant "listening" ack so the merchant isn't staring at silence
        bg.add_task(_send_ack, sender)
        bg.add_task(_handle_audio, sender, media_url, fields.get("Body", ""))
    else:
        body = fields.get("Body", "").strip()
        if not body:
            return JSONResponse({"status": "ignored", "reason": "empty_body"})
        bg.add_task(_handle_text, sender, body)

    return JSONResponse({"status": "queued"})


# =================================================================== HELPERS

def _normalize_sender(from_field: str) -> str:
    """'whatsapp:+2348012345678' -> '+2348012345678'"""
    p = (from_field or "").replace("whatsapp:", "").strip()
    return p


async def _send_ack(to: str) -> None:
    """The 'I'm listening to your voice note…' instant reply."""
    try:
        await get_whatsapp().send_text(to, "Got it 👂 I dey listen to your voice note…")
    except Exception:
        log.exception("ack send failed")


async def _handle_text(sender: str, body: str) -> None:
    """Text path: feed straight into the agent graph."""
    await _run_agent(sender, user_text=body, source="text")


async def _handle_audio(sender: str, media_url: str, caption: str) -> None:
    """Audio path: download → transcribe → feed transcript as text."""
    import httpx
    wa = get_whatsapp()

    try:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as http:
            resp = await http.get(
                media_url,
                auth=(settings.twilio_account_sid, settings.twilio_auth_token),
            )
            resp.raise_for_status()
        audio_bytes = resp.content
    except Exception:
        log.exception("audio download failed")
        await wa.send_text(sender, "I no fit download the voice note. Abeg try again.")
        return

    try:
        transcript = await transcribe(audio_bytes)
    except Exception:
        log.exception("transcribe failed")
        await wa.send_text(sender, "I no hear am well. Abeg send am again.")
        return

    if not transcript:
        await wa.send_text(sender, "I no hear anything for the voice note. Try again sir.")
        return

    await _run_agent(sender, user_text=transcript, source="voice")


async def _run_agent(sender: str, user_text: str, source: str) -> None:
    """Run the graph for this sender and deliver its reply.

    Each merchant gets one persistent thread (by phone), so the agent remembers
    across messages — critical for the confirmation loop.
    """
    config = {"configurable": {"thread_id": f"merchant:{sender}"}}
    graph = get_graph()

    init_state = {
        "messages": [HumanMessage(content=user_text)],
        "merchant_phone": sender,
        "merchant_lang": "pidgin",
        "source": source,
        # Do NOT set awaiting_confirmation here — the checkpointer restores it
        # from the previous turn. Overwriting it to False breaks the confirm flow.
    }

    try:
        # Quick "thinking" status so the user knows we're processing
        if source == "voice":
            await get_whatsapp().send_text(sender, "🧠 I dey think about wetin you talk…")
        final_state = await graph.ainvoke(init_state, config=config)
    except Exception:
        log.exception("agent run failed")
        await get_whatsapp().send_text(
            sender, "Something go wrong for my side. Abeg try again."
        )
        return

    await _deliver_reply(sender, final_state)


async def _deliver_reply(sender: str, final_state: dict) -> None:
    """Send the agent's final reply — text or voice note by length."""
    text = final_state.get("reply_text") or ""
    is_long = final_state.get("reply_is_long", False)

    if not text:
        log.warning("agent produced no reply_text for %s", sender)
        return

    wa = get_whatsapp()
    if is_long:
        # Long reply → synthesize a voice note in the merchant's language
        lang = final_state.get("merchant_lang", "pidgin")
        try:
            await wa.send_text(sender, "🎙️ I dey record the voice note now…")
            audio = await asyncio.to_thread(synthesise, text, lang)
            await wa.send_audio(sender, audio, ext=".wav")
            return
        except Exception:
            log.exception("TTS failed, falling back to text")
    await wa.send_text(sender, text)

