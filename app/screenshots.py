from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path
from urllib.parse import quote

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

_MEDIA_DIR = Path("data/media")


def screenshot_public_url(page_url: str) -> str | None:
    """Return a publicly fetchable screenshot URL for Twilio MediaUrl.

    Prefers hosting a downloaded snapshot on Guide (/media/...) when GUIDE_PUBLIC_URL
    is set; otherwise falls back to WordPress mShots (no API key).
    """
    page_url = (page_url or "").strip()
    if not page_url.startswith("http"):
        return None

    settings = get_settings()
    public = (settings.guide_public_url or "").rstrip("/")

    # Try to download a snapshot and serve it ourselves (more reliable for Twilio)
    if public:
        local = _download_mshot(page_url)
        if local:
            return f"{public}/media/{local.name}"

    # Direct mShots URL — Twilio pulls it when sending WhatsApp media
    return mshots_url(page_url)


def mshots_url(page_url: str, *, width: int = 900) -> str:
    return f"https://s0.wp.com/mshots/v1/{quote(page_url, safe='')}/?w={width}"


def _download_mshot(page_url: str) -> Path | None:
    _MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}.jpg"
    dest = _MEDIA_DIR / name
    url = mshots_url(page_url)
    try:
        with httpx.Client(timeout=45.0, follow_redirects=True) as client:
            # Warm the shot (mShots often needs a first hit to render)
            client.get(url)
            resp = client.get(url)
        if resp.status_code >= 300 or not resp.content or len(resp.content) < 1000:
            logger.warning("mShots empty/fail for %s status=%s", page_url[:80], resp.status_code)
            return None
        ctype = (resp.headers.get("content-type") or "").lower()
        if "image" not in ctype and not resp.content[:3] in (b"\xff\xd8\xff", b"\x89PN"):
            # Still may be JPEG without header clarity — write if large enough
            if len(resp.content) < 5000:
                return None
        dest.write_bytes(resp.content)
        return dest
    except Exception as exc:
        logger.warning("Screenshot download failed: %s", exc)
        return None


def media_path(filename: str) -> Path | None:
    """Safe path resolve under data/media."""
    name = Path(filename).name
    if not re.fullmatch(r"[a-f0-9]{16,}\.(jpg|jpeg|png|webp)", name, flags=re.I):
        return None
    path = _MEDIA_DIR / name
    return path if path.is_file() else None
