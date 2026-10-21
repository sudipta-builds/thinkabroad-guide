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


def send_reaction(to_phone: str, message_sid: str, emoji: str) -> bool:
    """React on the inbound WhatsApp bubble. Never sends the emoji as a new text."""
    settings = get_settings()
    emoji = (emoji or "").strip()
    message_sid = (message_sid or "").strip()
    if not settings.twilio_ready or not emoji or not message_sid:
        return False
    from_num = settings.twilio_whatsapp_from
    if not from_num.startswith("whatsapp:"):
        from_num = whatsapp_address(from_num)
    auth = (settings.twilio_account_sid, settings.twilio_auth_token)
    attempts = [
        (
            f"https://api.twilio.com/2010-04-01/Accounts/{settings.twilio_account_sid}/Messages.json",
            {
                "From": from_num,
                "To": whatsapp_address(to_phone),
                "ReactionEmoji": emoji,
                "ReactionMessageSid": message_sid,
            },
        ),
        (
            f"https://api.twilio.com/2010-04-01/Accounts/{settings.twilio_account_sid}/Messages/{message_sid}/Reactions.json",
            {"Emoji": emoji},
        ),
    ]
    try:
        with httpx.Client(timeout=6.0) as client:
            for url, payload in attempts:
                resp = client.post(url, data=payload, auth=auth)
                if resp.status_code < 300:
                    return True
                logger.info("WhatsApp reaction not accepted %s", resp.status_code)
    except Exception as exc:
        logger.info("WhatsApp reaction failed: %s", type(exc).__name__)
    return False


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
