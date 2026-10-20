from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from app import __version__, db, screenshots
from app.config import get_settings
from app.webhook import router as whatsapp_router

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("guide")

app = FastAPI(title="Guide", version=__version__, docs_url=None, redoc_url=None)
app.include_router(whatsapp_router)


@app.on_event("startup")
def on_startup() -> None:
    settings = get_settings()
    logging.getLogger().setLevel(settings.log_level.upper())
    db.init_db()
    logger.info(
        "Guide ready v%s twilio=%s deepseek=%s gemini=%s vault=%s public=%s",
        __version__,
        settings.twilio_ready,
        settings.llm_ready,
        settings.search_ready,
        settings.vault_ready,
        bool(settings.guide_public_url),
    )


@app.get("/health")
def health() -> JSONResponse:
    settings = get_settings()
    return JSONResponse(
        {
            "ok": True,
            "service": "guide",
            "channel": "whatsapp",
            "version": __version__,
            "twilio": settings.twilio_ready,
            "deepseek": settings.llm_ready,
            "gemini": settings.search_ready,
            "vault": settings.vault_ready,
        }
    )


@app.get("/")
def root() -> dict:
    return {"service": "guide", "channel": "whatsapp", "health": "/health"}


@app.get("/media/{filename}")
def serve_media(filename: str):
    """Public screenshot files for Twilio WhatsApp MediaUrl."""
    path = screenshots.media_path(filename)
    if not path:
        raise HTTPException(status_code=404, detail="Not found")
    suffix = path.suffix.lower()
    media = "image/jpeg" if suffix in {".jpg", ".jpeg"} else "image/png"
    return FileResponse(path, media_type=media)


@app.get("/robots.txt")
def robots() -> PlainTextResponse:
    return PlainTextResponse("User-agent: *\nDisallow: /\n")
