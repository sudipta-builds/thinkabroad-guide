from __future__ import annotations

import base64
import hashlib
import logging
from cryptography.fernet import Fernet, InvalidToken

from app import db
from app.config import get_settings

logger = logging.getLogger(__name__)

# Supported vault labels users can save over WhatsApp
KNOWN_SITES = ("linkedin", "indeed", "glassdoor", "email", "other")


def _fernet() -> Fernet | None:
    settings = get_settings()
    if not settings.vault_ready:
        return None
    # Derive a stable Fernet key from the secret
    digest = hashlib.sha256(settings.vault_secret_key.encode("utf-8")).digest()
    key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def save_login(phone: str, site: str, username: str, password: str) -> str:
    """Store encrypted credentials for later browser apply. Never log secrets."""
    f = _fernet()
    if not f:
        return (
            "Vault isn't configured yet (missing VAULT_SECRET_KEY on the server). "
            "Ask the Guide operator to set it."
        )
    site = (site or "other").strip().lower()
    if site not in KNOWN_SITES:
        site = "other"
    username = (username or "").strip()
    password = password or ""
    if not username or not password:
        return "Send both username/email and password. Example:\nSAVE LINKEDIN me@email.com mypassword"
    token_user = f.encrypt(username.encode("utf-8")).decode("ascii")
    token_pass = f.encrypt(password.encode("utf-8")).decode("ascii")
    db.upsert_vault_secret(phone, site, token_user, token_pass)
    return (
        f"Saved *{site}* login in Guide's vault (encrypted). "
        "I'll use it only when you confirm an apply. "
        "You can overwrite anytime with another SAVE message."
    )


def has_login(phone: str, site: str = "linkedin") -> bool:
    return db.get_vault_secret(phone, site) is not None


def unlock_login(phone: str, site: str) -> dict[str, str] | None:
    """Decrypt credentials for browser apply worker. Do not put in WhatsApp replies."""
    f = _fernet()
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
        logger.error("Vault decrypt failed for phone=%s site=%s", phone[-4:], site)
        return None


def list_sites(phone: str) -> list[str]:
    return db.list_vault_sites(phone)


def vault_help() -> str:
    return (
        "To save a login for applying:\n"
        "*SAVE LINKEDIN email@x.com yourpassword*\n"
        "*SAVE INDEED email@x.com yourpassword*\n\n"
        "Passwords are encrypted in Guide's vault. Never share them in a group chat."
    )
