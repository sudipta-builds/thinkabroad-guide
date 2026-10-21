"""Meta WhatsApp Cloud API — verify webhook, receive, send."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import get_settings
from app.reply import Reply
from app.twilio_wa import digits_only

logger = logging.getLogger(__name__)


def verify_challenge(mode: str, token: str, challenge: str) -> str | None:
    """Return challenge text if Meta subscription verify succeeds."""
    settings = get_settings()
    expected = (settings.whatsapp_verify_token or "").strip()
    if (mode or "").strip() != "subscribe":
        return None
    if not expected or (token or "").strip() != expected:
        return None
    return challenge or ""


def parse_inbound(payload: dict[str, Any]) -> list[dict[str, str]]:
    """Extract inbound user text messages from a Meta webhook body."""
    out: list[dict[str, str]] = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            contacts = {c.get("wa_id"): c for c in (value.get("contacts") or []) if c.get("wa_id")}
            for msg in value.get("messages") or []:
                if (msg.get("type") or "") != "text":
                    # Still accept button/interactive text later; text-only for v1
                    body = ""
                    if msg.get("type") == "button":
                        body = ((msg.get("button") or {}).get("text") or "").strip()
                    elif msg.get("type") == "interactive":
                        interactive = msg.get("interactive") or {}
                        body = (
                            ((interactive.get("button_reply") or {}).get("title") or "")
                            or ((interactive.get("list_reply") or {}).get("title") or "")
                        ).strip()
                    else:
                        continue
                else:
                    body = ((msg.get("text") or {}).get("body") or "").strip()
                wa_id = (msg.get("from") or "").strip()
                mid = (msg.get("id") or "").strip()
                if not wa_id or not body:
                    continue
                out.append(
                    {
                        "phone": digits_only(wa_id),
                        "body": body,
                        "message_id": mid,
                        "name": ((contacts.get(wa_id) or {}).get("profile") or {}).get("name") or "",
                    }
                )
    return out


def _messages_url() -> str:
    settings = get_settings()
    ver = (settings.whatsapp_graph_version or "v21.0").strip().lstrip("/")
    return f"https://graph.facebook.com/{ver}/{settings.whatsapp_phone_number_id}/messages"


def _headers() -> dict[str, str]:
    settings = get_settings()
    return {
        "Authorization": f"Bearer {settings.whatsapp_token}",
        "Content-Type": "application/json",
    }


def send_typing(message_id: str) -> bool:
    settings = get_settings()
    mid = (message_id or "").strip()
    if not settings.meta_wa_ready or not mid:
        return False
    payload = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": mid,
        "typing_indicator": {"type": "text"},
    }
    try:
        with httpx.Client(timeout=6.0) as client:
            resp = client.post(_messages_url(), headers=_headers(), json=payload)
        if resp.status_code >= 300:
            logger.info("Meta typing not accepted %s %s", resp.status_code, resp.text[:200])
            return False
        return True
    except Exception as exc:
        logger.info("Meta typing failed: %s", type(exc).__name__)
        return False


def send_whatsapp(to_phone: str, reply: Reply) -> bool:
    settings = get_settings()
    if not settings.meta_wa_ready:
        return False
    phone = digits_only(to_phone)
    if len(phone) < 8:
        return False

    text = (reply.text or "").strip()
    if reply.emoji and text and not text.startswith(reply.emoji):
        text = f"{reply.emoji} {text}".strip()

    ok_any = False
    # Cap like Twilio path — chunk long text
    chunks: list[str] = []
    remaining = text
    while remaining:
        chunks.append(remaining[:4000])
        remaining = remaining[4000:]
    if not chunks and reply.media_urls:
        chunks = [""]

    for i, chunk in enumerate(chunks or [""]):
        media = reply.media_urls[i] if i < len(reply.media_urls) else (reply.media_urls[0] if i == 0 and reply.media_urls else "")
        if media and i == 0:
            # Send image (or document) with optional caption on first chunk
            lower = media.lower()
            if any(lower.endswith(ext) for ext in (".pdf", ".docx")):
                payload: dict[str, Any] = {
                    "messaging_product": "whatsapp",
                    "to": phone,
                    "type": "document",
                    "document": {"link": media, "filename": media.rsplit("/", 1)[-1]},
                }
                if chunk:
                    payload["document"]["caption"] = chunk[:1024]
            else:
                payload = {
                    "messaging_product": "whatsapp",
                    "to": phone,
                    "type": "image",
                    "image": {"link": media},
                }
                if chunk:
                    payload["image"]["caption"] = chunk[:1024]
            if _post(payload):
                ok_any = True
            # Extra media without new caption
            for extra in reply.media_urls[1:]:
                el = extra.lower()
                if any(el.endswith(ext) for ext in (".pdf", ".docx")):
                    _post(
                        {
                            "messaging_product": "whatsapp",
                            "to": phone,
                            "type": "document",
                            "document": {"link": extra, "filename": extra.rsplit("/", 1)[-1]},
                        }
                    )
                else:
                    _post(
                        {
                            "messaging_product": "whatsapp",
                            "to": phone,
                            "type": "image",
                            "image": {"link": extra},
                        }
                    )
        elif chunk:
            if _post(
                {
                    "messaging_product": "whatsapp",
                    "to": phone,
                    "type": "text",
                    "text": {"preview_url": True, "body": chunk},
                }
            ):
                ok_any = True
    return ok_any


def _post(payload: dict[str, Any]) -> bool:
    try:
        with httpx.Client(timeout=20.0) as client:
            resp = client.post(_messages_url(), headers=_headers(), json=payload)
        if resp.status_code >= 300:
            logger.warning("Meta WA send failed %s %s", resp.status_code, resp.text[:400])
            return False
        return True
    except Exception as exc:
        logger.warning("Meta WA send error: %s", exc)
        return False
