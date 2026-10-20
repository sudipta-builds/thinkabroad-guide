from __future__ import annotations

import logging
import re

import httpx

from app.config import get_settings
from app.reply import Reply

logger = logging.getLogger(__name__)


def digits_only(raw: str) -> str:
    digits = re.sub(r"\D", "", (raw or "").strip())
    if digits.startswith("00"):
        digits = digits[2:]
    return digits


def whatsapp_address(phone: str) -> str:
    value = (phone or "").strip()
    if value.startswith("whatsapp:"):
        return value
    return f"whatsapp:+{digits_only(value).lstrip('+')}"


def send_whatsapp_text(to_phone: str, body: str) -> bool:
    return send_whatsapp(to_phone, Reply(text=body))


def send_whatsapp(to_phone: str, reply: Reply) -> bool:
    """Send text and optional media (screenshots) via Twilio REST."""
    settings = get_settings()
    if not settings.twilio_ready:
        logger.warning("Twilio not configured — skip send")
        return False
    from_num = settings.twilio_whatsapp_from
    if not from_num.startswith("whatsapp:"):
        from_num = whatsapp_address(from_num)

    body = (reply.text or "")[:4000]
    media = [u for u in (reply.media_urls or []) if u][:3]  # WhatsApp practical limit

    # First message: text + first image
    payload: dict[str, str] = {
        "From": from_num,
        "To": whatsapp_address(to_phone),
        "Body": body or (" " if media else ""),
    }
    if media:
        payload["MediaUrl"] = media[0]

    ok = _post_message(settings.twilio_account_sid, settings.twilio_auth_token, payload)

    # Extra images as follow-ups
    for url in media[1:]:
        extra = {
            "From": from_num,
            "To": whatsapp_address(to_phone),
            "Body": " ",
            "MediaUrl": url,
        }
        _post_message(settings.twilio_account_sid, settings.twilio_auth_token, extra)

    # Long text remainder
    if len(reply.text or "") > 4000:
        _post_message(
            settings.twilio_account_sid,
            settings.twilio_auth_token,
            {
                "From": from_num,
                "To": whatsapp_address(to_phone),
                "Body": reply.text[4000:8000],
            },
        )
    return ok


def _post_message(sid: str, token: str, payload: dict[str, str]) -> bool:
    try:
        with httpx.Client(timeout=20.0) as client:
            resp = client.post(
                f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
                data=payload,
                auth=(sid, token),
            )
        if resp.status_code >= 300:
            logger.warning("Twilio send failed %s: %s", resp.status_code, resp.text[:300])
            return False
        return True
    except Exception as exc:
        logger.warning("Twilio send error: %s", exc)
        return False
