"""Conscience for one booking sheet: the model names the step; rails block payment.

Hardcoded lists are only safety nets (pay words, common decline labels), not a catalog of screens.
"""

from __future__ import annotations

import os
import re

from app.config import get_settings

_READ = (
    "You are looking at one live flight-booking sheet. Controls show checked state: "
    "[x] is selected, [ ] is not. Prefer Controls over page text when they disagree.\n"
    'Return JSON only: {"kind":"upsell|form|login|otp|review|results|pay|other",'
    '"what":"","why":"","recommend":"","tap":"","auto":false}\n'
    "what, why, and recommend are one short sentence each, in plain English.\n"
    "tap is the exact short control label to press, copied from Controls or the page, or empty.\n"
    "auto is true only when that tap is a safe next step with no money spent "
    "(Confirm on a name check, Continue/Proceed, or declining an optional extra).\n"
    "auto is false for paid extras, unknown buttons, or anything that needs the traveller's yes.\n"
    "If a required radio or traveller checkbox is [ ], tap that label so it becomes selected.\n"
    "If the page is selling an optional extra, tap the decline control written there, set kind=upsell, auto=false.\n"
    "Never set tap to Pay, Pay now, Secure My Trip, Buy This, Buy, Purchase, or any control that spends money.\n"
    "If this is the payment page, kind is pay, tap is empty, auto is false.\n"
    "If you do not recognize the layout, kind is other and auto is false unless tap is clearly Confirm/Continue/a decline.\n"
    "Do not invent a button, a price, or a flight that is not in the text."
)

RECOMMENDED = "__recommended__"

_SPEND = re.compile(
    r"\b(pay now|pay to complete|make payment|secure my trip|buy this|buy now|purchase|add to trip|assure my trip)\b",
    re.I,
)
_DECLINE = re.compile(
    r"(I don'?t want Free Cancellation|No,? Thanks|Not this time|Continue without(?: add-?ons)?|"
    r"Skip (?:add-?ons|this)|I don'?t want|Not now|Maybe later)",
    re.I,
)
_AGREE = re.compile(
    r"(yes|yep|yeah|ok|okay|sure|skip|skip it|do that|do it|go on|go ahead|continue|confirm|"
    r"next|next step|no thanks|no, thanks)",
    re.I,
)
_SAFE_AUTO = re.compile(
    r"^(confirm|continue|proceed|no,? thanks|not this time|i don'?t want.*|skip.*|continue without.*)$",
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


def is_paid_upsell(page: str) -> bool:
    """Paid Assured / Secure My Trip sheet. True only when that buy path is on screen."""
    hay = page or ""
    if not re.search(r"free cancellation|\bassured\b|assure my trip", hay, re.I):
        return False
    return bool(re.search(r"secure my trip|buy this|assure my trip|no,?\s*thanks", hay, re.I))


def is_name_confirm(page: str) -> bool:
    """Name check sheet (Review Details + Confirm). Not payment."""
    hay = page or ""
    if not re.search(r"\bconfirm\b", hay, re.I):
        return False
    return bool(
        re.search(r"review details|govt\.?\s*id|aadhaar|matches your (govt|name)|driver.?s license", hay, re.I)
    )


def forward_tap(page: str) -> str:
    """Confirm / Continue / Proceed when that label is on this sheet."""
    for label in ("Confirm", "Continue", "Proceed"):
        hit = safe_tap(label, page)
        if hit:
            return hit
    return ""


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


def choice_phrase(text: str) -> str:
    """Strip click / quotes so Click 'No thanks' becomes No thanks."""
    raw = re.sub(r"\s+", " ", (text or "").strip())
    match = re.match(r"^(?:click|press|tap|hit)\s+(?:the\s+)?(.+)$", raw, re.I)
    if match:
        raw = match.group(1).strip()
    return raw.strip("'\"").strip()


def accept_choice(text: str, pending: dict[str, str]) -> str:
    """The button the traveller just agreed to. Empty until they choose.

    yes/skip/ok/continue uses the recommended label, or the decline text on the
    page, or RECOMMENDED so checkout can opt out and tick a saved traveller.
    Any other short phrase is used only when that phrase is on the page.
    Pay controls are never accepted here.
    """
    page = pending.get("page") or ""
    raw = choice_phrase(text)
    if not raw or not page:
        return ""
    if re.search(r"\b(pay now|pay to complete|make payment|secure my trip)\b", raw, re.I):
        return ""
    if _AGREE.fullmatch(raw):
        if re.fullmatch(r"confirm", raw, re.I):
            return (
                safe_tap("Confirm", page)
                or safe_tap(str(pending.get("tap") or ""), page)
                or forward_tap(page)
                or decline_tap(page)
                or RECOMMENDED
            )
        return safe_tap(str(pending.get("tap") or ""), page) or decline_tap(page) or forward_tap(page) or RECOMMENDED
    if re.search(r"no\s*,?\s*thanks", raw, re.I):
        return safe_tap("No, Thanks", page) or safe_tap("No Thanks", page) or decline_tap(page)
    if re.search(r"don.?t want", raw, re.I):
        return decline_tap(page) or RECOMMENDED
    if 2 <= len(raw) <= 80 and raw.lower() in re.sub(r"\s+", " ", page).lower():
        return safe_tap(raw, page)
    return ""


def arm_pending_step(state: dict, page: str, kind: str, *, tap: str = "") -> None:
    """Remember this page so yes / No thanks can resume checkout."""
    hay = (page or "")[:1500]
    label = tap or decline_tap(hay)
    state["stuck_page"] = hay
    state["pending_step"] = {
        "tap": label,
        "page": hay,
        "kind": kind,
    }


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
    auto = bool(data.get("auto"))
    if kind == "pay":
        return {"kind": "pay", "what": what, "why": why, "recommend": recommend, "tap": "", "auto": ""}
    hay = _hay(page, controls)
    tap = safe_tap(str(data.get("tap") or ""), hay)
    if not tap and is_name_confirm(hay):
        tap = forward_tap(hay)
        auto = True
    if kind == "upsell" and not tap:
        tap = decline_tap(hay)
        auto = False
    if kind in {"other", "review", "form"} and not tap:
        tap = decline_tap(hay) or forward_tap(hay)
    if kind == "upsell":
        auto = False
    elif kind in {"review", "form"} and tap and _SAFE_AUTO.match(tap):
        auto = True
    elif kind == "other":
        # Unknown sheets need a yes unless the model already set auto.
        auto = bool(data.get("auto")) and bool(tap) and bool(_SAFE_AUTO.match(tap))
    return {
        "kind": kind,
        "what": what[:220],
        "why": why[:220] or "the page has not moved on",
        "recommend": recommend[:220],
        "tap": tap,
        "auto": "1" if auto else "",
    }


def decide_front(
    page: str,
    controls: str = "",
    *,
    seller: str = "",
    hint: str = "",
    chosen_tap: str = "",
) -> dict[str, str]:
    """Conscience for the covering sheet: click, ask, or none.

    Rails only: never pay; ask on upsell unless the traveller already chose a tap.
    """
    hay = _hay(page, controls)
    raw_choice = (chosen_tap or "").strip()
    heard = read_step(page, seller=seller, hint=hint, controls=controls)

    if raw_choice:
        if raw_choice == RECOMMENDED or raw_choice.startswith("__"):
            label = decline_tap(hay) or forward_tap(hay)
            if label:
                return {"action": "click", "tap": label, "kind": (heard or {}).get("kind") or "other"}
        elif _SPEND.search(raw_choice):
            return {
                "action": "ask",
                "tap": "",
                "kind": (heard or {}).get("kind") or "other",
                "what": (heard or {}).get("what") or "a control that would spend money",
                "why": (heard or {}).get("why") or "I will not tap Pay or a paid extra",
                "recommend": (heard or {}).get("recommend") or "send a safe button name",
            }
        else:
            label = safe_tap(raw_choice, hay)
            if label:
                return {"action": "click", "tap": label, "kind": (heard or {}).get("kind") or "other"}
        return {
            "action": "ask",
            "tap": "",
            "kind": (heard or {}).get("kind") or "other",
            "what": (heard or {}).get("what") or "the sheet that is still open",
            "why": "that button is not on this sheet",
            "recommend": "send the exact label you see",
        }

    if heard and heard.get("kind") == "pay":
        return {
            "action": "ask",
            "tap": "",
            "kind": "pay",
            "what": heard.get("what") or "the payment page",
            "why": heard.get("why") or "I stop before payment",
            "recommend": heard.get("recommend") or "say pay with card or you'll pay yourself",
        }

    if heard and heard.get("kind") == "upsell":
        return {
            "action": "ask",
            "tap": heard.get("tap") or decline_tap(hay),
            "kind": "upsell",
            "what": heard.get("what") or "a paid extra",
            "why": heard.get("why") or "an optional purchase on top of the fare",
            "recommend": heard.get("recommend") or "reply yes to skip it",
        }

    # Auto only when the model marks auto, or a known form/review forward — never blind on "other".
    if (
        heard
        and heard.get("tap")
        and heard.get("kind") in {"review", "form"}
        and (heard.get("auto") or _SAFE_AUTO.match(heard["tap"]))
    ):
        return {"action": "click", "tap": heard["tap"], "kind": heard.get("kind") or "review"}

    if heard and heard.get("tap") and heard.get("auto") and _SAFE_AUTO.match(heard["tap"]):
        return {"action": "click", "tap": heard["tap"], "kind": heard.get("kind") or "other"}

    if heard and heard.get("tap"):
        return {
            "action": "ask",
            "tap": heard["tap"],
            "kind": heard.get("kind") or "other",
            "what": heard.get("what") or "a step I have not seen before",
            "why": heard.get("why") or "I want your go-ahead before tapping",
            "recommend": heard.get("recommend") or f"reply yes to press {heard['tap']}",
        }

    # Model off or empty: rails only. Name Confirm may auto-click; everything else asks.
    if is_name_confirm(hay):
        return {"action": "click", "tap": forward_tap(hay) or "Confirm", "kind": "review"}
    if is_paid_upsell(hay):
        return {
            "action": "ask",
            "tap": decline_tap(hay),
            "kind": "upsell",
            "what": "a paid Free Cancellation / Assured sheet",
            "why": "an optional purchase on top of the fare",
            "recommend": "reply yes or send No, Thanks — I will not buy Assured",
        }
    tap = decline_tap(hay) or forward_tap(hay)
    return {
        "action": "ask",
        "tap": tap,
        "kind": (heard or {}).get("kind") or "other",
        "what": (heard or {}).get("what") or "a sheet I don't treat as a known booking step",
        "why": (heard or {}).get("why") or "there is no fixed click for this layout",
        "recommend": (heard or {}).get("recommend")
        or (f"reply yes to press {tap}" if tap else "send the exact button you want pressed"),
    }
