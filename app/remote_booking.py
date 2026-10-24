"""Call the Hetzner Chromium worker from Render for Cleartrip checkout."""

from __future__ import annotations

import base64
import logging
import uuid
from pathlib import Path
from typing import Any

import httpx

from app.config import get_settings
from app.media_files import ensure_media_dir

logger = logging.getLogger(__name__)


def worker_configured() -> bool:
    settings = get_settings()
    return bool((settings.booking_worker_url or "").strip() and (settings.booking_worker_token or "").strip())


def run_remote_booking(
    *,
    phone: str,
    text: str,
    state: dict[str, Any],
    private: dict[str, str] | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    base = (settings.booking_worker_url or "").rstrip("/")
    token = (settings.booking_worker_token or "").strip()
    if not base or not token:
        raise RuntimeError("booking worker is not configured")
    url = f"{base}/v1/book"
    # Cleartrip checkout can take ~4 minutes on residential.
    timeout = httpx.Timeout(connect=30.0, read=360.0, write=60.0, pool=30.0)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body = {"phone": phone, "text": text, "state": state, "private": private or {}}
    logger.info("Dispatching booking to worker %s", base)
    with httpx.Client(timeout=timeout) as client:
        response = client.post(url, json=body, headers=headers)
        response.raise_for_status()
        return response.json()


def materialize_screenshots(shots_b64: list[str]) -> list[Path]:
    paths: list[Path] = []
    media = ensure_media_dir()
    for raw in shots_b64 or []:
        try:
            data = base64.b64decode(raw)
        except Exception:
            continue
        path = media / f"shot-{uuid.uuid4().hex[:12]}.jpg"
        path.write_bytes(data)
        paths.append(path)
    return paths
