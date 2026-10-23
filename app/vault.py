from __future__ import annotations

import base64
import hashlib
import json
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
        "Payment, saved encrypted for this number only:\n"
        "Send your UPI id, or the card number with expiry. "
        "I never store CVV or OTP. At payment I can send the page link, or fill the saved UPI or card."
    )


def save_payment(
    phone: str,
    *,
    upi: str = "",
    card: str = "",
    expiry: str = "",
    holder: str = "",
) -> str:
    """Encrypt UPI and card details for this phone. CVV and OTP are never written."""
    f = _fernet()
    if not f:
        return "Vault isn't configured yet (missing VAULT_SECRET_KEY on the server)."
    current = unlock_payment(phone) or {}
    if upi:
        current["upi"] = upi.strip()
    digits = re.sub(r"\D", "", card or "")
    if digits:
        if not 13 <= len(digits) <= 19:
            return "That card number doesn't look complete, so I didn't save it."
        current["card"] = digits
    if expiry:
        current["expiry"] = expiry.strip()
    if holder:
        current["holder"] = holder.strip()[:60]
    current.pop("cvv", None)
    current.pop("otp", None)
    if not any(current.get(key) for key in ("upi", "card", "expiry", "holder")):
        return "Nothing to save. Send a UPI id or a card number."
    label = payment_label(current)
    db.upsert_vault_secret(
        phone,
        "payment",
        f.encrypt(label.encode("utf-8")).decode("ascii"),
        f.encrypt(json.dumps(current).encode("utf-8")).decode("ascii"),
    )
    return f"Saved in your vault: {label}. CVV and OTP are never stored."


def unlock_payment(phone: str) -> dict[str, str] | None:
    f = _fernet()
    row = db.get_vault_secret(phone, "payment")
    if not f or not row:
        return None
    try:
        raw = f.decrypt(row["password_enc"].encode("ascii")).decode("utf-8")
        data = json.loads(raw)
    except (InvalidToken, json.JSONDecodeError, UnicodeError):
        logger.error("Vault payment decrypt failed for phone=…%s", phone[-4:])
        return None
    if not isinstance(data, dict):
        return None
    out = {
        key: str(data.get(key) or "").strip()
        for key in ("upi", "card", "expiry", "holder")
        if str(data.get(key) or "").strip()
    }
    return out or None


def payment_label(data: dict[str, str] | None) -> str:
    data = data or {}
    bits: list[str] = []
    if data.get("upi"):
        bits.append("UPI")
    card = re.sub(r"\D", "", data.get("card") or "")
    if card:
        bits.append(f"card ••••{card[-4:]}")
    if data.get("expiry"):
        bits.append(f"expiry {data['expiry']}")
    return ", ".join(bits) or "none"


def payment_summary(phone: str) -> str:
    return payment_label(unlock_payment(phone))
