from __future__ import annotations

import base64
import re
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlencode

# name or IATA -> (IATA, label, domestic India)
_PLACES: dict[str, tuple[str, str, bool]] = {
    "delhi": ("DEL", "Delhi", True),
    "new delhi": ("DEL", "Delhi", True),
    "del": ("DEL", "Delhi", True),
    "mumbai": ("BOM", "Mumbai", True),
    "bombay": ("BOM", "Mumbai", True),
    "bom": ("BOM", "Mumbai", True),
    "kolkata": ("CCU", "Kolkata", True),
    "calcutta": ("CCU", "Kolkata", True),
    "ccu": ("CCU", "Kolkata", True),
    "chennai": ("MAA", "Chennai", True),
    "madras": ("MAA", "Chennai", True),
    "maa": ("MAA", "Chennai", True),
    "hyderabad": ("HYD", "Hyderabad", True),
    "hyd": ("HYD", "Hyderabad", True),
    "goa": ("GOI", "Goa", True),
    "dabolim": ("GOI", "Goa", True),
    "goi": ("GOI", "Goa", True),
    "pune": ("PNQ", "Pune", True),
    "pnq": ("PNQ", "Pune", True),
    "ahmedabad": ("AMD", "Ahmedabad", True),
    "amd": ("AMD", "Ahmedabad", True),
    "jaipur": ("JAI", "Jaipur", True),
    "jai": ("JAI", "Jaipur", True),
    "lucknow": ("LKO", "Lucknow", True),
    "lko": ("LKO", "Lucknow", True),
    "guwahati": ("GAU", "Guwahati", True),
    "gauhati": ("GAU", "Guwahati", True),
    "ghy": ("GAU", "Guwahati", True),
    "gau": ("GAU", "Guwahati", True),
    "dibrugarh": ("DIB", "Dibrugarh", True),
    "dib": ("DIB", "Dibrugarh", True),
    "silchar": ("IXS", "Silchar", True),
    "ixs": ("IXS", "Silchar", True),
    "imphal": ("IMF", "Imphal", True),
    "imf": ("IMF", "Imphal", True),
    "agartala": ("IXA", "Agartala", True),
    "ixa": ("IXA", "Agartala", True),
    "kochi": ("COK", "Kochi", True),
    "cochin": ("COK", "Kochi", True),
    "cok": ("COK", "Kochi", True),
    "thiruvananthapuram": ("TRV", "Thiruvananthapuram", True),
    "trivandrum": ("TRV", "Thiruvananthapuram", True),
    "trv": ("TRV", "Thiruvananthapuram", True),
    "varanasi": ("VNS", "Varanasi", True),
    "vns": ("VNS", "Varanasi", True),
    "patna": ("PAT", "Patna", True),
    "pat": ("PAT", "Patna", True),
    "bhubaneswar": ("BBI", "Bhubaneswar", True),
    "bbi": ("BBI", "Bhubaneswar", True),
    "bagdogra": ("IXB", "Bagdogra", True),
    "ixb": ("IXB", "Bagdogra", True),
    "srinagar": ("SXR", "Srinagar", True),
    "sxr": ("SXR", "Srinagar", True),
    "chandigarh": ("IXC", "Chandigarh", True),
    "ixc": ("IXC", "Chandigarh", True),
    "indore": ("IDR", "Indore", True),
    "idr": ("IDR", "Indore", True),
    "nagpur": ("NAG", "Nagpur", True),
    "nag": ("NAG", "Nagpur", True),
    "coimbatore": ("CJB", "Coimbatore", True),
    "cjb": ("CJB", "Coimbatore", True),
    "visakhapatnam": ("VTZ", "Visakhapatnam", True),
    "vizag": ("VTZ", "Visakhapatnam", True),
    "vtz": ("VTZ", "Visakhapatnam", True),
    "dubai": ("DXB", "Dubai", False),
    "dxb": ("DXB", "Dubai", False),
    "london": ("LHR", "London", False),
    "lhr": ("LHR", "London", False),
    "singapore": ("SIN", "Singapore", False),
    "sin": ("SIN", "Singapore", False),
    "bangkok": ("BKK", "Bangkok", False),
    "bkk": ("BKK", "Bangkok", False),
    "kathmandu": ("KTM", "Kathmandu", False),
    "ktm": ("KTM", "Kathmandu", False),
    "doha": ("DOH", "Doha", False),
    "doh": ("DOH", "Doha", False),
    "abu dhabi": ("AUH", "Abu Dhabi", False),
    "auh": ("AUH", "Abu Dhabi", False),
    "new york": ("JFK", "New York", False),
    "jfk": ("JFK", "New York", False),
    "paris": ("CDG", "Paris", False),
    "cdg": ("CDG", "Paris", False),
}


def resolve_place(name: str) -> tuple[str, str, bool] | None:
    key = re.sub(r"\s+", " ", (name or "").strip().lower())
    if key in _PLACES:
        return _PLACES[key]
    if len(key) < 5 or not key.replace(" ", "").isalpha():
        return None
    import difflib

    match = difflib.get_close_matches(key, [item for item in _PLACES if len(item) > 4], n=1, cutoff=0.82)
    return _PLACES[match[0]] if match else None


def mentioned_places(text: str) -> list[str]:
    """City or airport names in left-to-right order, one hit per airport."""
    raw = text or ""
    occupied: list[tuple[int, int]] = []
    found: list[tuple[int, str]] = []
    for name in sorted(_PLACES, key=len, reverse=True):
        for match in re.finditer(rf"\b{re.escape(name)}\b", raw, flags=re.I):
            span = match.span()
            if any(span[0] < end and span[1] > start for start, end in occupied):
                continue
            occupied.append(span)
            found.append((span[0], name))
    for token in re.finditer(r"[A-Za-z]{5,}", raw):
        span = token.span()
        if any(span[0] < end and span[1] > start for start, end in occupied):
            continue
        hit = resolve_place(token.group(0))
        if not hit:
            continue
        key = hit[1].lower() if hit[1].lower() in _PLACES else ""
        if not key:
            continue
        occupied.append(span)
        found.append((span[0], key))
    ordered = [name for _, name in sorted(found)]
    out: list[str] = []
    seen: set[str] = set()
    for name in ordered:
        code = _PLACES[name][0]
        if code in seen:
            continue
        seen.add(code)
        out.append(name)
    return out


def find_flights(
    origin: str,
    destination: str,
    depart: date | str,
    *,
    round_trip: bool = False,
) -> list[dict[str, Any]]:
    """Build live booking links instantly. No network and no LLM."""
    when = depart if isinstance(depart, date) else date.fromisoformat(str(depart)[:10])
    origin_hit = resolve_place(origin)
    dest_hit = resolve_place(destination)
    origin_label = origin_hit[1] if origin_hit else _title_place(origin)
    dest_label = dest_hit[1] if dest_hit else _title_place(destination)
    origin_code = origin_hit[0] if origin_hit else ""
    dest_code = dest_hit[0] if dest_hit else ""
    domestic = bool(origin_hit and dest_hit and origin_hit[2] and dest_hit[2])
    route = _route_label(origin_label, origin_code, dest_label, dest_code)
    blurb = _when_blurb(when, round_trip)
    google = _google_url(origin_label, origin_code, dest_label, dest_code, when, round_trip)

    flights: list[dict[str, Any]] = [
        _row("Google Flights", route, dest_label, origin_label, google, blurb, when)
    ]
    if origin_code and dest_code:
        flights.append(
            _row(
                "Kayak",
                route,
                dest_label,
                origin_label,
                _kayak_url(origin_code, dest_code, when, round_trip),
                blurb,
                when,
            )
        )
        flights.append(
            _row(
                "MakeMyTrip",
                route,
                dest_label,
                origin_label,
                _mmt_url(origin_code, dest_code, when, round_trip, domestic),
                blurb,
                when,
            )
        )
    return flights


def _row(
    company: str,
    route: str,
    destination: str,
    origin: str,
    url: str,
    blurb: str,
    when: date,
) -> dict[str, Any]:
    return {
        "id": _flight_id(company, route, when),
        "title": route,
        "company": company,
        "country": destination.lower(),
        "city": origin,
        "apply_url": url,
        "sponsor_status": "flight",
        "tags": [],
        "description": blurb,
        "source": "links",
    }


def _flight_id(company: str, route: str, when: date) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", f"{company}-{route}-{when.isoformat()}").strip("-").lower()
    return slug[:80]


def _route_label(origin: str, origin_code: str, dest: str, dest_code: str) -> str:
    left = f"{origin} ({origin_code})" if origin_code else origin
    right = f"{dest} ({dest_code})" if dest_code else dest
    return f"{left} → {right}"


def _title_place(name: str) -> str:
    return " ".join(part.capitalize() for part in (name or "").split()) or "City"


def _when_blurb(when: date, round_trip: bool) -> str:
    start = when.strftime("%a %d %b %Y")
    if not round_trip:
        return f"{start} · 1 adult · one way"
    back = (when + timedelta(days=7)).strftime("%a %d %b %Y")
    return f"{start} → {back} · 1 adult · round trip"


def flight_results_url(origin: str, destination: str, depart: str, depart_until: str = "") -> str:
    """A Google Flights results page for a known trip. No model call required."""
    when = date.fromisoformat(str(depart)[:10])
    origin_hit = resolve_place(origin)
    dest_hit = resolve_place(destination)
    origin_label = origin_hit[1] if origin_hit else origin
    dest_label = dest_hit[1] if dest_hit else destination
    origin_code = origin_hit[0] if origin_hit else ""
    dest_code = dest_hit[0] if dest_hit else ""
    until = str(depart_until or "")[:10]
    if until and until != str(depart)[:10]:
        query = f"Flights from {origin_label} to {dest_label} from {str(depart)[:10]} to {until}"
        return "https://www.google.com/travel/flights?" + urlencode({"hl": "en", "curr": "INR", "q": query})
    return _google_url(origin_label, origin_code, dest_label, dest_code, when, False)


def cleartrip_results_url(origin: str, destination: str, depart: str) -> str:
    origin_hit = resolve_place(origin)
    dest_hit = resolve_place(destination)
    if not origin_hit or not dest_hit:
        return flight_results_url(origin, destination, depart)
    day, month, year = str(depart)[8:10], str(depart)[5:7], str(depart)[:4]
    domestic = "n" if origin_hit[2] and dest_hit[2] else "y"
    return "https://www.cleartrip.com/flights/results?" + urlencode(
        {
            "from": origin_hit[0],
            "to": dest_hit[0],
            "depart_date": f"{day}/{month}/{year}",
            "adults": "1",
            "childs": "0",
            "infants": "0",
            "class": "Economy",
            "intl": domestic,
        }
    )


def easemytrip_results_url(origin: str, destination: str, depart: str) -> str:
    origin_hit = resolve_place(origin)
    dest_hit = resolve_place(destination)
    if not origin_hit or not dest_hit:
        return flight_results_url(origin, destination, depart)
    when = date.fromisoformat(str(depart)[:10])
    day = when.strftime("%d%b%Y")  # 01Dec2026
    return "https://flight.easemytrip.com/FlightList/Index?" + urlencode(
        {
            "srch": f"{origin_hit[0]}-{dest_hit[0]}-{day}--0-0-0-E-O-1-0",
            "IsN": "N" if origin_hit[2] and dest_hit[2] else "Y",
        }
    )


def ixigo_results_url(origin: str, destination: str, depart: str) -> str:
    origin_hit = resolve_place(origin)
    dest_hit = resolve_place(destination)
    if not origin_hit or not dest_hit:
        return flight_results_url(origin, destination, depart)
    when = date.fromisoformat(str(depart)[:10])
    return "https://www.ixigo.com/search/result/flight?" + urlencode(
        {
            "from": origin_hit[0],
            "to": dest_hit[0],
            "date": when.strftime("%d%m%Y"),
            "adults": "1",
            "children": "0",
            "infants": "0",
            "class": "e",
            "source": "Search Form",
        }
    )


def kayak_results_url(origin: str, destination: str, depart: str) -> str:
    origin_hit = resolve_place(origin)
    dest_hit = resolve_place(destination)
    if not origin_hit or not dest_hit:
        return flight_results_url(origin, destination, depart)
    when = date.fromisoformat(str(depart)[:10])
    return _kayak_url(origin_hit[0], dest_hit[0], when, False)


def makemytrip_results_url(origin: str, destination: str, depart: str) -> str:
    origin_hit = resolve_place(origin)
    dest_hit = resolve_place(destination)
    if not origin_hit or not dest_hit:
        return flight_results_url(origin, destination, depart)
    when = date.fromisoformat(str(depart)[:10])
    domestic = bool(origin_hit[2] and dest_hit[2])
    return _mmt_url(origin_hit[0], dest_hit[0], when, False, domestic)


# Sellers the agent can open. Order is the default try order (feasible first).
SELLERS: list[dict[str, str]] = [
    {"id": "cleartrip", "label": "Cleartrip", "host": "cleartrip.com"},
    {"id": "easemytrip", "label": "EaseMyTrip", "host": "easemytrip.com"},
    {"id": "ixigo", "label": "Ixigo", "host": "ixigo.com"},
    {"id": "makemytrip", "label": "MakeMyTrip", "host": "makemytrip.com"},
]


def seller_results_url(seller_id: str, origin: str, destination: str, depart: str) -> str:
    builders = {
        "cleartrip": cleartrip_results_url,
        "easemytrip": easemytrip_results_url,
        "ixigo": ixigo_results_url,
        "makemytrip": makemytrip_results_url,
        "google": flight_results_url,
        "kayak": kayak_results_url,
    }
    build = builders.get((seller_id or "").lower())
    if not build:
        return cleartrip_results_url(origin, destination, depart)
    return build(origin, destination, depart)


def named_seller(text: str) -> str:
    """Seller id if the traveller named one, else empty."""
    raw = (text or "").lower()
    if re.search(r"\b(google flights|google)\b", raw):
        return "google"
    if re.search(r"\b(cleartrip|ct)\b", raw):
        return "cleartrip"
    if re.search(r"\b(easemytrip|emt)\b", raw):
        return "easemytrip"
    if re.search(r"\bixigo\b", raw):
        return "ixigo"
    if re.search(r"\b(makemytrip|mmt|goibibo)\b", raw):
        return "makemytrip"
    if re.search(r"\bkayak\b", raw):
        return "kayak"
    return ""


# Pages we can search but cannot ticket.
NOT_BOOKABLE = {"google", "kayak"}


def search_seller(preferred: str = "") -> dict[str, str]:
    """One page for fares. Google Flights unless the traveller named a bookable site."""
    preferred = (preferred or "").lower()
    if preferred and preferred not in NOT_BOOKABLE:
        match = [item for item in SELLERS if item["id"] == preferred]
        if match:
            return match[0]
    if preferred == "kayak":
        return {"id": "kayak", "label": "Kayak", "host": "kayak.co.in"}
    return {"id": "google", "label": "Google Flights", "host": "google.com"}


def seller_order(preferred: str = "") -> list[dict[str, str]]:
    """Preferred seller first, then the rest. MakeMyTrip stays last (often blocked)."""
    preferred = (preferred or "").lower()
    primary = [item for item in SELLERS if item["id"] == preferred]
    rest = [item for item in SELLERS if item["id"] != preferred]
    if preferred and preferred != "makemytrip":
        rest = [item for item in rest if item["id"] != "makemytrip"] + [
            item for item in rest if item["id"] == "makemytrip"
        ]
    return primary + rest


def _google_url(
    origin: str,
    origin_code: str,
    dest: str,
    dest_code: str,
    when: date,
    round_trip: bool,
) -> str:
    if origin_code and dest_code:
        tfs = _google_tfs(origin_code, dest_code, when, round_trip)
        return "https://www.google.com/travel/flights/search?" + urlencode(
            {"tfs": tfs, "hl": "en", "curr": "INR"}
        )
    left = f"{origin} {origin_code}".strip()
    right = f"{dest} {dest_code}".strip()
    q = f"Flights to {right} from {left} on {when.isoformat()}"
    return "https://www.google.com/travel/flights?" + urlencode({"hl": "en", "curr": "INR", "q": q})


def _google_tfs(origin: str, dest: str, when: date, round_trip: bool) -> str:
    """Minimal Google Flights protobuf (airport codes + date)."""
    legs = _pb_leg(when, origin.upper(), dest.upper())
    if round_trip:
        legs += _pb_leg(when + timedelta(days=7), dest.upper(), origin.upper())
    body = (
        legs
        + _pb_varint_field(8, 1)  # 1 adult
        + _pb_varint_field(9, 1)  # economy
        + _pb_varint_field(19, 1 if round_trip else 2)  # 1=round trip, 2=one way
    )
    return base64.urlsafe_b64encode(body).decode("ascii").rstrip("=")


def _pb_leg(when: date, origin: str, dest: str) -> bytes:
    date_s = when.isoformat().encode("ascii")
    airport = lambda code: _pb_bytes_field(2, code.encode("ascii"))
    leg = (
        _pb_bytes_field(2, date_s)
        + _pb_len_field(13, airport(origin))
        + _pb_len_field(14, airport(dest))
    )
    return _pb_len_field(3, leg)


def _pb_varint(value: int) -> bytes:
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value & 0x7F)
    return bytes(out)


def _pb_varint_field(field: int, value: int) -> bytes:
    return bytes([(field << 3) | 0]) + _pb_varint(value)


def _pb_bytes_field(field: int, value: bytes) -> bytes:
    return bytes([(field << 3) | 2]) + _pb_varint(len(value)) + value


def _pb_len_field(field: int, value: bytes) -> bytes:
    return _pb_bytes_field(field, value)


def _kayak_url(origin: str, dest: str, when: date, round_trip: bool) -> str:
    path = f"{origin.upper()}-{dest.upper()}/{when.isoformat()}"
    if round_trip:
        path += f"/{(when + timedelta(days=7)).isoformat()}"
    return f"https://www.kayak.co.in/flights/{path}?sort=bestflight_a&fs=cfc=0"


def _mmt_url(origin: str, dest: str, when: date, round_trip: bool, domestic: bool) -> str:
    depart = when.strftime("%d/%m/%Y")
    if round_trip:
        back = (when + timedelta(days=7)).strftime("%d/%m/%Y")
        itinerary = f"{origin}-{dest}-{depart}_{dest}-{origin}-{back}"
        trip = "R"
    else:
        itinerary = f"{origin}-{dest}-{depart}"
        trip = "O"
    return "https://www.makemytrip.com/flight/search?" + urlencode(
        {
            "itinerary": itinerary,
            "tripType": trip,
            "paxType": "A-1_C-0_I-0",
            "intl": "false" if domestic else "true",
            "cabinClass": "E",
            "ccde": "IN",
            "lang": "eng",
        }
    )


