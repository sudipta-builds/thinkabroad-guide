"""Checkout brain: rules vs LLM/vision for stage, action, recommend, reply.

Rails still own never-pay and contact≠pay. LLM/vision owns wording and
page understanding when enabled.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_BRAIN_JSON = (
    "You are the brain for an Indian flight-booking WhatsApp agent.\n"
    "Given seller page text (and optionally a screenshot), decide the sheet and next move.\n"
    "Return JSON only:\n"
    '{"kind":"results|form|addons|review|login|otp|pay|unavailable|error|other",'
    '"action":"ask|fill|click|stop|none",'
    '"tap":"",'
    '"recommend":"",'
    '"reply":""}\n'
    "Rules:\n"
    "- kind=form for Add Contact / traveller contact with mobile/email (even if sidebar says Payment options).\n"
    "- kind=pay only when card/UPI/pay-to-complete is the main sheet — never for contact validation errors.\n"
    "- action=ask when a required field is missing (e.g. Indian mobile); do not click Continue yet.\n"
    "- action=fill when saved details can be typed; action=click only for a safe labeled control.\n"
    "- action=stop on pay. Never tap Pay/Buy/Secure my trip.\n"
    "- reply: one or two short WhatsApp sentences, plain English, end with Nothing paid.\n"
    "- recommend: short next move for the traveller or agent.\n"
)


def rules_brain(
    page_text: str,
    *,
    url: str = "",
    seller: str = "Cleartrip",
    hint: str = "",
    trip: str = "",
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Current production-style decision: regex classify + template reply."""
    from app.page_agent import classify_page, payment_sheet_open, site_error_page
    from app.step_read import format_unknown_plan, form_sheet, plan_unknown_step

    text = page_text or ""
    if site_error_page(text, url):
        return {
            "kind": "error",
            "action": "ask",
            "tap": "",
            "recommend": "search again or name another site",
            "reply": f"{seller} hit a system error for {trip or 'that trip'}. Reply search again. Nothing paid.",
            "source": "rules",
        }
    stage = classify_page(text, url)
    if form_sheet(text) and stage not in {"pay", "otp", "login", "error", "unavailable"}:
        stage = "other"
    if payment_sheet_open(text, stage) and stage == "pay":
        return {
            "kind": "pay",
            "action": "stop",
            "tap": "",
            "recommend": "pay yourself or pay with card",
            "reply": f"I stopped before payment for {trip or 'that trip'} on {seller}. Nothing paid.",
            "source": "rules",
        }
    if stage in {"results", "loading"}:
        return {
            "kind": "results",
            "action": "ask",
            "tap": "",
            "recommend": f"reply {hint or 'a departure time'} to book",
            "reply": f"Fare list is open on {seller} for {trip or 'that trip'}. Say a time and I'll book it. Nothing paid.",
            "source": "rules",
        }
    if stage == "addons":
        return {
            "kind": "addons",
            "action": "click",
            "tap": "Skip add-ons",
            "recommend": "press Skip add-ons",
            "reply": f"Choose add-ons is open on {seller}. I recommend Skip add-ons. Nothing paid.",
            "source": "rules",
        }
    if stage == "review":
        return {
            "kind": "review",
            "action": "click",
            "tap": "Continue",
            "recommend": "press Continue",
            "reply": f"Review is open on {seller}. I recommend Continue. Nothing paid.",
            "source": "rules",
        }
    plan = plan_unknown_step(text, "", profile or {}, intent="give_facts", use_model=False)
    spoken = format_unknown_plan(plan) if plan.get("handled") else ""
    if not spoken and form_sheet(text):
        spoken = (
            f"a contact or traveller form. this step needs an Indian mobile (+91). "
            f"send an Indian mobile (+91). Nothing paid."
        )
        plan = {"action": "ask", "tap": "", "recommend": "send an Indian mobile (+91)"}
    return {
        "kind": "form" if form_sheet(text) or plan.get("action") == "ask" else (stage or "other"),
        "action": str(plan.get("action") or "ask"),
        "tap": str(plan.get("tap") or ""),
        "recommend": str(plan.get("recommend") or "tell me what to press"),
        "reply": spoken
        or f"On {seller} for {trip or 'that trip'}. Send the next detail or say go on. Nothing paid.",
        "source": "rules",
    }


def llm_brain(
    page_text: str,
    *,
    url: str = "",
    seller: str = "Cleartrip",
    hint: str = "",
    trip: str = "",
    profile: dict[str, Any] | None = None,
    image_path: str = "",
) -> dict[str, Any]:
    """Gemini (vision if shot, else text) decides kind/action/recommend/reply."""
    from app import llm
    from app.config import get_settings
    from app.page_agent import _CONTACT_SHEET

    settings = get_settings()
    profile = profile or {}
    user = (
        f"Seller: {seller}. Trip: {trip or 'unknown'}. Chosen departure: {hint or 'unknown'}.\n"
        f"URL: {url or 'unknown'}.\n"
        f"Saved profile: email={bool(profile.get('email'))} phone={bool(profile.get('phone'))} "
        f"name={bool(profile.get('name'))}.\n"
        f"Page text:\n{(page_text or '')[:3500]}"
    )
    data: dict[str, Any] = {}
    if image_path and settings.vision_ready:
        data = llm.vision_converse(_BRAIN_JSON, user, image_path, timeout=28.0) or {}
    if not data and settings.gemini_api_key:
        raw = llm.gemini_chat(_BRAIN_JSON + "\n\n" + user, timeout=18.0, max_tokens=350)
        data = llm._parse_json_obj(raw) if raw else {}
    if not data and settings.llm_ready:
        data = llm.converse(_BRAIN_JSON, user, timeout=18) or {}
    if not isinstance(data, dict) or not data:
        return {}
    kind = str(data.get("kind") or "other").strip().lower()
    action = str(data.get("action") or "ask").strip().lower()
    tap = re.sub(r"\s+", " ", str(data.get("tap") or "")).strip()[:80]
    recommend = re.sub(r"\s+", " ", str(data.get("recommend") or "")).strip()[:220]
    reply = re.sub(r"\s+", " ", str(data.get("reply") or "")).strip()[:500]
    # Safety rails — contact is never pay; never recommend spending.
    if _CONTACT_SHEET.search(page_text or "") or (
        re.search(r"add contact|please enter mobile", page_text or "", re.I)
    ):
        kind = "form"
        if action in {"click", "stop"} and re.search(r"please enter mobile|mobile number", page_text or "", re.I):
            action = "ask"
            tap = ""
            recommend = "send an Indian mobile (+91)"
    if re.search(r"\b(pay now|buy|secure my trip|assure)\b", tap, re.I):
        tap = ""
        action = "stop" if kind == "pay" else "ask"
    if reply and "nothing paid" not in reply.lower():
        reply = reply.rstrip(".") + ". Nothing paid."
    if not reply:
        return {}
    return {
        "kind": kind,
        "action": action,
        "tap": tap,
        "recommend": recommend or "tell me what to press",
        "reply": reply,
        "source": "llm_vision" if image_path else "llm",
    }


def decide_checkout(
    page_text: str,
    *,
    url: str = "",
    seller: str = "Cleartrip",
    hint: str = "",
    trip: str = "",
    profile: dict[str, Any] | None = None,
    image_path: str = "",
    prefer_llm: bool = True,
) -> dict[str, Any]:
    """Prefer LLM/vision when it returns a usable decision; else rules."""
    rules = rules_brain(
        page_text, url=url, seller=seller, hint=hint, trip=trip, profile=profile
    )
    if not prefer_llm:
        return rules
    try:
        neural = llm_brain(
            page_text,
            url=url,
            seller=seller,
            hint=hint,
            trip=trip,
            profile=profile,
            image_path=image_path,
        )
    except Exception:
        logger.exception("llm_brain failed")
        neural = {}
    if not neural.get("reply"):
        return rules
    # Keep rules for hard pay/error when LLM agrees or when rails force form.
    if rules.get("kind") == "form" and neural.get("kind") == "pay":
        neural["kind"] = "form"
        neural["action"] = "ask"
        neural["tap"] = ""
        neural["recommend"] = rules.get("recommend") or neural.get("recommend")
    # Ready contact fill: keep rules action/tap/reply so WhatsApp matches the fill.
    if (
        rules.get("action") == "fill"
        and rules.get("tap")
        and neural.get("kind") in {"form", "other", "passenger"}
        and neural.get("action") in {"ask", "none", ""}
    ):
        neural["action"] = "fill"
        neural["tap"] = rules["tap"]
        neural["recommend"] = rules.get("recommend") or neural.get("recommend")
        neural["kind"] = rules.get("kind") or neural.get("kind")
        neural["reply"] = rules.get("reply") or neural.get("reply")
    # Fare list: never auto-Book from the brain alone — ask for a time / go on.
    if rules.get("kind") == "results" and neural.get("action") == "click":
        neural["kind"] = "results"
        neural["action"] = "ask"
        neural["tap"] = ""
        neural["recommend"] = rules.get("recommend") or neural.get("recommend")
        # Keep LLM phrasing only if it still asks / offers; else use rules.
        if re.search(r"\b(tapping book|proceeding to book|book now)\b", str(neural.get("reply") or ""), re.I):
            neural["reply"] = rules.get("reply") or neural.get("reply")
    return {**neural, "rules_kind": rules.get("kind"), "rules_reply": rules.get("reply")}
