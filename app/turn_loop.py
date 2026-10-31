"""One Gemini turn: user wish + live screenshot → goal, recommend, tap.

DeepSeek is not used here. Rails stay code: never pay, never invent a button.
"""

from __future__ import annotations

import re
from typing import Any

from app.config import get_settings
from app.llm import _parse_json_obj, gemini_chat, vision_converse

_TURN = (
    "You are Guide on WhatsApp. You get ONE traveller message and optionally a screenshot "
    "of the live Indian flight-booking page (Cleartrip/Ixigo/EaseMyTrip).\n"
    "Understand what they want. Look at the screenshot if present — that is the current step.\n"
    "Rails: never pay; never tap Pay/Buy/Secure/Assure/Insure; never invent a button or a flight.\n"
    "Return JSON only:\n"
    '{"goal":"skip|forward|ask|stop|pay|search|details",'
    '"intent":"search|select|book|login|details|pay|pay_self|cancel|status|chat",'
    '"kind":"upsell|form|login|otp|review|results|pay|unavailable|addons|other",'
    '"what":"","why":"","recommend":"","tap":"","auto":false,"reply":"","facts":[]}\n'
    "goal=ask (intent=status) when they ask which step, what to do next, check, or what you see. "
    "Name the screenshot and recommend the safe next tap.\n"
    "goal=forward (intent=book) when they confirm the flight, continue, go on, or yes to your last rec.\n"
    "goal=skip (intent=book) when they skip extras.\n"
    "If the screenshot is NOT Choose add-ons / seat map / missed-meals, do not recommend Skip add-ons.\n"
    "If the screenshot is an optional paid extra (flexibility, protect, cancellation, insurance), "
    "kind=upsell, tap Continue or the decline label, never Book now on that extra.\n"
    "Your job is to complete the booking until the payment page, then stop. Nothing is paid.\n"
    "If they sent passenger details (name/email/DOB/gender) and the screenshot is add-ons or an extra, "
    "do not re-ask the form. goal=skip or forward, tap Skip add-ons or Continue, intent=book.\n"
    "If the screenshot is the traveller form and a field in Memory is still empty, intent=details.\n"
    "Do not re-ask a field already listed as saved. Do not recommend Skip add-ons unless that sheet is on screen.\n"
    "tap must be a short label visible on the page/screenshot. auto true only for Skip add-ons, "
    "name-check Confirm, or Continue that does not spend money.\n"
    "If NO screenshot is attached, never invent a fare list or say results are ready. "
    "A new trip (from/to/date) is intent=search, goal=search.\n"
    "reply: one short sentence for WhatsApp. No emoji. Nothing paid."
)

_SPEND = re.compile(
    r"\b(pay now|pay to complete|make payment|secure my trip|buy this|buy now|purchase|assure my trip)\b",
    re.I,
)


def _page_from(state: dict[str, Any]) -> str:
    pending = state.get("pending_step") if isinstance(state.get("pending_step"), dict) else None
    if pending:
        return str(pending.get("page") or "")
    return str(state.get("stuck_page") or "")


def _shot_from(state: dict[str, Any]) -> str:
    from pathlib import Path

    raw = str(state.get("last_shot") or "")
    if raw and Path(raw).is_file():
        return raw
    vision = state.get("last_vision") if isinstance(state.get("last_vision"), dict) else None
    if vision:
        path = str(vision.get("shot") or "")
        if path and Path(path).is_file():
            return path
    return ""


def remember_shot(state: dict[str, Any], path: str) -> None:
    """Keep the last booking screenshot so the next WhatsApp turn can see it."""
    if not path:
        return
    from pathlib import Path

    if Path(path).is_file():
        state["last_shot"] = path


def _local_turn(text: str, page: str, state: dict[str, Any]) -> dict[str, Any]:
    """Offline stand-in so tests and a missing Gemini key still route the wish."""
    raw = re.sub(r"\s+", " ", (text or "").strip())
    vision = state.get("last_vision") if isinstance(state.get("last_vision"), dict) else {}
    kind = str(vision.get("kind") or state.get("stuck_kind") or "")
    hay = page or ""
    tap = str(vision.get("tap") or "")
    if re.search(
        r"what (?:to do |should i do )?next|which step|check and tell|tell me what to do|"
        r"why (?:stuck|blocked)|what(?:'s| is) (?:wrong|happening)|what do you see",
        raw,
        re.I,
    ):
        rec = str(vision.get("recommend") or vision.get("what") or "say go on if you want me to continue")
        if kind and kind != "addons":
            tap = tap or "Continue"
        return {
            "goal": "ask",
            "intent": "status",
            "kind": kind or "other",
            "what": str(vision.get("what") or "the open booking page"),
            "why": "you asked what to do next",
            "recommend": rec,
            "tap": tap,
            "auto": "",
            "reply": (
                f"On screen: {vision.get('what') or kind or 'the booking page'}. "
                f"I recommend {rec}. Nothing paid."
            ),
            "facts": list(vision.get("facts") or []),
            "source": "local",
        }
    looks_details = bool(re.search(r"@|\bmale\b|\bfemale\b|\bother\b|\d{1,2}/\d{1,2}/\d{2,4}", raw, re.I))
    if looks_details and kind in {"addons", "upsell", "review", "fare_sheet"}:
        label = tap or ("Skip add-ons" if kind == "addons" or re.search(r"skip add", hay, re.I) else "Continue")
        return {
            "goal": "skip" if "skip" in label.lower() else "forward",
            "intent": "book",
            "kind": kind,
            "what": str(vision.get("what") or "the open booking sheet"),
            "why": "details are saved; this screenshot is not the form",
            "recommend": f"press {label} and continue the booking",
            "tap": label,
            "auto": "1",
            "reply": f"Saved those details. The page is already past the form — I'll press {label}. Nothing paid.",
            "facts": list(vision.get("facts") or []),
            "source": "local",
        }
    if looks_details:
        return {
            "goal": "details",
            "intent": "details",
            "kind": kind or "form",
            "what": "passenger details",
            "why": "you sent name or contact fields",
            "recommend": "I'll save these and fill the form if it is open",
            "tap": "",
            "auto": "",
            "reply": "",
            "facts": [],
            "source": "local",
        }
    if re.search(r"\bskip\b", raw, re.I):
        return {
            "goal": "skip",
            "intent": "book",
            "kind": kind or "addons",
            "what": "skip extras",
            "why": "you asked to skip",
            "recommend": "press Skip add-ons if that sheet is open, otherwise Continue",
            "tap": tap or ("Skip add-ons" if re.search(r"skip add", hay, re.I) else "Continue"),
            "auto": "1",
            "reply": "Skipping extras and moving forward. Nothing paid.",
            "facts": [],
            "source": "local",
        }
    if re.search(r"confirm(?: the)? fl|go on|go ahead|continue\b", raw, re.I) or re.fullmatch(
        r"yes|yep|yeah|ok|okay", raw, re.I
    ):
        pending = state.get("pending_step") if isinstance(state.get("pending_step"), dict) else {}
        label = tap or str(pending.get("tap") or "") or "Continue"
        if kind == "addons" and re.search(r"skip add", hay, re.I) and re.fullmatch(r"yes|yep|yeah|ok|okay", raw, re.I):
            label = "Skip add-ons"
        return {
            "goal": "forward",
            "intent": "book",
            "kind": kind or "other",
            "what": "move the booking forward",
            "why": "you asked to confirm or continue",
            "recommend": f"press {label}",
            "tap": label,
            "auto": "",
            "reply": f"I'll press {label} on the open page. Nothing paid.",
            "facts": [],
            "source": "local",
        }
    return {
        "goal": "",
        "intent": "",
        "kind": kind or "",
        "what": "",
        "why": "",
        "recommend": "",
        "tap": "",
        "auto": "",
        "reply": "",
        "facts": [],
        "source": "local",
    }


def _gemini_turn(text: str, page: str, image_path: str, state: dict[str, Any]) -> dict[str, Any]:
    import os

    if os.getenv("GUIDE_STEP_READ", "1").strip().lower() in {"0", "false", "no"}:
        return {}
    if not get_settings().gemini_api_key:
        return {}
    hint = str(state.get("selected_hint") or "")
    seller = str(state.get("seller") or state.get("preferred_seller") or "")
    missing = [key for key in ("name", "email", "phone", "dob", "gender") if not str(state.get(key) or "").strip()]
    user = (
        f"Traveller: {text}\nSeller: {seller or 'unknown'}. Chosen departure: {hint or 'unknown'}.\n"
        f"Screenshot attached: {'yes' if image_path else 'NO — do not invent results'}.\n"
        f"Last step kind: {state.get('stuck_kind') or state.get('stage') or ''}.\n"
        f"Saved passenger: name={bool(state.get('name'))} email={bool(state.get('email'))} "
        f"phone={bool(state.get('phone'))} dob={bool(state.get('dob'))} gender={bool(state.get('gender'))}. "
        f"Still missing: {', '.join(missing) if missing else 'none'}.\n"
        f"Page text (may be stale):\n{(page or '')[:1200]}"
    )
    if image_path:
        data = vision_converse(_TURN, user, image_path, timeout=28.0)
    else:
        raw = gemini_chat(_TURN + "\n\n" + user, timeout=12.0, max_tokens=400)
        data = _parse_json_obj(raw)
    return data if isinstance(data, dict) else {}


def interpret_turn(
    text: str,
    state: dict[str, Any],
    *,
    page_text: str = "",
    image_path: str = "",
) -> dict[str, Any]:
    """Gemini reads the wish and the sheet. Empty fields fall back to a local read."""
    page = (page_text or _page_from(state))[:4000]
    shot = image_path or _shot_from(state)
    data = _gemini_turn(text, page, shot, state)
    local = _local_turn(text, page, state)
    if not data:
        return local
    goal = str(data.get("goal") or local.get("goal") or "").strip().lower()
    intent = str(data.get("intent") or local.get("intent") or "").strip().lower()
    kind = str(data.get("kind") or local.get("kind") or "").strip().lower()
    tap = re.sub(r"\s+", " ", str(data.get("tap") or "")).strip()[:80]
    if tap and _SPEND.search(tap):
        tap = ""
        goal = "ask"
    reply = re.sub(r"\s+", " ", str(data.get("reply") or "")).strip()[:400]
    what = re.sub(r"\s+", " ", str(data.get("what") or "")).strip()[:220]
    recommend = re.sub(r"\s+", " ", str(data.get("recommend") or "")).strip()[:220]
    facts_raw = data.get("facts") if isinstance(data.get("facts"), list) else []
    facts = [re.sub(r"\s+", " ", str(item or "")).strip()[:140] for item in facts_raw if str(item or "").strip()]
    if kind and kind != "addons" and re.search(r"skip add", tap, re.I):
        tap = local.get("tap") if local.get("goal") == "forward" else ""
        if not tap and re.search(r"continue", page, re.I):
            tap = "Continue"
    if not shot and not page:
        trip_ready = bool(state.get("origin") and state.get("destination") and state.get("depart"))
        if trip_ready and intent in {"status", "select", "book", "chat", ""}:
            intent = "search"
            goal = "search"
            kind = ""
            tap = ""
            reply = ""
    return {
        "goal": goal or str(local.get("goal") or ""),
        "intent": intent or str(local.get("intent") or ""),
        "kind": kind,
        "what": what or str(local.get("what") or ""),
        "why": re.sub(r"\s+", " ", str(data.get("why") or "")).strip()[:220],
        "recommend": recommend or str(local.get("recommend") or ""),
        "tap": tap or str(local.get("tap") or ""),
        "auto": "1" if data.get("auto") in (True, "1", "true", "True") else "",
        "reply": reply or str(local.get("reply") or ""),
        "facts": facts or list(local.get("facts") or []),
        "source": "gemini",
    }


def format_turn_reply(turn: dict[str, Any]) -> str:
    spoken = str(turn.get("reply") or "").strip()
    if spoken:
        if "nothing paid" not in spoken.lower():
            spoken = spoken.rstrip(".") + ". Nothing paid."
        return spoken
    what = turn.get("what") or "the open page"
    rec = turn.get("recommend") or "tell me what to press"
    tap = turn.get("tap") or ""
    nxt = f" Reply yes and I'll press {tap}." if tap else ""
    return f"On screen: {what}. I recommend {rec}.{nxt} Nothing paid."


def has_live_facts(state: dict[str, Any]) -> bool:
    """True when this booking has a screenshot or a vision read of that page."""
    if _shot_from(state):
        return True
    vision = state.get("last_vision") if isinstance(state.get("last_vision"), dict) else {}
    return bool(vision.get("kind"))


def align_action(text: str, state: dict[str, Any], turn: dict[str, Any] | None = None) -> dict[str, Any]:
    """Bind intent + page facts to one action. Empty facts cannot mean 'results are ready'."""
    turn = turn or interpret_turn(text, state)
    vision = state.get("last_vision") if isinstance(state.get("last_vision"), dict) else {}
    sheet = str(turn.get("kind") or vision.get("kind") or "")
    intent = str(turn.get("intent") or "")
    goal = str(turn.get("goal") or "")
    tap = str(turn.get("tap") or "")
    live = has_live_facts(state)
    trip = bool(state.get("origin") and state.get("destination") and state.get("depart"))
    clock = bool(re.search(r"\b\d{1,2}[:.]\d{2}\b", text or ""))

    if intent == "cancel" or goal == "stop":
        return {"do": "reply", "kind": "cancel", "tap": "", "turn": turn, "reply": turn.get("reply") or ""}
    if intent == "pay_self":
        return {"do": "pay_self", "kind": "pay_self", "tap": "", "turn": turn, "reply": ""}
    if intent == "pay":
        return {"do": "pay_stop", "kind": "pay", "tap": "", "turn": turn, "reply": ""}

    if not live:
        if trip:
            return {"do": "search", "kind": "search", "tap": "", "turn": turn, "reply": ""}
        return {"do": "ask_trip", "kind": "search", "tap": "", "turn": turn, "reply": turn.get("reply") or ""}

    if sheet in {"results", "loading"}:
        if clock or intent in {"select", "book"} or goal == "forward":
            return {"do": "book", "kind": "select" if clock else "book", "tap": tap, "turn": turn, "reply": ""}
        if intent == "status" or goal == "ask":
            return {"do": "reply", "kind": "status", "tap": tap, "turn": turn, "reply": format_turn_reply(turn)}
        return {"do": "search", "kind": "search", "tap": "", "turn": turn, "reply": ""}

    if sheet in {"addons", "upsell", "review", "fare_sheet"}:
        if intent == "status" or goal == "ask":
            return {"do": "reply", "kind": "status", "tap": tap, "turn": turn, "reply": format_turn_reply(turn)}
        if not tap:
            tap = "Skip add-ons" if sheet == "addons" else "Continue"
        return {"do": "book", "kind": "book", "tap": tap, "turn": turn, "reply": ""}

    if sheet in {"form", "passenger"} or intent == "details":
        if sheet in {"addons", "upsell", "review", "fare_sheet"}:
            return {
                "do": "book",
                "kind": "book",
                "tap": tap or "Skip add-ons",
                "turn": turn,
                "reply": "",
            }
        return {"do": "details", "kind": "details", "tap": tap, "turn": turn, "reply": ""}

    if sheet == "pay":
        return {"do": "pay_stop", "kind": "pay", "tap": "", "turn": turn, "reply": ""}

    if intent in {"book", "select"} or goal in {"skip", "forward"}:
        return {"do": "book", "kind": "book", "tap": tap, "turn": turn, "reply": ""}
    if trip and intent in {"search", ""}:
        return {"do": "search", "kind": "search", "tap": "", "turn": turn, "reply": ""}
    if intent == "status" or goal == "ask":
        return {"do": "reply", "kind": "status", "tap": tap, "turn": turn, "reply": format_turn_reply(turn)}
    return {"do": "reply", "kind": intent or "chat", "tap": tap, "turn": turn, "reply": format_turn_reply(turn)}

