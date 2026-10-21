from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

from app import db, intent, jobs, llm, screenshots, vault
from app.reply import Reply
from app.search import resolve_place


_VOICE = (
    "You are Guide, texting on WhatsApp as a travel agent. Sound like a person, not a menu.\n"
    "Return JSON only: {\"reply\":\"...\",\"reaction\":\"👍\"}\n"
    "reply: 1 or 2 short sentences. No bullets, no \"Reply YES\", no option menus.\n"
    "Do not invent prices, airlines, or flight times. A screenshot shows the fares.\n"
    "Never say a ticket is booked or that payment happened.\n"
    "Never say a search is still running, and never promise screenshots later.\n"
    "Do not start with Yes unless allowed_to_agree is true.\n"
    "Ask only for the missing piece. If when is already filled, do not ask the date again.\n"
    "reaction: one emoji for their message. 👍 when you understood, ✈️ when results are ready, "
    "🤔 when you still need something, 👌 when you drop it, ✅ only if allowed_to_agree.\n"
    "Voice to copy:\n"
    "- Which date, and one-way or return?\n"
    "- Got it, 2 Dec one way. Jorhat to Bengaluru, right? Just confirm and I search.\n"
    "- Searching Jorhat to Bengaluru for 2 Dec now. I'll bring back options before booking anything.\n"
    "- Jorhat to Bengaluru for 2 Dec — live options below. I have not bought a ticket.\n"
    "- Want me to book the first one? I won't confirm the ticket until you say so.\n"
    "- Yes. Opening that page. I stop before payment."
)

_READ = (
    "You read a WhatsApp message for a travel agent and decide what to do.\n"
    "Return JSON only with keys: action, origin, destination, depart, round_trip, reply, reaction.\n"
    "action is one of: ask, confirm, search, chat.\n"
    "search ONLY when both cities were said by the traveller (this message or already in state as route_explicit) "
    "AND the travel date is known.\n"
    "confirm when you are guessing a route and need a yes before searching.\n"
    "ask when a city, date, or one-way/return is still missing.\n"
    "Never invent a city that is not in the message, the history, or the state.\n"
    "depart is YYYY-MM-DD or empty. round_trip is true, false, or null.\n"
    "reply: 1 or 2 short WhatsApp sentences in the same voice as a person at a travel desk. "
    "Do not start with Yes. Do not claim anything is booked.\n"
    "reaction: one emoji."
)


def _load(user: dict[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(user.get("interests_json") or "{}")
    except json.JSONDecodeError:
        return {}


def _save(phone: str, state: dict[str, Any]) -> None:
    data = dict(state)
    data.pop("_pending_apply", None)
    db.update_user(phone, interests=data)


def _finish(phone: str, state: dict[str, Any], reply: Reply) -> Reply:
    _save(phone, state)
    db.add_message(phone, "assistant", reply.text)
    return reply


def handle_message(phone: str, body: str) -> Reply:
    text = (body or "").strip()
    if not text:
        return Reply.of("Hey. Where are you flying, and when?", emoji="👋")

    user = db.upsert_user(phone)
    state = _load(user)
    db.add_message(phone, "user", text)
    history = db.recent_messages(phone, 8)
    prior = history[:-1] if history and history[-1].get("body") == text else history

    profile_m = re.match(r"^SAVE\s+PROFILE\s+(.+)$", text, flags=re.I | re.S)
    if profile_m:
        reply = _save_profile(phone, state, profile_m.group(1))
        return _finish(phone, _load(db.get_user(phone) or {}), Reply.of(reply, emoji="🧳"))

    save_m = re.match(r"^SAVE\s+(\w+)\s+(\S+)\s+(.+)$", text, flags=re.I | re.S)
    if save_m:
        reply = vault.save_login(phone, save_m.group(1), save_m.group(2), save_m.group(3).strip())
        return _finish(phone, state, Reply.of(reply, emoji="🔐"))

    from app.travel_agent import handle_turn

    return handle_turn(phone, text, state, prior)


def _resolve(
    text: str,
    state: dict[str, Any],
    prior: list[dict[str, Any]],
) -> tuple[str, dict[str, Any], dict[str, Any], str | None, str]:
    state = dict(state)
    updates = intent.parse_updates(text)
    kind = (intent.classify_heuristic(text) or {}).get("intent")
    state = _merge(state, updates)

    if kind == "greet" and not updates:
        if state.get("origin") or state.get("destination"):
            facts = _facts(state, "ask")
            return "ask", state, facts, _fallback("ask", facts), "👋"
        return "greet", state, _facts(state, "greet"), "Hey. Where are you flying, and when?", "👋"

    if kind == "cancel":
        return _cancel(state)

    if kind == "status":
        return "status", state, _facts(state, "status"), None, ""

    if kind == "vault":
        return "chat", state, _facts(state, "chat"), vault.vault_help(), "🔐"

    if _wants_results(text):
        if _ready(state):
            state["route_explicit"] = True
            state["asking"] = ""
            return "search", state, _facts(state, "search"), None, "👍"
        if state.get("_last_matches"):
            return "show", state, _facts(state, "search"), None, "👍"
        facts = _facts(state, "ask")
        state["asking"] = _missing(state) or "route"
        return "ask", state, facts, _fallback("ask", facts), "👍"

    if kind == "confirm":
        if state.get("asking") == "book" and state.get("_pending_book"):
            facts = _facts(state, "book_yes", allowed=True)
            facts["option"] = str((state.get("_pending_book") or {}).get("company") or "")
            return "book_yes", state, facts, None, ""
        if _ready(state):
            state["route_explicit"] = True
            state["asking"] = ""
            return "search", state, _facts(state, "search"), None, "👍"
        if state.get("_last_matches") and state.get("asking") not in {"confirm", "date", "route", "trip_type"}:
            return _arm_book(state, 0)
        facts = _facts(state, "ask")
        state["asking"] = _missing(state) or "route"
        return "ask", state, facts, _fallback("ask", facts), "👍"

    picked = _book_choice(text, updates)
    if picked is not None and not (updates.get("origin") and updates.get("destination")):
        return _arm_book(state, picked)

    place_action = state.pop("_place_action", None)
    if place_action == "swap":
        state["asking"] = "confirm"
        state["route_explicit"] = False
        return "confirm", state, _facts(state, "confirm"), None, ""
    if place_action == "echo" and _ready(state):
        state["asking"] = ""
        state["route_explicit"] = True
        return "search", state, _facts(state, "search"), None, ""

    if _ready(state) and _search_now(state, updates):
        state["asking"] = ""
        return "search", state, _facts(state, "search"), None, ""

    if state.get("origin") and state.get("destination") and state.get("depart") and not state.get("trip_type"):
        state["asking"] = "trip_type"
        facts = _facts(state, "ask")
        return "ask", state, facts, _fallback("ask", facts), "👍"

    if state.get("origin") and state.get("destination") and not state.get("route_explicit") and updates:
        state["asking"] = "confirm"
        facts = _facts(state, "confirm")
        return "confirm", state, facts, _fallback("confirm", facts), "👍"

    if not state.get("origin") or not state.get("destination") or not state.get("depart"):
        if updates or kind in {"search", "book"}:
            state["asking"] = _missing(state) or "route"
            facts = _facts(state, "ask")
            return "ask", state, facts, _fallback("ask", facts), "👍"

    data = llm.converse(_READ, _packet(state, prior, text))
    action, state, facts, spoken, reaction = _apply_model(data, state, text, prior)
    return action, state, facts, spoken, reaction


def _merge(state: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    data = dict(state)
    if updates.get("origin") and updates.get("destination"):
        data["origin"] = updates["origin"]
        data["destination"] = updates["destination"]
        data["route_explicit"] = True
    elif updates.get("place"):
        place = str(updates["place"])
        if data.get("asking") == "confirm" and _same_place(place, str(data.get("destination") or "")):
            data["route_explicit"] = True
            data["_place_action"] = "echo"
        elif not data.get("origin") and not data.get("destination") and updates.get("depart"):
            data["destination"] = place
            data["_place_action"] = "fill"
        elif not data.get("origin"):
            data["origin"] = place
            if data.get("destination"):
                data["route_explicit"] = True
            data["_place_action"] = "fill"
        elif not data.get("destination"):
            data["destination"] = place
            if data.get("origin"):
                data["route_explicit"] = True
            data["_place_action"] = "fill"
        else:
            data["destination"] = place
            data["route_explicit"] = False
            data["asking"] = "confirm"
            data["_place_action"] = "swap"
    if updates.get("depart"):
        data["depart"] = updates["depart"]
    if "round_trip" in updates:
        data["trip_type"] = "return" if updates["round_trip"] else "one_way"
    return data


def _search_now(state: dict[str, Any], updates: dict[str, Any]) -> bool:
    if not state.get("trip_type"):
        return False
    named_both = bool(updates.get("origin") and updates.get("destination"))
    if named_both and state.get("route_explicit"):
        return True
    if state.get("route_explicit") and (updates.get("depart") or "round_trip" in updates):
        return True
    return False


def _cancel(state: dict[str, Any]) -> tuple[str, dict[str, Any], dict[str, Any], None, str]:
    asking = state.get("asking")
    state.pop("_pending_book", None)
    if asking == "confirm":
        state["destination"] = ""
        state["route_explicit"] = False
        state["asking"] = "route"
    else:
        state["asking"] = ""
    return "cancel", state, _facts(state, "cancel"), None, ""


def _apply_model(
    data: dict[str, Any],
    state: dict[str, Any],
    text: str,
    prior: list[dict[str, Any]],
) -> tuple[str, dict[str, Any], dict[str, Any], str | None, str]:
    blob = " ".join(
        [text, *[str(m.get("body") or "") for m in prior], str(state.get("origin") or ""), str(state.get("destination") or "")]
    ).lower()
    for key in ("origin", "destination"):
        raw = str(data.get(key) or "").strip()
        if raw and raw.lower() in blob:
            state[key] = raw
    depart = str(data.get("depart") or "").strip()
    if re.fullmatch(r"20\d{2}-\d{2}-\d{2}", depart):
        state["depart"] = depart
    if data.get("round_trip") is True:
        state["trip_type"] = "return"
    elif data.get("round_trip") is False:
        state["trip_type"] = "one_way"

    action = str(data.get("action") or "chat")
    if action not in {"ask", "confirm", "search", "chat"}:
        action = "chat"
    named_in_text = bool(intent.parse_updates(text).get("origin") and intent.parse_updates(text).get("destination"))
    spoken = _clean_reply(str(data.get("reply") or ""), allow_yes=False)
    if action == "search" and not (_ready(state) and (named_in_text or state.get("route_explicit"))):
        action = "confirm" if state.get("origin") and state.get("destination") else "ask"
        spoken = ""
    if action == "search" and not state.get("trip_type"):
        action = "ask"
        spoken = ""
    if action == "confirm":
        state["asking"] = "confirm"
        if not named_in_text:
            state["route_explicit"] = False
    elif action == "ask":
        state["asking"] = _missing(state) or "route"
    elif action == "search":
        state["route_explicit"] = True
        state["asking"] = ""

    reaction = _one_emoji(str(data.get("reaction") or ""), _default_reaction(action))
    if not spoken:
        return action, state, _facts(state, action), None, reaction
    return action, state, _facts(state, action), spoken, reaction


def _search(phone: str, state: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    flights = jobs.search_and_cache(
        str(state.get("origin") or ""),
        str(state.get("destination") or ""),
        str(state.get("depart") or ""),
        round_trip=state.get("trip_type") == "return",
    )
    state = dict(state)
    state["_last_matches"] = [flight["id"] for flight in flights]
    state["asking"] = ""
    _save(phone, state)
    return flights, state


def _cached(state: dict[str, Any]) -> list[dict[str, Any]]:
    flights = []
    for flight_id in state.get("_last_matches") or []:
        job = db.get_job(str(flight_id))
        if job:
            flights.append(job)
    return flights


def _ready(state: dict[str, Any]) -> bool:
    return bool(state.get("origin") and state.get("destination") and state.get("depart") and state.get("trip_type"))


def _missing(state: dict[str, Any]) -> str:
    if not state.get("origin") or not state.get("destination"):
        return "route"
    if not state.get("depart"):
        return "date"
    if not state.get("trip_type"):
        return "trip_type"
    return ""


def _facts(state: dict[str, Any], action: str = "", *, allowed: bool = False) -> dict[str, Any]:
    return {
        "action": action,
        "origin": _label(str(state.get("origin") or "")),
        "destination": _label(str(state.get("destination") or "")),
        "when": _when(str(state.get("depart") or "")),
        "trip": {"one_way": "one way", "return": "return"}.get(str(state.get("trip_type") or ""), "not said"),
        "missing": _missing(state),
        "allowed_to_agree": allowed or action == "book_yes",
        "asking": state.get("asking") or "",
        "option": "",
        "bookings": "",
    }


def _arm_book(state: dict[str, Any], picked: int) -> tuple[str, dict[str, Any], dict[str, Any], None, str]:
    flights = _cached(state)
    if not flights:
        state["asking"] = _missing(state) or "route"
        return "ask", state, _facts(state, "ask"), None, ""
    if picked < 0 or picked >= len(flights):
        picked = 0
    chosen = flights[picked]
    state["_pending_book"] = {
        "flight_id": chosen["id"],
        "title": chosen.get("title"),
        "company": chosen.get("company"),
        "url": chosen.get("apply_url"),
        "index": picked,
    }
    state["asking"] = "book"
    facts = _facts(state, "book_ask")
    words = ["first", "second", "third"]
    which = words[picked] if picked < len(words) else str(picked + 1)
    facts["option"] = f"the {which} one ({chosen.get('company')})"
    return "book_ask", state, facts, None, ""


def _speak(action: str, facts: dict[str, Any], prior: list[dict[str, Any]], text: str) -> tuple[str, str]:
    payload = {
        "action": action,
        "trip": facts,
        "their_message": text,
        "recent_chat": [_line(m) for m in prior[-6:]],
    }
    if action == "status":
        payload["trip"]["bookings"] = facts.get("bookings") or "none yet"
    data = llm.converse(_VOICE, json.dumps(payload, ensure_ascii=False))
    reply = _clean_reply(str(data.get("reply") or ""), allow_yes=action == "book_yes")
    reaction = _one_emoji(str(data.get("reaction") or ""), _default_reaction(action))
    if action == "book_yes" and reply and not re.match(r"(?i)^yes\b", reply):
        reply = "Yes. " + reply
    if not reply:
        reply = _fallback(action, facts)
        reaction = reaction or _default_reaction(action)
    return reply, reaction


def _wants_results(text: str) -> bool:
    raw = (text or "").strip()
    if re.fullmatch(r"\?+|how long\??", raw, re.I):
        return True
    return bool(re.search(r"\b(screenshots?|show me|the results|the flights|the options)\b", raw, re.I))


def _results_line(state: dict[str, Any]) -> str:
    facts = _facts(state, "search")
    trip = facts["trip"] if facts["trip"] != "not said" else "one way"
    return (
        f"{facts['origin']} to {facts['destination']}, {facts['when']}, {trip}. "
        "Live options in the screenshot. I have not bought a ticket."
    )


def _fallback(action: str, facts: dict[str, Any]) -> str:
    origin = facts.get("origin") or ""
    dest = facts.get("destination") or ""
    when = facts.get("when") or ""
    trip = facts.get("trip") or ""
    route = f"{origin} to {dest}".strip() if origin and dest else ""
    if action == "greet":
        return "Hey. Where are you flying, and when?"
    if action == "cancel":
        return "All good, I won't book that."
    if action == "book_yes":
        return f"Yes. Opening {facts.get('option') or route or 'that fare'}. I stop before payment."
    if action == "book_ask":
        return f"Want me to book {facts.get('option') or 'that one'}? I won't confirm the ticket until you say so."
    if action == "confirm":
        bit = " ".join(p for p in (when, trip if trip != "not said" else "") if p)
        head = f"Got it, {bit}. " if bit else ""
        return f"{head}{route}, right? Just confirm and I search."
    if action == "search":
        kind = f", {trip}" if trip and trip != "not said" else ""
        return (
            f"Searching {route} for {when}{kind}. I'll bring back options before booking anything.\n\n"
            f"{route} for {when} — live options below. I have not bought a ticket."
        )
    if action == "status":
        return "No ticket bought yet. Where do you want to fly?"
    if action == "ask":
        if facts.get("missing") == "trip_type" and route and when:
            return f"{route} on {when}. One-way or return?"
        if facts.get("missing") in {"date", "trip_type"}:
            return f"{route}. Which date, and one-way or return?" if route else "Which date, and one-way or return?"
        if origin and not dest:
            if when:
                return f"From {origin} on {when}. Where to, and one-way or return?"
            return f"From {origin}. Where to?"
        if dest and not origin:
            if when:
                return f"{dest} on {when}. Flying from where, and one-way or return?"
            return f"To {dest}. Flying from where?"
        return "Where from and where to?"
    return "Tell me the cities and a date and I'll pull the flights."


def _clean_reply(text: str, *, allow_yes: bool) -> str:
    body = re.sub(r"\s+", " ", (text or "").replace("\n", " ")).strip().strip('"')
    if not allow_yes:
        body = re.sub(r"(?i)^yes\b[.!,]?\s*", "", body).strip()
    return body[:500]


def _default_reaction(action: str) -> str:
    return {
        "greet": "👋",
        "ask": "🤔",
        "confirm": "👍",
        "search": "✈️",
        "book_ask": "👍",
        "book_yes": "✅",
        "cancel": "👌",
        "status": "📋",
        "chat": "👍",
    }.get(action, "👍")


def _one_emoji(value: str, default: str) -> str:
    value = (value or "").strip()
    if value and len(value) <= 3 and not any(ch.isalnum() for ch in value):
        return value
    return default


def _packet(state: dict[str, Any], prior: list[dict[str, Any]], text: str) -> str:
    return json.dumps(
        {
            "state": {
                "origin": state.get("origin") or "",
                "destination": state.get("destination") or "",
                "depart": state.get("depart") or "",
                "trip_type": state.get("trip_type") or "",
                "route_explicit": bool(state.get("route_explicit")),
                "asking": state.get("asking") or "",
            },
            "recent_chat": [_line(m) for m in prior[-6:]],
            "message": text,
        },
        ensure_ascii=False,
    )


def _line(message: dict[str, Any]) -> str:
    role = "them" if message.get("role") == "user" else "me"
    return f"{role}: {str(message.get('body') or '')[:240]}"


def _label(name: str) -> str:
    if not name:
        return ""
    hit = resolve_place(name)
    return hit[1] if hit else name.strip().title()


def _when(iso: str) -> str:
    if not iso:
        return ""
    try:
        found = date.fromisoformat(iso[:10])
    except ValueError:
        return iso
    return f"{found.day} {found.strftime('%b')}"


def _same_place(left: str, right: str) -> bool:
    a = resolve_place(left)
    b = resolve_place(right)
    if a and b:
        return a[0] == b[0]
    return left.strip().lower() == right.strip().lower()


def _book_choice(text: str, updates: dict[str, Any]) -> int | None:
    if updates.get("origin") or updates.get("destination"):
        return None
    hit = intent.classify_heuristic(text) or {}
    if hit.get("intent") == "book" and hit.get("index") is not None and not hit.get("origin"):
        return int(hit["index"])
    return None


def _google_link(flights: list[dict[str, Any]]) -> str:
    for flight in flights:
        if "google" in str(flight.get("company") or "").lower():
            return str(flight.get("apply_url") or "")
    return str(flights[0].get("apply_url") or "") if flights else ""


def _shots(flights: list[dict[str, Any]]) -> list[str]:
    prefer = ("google", "kayak", "makemytrip")
    ordered = sorted(
        flights,
        key=lambda flight: next(
            (i for i, name in enumerate(prefer) if name in str(flight.get("company") or "").lower()),
            99,
        ),
    )
    urls = []
    for flight in ordered[:2]:
        page = str(flight.get("apply_url") or "")
        if page.startswith("http"):
            urls.append(screenshots.mshots_url(page))
    return urls


def _save_profile(phone: str, state: dict[str, Any], raw: str) -> str:
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) < 2:
        return "Send it like: SAVE PROFILE Full Name | you@email.com | +phone | DOB | Gender"
    name, email = parts[0], parts[1]
    phone_n = parts[2] if len(parts) > 2 else ""
    dob = parts[3] if len(parts) > 3 else ""
    gender = parts[4] if len(parts) > 4 else ""
    data = dict(state)
    data["name"] = name
    data["email"] = email
    if phone_n:
        data["phone"] = phone_n
    if dob:
        data["dob"] = dob
    if gender:
        data["gender"] = gender
    db.update_user(phone, interests=data, display_name=name)
    return f"Saved, {name}. I'll use this on booking forms and still stop before payment."


def _status_text(phone: str) -> str:
    apps = db.list_applications(phone)
    if not apps:
        return "none yet"
    return "; ".join(
        f"{a.get('title') or a.get('job_id')} ({a.get('status')})"
        for a in apps[:5]
    )
