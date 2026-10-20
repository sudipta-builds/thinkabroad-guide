from __future__ import annotations

import logging

from fastapi import APIRouter, Form, Request, Response
from twilio.twiml.messaging_response import MessagingResponse

from app import agent
from app.config import get_settings
from app.reply import Reply
from app.twilio_wa import digits_only, send_whatsapp

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/whatsapp")
async def twilio_whatsapp_inbound(
    request: Request,
    From: str = Form(default=""),
    Body: str = Form(default=""),
    MessageSid: str = Form(default=""),
):
    """Twilio WhatsApp webhook — HTTPS URL on Render.

    Example: https://YOUR-GUIDE-SERVICE.onrender.com/whatsapp
    """
    _ = request
    phone = digits_only(From)
    body = (Body or "").strip()
    logger.info("Inbound WA from %s sid=%s len=%s", phone, MessageSid, len(body))

    try:
        reply = agent.handle_message(phone, body)
    except Exception:
        logger.exception("Guide agent failed")
        reply = Reply.of("Something went wrong on my side. Try again in a moment.")

    settings = get_settings()
    # Prefer REST when media (screenshots) is present — TwiML media is flaky with multiple URLs
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
