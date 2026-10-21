from __future__ import annotations

import base64
import hashlib
import logging
import re

from cryptography.fernet import Fernet, InvalidToken

from app import db
from app.config import get_settings

logger = logging.getLogger(__name__)


def site_slug(site: str) -> str:
    slug = re.sub(r"[^a-z0-9_-]", "", (site or "").lower())[:32]
    return slug or "other"


def _fernet() -> Fernet | None:
    settings = get_settings()
    if not settings.vault_ready:
        return None
    digest = hashlib.sha256(settings.vault_secret_key.encode("utf-8")).digest()
    key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def save_login(phone: str, site: str, username: str, password: str) -> str:
    f = _fernet()
    if not f:
        return (
            "Vault isn't configured yet (missing VAULT_SECRET_KEY on the server)."
        )
    site = site_slug(site)
    username = (username or "").strip()
    password = password or ""
    if not username or not password:
        return "Send both username/email and password.\nExample: SAVE cleartrip you@email.com yourpassword"
    token_user = f.encrypt(username.encode("utf-8")).decode("ascii")
    token_pass = f.encrypt(password.encode("utf-8")).decode("ascii")
    db.upsert_vault_secret(phone, site, token_user, token_pass)
    return f"Saved {site} login, encrypted. I'll use it only when that site asks me to sign in."


def unlock_login(phone: str, site: str) -> dict[str, str] | None:
    f = _fernet()
    site = site_slug(site)
    row = db.get_vault_secret(phone, site)
    if not f or not row:
        return None
    try:
        return {
            "username": f.decrypt(row["username_enc"].encode("ascii")).decode("utf-8"),
            "password": f.decrypt(row["password_enc"].encode("ascii")).decode("utf-8"),
            "site": site,
        }
    except InvalidToken:
        logger.error("Vault decrypt failed for phone=…%s site=%s", phone[-4:], site)
        return None


def list_sites(phone: str) -> list[str]:
    return db.list_vault_sites(phone)


def vault_help() -> str:
    return (
        "Passenger details, if you want them on file:\n"
        "SAVE PROFILE Full Name | you@email.com | +phone\n\n"
        "A site login, if a page asks for one:\n"
        "SAVE cleartrip you@email.com yourpassword\n\n"
        "I stop on the payment button. I never store a card."
    )
