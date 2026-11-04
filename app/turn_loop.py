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
    '{"goal":"skip|forward|ask|stop|pay|search|details|pick_traveller|delete_traveller",'
    '"intent":"search|select|book|login|details|pay|pay_self|cancel|status|chat",'
    '"kind":"upsell|form|login|otp|review|results|pay|unavailable|addons|other",'
    '"what":"","why":"","recommend":"","tap":"","auto":false,"reply":"","facts":[]}\n'
    "goal=ask (intent=status) when they ask which step, what to do next, check, or what you see. "
    "Name the screenshot and recommend the safe next tap.\n"
    "goal=forward (intent=book) when they confirm the flight, continue, go on, or yes to your last rec.\n"
    "goal=skip (intent=book) when they skip extras.\n"
    "goal=pick_traveller (intent=book) when they want you to tick/select one existing saved name on the "
    "traveller list (any wording: click a name, use the saved one, pick that person) — tap empty; "
    "do NOT fill contact mobile and do NOT create a new traveller.\n"
    "goal=delete_traveller (intent=book) when they want a saved profile removed "
    "(delete/remove/clear a duplicate name or profile) — tap empty; never pay.\n"
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
        r"|\bone of the names\b|\bsaved (?:traveller|traveler|profile)\b"
        r"|\buse (?:the |an? )?existing\b|\bdon'?t create (?:a )?new\b",
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
    # Gemini paraphrases for saved-traveller actions → stable taps for checkout rails.
    local_goal = str(local.get("goal") or "")
    if local_goal in {"pick_traveller", "delete_traveller"}:
        goal = local_goal
        intent = "book"
        tap = "__select_traveller__" if local_goal == "pick_traveller" else "__delete_traveller__"
    elif goal in {"pick_traveller", "select_traveller"} or (
        intent == "book"
        and re.search(r"\b(saved traveller|tick|select).+\b(name|traveller|profile)\b", f"{what} {recommend} {reply}", re.I)
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


def align_action(text: str, state: dict[str, Any], turn: dict[str, Any] | None = None) -> dict[str, Any]:
    """Bind intent + page facts to one action. Empty facts cannot mean 'results are ready'."""
    turn = turn or interpret_turn(text, state)
    vision = state.get("last_vision") if isinstance(state.get("last_vision"), dict) else {}
    sheet = str(turn.get("kind") or vision.get("kind") or "")
    if not sheet and stored_fare_list(state):
        sheet = "results"
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

    if wants_fresh_search(text) and trip:
        return {"do": "search", "kind": "search", "tap": "", "turn": turn, "reply": ""}

    # Gemini said ask/status — clarify; do not invent a click.
    if goal == "ask" or (intent == "status" and goal in {"ask", ""}):
        if not (goal in {"pick_traveller", "delete_traveller", "forward", "skip"} or clock):
            return {
                "do": "reply",
                "kind": "status",
                "tap": "",
                "turn": turn,
                "reply": format_turn_reply(turn)
                or (
                    "I'm on the open booking page — say what you want: click a saved name, "
                    "delete a profile, continue, or skip. Nothing paid."
                ),
            }

    # Saved-name pick/delete: Gemini goal first, then paraphrase regex.
    raw_ask = (text or "").strip()
    if tap in {"__delete_traveller__", "__remove_traveller__"} or goal == "delete_traveller" or re.search(
        r"\b(?:delete|remove|erase|clear)\b.+\b(?:travellers?|travelers?|profiles?|names?|passengers?|duplicates?)\b"
        r"|\bdelete (?:one|a|the|this|that|some)\b.+\b(?:names?|profiles?|travellers?|travelers?)\b"
        r"|\bget rid of\b.+\b(?:profile|name|traveller|traveler|duplicate)\b",
        raw_ask,
        re.I,
    ):
        return {
            "do": "book",
            "kind": "book",
            "tap": "__delete_traveller__",
            "turn": {**turn, "intent": "book", "goal": "delete_traveller"},
            "reply": "",
        }
    if tap in {"__select_traveller__", "__pick_traveller__"} or goal == "pick_traveller" or re.search(
        r"\b(?:click|pick|select|tick|choose|use|tap)\b.+\b(?:name|names|traveller|traveler|profile|saved|one of)\b"
        r"|\bone of the names\b|\bsaved (?:traveller|traveler|profile)\b"
        r"|\buse (?:the |an? )?existing\b|\bdon'?t create (?:a )?new\b",
        raw_ask,
        re.I,
    ):
        return {
            "do": "book",
            "kind": "book",
            "tap": "__select_traveller__",
            "turn": {**turn, "intent": "book", "goal": "pick_traveller"},
            "reply": "",
        }

    from app.page_agent import site_error_page
    from app.step_read import safe_tap

    stuck_text = str(state.get("stuck_page") or "")
    if sheet == "error" or site_error_page(stuck_text, ""):
        if wants_fresh_search(text):
            return {"do": "search", "kind": "search", "tap": "", "turn": turn, "reply": ""}
        from app.search import named_seller

        from app.search import seller_can_book

        pending = str(state.get("pending_seller_switch") or "").strip().lower()
        yes = bool(re.fullmatch(r"yes|yep|yeah|ok|okay|sure", (text or "").strip(), re.I))
        guest = str(state.get("kernel_auth_mode") or "").strip().lower() == "guest"
        login_mode = str(state.get("kernel_auth_mode") or "").strip().lower() == "login"
        # Guest/login Kernel: reopen the same flight on a fresh tab — never a seller hop.
        reopen = bool(
            re.search(r"\btry again\b|\bagain\b|\bgo on\b|\breopen\b|\bfresh\b", text or "", re.I)
            or clock
            or intent in {"book", "select"}
            or goal in {"forward", "skip"}
            or yes
        )
        if (guest or login_mode) and reopen and not named_seller(text):
            state.pop("kernel_session_id", None)
            state.pop("pending_seller_switch", None)
            return {"do": "book", "kind": "book", "tap": "", "turn": turn, "reply": ""}
        # Accept the recommended bookable travel site — never silent switch.
        if named_seller(text) or clock or (yes and seller_can_book(pending)):
            return {"do": "book", "kind": "book", "tap": "", "turn": turn, "reply": ""}
        home = safe_tap("Go to home", stuck_text) or safe_tap("Search Again", stuck_text) or safe_tap("Home", stuck_text)
        if intent in {"book", "select"} or goal in {"forward", "skip"}:
            # Yes-without-pending stays on recovery controls; do not invent Ixigo.
            return {
                "do": "book",
                "kind": "book",
                "tap": tap or home or "",
                "turn": turn,
                "reply": "",
            }
        return {"do": "reply", "kind": "error", "tap": "", "turn": turn, "reply": format_turn_reply(turn)}

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
        from app.step_read import visible_forward

        page = str(state.get("stuck_page") or "")
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
        return {"do": "book", "kind": "book", "tap": tap, "turn": turn, "reply": ""}

    # Stale "pay" vision while contact is still open — treat as form, not payment handoff.
    stuck_page = str(state.get("stuck_page") or "")
    if sheet == "pay" and (
        re.search(r"add contact|please enter mobile|please enter email", stuck_page, re.I)
        or re.search(r"add contact|please enter mobile", str(vision.get("what") or ""), re.I)
    ):
        sheet = "form"
        state.pop("stuck_kind", None)

    from app.kernel_otp import indian_national as _nat_mobile

    if _nat_mobile(text or "") and (
        sheet in {"form", "passenger", "pay", "other", ""}
        or intent == "details"
        or str(state.get("stuck_kind") or "") in {"form", "pay", "passenger"}
    ):
        intent = "details"

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
        from app.search import named_seller

        named = named_seller(text)
        bare_yes = bool(re.fullmatch(r"yes|yep|yeah|ok|okay|sure", (text or "").strip(), re.I))
        wants_login = bool(
            re.search(
                r"\b(?:log\s*in|sign\s*in|with (?:an? )?otp|use (?:an? )?otp|use (?:my )?account)\b",
                text or "",
                re.I,
            )
        )
        price_elsewhere = bool(
            re.search(r"\b(?:price|fare|cost|how much|cheaper|compare|rates?)\b", text or "", re.I)
            and (
                named
                or re.search(r"\b(?:another|other|different)\s+(?:site|seller|service)\b", text or "", re.I)
            )
        )
        # Payment is a stop — unless they want another site, a fare check, or to log in.
        # Bare "yes" stays on the pay handoff (do not treat as a seller hop).
        if wants_fresh_search(text) and trip:
            return {"do": "search", "kind": "search", "tap": "", "turn": turn, "reply": ""}
        leave_pay = bool(
            (not bare_yes)
            and (
                named
                or wants_login
                or price_elsewhere
                or clock
                or intent in {"login", "search", "select"}
            )
        )
        if leave_pay:
            if wants_login:
                state["kernel_auth_mode"] = "login"
                state.pop("kernel_logged_in", None)
            state.pop("stuck_kind", None)
            state.pop("pending_step", None)
            state.pop("chosen_tap", None)
            if price_elsewhere or (intent == "search" and not clock and not wants_login):
                return {"do": "search", "kind": "search", "tap": "", "turn": turn, "reply": ""}
            return {"do": "book", "kind": "book", "tap": "", "turn": turn, "reply": ""}
        return {"do": "pay_stop", "kind": "pay", "tap": "", "turn": turn, "reply": ""}

    if intent in {"book", "select"} or goal in {"skip", "forward"}:
        return {"do": "book", "kind": "book", "tap": tap, "turn": turn, "reply": ""}
    if trip and intent in {"search", ""}:
        return {"do": "search", "kind": "search", "tap": "", "turn": turn, "reply": ""}
    if intent == "status" or goal == "ask":
        return {"do": "reply", "kind": "status", "tap": tap, "turn": turn, "reply": format_turn_reply(turn)}
    return {"do": "reply", "kind": intent or "chat", "tap": tap, "turn": turn, "reply": format_turn_reply(turn)}

