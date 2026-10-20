from __future__ import annotations

import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken

from app import db
from app.config import get_settings

logger = logging.getLogger(__name__)

KNOWN_SITES = ("linkedin", "indeed", "glassdoor", "email", "other")


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
    site = (site or "other").strip().lower()
    if site not in KNOWN_SITES:
        site = "other"
    username = (username or "").strip()
    password = password or ""
    if not username or not password:
        return "Send both username/email and password.\nExample: SAVE LINKEDIN me@email.com mypassword"
    token_user = f.encrypt(username.encode("utf-8")).decode("ascii")
    token_pass = f.encrypt(password.encode("utf-8")).decode("ascii")
    db.upsert_vault_secret(phone, site, token_user, token_pass)
    return (
        f"Saved *{site}* login (encrypted). Used only when you reply *YES* to apply."
    )


def unlock_login(phone: str, site: str) -> dict[str, str] | None:
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
        logger.error("Vault decrypt failed for phone=…%s site=%s", phone[-4:], site)
        return None


def list_sites(phone: str) -> list[str]:
    return db.list_vault_sites(phone)


def vault_help() -> str:
    return (
        "Before I can apply for you, save:\n\n"
        "1) Profile (name | email | phone):\n"
        "*SAVE PROFILE Ada Lovelace | ada@email.com | +441234567890*\n\n"
        "2) Optional site login:\n"
        "*SAVE LINKEDIN ada@email.com yourpassword*\n\n"
        "Then search jobs → *APPLY N* → *YES*."
    )
