"""One Gemini turn: user wish + live screenshot → goal, then code maps goal → do.

Pipeline: intent first → propose action → align gate → execute only if they match.
DeepSeek is not used here. Rails stay code: never invent a button; pay only when authorized.
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
    "Rails: never invent a button or a flight. Tap Pay/Buy only when they explicitly authorize payment "
    "(pay with UPI/card / fill payment / pay now) — otherwise stop on the pay sheet and ask.\n"
    "INTENT FIRST: decide goal from the USER MESSAGE. Screenshot kind is context only — "
    "being on a pay sheet must NOT force goal=pay if they asked to search/compare/leave.\n"
    "Return JSON only:\n"
    '{"goal":"skip|forward|ask|stop|pay|pay_self|search|details|pick_traveller|delete_traveller",'
    '"intent":"search|select|book|login|details|pay|pay_self|cancel|status|chat",'
    '"kind":"upsell|form|login|otp|review|results|pay|unavailable|addons|other",'
    '"what":"","why":"","recommend":"","tap":"","auto":false,"reply":"","facts":[]}\n'
    "goal=ask (intent=status) when they ask which step, what to do next, check, or what you see. "
    "Name the screenshot and recommend the safe next tap.\n"
    "goal=forward (intent=book) when they confirm the flight, continue, go on, or yes to your last rec "
    "(not bare yes on a payment sheet — that is ask or pay).\n"
    "goal=skip (intent=book) when they skip extras.\n"
    "goal=pick_traveller (intent=book) when they want an existing saved name ticked — ANY wording "
    "(pick one, click a name, use saved, that person, whoever is listed). tap empty; "
    "do NOT fill contact mobile; do NOT create a new traveller; do NOT use goal=details.\n"
    "goal=delete_traveller (intent=book) when they want a saved profile removed "
    "(delete/remove/clear duplicates) — tap empty.\n"
    "goal=details ONLY when they send passenger/contact VALUES (phone/email/name/DOB/gender) "
    "OR ask you to fill/type a saved contact field — not when they ask to click a traveller name.\n"
    "goal=search when they want fares / prices / other sites / compare — even if the screenshot is pay.\n"
    "goal=pay when they want YOU to fill/pay (UPI ID, card, CVV, pay with UPI/card, authorize pay).\n"
    "goal=pay_self when they will pay on the link themselves.\n"
    "ACTION RULE: goal details|forward|skip|pick_traveller|delete_traveller|search|pay means the app "
    "ACTS this turn. reply must name that action (Typing… / Opening… / Checking fares… / "
    "Asking for UPI…). Never reply with noted/saved alone when an action goal applies.\n"
    "goal=ask ONLY for pure status / unclear messages with NO page action this turn.\n"
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
    "If NO screenshot is attached, never invent a fare list. Saved fare rows in Memory "
    "are the current results sheet — a departure time is intent=select, not a new trip. "
    "A new trip (from/to/date) is intent=search, goal=search.\n"
    "If they say start fresh / start over / search again, intent=search, goal=search. "
    "Ignore a stale add-ons screenshot and do not recommend Skip add-ons.\n"
    "If the screenshot is a site error (system acting up, Go to home), kind=error. "
    "Never say ready to pay. Do not recommend Go to home. Recommend the other seller and the saved departure time.\n"
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


def stored_fare_list(state: dict[str, Any]) -> bool:
    """True when this turn already has a fare list, even if the JPEG is gone."""
    options = state.get("options") if isinstance(state.get("options"), list) else []
    if options:
        return True
    vision = state.get("last_vision") if isinstance(state.get("last_vision"), dict) else {}
    return str(vision.get("kind") or "") in {"results", "loading"}


def remember_results_page(
    state: dict[str, Any],
    *,
    seller: str,
    options: list[dict[str, Any]] | None = None,
    shot: str = "",
    page: str = "",
) -> None:
    """One memory for any site's fare list: kind, seller, bookable, rows, optional shot."""
    from app.search import seller_can_book

    rows = [item for item in (options or []) if isinstance(item, dict)]
    facts = []
    for item in rows[:6]:
        bits = [str(item.get("airline") or ""), str(item.get("depart") or ""), str(item.get("price") or "")]
        line = " ".join(part for part in bits if part).strip()
        if line:
            facts.append(line[:140])
    if shot:
        remember_shot(state, shot)
    state["seller"] = seller
    state["stage"] = "results"
    state["stuck_kind"] = "results"
    if page:
        state["stuck_page"] = str(page)[:4000]
    if rows:
        state["options"] = [
            {k: item[k] for k in ("airline", "flight", "depart", "arrive", "price") if k in item}
            for item in rows
        ]
    state["last_vision"] = {
        "kind": "results",
        "seller": seller,
        "bookable": seller_can_book(seller),
        "what": "fare list",
        "facts": facts,
        "tap": "",
        "shot": str(state.get("last_shot") or shot or ""),
    }


_FRESH = re.compile(
    r"\b(?:let'?s )?start (?:fresh|over)\b|\bstart over\b|\bfresh (?:search|fare|start)\b|"
    r"\bnew search\b|\bsearch again\b",
    re.I,
)


def wants_fresh_search(text: str) -> bool:
    """True when the traveller wants a new fare list, not Skip on a dead checkout."""
    return bool(_FRESH.search(text or ""))


def clear_checkout_memory(state: dict[str, Any]) -> None:
    """Drop the saved itinerary, the last screenshot, and the Kernel tab."""
    for key in (
        "live",
        "last_shot",
        "last_vision",
        "stuck_page",
        "pending_step",
        "stuck_kind",
        "last_stuck",
        "chosen_tap",
        "options",
        "kernel_session_id",
        "kernel_seller",
        "kernel_logged_in",
        "kernel_awaiting_otp",
        "kernel_awaiting_phone",
    ):
        state.pop(key, None)


def _local_turn(text: str, page: str, state: dict[str, Any]) -> dict[str, Any]:
    """Offline stand-in so tests and a missing Gemini key still route the wish."""
    raw = re.sub(r"\s+", " ", (text or "").strip())
    vision = state.get("last_vision") if isinstance(state.get("last_vision"), dict) else {}
    kind = str(vision.get("kind") or state.get("stuck_kind") or "")
    hay = page or ""
    tap = str(vision.get("tap") or "")
    if wants_fresh_search(raw):
        return {
            "goal": "search",
            "intent": "search",
            "kind": "",
            "what": "a fresh fare list",
            "why": "you asked to start over",
            "recommend": "open results for this trip",
            "tap": "",
            "auto": "",
            "reply": "I'll open a fresh fare list. Nothing paid.",
            "facts": [],
            "source": "local",
        }
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
    from app.kernel_otp import indian_national

    if re.search(
        r"\b(?:delete|remove|erase|clear)\b.+\b(?:travellers?|travelers?|profiles?|names?|passengers?|duplicates?)\b"
        r"|\bget rid of\b.+\b(?:profile|name|traveller|traveler|duplicate)\b"
        r"|\b(?:too many|duplicate)\b.+\b(?:profiles?|names?)\b",
        raw,
        re.I,
    ):
        return {
            "goal": "delete_traveller",
            "intent": "book",
            "kind": kind or "form",
            "what": "saved traveller profiles",
            "why": "you asked to remove a saved profile",
            "recommend": "delete one saved traveller on screen",
            "tap": "__delete_traveller__",
            "auto": "",
            "reply": "I'll remove one saved traveller profile. Nothing paid.",
            "facts": [],
            "source": "local",
        }
    if re.search(
        r"\b(?:click|pick|select|tick|choose|use|tap)\b.+\b(?:name|names|traveller|traveler|profile|saved|one of)\b"
        r"|\bpick one\b|\bthat one\b|\bone of the names\b|\bsaved (?:traveller|traveler|profile)\b"
        r"|\buse (?:the |an? )?existing\b|\bdon'?t create (?:a )?new\b|\banyone (?:of them|listed)\b",
        raw,
        re.I,
    ):
        return {
            "goal": "pick_traveller",
            "intent": "book",
            "kind": kind or "form",
            "what": "saved traveller list",
            "why": "you asked to use an existing name",
            "recommend": "tick one saved traveller",
            "tap": "__select_traveller__",
            "auto": "",
            "reply": "I'll tick one saved traveller. I won't create a new profile. Nothing paid.",
            "facts": [],
            "source": "local",
        }
    looks_details = bool(
        re.search(r"@|\bmale\b|\bfemale\b|\bother\b|\d{1,2}/\d{1,2}/\d{2,4}", raw, re.I)
        or indian_national(raw)
        or re.search(r"\b(?:use|fill)\b.+\b(?:mobile|phone)\b", raw, re.I)
    )
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
    if re.search(r"\b(skip|decline)\b", raw, re.I):
        from app.step_read import visible_forward

        label = visible_forward(hay, raw) or ("Continue" if re.search(r"\bcontinue\b", hay, re.I) else "")
        return {
            "goal": "skip",
            "intent": "book",
            "kind": "addons" if label.lower().startswith("skip") else (kind or "review"),
            "what": "the open page",
            "why": "you asked to leave the extras",
            "recommend": f"press {label}" if label else "the visible forward control",
            "tap": label,
            "auto": "1",
            "reply": f"I'll press {label or 'the visible control'} on this page. Nothing paid.",
            "facts": [],
            "source": "local",
        }
    if re.search(r"confirm(?: the)? fl|go on|go ahead|continue\b", raw, re.I) or re.fullmatch(
        r"yes|yep|yeah|ok|okay", raw, re.I
    ):
        pending = state.get("pending_step") if isinstance(state.get("pending_step"), dict) else {}
        from app.step_read import visible_forward

        label = visible_forward(hay, raw) or tap or str(pending.get("tap") or "")
        if re.search(r"\bcontinue\b", raw, re.I) and re.search(r"\bcontinue\b", hay, re.I):
            label = "Continue"
        elif re.fullmatch(r"yes|yep|yeah|ok|okay", raw, re.I):
            label = visible_forward(hay) or label
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
    if stored_fare_list(state) and re.search(r"\b\d{1,2}[:.]\d{2}\b", raw):
        clock = re.search(r"\b\d{1,2}[:.]\d{2}\b", raw)
        when = clock.group(0) if clock else ""
        return {
            "goal": "forward",
            "intent": "select",
            "kind": "results",
            "what": "the saved fare list",
            "why": "you named a departure time",
            "recommend": f"book the {when} on a site that can ticket",
            "tap": "",
            "auto": "",
            "reply": f"I'll book the {when} on a site that can take the ticket. Nothing paid.",
            "facts": list(vision.get("facts") or []),
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
    rows = state.get("options") if isinstance(state.get("options"), list) else []
    fares = ", ".join(
        f"{item.get('airline') or ''} {item.get('depart') or ''} ₹{item.get('price') or ''}".strip()
        for item in rows[:6]
        if isinstance(item, dict)
    )
    user = (
        f"Traveller: {text}\nSeller: {seller or 'unknown'}. Chosen departure: {hint or 'unknown'}.\n"
        f"Screenshot attached: {'yes' if image_path else 'NO — do not invent results'}.\n"
        f"Saved fare rows: {fares or 'none'}.\n"
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
    trip_ready = bool(state.get("origin") and state.get("destination") and state.get("depart"))
    from app.page_agent import site_error_page
    from app.step_read import safe_tap

    if site_error_page(page, ""):
        kind = "error"
        if wants_fresh_search(text):
            intent, goal, tap, reply = "search", "search", "", ""
        elif re.search(r"go to home", text or "", re.I):
            intent, goal = "book", "forward"
            tap = safe_tap("Go to home", page) or tap
        elif goal in {"forward", "skip"} and tap:
            intent = "book"
        elif re.fullmatch(r"yes|yep|yeah|ok|okay|sure", (text or "").strip(), re.I):
            intent, goal = "book", "forward"
            tap = safe_tap("Go to home", page) or safe_tap("Search Again", page) or tap
        if intent == "pay" or goal == "pay" or (reply and re.search(r"ready to pay", reply, re.I)):
            intent, goal, reply = "status", "ask", ""
    if wants_fresh_search(text) and trip_ready:
        intent = "search"
        goal = "search"
        kind = ""
        tap = ""
        reply = ""
    # Gemini goal wins for traveller actions; local is fallback when Gemini omitted goal.
    local_goal = str(local.get("goal") or "")
    if goal in {"pick_traveller", "select_traveller"} or (
        intent == "book"
        and re.search(
            r"\b(saved traveller|tick|select|pick one|click.+\bname)\b",
            f"{what} {recommend} {reply} {text}",
            re.I,
        )
        and not re.search(r"\b(delete|remove)\b", f"{goal} {what} {recommend}", re.I)
    ):
        goal = "pick_traveller"
        intent = "book"
        tap = "__select_traveller__"
    elif goal in {"delete_traveller", "remove_traveller"} or (
        intent == "book"
        and re.search(r"\b(delete|remove|clear).+\b(profile|traveller|name|duplicate)\b", f"{what} {recommend} {reply}", re.I)
    ):
        goal = "delete_traveller"
        intent = "book"
        tap = "__delete_traveller__"
    elif local_goal in {"pick_traveller", "delete_traveller"}:
        goal = local_goal
        intent = "book"
        tap = "__select_traveller__" if local_goal == "pick_traveller" else "__delete_traveller__"
    # Noted/saved reply + contact values → details action (never note-only).
    if _contact_value_in(text) or _wants_fill_field(text):
        if goal in {"ask", "status", ""} or re.search(
            r"\b(?:noted|saved|i(?:'ll| will) remember|got (?:it|those|that))\b",
            reply,
            re.I,
        ):
            goal, intent = "details", "details"
    if not shot and not page and not stored_fare_list(state):
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
        "source": "gemini" if data else str(local.get("source") or "local"),
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
    """True when this booking has a screenshot, a vision read, or a saved fare list."""
    if _shot_from(state):
        return True
    vision = state.get("last_vision") if isinstance(state.get("last_vision"), dict) else {}
    if vision.get("kind"):
        return True
    return stored_fare_list(state)


def _contact_value_in(text: str) -> bool:
    from app.kernel_otp import indian_national

    return bool(indian_national(text or "")) or bool(
        re.search(r"@|\bmale\b|\bfemale\b|\d{1,2}/\d{1,2}/\d{2,4}", text or "", re.I)
    )


def _wants_fill_field(text: str) -> bool:
    return bool(
        re.search(
            r"\b(?:fill|type|enter|put)\b.+\b(?:e-?mail|mail|mobile|phone|name|dob|gender)\b"
            r"|\bfill my e-?mail\b|\btype (?:my )?e-?mail\b",
            text or "",
            re.I,
        )
    )


def _wants_fare_compare(text: str) -> bool:
    """Check / compare fares across sites — even from a payment sheet."""
    raw = text or ""
    if not re.search(
        r"\b(?:prices?|fares?|costs?|how much|cheaper|compare|rates?)\b"
        r"|\bcheck (?:all )?(?:the )?(?:fares?|prices?)\b",
        raw,
        re.I,
    ):
        return False
    from app.search import named_seller

    return bool(
        named_seller(raw)
        or re.search(
            r"\b(?:another|other|different|all)\s+(?:sites?|sellers?|services?)\b"
            r"|\bacross (?:sites?|sellers?)\b",
            raw,
            re.I,
        )
    )


def _wants_bot_pay(text: str) -> bool:
    raw = text or ""
    from app.browser_hands import looks_like_card

    if re.search(
        r"\b(?:pay with (?:upi|card|debit|credit)|fill (?:my )?(?:upi|card|payment)|"
        r"use (?:my )?(?:upi|card)|pay (?:it |for me|now)|authorize pay|go ahead and pay)\b"
        r"|\b(?:upi id|vpa)\b",
        raw,
        re.I,
    ):
        return True
    if re.search(
        r"\b([a-z0-9.\-_]{2,}@(?:okaxis|okhdfcbank|okicici|oksbi|ybl|paytm|ibl|axl|apl|upi))\b",
        raw,
        re.I,
    ):
        return True
    return bool(looks_like_card(raw) or re.search(r"\b(?:cvv|cvc)\s*[:\-]?\s*\d{3,4}\b", raw, re.I))


def _wants_pay_self(text: str) -> bool:
    return bool(
        re.search(
            r"\b(?:pay (?:myself|yourself|on (?:the )?link|on my own)|i'?ll pay|"
            r"send (?:me )?(?:the )?link|i will pay)\b",
            text or "",
            re.I,
        )
    )


def _message_implies_action(text: str) -> bool:
    """True when the user message itself asks for a page action this turn."""
    raw = text or ""
    if _contact_value_in(raw) or _wants_fill_field(raw) or _wants_fare_compare(raw) or _wants_bot_pay(raw) or _wants_pay_self(raw):
        return True
    if re.search(
        r"\b(?:delete|remove|erase|clear)\b.+\b(?:travellers?|travelers?|profiles?|names?|passengers?|duplicates?)\b"
        r"|\bget rid of\b.+\b(?:profile|name|traveller|traveler|duplicate)\b"
        r"|\b(?:click|pick|select|tick|choose|use|tap)\b.+\b(?:name|names|traveller|traveler|profile|saved|one of)\b"
        r"|\bpick one\b|\bone of the names\b|\bsaved (?:traveller|traveler|profile)\b"
        r"|\buse (?:the |an? )?existing\b"
        r"|\b(?:continue|go on|skip(?:\s+add)?|confirm|book it|book this)\b"
        r"|\b\d{1,2}[:.]\d{2}\b",
        raw,
        re.I,
    ):
        return True
    from app.search import named_seller

    return bool(named_seller(raw))


def _contact_sheet_open(sheet: str, state: dict[str, Any]) -> bool:
    page = str(state.get("stuck_page") or "")
    contact_copy = bool(
        re.search(
            r"please enter (?:an? )?(?:email|mobile)|contact details|add contact|traveller details",
            page,
            re.I,
        )
    )
    # Real pay sheet is not contact — unless the page still shows contact fields.
    if sheet == "pay" or str(state.get("stuck_kind") or "") == "pay":
        return contact_copy
    return bool(
        sheet in {"form", "passenger", "review", "other", ""}
        or str(state.get("stuck_kind") or "") in {"form", "passenger", "review", "name_confirm"}
        or contact_copy
    )


# Intent → allowed do values. Align gate fails if proposed do is outside this set.
_GOAL_DO: dict[str, set[str]] = {
    "details": {"details"},
    "forward": {"book"},
    "skip": {"book"},
    "pick_traveller": {"book"},
    "delete_traveller": {"book"},
    "search": {"search"},
    "select": {"book"},
    "login": {"book"},
    "ask": {"reply", "ask_trip", "pay_ask"},
    "stop": {"reply"},
    "pay": {"pay", "pay_ask"},
    "pay_self": {"pay_self"},
    "cancel": {"reply"},
    "status": {"reply", "pay_ask"},
    "chat": {"reply"},
    "book": {"book"},
}


def _align_ok(goal: str, do: str) -> bool:
    allowed = _GOAL_DO.get(goal)
    if not allowed:
        return True
    return do in allowed


def _intent_first(text: str, state: dict[str, Any], turn: dict[str, Any]) -> tuple[str, str]:
    """User message wins over screenshot-biased Gemini goals."""
    goal = str(turn.get("goal") or "").strip().lower()
    intent = str(turn.get("intent") or "").strip().lower()
    raw = (text or "").strip()
    tap = str(turn.get("tap") or "")
    vision = state.get("last_vision") if isinstance(state.get("last_vision"), dict) else {}
    on_pay = (
        str(turn.get("kind") or vision.get("kind") or state.get("stuck_kind") or "") == "pay"
        or str(state.get("stage") or "") == "payment"
    )

    if re.search(r"\b(?:stop|cancel|never mind)\b", raw, re.I) and not re.search(r"\bbook\b", raw, re.I):
        return "stop", "cancel"
    if _wants_pay_self(raw) or intent == "pay_self" or goal == "pay_self":
        return "pay_self", "pay_self"
    if _wants_fare_compare(raw) or wants_fresh_search(raw):
        return "search", "search"
    if _wants_bot_pay(raw):
        return "pay", "pay"
    # Bare yes/ok on the pay sheet = payment handoff options, not Continue/Book.
    if on_pay and re.fullmatch(r"yes|yep|yeah|ok|okay|sure", raw, re.I):
        return "pay", "pay"
    if re.search(
        r"\b(?:delete|remove|erase|clear)\b.+\b(?:travellers?|travelers?|profiles?|names?|passengers?|duplicates?)\b"
        r"|\bget rid of\b.+\b(?:profile|name|traveller|traveler|duplicate)\b",
        raw,
        re.I,
    ) or goal == "delete_traveller" or tap in {"__delete_traveller__", "__remove_traveller__"}:
        return "delete_traveller", "book"
    if re.search(
        r"\b(?:click|pick|select|tick|choose|use|tap)\b.+\b(?:name|names|traveller|traveler|profile|saved|one of)\b"
        r"|\bpick one\b|\bone of the names\b|\bsaved (?:traveller|traveler|profile)\b"
        r"|\buse (?:the |an? )?existing\b",
        raw,
        re.I,
    ) or goal == "pick_traveller" or tap in {"__select_traveller__", "__pick_traveller__"}:
        return "pick_traveller", "book"
    if _contact_value_in(raw) or _wants_fill_field(raw) or goal == "details" or intent == "details":
        # Passenger values beat a stale pay intent from the screenshot.
        return "details", "details"
    if goal in {"forward", "skip"} or intent in {"book", "select"}:
        if re.search(r"\b(?:continue|go on|skip(?:\s+add)?|confirm|book it|book this)\b", raw, re.I) or goal in {
            "forward",
            "skip",
        }:
            return ("skip" if goal == "skip" or re.search(r"\bskip\b", raw, re.I) else "forward"), "book"
    if goal == "search" or intent == "search":
        return "search", "search"
    if goal == "login" or intent == "login":
        return "login", "book"
    if goal == "pay" or intent == "pay":
        # Screenshot-only pay: keep pay only if message is empty/yes or explicitly about paying.
        if not raw or re.fullmatch(r"yes|yep|yeah|ok|okay|sure", raw, re.I) or _wants_bot_pay(raw):
            return "pay", "pay"
        # Otherwise treat as ask — do not force pay_stop over a new wish.
        return "ask", "status"
    if goal == "ask" or intent == "status":
        return "ask", "status"
    if goal == "stop":
        return "stop", "cancel"
    return goal or intent or "ask", intent or "status"


def _plan(
    *,
    do: str,
    kind: str,
    tap: str = "",
    turn: dict[str, Any],
    goal: str,
    intent: str,
    reply: str = "",
) -> dict[str, Any]:
    out = {
        "do": do,
        "kind": kind,
        "tap": tap,
        "turn": {**turn, "goal": goal, "intent": intent},
        "reply": reply,
        "aligned": True,
    }
    if not _align_ok(goal, do):
        out["do"] = "reply"
        out["kind"] = "status"
        out["tap"] = ""
        out["aligned"] = False
        out["reply"] = reply or format_turn_reply(turn) or (
            "I heard that, but I won't act until it matches — say what you want: "
            "fill details, continue, pick a name, check fares, pay with UPI/card, or pay yourself. Nothing paid."
        )
    return out


def align_action(text: str, state: dict[str, Any], turn: dict[str, Any] | None = None) -> dict[str, Any]:
    """Intent first → propose do → align gate. Mismatched intent/action never executes."""
    turn = turn or interpret_turn(text, state)
    vision = state.get("last_vision") if isinstance(state.get("last_vision"), dict) else {}
    sheet = str(turn.get("kind") or vision.get("kind") or "")
    if not sheet and stored_fare_list(state):
        sheet = "results"
    tap = str(turn.get("tap") or "")
    live = has_live_facts(state)
    trip = bool(state.get("origin") and state.get("destination") and state.get("depart"))
    clock = bool(re.search(r"\b\d{1,2}[:.]\d{2}\b", text or ""))
    goal, intent = _intent_first(text, state, turn)
    turn = {**turn, "goal": goal, "intent": intent}

    if goal == "stop" or intent == "cancel":
        return _plan(do="reply", kind="cancel", turn=turn, goal="stop", intent="cancel", reply=str(turn.get("reply") or ""))
    if goal == "pay_self":
        return _plan(do="pay_self", kind="pay_self", turn=turn, goal="pay_self", intent="pay_self")
    if wants_fresh_search(text) and trip:
        return _plan(do="search", kind="search", turn=turn, goal="search", intent="search")
    if goal == "search" and trip:
        return _plan(do="search", kind="search", turn=turn, goal="search", intent="search")
    if goal == "delete_traveller":
        return _plan(
            do="book",
            kind="book",
            tap="__delete_traveller__",
            turn=turn,
            goal="delete_traveller",
            intent="book",
        )
    if goal == "pick_traveller":
        return _plan(
            do="book",
            kind="book",
            tap="__select_traveller__",
            turn=turn,
            goal="pick_traveller",
            intent="book",
        )

    # Contact fill — page grounds where, intent says details.
    if goal == "details" and live:
        if sheet in {"addons", "upsell", "fare_sheet"} and not _contact_sheet_open(sheet, state):
            return _plan(do="book", kind="book", tap=tap or "Skip add-ons", turn=turn, goal="forward", intent="book")
        if _contact_sheet_open(sheet, state) or sheet in {"form", "passenger", "review", "other", ""}:
            return _plan(do="details", kind="details", tap=tap, turn=turn, goal="details", intent="details")

    # Payment: ask for UPI/card, or fill when they authorized / sent secrets.
    on_pay = sheet == "pay" or str(state.get("stuck_kind") or "") == "pay"
    if goal == "pay" or (on_pay and goal in {"ask", "pay", "status"} and not _wants_fare_compare(text)):
        from app.search import named_seller

        named = named_seller(text)
        wants_login = bool(
            re.search(
                r"\b(?:log\s*in|sign\s*in|with (?:an? )?otp|use (?:an? )?otp|use (?:my )?account)\b",
                text or "",
                re.I,
            )
        )
        if wants_login or (named and not _wants_bot_pay(text) and goal != "pay"):
            if wants_login:
                state["kernel_auth_mode"] = "login"
                state.pop("kernel_logged_in", None)
            state.pop("stuck_kind", None)
            state.pop("pending_step", None)
            state.pop("chosen_tap", None)
            g = "login" if wants_login else "forward"
            return _plan(do="book", kind="book", tap="", turn=turn, goal=g, intent="book" if wants_login else "select")
        has_vault = bool(state.get("payment_on_file"))
        if _wants_bot_pay(text) and (
            has_vault
            or re.search(r"@", text or "")
            or re.search(r"\d{13,19}", re.sub(r"\D", "", text or ""))
            or re.search(r"\bcvv\b|\bcvc\b", text or "", re.I)
        ):
            return _plan(do="pay", kind="pay", turn=turn, goal="pay", intent="pay")
        # Authorized pay with vault already on file.
        if goal == "pay" and has_vault and re.search(
            r"\b(?:pay with|fill|use my|pay now|go ahead and pay|authorize)\b", text or "", re.I
        ):
            return _plan(do="pay", kind="pay", turn=turn, goal="pay", intent="pay")
        return _plan(do="pay_ask", kind="pay", turn=turn, goal="pay" if goal == "pay" else "ask", intent="pay" if goal == "pay" else "status")

    from app.page_agent import site_error_page
    from app.step_read import safe_tap

    stuck_text = str(state.get("stuck_page") or "")
    if sheet == "error" or site_error_page(stuck_text, ""):
        if wants_fresh_search(text):
            return _plan(do="search", kind="search", turn=turn, goal="search", intent="search")
        from app.search import named_seller, seller_can_book

        pending = str(state.get("pending_seller_switch") or "").strip().lower()
        yes = bool(re.fullmatch(r"yes|yep|yeah|ok|okay|sure", (text or "").strip(), re.I))
        guest = str(state.get("kernel_auth_mode") or "").strip().lower() == "guest"
        login_mode = str(state.get("kernel_auth_mode") or "").strip().lower() == "login"
        reopen = bool(
            re.search(r"\btry again\b|\bagain\b|\bgo on\b|\breopen\b|\bfresh\b", text or "", re.I)
            or clock
            or goal in {"forward", "skip", "login"}
            or intent in {"book", "select"}
            or yes
        )
        if (guest or login_mode) and reopen and not named_seller(text):
            state.pop("kernel_session_id", None)
            state.pop("pending_seller_switch", None)
            return _plan(do="book", kind="book", turn=turn, goal="forward", intent="book")
        if named_seller(text) or clock or (yes and seller_can_book(pending)):
            return _plan(do="book", kind="book", turn=turn, goal="forward", intent="book")
        home = safe_tap("Go to home", stuck_text) or safe_tap("Search Again", stuck_text) or safe_tap("Home", stuck_text)
        if goal in {"forward", "skip", "login"} or intent in {"book", "select"}:
            return _plan(do="book", kind="book", tap=tap or home or "", turn=turn, goal="forward", intent="book")
        return _plan(
            do="reply",
            kind="error",
            turn=turn,
            goal="ask",
            intent="status",
            reply=format_turn_reply(turn),
        )

    if not live:
        if trip:
            return _plan(do="search", kind="search", turn=turn, goal="search", intent="search")
        return _plan(
            do="ask_trip",
            kind="search",
            turn=turn,
            goal="ask",
            intent="search",
            reply=str(turn.get("reply") or ""),
        )

    if sheet in {"results", "loading"}:
        if clock or goal in {"forward", "select"} or intent in {"select", "book"}:
            return _plan(
                do="book",
                kind="select" if clock else "book",
                tap=tap,
                turn=turn,
                goal="select" if clock else "forward",
                intent="select" if clock else "book",
            )
        if goal == "ask":
            return _plan(do="reply", kind="status", tap=tap, turn=turn, goal="ask", intent="status", reply=format_turn_reply(turn))
        return _plan(do="search", kind="search", turn=turn, goal="search", intent="search")

    stuck_page = str(state.get("stuck_page") or "")
    if sheet == "pay" and (
        re.search(r"add contact|please enter mobile|please enter email", stuck_page, re.I)
        or re.search(r"add contact|please enter mobile", str(vision.get("what") or ""), re.I)
    ):
        sheet = "form"
        state.pop("stuck_kind", None)
        if goal == "details":
            return _plan(do="details", kind="details", tap=tap, turn=turn, goal="details", intent="details")

    if sheet in {"addons", "upsell", "review", "fare_sheet"}:
        if goal == "ask" and not _message_implies_action(text):
            return _plan(do="reply", kind="status", tap=tap, turn=turn, goal="ask", intent="status", reply=format_turn_reply(turn))
        from app.step_read import visible_forward

        page = stuck_page
        if re.search(r"\bcontinue\b", text or "", re.I):
            tap = "Continue"
        else:
            grounded = visible_forward(page, text) or visible_forward(page, tap)
            if grounded:
                tap = grounded
            elif re.search(r"skip add", tap, re.I) and not re.search(r"skip add", page, re.I):
                tap = visible_forward(page) or ""
            elif sheet == "addons" and not tap:
                tap = ""
            elif not tap:
                tap = "Continue"
        g = "skip" if goal == "skip" or re.search(r"\bskip\b", text or "", re.I) else "forward"
        return _plan(do="book", kind="book", tap=tap, turn=turn, goal=g, intent="book")

    if sheet in {"form", "passenger"} or str(state.get("stuck_kind") or "") in {"form", "passenger"}:
        if goal in {"forward", "skip", "login"} or intent in {"book", "select"} or re.search(
            r"\b(?:continue|go on|skip(?:\s+add)?|confirm)\b", text or "", re.I
        ):
            from app.step_read import visible_forward

            page = str(state.get("stuck_page") or "")
            return _plan(
                do="book",
                kind="book",
                tap=tap or visible_forward(page) or "Continue",
                turn=turn,
                goal="forward",
                intent="book",
            )
        return _plan(
            do="reply",
            kind="status",
            turn=turn,
            goal="ask",
            intent="status",
            reply=format_turn_reply(turn)
            or (
                "Saved travellers are on screen. Say pick one, delete a profile, or send a mobile "
                "if you want contact filled. Nothing paid."
            ),
        )

    if goal in {"forward", "skip", "login", "select"} or intent in {"book", "select"}:
        return _plan(do="book", kind="book", tap=tap, turn=turn, goal=goal or "forward", intent="book")
    if trip and goal == "search":
        return _plan(do="search", kind="search", turn=turn, goal="search", intent="search")
    if goal == "ask":
        return _plan(do="reply", kind="status", tap=tap, turn=turn, goal="ask", intent="status", reply=format_turn_reply(turn))
    if _message_implies_action(text) and live:
        return _plan(do="book", kind="book", tap=tap or "Continue", turn=turn, goal="forward", intent="book")
    return _plan(
        do="reply",
        kind=intent or "chat",
        tap=tap,
        turn=turn,
        goal=goal or "ask",
        intent=intent or "chat",
        reply=format_turn_reply(turn),
    )

