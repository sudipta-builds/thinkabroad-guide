"""Hetzner booking worker: Chromium + DataImpulse only. Render calls this over HTTP."""

from __future__ import annotations

import base64
import logging
import threading
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app import __version__
from app.browser_hands import BrowserSession, chromium_ready, ensure_chromium
from app.config import get_settings
from app.travel_agent import _book_on_seller, _seller_label

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("guide-worker")

app = FastAPI(title="Guide booking worker", version=__version__, docs_url=None, redoc_url=None)
_LOCK = threading.Lock()


class BookRequest(BaseModel):
    phone: str = ""
    text: str = ""
    state: dict[str, Any] = Field(default_factory=dict)
    private: dict[str, str] = Field(default_factory=dict)


def _check_token(authorization: str | None) -> None:
    settings = get_settings()
    expected = (settings.booking_worker_token or "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="BOOKING_WORKER_TOKEN not set on worker")
    got = (authorization or "").strip()
    if got.lower().startswith("bearer "):
        got = got[7:].strip()
    if got != expected:
        raise HTTPException(status_code=401, detail="unauthorized")


@app.on_event("startup")
def on_startup() -> None:
    settings = get_settings()
    logging.getLogger().setLevel(settings.log_level.upper())
    if not chromium_ready():
        threading.Thread(target=ensure_chromium, name="chromium-install", daemon=True).start()
    logger.info(
        "Booking worker ready v%s chromium=%s proxy=%s",
        __version__,
        chromium_ready(),
        settings.proxy_ready,
    )


@app.get("/health")
def health() -> JSONResponse:
    settings = get_settings()
    return JSONResponse(
        {
            "ok": True,
            "service": "guide-booking-worker",
            "version": __version__,
            "chromium": chromium_ready(),
            "proxy": settings.proxy_ready,
            "force_proxy": str(__import__("os").environ.get("BOOKING_FORCE_PROXY") or "").strip() in {"1", "true", "yes"},
        }
    )


@app.post("/v1/book")
def book(payload: BookRequest, authorization: str | None = Header(default=None)) -> JSONResponse:
    _check_token(authorization)
    settings = get_settings()
    if not chromium_ready():
        raise HTTPException(status_code=503, detail="chromium not ready")

    # Hetzner Germany can load Cleartrip fares direct after ~45s. DataImpulse often returns an
    # empty shell — so the worker prefers direct unless BOOKING_FORCE_PROXY=1.
    force_proxy = (getattr(settings, "booking_force_proxy", False) is True) or (
        str(__import__("os").environ.get("BOOKING_FORCE_PROXY") or "").strip() in {"1", "true", "yes"}
    )
    use_proxy = bool(settings.proxy_ready and force_proxy)
    logger.info("Worker booking use_proxy=%s force_proxy=%s", use_proxy, force_proxy)

    from app import db
    from app.travel_agent import _load, _save

    phone = (payload.phone or "worker").strip() or "worker"
    text = payload.text or ""
    state = dict(payload.state or {})
    secrets: list[str] = []

    db.init_db()
    _save(phone, state)

    replies: list[dict[str, Any]] = []
    missed = ""

    def capture_deliver(p: str, spoken: str, session: BrowserSession | None, secs: list[str]) -> None:
        shots: list[str] = []
        if session is not None and session.unsent:
            for path in session.unsent[-2:]:
                try:
                    shots.append(base64.b64encode(path.read_bytes()).decode("ascii"))
                except Exception:
                    logger.exception("Could not read screenshot %s", path)
            session.unsent.clear()
        replies.append({"text": spoken, "screenshots_b64": shots})

    import app.travel_agent as travel

    original = travel._deliver
    travel._deliver = capture_deliver  # type: ignore[assignment]
    try:
        if not _LOCK.acquire(timeout=40):
            raise HTTPException(status_code=409, detail="worker busy")
        try:
            loaded = _load(phone)
            with BrowserSession(phone, fresh=True, headed=False, use_proxy=use_proxy) as session:
                session.private = dict(payload.private or {})
                missed = _book_on_seller(phone, text, session, secrets, None) or ""
            if missed == "proxy-miss":
                seller_id = str(_load(phone).get("seller") or _load(phone).get("preferred_seller") or "cleartrip")
                if seller_id in {"google", "kayak", ""}:
                    seller_id = "cleartrip"
                label = "India proxy" if use_proxy else "booking browser"
                capture_deliver(
                    phone,
                    f"{_seller_label(seller_id)} didn't open a bookable fare on the {label}. Nothing paid. "
                    "Say that time again, or name EaseMyTrip or Ixigo.",
                    None,
                    secrets,
                )
        finally:
            _LOCK.release()
    except HTTPException:
        raise
    except Exception:
        logger.exception("Worker booking failed")
        seller_id = str(state.get("seller") or state.get("preferred_seller") or "cleartrip")
        capture_deliver(
            phone,
            f"{_seller_label(seller_id)} didn't open the booking page. Nothing paid. Say another site and I'll try that one.",
            None,
            secrets,
        )
    finally:
        travel._deliver = original  # type: ignore[assignment]

    return JSONResponse(
        {
            "ok": True,
            "missed": missed,
            "use_proxy": use_proxy,
            "state": _load(phone),
            "replies": replies,
        }
    )
