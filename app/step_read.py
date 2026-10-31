"""Read a booking step from the page text. The model names it; we do not list every label.

The tap it returns is used only when that exact phrase is on the page and it is not a purchase.
"""

from __future__ import annotations

import os
import re

from app.config import get_settings

_READ = (
    "You are looking at one live flight-booking page. The text is what the browser shows. "
    "You have not been given a list of steps. Name this step from these words only.\n"
    'Return JSON only: {"kind":"upsell|form|login|otp|review|results|pay|other",'
    '"what":"","why":"","recommend":"","tap":""}\n'
    "what, why, and recommend are one short sentence each, in plain English.\n"
    "tap is the exact short button label to press, copied from the page, or empty.\n"
    "If the page is selling an optional extra, tap the decline control that is actually written there, "
    "whatever its words are.\n"
    "Never set tap to Pay, Pay now, Secure My Trip, Buy This, Buy, Purchase, or any control that spends money.\n"
    "If this is the payment page, kind is pay and tap is empty.\n"
    "Do not invent a button, a price, or a flight that is not in the text."
)

_SPEND = re.compile(
    r"\b(pay now|pay to complete|make payment|secure my trip|buy this|buy now|purchase|add to trip)\b",
    re.I,
)


def step_read_enabled() -> bool:
    if os.getenv("GUIDE_STEP_READ", "1").strip().lower() in {"0", "false", "no"}:
        return False
    return bool(get_settings().llm_ready)


def safe_tap(tap: str, page: str) -> str:
    """A click the model named, only if the page shows it and it does not spend money."""
    label = re.sub(r"\s+", " ", (tap or "").strip())
    if not label or len(label) > 40 or _SPEND.search(label):
        return ""
    if label.lower() not in re.sub(r"\s+", " ", page or "").lower():
        return ""
    return label


def describe_step(heard: dict[str, str]) -> str:
    """What to tell the traveller. Nothing is tapped until they answer."""
    recommend = heard.get("recommend") or "tell me what you want to do"
    tap = heard.get("tap") or ""
    if tap:
        nxt = f"Reply yes and I'll press {tap}. Or send the exact button you want."
    else:
        nxt = "Send the exact button you want pressed."
    return (
        f"Stuck: {heard.get('what')}. That's {heard.get('why')}. "
        f"I recommend {recommend}. {nxt} I haven't tapped anything. Nothing paid."
    )


def accept_choice(text: str, pending: dict[str, str]) -> str:
    """The button the traveller just agreed to. Empty until they choose.

    yes/skip/ok uses the recommended label. Any other short phrase is used only
    when that phrase is on the page. Pay controls are never accepted here.
    """
    page = pending.get("page") or ""
    raw = re.sub(r"\s+", " ", (text or "").strip())
    if not raw or not page:
        return ""
    if re.search(r"\b(pay now|pay to complete|make payment)\b", raw, re.I):
        return ""
    if re.fullmatch(
        r"(yes|yep|yeah|ok|okay|sure|skip|skip it|do that|do it|go on|go ahead|no thanks|no, thanks)",
        raw,
        re.I,
    ):
        return safe_tap(str(pending.get("tap") or ""), page)
    if pending.get("kind") == "upsell" and re.fullmatch(r"continue", raw, re.I):
        return ""
    if 2 <= len(raw) <= 40 and raw.lower() in re.sub(r"\s+", " ", page).lower():
        return raw
    return ""


def read_step(page: str, *, seller: str = "", hint: str = "") -> dict[str, str] | None:
    """Ask the model what this page is. None when the model is off or the answer is empty."""
    if not step_read_enabled():
        return None
    from app import llm

    data = llm.converse(
        _READ,
        f"Seller: {seller or 'unknown'}. Chosen departure: {hint or 'unknown'}.\nPage text:\n{(page or '')[:2400]}",
        timeout=12,
    )
    what = re.sub(r"\s+", " ", str(data.get("what") or "")).strip()
    why = re.sub(r"\s+", " ", str(data.get("why") or "")).strip()
    recommend = re.sub(r"\s+", " ", str(data.get("recommend") or "")).strip()
    if not what or not recommend:
        return None
    kind = str(data.get("kind") or "other").strip().lower()
    if kind not in {"upsell", "form", "login", "otp", "review", "results", "pay", "other"}:
        kind = "other"
    if kind == "pay":
        return {"kind": "pay", "what": what, "why": why, "recommend": recommend, "tap": ""}
    return {
        "kind": kind,
        "what": what[:220],
        "why": why[:220] or "the page has not moved on",
        "recommend": recommend[:220],
        "tap": safe_tap(str(data.get("tap") or ""), page),
    }
