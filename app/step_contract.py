"""Step contracts: name the page, list facts, bind intent to an action until success.

Conscience + vision fill the contract. Checkout loops the action until success_when
or a new block — never the same "reply yes" spam after the traveller already agreed.
"""

from __future__ import annotations

import re
from typing import Any


def _facts_from_text(page: str, vision: dict[str, Any] | None = None) -> list[str]:
    hay = page or ""
    facts: list[str] = []
    if vision:
        for item in vision.get("facts") or []:
            line = re.sub(r"\s+", " ", str(item or "")).strip()
            if line and line not in facts:
                facts.append(line[:140])
        if vision.get("seat_map") and not any(re.search(r"seat", f, re.I) for f in facts):
            facts.append("seat map is on screen")
        if vision.get("meals_offered") and not any(re.search(r"meal", f, re.I) for f in facts):
            facts.append("meals are offered")
    if re.search(r"choose add-ons|add seats|seat map|no seat added|add seats", hay, re.I):
        if not any(re.search(r"seat", f, re.I) for f in facts):
            if re.search(r"no seat added|0/\d+\s*selected|no seat", hay, re.I):
                facts.append("seat map open — no seat selected yet")
            else:
                facts.append("seat selection is on this page")
    if re.search(r"add (?:delicious )?meals|missed adding meal|0/\d+\s*selected", hay, re.I):
        if not any(re.search(r"meal", f, re.I) for f in facts):
            facts.append("meals are offered (optional)")
    if re.search(r"extra baggage|check-in baggage|additional check-in", hay, re.I):
        if not any(re.search(r"bag", f, re.I) for f in facts):
            facts.append("extra baggage is offered (optional)")
    if re.search(r"missed adding meal|okay,?\s*let me add", hay, re.I):
        facts.append("nag: You missed adding meals/seats — Skip add-ons dismisses it")
    if re.search(r"skip add-?ons", hay, re.I):
        if not any(re.search(r"skip", f, re.I) for f in facts):
            facts.append("Skip add-ons is available to leave without extras")
    if re.search(r"free cancellation|\bassured\b", hay, re.I) and re.search(
        r"no,?\s*thanks|secure my trip|assure my trip", hay, re.I
    ):
        facts.append("paid Free Cancellation / Assured is offered")
    return facts[:6]


def build_contract(
    page: str,
    *,
    stage: str = "",
    seller: str = "",
    hint: str = "",
    vision: dict[str, Any] | None = None,
    user_goal: str = "",
) -> dict[str, Any]:
    """One booking sheet → structured next move."""
    hay = page or ""
    shown = (stage or "").strip().lower()
    if not shown:
        if re.search(r"choose add-ons|skip add-?ons|add seats|add meal|missed adding", hay, re.I):
            shown = "addons"
        elif re.search(r"no longer available|search again", hay, re.I):
            shown = "unavailable"
        elif re.search(r"\bBook\b", hay) and re.search(r"₹", hay):
            shown = "results"

    facts = _facts_from_text(hay, vision)
    goal = (user_goal or "").strip().lower()
    wants_seat = bool(re.search(r"\bseats?\b|\bwindow\b|\baisle\b", goal))
    wants_skip = goal in {"skip", "yes", "decline", "no thanks"} or goal.startswith("skip")

    action = "ask"
    tap = ""
    requires_user = True
    success_when = "page leaves this step"
    recommend = "tell me what to press"

    if shown == "addons" or any(re.search(r"seat|meal|bag|skip add", f, re.I) for f in facts):
        shown = "addons"
        if wants_seat:
            action = "ask"
            tap = ""
            requires_user = True
            recommend = "say which seat (or a price band) and I'll tap it — I will not guess a paid seat"
            success_when = "a seat is selected or you say skip"
        else:
            action = "skip_extras"
            tap = "Skip add-ons"
            requires_user = False if wants_skip or not wants_seat else True
            # Default Instinct path: skip optional extras unless they asked for seats.
            if not wants_seat:
                requires_user = False
            recommend = (
                "Skip add-ons (and the missed meals/seats nag if it appears) — seats/meals stay off unless you ask"
            )
            success_when = "left Choose add-ons / no seat map"
    elif shown == "unavailable":
        action = "search_again"
        tap = "Search Again"
        requires_user = True
        recommend = "reply search again or name another seller"
        success_when = "back on a fare list"
    elif shown == "results":
        action = "book_time"
        tap = ""
        requires_user = not bool(hint)
        recommend = f"Book the {hint}" if hint else "reply with a departure time"
        success_when = "fare sheet / review / login opened"

    what = {
        "addons": f"{seller or 'Site'} Choose add-ons (seats, meals, bags)",
        "unavailable": f"{seller or 'Site'} sold-out / search again",
        "results": f"{seller or 'Site'} fare list",
    }.get(shown, f"{seller or 'Site'} step")

    return {
        "stage": shown or "other",
        "what": what,
        "facts": facts,
        "options": [tap] if tap else [],
        "action": action,
        "tap": tap,
        "requires_user": requires_user,
        "recommend": recommend,
        "success_when": success_when,
        "hint": hint or "",
        "seller": seller or "",
    }


def format_contract_stuck(contract: dict[str, Any], *, which: str = "that flight") -> str:
    """WhatsApp stuck line that always surfaces important facts (e.g. seat map)."""
    facts = contract.get("facts") or []
    fact_bit = (" " + "; ".join(facts) + ".") if facts else ""
    tap = contract.get("tap") or ""
    rec = contract.get("recommend") or "tell me what to press"
    if contract.get("requires_user") and tap:
        nxt = f" Reply yes and I'll press {tap}."
    elif contract.get("requires_user"):
        nxt = " " + rec
    else:
        nxt = f" I'll press {tap or 'the safe control'} without waiting." if tap else f" {rec}"
    return (
        f"Stuck: {contract.get('what') or 'this step'} for {which}.{fact_bit} "
        f"That's optional unless you asked for it. I recommend {rec}.{nxt} Nothing paid."
    )


def left_addons(page: str, stage: str = "") -> bool:
    """True when Choose add-ons / seat map is no longer the step."""
    if (stage or "") in {"review", "passenger", "pay", "login", "otp", "error", "unavailable"}:
        return True
    hay = page or ""
    if re.search(r"choose add-ons|add seats|seat map|missed adding meal", hay, re.I):
        return False
    if re.search(r"skip add-?ons", hay, re.I) and re.search(r"add meal|extra baggage", hay, re.I):
        return False
    return True
