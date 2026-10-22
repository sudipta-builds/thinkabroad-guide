from __future__ import annotations

import logging
import re
import threading
from contextlib import contextmanager

import httpx

from app.config import get_settings
from app.reply import Reply

logger = logging.getLogger(__name__)

# WhatsApp clears the bubble when the message arrives, or after about 25s.
TYPING_REFRESH_SECONDS = 20
_TYPING_URL = "https://messaging.twilio.com/v2/Indicators/Typing.json"
_INBOUND: dict[str, str] = {}


def typing_form(message_sid: str) -> tuple[str, dict[str, str]]:
    """Body of Twilio's WhatsApp typing indicator. Same bubble a person gets while writing."""
    return _TYPING_URL, {"messageId": (message_sid or "").strip(), "channel": "whatsapp"}


def remember_inbound(phone: str, message_sid: str) -> None:
    sid = (message_sid or "").strip()
    if phone and sid:
        _INBOUND[digits_only(phone)] = sid


def inbound_sid(phone: str) -> str:
    return _INBOUND.get(digits_only(phone), "")


@contextmanager
def composing(phone: str):
    """Typing bubble only while the reply text is being written. The send clears it."""
    sid = inbound_sid(phone)
    stop = threading.Event()
    if sid:
        send_typing(sid)

        def _refresh() -> None:
            while not stop.is_set():
                if stop.wait(TYPING_REFRESH_SECONDS):
                    return
                send_typing(sid)

        threading.Thread(target=_refresh, name="wa-typing", daemon=True).start()
    try:
        yield
    finally:
        stop.set()


def send_typing(message_sid: str) -> bool:
    settings = get_settings()
    if settings.meta_wa_ready:
        from app.meta_wa import send_typing as meta_typing

        return meta_typing(message_sid)
    url, payload = typing_form(message_sid)
    if not settings.twilio_ready or not payload["messageId"]:
        return False
    try:
        with httpx.Client(timeout=4.0) as client:
            resp = client.post(
                url,
                data=payload,
                auth=(settings.twilio_account_sid, settings.twilio_auth_token),
            )
        if resp.status_code >= 300:
            logger.info("Typing indicator not accepted %s", resp.status_code)
            return False
        return True
    except Exception as exc:
        logger.info("Typing indicator failed: %s", type(exc).__name__)
        return False


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
    if not emoji or not message_sid:
        return False
    if settings.meta_wa_ready:
        from app.meta_wa import send_reaction as meta_reaction

        return meta_reaction(to_phone, message_sid, emoji)
    if not settings.twilio_ready:
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
    """Send text and optional media via Meta Cloud API when configured, else Twilio."""
    settings = get_settings()
    if settings.meta_wa_ready:
        from app.meta_wa import send_whatsapp as meta_send

        return meta_send(to_phone, reply)
    if not settings.twilio_ready:
        logger.warning("WhatsApp not configured (Meta or Twilio) — skip send")
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
