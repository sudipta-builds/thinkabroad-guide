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
    db_path = db.resolve_db_path()
    db.init_db()
    from app.browser_hands import chromium_ready, ensure_chromium
    from app.travel_agent import start_price_watch

    browser_ok = chromium_ready()
    if not browser_ok:
        import threading

        threading.Thread(target=ensure_chromium, name="chromium-install", daemon=True).start()
    start_price_watch()
    logger.info(
        "Guide travel agent ready v%s db=%s meta_wa=%s twilio=%s deepseek=%s gemini=%s vault=%s public=%s chromium=%s",
        __version__,
        db_path,
        settings.meta_wa_ready,
        settings.twilio_ready,
        settings.llm_ready,
        settings.search_ready,
        settings.vault_ready,
        bool(settings.guide_public_url),
        browser_ok,
    )


@app.get("/health")
def health() -> JSONResponse:
    from app.browser_hands import chromium_ready

    settings = get_settings()
    db_path = db.resolve_db_path()
    return JSONResponse(
        {
            "ok": True,
            "service": "guide",
            "agent": "travel",
            "channel": "whatsapp",
            "version": __version__,
            "meta_wa": settings.meta_wa_ready,
            "meta_verify": settings.meta_verify_ready,
            "twilio": settings.twilio_ready,
            "deepseek": settings.llm_ready,
            "gemini": settings.search_ready,
            "vault": settings.vault_ready,
            "chromium": chromium_ready(),
            "memory": "persistent" if str(db_path).startswith("/var/data") else "ephemeral",
            "db": str(db_path),
        }
    )


@app.get("/")
def root() -> dict:
    return {"service": "guide", "agent": "travel", "channel": "whatsapp", "health": "/health"}


@app.get("/media/{filename}")
def serve_media(filename: str):
    """Public screenshots + CV files for Twilio WhatsApp MediaUrl."""
    path = screenshots.media_path(filename)
    if not path:
        raise HTTPException(status_code=404, detail="Not found")
    suffix = path.suffix.lower()
    media = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }.get(suffix, "application/octet-stream")
    return FileResponse(path, media_type=media)


@app.get("/robots.txt")
def robots() -> PlainTextResponse:
    return PlainTextResponse("User-agent: *\nDisallow: /\n")
