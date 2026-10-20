from __future__ import annotations

import re
import shutil
import uuid
from pathlib import Path

from app.config import get_settings

_MEDIA = Path("data/media")


def ensure_media_dir() -> Path:
    _MEDIA.mkdir(parents=True, exist_ok=True)
    return _MEDIA


def public_url_for(path: Path) -> str | None:
    settings = get_settings()
    public = (settings.guide_public_url or "").rstrip("/")
    if not public or not path.is_file():
        return None
    dest_dir = ensure_media_dir()
    if path.parent.resolve() != dest_dir.resolve():
        dest = dest_dir / f"{uuid.uuid4().hex[:8]}-{path.name}"
        shutil.copy2(path, dest)
        path = dest
    return f"{public}/media/{path.name}"


def media_path(filename: str) -> Path | None:
    name = Path(filename).name
    if not re.fullmatch(r"[A-Za-z0-9._-]{8,120}", name):
        return None
    path = _MEDIA / name
    return path if path.is_file() else None
