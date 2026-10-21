from __future__ import annotations

import re
import shutil
import uuid
from pathlib import Path

from app.config import get_settings

def media_dirs() -> list[Path]:
    dirs = [Path("data/media")]
    try:
        extra = Path(get_settings().guide_db_path).parent / "media"
    except Exception:
        extra = None
    if extra is not None and str(extra) not in {str(path) for path in dirs}:
        dirs.append(extra)
    return dirs


def ensure_media_dir() -> Path:
    for folder in media_dirs():
        try:
            folder.mkdir(parents=True, exist_ok=True)
            probe = folder / ".write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return folder
        except OSError:
            continue
    fallback = Path("data/media")
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


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
    for folder in media_dirs():
        path = folder / name
        if path.is_file():
            return path
    return None
