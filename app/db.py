from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from app.config import get_settings

_lock = threading.Lock()
_resolved_db_path: Path | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def choose_db_path(configured: Path, *, project: Path, mount: Path | None) -> Path:
    """A file inside the repo is replaced on every Render deploy. The disk mount is not."""
    if mount is not None:
        inside_project = False
        if configured.is_absolute():
            try:
                inside_project = configured.resolve().is_relative_to(project.resolve())
            except (OSError, ValueError):
                inside_project = False
        if not configured.is_absolute() or inside_project:
            return mount / "guide.db"
    return configured


def _mount_dir() -> Path | None:
    mount = Path("/var/data")
    if mount.is_dir() and os.access(mount, os.W_OK):
        return mount
    return None


def resolve_db_path() -> Path:
    """Pick a writable SQLite path. /var/data needs a Render disk mount."""
    global _resolved_db_path
    if _resolved_db_path is not None:
        return _resolved_db_path

    project = Path(__file__).resolve().parents[1]
    configured = Path(get_settings().guide_db_path or "data/guide.db")
    mount = _mount_dir()
    chosen = choose_db_path(configured, project=project, mount=mount)
    if mount is not None and chosen.parent == mount and not chosen.exists():
        legacy = project / "data" / "guide.db"
        if legacy.is_file() and legacy.stat().st_size > 0:
            try:
                chosen.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(legacy, chosen)
            except OSError:
                pass
    candidates = [chosen, configured, Path("data/guide.db"), Path("/tmp/guide.db")]
    # de-dupe while preserving order
    seen: set[str] = set()
    ordered: list[Path] = []
    for p in candidates:
        key = str(p)
        if key not in seen:
            seen.add(key)
            ordered.append(p)

    last_err: Exception | None = None
    for path in ordered:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # prove writable
            probe = path.parent / ".guide_write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            _resolved_db_path = path
            return path
        except OSError as exc:
            last_err = exc
            continue
    raise PermissionError(
        f"No writable DB path. Tried {[str(p) for p in ordered]}. Last error: {last_err}"
    )


def _connect() -> sqlite3.Connection:
    path = resolve_db_path()
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with _lock:
        conn = _connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    phone TEXT PRIMARY KEY,
                    display_name TEXT,
                    interests_json TEXT NOT NULL DEFAULT '{}',
                    cv_markdown TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phone TEXT NOT NULL,
                    role TEXT NOT NULL,
                    body TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    company TEXT NOT NULL,
                    country TEXT NOT NULL,
                    city TEXT,
                    apply_url TEXT,
                    sponsor_status TEXT NOT NULL DEFAULT 'likely',
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    description TEXT,
                    source TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS applications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phone TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    draft_cover TEXT,
                    notes TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(phone, job_id)
                );

                CREATE TABLE IF NOT EXISTS vault_secrets (
                    phone TEXT NOT NULL,
                    site TEXT NOT NULL,
                    username_enc TEXT NOT NULL,
                    password_enc TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (phone, site)
                );

                CREATE INDEX IF NOT EXISTS idx_messages_phone ON messages(phone);
                CREATE INDEX IF NOT EXISTS idx_jobs_country ON jobs(country);
                CREATE INDEX IF NOT EXISTS idx_apps_phone ON applications(phone);
                """
            )
            conn.commit()
        finally:
            conn.close()


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    with _lock:
        conn = _connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def upsert_user(phone: str) -> dict[str, Any]:
    now = _utc_now()
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE phone = ?", (phone,)).fetchone()
        if row:
            return dict(row)
        conn.execute(
            "INSERT INTO users (phone, interests_json, created_at, updated_at) VALUES (?, '{}', ?, ?)",
            (phone, now, now),
        )
        row = conn.execute("SELECT * FROM users WHERE phone = ?", (phone,)).fetchone()
        return dict(row)


def list_users() -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute("SELECT phone, interests_json, display_name FROM users").fetchall()
    return [dict(row) for row in rows]


def get_user(phone: str) -> dict[str, Any] | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE phone = ?", (phone,)).fetchone()
        return dict(row) if row else None


def update_user(
    phone: str,
    *,
    display_name: str | None = None,
    interests: dict[str, Any] | None = None,
    cv_markdown: str | None = None,
) -> None:
    now = _utc_now()
    user = upsert_user(phone)
    name = display_name if display_name is not None else user.get("display_name")
    interests_json = (
        json.dumps(interests) if interests is not None else user.get("interests_json") or "{}"
    )
    cv = cv_markdown if cv_markdown is not None else user.get("cv_markdown")
    with db() as conn:
        conn.execute(
            """
            UPDATE users
            SET display_name = ?, interests_json = ?, cv_markdown = ?, updated_at = ?
            WHERE phone = ?
            """,
            (name, interests_json, cv, now, phone),
        )


def add_message(phone: str, role: str, body: str) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO messages (phone, role, body, created_at) VALUES (?, ?, ?, ?)",
            (phone, role, body, _utc_now()),
        )


def recent_messages(phone: str, limit: int = 20) -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT role, body, created_at FROM messages
            WHERE phone = ?
            ORDER BY id DESC LIMIT ?
            """,
            (phone, limit),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


def upsert_job(job: dict[str, Any]) -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO jobs (
                id, title, company, country, city, apply_url, sponsor_status,
                tags_json, description, source, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title = excluded.title,
                company = excluded.company,
                country = excluded.country,
                city = excluded.city,
                apply_url = excluded.apply_url,
                sponsor_status = excluded.sponsor_status,
                tags_json = excluded.tags_json,
                description = excluded.description,
                source = excluded.source
            """,
            (
                job["id"],
                job["title"],
                job["company"],
                job["country"],
                job.get("city"),
                job.get("apply_url"),
                job.get("sponsor_status", "likely"),
                json.dumps(job.get("tags") or []),
                job.get("description"),
                job.get("source"),
                job.get("created_at") or _utc_now(),
            ),
        )


def list_jobs(countries: list[str] | None = None, limit: int = 50) -> list[dict[str, Any]]:
    with db() as conn:
        if countries:
            placeholders = ",".join("?" for _ in countries)
            rows = conn.execute(
                f"SELECT * FROM jobs WHERE lower(country) IN ({placeholders}) ORDER BY created_at DESC LIMIT ?",
                [*[c.lower() for c in countries], limit],
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        try:
            d["tags"] = json.loads(d.get("tags_json") or "[]")
        except json.JSONDecodeError:
            d["tags"] = []
        out.append(d)
    return out


def upsert_application(
    phone: str,
    job_id: str,
    status: str,
    *,
    draft_cover: str | None = None,
    notes: str | None = None,
) -> None:
    now = _utc_now()
    with db() as conn:
        existing = conn.execute(
            "SELECT id FROM applications WHERE phone = ? AND job_id = ?",
            (phone, job_id),
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE applications
                SET status = ?, draft_cover = COALESCE(?, draft_cover),
                    notes = COALESCE(?, notes), updated_at = ?
                WHERE phone = ? AND job_id = ?
                """,
                (status, draft_cover, notes, now, phone, job_id),
            )
        else:
            conn.execute(
                """
                INSERT INTO applications
                    (phone, job_id, status, draft_cover, notes, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (phone, job_id, status, draft_cover, notes, now, now),
            )


def list_applications(phone: str) -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT a.*, j.title, j.company, j.country, j.apply_url
            FROM applications a
            LEFT JOIN jobs j ON j.id = a.job_id
            WHERE a.phone = ?
            ORDER BY a.updated_at DESC
            """,
            (phone,),
        ).fetchall()
    return [dict(r) for r in rows]


def upsert_vault_secret(phone: str, site: str, username_enc: str, password_enc: str) -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO vault_secrets (phone, site, username_enc, password_enc, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(phone, site) DO UPDATE SET
                username_enc = excluded.username_enc,
                password_enc = excluded.password_enc,
                updated_at = excluded.updated_at
            """,
            (phone, site, username_enc, password_enc, _utc_now()),
        )


def get_vault_secret(phone: str, site: str) -> dict[str, Any] | None:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM vault_secrets WHERE phone = ? AND site = ?",
            (phone, site),
        ).fetchone()
    return dict(row) if row else None


def list_vault_sites(phone: str) -> list[str]:
    with db() as conn:
        rows = conn.execute(
            "SELECT site FROM vault_secrets WHERE phone = ? ORDER BY site",
            (phone,),
        ).fetchall()
    return [str(r["site"]) for r in rows]


def get_job(job_id: str) -> dict[str, Any] | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["tags"] = json.loads(d.get("tags_json") or "[]")
    except json.JSONDecodeError:
        d["tags"] = []
    return d
