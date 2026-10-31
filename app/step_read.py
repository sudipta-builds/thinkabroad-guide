"""Read a booking step from the page text. The model names it; we do not list every label.

The tap it returns is used only when that exact phrase is on the page and it is not a purchase.
"""

from __future__ import annotations

import os
import re

from app.config import get_settings

_READ = (
    "You are looking at one live flight-booking page. Controls list checked state: "
    "[x] is selected, [ ] is not. Page text is extra context. "
    "You have not been given a list of steps. Name this step from these words only.\n"
    'Return JSON only: {"kind":"upsell|form|login|otp|review|results|pay|other",'
    '"what":"","why":"","recommend":"","tap":""}\n'
    "what, why, and recommend are one short sentence each, in plain English.\n"
    "tap is the exact short control label to press, copied from Controls or the page, or empty.\n"
    "If a required radio or traveller checkbox is [ ], tap that label so it becomes selected.\n"
    "If the page is selling an optional extra, tap the decline control that is actually written there, "
    "whatever its words are.\n"
    "Never set tap to Pay, Pay now, Secure My Trip, Buy This, Buy, Purchase, or any control that spends money.\n"
    "If this is the payment page, kind is pay and tap is empty.\n"
    "If you do not recognize the layout, kind is other.\n"
    "Do not invent a button, a price, or a flight that is not in the text."
)

RECOMMENDED = "__recommended__"

_SPEND = re.compile(
    r"\b(pay now|pay to complete|make payment|secure my trip|buy this|buy now|purchase|add to trip)\b",
    re.I,
)
_DECLINE = re.compile(
    r"(I don'?t want Free Cancellation|No,? Thanks|Not this time|Continue without(?: add-?ons)?|"
    r"Skip (?:add-?ons|this)|I don'?t want|Not now|Maybe later)",
    re.I,
)
_AGREE = re.compile(
    r"(yes|yep|yeah|ok|okay|sure|skip|skip it|do that|do it|go on|go ahead|continue|no thanks|no, thanks)",
    re.I,
)


def step_read_enabled() -> bool:
    if os.getenv("GUIDE_STEP_READ", "1").strip().lower() in {"0", "false", "no"}:
        return False
    return bool(get_settings().llm_ready)


def decline_tap(page: str) -> str:
    """The skip/decline control actually written on this page. Empty when none."""
    match = _DECLINE.search(page or "")
    if not match:
        return ""
    return safe_tap(match.group(1), page)


def safe_tap(tap: str, page: str) -> str:
    """A click the model named, only if the page shows it and it does not spend money."""
    label = re.sub(r"\s+", " ", (tap or "").strip())
    if not label or label == RECOMMENDED or len(label) > 80 or _SPEND.search(label):
        return ""
    hay = re.sub(r"\s+", " ", page or "")
    if label.lower() not in hay.lower():
        return ""
    return label


def _hay(page: str, controls: str = "") -> str:
    return f"{page or ''}\n{controls or ''}"


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

    yes/skip/ok/continue uses the recommended label, or the decline text on the
    page, or RECOMMENDED so checkout can opt out and tick a saved traveller.
    Any other short phrase is used only when that phrase is on the page.
    Pay controls are never accepted here.
    """
    page = pending.get("page") or ""
    raw = re.sub(r"\s+", " ", (text or "").strip())
    if not raw or not page:
        return ""
    if re.search(r"\b(pay now|pay to complete|make payment)\b", raw, re.I):
        return ""
    if _AGREE.fullmatch(raw):
        return safe_tap(str(pending.get("tap") or ""), page) or decline_tap(page) or RECOMMENDED
    if 2 <= len(raw) <= 80 and raw.lower() in re.sub(r"\s+", " ", page).lower():
        return safe_tap(raw, page)
    return ""


def read_step(page: str, *, seller: str = "", hint: str = "", controls: str = "") -> dict[str, str] | None:
    """Ask the model what this page is. None when the model is off or the answer is empty."""
    if not step_read_enabled():
        return None
    from app import llm

    data = llm.converse(
        _READ,
        (
            f"Seller: {seller or 'unknown'}. Chosen departure: {hint or 'unknown'}.\n"
            f"Controls ([x]=selected, [ ]=not):\n{(controls or '(none listed)')[:1600]}\n"
            f"Page text:\n{(page or '')[:1800]}"
        ),
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
    hay = _hay(page, controls)
    tap = safe_tap(str(data.get("tap") or ""), hay)
    if kind in {"upsell", "other"} and not tap:
        tap = decline_tap(hay)
    return {
        "kind": kind,
        "what": what[:220],
        "why": why[:220] or "the page has not moved on",
        "recommend": recommend[:220],
        "tap": tap,
    }
