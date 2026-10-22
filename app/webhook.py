from __future__ import annotations

import logging
from urllib.parse import parse_qs

from fastapi import APIRouter, BackgroundTasks, Query, Request, Response
from fastapi.responses import PlainTextResponse
from twilio.twiml.messaging_response import MessagingResponse

from app import agent
from app.config import get_settings
from app import meta_wa
from app.reply import Reply, for_send
from app.twilio_wa import digits_only, remember_inbound, send_whatsapp

logger = logging.getLogger(__name__)

router = APIRouter()


async def _form_fields(request: Request) -> dict[str, str]:
    """Parse Twilio urlencoded POST without python-multipart / FastAPI Form()."""
    raw = await request.body()
    parsed = parse_qs(raw.decode("utf-8", errors="replace"), keep_blank_values=True)
    return {k: (v[0] if v else "") for k, v in parsed.items()}


def _build_reply(phone: str, body: str, message_sid: str) -> Reply:
    remember_inbound(phone, message_sid)
    try:
        return agent.handle_message(phone, body, message_sid=message_sid)
    except Exception:
        logger.exception("Guide agent failed")
        return Reply.of("Something went wrong on my side. Try again in a moment.", emoji="⚠️")


# ── Meta Cloud API (preferred) ─────────────────────────────────────────


@router.get("/webhook/whatsapp")
async def meta_whatsapp_verify(
    hub_mode: str = Query("", alias="hub.mode"),
    hub_verify_token: str = Query("", alias="hub.verify_token"),
    hub_challenge: str = Query("", alias="hub.challenge"),
):
    """Meta subscription verification — Callback URL must hit this GET."""
    challenge = meta_wa.verify_challenge(hub_mode, hub_verify_token, hub_challenge)
    if challenge is None:
        logger.warning("Meta webhook verify rejected mode=%s", hub_mode)
        return Response(content="Forbidden", status_code=403)
    logger.info("Meta webhook verified")
    return PlainTextResponse(content=challenge)


@router.post("/webhook/whatsapp")
async def meta_whatsapp_inbound(request: Request, background_tasks: BackgroundTasks):
    """Meta WhatsApp inbound. Reply asynchronously via Graph API."""
    try:
        payload = await request.json()
    except Exception:
        return Response(content="Bad Request", status_code=400)

    # Status-only webhooks (delivery receipts) — ack quickly
    messages = meta_wa.parse_inbound(payload if isinstance(payload, dict) else {})
    if not messages:
        return Response(content="EVENT_RECEIVED", status_code=200)

    settings = get_settings()

    def _handle_one(phone: str, body: str, message_id: str) -> None:
        logger.info("Inbound Meta WA from %s id=%s len=%s", phone, message_id, len(body))
        reply = _build_reply(phone, body, message_id)
        reacted = False
        if reply.emoji and message_id:
            from app.twilio_wa import send_reaction

            reacted = send_reaction(phone, message_id, reply.emoji)
        reply = for_send(reply, reacted=reacted)
        if not (reply.text or "").strip() and not reply.media_urls:
            return
        if settings.meta_wa_ready:
            send_whatsapp(phone, reply)
        else:
            logger.warning("Meta inbound received but WHATSAPP_TOKEN / PHONE_NUMBER_ID not set")

    for msg in messages:
        background_tasks.add_task(_handle_one, msg["phone"], msg["body"], msg["message_id"])

    return Response(content="EVENT_RECEIVED", status_code=200)


# ── Twilio (legacy) ─────────────────────────────────────────────────────


@router.post("/whatsapp")
async def twilio_whatsapp_inbound(request: Request):
    """Twilio WhatsApp webhook — HTTPS URL on Render.

    Example: https://YOUR-GUIDE-SERVICE.onrender.com/whatsapp
    """
    fields = await _form_fields(request)
    phone = digits_only(fields.get("From", ""))
    body = (fields.get("Body") or "").strip()
    message_sid = fields.get("MessageSid", "")
    logger.info("Inbound Twilio WA from %s sid=%s len=%s", phone, message_sid, len(body))

    reply = _build_reply(phone, body, message_sid)

    settings = get_settings()
    reacted = False
    if reply.emoji and message_sid:
        from app.twilio_wa import send_reaction

        reacted = send_reaction(phone, message_sid, reply.emoji)
    reply = for_send(reply, reacted=reacted)

    if not (reply.text or "").strip() and not reply.media_urls:
        return Response(content=str(MessagingResponse()), media_type="application/xml")

    if reply.media_urls and (settings.meta_wa_ready or settings.twilio_ready):
        send_whatsapp(phone, reply)
        return Response(content=str(MessagingResponse()), media_type="application/xml")

    twiml = MessagingResponse()
    msg = twiml.message((reply.text or "")[:1500])
    if reply.media_urls:
        msg.media(reply.media_urls[0])
    if len(reply.text or "") > 1500 and (settings.meta_wa_ready or settings.twilio_ready):
        send_whatsapp(phone, Reply(text=reply.text[1500:], media_urls=reply.media_urls[1:]))
    return Response(content=str(twiml), media_type="application/xml")
