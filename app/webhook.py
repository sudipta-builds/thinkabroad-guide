from __future__ import annotations

import logging
from urllib.parse import parse_qs

from fastapi import APIRouter, Request, Response
from twilio.twiml.messaging_response import MessagingResponse

from app import agent
from app.config import get_settings
from app.reply import Reply
from app.twilio_wa import digits_only, remember_inbound, send_whatsapp

logger = logging.getLogger(__name__)

router = APIRouter()


async def _form_fields(request: Request) -> dict[str, str]:
    """Parse Twilio urlencoded POST without python-multipart / FastAPI Form()."""
    raw = await request.body()
    parsed = parse_qs(raw.decode("utf-8", errors="replace"), keep_blank_values=True)
    return {k: (v[0] if v else "") for k, v in parsed.items()}


@router.post("/whatsapp")
async def twilio_whatsapp_inbound(request: Request):
    """Twilio WhatsApp webhook — HTTPS URL on Render.

    Example: https://YOUR-GUIDE-SERVICE.onrender.com/whatsapp
    """
    fields = await _form_fields(request)
    phone = digits_only(fields.get("From", ""))
    body = (fields.get("Body") or "").strip()
    message_sid = fields.get("MessageSid", "")
    logger.info("Inbound WA from %s sid=%s len=%s", phone, message_sid, len(body))
    remember_inbound(phone, message_sid)

    try:
        reply = agent.handle_message(phone, body, message_sid=message_sid)
    except Exception:
        logger.exception("Guide agent failed")
        reply = Reply.of("Something went wrong on my side. Try again in a moment.", emoji="⚠️")

    if not (reply.text or "").strip() and not reply.media_urls:
        return Response(content=str(MessagingResponse()), media_type="application/xml")

    # Twilio cannot tap-back WhatsApp messages. Keep the emoji on the reply text.
    if reply.emoji and not (reply.text or "").startswith(reply.emoji):
        reply.text = f"{reply.emoji} {reply.text}".strip()

    settings = get_settings()
    if reply.media_urls and settings.twilio_ready:
        send_whatsapp(phone, reply)
        return Response(content=str(MessagingResponse()), media_type="application/xml")

    twiml = MessagingResponse()
    msg = twiml.message((reply.text or "")[:1500])
    if reply.media_urls:
        msg.media(reply.media_urls[0])
    if len(reply.text or "") > 1500 and settings.twilio_ready:
        send_whatsapp(phone, Reply(text=reply.text[1500:], media_urls=reply.media_urls[1:]))
    return Response(content=str(twiml), media_type="application/xml")
