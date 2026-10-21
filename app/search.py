from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlencode

# name or IATA -> (IATA, label, domestic India)
_PLACES: dict[str, tuple[str, str, bool]] = {
    "jorhat": ("JRH", "Jorhat", True),
    "jrh": ("JRH", "Jorhat", True),
    "rowriah": ("JRH", "Jorhat", True),
    "bengaluru": ("BLR", "Bengaluru", True),
    "bangalore": ("BLR", "Bengaluru", True),
    "begaluru": ("BLR", "Bengaluru", True),
    "bangaluru": ("BLR", "Bengaluru", True),
    "banglore": ("BLR", "Bengaluru", True),
    "bangalor": ("BLR", "Bengaluru", True),
    "bengaluru airport": ("BLR", "Bengaluru", True),
    "blr": ("BLR", "Bengaluru", True),
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

    flights: list[dict[str, Any]] = []
    if origin_code and dest_code:
        flights.append(
            _row("MakeMyTrip", route, dest_label, origin_label, _mmt_url(origin_code, dest_code, when, round_trip, domestic), blurb, when)
        )
    flights.append(_row("Google Flights", route, dest_label, origin_label, google, blurb, when))
    if origin_code and dest_code:
        flights.append(
            _row(
                "Skyscanner",
                route,
                dest_label,
                origin_label,
                _sky_url(origin_code, dest_code, when, round_trip),
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


def _google_url(
    origin: str,
    origin_code: str,
    dest: str,
    dest_code: str,
    when: date,
    round_trip: bool,
) -> str:
    left = f"{origin} {origin_code}".strip()
    right = f"{dest} {dest_code}".strip()
    if round_trip:
        back = when + timedelta(days=7)
        q = f"round trip flights from {left} to {right} on {when.isoformat()} returning {back.isoformat()}"
    else:
        q = f"one way flights from {left} to {right} on {when.isoformat()}"
    return "https://www.google.com/travel/flights?" + urlencode({"hl": "en", "curr": "INR", "q": q})


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


def _sky_url(origin: str, dest: str, when: date, round_trip: bool) -> str:
    o = origin.lower()
    d = dest.lower()
    start = when.strftime("%y%m%d")
    if round_trip:
        back = (when + timedelta(days=7)).strftime("%y%m%d")
        path = f"{o}/{d}/{start}/{back}"
        rtn = "1"
    else:
        path = f"{o}/{d}/{start}"
        rtn = "0"
    return (
        f"https://www.skyscanner.co.in/transport/flights/{path}/"
        f"?adultsv2=1&cabinclass=economy&rtn={rtn}"
    )
