"""PayWise — FastAPI application entry point.

Boots in this order:
  1. Load config from .env
  2. Connect MongoDB + ensure indexes
  3. Initialise WhatsApp sender
  4. Mount routers (webhooks, media)
  5. Expose health check

Run with:  uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import os
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, PlainTextResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.db import connect, close
from app.services.whatsapp import init_whatsapp
from app.api.whatsapp_webhook import router as wa_router
from app.api.nomba_webhook import router as nomba_router
from app.api.wallet import router as wallet_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("paywise")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # ---- STARTUP ----
    log.info("🚀 PayWise starting…")
    await connect()
    init_whatsapp()
    # Pre-build the LangGraph graph so the first request isn't slow
    from app.agent.graph import get_graph
    get_graph()
    log.info("✅ PayWise ready — env=%s port=%s", settings.app_env, settings.port)
    yield
    # ---- SHUTDOWN ----
    from app.services.nomba import nomba
    await nomba.close()
    await close()
    log.info("PayWise shut down.")


app = FastAPI(
    title="PayWise",
    version="0.1.0",
    lifespan=lifespan,
)

# ---- routers ----
app.include_router(wa_router)
app.include_router(nomba_router)
app.include_router(wallet_router)


# ---- static files ----
app.mount("/static", StaticFiles(directory="app/templates"), name="static")

# ---- templates ----
templates = Jinja2Templates(directory="app/templates")


# ---- health check ----
@app.get("/health")
async def health():
    return {"status": "ok", "env": settings.app_env}


@app.get("/")
async def root(request: Request):
    return templates.TemplateResponse("landing.html", {"request": request})


# ---- media endpoint — serves generated voice notes for Twilio ----
# Twilio needs a public URL to fetch the WAV files we generate for voice-note
# replies. We write them to /tmp and serve them here.
@app.get("/media/{filename}")
async def serve_media(filename: str):
    import tempfile
    path = os.path.join(tempfile.gettempdir(), f"pw_{filename}")
    if not os.path.exists(path):
        return PlainTextResponse("not found", status_code=404)
    # WhatsApp voice notes are OGG/Opus; serve with the correct mime type
    media_type = "audio/ogg" if filename.endswith(".ogg") else "audio/wav"
    return FileResponse(path, media_type=media_type, filename=filename)
