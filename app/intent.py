from __future__ import annotations

import calendar
import re
from datetime import date, timedelta
from typing import Any

from app.search import mentioned_places, resolve_place


_GREET = re.compile(r"^(hi+|hello|hey+|yo|hola|start|help)\b", re.I)
_STATUS = re.compile(r"\b(status|my (bookings?|trips?|flights?)|what did i book)\b", re.I)
_VAULT = re.compile(r"\b(vault|save (my )?login|password|credentials)\b", re.I)
_CANCEL = re.compile(
    r"^(no|nope|cancel|stop|never ?mind|don't|do not)(\s+please)?[\s!.]*$",
    re.I,
)
_CONFIRM = re.compile(
    r"^(yes|yep|yeah|y|ok|okay|sure|allow|confirm|confirmed|do it|go ahead|book it|book this|book that|book now)"
    r"(\s+(please|thanks|now|book it))?[\s!.]*$",
    re.I,
)
_PLACE = r"[A-Za-z][A-Za-z.'-]{1,20}(?:\s+[A-Za-z][A-Za-z.'-]{1,20}){0,2}"
_ROUTE = re.compile(rf"(?:from\s+)?({_PLACE})\s+(?:to|->|→)\s+({_PLACE})", re.I)
_STOP = {
    "i", "me", "my", "you", "want", "need", "go", "fly", "flying", "book", "get",
    "the", "a", "an", "to", "from", "please", "flight", "flights", "ticket", "tickets",
    "show", "find", "search", "for", "and", "with", "that", "this", "one", "way",
}
_BOOK_NUM = re.compile(
    r"(?:book|apply|take|choose)\s*(?:option\s*|flight\s*)?(?:#|number\s*)?(\d+)"
    r"|(?:book|apply|take|choose)\s+(?:the\s+)?(first|second|third|1st|2nd|3rd)"
    r"|^(?:option\s*)?#?(\d+)$",
    re.I,
)
_LEAD = re.compile(
    r"^(?:please|kindly|book|find|search|show|get|need|want|looking|a|an|the|me|for|flight|flights|ticket|tickets|from|one|way|ow)\s+",
    re.I,
)
_TAIL = re.compile(
    r"\b(?:today|tomorrow|tonight|next week|one way|round trip|round-trip|return|two way|please|flights?|tickets?|thanks)\b.*$",
    re.I,
)
_WEEKDAY = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


def _ordinal_to_idx(token: str) -> int | None:
    return {
        "1": 0,
        "1st": 0,
        "first": 0,
        "2": 1,
        "2nd": 1,
        "second": 1,
        "3": 2,
        "3rd": 2,
        "third": 2,
    }.get(token.lower())


def classify_heuristic(text: str) -> dict[str, Any] | None:
    raw = (text or "").strip()
    if not raw:
        return {"intent": "greet"}

    trip = extract_trip(raw)
    if trip.get("origin") and trip.get("destination"):
        intent = "book" if re.search(r"\b(book|apply)\b", raw, re.I) else "search"
        return {"intent": intent, "index": 0, **trip}

    picked = _book_index(raw)
    if picked is not None:
        return {"intent": "book", "index": picked}

    if _CONFIRM.match(raw):
        return {"intent": "confirm"}
    if _CANCEL.match(raw):
        return {"intent": "cancel"}
    if _GREET.match(raw) and len(raw) < 40:
        return {"intent": "greet"}
    if _STATUS.search(raw):
        return {"intent": "status"}
    if raw.upper() == "VAULT" or _VAULT.search(raw):
        return {"intent": "vault"}
    if re.search(r"\b(flight|flights|fly|ticket|tickets)\b", raw, re.I):
        return {"intent": "search"}
    if trip.get("origin") or trip.get("destination"):
        kind = "book" if re.search(r"\b(book|apply)\b", raw, re.I) else "search"
        return {"intent": kind, "index": 0, **trip}
    if re.search(r"\b(book|apply)\b", raw, re.I):
        return {"intent": "book", "index": 0}
    return None


def classify(text: str, interests: dict[str, Any]) -> dict[str, Any]:
    """Fast, local intent. Never calls an LLM."""
    del interests  # context is applied by the agent; parsing stays instant
    return classify_heuristic(text) or {"intent": "chat"}


def extract_trip(text: str) -> dict[str, Any]:
    origin, destination = _route_pair(text)
    if not origin or not destination:
        names = mentioned_places(text)
        if len(names) >= 2:
            origin, destination = names[0], names[1]
        elif len(names) == 1:
            return {"destination": names[0]}
        else:
            return {}
    when, assumed = _extract_date(text)
    return {
        "origin": origin,
        "destination": destination,
        "depart": when.isoformat(),
        "date_assumed": assumed,
        "round_trip": bool(re.search(r"\b(return|round[\s-]?trip|two[\s-]?way)\b", text, re.I)),
    }


def _route_pair(text: str) -> tuple[str, str]:
    best: tuple[int, str, str] | None = None
    for match in _ROUTE.finditer(text or ""):
        origin = _clean_place(match.group(1))
        destination = _clean_place(match.group(2))
        if not _acceptable(origin) or not _acceptable(destination):
            continue
        score = int(bool(resolve_place(origin))) + int(bool(resolve_place(destination)))
        if best is None or score > best[0]:
            best = (score, origin, destination)
    if not best:
        return "", ""
    return best[1], best[2]


def _acceptable(name: str) -> bool:
    if resolve_place(name):
        return True
    parts = (name or "").lower().split()
    if not parts or len(parts) > 3:
        return False
    if any(part in _STOP or len(part) < 3 for part in parts):
        return False
    return all(re.fullmatch(r"[a-z.'-]+", part) for part in parts)


def _book_index(raw: str) -> int | None:
    match = _BOOK_NUM.search(raw)
    if not match:
        return None
    # "book jorhat ..." is a route, handled earlier. A digit/ordinal is required here.
    number = match.group(1) or match.group(3)
    ordinal = match.group(2)
    if number:
        return max(0, int(number) - 1)
    if ordinal:
        return _ordinal_to_idx(ordinal)
    return None


_DATE_BITS = re.compile(
    r"\b(?:on|departing|leaving|dated)\b"
    r"|\b\d{1,2}(?:st|nd|rd|th)?\s+[a-z]{3,9}(?:\s+20\d{2})?\b"
    r"|\b[a-z]{3,9}\s+\d{1,2}(?:st|nd|rd|th)?(?:\s+20\d{2})?\b"
    r"|\b\d{1,2}[/-]\d{1,2}(?:[/-]20\d{2})?\b"
    r"|\b20\d{2}-\d{2}-\d{2}\b"
    r"|\b(?:next\s+)?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    re.I,
)


def _clean_place(raw: str) -> str:
    text = (raw or "").strip(" .,!?:;")
    for _ in range(6):
        nxt = _LEAD.sub("", text).strip()
        if nxt == text:
            break
        text = nxt
    text = _TAIL.sub("", text)
    text = _DATE_BITS.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip(" .,!?:;-")
    return text


def week_window(text: str) -> tuple[str, str] | None:
    """First/second/third/fourth/last week of a named month, as ISO start and end."""
    lower = (text or "").lower()
    month_names = "|".join(sorted(_MONTHS, key=len, reverse=True))
    match = re.search(
        rf"\b(first|1st|second|2nd|third|3rd|fourth|4th|last)\s+week(?:\s+of)?(?:\s+({month_names}))?\b",
        lower,
    )
    if not match or not match.group(2):
        return None
    month = _MONTHS[match.group(2)]
    today = date.today()
    year = today.year
    start_day, end_day = _week_span(match.group(1), year, month)
    end = _safe_date(year, month, end_day, today)
    if end < today:
        year += 1
        start_day, end_day = _week_span(match.group(1), year, month)
    start = _safe_date(year, month, start_day, today)
    end = _safe_date(year, month, end_day, today)
    return start.isoformat(), end.isoformat()


def _week_span(which: str, year: int, month: int) -> tuple[int, int]:
    last = calendar.monthrange(year, month)[1]
    table = {
        "first": (1, 7),
        "1st": (1, 7),
        "second": (8, 14),
        "2nd": (8, 14),
        "third": (15, 21),
        "3rd": (15, 21),
        "fourth": (22, min(28, last)),
        "4th": (22, min(28, last)),
        "last": (max(1, last - 6), last),
    }
    return table[which]


def _extract_date(text: str) -> tuple[date, bool]:
    today = date.today()
    lower = text.lower()

    if re.search(r"\b(?:first|1st|second|2nd|third|3rd|fourth|4th|last)\s+week\b", lower):
        window = week_window(text)
        if window:
            year, month, day = (int(part) for part in window[0].split("-"))
            return date(year, month, day), False
        return today, True

    iso = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", lower)
    if iso:
        return _safe_date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)), today), False

    month_names = "|".join(sorted(_MONTHS, key=len, reverse=True))
    named_re = re.compile(
        rf"\b(\d{{1,2}})(?:st|nd|rd|th)?(?:\s+of)?\s+({month_names})(?:\s+(20\d{{2}}))?\b"
        rf"|\b({month_names})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:\s+(20\d{{2}}))?\b"
    )
    named = named_re.search(lower)
    if named:
        if named.group(1):
            day, month_name, year = int(named.group(1)), named.group(2), named.group(3)
        else:
            day, month_name, year = int(named.group(5)), named.group(4), named.group(6)
        month = _MONTHS[month_name]
        year_n = int(year) if year else today.year
        found = _safe_date(year_n, month, day, today)
        if not year and found < today:
            found = _safe_date(year_n + 1, month, day, today)
        return found, False

    slash = re.search(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](20\d{2}))?\b", lower)
    if slash:
        day, month = int(slash.group(1)), int(slash.group(2))
        year = int(slash.group(3)) if slash.group(3) else today.year
        found = _safe_date(year, month, day, today)
        if not slash.group(3) and found < today:
            found = _safe_date(year + 1, month, day, today)
        return found, False

    if re.search(r"\btoday|tonight\b", lower):
        return today, False
    if re.search(r"\btomorrow\b", lower):
        return today + timedelta(days=1), False
    if re.search(r"\bnext week\b", lower):
        return today + timedelta(days=7), False
    for name, idx in _WEEKDAY.items():
        if re.search(rf"\b{name}\b", lower):
            delta = (idx - today.weekday()) % 7
            if delta == 0 or re.search(rf"\bnext {name}\b", lower):
                delta = 7 if delta == 0 else delta
            return today + timedelta(days=delta), False
    bare = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)\b", lower)
    if bare:
        day = int(bare.group(1))
        year, month = today.year, today.month
        found = _safe_date(year, month, day, today)
        if found < today:
            month += 1
            if month == 13:
                month, year = 1, year + 1
            found = _safe_date(year, month, day, today)
        if found != today or day == today.day:
            return found, False
    return today + timedelta(days=1), True


def explicit_date(text: str) -> str | None:
    """ISO date only when the person actually named one."""
    when, assumed = _extract_date(text)
    if assumed:
        return None
    return when.isoformat()


def parse_updates(text: str) -> dict[str, Any]:
    """Slots this message actually contains. Missing fields are omitted."""
    out: dict[str, Any] = {}
    origin, destination = _route_pair(text)
    if origin and destination:
        out["origin"] = origin
        out["destination"] = destination
    else:
        names = mentioned_places(text)
        if len(names) >= 2:
            out["origin"], out["destination"] = names[0], names[1]
        elif len(names) == 1:
            out["place"] = names[0]
    window = week_window(text)
    if window:
        out["depart"], out["depart_until"] = window
    else:
        when = explicit_date(text)
        if when:
            out["depart"] = when
            out["depart_until"] = ""
    if re.search(r"\bone[\s-]?way\b|\boneway\b", text or "", re.I):
        out["round_trip"] = False
    elif re.search(r"\b(return|round[\s-]?trip|two[\s-]?way)\b", text or "", re.I):
        out["round_trip"] = True
    return out


def _safe_date(year: int, month: int, day: int, fallback: date) -> date:
    try:
        return date(year, month, day)
    except ValueError:
        return fallback
