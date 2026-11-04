"""Observe a booking page, act once, verify, recover.

This is the execution layer Guide was missing: one step at a time, then a check
that the page actually changed. DeepSeek is used only when the page is not an
obvious cookie, results, fare sheet, or error screen.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_CLICK_TIME = """(hint) => {
    const needles = [];
    const m = String(hint || '').match(/(\\d{1,2}):(\\d{2})/);
    if (!m) return '';
    let h = parseInt(m[1], 10);
    const mm = m[2];
    const push = (x) => { if (x && !needles.includes(x)) needles.push(x); };
    push(h + ':' + mm);
    push(String(h).padStart(2, '0') + ':' + mm);
    if (h < 12) push((h + 12) + ':' + mm);
    else if (h > 12) push((h - 12) + ':' + mm);

    const isBook = (b) => /^(book|book now|select|view fares)$/i.test((b.innerText || '').trim());
    const departOf = (card) => {
        const times = (card.match(/\\b\\d{1,2}:\\d{2}\\b/g) || []);
        return times[0] || '';
    };
    const departMatches = (card) => needles.some(n => departOf(card) === n);

    // Ixigo cards repeat "Book" inside the same card (Book + fare strip).
    // Stop on many Books/times (a list of flights), not on a second Book.
    const cardText = (btn) => {
        let el = btn, card = '';
        for (let i = 0; i < 12 && el; i++) {
            const t = (el.innerText || '').replace(/\\s+/g, ' ').trim();
            const books = (t.match(/\\b(Book|Select|View fares)\\b/gi) || []).length;
            const times = (t.match(/\\b\\d{1,2}:\\d{2}\\b/g) || []).length;
            if (books > 2 || times > 4) break;
            if (t) card = t;
            el = el.parentElement;
        }
        return card;
    };

    const tryClick = () => {
        const buttons = [...document.querySelectorAll('button,a,[role=button]')].filter(isBook);
        for (const btn of buttons) {
            const card = cardText(btn);
            if (!departMatches(card)) continue;
            btn.scrollIntoView({block: 'center'});
            btn.click();
            return card.slice(0, 180);
        }
        return '';
    };

    const scrollResults = () => {
        const books = [...document.querySelectorAll('button,a,[role=button]')].filter(isBook);
        let el = books[0] || document.body;
        for (let i = 0; i < 14 && el; i++) {
            const st = getComputedStyle(el);
            if ((el.scrollHeight - el.clientHeight) > 80 && /(auto|scroll)/.test(st.overflowY || '')) {
                el.scrollTop = Math.min(el.scrollTop + Math.max(500, el.clientHeight * 0.9), el.scrollHeight);
                if (books.length) books[books.length - 1].scrollIntoView({block: 'end'});
                return true;
            }
            el = el.parentElement;
        }
        window.scrollBy(0, Math.max(600, Math.floor(window.innerHeight * 0.85)));
        return false;
    };

    let hit = tryClick();
    if (hit) return hit;
    // Ixigo paints flights inside an overflow-y panel; window scroll does nothing.
    for (let s = 0; s < 14 && !hit; s++) {
        scrollResults();
        hit = tryClick();
    }
    return hit;
}"""


def _clock_needles(hint: str) -> list[str]:
    match = re.match(r"(\d{1,2}):(\d{2})", hint or "")
    if not match:
        return [hint] if hint else []
    hour, minute = int(match.group(1)), match.group(2)
    needles = {f"{hour}:{minute}", f"{hour:02d}:{minute}"}
    if hour < 12:
        needles.add(f"{hour + 12}:{minute}")
    elif hour > 12:
        needles.add(f"{hour - 12}:{minute}")
        needles.add(f"{hour - 12:02d}:{minute}")
    return sorted(needles)


def card_matches_depart_hint(card: str, hint: str) -> bool:
    """Match the flight's departure (first clock), not the arrival.

    Ixigo cards look like '12:30 GAU … 15:15 DEL'. Hint 1:15 must not pick
    that row via arrival 15:15, and must accept a card whose first time is 13:15.
    """
    times = re.findall(r"\b\d{1,2}:\d{2}\b", card or "")
    if not times:
        return False
    depart = times[0]
    return depart in set(_clock_needles(hint))


@dataclass
class CheckoutOutcome:
    stage: str
    text: str
    url: str
    log: list[str] = field(default_factory=list)
    facts: dict[str, str] = field(default_factory=dict)
    checks: list[str] = field(default_factory=list)
    handoff: str = ""


def _results_url(url: str) -> bool:
    return bool(re.search(r"flightlist|flight-select|search/result/flight|/flights/results", url or "", re.I))


def extract_trip_facts(text: str, url: str = "") -> dict[str, str]:
    """Pull flight number, clocks, route codes, and total from a booking page."""
    hay = f"{text or ''}\n{url or ''}"
    flight = ""
    m = re.search(r"\b((?:IX|AI|6E|SG|QP|UK|I5)[\s-]?[0-9]{2,4})\b", hay, re.I)
    if m:
        flight = re.sub(r"[\s-]+", "", m.group(1).upper())
    depart_time = ""
    arrive_time = ""
    dm = re.search(r"(\d{1,2}:\d{2})\s*(?:\n|\s)+(?:GAU\b|Guwahati)", text or "", re.I)
    if dm:
        depart_time = dm.group(1)
    am = re.search(r"(\d{1,2}:\d{2})\s*(?:\n|\s)+(?:DEL\b|New Delhi)", text or "", re.I)
    if am:
        arrive_time = am.group(1)
    if not depart_time:
        times = re.findall(r"\b\d{1,2}:\d{2}\b", text or "")
        if times:
            depart_time = times[0]
            if len(times) > 1:
                arrive_time = times[1]
    total = ""
    tm = re.search(r"Total Amount\s*₹\s*([0-9,]+)", text or "", re.I)
    if not tm:
        tm = re.search(r"₹\s*([0-9,]+)\s*(?:\n|).*?(?:Continue|Lock Price)", text or "", re.I)
    if tm:
        total = "₹" + tm.group(1).replace(",", "")
    origin = "GAU" if re.search(r"\bGAU\b", hay) else ""
    dest = "DEL" if re.search(r"\bDEL\b", hay) else ""
    fare = ""
    if re.search(r"\bVALUE\b", hay):
        fare = "VALUE"
    elif re.search(r"\bSAVER\b", hay):
        fare = "SAVER"
    return {
        "flight": flight,
        "depart_time": depart_time,
        "arrive_time": arrive_time,
        "origin": origin,
        "destination": dest,
        "total": total,
        "fare_type": fare,
    }


def verify_trip_facts(
    facts: dict[str, str],
    *,
    expect_hint: str = "",
    expect_origin: str = "",
    expect_dest: str = "",
    expect_flight: str = "",
) -> tuple[bool, list[str]]:
    """Return (ok, problems). Used to refuse continuing on the wrong itinerary."""
    problems: list[str] = []
    if expect_origin and facts.get("origin") and facts["origin"].upper() != expect_origin.upper():
        problems.append(f"origin={facts.get('origin')} want={expect_origin}")
    if expect_dest and facts.get("destination") and facts["destination"].upper() != expect_dest.upper():
        problems.append(f"dest={facts.get('destination')} want={expect_dest}")
    if expect_flight:
        got = re.sub(r"[\s-]+", "", (facts.get("flight") or "").upper())
        want = re.sub(r"[\s-]+", "", expect_flight.upper())
        if got and want and got != want:
            problems.append(f"flight={got} want={want}")
    if expect_hint and facts.get("depart_time"):
        if facts["depart_time"] not in set(_clock_needles(expect_hint)):
            problems.append(f"depart={facts.get('depart_time')} want~={expect_hint}")
    return (not problems, problems)


_SITE_ERROR = re.compile(
    r"acting up|system is acting|go to home|itinerary/failure|been disconnected|connection lost|server error|"
    r"servers are stumped|wrong url|check your url|please check the url",
    re.I,
)
# Real checkout pay sheet — not Cleartrip contact sidebar "Payment options" marketing.
_PAY_SHEET = re.compile(
    r"pay to complete your booking|name on card|enter card number|scan qr code|"
    r"pay now|upi id|net banking|card number",
    re.I,
)
_CONTACT_SHEET = re.compile(
    r"add contact details|please enter mobile number|please enter email",
    re.I,
)


def site_error_page(text: str, url: str = "") -> bool:
    """Cleartrip/Ixigo hard failure — not review, not payment."""
    hay = f"{text or ''}\n{url or ''}"
    return bool(_SITE_ERROR.search(hay))


def payment_sheet_open(hay: str, stage: str) -> bool:
    """Payment wording counts only on a pay sheet, never on a homepage or a site error."""
    if re.search(r"first time flying|popular destinations|acting up|go to home", hay or "", re.I):
        return False
    # Contact / traveller contact is never pay — sidebar "Payment options" used to false-trigger.
    if _CONTACT_SHEET.search(hay or "") or (
        re.search(r"add contact|email address", hay or "", re.I)
        and re.search(r"\+91|mobile number|please enter mobile", hay or "", re.I)
    ):
        return False
    if stage in {"error", "home", "cookie", "loading", "results", "review", "addons"}:
        return False
    if site_error_page(hay, ""):
        return False
    if stage == "pay":
        return True
    return bool(_PAY_SHEET.search(hay or ""))


def read_visible_page(page) -> tuple[str, str, dict]:
    """Text that matches the viewport sheet — what the screenshot should show."""
    text, url = _read(page)
    try:
        surface = read_surface(page)
        visible = apply_front_text(text, surface)
    except Exception:
        surface = {"overlay": False, "text": "", "controls": ""}
        visible = text
    return visible, url, surface


def classify_page(text: str, url: str, *, book_count: int = 0) -> str:
    """Name the booking step from visible text. A nav 'Book' link is not a fare list."""
    hay = f"{text or ''}\n{url or ''}"
    if flight_unavailable(hay):
        return "unavailable"
    if site_error_page(hay, url):
        return "error"
    # Empty Cleartrip/Ixigo search home — before pay. Footer "Payment options" is not checkout.
    if re.search(r"first time flying|popular destinations|where from|search flights", hay, re.I):
        return "home"
    if re.search(
        r"enter otp|enter the otp|otp sent|otp received|verify your mobile|"
        r"one.?time password|resend otp|resend in \d+",
        hay,
        re.I,
    ):
        return "otp"
    # Cleartrip contact validation says "Please enter Mobile Number" — that is not login.
    if re.search(r"add contact details|email address", hay, re.I) and re.search(
        r"\+91|continue|review traveller", hay, re.I
    ):
        return "other"
    if re.search(r"log in to ixigo|loginVisible=true", hay, re.I) and re.search(
        r"continue|google", hay, re.I
    ):
        return "login"
    if (
        re.search(r"enter mobile number", hay, re.I)
        and re.search(r"continue|google|get otp", hay, re.I)
        and not re.search(r"add contact|email address|review traveller", hay, re.I)
    ):
        return "login"
    # Contact with empty mobile must never become pay (sidebar "Payment options" is marketing).
    if _CONTACT_SHEET.search(hay) or (
        re.search(r"add contact details", hay, re.I) and re.search(r"\+91|mobile", hay, re.I)
    ):
        return "other"
    if re.search(r"pay to complete your booking|name on card|enter card number|scan qr code", hay, re.I):
        return "pay"
    if re.search(r"payment options", hay, re.I) and re.search(
        r"pay to complete|enter card number|name on card|upi|net banking", hay, re.I
    ):
        return "pay"
    if re.search(r"\bupi\b", hay, re.I) and re.search(r"pay now|card number|net banking", hay, re.I):
        return "pay"
    if re.search(r"trip summary", hay, re.I) and re.search(r"\bpay\b|total fare", hay, re.I):
        return "pay"
    priced = bool(re.search(r"₹\s*[0-9]", hay))
    has_time = bool(re.search(r"\b\d{1,2}:\d{2}\b", hay))
    # Fare picker drawer can sit on top of a still-visible results list — detect it first.
    if _looks_like_fare_sheet(hay):
        return "fare_sheet"
    # Cleartrip "Review your itinerary" (flex Add cards) is review — never Skip-addons.
    if re.search(r"review your itinerary", hay, re.I):
        return "review"
    # Live Skip / Choose add-ons beats a lone "Book" word left on the itinerary sheet.
    if re.search(r"choose add-ons|missed adding|skip add-?ons", hay, re.I):
        return "addons"
    fare_list = bool(
        has_time and priced and (book_count >= 1 or re.search(r"\b(Book|Select|View fares)\b", hay))
    )
    # Fare lists win over soft add-on marketing copy on Cleartrip/Ixigo results.
    if fare_list and not flight_unavailable(hay):
        return "results"
    # Meals/luggage sheet without a review heading (Skip already handled above).
    if re.search(r"add seats|add meals|add extra luggage|choose your seat|add meal", hay, re.I):
        return "addons"
    # A review sheet that already shows the traveller is not the empty name form.
    if re.search(r"review details", hay, re.I) and re.search(r"\bcontinue\b", hay, re.I) and not re.search(
        r"please select your title", hay, re.I
    ):
        return "review"
    if re.search(r"first name|last name|date of birth|add adult|primary passenger", hay, re.I) and re.search(
        r"mobile|email|gender", hay, re.I
    ):
        return "passenger"
    # Heading alone is enough — placeholders may not appear in inner_text.
    if re.search(r"add traveller details|review traveller details", hay, re.I) and re.search(
        r"\bcontinue\b|male|female|title", hay, re.I
    ):
        return "passenger"
    if re.search(r"review & traveller|fare summary|total amount", hay, re.I) and re.search(
        r"\b(?:IX|AI|6E|SG|QP)[\s-]*\d+", hay, re.I
    ):
        return "review"
    if re.search(r"fare summary", hay, re.I) and re.search(r"\bcontinue\b", hay, re.I) and not re.search(
        r"first & middle|first name|last name|add traveller", hay, re.I
    ):
        return "review"
    if re.search(r"first & middle|first name|last name|add adult|primary passenger|add traveller", hay, re.I):
        return "passenger"
    if re.search(r"we respect your privacy|accept all cookies|cookie policy", hay, re.I) and not (priced and has_time):
        return "cookie"
    if _results_url(url):
        return "loading"
    if re.search(r"one way|search flight", hay, re.I):
        return "home"
    return "other"


def parse_action(raw: str) -> dict[str, str] | None:
    """Pull one action object out of a model reply."""
    match = re.search(r"\{.*\}", raw or "", re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    action = str(data.get("action") or "").strip().lower()
    if action not in {
        "click",
        "click_time",
        "search",
        "wait",
        "done",
        "stuck",
        "review_continue",
        "login_phone",
        "fill_passenger",
    }:
        return None
    return {
        "action": action,
        "target": str(data.get("target") or "").strip(),
        "value": str(data.get("value") or "").strip(),
    }


def deterministic_action(stage: str, *, hint: str, recovers: int, url: str = "") -> dict[str, str] | None:
    """Known screens get a fixed next step. The model is not asked."""
    if stage in {"pay", "otp"}:
        # Pay is handoff only. OTP needs the traveller — never invent a code.
        return {"action": "done", "target": stage, "value": ""}
    if stage == "cookie":
        return {"action": "click", "target": "Accept", "value": ""}
    if stage == "error" and recovers < 1:
        # Cleartrip's sheet says Go to home, not Try again.
        return {"action": "click", "target": "Go to home", "value": ""}
    if stage == "error":
        return {"action": "stuck", "target": "error", "value": ""}
    if stage == "fare_sheet":
        return {"action": "click", "target": "Continue", "value": ""}
    if stage == "review":
        return {"action": "review_continue", "target": "Continue", "value": ""}
    if stage == "login":
        return {"action": "login_phone", "target": "Continue", "value": ""}
    if stage == "passenger":
        return {"action": "fill_passenger", "target": "Continue", "value": ""}
    if stage == "addons":
        # Cleartrip shows Skip add-ons (and a "missed meals/seats" nag). Continue re-opens the nag.
        return {"action": "click", "target": "Skip add-ons", "value": ""}
    if stage == "unavailable":
        # Instinct reports sold-out honestly; Search Again restarts the fare list.
        return {"action": "stuck", "target": "unavailable", "value": ""}
    if stage == "results" and hint:
        return {"action": "click_time", "target": hint, "value": ""}
    if stage == "loading":
        return {"action": "wait", "target": "", "value": ""}
    if stage == "home" and not _results_url(url):
        return {"action": "search", "target": "", "value": ""}
    return None


_SURFACE_JS = """() => {
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 4 && r.height > 4 && s.visibility !== 'hidden'
      && s.display !== 'none' && r.bottom > 0 && r.top < innerHeight;
  };
  const labelOf = (el) => {
    const aria = el.getAttribute('aria-label') || '';
    let t = (el.innerText || aria || el.value || '').replace(/\\s+/g, ' ').trim();
    if (!t || t.length > 100) {
      const wrap = el.closest('label');
      if (wrap) t = (wrap.innerText || '').replace(/\\s+/g, ' ').trim();
    }
    return t.slice(0, 80);
  };
  const onOf = (el) => !!(el.checked
    || el.getAttribute('aria-checked') === 'true'
    || el.getAttribute('aria-pressed') === 'true');
  const disOf = (el) => !!(el.disabled || el.getAttribute('aria-disabled') === 'true');
  const frontRoot = () => {
    const cx = innerWidth / 2, cy = innerHeight / 2;
    const marked = [...document.querySelectorAll('[role=dialog],[aria-modal=true]')].filter(vis);
    // Do NOT query every div/section — Cleartrip seat maps stall Kernel CDP for minutes.
    const candidates = [...document.querySelectorAll(
      '[role=dialog],[aria-modal=true],aside,[data-testid*="modal"],[class*="modal"],[class*="drawer"],[class*="overlay"]'
    )].filter(vis);
    const panels = candidates.filter((el) => {
      if (marked.includes(el)) return false;
      const s = getComputedStyle(el);
      if (s.position !== 'fixed' && s.position !== 'absolute') return false;
      const r = el.getBoundingClientRect();
      if (r.width < 220 || r.height < 120) return false;
      if (r.width > innerWidth * 0.96 && r.height > innerHeight * 0.9) return false;
      if (!(r.left <= cx && r.right >= cx && r.top <= cy && r.bottom >= cy)) return false;
      const copy = (el.innerText || '').replace(/\\s+/g, ' ').trim();
      // Ignore Cleartrip/Ixigo toast chips ("No flight data available") when fares are underneath.
      if (copy.length < 48 && /no flight data|loading|pre-applied/i.test(copy)) return false;
      return [...el.querySelectorAll('button,a,[role=button],input')].some(vis);
    });
    const roots = [...marked, ...panels];
    if (!roots.length) return null;
    roots.sort((a, b) => {
      const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
      return (ra.width * ra.height) - (rb.width * rb.height);
    });
    return roots[0];
  };
  const root = frontRoot();
  const scope = root || document.body;
  const lines = [];
  const seen = new Set();
  const add = (kind, el) => {
    if (!vis(el) || !scope.contains(el)) return;
    const t = labelOf(el);
    if (!t || t.length < 2) return;
    const line = kind + '[' + (onOf(el) ? 'x' : ' ') + ']' + (disOf(el) ? '!' : '') + ' ' + t;
    if (seen.has(line)) return;
    seen.add(line);
    lines.push(line);
  };
  for (const el of scope.querySelectorAll('input[type=radio],[role=radio]')) add('radio', el);
  for (const el of scope.querySelectorAll('input[type=checkbox],[role=checkbox]')) add('check', el);
  for (const el of scope.querySelectorAll('button,a,[role=button]')) add('btn', el);
  return {
    overlay: !!root,
    text: (scope.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 4000),
    controls: lines.slice(0, 36).join('\\n'),
  };
}"""


def read_surface(page) -> dict[str, str | bool]:
    """The sheet on top, or the page itself when nothing covers the centre.

    overlay is true only for a dialog or a centred fixed panel. Text and controls
    are limited to that sheet so the review form behind it is not the step.
    """
    try:
        raw = page.evaluate(_SURFACE_JS)
    except Exception:
        raw = None
    if not isinstance(raw, dict):
        return {"overlay": False, "text": "", "controls": ""}
    return {
        "overlay": bool(raw.get("overlay")),
        "text": str(raw.get("text") or "")[:4000],
        "controls": str(raw.get("controls") or "")[:2000],
    }


def apply_front_text(page_text: str, surface: dict) -> str:
    """Use a real covering sheet. Ignore toast chips that sit on a full fare list."""
    if not surface.get("overlay"):
        return page_text or ""
    front = str(surface.get("text") or "").strip()
    if not front:
        return page_text or ""
    body = page_text or ""
    toast = bool(re.search(r"no flight data available|filter is pre-applied", front, re.I))
    has_fares = bool(re.search(r"\bBook\b", body, re.I) and re.search(r"₹\s*[0-9]", body))
    if toast and has_fares and not re.search(r"no longer available|search again", front, re.I):
        return body
    if len(front) < 48 and not re.search(
        r"confirm|search again|no longer available|no,?\s*thanks|secure my trip|assured",
        front,
        re.I,
    ):
        return body
    return front


def flight_unavailable(page: str) -> bool:
    """Sold-out / expired fare modal. Not a toast on a live results list."""
    hay = page or ""
    return bool(
        re.search(
            r"no longer available|flight is no longer|please search again|sold out|"
            r"fare (?:is )?no longer|itinerary (?:is )?unavailable",
            hay,
            re.I,
        )
    )


def _looks_like_fare_sheet(hay: str) -> bool:
    """Cleartrip/Ixigo fare picker (often a drawer over the results list)."""
    text = hay or ""
    if re.search(r"select your fare|choose your fare|choose a fare|pick a fare|fare options", text, re.I):
        return True
    # A single VALUE badge on Ixigo review is not the fare picker.
    if re.search(r"review\s*&\s*traveller|review details|traveller details|fare summary", text, re.I):
        return False
    fare_names = len(re.findall(r"\b(saver|value|flexi|classic|standard fare|regular fare)\b", text, re.I))
    if fare_names >= 2 and re.search(r"\b(continue|proceed|continue booking)\b", text, re.I):
        return True
    return False


def nearest_depart_hint(page: str, hint: str) -> str:
    """Exact clock, else the closest departure within 45 minutes (18:20 → 18:25)."""
    raw = (hint or "").strip()
    if not raw:
        return ""
    if any(re.search(rf"(^|[^0-9]){re.escape(n)}([^0-9]|$)", page or "") for n in _clock_needles(raw)):
        return raw
    match = re.match(r"(\d{1,2}):(\d{2})", raw)
    if not match:
        return raw
    want = int(match.group(1)) * 60 + int(match.group(2))
    alts = {want}
    if int(match.group(1)) < 12:
        alts.add(want + 12 * 60)
    elif int(match.group(1)) >= 13:
        alts.add(want - 12 * 60)
    best = ""
    best_gap = 46
    for clock in re.findall(r"\b(\d{1,2}):(\d{2})\b", page or ""):
        mins = int(clock[0]) * 60 + int(clock[1])
        for target in alts:
            gap = abs(mins - target)
            if gap < best_gap:
                best_gap = gap
                best = f"{int(clock[0])}:{clock[1]}"
    return best or raw


def control_digest(page) -> str:
    """Visible radios, checkboxes, and buttons with selected/disabled state.

    [x] is on, [ ] is off, a trailing ! is disabled. Empty when the DOM cannot be read.
    """
    try:
        raw = page.evaluate(
            """() => {
              const vis = (el) => {
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 4 && r.height > 4 && s.visibility !== 'hidden'
                  && s.display !== 'none' && r.bottom > 0 && r.top < innerHeight;
              };
              const labelOf = (el) => {
                const aria = el.getAttribute('aria-label') || '';
                let t = (el.innerText || aria || el.value || '').replace(/\\s+/g, ' ').trim();
                if ((!t || t.length > 100) && el.id) {
                  const lab = document.querySelector('label[for=\"' + String(el.id).replace(/\"/g, '') + '\"]');
                  if (lab) t = (lab.innerText || '').replace(/\\s+/g, ' ').trim();
                }
                if (!t || t.length > 100) {
                  const wrap = el.closest('label');
                  if (wrap) t = (wrap.innerText || '').replace(/\\s+/g, ' ').trim();
                }
                return t.slice(0, 80);
              };
              const onOf = (el) => !!(el.checked
                || el.getAttribute('aria-checked') === 'true'
                || el.getAttribute('aria-pressed') === 'true');
              const disOf = (el) => !!(el.disabled || el.getAttribute('aria-disabled') === 'true');
              const lines = [];
              const seen = new Set();
              const add = (kind, el) => {
                if (!vis(el)) return;
                const t = labelOf(el);
                if (!t || t.length < 2) return;
                const line = kind + '[' + (onOf(el) ? 'x' : ' ') + ']' + (disOf(el) ? '!' : '') + ' ' + t;
                if (seen.has(line)) return;
                seen.add(line);
                lines.push(line);
              };
              for (const el of document.querySelectorAll('input[type=radio],[role=radio]')) add('radio', el);
              for (const el of document.querySelectorAll('input[type=checkbox],[role=checkbox]')) add('check', el);
              for (const el of document.querySelectorAll('button,a,[role=button]')) add('btn', el);
              return lines.slice(0, 36).join('\\n');
            }"""
        )
    except Exception:
        return ""
    return str(raw or "").strip()


def _surface_hay(surface: dict) -> str:
    return f"{surface.get('text') or ''}\n{surface.get('controls') or ''}"


def _clear_front_sheet(page, chosen_tap: str = "", *, seller: str = "", hint: str = "") -> str:
    """Conscience decides; rails click only a safe on-page label. Empty when none."""
    from app.step_read import decide_front

    surface = read_surface(page)
    if not surface.get("overlay"):
        return ""
    shot = ""
    decision: dict = {}
    try:
        from app.vision_agent import capture_page_shot, vision_enabled

        if vision_enabled():
            shot = capture_page_shot(page)
        decision = decide_front(
            str(surface.get("text") or ""),
            str(surface.get("controls") or ""),
            seller=seller,
            hint=hint,
            chosen_tap=chosen_tap,
            image_path=shot,
        )
    finally:
        if shot:
            try:
                Path(shot).unlink(missing_ok=True)
            except Exception:
                pass
    if decision.get("action") != "click":
        return "front_ask" if decision.get("action") == "ask" else "front_none"
    label = str(decision.get("tap") or "")
    front_copy = str(surface.get("text") or "")
    if re.search(r"skip add|continue without add", label, re.I) or re.search(
        r"missed adding|skip add-?ons|choose add-ons|add seats", front_copy, re.I
    ):
        notes: list[str] = []
        if _leave_addons(page, notes, rounds=3):
            return "front_tap:" + (notes[-1] if notes else "Skip add-ons")
        hit = _skip_addons(page)
        if hit:
            return f"front_tap:{hit}"
    if label and _click_label(page, label):
        try:
            page.wait_for_timeout(500)
        except Exception:
            pass
        # Meal/seat nag after a decline / continue on add-ons.
        if re.search(r"skip|no,? thanks|continue", label, re.I):
            notes = []
            if _leave_addons(page, notes, rounds=2):
                return f"front_tap:{label}+left"
        return f"front_tap:{label}"
    return "front_tap_miss"


def _pause_unknown(
    page,
    *,
    seller: str,
    hint: str,
    text: str,
    url: str,
    log: list[str],
    facts: dict[str, str],
    checks: list[str],
    step: int,
    chosen_tap: str = "",
) -> CheckoutOutcome:
    """Stop before a guessed click. Conscience names the front sheet for WhatsApp."""
    from app.step_read import decide_front, describe_step

    surface = read_surface(page)
    if surface.get("overlay") and str(surface.get("text") or "").strip():
        text = apply_front_text(text, surface)
    digest = str(surface.get("controls") or "") or control_digest(page)
    shot = ""
    try:
        from app.vision_agent import capture_page_shot, vision_enabled

        if vision_enabled():
            shot = capture_page_shot(page)
    except Exception:
        shot = ""
    try:
        decision = decide_front(
            text, digest, seller=seller, hint=hint, chosen_tap=chosen_tap, image_path=shot
        )
    finally:
        if shot:
            try:
                Path(shot).unlink(missing_ok=True)
            except Exception:
                pass
    heard = {
        "kind": decision.get("kind") or "other",
        "what": decision.get("what") or f"a {seller} step I don't treat as done",
        "why": decision.get("why") or "the page has not moved on",
        "recommend": decision.get("recommend") or "send the exact button you want",
        "tap": decision.get("tap") or "",
    }
    facts["tap"] = heard["tap"]
    facts["controls"] = digest[:800]
    handoff = describe_step(heard)
    log.append(f"{step}:ask:{heard['kind']}:{heard['tap']}")
    return CheckoutOutcome(
        "ask",
        text,
        url,
        log,
        facts=facts,
        checks=checks,
        handoff=handoff,
    )


def _buttons(page) -> list[str]:
    try:
        labels = page.evaluate(
            """() => [...document.querySelectorAll('button,a,[role=button]')]
                .map(b => (b.innerText || '').replace(/\\s+/g, ' ').trim())
                .filter(t => t && t.length < 40)
                .slice(0, 25)"""
        )
    except Exception:
        return []
    return [str(item) for item in labels or []]


def _ixigo_scroll_until_depart(page, hint: str, *, rounds: int = 16) -> bool:
    """Scroll Ixigo's overflow panel until a matching departure is in the DOM."""
    needles = set(_clock_needles(hint))
    for _ in range(rounds):
        try:
            text = page.inner_text("body", timeout=5000) or ""
        except Exception:
            text = ""
        # Prefer real departure rows (time near GAU), not only any clock on the page.
        if any(
            re.search(rf"(^|[^0-9]){re.escape(n)}([^0-9].*)?\bGAU\b", text, re.I) for n in needles
        ) or any(n in text for n in needles):
            # Confirm a Book button card actually carries that depart.
            probe = page.evaluate(
                """(needles) => {
                    const isBook = (b) => /^(book)$/i.test((b.innerText || '').trim());
                    for (const btn of document.querySelectorAll('button,a,[role=button]')) {
                        if (!isBook(btn)) continue;
                        let el = btn, card = '';
                        for (let i = 0; i < 12 && el; i++) {
                            const t = (el.innerText || '').replace(/\\s+/g, ' ').trim();
                            const books = (t.match(/\\bBook\\b/gi) || []).length;
                            const times = (t.match(/\\b\\d{1,2}:\\d{2}\\b/g) || []).length;
                            if (books > 2 || times > 4) break;
                            if (t) card = t;
                            el = el.parentElement;
                        }
                        const depart = (card.match(/\\b\\d{1,2}:\\d{2}\\b/g) || [])[0] || '';
                        if (needles.includes(depart)) return true;
                    }
                    return false;
                }""",
                list(needles),
            )
            if probe:
                return True
        page.evaluate(
            """() => {
                const books = [...document.querySelectorAll('button')]
                    .filter(b => /^book$/i.test((b.innerText || '').trim()));
                let el = books[0] || document.body;
                for (let i = 0; i < 14 && el; i++) {
                    const st = getComputedStyle(el);
                    if ((el.scrollHeight - el.clientHeight) > 80 && /(auto|scroll)/.test(st.overflowY || '')) {
                        el.scrollTop = Math.min(el.scrollTop + Math.max(500, el.clientHeight * 0.9), el.scrollHeight);
                        if (books.length) books[books.length - 1].scrollIntoView({block: 'end'});
                        return;
                    }
                    el = el.parentElement;
                }
                window.scrollBy(0, 700);
            }"""
        )
        try:
            page.wait_for_timeout(350)
        except Exception:
            break
    return False


def _click_ixigo_depart(page, hint: str) -> str:
    """Playwright click on the Book control inside the matching Ixigo card."""
    needles = _clock_needles(hint)
    for needle in needles:
        try:
            card = page.locator("div").filter(has_text=re.compile(rf"(^|[^0-9]){re.escape(needle)}([^0-9].*)?\bGAU\b", re.I)).filter(
                has=page.get_by_role("button", name=re.compile(r"^Book$", re.I))
            )
            if card.count() < 1:
                continue
            target = card.first
            target.scroll_into_view_if_needed(timeout=5000)
            label = (target.inner_text(timeout=3000) or "").replace("\n", " ")
            target.get_by_role("button", name=re.compile(r"^Book$", re.I)).first.click(timeout=5000)
            return label[:180]
        except Exception:
            continue
    return ""


def _prepare_ixigo_results(page, hint: str = "") -> None:
    """Wait out Ixigo hydration, dismiss overlays, scroll until the depart row exists."""
    try:
        url = str(page.url or "")
    except Exception:
        url = ""
    if "ixigo.com" not in url:
        return
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)
    except Exception:
        pass
    # Featured sticky cards appear first; wait until more Books hydrate (exit early).
    for _ in range(40):
        try:
            n = _book_count(page)
        except Exception:
            n = 0
        if n >= 2:
            break
        try:
            page.wait_for_timeout(300)
        except Exception:
            break
    if hint:
        _ixigo_scroll_until_depart(page, hint)


def _book_count(page) -> int:
    try:
        return int(
            page.evaluate(
                """() => [...document.querySelectorAll('button,a,[role=button]')]
                    .filter(b => /^(book|book now|select|view fares)$/i.test((b.innerText || '').trim())).length"""
            )
            or 0
        )
    except Exception:
        return 0


def _read(page) -> tuple[str, str]:
    try:
        text = page.inner_text("body", timeout=8000) or ""
    except Exception:
        text = ""
    try:
        url = str(page.url or "")
    except Exception:
        url = ""
    return text, url


_ITINERARY_CREATE = "/itin/v7/itinerary/create"


def interpret_itinerary_create(status: int | None, body: str = "") -> str:
    """Classify Cleartrip's itinerary create response. A Playwright click is not enough."""
    text = body or ""
    if status is None:
        return "unknown"
    if status == 403 or re.search(r"access denied", text, re.I):
        return "denied"
    if status >= 400 or re.search(r"server error|itinerary/failure", text, re.I):
        return "error"
    if 200 <= status < 300:
        return "ok"
    return "unknown"


def itinerary_id_from_create(body: str = "", payload: dict | None = None) -> str:
    """Cleartrip create returns data.itineraryId even when the fare drawer never navigates."""
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict):
            raw = str(data.get("itineraryId") or data.get("itinId") or "").strip()
            if raw:
                return raw
    text = body or ""
    match = re.search(r'"itineraryId"\s*:\s*"([^"]+)"', text)
    if match:
        return str(match.group(1) or "").strip()
    match = re.search(r'"itinId"\s*:\s*"([^"]+)"', text)
    return str(match.group(1) or "").strip() if match else ""


def cleartrip_itinerary_url(itinerary_id: str, *, info: bool = True) -> str:
    """Guest checkout page for a created itinerary."""
    iid = (itinerary_id or "").strip()
    if not iid:
        return ""
    base = f"https://www.cleartrip.com/flights/itinerary/{iid}"
    return f"{base}/info" if info else base


def open_cleartrip_itinerary(page, itinerary_id: str) -> bool:
    """Force the SPA onto the created itinerary when Continue left the results URL open."""
    iid = (itinerary_id or "").strip()
    if not iid:
        return False
    # Bare /itinerary/{id} often works when /info returns Cleartrip's "wrong url" shell.
    for dest in (cleartrip_itinerary_url(iid, info=False), cleartrip_itinerary_url(iid, info=True)):
        try:
            page.goto(dest, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(1200)
        except Exception:
            continue
        try:
            text, url = _read(page)
            stage = classify_page(text, url, book_count=_book_count(page))
        except Exception:
            continue
        if stage == "error" or site_error_page(text, url) or "itinerary/failure" in (url or "").lower():
            continue
        if "itinerary" in (url or "").lower() and "/results" not in (url or "").lower():
            return True
    return False


def _advance_after_book(page, hint: str, log: list[str]) -> bool:
    """After Book on a results row: open/confirm the fare drawer instead of asking.

    Cleartrip often keeps the fare list URL while a Select-your-fare sheet is open.
    Returns True when the page left results (or Continue was attempted).
    """
    text, url = _read(page)
    surface = read_surface(page)
    front = apply_front_text(text, surface)
    stage = classify_page(front, url, book_count=_book_count(page))
    if stage == "fare_sheet" or _looks_like_fare_sheet(front) or (
        surface.get("overlay") and re.search(r"continue|saver|value|fare", str(surface.get("text") or ""), re.I)
    ):
        log.append("after_book:fare_sheet")
        ok = _confirm_fare(page)
        log.append(f"confirm_fare:{ok}")
        if not ok:
            try:
                page.wait_for_timeout(800)
            except Exception:
                pass
            ok = _confirm_fare(page)
            log.append(f"confirm_fare_retry:{ok}")
        text_after, url_after = _read(page)
        stage_after = classify_page(text_after, url_after, book_count=_book_count(page))
        return ok or stage_after in {"addons", "review", "passenger", "pay", "login", "otp"} or (
            "itinerary" in (url_after or "").lower() and stage_after != "error"
        )
    # JS Book sometimes paints no drawer — try a real mouse Book on the departure card.
    if hint and _mouse_book_depart(page, hint):
        log.append("after_book:mouse_book")
        try:
            page.wait_for_timeout(1200)
        except Exception:
            pass
        after, _au, after_stage, _af = _poll_ready(
            page,
            {"fare_sheet", "review", "passenger", "login", "otp", "pay", "error", "unavailable", "addons"},
            budget_ms=3500,
        )
        if after_stage == "fare_sheet" or _looks_like_fare_sheet(after):
            log.append("after_book:fare_sheet")
            ok = _confirm_fare(page)
            log.append(f"confirm_fare:{ok}")
            if not ok:
                ok = _confirm_fare(page)
                log.append(f"confirm_fare_retry:{ok}")
            return ok or "itinerary" in (_read(page)[1] or "").lower()
        if after_stage in {"review", "passenger", "login", "otp", "pay", "addons"}:
            log.append(f"after_book:{after_stage}")
            return True
    return False


def _mouse_book_depart(page, hint: str) -> bool:
    """Playwright mouse click on Book for the matching departure (Cleartrip-friendly)."""
    needles = set(_clock_needles(hint))
    try:
        buttons = page.locator("button, a, [role=button]").filter(
            has_text=re.compile(r"^(Book|Book now|Select|View fares)$", re.I)
        )
        count = min(buttons.count(), 40)
    except Exception:
        return False
    for i in range(count):
        try:
            btn = buttons.nth(i)
            card = ""
            handle = btn.element_handle()
            if handle is not None:
                card = str(
                    page.evaluate(
                        """(el) => {
                            let n = el, t = '';
                            for (let i = 0; i < 10 && n; i++) {
                                const x = (n.innerText || '').replace(/\\s+/g, ' ').trim();
                                const books = (x.match(/\\b(Book|Select|View fares)\\b/gi) || []).length;
                                const times = (x.match(/\\b\\d{1,2}:\\d{2}\\b/g) || []).length;
                                if (books > 2 || times > 4) break;
                                if (x) t = x;
                                n = n.parentElement;
                            }
                            return t.slice(0, 240);
                        }""",
                        handle,
                    )
                    or ""
                )
            if not card_matches_depart_hint(card, hint) and not any(n in card for n in needles):
                continue
            btn.scroll_into_view_if_needed(timeout=2000)
            box = btn.bounding_box()
            if not box:
                continue
            page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            return True
        except Exception:
            continue
    return False


def _confirm_fare(page) -> bool:
    """Select the fare, click Continue, and require leaving the fare sheet.

    Cleartrip's Continue posts to /itin/v7/itinerary/create. A mouse click that
    returns without navigation is not success — Akamai often replies 403 Access
    Denied and the sheet stays open. A 200 with itineraryId still counts when the
    SPA leaves the drawer closed on the results URL — open /itinerary/{id}/info.
    """
    create: dict[str, object] = {"status": None, "body": "", "itinerary_id": ""}

    def on_response(res) -> None:
        if _ITINERARY_CREATE not in (res.url or ""):
            return
        create["status"] = res.status
        payload = None
        try:
            payload = res.json()
        except Exception:
            payload = None
        if isinstance(payload, dict):
            create["itinerary_id"] = itinerary_id_from_create(payload=payload)
            try:
                import json as _json

                create["body"] = _json.dumps(payload)[:8000]
            except Exception:
                create["body"] = str(payload)[:8000]
            return
        try:
            body = res.text() or ""
        except Exception:
            body = ""
        create["body"] = body[:8000]
        create["itinerary_id"] = itinerary_id_from_create(body=body)

    try:
        page.on("response", on_response)
    except Exception:
        pass

    def _click_continue() -> bool:
        try:
            point = page.evaluate(
                """() => {
                    const root = document.querySelector('[role="dialog"]') || document.body;
                    const radios = [...root.querySelectorAll('input[type="radio"]')];
                    const radio = radios.find(r => r.checked) || radios.find(r => !r.disabled) || radios[0];
                    if (radio && !radio.checked) radio.click();
                    const fare = [...root.querySelectorAll('label,div,button')].find(el => {
                        const t = (el.innerText || '').replace(/\\s+/g, ' ').trim();
                        return t.length > 0 && t.length < 180 && /₹/.test(t)
                            && /saver|value|flexi|classic|flex|standard|regular fare/i.test(t);
                    });
                    if (fare) fare.click();
                    const btn = [...root.querySelectorAll('button,a,[role=button]')].reverse().find(
                        b => /^(continue|proceed|continue booking)$/i.test((b.innerText || '').trim())
                    );
                    if (!btn) return null;
                    btn.scrollIntoView({block:'center'});
                    const r = btn.getBoundingClientRect();
                    return {x: r.x + r.width / 2, y: r.y + r.height / 2};
                }"""
            )
        except Exception:
            point = None
        if isinstance(point, dict) and point.get("x") is not None:
            try:
                page.mouse.click(float(point["x"]), float(point["y"]))
                return True
            except Exception:
                pass
        try:
            btn = page.get_by_role("button", name=re.compile(r"^(Continue|Proceed)$", re.I)).last
            btn.scroll_into_view_if_needed(timeout=3000)
            btn.click(timeout=5000)
            return True
        except Exception:
            return _safe_continue(page)

    clicked = _click_continue()
    if not clicked:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass
        return False

    for _ in range(60):
        if create["status"] is not None:
            break
        try:
            url_now = page.url or ""
            if "itinerary" in url_now and "/results" not in url_now:
                break
            page.wait_for_timeout(500)
        except Exception:
            break

    # First click often no-ops under Akamai; one more Continue if still on the fare drawer.
    try:
        text_now, url_now = _read(page)
        still_fare = classify_page(text_now, url_now, book_count=_book_count(page)) == "fare_sheet" or (
            _looks_like_fare_sheet(text_now) and "itinerary" not in (url_now or "")
        )
    except Exception:
        still_fare = create["status"] is None
    if still_fare and create["status"] is None:
        _click_continue()
        for _ in range(40):
            if create["status"] is not None:
                break
            try:
                if "itinerary" in (page.url or "") and "/results" not in (page.url or ""):
                    break
                page.wait_for_timeout(500)
            except Exception:
                break

    try:
        page.remove_listener("response", on_response)
    except Exception:
        pass

    outcome = interpret_itinerary_create(
        create["status"] if isinstance(create["status"], int) else None,
        str(create.get("body") or ""),
    )
    itinerary_id = str(create.get("itinerary_id") or itinerary_id_from_create(str(create.get("body") or "")))
    logger.info(
        "Continue create status=%s outcome=%s itinerary_id=%s",
        create.get("status"),
        outcome,
        itinerary_id[:40],
    )
    # A 403 or server error is a rejected booking even if the URL changes to the beach-chair page.
    if outcome in {"denied", "error"}:
        return False
    # Guest holds expire in ~20s — navigate the instant we have itineraryId.
    # Do not burn the TTL polling the fare drawer.
    try:
        if "itinerary" in (page.url or "") and "/results" not in (page.url or ""):
            return True
    except Exception:
        pass
    if outcome == "ok" and itinerary_id and open_cleartrip_itinerary(page, itinerary_id):
        left = classify_page(*_read(page), book_count=_book_count(page))
        return left not in {"error", "fare_sheet", "results", "home"}
    left = _wait_until(page, "fare_sheet", rounds=8)
    if left == "error":
        return False
    if left in {"addons", "review", "passenger", "pay", "login", "otp", "other"}:
        return True
    if left != "fare_sheet":
        return True
    # Click ran; Cleartrip did not accept the booking step.
    return False


def _latest(page):
    try:
        pages = page.context.pages
        if pages:
            return pages[-1]
    except Exception:
        pass
    return page


def _wait_until(page, leave: str, *, rounds: int = 12) -> str:
    """Poll until the page is no longer `leave`. Returns the new stage."""
    stage = leave
    for _ in range(rounds):
        text, url = _read(page)
        stage = classify_page(text, url, book_count=_book_count(page))
        if stage != leave:
            return stage
        try:
            page.wait_for_timeout(400)
        except Exception:
            return stage
    return stage


def _poll_ready(page, ready: set[str], *, budget_ms: int = 4000, step_ms: int = 300):
    """Return as soon as the page is a known booking stage. Caps the wait."""
    deadline = time.monotonic() + budget_ms / 1000
    text, url, stage, facts = "", "", "other", {}
    while True:
        text, url = _read(page)
        stage = classify_page(text, url, book_count=_book_count(page))
        facts = extract_trip_facts(text, url)
        if stage in ready or time.monotonic() >= deadline:
            return text, url, stage, facts
        try:
            page.wait_for_timeout(step_ms)
        except Exception:
            return text, url, stage, facts


_AIM_JS = """(label) => {
  const want = String(label || '').trim().toLowerCase();
  if (!want) return [];
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 8 && r.height > 8 && r.bottom > 0 && r.top < innerHeight
      && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const textOf = (el) => (
    (el.innerText || el.getAttribute('aria-label') || el.value || '')
      .replace(/\\s+/g, ' ').trim().toLowerCase()
  );
  const dialogs = [...document.querySelectorAll('[role=dialog],[aria-modal=true]')].filter(vis);
  dialogs.sort((a, b) => {
    const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
    return (ra.width * ra.height) - (rb.width * rb.height);
  });
  const roots = dialogs.length ? [dialogs[0], document.body] : [document.body];
  const seen = new Set();
  const out = [];
  for (const root of roots) {
    // Prefer real controls. Scanning every span/div on Cleartrip seat maps stalls Kernel.
    const nodes = [...root.querySelectorAll('button,a,[role=button],input[type=submit],label')];
    for (const el of nodes) {
      if (textOf(el) !== want) continue;
      const rough = el.getBoundingClientRect();
      if (rough.width < 8 || rough.height < 8) continue;
      if (rough.width > innerWidth * 0.72 || rough.height > innerHeight * 0.45) continue;
      el.scrollIntoView({block: 'center', inline: 'nearest'});
      if (!vis(el)) continue;
      const r = el.getBoundingClientRect();
      const key = Math.round(r.x) + ':' + Math.round(r.y) + ':' + Math.round(r.width);
      if (seen.has(key)) continue;
      seen.add(key);
      const tag = (el.tagName || '').toLowerCase();
      const role = (el.getAttribute('role') || '').toLowerCase();
      const control = tag === 'button' || tag === 'a' || role === 'button' ? 0 : 1;
      out.push({
        x: r.x + r.width / 2,
        y: r.y + r.height / 2,
        w: r.width,
        h: r.height,
        control,
        area: r.width * r.height,
      });
    }
    if (out.length) break;
  }
  out.sort((a, b) => (a.control - b.control) || (a.area - b.area));
  return out.slice(0, 3);
}"""


def _aim_mark(page, label: str) -> str:
    """Fingerprint used to prove a click moved the sheet."""
    text, url = _read(page)
    try:
        surface = read_surface(page)
        front = apply_front_text(text, surface)
        overlay = "1" if surface.get("overlay") else "0"
    except Exception:
        front, overlay = text, "0"
    stage = classify_page(front, url, book_count=_book_count(page))
    needle = re.sub(r"\s+", " ", (label or "").strip().lower())
    present = "1" if needle and needle in re.sub(r"\s+", " ", front.lower()) else "0"
    return f"{stage}|{overlay}|{present}|{re.sub(r'\\s+', ' ', front)[:180]}"


def _sheet_moved(page, label: str, before: str, *, wait_s: float = 2.0) -> bool:
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        try:
            page.wait_for_timeout(250)
        except Exception:
            break
        if _aim_mark(page, label) != before:
            return True
    return False


def _locator_click(page, label: str) -> bool:
    """Playwright hit-test for the same exact label when the mouse point misses."""
    exact = re.compile(rf"^{re.escape(label)}$", re.I)
    for loc in (
        page.get_by_role("button", name=exact),
        page.get_by_role("link", name=exact),
        page.get_by_text(exact),
    ):
        try:
            target = loc.first
            target.scroll_into_view_if_needed(timeout=1500)
            target.click(timeout=2000)
            return True
        except Exception:
            continue
    return False


def click_verified(page, label: str) -> bool:
    """Mouse-click the smallest real control, then require the sheet to change.

    A dialog-sized box is never a target. Works for button, link, or a short div
    with that exact label, including one that starts below the fold. True only
    when the label disappears, the dialog closes, or the booking step changes.
    """
    wanted = (label or "").strip()
    if not wanted or wanted.startswith("__") or re.search(r"\bpay\b", wanted, re.I):
        return False
    try:
        points = page.evaluate(_AIM_JS, wanted) or []
    except Exception:
        points = []
    if not isinstance(points, list) or not points:
        try:
            page.mouse.wheel(0, 900)
            page.wait_for_timeout(200)
            points = page.evaluate(_AIM_JS, wanted) or []
        except Exception:
            points = []
    if not isinstance(points, list) or not points:
        before = _aim_mark(page, wanted)
        if not _locator_click(page, wanted):
            return False
        return _sheet_moved(page, wanted, before)
    before = _aim_mark(page, wanted)
    for point in points[:2]:
        if not isinstance(point, dict):
            continue
        try:
            page.mouse.click(float(point["x"]), float(point["y"]))
        except Exception:
            continue
        if _sheet_moved(page, wanted, before):
            return True
    if _locator_click(page, wanted) and _sheet_moved(page, wanted, before):
        return True
    return False


def _click_label(page, label: str) -> bool:
    """Verified click. True only when the page actually changes."""
    return click_verified(page, label)


def leave_site_error(page) -> str:
    """Click the control Cleartrip actually shows on the acting-up / stumped sheet."""
    text, _url = _read(page)
    for label in ("Go to home", "Search Again", "Try again", "Home"):
        if not re.search(rf"(?<![A-Za-z]){re.escape(label)}(?![A-Za-z])", text or "", re.I):
            continue
        if _click_label(page, label):
            return label
    return ""


def _reopen_cleartrip_results(page, origin: str, destination: str, depart: str, log: list[str]) -> bool:
    """Drop the dead itinerary and open a fresh Cleartrip fare list. One recovery, same browser."""
    from app.search import cleartrip_results_url

    try:
        page.goto(
            cleartrip_results_url(origin, destination, depart),
            wait_until="domcontentloaded",
            timeout=60000,
        )
        page.wait_for_timeout(800)
        log.append("error_rebook:results")
        return True
    except Exception:
        log.append("error_rebook:miss")
        return False


def _skip_addons(page) -> str:
    """Leave Cleartrip/Ixigo optional extras, including the missed meals/seats nag.

    Scrolls to find Skip when the seat map fills the viewport. Returns the label
    clicked, or empty when nothing matched. Cleartrip Skip ignores el.click() and
    CDP/mouse — invoke React fiber onClick instead (verified live on Kernel).
    """
    labels = (
        "Skip add-ons",
        "Skip add ons",
        "Continue without add-ons",
        "Continue without addons",
        "No, Thanks",
        "No Thanks",
        "Skip",
    )

    def _js_skip() -> str:
        try:
            # Buttons only — scanning every span/div on Cleartrip's seat map stalls Kernel CDP.
            hit = page.evaluate(
                """(labels) => {
                  const nodes = [...document.querySelectorAll('button,a,[role=button]')];
                  const fakeEvt = (el) => ({
                    preventDefault() {},
                    stopPropagation() {},
                    nativeEvent: { isTrusted: true },
                    target: el,
                    currentTarget: el,
                    type: 'click',
                    bubbles: true,
                    cancelable: true,
                  });
                  const fireReact = (el) => {
                    // Prefer fiber memoizedProps — Cleartrip also sets a DOM onclick that only
                    // fires analytics (FB pixel) and leaves Choose add-ons open.
                    const fiberKey = Object.keys(el).find(k => k.startsWith('__reactFiber$'));
                    let fiber = fiberKey ? el[fiberKey] : null;
                    for (let i = 0; i < 12 && fiber; i++) {
                      const props = fiber.memoizedProps || fiber.pendingProps;
                      if (props && typeof props.onClick === 'function') {
                        props.onClick(fakeEvt(el));
                        return 'fiber:' + i;
                      }
                      fiber = fiber.return;
                    }
                    const propsKey = Object.keys(el).find(k => k.startsWith('__reactProps$'));
                    if (propsKey && el[propsKey] && typeof el[propsKey].onClick === 'function') {
                      el[propsKey].onClick(fakeEvt(el));
                      return 'props';
                    }
                    if (typeof el.onclick === 'function') {
                      el.onclick(new MouseEvent('click', { bubbles: true, cancelable: true }));
                      return 'dom';
                    }
                    return '';
                  };
                  for (const want of labels) {
                    const el = nodes.find(n => {
                      const t = (n.innerText || n.getAttribute('aria-label') || '').replace(/\\s+/g,' ').trim();
                      return t && t.length < 48 && t.toLowerCase() === String(want).toLowerCase();
                    });
                    if (!el) continue;
                    // Footer sits inside .oy-scroll — scroll that first or Skip stays below the fold.
                    let p = el.parentElement;
                    while (p) {
                      const s = getComputedStyle(p);
                      if (/(auto|scroll)/.test(s.overflowY) && p.scrollHeight > p.clientHeight + 20) {
                        p.scrollTop = p.scrollHeight;
                      }
                      p = p.parentElement;
                    }
                    el.scrollIntoView({block:'center', inline:'nearest'});
                    if (fireReact(el)) return want;
                    const r = el.getBoundingClientRect();
                    if (r.width < 8 || r.height < 8) continue;
                    if (r.top < 0 || r.bottom > innerHeight) continue;
                    return {
                      label: want,
                      x: r.x + r.width / 2,
                      y: r.y + r.height / 2,
                    };
                  }
                  return null;
                }""",
                list(labels),
            )
            if isinstance(hit, str) and hit:
                return hit
            if isinstance(hit, dict) and hit.get("x") is not None:
                try:
                    page.mouse.click(float(hit["x"]), float(hit["y"]))
                    return str(hit.get("label") or "Skip add-ons")
                except Exception:
                    pass
            return ""
        except Exception:
            return ""

    def _playwright_skip() -> str:
        for label in ("Skip add-ons", "Skip add ons", "Continue without add-ons"):
            try:
                btn = page.get_by_role("button", name=re.compile(rf"^{re.escape(label)}$", re.I)).last
                btn.scroll_into_view_if_needed(timeout=3000)
                btn.click(timeout=5000, force=True)
                return label
            except Exception:
                continue
        return ""

    def _try() -> str:
        # Fiber/React first — Playwright force often times out on Cleartrip's unstable seat map.
        hit = _js_skip() or _playwright_skip()
        if hit:
            return hit
        for label in labels:
            if label == "Skip":
                continue
            if _click_label(page, label):
                return label
        return ""

    clicked = _try()
    if not clicked:
        for _ in range(4):
            try:
                page.mouse.wheel(0, 900)
                page.wait_for_timeout(350)
            except Exception:
                break
            clicked = _try()
            if clicked:
                break
    if not clicked:
        return ""
    try:
        page.wait_for_timeout(700)
    except Exception:
        pass
    # Cleartrip often opens "You missed adding Meal(s) and Seat(s)" — Skip again.
    try:
        text, _url = _read(page)
        surface = read_surface(page)
        hay = apply_front_text(text, surface)
    except Exception:
        hay = ""
    if re.search(r"missed adding|skip add-?ons|okay,?\s*let me add", hay, re.I):
        again = _js_skip()
        if again:
            clicked = again
            try:
                page.wait_for_timeout(600)
            except Exception:
                pass
        else:
            for label in ("Skip add-ons", "Skip add ons"):
                if _click_label(page, label):
                    clicked = label
                    try:
                        page.wait_for_timeout(600)
                    except Exception:
                        pass
                    break
    return clicked


def settle_visible_sheet(page, budget_ms: int = 8000) -> None:
    """Wait until Choose add-ons has painted Skip, and scroll that control into view.

    A gray skeleton is not a screenshot. Header and fare rail can be ready while Skip is still missing.
    Cleartrip's seat map keeps growing — Skip's React handler no-ops until scrollHeight settles.
    """
    from app.step_read import addons_sheet

    deadline = time.monotonic() + max(0.5, budget_ms / 1000)
    last_sh = -1
    stable = 0
    while time.monotonic() < deadline:
        try:
            text, _url = _read(page)
        except Exception:
            return
        if not addons_sheet(text):
            return
        try:
            found = page.evaluate(
                """() => {
                  // Buttons only — span/div scans on Cleartrip seat maps stall Kernel CDP.
                  const nodes = [...document.querySelectorAll('button, a, [role="button"]')];
                  const hit = nodes.find(n => /skip add-?ons|continue without add-?ons/i.test((n.innerText || '').trim())
                    && (n.innerText || '').trim().length < 40);
                  if (!hit) return {ok:false, sh:0};
                  let p = hit.parentElement;
                  let sh = 0;
                  while (p) {
                    const s = getComputedStyle(p);
                    if (/(auto|scroll)/.test(s.overflowY) && p.scrollHeight > p.clientHeight + 20) {
                      p.scrollTop = p.scrollHeight;
                      sh = Math.max(sh, p.scrollHeight);
                    }
                    p = p.parentElement;
                  }
                  hit.scrollIntoView({block: 'center', inline: 'nearest'});
                  return {ok:true, sh: sh || document.body.scrollHeight};
                }"""
            )
        except Exception:
            return
        if isinstance(found, dict) and found.get("ok"):
            sh = int(found.get("sh") or 0)
            if sh == last_sh and sh > 0:
                stable += 1
            else:
                stable = 0
            last_sh = sh
            # One stable paint is enough — waiting for seat-map growth burns TTL.
            if stable >= 1 or sh > 0:
                try:
                    page.wait_for_timeout(250)
                except Exception:
                    pass
                return
        try:
            page.wait_for_timeout(350)
        except Exception:
            return


def _leave_addons(page, log: list[str], *, rounds: int = 3) -> bool:
    """Outcome loop: skip extras until Choose add-ons is gone."""
    from app.step_contract import left_addons

    deadline = time.monotonic() + 28
    try:
        # Skip paints quickly; long settle burns Cleartrip's short itinerary TTL.
        settle_visible_sheet(page, budget_ms=4000)
    except Exception:
        log.append("addons_settle_err")
    for i in range(rounds):
        if time.monotonic() > deadline:
            log.append("addons_deadline")
            return False
        text, url = _read(page)
        try:
            surface = read_surface(page)
            text = apply_front_text(text, surface)
        except Exception:
            pass
        stage = classify_page(text, url, book_count=_book_count(page))
        if left_addons(text, stage):
            log.append(f"addons_left:{stage}")
            return True
        hit = _skip_addons(page)
        log.append(f"addons_skip:{hit or 'miss'}:{i}")
        try:
            # Fiber Skip navigates to contact; keep this short — itineraries expire fast.
            page.wait_for_timeout(700)
        except Exception:
            pass
        text, url = _read(page)
        stage = classify_page(text, url, book_count=_book_count(page))
        if left_addons(text, stage):
            log.append(f"addons_left:{stage}")
            return True
        # Flex upsell / meals with Skip still up: scroll and try Skip again before Continue.
        if re.search(r"skip add-?ons|add meals|fully flexible", text or "", re.I):
            try:
                page.mouse.wheel(0, 1200)
                page.wait_for_timeout(400)
            except Exception:
                pass
            hit2 = _skip_addons(page)
            log.append(f"addons_skip_scroll:{hit2 or 'miss'}:{i}")
            text, url = _read(page)
            stage = classify_page(text, url, book_count=_book_count(page))
            if left_addons(text, stage):
                log.append(f"addons_left:{stage}")
                return True
        # Skip is absent or did not move this sheet. Continue opens contact / review.
        if stage != "pay" and not re.search(r"pay to complete|enter card number|payment options", text or "", re.I):
            try:
                page.mouse.wheel(0, 900)
                page.wait_for_timeout(300)
            except Exception:
                pass
            # On Cleartrip Choose add-ons, Continue (same footer as Skip) often advances.
            moved = _js_continue(page) or _safe_continue(page)
            log.append(f"review_continue:{int(bool(moved))}:{i}")
            try:
                page.wait_for_timeout(1200)
            except Exception:
                pass
        _poll_ready(
            page,
            {"review", "passenger", "pay", "login", "otp", "error", "unavailable", "other"},
            budget_ms=4500,
        )
        text, url = _read(page)
        stage = classify_page(text, url, book_count=_book_count(page))
        if left_addons(text, stage):
            log.append(f"addons_left:{stage}")
            return True
    text, url = _read(page)
    stage = classify_page(text, url, book_count=_book_count(page))
    ok = left_addons(text, stage)
    log.append(f"addons_done:{stage}:{'ok' if ok else 'still'}")
    return ok


def _search(page, origin: str, destination: str) -> None:
    """Type into the visible From/To boxes. Setting .value alone does not update React forms."""
    for hint, value in ((r"from|origin", origin), (r"to|destination", destination)):
        try:
            box = page.get_by_placeholder(re.compile(hint, re.I)).first
            box.click(timeout=3000)
            box.fill(value, timeout=3000)
            page.wait_for_timeout(600)
            page.keyboard.press("ArrowDown")
            page.keyboard.press("Enter")
        except Exception:
            logger.info("Could not fill a %s box", hint)
    _click_label(page, "Search")


def _dismiss_overlays(page) -> None:
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
    except Exception:
        pass
    for sel in ("button[aria-label='Close']", "button[aria-label='close']"):
        try:
            page.locator(sel).first.click(timeout=800)
        except Exception:
            pass


def _ixigo_buy_this_visible(page) -> bool:
    """Paid Assured / Free Cancellation sheet sits on top of Continue."""
    try:
        return bool(
            page.evaluate(
                """() => {
                  const vis = (el) => {
                    const r = el.getBoundingClientRect();
                    const s = getComputedStyle(el);
                    return r.width > 8 && r.height > 8 && s.visibility !== 'hidden'
                      && s.display !== 'none' && r.bottom > 0 && r.top < innerHeight;
                  };
                  const label = (el) => (el.innerText || '').replace(/\\s+/g, ' ').trim();
                  const hits = [...document.querySelectorAll('button,a,[role=button],span,p,div')].filter(vis);
                  return hits.some((el) => {
                    const t = label(el);
                    return /^(buy this|secure my trip|no,?\\s*thanks)$/i.test(t);
                  });
                }"""
            )
        )
    except Exception:
        return False


def _tap_step_decline(page) -> str:
    """Click the decline label the model copied off this page. Empty when it names none."""
    try:
        text, _url = _read(page)
    except Exception:
        return ""
    from app.step_read import read_step

    heard = read_step(text, seller="ixigo", hint="")
    tap = str((heard or {}).get("tap") or "")
    if tap and _click_label(page, tap):
        return f"step_tap:{tap}"
    return ""


def _select_saved_traveller(page, name: str = "") -> str:
    """Tick one existing person on the front page. Never invent another profile.

    Returns behind_sheet when a dialog covers the list, traveller_unticked when
    people are listed but none matched, or traveller_clicked / already / card.
    """
    if read_surface(page).get("overlay"):
        return "behind_sheet"
    wanted = re.sub(r"\s+", " ", (name or "").strip()).lower()
    wanted = re.sub(r"^(mr|ms|mrs|mx)\.?\s+", "", wanted)
    try:
        picked = page.evaluate(
            """(want) => {
              const vis = (el) => {
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 4 && r.height > 4 && s.visibility !== 'hidden'
                  && s.display !== 'none' && r.bottom > 0 && r.top < innerHeight;
              };
              const shortLine = (raw) => (raw || '').split('\\n').map(s => s.replace(/\\s+/g, ' ').trim())
                .find(s => s.length >= 4 && s.length <= 60) || '';
              const isPerson = (t) => {
                const line = shortLine(t).toLowerCase();
                if (!line || line.length > 60) return false;
                if (/pay|assured|cancellation|seat|meal|baggage|continue|gst|email|mobile|contact|add new|gender|first/.test(line)) return false;
                return /\\b(mr|ms|mrs|mx)\\.?\\b/.test(line) || /^[a-z]{2,}\\s+[a-z]{2,}/.test(line);
              };
              const personLine = (el) => {
                let node = el;
                for (let i = 0; i < 6 && node; i++) {
                  const t = shortLine((node.innerText || node.getAttribute?.('aria-label') || '')).toLowerCase();
                  if (isPerson(t)) return t;
                  node = node.parentElement;
                }
                return '';
              };
              const boxes = [...document.querySelectorAll(
                'input[type=checkbox],[role=checkbox],input[type=radio]'
              )].filter(vis);
              const adults = boxes.filter((el) => isPerson(personLine(el)));
              const named = want
                ? adults.filter((el) => personLine(el).includes(want))
                : adults;
              const pool = named.length ? named : adults;
              const alreadyOn = pool.find((el) => el.checked || el.getAttribute('aria-checked') === 'true');
              if (alreadyOn) return 'already:' + adults.length;
              const match = pool[0];
              if (!match && adults.length) return 'unticked:' + adults.length;
              if (match) {
                const lab = match.closest('label') || match;
                lab.click();
                if (!match.checked && match.getAttribute('aria-checked') !== 'true') match.click();
                return 'clicked:' + adults.length;
              }
              const cards = [...document.querySelectorAll('label,button,div,li,span')].filter(vis);
              const card = cards.find((el) => {
                const t = shortLine(el.innerText || '').toLowerCase();
                if (!isPerson(t)) return false;
                if ((el.innerText || '').length > 80) return false;
                return !want || t.includes(want);
              });
              if (card) { card.click(); return 'card:' + adults.length; }
              return adults.length ? 'unticked:' + adults.length : '';
            }""",
            wanted,
        )
        if picked:
            return f"traveller_{picked}"
    except Exception:
        return ""
    return ""


def _delete_saved_traveller(page, name: str = "") -> str:
    """Remove one saved traveller chip/row when the UI exposes a delete control."""
    if read_surface(page).get("overlay"):
        return "behind_sheet"
    wanted = re.sub(r"\s+", " ", (name or "").strip()).lower()
    wanted = re.sub(r"^(mr|ms|mrs|mx)\.?\s+", "", wanted)
    try:
        result = page.evaluate(
            """(want) => {
              const vis = (el) => {
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 2 && r.height > 2 && s.visibility !== 'hidden'
                  && s.display !== 'none' && r.bottom > 0 && r.top < innerHeight;
              };
              const shortLine = (raw) => (raw || '').split('\\n').map(s => s.replace(/\\s+/g, ' ').trim())
                .find(s => s.length >= 4 && s.length <= 60) || '';
              const isPerson = (t) => {
                const line = shortLine(t).toLowerCase();
                if (!line) return false;
                return /\\b(mr|ms|mrs|mx)\\.?\\b/.test(line);
              };
              const nodes = [...document.querySelectorAll('button,a,[role=button],svg,i,span,div')].filter(vis);
              const dels = nodes.filter((el) => {
                const aria = (el.getAttribute('aria-label') || el.getAttribute('title') || '').toLowerCase();
                const t = (el.innerText || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                if (/delete|remove|trash/.test(aria)) return true;
                if (/^delete$|^remove$|✕|×|🗑/.test(t) && t.length < 12) return true;
                return false;
              });
              const nearPerson = (del) => {
                let node = del;
                for (let i = 0; i < 8 && node; i++) {
                  const line = shortLine(node.innerText || '').toLowerCase();
                  if (isPerson(line)) return line;
                  node = node.parentElement;
                }
                return '';
              };
              const pool = want
                ? dels.filter((el) => nearPerson(el).includes(want))
                : dels.filter((el) => nearPerson(el));
              const hit = pool[0] || dels[0];
              if (!hit) return '';
              hit.click();
              return 'clicked';
            }""",
            wanted,
        )
        if result == "clicked":
            try:
                page.wait_for_timeout(400)
            except Exception:
                pass
            # Confirm delete dialog if present.
            for label in ("Delete", "Remove", "Yes", "Confirm"):
                if _click_label(page, label):
                    return f"traveller_deleted:{label}"
            return "traveller_deleted"
    except Exception:
        return ""
    return "traveller_delete_miss"


def _ixigo_opt_out_cancellation(page) -> str:
    try:
        page.locator("#standalone-none-fareType").check(timeout=2500)
        return "optout_radio"
    except Exception:
        pass
    try:
        page.get_by_text(re.compile(r"don.?t want Free Cancellation", re.I)).first.click(timeout=2500)
        return "optout_text"
    except Exception:
        pass
    for pattern in (r"^no,?\s*thanks$", r"^no thanks$"):
        try:
            page.get_by_text(re.compile(pattern, re.I)).first.click(timeout=2500)
            return "optout_no_thanks"
        except Exception:
            pass
    try:
        clicked = page.evaluate(
            """() => {
              const vis = (el) => {
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 8 && r.height > 8 && s.visibility !== 'hidden'
                  && s.display !== 'none' && r.bottom > 0 && r.top < innerHeight;
              };
              const label = (el) => (el.innerText || el.getAttribute('aria-label') || '')
                .replace(/\\s+/g, ' ').trim();
              const nodes = [...document.querySelectorAll('label, div, span, button, a, p, [role=button]')].filter(vis);
              const skip = nodes.find((el) => {
                const text = label(el);
                if (!text || text.length > 90) return false;
                if (/^buy this$|^secure my trip$/i.test(text) || /\\bpay\\b/i.test(text)) return false;
                return /don.?t want free cancellation|no,? thanks|i don.?t want|continue without|skip (add-?on|this)|not now|maybe later|^none$|^no$/i.test(text);
              });
              if (skip) { skip.click(); return 'skip'; }
              const none = document.querySelector('#standalone-none-fareType');
              if (none) { none.click(); return 'radio'; }
              const close = nodes.find((el) => {
                const aria = (el.getAttribute('aria-label') || '');
                const r = el.getBoundingClientRect();
                return /close|dismiss/i.test(aria) && r.y > 60 && r.width < 64 && r.height < 64;
              });
              if (close) { close.click(); return 'close'; }
              return '';
            }"""
        )
        if clicked:
            return f"optout_js:{clicked}"
    except Exception:
        pass
    return "optout_miss"


def _decline_cleartrip_protect(page) -> str:
    """Cleartrip Trip Protect sits above Continue — decline it or Continue is a no-op."""
    try:
        hit = page.evaluate(
            """() => {
              const nodes = [...document.querySelectorAll('button,a,[role=button]')];
              const el = nodes.find(n => {
                const t = (n.innerText || n.getAttribute('aria-label') || '')
                  .replace(/\\s+/g, ' ').trim();
                return t.length > 8 && t.length < 120 && (
                  /no,?\\s*i will continue without/i.test(t)
                  || /continue without(?:\\s+trip)?\\s+protect/i.test(t)
                  || /without trip protect/i.test(t)
                );
              });
              if (!el) return '';
              el.scrollIntoView({block: 'center', inline: 'nearest'});
              el.click();
              return (el.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 80);
            }"""
        )
        return str(hit or "")
    except Exception:
        return ""


def _js_continue(page) -> bool:
    """Click Continue/Proceed/Confirm on a real button. Buttons only — no span/div scan.

    Cleartrip review uses "Continue with 1-step checkout"; traveller uses
    "Continue to payment". Both advance without charging. Refuse Pay now only.
    """
    try:
        hit = page.evaluate(
            """() => {
              const payOnly = /^(pay now|pay to complete|make payment|complete payment|pay securely)$/i;
              const isForward = (t) => {
                const s = String(t || '').replace(/\\s+/g, ' ').trim();
                if (!s || s.length > 56 || payOnly.test(s)) return false;
                if (/^continue\\b/i.test(s)) return true;
                return /^(proceed|confirm)$/i.test(s);
              };
              const rank = (t, travellerReady) => {
                const s = String(t || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                if (/continue with 1-?step checkout/i.test(s)) return 0;
                if (s === 'continue to payment') return travellerReady ? 0 : 4;
                if (s === 'continue' || s === 'proceed' || s === 'confirm') return 1;
                if (/^continue without\\b/i.test(s)) return 3;
                if (/^continue\\b/i.test(s)) return 2;
                return 5;
              };
              const nodes = [...document.querySelectorAll('button,a,[role=button]')];
              const hits = [];
              const fakeEvt = (el) => ({
                preventDefault() {}, stopPropagation() {},
                nativeEvent: { isTrusted: true }, target: el, currentTarget: el,
                type: 'click', bubbles: true, cancelable: true,
              });
              const fire = (el) => {
                const fiberKey = Object.keys(el).find(k => k.startsWith('__reactFiber$'));
                let fiber = fiberKey ? el[fiberKey] : null;
                for (let i = 0; i < 12 && fiber; i++) {
                  const props = fiber.memoizedProps || fiber.pendingProps;
                  if (props && typeof props.onClick === 'function') {
                    props.onClick(fakeEvt(el));
                    return true;
                  }
                  fiber = fiber.return;
                }
                el.click();
                return true;
              };
              for (const el of nodes) {
                const text = (el.innerText || el.getAttribute('aria-label') || '')
                  .replace(/\\s+/g, ' ').trim();
                if (!isForward(text)) continue;
                if (el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
                const style = window.getComputedStyle(el);
                if (style.visibility === 'hidden' || style.display === 'none') continue;
                const rect = el.getBoundingClientRect();
                if (rect.width < 40 || rect.height < 24) continue;
                hits.push(el);
              }
              if (!hits.length) return false;
              // Prefer Continue to payment only once traveller name fields exist.
              // On the contact step CTP is already in the DOM as step 4 — clicking it
              // early no-ops / burns the hold before names are filled.
              const travellerReady = [...document.querySelectorAll('label,h4,p')].some(el => {
                const t = (el.innerText || '').replace(/\\s+/g, ' ').trim();
                return t.length < 48 && /first\\s*&\\s*middle\\s*name|traveller name|last\\s*name/i.test(t);
              });
              hits.sort((a, b) => {
                const ta = (a.innerText || '').replace(/\\s+/g, ' ').trim();
                const tb = (b.innerText || '').replace(/\\s+/g, ' ').trim();
                const pa = rank(ta, travellerReady);
                const pb = rank(tb, travellerReady);
                if (pa !== pb) return pa - pb;
                const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
                return (rb.top - ra.top) || ((ra.width * ra.height) - (rb.width * rb.height));
              });
              const btn = hits[0];
              btn.scrollIntoView({block: 'center', inline: 'nearest'});
              return fire(btn);
            }"""
        )
        return bool(hit)
    except Exception:
        return False


def _safe_continue(page) -> bool:
    """One click on the visible Continue, Confirm, or Proceed control.

    Prefer a button JS click first — scanning every div/span on Cleartrip stalls Kernel.
    Buy This is an add-on purchase, not Continue, and it covers the real button.
    """
    if _ixigo_buy_this_visible(page):
        return False
    if _js_continue(page):
        return True
    try:
        point = page.evaluate(
            """() => {
              const payOnly = /^(pay now|pay to complete|make payment|complete payment|pay securely)$/i;
              const isForward = (t) => {
                const s = String(t || '').replace(/\\s+/g, ' ').trim();
                if (!s || s.length > 56 || payOnly.test(s)) return false;
                if (/^continue\\b/i.test(s)) return true;
                return /^(proceed|confirm)$/i.test(s);
              };
              const nodes = [...document.querySelectorAll('button, a, [role=button]')];
              const hits = [];
              for (const el of nodes) {
                const own = [...el.childNodes]
                  .filter(n => n.nodeType === 3)
                  .map(n => n.textContent || '')
                  .join(' ')
                  .replace(/\\s+/g, ' ')
                  .trim();
                const text = (el.innerText || '').replace(/\\s+/g, ' ').trim();
                const label = own || (text.length <= 56 ? text : '');
                if (!isForward(label) && !isForward(text)) continue;
                if (payOnly.test(text)) continue;
                const style = window.getComputedStyle(el);
                if (style.visibility === 'hidden' || style.display === 'none' || style.pointerEvents === 'none') continue;
                const rect = el.getBoundingClientRect();
                if (rect.width < 48 || rect.height < 28 || rect.width > 520) continue;
                hits.push(el);
              }
              if (!hits.length) return null;
              hits.sort((a, b) => {
                const ra = a.getBoundingClientRect();
                const rb = b.getBoundingClientRect();
                const areaA = ra.width * ra.height;
                const areaB = rb.width * rb.height;
                if (Math.abs(areaA - areaB) > 400) return areaA - areaB;
                return rb.top - ra.top;
              });
              const btn = hits[0];
              btn.scrollIntoView({block: 'center', inline: 'center'});
              const r = btn.getBoundingClientRect();
              const x = Math.round(r.x + r.width / 2);
              const y = Math.round(r.y + r.height / 2);
              if (x < 1 || y < 1 || x >= window.innerWidth || y >= window.innerHeight) return null;
              const top = document.elementFromPoint(x, y);
              if (!top || (top !== btn && !btn.contains(top) && !top.contains(btn))) return null;
              return {x, y};
            }"""
        )
    except Exception:
        point = None
    if isinstance(point, dict) and point.get("x") is not None and point.get("y") is not None:
        try:
            page.mouse.click(float(point["x"]), float(point["y"]))
            return True
        except Exception:
            pass
    try:
        btn = page.get_by_role(
            "button",
            name=re.compile(r"^(Continue\b.*|Confirm|Proceed)$", re.I),
        ).last
        label = (btn.inner_text(timeout=2000) or "").strip()
        if re.search(r"\bpay\b", label, re.I) and not re.search(r"continue to payment", label, re.I):
            return False
        box = btn.bounding_box()
        if box and box.get("width", 0) > 8 and box.get("height", 0) > 8:
            page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            return True
        btn.click(timeout=5000)
        return True
    except Exception:
        return _click_label(page, "Continue")


def parse_login_phone(phone: str) -> tuple[str, str]:
    """Return (country_dial_code, national_number) from +E.164 or local digits."""
    digits = re.sub(r"\D", "", phone or "")
    if digits.startswith("33") and len(digits) >= 11:
        return "33", digits[2:].lstrip("0")
    if digits.startswith("91") and len(digits) >= 12:
        return "91", digits[-10:]
    if len(digits) == 10:
        return "91", digits
    if len(digits) == 9 and digits.startswith("7"):
        # French mobile without country code (e.g. 759341005).
        return "33", digits
    return "", digits


def _set_ixigo_dial_code(page, dial: str) -> str:
    """Switch the login country code when it is not already selected."""
    if not dial:
        return "dial_skip"
    try:
        current = page.evaluate(
            """() => {
                const t = document.body.innerText || '';
                const m = t.match(/\\+\\d{1,3}/);
                return m ? m[0] : '';
            }"""
        )
        if current and current.replace("+", "") == dial:
            return "dial_ok"
    except Exception:
        pass
    try:
        page.get_by_text(re.compile(r"^\+\d{1,3}$")).first.click(timeout=2500)
        page.wait_for_timeout(400)
        page.get_by_text(re.compile(rf"^\+{re.escape(dial)}\b|{re.escape(dial)}\b")).first.click(timeout=3000)
        page.wait_for_timeout(400)
        return "dial_set"
    except Exception:
        try:
            page.locator("div,button,span").filter(has_text=re.compile(r"^\+91$")).first.click(timeout=2000)
            page.wait_for_timeout(300)
            box = page.get_by_placeholder(re.compile(r"search|country|code", re.I))
            if box.count():
                box.first.fill(dial, timeout=2000)
                page.wait_for_timeout(400)
            page.get_by_text(re.compile(rf"\+{re.escape(dial)}\b")).first.click(timeout=3000)
            return "dial_search_set"
        except Exception:
            return "dial_miss"


def install_ixigo_otpless_callback_bridge(page) -> str:
    """Capture Ixigo's OTPless merchant callback at `new OTPless(cb)` time.

    The SDK closes over `cb` and does not expose it on OTPlessSignin, so a blind
    postMessage rebroadcast cannot run Ixigo's session exchange. We patch the
    constructor early and keep only a function reference (no secrets).

    Set IXIGO_OTPLESS_BRIDGE=0 for passive tests that must not wrap OTPless.
    """
    if (os.getenv("IXIGO_OTPLESS_BRIDGE") or "1").strip() in {"0", "false", "no"}:
        return "bridge_disabled"
    try:
        return str(
            page.evaluate(
                """() => {
                    if (window.__guideOtplessBridge && window.__guideOtplessBridge.patched) {
                        return 'bridge_already';
                    }
                    window.__guideOtplessBridge = { patched: false, hasCb: false, cb: null };
                    const patch = () => {
                        const Orig = window.OTPless;
                        if (!Orig || Orig.__guidePatched) return !!Orig;
                        function Wrapped(cb) {
                            try {
                                if (typeof cb === 'function') {
                                    window.__guideOtplessBridge.cb = cb;
                                    window.__guideOtplessBridge.hasCb = true;
                                }
                            } catch (e) {}
                            return new Orig(cb);
                        }
                        Wrapped.__guidePatched = true;
                        try {
                            Object.setPrototypeOf(Wrapped, Orig);
                            Object.getOwnPropertyNames(Orig).forEach((k) => {
                                try { Wrapped[k] = Orig[k]; } catch (e) {}
                            });
                        } catch (e) {}
                        window.OTPless = Wrapped;
                        window.__guideOtplessBridge.patched = true;
                        return true;
                    };
                    if (patch()) return 'bridge_patched';
                    const iv = setInterval(() => { if (patch()) clearInterval(iv); }, 150);
                    setTimeout(() => clearInterval(iv), 45000);
                    return 'bridge_waiting';
                }"""
            )
            or "bridge_eval_empty"
        )
    except Exception as exc:
        return f"bridge_fail:{type(exc).__name__}"


def install_ixigo_otpless_tap(page) -> dict:
    """Watch OTPless verify responses.

    Holds the SUCCESS token only in memory for a one-shot ONETAP rebroadcast.
    Never logs OTP, phone, cookies, or the token.
    """
    state: dict = {
        "success": False,
        "status": None,
        "events": [],
        "_onetap_token": None,  # ephemeral; cleared after rebroadcast
        "bridge": install_ixigo_otpless_callback_bridge(page),
    }

    def on_response(resp) -> None:
        try:
            url = resp.url or ""
            if "otpless.app" not in url or "/transaction/otp" not in url:
                return
            if resp.request.method.upper() != "POST":
                return
            body = ""
            try:
                body = resp.text() or ""
            except Exception:
                body = ""
            ok = resp.status == 200 and bool(re.search(r'"status"\s*:\s*"SUCCESS"', body))
            state["status"] = resp.status
            state["success"] = bool(ok)
            state["events"].append({"http_status": resp.status, "success": bool(ok), "url_path": "/transaction/otp"})
            if ok:
                # Prefer authDetail.token; never print.
                m = re.search(r'"authDetail"\s*:\s*\{[^}]*?"token"\s*:\s*"([^"]+)"', body) or re.search(
                    r'"token"\s*:\s*"([^"]+)"', body
                )
                if m:
                    state["_onetap_token"] = m.group(1)
        except Exception:
            pass

    page.on("response", on_response)
    # Re-attempt bridge on navigations (booking shell loads OTPless late).
    try:
        page.on("framenavigated", lambda _f: install_ixigo_otpless_callback_bridge(page))
    except Exception:
        pass
    return state


def _rebroadcast_otpless_onetap(page, otpless_state: dict) -> str:
    """Re-fire ONETAP via the captured merchant callback (Ixigo session exchange)."""
    token = otpless_state.pop("_onetap_token", None) if otpless_state else None
    if not token:
        return "onetap_rebroadcast_skip"
    try:
        # Ensure bridge exists even if OTPless loaded after first install.
        install_ixigo_otpless_callback_bridge(page)
        result = page.evaluate(
            """(token) => {
                const payload = {
                    responseType: 'ONETAP',
                    statusCode: 200,
                    response: { token, status: 'SUCCESS' },
                };
                const bridge = window.__guideOtplessBridge || {};
                let via = 'none';
                if (typeof bridge.cb === 'function') {
                    try { bridge.cb(payload); via = 'merchant_cb'; } catch (e) { via = 'merchant_cb_throw'; }
                }
                // Fallback noise only if bridge missed construction.
                if (via === 'none') {
                    try { window.postMessage(payload, window.location.origin); via = 'postMessage'; } catch (e) {}
                }
                return { via, hasCb: !!bridge.hasCb, patched: !!bridge.patched };
            }""",
            token,
        )
        via = (result or {}).get("via")
        has_cb = (result or {}).get("hasCb")
        return f"onetap_rebroadcast_ok:via={via}:hasCb={has_cb}"
    except Exception as exc:
        return f"onetap_rebroadcast_fail:{type(exc).__name__}"
    finally:
        token = None


def complete_ixigo_login_after_otp(page, *, otpless_state: dict | None = None) -> str:
    """Finish Ixigo session handoff after OTPless SUCCESS when the OTP sheet sticks.

    Root cause (observed): OTPless returns HTTP 200 authDetail.status=SUCCESS, then
    SDK_SESSION_END, but Ixigo never issues a session API and keeps loginVisible=true.
    Cloudflare Turnstile postMessage/sandbox console noise often accompanies the stuck UI.
    """
    notes: list[str] = []
    ready = {"passenger", "addons", "review", "pay"}
    text, url, stage, _facts = _poll_ready(page, ready, budget_ms=8000)
    if stage in ready or stage not in {"otp", "login"}:
        return f"handoff_natural:{stage}"

    if otpless_state and not otpless_state.get("success"):
        notes.append("otpless_success_not_seen")
    else:
        notes.append("otpless_success_seen" if (otpless_state or {}).get("success") else "otpless_unknown")

    # Re-deliver ONETAP once — Ixigo's listener may have missed the SDK callback.
    if otpless_state and otpless_state.get("success"):
        notes.append(_rebroadcast_otpless_onetap(page, otpless_state))
        text, url, stage, _facts = _poll_ready(page, ready, budget_ms=10000)
        if stage in ready or stage not in {"otp", "login"}:
            notes.append(f"rebroadcast_cleared:{stage}")
            return ";".join(notes)

    # Snapshot non-secret page hooks (names only).
    try:
        probe = page.evaluate(
            """() => {
                const nameOk = (k) => /otp|auth|token|user|login|session|ixigo/i.test(k);
                const ls = Object.keys(localStorage || {}).filter(nameOk).slice(0, 20);
                const ss = Object.keys(sessionStorage || {}).filter(nameOk).slice(0, 20);
                const cookies = (document.cookie || '').split(';').map(c => c.split('=')[0].trim()).filter(Boolean).slice(0, 30);
                const win = Object.keys(window).filter(k => /otpless|OTPless|ixigoAuth|loginSDK/i.test(k)).slice(0, 15);
                return {
                  ls, ss, cookie_names: cookies, win_keys: win,
                  loginVisible: /loginVisible=true/i.test(location.href),
                  hasVerify: /verify your mobile|enter otp/i.test(document.body.innerText || '')
                };
            }"""
        )
        notes.append(
            "probe:"
            + json.dumps(
                {
                    "ls": (probe or {}).get("ls"),
                    "ss": (probe or {}).get("ss"),
                    "cookie_count": len((probe or {}).get("cookie_names") or []),
                    "win_keys": (probe or {}).get("win_keys"),
                    "loginVisible": (probe or {}).get("loginVisible"),
                }
            )
        )
    except Exception as exc:
        notes.append(f"probe_fail:{type(exc).__name__}")
        probe = {}

    # Try closing the OTP drawer — session may already be established underneath.
    for _ in range(2):
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(400)
        except Exception:
            break
        text, url, stage, _facts = _snapshot_stage(page)
        if stage in ready or stage not in {"otp", "login"}:
            notes.append(f"escape_cleared:{stage}")
            return ";".join(notes)

    # Soft-reload booking URL without loginVisible so Ixigo re-reads session cookies.
    # Only treat as cleared if passenger fields appear (review alone can still be logged-out).
    try:
        cur = str(page.url or "")
        if "loginVisible=true" in cur or (probe or {}).get("loginVisible"):
            cleaned = re.sub(r"([?&])loginVisible=true&?", r"\1", cur)
            cleaned = cleaned.replace("?&", "?").rstrip("?&")
            page.goto(cleaned or cur, wait_until="domcontentloaded", timeout=60000)
            notes.append("reloaded_without_loginVisible")
            text, url, stage, _facts = _poll_ready(
                page, {"passenger", "addons", "pay", "login", "otp", "error"}, budget_ms=12000
            )
            if stage == "passenger" or (
                stage == "review"
                and re.search(r"first name|last name|date of birth|add adult", text or "", re.I)
            ):
                notes.append(f"reload_cleared:{stage}")
                return ";".join(notes)
            if stage in {"login", "otp"}:
                notes.append(f"reload_still_auth:{stage}")
            else:
                notes.append(f"reload_stage:{stage}")
    except Exception as exc:
        notes.append(f"reload_fail:{type(exc).__name__}")

    # Last resort: click a close control on the sheet if present.
    for label in ("Close", "×", "Got it"):
        if _click_label(page, label):
            notes.append(f"clicked:{label}")
            text, url, stage, _facts = _poll_ready(page, ready | {"login", "otp"}, budget_ms=4000)
            if stage in ready or stage not in {"otp", "login"}:
                notes.append(f"close_cleared:{stage}")
                return ";".join(notes)

    text, url, stage, _facts = _snapshot_stage(page)
    notes.append(f"still:{stage}")
    return ";".join(notes)


def _snapshot_stage(page) -> tuple[str, str, str, dict[str, str]]:
    t, u = _read(page)
    s = classify_page(t, u, book_count=_book_count(page))
    f = extract_trip_facts(t, u)
    return t, u, s, f


def _fill_ixigo_login_phone(page, phone: str) -> str:
    dial, national = parse_login_phone(phone)
    if not national or len(national) < 8:
        return "bad_phone"
    dial_note = _set_ixigo_dial_code(page, dial)

    def _type(locator) -> None:
        locator.click(timeout=3000)
        locator.fill("")
        locator.fill(national, timeout=3500)

    for attempt in (
        lambda: _type(page.get_by_placeholder(re.compile(r"Enter Mobile Number", re.I)).last),
        lambda: _type(page.get_by_placeholder(re.compile(r"mobile|phone", re.I)).last),
        lambda: _type(page.locator("input[type='tel']").last),
        lambda: _type(page.locator("input[maxlength='10'], input[maxlength='15']").last),
    ):
        try:
            attempt()
            return f"filled_mobile:{dial_note}"
        except Exception:
            continue
    return f"mobile_miss:{dial_note}"


def _field_boxes(page) -> list[dict[str, str]]:
    """Each visible input with the short wrapper text Ixigo uses as its label."""
    try:
        rows = page.evaluate(
            """() => {
              const vis = (el) => {
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 4 && r.height > 4 && s.visibility !== 'hidden' && s.display !== 'none'
                  && s.opacity !== '0';
              };
              const labelOf = (el) => {
                let node = el;
                for (let i = 0; i < 4 && node.parentElement; i++) {
                  node = node.parentElement;
                  const text = (node.innerText || '').replace(/\\s+/g, ' ').trim();
                  if (text && text.length < 80) return text;
                }
                return '';
              };
              const nodes = [...document.querySelectorAll(
                'input, textarea, [contenteditable="true"], [role="textbox"]'
              )];
              return nodes.filter(el => {
                if (!vis(el)) return false;
                const type = (el.getAttribute('type') || '').toLowerCase();
                if (/hidden|radio|checkbox|submit|button/.test(type)) return false;
                return true;
              }).map((el, index) => {
                const r = el.getBoundingClientRect();
                return {
                  index: String(index),
                  label: labelOf(el),
                  placeholder: el.getAttribute('placeholder') || '',
                  type: (el.getAttribute('type') || (el.isContentEditable ? 'text' : '')).toLowerCase(),
                  name: (el.getAttribute('aria-label') || el.getAttribute('name') || '').slice(0, 80),
                  value: (el.value != null ? el.value : (el.innerText || '')).slice(0, 120),
                  x: r.x,
                  y: r.y,
                };
              });
            }"""
        )
    except Exception:
        return []
    return [row for row in (rows or []) if isinstance(row, dict)]


def _wake_cleartrip_mobile(page) -> None:
    """Cleartrip often hides the national box until the +91 row is focused."""
    try:
        page.evaluate(
            """() => {
              const vis = (el) => {
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 2 && r.height > 2 && s.visibility !== 'hidden' && s.display !== 'none';
              };
              const inputs = [...document.querySelectorAll('input')].filter(vis);
              const prefix = inputs.find((el) => /^\\+?91$/.test((el.value || '').trim()));
              const row = [...document.querySelectorAll('div,span,label')].find((el) => {
                const t = (el.innerText || '').replace(/\\s+/g, ' ').trim();
                return t === '+91' || /^\\+91\\s*$/.test(t);
              });
              const hit = prefix || row;
              if (!hit) return false;
              hit.scrollIntoView({block: 'center'});
              hit.click();
              return true;
            }"""
        )
        page.wait_for_timeout(200)
        page.keyboard.press("Tab")
        page.wait_for_timeout(300)
    except Exception:
        pass


def _type_phone_after_prefix(page, digits: str) -> bool:
    """When no separate mobile input is listed, click right of +91 and type the 10 digits."""
    national = re.sub(r"\D", "", digits or "")[-10:]
    if len(national) != 10:
        return False
    _wake_cleartrip_mobile(page)
    boxes = _field_boxes(page)
    phone = _phone_box(boxes)
    if phone is not None and not _is_email_box(phone):
        return _set_box(page, int(phone["index"]), national) == "ok"
    try:
        # Only type into the input to the right of +91 — never the email box.
        placed = page.evaluate(
            """(national) => {
              const vis = (el) => {
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 4 && r.height > 4 && s.visibility !== 'hidden' && s.display !== 'none';
              };
              const deep = [];
              const walk = (root) => {
                if (!root) return;
                if (root.querySelectorAll) {
                  for (const el of root.querySelectorAll('input')) deep.push(el);
                }
                const all = root.querySelectorAll ? root.querySelectorAll('*') : [];
                for (const el of all) {
                  if (el.shadowRoot) walk(el.shadowRoot);
                }
              };
              walk(document);
              const inputs = deep.filter(vis);
              const prefix = inputs.find((el) => /^\\+?91$/.test((el.value || '').trim()));
              if (!prefix) return {ok: false, reason: 'no-prefix'};
              const pr = prefix.getBoundingClientRect();
              const next = inputs
                .filter((el) => {
                  if (el === prefix) return false;
                  const r = el.getBoundingClientRect();
                  const type = (el.type || '').toLowerCase();
                  const ph = (el.placeholder || '') + (el.name || '') + (el.getAttribute('aria-label') || '');
                  if (type === 'email' || /e-?mail/i.test(ph)) return false;
                  return Math.abs(r.top - pr.top) < 50 && r.left >= pr.left - 4;
                })
                .sort((a, b) => a.getBoundingClientRect().left - b.getBoundingClientRect().left)[0];
              if (next) {
                next.focus();
                next.click();
                const proto = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');
                if (proto && proto.set) proto.set.call(next, national);
                else next.value = national;
                next.dispatchEvent(new InputEvent('input', {bubbles: true, data: national, inputType: 'insertFromPaste'}));
                next.dispatchEvent(new Event('change', {bubbles: true}));
                return {ok: (next.value || '').replace(/\\D/g, '').includes(national), x: 0, y: 0};
              }
              // No neighbour input in the tree — return click point to the right of +91.
              return {ok: false, x: pr.right + 36, y: pr.top + pr.height / 2};
            }""",
            national,
        )
        if isinstance(placed, dict) and placed.get("ok"):
            return True
        if isinstance(placed, dict) and placed.get("x"):
            page.mouse.click(float(placed["x"]), float(placed["y"]))
            page.wait_for_timeout(150)
            page.keyboard.press("Control+A")
            page.keyboard.insert_text(national)
            page.wait_for_timeout(250)
            # Accept if any non-email input now holds the digits.
            for row in _field_boxes(page):
                if _is_email_box(row) or row.get("type") == "email":
                    continue
                if _value_stuck(str(row.get("value") or ""), national, "phone"):
                    return True
            # Also check shadow inputs.
            got = page.evaluate(
                """(national) => {
                  const deep = [];
                  const walk = (root) => {
                    if (!root || !root.querySelectorAll) return;
                    for (const el of root.querySelectorAll('input')) deep.push(el);
                    for (const el of root.querySelectorAll('*')) {
                      if (el.shadowRoot) walk(el.shadowRoot);
                    }
                  };
                  walk(document);
                  return deep.some((el) => {
                    const type = (el.type || '').toLowerCase();
                    if (type === 'email') return false;
                    return (el.value || '').replace(/\\D/g, '').includes(national);
                  });
                }""",
                national,
            )
            return bool(got)
        return False
    except Exception:
        return False


def _box_keeps(got: str, expected: str) -> bool:
    """True when the input kept what we typed. Phone allows formatting spaces."""
    want = (expected or "").strip()
    have = (got or "").strip()
    if not want:
        return False
    if want == have or want.lower() in have.lower():
        return True
    digits = re.sub(r"\D", "", want)
    if len(digits) >= 10:
        return digits[-10:] in re.sub(r"\D", "", have)
    return False


def _set_box(page, index: int, value: str) -> str:
    """Set one input by its position. ok only when that box keeps the text."""
    try:
        stuck = page.evaluate(
            """({index, value}) => {
              const vis = (el) => {
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 4 && r.height > 4 && s.visibility !== 'hidden' && s.display !== 'none'
                  && s.opacity !== '0';
              };
              const inputs = [...document.querySelectorAll(
                'input, textarea, [contenteditable="true"], [role="textbox"]'
              )].filter(el => {
                if (!vis(el)) return false;
                const type = (el.getAttribute('type') || '').toLowerCase();
                return !/hidden|radio|checkbox|submit|button/.test(type);
              });
              const el = inputs[index];
              if (!el) return {status: 'gone', value: ''};
              el.scrollIntoView({block: 'center'});
              el.focus();
              el.click();
              if (el.isContentEditable) {
                el.textContent = value;
                el.dispatchEvent(new InputEvent('input', {bubbles: true, data: value, inputType: 'insertFromPaste'}));
                return {status: 'set', value: el.innerText || ''};
              }
              const proto = el instanceof HTMLTextAreaElement
                ? Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value')
                : Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');
              if (proto && proto.set) proto.set.call(el, '');
              else el.value = '';
              if (proto && proto.set) proto.set.call(el, value);
              else el.value = value;
              el.dispatchEvent(new InputEvent('input', {bubbles: true, cancelable: true, data: value, inputType: 'insertFromPaste'}));
              el.dispatchEvent(new Event('input', {bubbles: true}));
              el.dispatchEvent(new Event('change', {bubbles: true}));
              el.dispatchEvent(new Event('blur', {bubbles: true}));
              return {status: 'set', value: el.value || ''};
            }""",
            {"index": index, "value": value},
        )
    except Exception:
        stuck = None
    if isinstance(stuck, dict):
        if stuck.get("status") == "gone":
            return "miss"
        if _box_keeps(str(stuck.get("value") or ""), value):
            return "ok"
    try:
        page.evaluate(
            """(index) => {
              const vis = (el) => {
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 4 && r.height > 4 && s.visibility !== 'hidden' && s.display !== 'none'
                  && s.opacity !== '0';
              };
              const inputs = [...document.querySelectorAll(
                'input, textarea, [contenteditable="true"], [role="textbox"]'
              )].filter(el => {
                if (!vis(el)) return false;
                const type = (el.getAttribute('type') || '').toLowerCase();
                return !/hidden|radio|checkbox|submit|button/.test(type);
              });
              document.querySelectorAll('[data-guide-fill]').forEach(el => el.removeAttribute('data-guide-fill'));
              if (inputs[index]) inputs[index].setAttribute('data-guide-fill', '1');
            }""",
            index,
        )
        box = page.locator("[data-guide-fill='1']")
        box.click(timeout=2000)
        box.fill("")
        box.type(value, delay=25)
        page.wait_for_timeout(200)
        got = (box.input_value(timeout=1500) or "").strip()
        if _box_keeps(got, value):
            return "ok"
        page.keyboard.press("Control+A")
        page.keyboard.insert_text(value)
        page.wait_for_timeout(200)
        got = (box.input_value(timeout=1500) or "").strip()
        return "ok" if _box_keeps(got, value) else "miss"
    except Exception:
        return "miss"


def _type_near_label(page, label_re: str, value: str) -> str:
    """Fill the input nearest a visible label. Cleartrip labels often lack htmlFor."""
    if not value:
        return "skip"
    try:
        stuck = page.evaluate(
            """({labelRe, value}) => {
              const re = new RegExp(labelRe, 'i');
              const vis = (el) => {
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 4 && r.height > 4 && s.visibility !== 'hidden'
                  && s.display !== 'none' && s.opacity !== '0';
              };
              // Exact-ish short labels only — avoid home-page / fat wrappers.
              // No span scan — Cleartrip seat maps make span queries hang Kernel CDP.
              const labs = [...document.querySelectorAll('label,h4,h3,p')]
                .filter(vis)
                .filter(el => {
                  const own = [...el.childNodes]
                    .filter(n => n.nodeType === 3)
                    .map(n => n.textContent || '')
                    .join(' ')
                    .replace(/\\s+/g, ' ')
                    .trim();
                  const t = own || (el.innerText || '').replace(/\\s+/g, ' ').trim();
                  return t.length > 3 && t.length < 48 && re.test(t);
                });
              labs.sort((a, b) => {
                const ta = (a.innerText || '').replace(/\\s+/g, ' ').trim().length;
                const tb = (b.innerText || '').replace(/\\s+/g, ' ').trim().length;
                return ta - tb;
              });
              const lab = labs[0];
              if (!lab) return {status: 'none'};
              const lr = lab.getBoundingClientRect();
              const inputs = [...document.querySelectorAll('input,textarea')]
                .filter(vis)
                .filter(el => {
                  const type = (el.getAttribute('type') || '').toLowerCase();
                  return !/hidden|radio|checkbox|submit|button|email|tel/.test(type);
                });
              let el = inputs.find(inp => lab.contains(inp));
              if (!el) {
                const wantLast = /last|surname/i.test(String(labelRe || ''));
                const wantFirst = /first|given|middle|traveller/i.test(String(labelRe || ''));
                const scored = inputs.map(inp => {
                  const r = inp.getBoundingClientRect();
                  const dy = Math.abs(r.top - lr.top);
                  if (dy > 56) return {el: inp, score: 99999};
                  const gap = Math.abs((r.left + r.width / 2) - (lr.left + lr.width / 2));
                  let score = dy * 20 + gap;
                  if (wantLast && r.left < lr.left - 40) score += 800;
                  if (wantFirst && r.left > lr.right + 40) score += 800;
                  return {el: inp, score};
                }).filter(x => x.score < 99999)
                  .sort((a, b) => a.score - b.score);
                el = scored.length ? scored[0].el : null;
              }
              if (!el) return {status: 'none'};
              el.scrollIntoView({block: 'center'});
              const r = el.getBoundingClientRect();
              return {
                status: 'aim',
                x: r.x + r.width / 2,
                y: r.y + r.height / 2,
                label: (lab.innerText || '').trim().slice(0, 60),
              };
            }""",
            {"labelRe": label_re, "value": value},
        )
    except Exception:
        return "miss"
    if not isinstance(stuck, dict):
        return "miss"
    if stuck.get("status") == "none":
        return "none"
    if stuck.get("status") == "aim" and stuck.get("x") is not None:
        try:
            page.mouse.click(float(stuck["x"]), float(stuck["y"]))
            page.keyboard.press("Control+A")
            page.keyboard.insert_text(value)
            page.wait_for_timeout(200)
        except Exception:
            return "miss"
        # Re-read via near-label aim target value.
        try:
            got = page.evaluate(
                """(labelRe) => {
                  const re = new RegExp(labelRe, 'i');
                  const vis = (el) => {
                    const r = el.getBoundingClientRect();
                    const s = getComputedStyle(el);
                    return r.width > 4 && r.height > 4 && s.visibility !== 'hidden' && s.display !== 'none';
                  };
                  const labs = [...document.querySelectorAll('label,h4,h3,p,span')]
                    .filter(vis)
                    .filter(el => {
                      const own = [...el.childNodes].filter(n => n.nodeType === 3)
                        .map(n => n.textContent || '').join(' ').replace(/\\s+/g, ' ').trim();
                      const t = own || (el.innerText || '').replace(/\\s+/g, ' ').trim();
                      return t.length > 3 && t.length < 48 && re.test(t);
                    });
                  labs.sort((a, b) => ((a.innerText||'').length - (b.innerText||'').length));
                  const lab = labs[0];
                  if (!lab) return '';
                  const lr = lab.getBoundingClientRect();
                  const inputs = [...document.querySelectorAll('input')].filter(vis);
                  const hit = inputs
                    .map(el => {
                      const r = el.getBoundingClientRect();
                      return {el, dy: Math.abs(r.top - lr.top), gap: Math.abs((r.left+r.width/2)-(lr.left+lr.width/2))};
                    })
                    .filter(x => x.dy <= 56)
                    .sort((a, b) => (a.dy - b.dy) || (a.gap - b.gap));
                  return hit.length ? (hit[0].el.value || '') : '';
                }""",
                label_re,
            )
        except Exception:
            got = ""
        if _box_keeps(str(got or ""), value):
            return "ok"
        return "miss"
    if _box_keeps(str(stuck.get("value") or ""), value):
        return "ok"
    return "miss"


def _type_labeled(page, labels: tuple[str, ...], value: str, *, optional: bool = False) -> str:
    """Type into the box whose wrapper text matches this label (exact or contains).

    Cleartrip traveller labels have no htmlFor — find the label node, then the
    nearest input (first/last sit side by side on one row).
    """
    if not value:
        return "skip"
    wanted = {item.lower() for item in labels}
    last_want = any(re.search(r"last|surname", w) for w in wanted)
    first_want = any(re.search(r"first|given|middle|traveller name", w) for w in wanted) and not last_want

    if last_want:
        near = _type_near_label(page, r"last\s*name|surname", value)
        if near in {"ok", "typed"}:
            return near
    elif first_want:
        near = _type_near_label(
            page,
            r"first\s*(?:&|and)?\s*middle\s*name|given\s*name|first\s*name|traveller\s*name",
            value,
        )
        if near in {"ok", "typed"}:
            return near

    def _blob(row: dict) -> str:
        return " ".join(
            [
                str(row.get("label") or ""),
                str(row.get("placeholder") or ""),
                str(row.get("name") or ""),
            ]
        ).strip().lower()

    def _score(row: dict) -> int:
        if _is_email_box(row):
            return -1
        blob = _blob(row)
        if not blob:
            return -1
        if last_want and re.search(r"first|given|middle|traveller name", blob) and not re.search(
            r"last name|surname", blob
        ):
            return -1
        if first_want and re.search(r"last name|surname", blob) and not re.search(
            r"first|given|middle|traveller name", blob
        ):
            return -1
        label = str(row.get("label") or "").strip().lower()
        best = -1
        for w in wanted:
            if label == w:
                best = max(best, 200 - len(label))
            elif w in label and len(label) < 48:
                best = max(best, 120 - len(label))
            elif w in blob and len(blob) < 48:
                best = max(best, 60 - len(blob))
        return best

    boxes = _field_boxes(page)
    ranked = sorted(
        ((_score(row), row) for row in boxes),
        key=lambda item: (-item[0], len(str(item[1].get("label") or ""))),
    )
    match = next((row for score, row in ranked if score >= 40), None)
    if match is None:
        return "none" if optional else "miss"
    if str(match.get("value") or "").strip() == value.strip():
        return "ok"
    return _set_box(page, int(match["index"]), value)


def _pick_gender(page, gender: str) -> str:
    """Cleartrip shows Male/Female toggles instead of a Title dropdown.

    Do not use click_verified — selecting Male does not change the sheet mark, so
    verified clicks stall for seconds and burn the itinerary TTL.
    """
    wanted = (gender or "").strip().lower()
    label = "Female" if wanted in {"female", "f", "ms", "mrs"} else "Male"
    try:
        hit = page.evaluate(
            """(label) => {
              // Exact short chips only — Playwright get_by_text is slow on seat-map DOM.
              const nodes = [...document.querySelectorAll('button,[role=button],label,div')];
              const el = nodes.find((n) => {
                if (n.children.length > 2) return false;
                const t = (n.innerText || '').replace(/\\s+/g, ' ').trim();
                return t.toLowerCase() === String(label).toLowerCase();
              });
              if (!el) return '';
              el.scrollIntoView({block:'center'});
              el.click();
              return 'dom';
            }""",
            label,
        )
        return "ok" if hit else "miss"
    except Exception:
        return "miss"


def _title_selected(page, title: str) -> bool:
    try:
        if page.get_by_text(re.compile(r"Please select your title", re.I)).first.is_visible():
            return False
    except Exception:
        pass
    wanted = re.compile(rf"\b{re.escape(title)}\b", re.I)
    for row in _field_boxes(page):
        blob = f"{row.get('label') or ''} {row.get('value') or ''}"
        if wanted.search(blob) and not re.search(r"first|last|email|mobile|city", blob, re.I):
            return True
    return False


def _choose_title(page, title: str) -> tuple[str, str]:
    """Open the Title box and pick Mr or Ms. Returns (status, menu texts seen)."""
    if not title:
        return "skip", ""
    match = next(
        (row for row in _field_boxes(page) if str(row.get("label") or "").strip().lower() == "title"),
        None,
    )
    if match is None:
        return "none", ""
    try:
        page.evaluate(
            """(index) => {
              const vis = (el) => {
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 8 && r.height > 8 && s.visibility !== 'hidden' && s.display !== 'none';
              };
              const inputs = [...document.querySelectorAll('input, textarea')].filter(
                el => vis(el) && !/hidden|radio|checkbox|submit/i.test(el.type || '')
              );
              document.querySelectorAll('[data-guide-fill]').forEach(el => el.removeAttribute('data-guide-fill'));
              const el = inputs[index];
              if (!el) return;
              el.setAttribute('data-guide-fill', '1');
              el.scrollIntoView({block: 'center'});
            }""",
            int(match["index"]),
        )
        box = page.locator("[data-guide-fill='1']")
        rect = box.bounding_box()
        if rect:
            page.mouse.click(rect["x"] + rect["width"] - 16, rect["y"] + rect["height"] / 2)
        else:
            box.click(timeout=2000)
        page.wait_for_timeout(500)
        menu = page.evaluate(
            """(title) => {
              const want = String(title || '').toLowerCase();
              const leaves = [...document.querySelectorAll('body *')].filter(node => {
                if (node.children.length) return false;
                const text = (node.textContent || '').replace(/\\s+/g, ' ').trim();
                const r = node.getBoundingClientRect();
                return text && text.length < 16 && r.width > 4 && r.height > 4 && r.height < 48;
              });
              const hit = leaves.find(node => (node.textContent || '').replace(/\\s+/g, ' ').trim().toLowerCase().replace(/\\.$/, '') === want);
              if (hit) hit.click();
              const seen = [];
              for (const node of leaves) {
                const text = (node.textContent || '').replace(/\\s+/g, ' ').trim();
                if (text && !seen.includes(text) && seen.length < 12) seen.push(text);
              }
              return seen.join('|');
            }""",
            title,
        )
    except Exception:
        return "miss", ""
    page.wait_for_timeout(300)
    if _title_selected(page, title):
        return "ok", str(menu or "")
    return "miss", str(menu or "")


def box_slot(row: dict) -> str:
    """Map one input to a profile slot. Prefix boxes are not the mobile number."""
    label = str(row.get("label") or "")
    # A parent that wraps several fields leaks every label into one box.
    if re.search(r"e-?mail", label, re.I) and re.search(r"\+91|mobile|phone", label, re.I):
        label = ""
    blob = " ".join(
        [
            label,
            str(row.get("placeholder") or ""),
            str(row.get("name") or ""),
            str(row.get("type") or ""),
        ]
    ).lower()
    value = str(row.get("value") or "").strip()
    if re.fullmatch(r"\+?91", value) or re.search(r"country code|dial code|dialling", blob):
        return "prefix"
    if row.get("type") == "email" or re.search(r"e-?mail", blob):
        return "email"
    if row.get("type") == "tel" or re.search(r"\bmobile\b|\bphone\b", blob):
        return "phone"
    if re.search(r"date of birth|\bdob\b", blob):
        return "dob"
    if re.search(r"last name", blob):
        return "last"
    if re.search(r"first name|full name|traveller name", blob):
        return "name"
    return ""


def reveal_form_step(page) -> bool:
    """Scroll a contact or traveller block into view when it sits below the fold."""
    try:
        return bool(
            page.evaluate(
                """() => {
                  // Headings/labels only — scanning every div/span on Cleartrip stalls Kernel.
                  const nodes = [...document.querySelectorAll('h1,h2,h3,h4,label,legend,button')];
                  const hit = nodes.find((node) => {
                    const text = (node.innerText || '').replace(/\\s+/g, ' ').trim();
                    return text.length > 3 && text.length < 80
                      && /add contact|contact details|email address|mobile number|first name|traveller name|traveller details|add traveller/i.test(text);
                  });
                  if (!hit) return false;
                  hit.scrollIntoView({block: 'center'});
                  return true;
                }"""
            )
        )
    except Exception:
        return False


def _value_stuck(got: str, expected: str, key: str) -> bool:
    if key == "phone":
        want = re.sub(r"\D", "", expected)[-10:]
        return bool(want) and want in re.sub(r"\D", "", got or "")
    return expected.strip().lower() in (got or "").strip().lower()


def _is_email_box(row: dict) -> bool:
    """True only for the email input — not a mobile box whose parent leaked 'Email address'."""
    if row.get("type") == "email":
        return True
    label = str(row.get("label") or "")
    # Parent wrappers on Cleartrip include both +91 and Email address in one label.
    if re.search(r"e-?mail", label, re.I) and re.search(r"\+91|mobile|phone|add contact", label, re.I):
        label = ""
    own = f"{row.get('placeholder') or ''} {row.get('name') or ''} {label} {row.get('type') or ''}".lower()
    if re.search(r"e-?mail", own):
        return True
    value = str(row.get("value") or "")
    return "@" in value and "." in value


def _phone_box(boxes: list[dict]) -> dict | None:
    """The national-number box. A +91 prefix next to it is not that box.

    Never returns the email field — Kernel once typed the mobile into Email address.
    Live Cleartrip often paints +91 as plain text (not an input). Then the mobile
    box is the empty text input on the same row, to the left of Email address.
    """
    prefix_at = None
    for index, row in enumerate(boxes):
        if box_slot(row) == "prefix":
            prefix_at = index
    for row in boxes:
        if box_slot(row) == "phone" and not _is_email_box(row):
            return row
    if prefix_at is not None:
        prefix = boxes[prefix_at]
        try:
            px = float(prefix.get("x") or 0)
            py = float(prefix.get("y") or 0)
        except (TypeError, ValueError):
            px, py = 0.0, 0.0
        same_row: list[tuple[float, dict]] = []
        for nxt in boxes:
            if nxt is prefix or _is_email_box(nxt) or box_slot(nxt) in {"name", "last", "dob", "email", "prefix"}:
                continue
            value = str(nxt.get("value") or "").strip()
            if re.fullmatch(r"\+?91", value):
                continue
            try:
                nx = float(nxt.get("x") or 0)
                ny = float(nxt.get("y") or 0)
            except (TypeError, ValueError):
                nx, ny = 0.0, 0.0
            if py and abs(ny - py) < 45 and nx >= px - 8:
                same_row.append((nx, nxt))
        if same_row:
            same_row.sort(key=lambda item: item[0])
            return same_row[0][1]
        for nxt in boxes[prefix_at + 1 :]:
            if _is_email_box(nxt) or box_slot(nxt) in {"name", "last", "dob", "email", "prefix"}:
                continue
            value = str(nxt.get("value") or "").strip()
            if value and "@" in value:
                continue
            if re.fullmatch(r"\+?91", value):
                continue
            return nxt
    # No +91 input: Cleartrip contact — empty text left of the email field.
    email = next((row for row in boxes if row.get("type") == "email" or _is_email_box(row)), None)
    if email is None:
        return None
    try:
        ex = float(email.get("x") or 0)
        ey = float(email.get("y") or 0)
    except (TypeError, ValueError):
        return None
    left: list[tuple[float, dict]] = []
    for row in boxes:
        if row is email or _is_email_box(row) or row.get("type") == "email":
            continue
        if box_slot(row) in {"name", "last", "dob", "prefix"}:
            continue
        value = str(row.get("value") or "").strip()
        if "@" in value:
            continue
        try:
            nx = float(row.get("x") or 0)
            ny = float(row.get("y") or 0)
        except (TypeError, ValueError):
            continue
        if abs(ny - ey) < 50 and nx < ex - 8:
            left.append((nx, row))
    if not left:
        return None
    # Closest to the email on its left (the national number box).
    left.sort(key=lambda item: item[0])
    return left[-1][1]


def fill_profile_slots(page, profile: dict, *, keys: tuple[str, ...] = ("email", "phone", "name", "dob")) -> list[str]:
    """Type saved facts into whatever boxes this sheet actually shows.

    Matching uses label, placeholder, aria-name, and input type. A +91 prefix
    stays put; the 10-digit mobile goes in the next box. Each value is checked
    after typing, with one retry.
    """
    from app.step_read import profile_slots

    slots = profile_slots(profile or {})
    if slots.get("name") == "" and profile.get("name"):
        slots["name"] = str(profile.get("name") or "").strip()
    reveal_form_step(page)
    notes: dict[str, str] = {}

    def _targets() -> dict[str, dict]:
        boxes = _field_boxes(page)
        found: dict[str, dict] = {}
        for row in boxes:
            slot = box_slot(row)
            if slot in keys and slot not in found and not (slot == "phone" and _is_email_box(row)):
                found[slot] = row
        phone = _phone_box(boxes)
        if phone is not None and "phone" in keys and not _is_email_box(phone):
            found["phone"] = phone
        elif "phone" in found and _is_email_box(found["phone"]):
            found.pop("phone", None)
        # Email must be the real email input, never the +91 neighbour.
        for row in boxes:
            if _is_email_box(row) or (
                box_slot(row) == "email" and row.get("type") == "email"
            ):
                found["email"] = row
                break
        return found

    for _attempt in (1, 2):
        if slots.get("phone") and "phone" in keys:
            _wake_cleartrip_mobile(page)
        # Cleartrip bug: mobile digits landed in Email address.
        for row in _field_boxes(page):
            if _is_email_box(row) or row.get("type") == "email":
                got = str(row.get("value") or "")
                if re.fullmatch(r"\d{10}", re.sub(r"\D", "", got) or "") and "@" not in got:
                    _set_box(page, int(row["index"]), "")
        # Fill phone before email. Re-resolve boxes before every field — wake/Tab shifts indexes.
        order = [key for key in ("phone", "email", "name", "dob") if key in keys]
        for key in order:
            value = slots.get(key) or ""
            if not value:
                continue
            targets = _targets()
            row = targets.get(key)
            if key == "phone" and row is not None and _is_email_box(row):
                row = None
            if key == "email" and row is not None and not (
                _is_email_box(row) or row.get("type") == "email" or re.search(r"e-?mail", str(row.get("placeholder") or ""), re.I)
            ):
                row = None
            if row is None:
                if key == "phone" and _type_phone_after_prefix(page, value):
                    notes[key] = "ok"
                    continue
                notes[key] = "none"
                continue
            if _value_stuck(str(row.get("value") or ""), value, key):
                notes[key] = "ok"
                continue
            status = _set_box(page, int(row["index"]), value)
            # Cleartrip contact email often reports ok then shows "Please enter Email".
            # Always reinforce email/phone with keyboard insert into the aimed box.
            if key in {"email", "phone"}:
                try:
                    page.mouse.click(float(row["x"]) + 8, float(row["y"]) + 8)
                    page.keyboard.press("Control+A")
                    page.keyboard.insert_text(value)
                    page.wait_for_timeout(250)
                    again = _targets().get(key)
                    if again is not None and _value_stuck(str(again.get("value") or ""), value, key):
                        status = "ok"
                    elif key == "email":
                        # Prefer the live type=email value over a stale index read.
                        live = page.evaluate(
                            """(email) => {
                              const el = document.querySelector('input[type=email]');
                              return el ? (el.value || '') : '';
                            }"""
                        )
                        if _value_stuck(str(live or ""), value, "email"):
                            status = "ok"
                        else:
                            status = "miss"
                except Exception:
                    pass
            if status == "ok":
                notes[key] = "ok"
                continue
            again = _targets().get(key)
            if again is not None and _value_stuck(str(again.get("value") or ""), value, key):
                notes[key] = "ok"
            elif key == "phone" and _type_phone_after_prefix(page, value):
                notes[key] = "ok"
            else:
                notes[key] = "miss"
        # Final guard: phone must not sit in the email box; email must not sit in mobile.
        boxes = _field_boxes(page)
        phone_row = _phone_box(boxes)
        for row in boxes:
            if row.get("type") == "email" or _is_email_box(row):
                got = str(row.get("value") or "")
                if slots.get("phone") and _value_stuck(got, slots["phone"], "phone") and "@" not in got:
                    notes["phone"] = "miss"
                    if slots.get("email"):
                        _set_box(page, int(row["index"]), slots["email"])
                elif slots.get("email") and _value_stuck(got, slots["email"], "email"):
                    notes["email"] = "ok"
        if phone_row is not None and slots.get("email"):
            got = str(phone_row.get("value") or "")
            if slots["email"].lower() in got.lower():
                notes["phone"] = "miss"
                _set_box(page, int(phone_row["index"]), slots.get("phone") or "")
                email_row = _targets().get("email")
                if email_row is not None:
                    _set_box(page, int(email_row["index"]), slots["email"])
        if all(notes.get(key) in {"ok", "skip"} for key in keys if slots.get(key) and notes.get(key) != "none"):
            break
    return [f"{key}:{status}" for key, status in notes.items()]


def contact_report(notes: str, page: str) -> str:
    """WhatsApp line from the fill log and the page after the attempt."""
    blob = notes or ""
    if not re.search(r"\b(?:email|phone):", blob):
        return ""
    misses = []
    if re.search(r"\bemail:miss\b", blob):
        misses.append("email")
    if re.search(r"\bphone:miss\b", blob):
        misses.append("mobile")
    if misses or "contact_unready" in blob:
        fields = " and ".join(misses) or "a contact field"
        return f"The contact step is open, but {fields} did not stick. Nothing paid."
    if re.search(r"\bemail:ok\b", blob) and re.search(r"\bphone:(?:ok|none)\b", blob):
        if re.search(r"review traveller", page or "", re.I):
            return "Contact is saved. The page is on Review traveller details. Nothing paid."
        if re.search(r"\btap:1\b", blob):
            return "I typed the contact details and pressed Continue. Nothing paid."
        return "I typed the contact details. Nothing paid."
    return ""


def _field_state(notes: list[str], tag: str) -> str:
    for item in notes:
        if item.startswith(f"{tag}:"):
            return item.split(":", 1)[1]
    return ""


def _passenger_missing(notes: list[str]) -> list[str]:
    labels = {
        "title": "title",
        "first": "first name",
        "last": "last name",
        "email": "email",
        "phone": "mobile",
        "dob": "date of birth",
    }
    return [label for tag, label in labels.items() if _field_state(notes, tag) in {"miss", "set"}]


def _passenger_ready(notes: list[str]) -> bool:
    """Ready when every box this sheet actually has kept its value.

    A contact step with no name field is ready once email (and mobile, if shown) stuck.
    A traveller-name step is ready once first + last stick (title/gender optional).
    """
    first = _field_state(notes, "first")
    email = _field_state(notes, "email")
    if first in {"", "none", "skip"} and email in {"ok", "typed"}:
        if _field_state(notes, "phone") in {"miss", "set"}:
            return False
        return True
    if first in {"ok", "typed"} and _field_state(notes, "last") in {"ok", "typed", "skip", "none"}:
        if _field_state(notes, "title") in {"miss", "set"} and _field_state(notes, "gender") in {"miss", "set", ""}:
            return False
        # email/phone "set" or "miss" means the box is on this sheet and did not stick.
        if _field_state(notes, "email") in {"miss", "set"}:
            return False
        if _field_state(notes, "phone") in {"miss", "set"}:
            return False
        return True
    for tag in ("title", "first", "email"):
        if _field_state(notes, tag) not in {"ok", "typed", "skip", "none"}:
            return False
    if _field_state(notes, "last") in {"miss", "set"}:
        return False
    if _field_state(notes, "dob") in {"miss", "set"}:
        return False
    if _field_state(notes, "phone") in {"miss", "set"}:
        return False
    return True


def _apply_unknown_plan(page, plan: dict) -> list[str]:
    """Fill the slots this sheet shows, and press Continue only after they stick."""
    profile = {
        "email": "",
        "phone": "",
        "name": "",
        "dob": "",
    }
    for item in plan.get("fills") or []:
        key = str(item.get("key") or "")
        if key in profile:
            profile[key] = str(item.get("value") or "")
    notes = fill_profile_slots(page, profile)
    wanted = [str(item.get("key") or "") for item in (plan.get("fills") or [])]
    missed = [key for key in wanted if _field_state(notes, key) == "miss"]
    absent = [key for key in wanted if _field_state(notes, key) in {"", "none"}]
    if missed or absent:
        notes.append("contact_unready:" + ",".join(missed + absent))
        return notes
    tap = str(plan.get("tap") or "")
    if tap:
        notes.append(f"tap:{int(_click_label(page, tap))}")
    return notes


def _fill_cleartrip_name_pair(page, first: str, last: str) -> tuple[str, str]:
    """Fill Cleartrip's side-by-side First & middle / Last name via keyboard.

    Synthetic value sets report ok but React drops them; insert_text sticks.
    """
    if not first and not last:
        return "skip", "skip"
    try:
        aimed = page.evaluate(
            """() => {
              const vis = (el) => {
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 4 && r.height > 4 && s.visibility !== 'hidden' && s.display !== 'none';
              };
              const textOf = (el) => (el.innerText || '').replace(/\\s+/g, ' ').trim();
              // Labels only — span scans stall Kernel on leftover seat-map DOM.
              const findLab = (re) => [...document.querySelectorAll('label,p,h4')]
                .filter(vis)
                .find(el => {
                  const own = [...el.childNodes].filter(n => n.nodeType === 3)
                    .map(n => n.textContent || '').join('').replace(/\\s+/g,' ').trim();
                  const t = own || textOf(el);
                  return t.length > 3 && t.length < 48 && re.test(t);
                });
              const firstLab = findLab(/first\\s*&\\s*middle\\s*name|given\\s*name/i);
              const lastLab = findLab(/^last\\s*name|surname/i);
              const inputs = [...document.querySelectorAll('input')].filter(vis).filter(el => {
                const type = (el.type || '').toLowerCase();
                return !/hidden|radio|checkbox|submit|button|email|tel/.test(type);
              });
              const near = (lab) => {
                if (!lab) return null;
                const lr = lab.getBoundingClientRect();
                return inputs
                  .map(el => {
                    const r = el.getBoundingClientRect();
                    return {el, dy: Math.abs(r.top - lr.top), gap: Math.abs((r.left+r.width/2)-(lr.left+lr.width/2)), x: r.left};
                  })
                  .filter(x => x.dy <= 60)
                  .sort((a,b) => (a.dy - b.dy) || (a.gap - b.gap))[0] || null;
              };
              let firstHit = near(firstLab);
              let lastHit = near(lastLab);
              if (!firstHit || !lastHit || firstHit.el === lastHit.el) {
                const rows = {};
                for (const el of inputs) {
                  const y = Math.round(el.getBoundingClientRect().top / 8) * 8;
                  (rows[y] = rows[y] || []).push(el);
                }
                const pair = Object.values(rows)
                  .map(list => list.sort((a,b) => a.getBoundingClientRect().left - b.getBoundingClientRect().left))
                  .find(list => list.length === 2);
                if (pair) {
                  firstHit = {el: pair[0], x: pair[0].getBoundingClientRect().left};
                  lastHit = {el: pair[1], x: pair[1].getBoundingClientRect().left};
                }
              }
              const point = (hit) => {
                if (!hit || !hit.el) return null;
                const r = hit.el.getBoundingClientRect();
                return {x: r.x + r.width/2, y: r.y + r.height/2};
              };
              return {first: point(firstHit), last: point(lastHit)};
            }"""
        )
    except Exception:
        return "miss", "miss" if last else "skip"
    first_status, last_status = "miss", "miss" if last else "skip"

    def _type_at(point: dict | None, value: str) -> str:
        if not value:
            return "skip"
        if not isinstance(point, dict) or point.get("x") is None:
            return "miss"
        try:
            page.mouse.click(float(point["x"]), float(point["y"]))
            page.keyboard.press("Control+A")
            page.keyboard.insert_text(value)
            page.wait_for_timeout(200)
            return "ok"
        except Exception:
            return "miss"

    if isinstance(aimed, dict):
        first_status = _type_at(aimed.get("first"), first)
        last_status = _type_at(aimed.get("last"), last) if last else "skip"
    return first_status, last_status


def _fill_passenger_fields(page, profile: dict[str, str]) -> list[str]:
    """Type every traveller box we have. A label match is that box only, not a neighbour."""
    name = (profile.get("name") or "").strip()
    parts = name.split()
    first, last = (parts[0], " ".join(parts[1:])) if len(parts) >= 2 else (name, "")
    phone = re.sub(r"\D", "", profile.get("phone") or "")
    if phone.startswith("91") and len(phone) == 12:
        phone = phone[2:]
    dob = (profile.get("dob") or "").strip()
    gender = (profile.get("gender") or "").strip()
    # Ixigo leaves Title blank when we skip it; the Review Details sheet then blocks Continue.
    title = {"male": "Mr", "female": "Ms", "m": "Mr", "f": "Ms"}.get(gender.lower(), "Mr" if name else "")
    title_status, title_menu = _choose_title(page, title)
    gender_status = "skip"
    if title_status in {"none", "skip"} and (gender or name):
        gender_status = _pick_gender(page, gender or "male")
    # Cleartrip side-by-side name row needs keyboard typing (React drops synthetic sets).
    first_status, last_status = _fill_cleartrip_name_pair(page, first, last)
    if first_status != "ok":
        first_status = _type_labeled(
            page, ("first & middle name", "first name", "given name", "traveller name"), first
        )
    if last and last_status != "ok":
        last_status = _type_labeled(page, ("last name", "surname", "last name (surname)"), last)
    notes = [
        f"title:{title_status}",
        f"gender:{gender_status}",
        f"first:{first_status}",
        f"last:{last_status}",
        # Contact email/phone are often already saved; traveller sheet may not show them.
        f"email:{_type_labeled(page, ('email',), profile.get('email') or '', optional=True)}",
        f"phone:{_type_labeled(page, ('mobile number',), phone, optional=True)}",
        f"dob:{_type_labeled(page, ('date of birth', 'dob'), dob, optional=True)}",
    ]
    if title_menu:
        notes.append(f"title_menu:{title_menu[:120]}")
    return notes


_PAY_REFUSE = re.compile(r"pay now|pay to complete|make payment|complete payment|pay securely", re.I)


def _cleartrip_phone_stuck(page, national: str) -> bool:
    """True when a non-email input holds the 10-digit mobile."""
    digits = re.sub(r"\D", "", national or "")[-10:]
    if len(digits) != 10:
        return False
    try:
        return bool(
            page.evaluate(
                """(national) => {
                  const deep = [];
                  const walk = (root) => {
                    if (!root || !root.querySelectorAll) return;
                    for (const el of root.querySelectorAll('input')) deep.push(el);
                    for (const el of root.querySelectorAll('*')) {
                      if (el.shadowRoot) walk(el.shadowRoot);
                    }
                  };
                  walk(document);
                  return deep.some((el) => {
                    const type = (el.type || '').toLowerCase();
                    const ph = (el.placeholder || '') + (el.name || '') + (el.getAttribute('aria-label') || '');
                    if (type === 'email' || /e-?mail/i.test(ph)) return false;
                    const val = (el.value || '').replace(/\\D/g, '');
                    return val.includes(national);
                  });
                }""",
                digits,
            )
        )
    except Exception:
        return False


def _cleartrip_fill_contact_phone(page, phone: str, email: str, log: list[str]) -> bool:
    """Type mobile into Cleartrip contact. Never Continue until digits stick (or phone empty)."""
    national = re.sub(r"\D", "", phone or "")[-10:] if phone else ""
    try:
        page.evaluate(
            """() => {
              document.querySelectorAll('[data-ct-fill]').forEach(el => el.removeAttribute('data-ct-fill'));
              const emailEl = document.querySelector('input[type=email]');
              if (emailEl) emailEl.setAttribute('data-ct-fill', 'email');
              if (emailEl) {
                const er = emailEl.getBoundingClientRect();
                const phoneEl = [...document.querySelectorAll('input')].find(el => {
                  const r = el.getBoundingClientRect();
                  return el !== emailEl && (el.type || '') !== 'hidden' && r.width > 4
                    && Math.abs(r.top - er.top) < 40 && r.left < er.left;
                });
                if (phoneEl) phoneEl.setAttribute('data-ct-fill', 'phone');
              }
            }"""
        )
    except Exception:
        pass
    if email:
        try:
            page.locator("[data-ct-fill=email]").fill(email, timeout=3000)
        except Exception:
            pass
    if not national:
        log.append("ct_phone:none")
        return True
    stuck = False
    try:
        page.locator("[data-ct-fill=phone]").fill(national, timeout=3000)
        stuck = _cleartrip_phone_stuck(page, national)
    except Exception:
        stuck = False
    if not stuck:
        stuck = _type_phone_after_prefix(page, national)
    if not stuck:
        notes = fill_profile_slots(page, {"phone": national, "email": email}, keys=("phone", "email"))
        stuck = _field_state(notes, "phone") == "ok" or _cleartrip_phone_stuck(page, national)
    log.append(f"ct_phone:{'ok' if stuck else 'miss'}")
    return stuck


def _cleartrip_guest_sprint(page, profile: dict, log: list[str]) -> str:
    """Contact → traveller → Continue to payment as fast as possible.

    Cleartrip guest itineraries expire in ~20s after create. Returns
    pay|passenger|expired|miss.
    """
    email = str((profile or {}).get("email") or "").strip()
    phone = re.sub(r"\D", "", str((profile or {}).get("phone") or ""))
    if phone.startswith("91") and len(phone) == 12:
        phone = phone[2:]
    name = str((profile or {}).get("name") or "").strip()
    parts = name.split()
    first, last = (parts[0], " ".join(parts[1:])) if len(parts) >= 2 else (name, "")
    gender = str((profile or {}).get("gender") or "male").strip()

    try:
        text0, _url0 = _read(page)
    except Exception:
        text0 = ""
    needs_mobile = bool(
        re.search(r"add contact|please enter mobile|mobile number", text0 or "", re.I)
        or re.search(r"\+91", text0 or "")
    )
    phone_ok = _cleartrip_fill_contact_phone(page, phone, email, log)
    if needs_mobile and phone and not phone_ok:
        # Do not click Continue on an empty mobile — WhatsApp must send digits again.
        log.append("ct_contact_continue:0")
        return "miss"
    if needs_mobile and not phone:
        log.append("ct_contact_continue:0")
        return "miss"
    try:
        page.get_by_role("button", name=re.compile(r"^Continue$", re.I)).last.click(
            timeout=3000, force=True
        )
        log.append("ct_contact_continue:1")
    except Exception:
        log.append("ct_contact_continue:0")
        return "miss"

    try:
        page.wait_for_timeout(500)
    except Exception:
        pass
    try:
        text_after, _ = _read(page)
    except Exception:
        text_after = ""
    if re.search(r"please enter mobile number", text_after or "", re.I):
        log.append("ct_phone:still_empty")
        return "miss"

    # Traveller fields
    gender_status = _pick_gender(page, gender or "male")
    log.append(f"ct_gender:{gender_status}")
    try:
        page.evaluate(
            """() => {
              document.querySelectorAll('[data-ct-fill]').forEach(el => el.removeAttribute('data-ct-fill'));
              const textOf = (el) => (el.innerText || '').replace(/\\s+/g, ' ').trim();
              const findLab = (re) => [...document.querySelectorAll('label,p,h4')]
                .find(el => { const t = textOf(el); return t.length > 3 && t.length < 48 && re.test(t); });
              const firstLab = findLab(/first\\s*&\\s*middle\\s*name|given\\s*name/i);
              const lastLab = findLab(/^last\\s*name|surname/i);
              const inputs = [...document.querySelectorAll('input')].filter(el => {
                const r = el.getBoundingClientRect();
                return r.width > 4 && r.height > 4 && !/hidden|email|tel/i.test(el.type || '');
              });
              const near = (lab) => {
                if (!lab) return null;
                const lr = lab.getBoundingClientRect();
                return inputs
                  .map(el => {
                    const r = el.getBoundingClientRect();
                    return {el, dy: Math.abs(r.top - lr.top), gap: Math.abs((r.left + r.width / 2) - (lr.left + lr.width / 2))};
                  })
                  .filter(x => x.dy <= 60)
                  .sort((a, b) => (a.dy - b.dy) || (a.gap - b.gap))[0];
              };
              const f = near(firstLab); const l = near(lastLab);
              if (f && f.el) f.el.setAttribute('data-ct-fill', 'first');
              if (l && l.el) l.el.setAttribute('data-ct-fill', 'last');
            }"""
        )
        if first:
            page.locator("[data-ct-fill=first]").fill(first, timeout=2500)
            log.append("ct_first:ok")
        if last:
            page.locator("[data-ct-fill=last]").fill(last, timeout=2500)
            log.append("ct_last:ok")
    except Exception:
        first_s, last_s = _fill_cleartrip_name_pair(page, first, last)
        log.append(f"ct_first:{first_s}")
        log.append(f"ct_last:{last_s}")

    ctp = _continue_to_payment(page)
    log.append(f"continue_to_payment:{ctp}")
    if ctp == "expired":
        return "expired"
    try:
        page.wait_for_timeout(1200)
    except Exception:
        pass
    text, url = _read(page)
    stage = classify_page(text, url, book_count=_book_count(page))
    # Contact / empty mobile is never pay — even if sidebar says Payment options.
    if _CONTACT_SHEET.search(text or "") or re.search(r"please enter mobile", text or "", re.I):
        return "miss"
    if stage == "pay" or payment_sheet_open(text, stage):
        return "pay"
    if stage == "passenger" or re.search(r"add traveller|continue to payment", text or "", re.I):
        return "passenger"
    return "miss" if ctp == "miss" else "passenger"


def _continue_to_payment(page) -> str:
    """Press Cleartrip's traveller handoff. Returns ok, miss, or expired."""
    update: dict[str, object] = {"code": None, "failure": ""}

    def on_resp(res) -> None:
        if "itinerary/update" not in (res.url or ""):
            return
        try:
            data = res.json()
        except Exception:
            return
        if not isinstance(data, dict):
            return
        update["code"] = data.get("responseCode")
        err = data.get("error") if isinstance(data.get("error"), dict) else {}
        update["failure"] = str((err or {}).get("failureType") or "")

    try:
        page.on("response", on_resp)
    except Exception:
        pass
    moved = False
    try:
        moved = bool(
            page.evaluate(
                """() => {
                  const el = [...document.querySelectorAll('button,a,[role=button]')]
                    .find(n => /^continue to payment$/i.test((n.innerText||'').replace(/\\s+/g,' ').trim()));
                  if (!el || el.disabled) return false;
                  el.scrollIntoView({block:'center'});
                  const fiberKey = Object.keys(el).find(k => k.startsWith('__reactFiber$'));
                  let fiber = fiberKey ? el[fiberKey] : null;
                  const evt = {
                    preventDefault() {}, stopPropagation() {},
                    nativeEvent: { isTrusted: true }, target: el, currentTarget: el, type: 'click',
                  };
                  for (let i = 0; i < 12 && fiber; i++) {
                    const props = fiber.memoizedProps || fiber.pendingProps;
                    if (props && typeof props.onClick === 'function') {
                      props.onClick(evt);
                      return true;
                    }
                    fiber = fiber.return;
                  }
                  el.click();
                  return true;
                }"""
            )
        )
    except Exception:
        moved = False
    if not moved:
        try:
            page.get_by_role("button", name=re.compile(r"^Continue to payment$", re.I)).last.click(
                timeout=4000, force=True
            )
            moved = True
        except Exception:
            moved = _js_continue(page) or _safe_continue(page)
    for _ in range(20):
        if update["code"] is not None or update["failure"]:
            break
        try:
            page.wait_for_timeout(250)
        except Exception:
            break
    try:
        page.remove_listener("response", on_resp)
    except Exception:
        pass
    failure = str(update.get("failure") or "")
    code = update.get("code")
    if "EXPIRED" in failure.upper() or code in {4004, "4004"}:
        return "expired"
    if moved:
        return "ok"
    return "miss"


def _ask_model(snapshot: str) -> dict[str, str] | None:
    from app.llm import chat

    raw = chat(
        [
            {
                "role": "system",
                "content": (
                    "You drive one booking page. Reply with JSON only: "
                    '{"action":"click"|"wait"|"stuck","target":"visible button text","value":""}. '
                    "Never pay. Never click Pay now. Prefer Book for the requested time, then Continue."
                ),
            },
            {"role": "user", "content": snapshot[:3500]},
        ],
        temperature=0,
        max_tokens=120,
        timeout=25,
    )
    return parse_action(raw)


def ixigo_auth_session_signals(text: str, url: str, *, stage: str = "") -> bool:
    """True only when Ixigo UI shows post-login booking state (not OTP SUCCESS alone)."""
    stage = stage or classify_page(text, url)
    if stage in {"otp", "login"}:
        return False
    if re.search(r"loginVisible=true", url or "", re.I):
        return False
    if stage in {"passenger", "addons", "pay"}:
        return True
    if stage == "review" and re.search(r"first name|last name|primary passenger|date of birth", text or "", re.I):
        return True
    if re.search(r"\blog\s*out\b", text or "", re.I) and re.search(
        r"view profile|ixigoer|my profile", text or "", re.I
    ):
        return True
    return False


def ixigo_auth_session_ready(page) -> bool:
    """Page wrapper for ixigo_auth_session_signals."""
    text, url = _read(page)
    stage = classify_page(text, url, book_count=_book_count(page))
    return ixigo_auth_session_signals(text, url, stage=stage)


def run_checkout_agent(
    page,
    *,
    hint: str,
    seller: str,
    origin: str,
    destination: str,
    depart: str,
    max_steps: int = 18,
    profile: dict[str, str] | None = None,
    expect_flight: str = "",
    allow_pay: bool = False,
    pause_on_auth: bool = False,
    chosen_tap: str = "",
) -> CheckoutOutcome:
    """Walk one seller toward payment handoff. Never charges unless allow_pay is True.

    pause_on_auth: stop at login/otp without filling phone or requesting OTP, so the
    human can authenticate in the browser; resume after ixigo_auth_session_ready.
    """
    log: list[str] = []
    checks: list[str] = []
    recovers = 0
    seen: set[str] = set()
    text, url = "", ""
    stage = "other"
    facts: dict[str, str] = {}
    profile = profile or {}
    handoff = ""
    origin_code = "GAU" if re.search(r"guwahati|gau", origin or "", re.I) else ""
    dest_code = "DEL" if re.search(r"delhi|del", destination or "", re.I) else ""
    tap_pending = (chosen_tap or "").strip()
    # Guest Cleartrip: recover a dead itinerary on the same site — never hop sellers here.
    max_recovers = 2 if seller == "cleartrip" else 1
    # Book → meals Skip → contact → traveller → pay needs a long Cleartrip pass.
    deadline = time.monotonic() + (240 if seller == "cleartrip" else 90)
    vision_down = False
    try:
        page.set_default_timeout(20_000)
        page.set_default_navigation_timeout(90_000)
    except Exception:
        pass

    def _snapshot() -> tuple[str, str, str, dict[str, str]]:
        t, u = _read(page)
        # Past Cleartrip create, skip heavy overlay scans — they can stall Kernel on seat maps.
        on_ct_itin = (
            "cleartrip.com" in (u or "").lower()
            and re.search(r"/flights/itinerary/[A-Za-z0-9_-]+", u or "")
            and "failure" not in (u or "").lower()
        )
        if not on_ct_itin:
            try:
                surface = read_surface(page)
                t = apply_front_text(t, surface)
            except Exception:
                pass
        s = classify_page(t, u, book_count=_book_count(page))
        f = extract_trip_facts(t, u)
        return t, u, s, f

    for step in range(max_steps):
        page = _latest(page)
        text, url, stage, facts = _snapshot()
        log.append(f"{step}:stage:{stage}")
        # Skip reveal on Cleartrip review — the flex sheet has no contact block yet, and
        # a full-DOM scroll search can stall Kernel for minutes.
        skip_reveal = stage == "review" and (
            seller == "cleartrip" or "cleartrip.com" in (url or "").lower()
        )
        if not skip_reveal and "reveal_form" not in log and stage in {"other", "passenger", "review"}:
            log.append("reveal_form")
            if reveal_form_step(page):
                continue
        if time.monotonic() > deadline:
            log.append("checkout_deadline")
            handoff = handoff or (
                "This step took too long, so I stopped and sent the page. Nothing paid."
            )
            return CheckoutOutcome(stage, text, url, log, facts=facts, checks=checks, handoff=handoff)
        # A named button is clicked before any add-ons shortcut. Review is not Choose add-ons.
        if tap_pending and not tap_pending.startswith("__"):
            from app.step_read import form_sheet

            if form_sheet(text) and re.fullmatch(r"continue", tap_pending, re.I):
                log.append("defer_continue_for_fill")
            elif re.search(r"pay now|pay to complete|make payment", tap_pending, re.I) and not allow_pay:
                log.append("user_tap_refused")
                tap_pending = ""
            elif re.search(r"skip add", tap_pending, re.I):
                # click_verified on Skip can stall Kernel CDP; use the add-ons outcome loop.
                log.append(f"user_tap:{tap_pending}")
                tap_pending = ""
                _leave_addons(page, log, rounds=2)
                continue
            elif re.search(r"\bcontinue\b", tap_pending, re.I):
                # Cleartrip Review uses "Continue with 1-step checkout" — exact "Continue" misses.
                moved = _js_continue(page) or _safe_continue(page) or _click_label(page, tap_pending)
                log.append(f"user_tap_continue:{int(bool(moved))}")
                tap_pending = ""
                try:
                    page.wait_for_timeout(900)
                except Exception:
                    pass
                continue
            elif _click_label(page, tap_pending):
                log.append(f"user_tap:{tap_pending}")
                tap_pending = ""
                try:
                    page.wait_for_timeout(800)
                except Exception:
                    pass
                continue
            else:
                log.append("user_tap_miss")
                tap_pending = ""
        from app.step_read import on_review_itinerary

        # Add-ons skip is an outcome loop, not a 16-step vision tour. Reply within ~20s.
        # Never treat Review-your-itinerary flex "Add" upsells as Choose add-ons / Skip.
        skip_now = (
            stage == "addons"
            or (
                "itinerary" in (url or "").lower()
                and stage not in {"results", "fare_sheet", "loading", "home", "error", "review"}
                and not re.search(r"review your itinerary", text or "", re.I)
                and bool(
                    re.search(
                        r"choose add-ons|skip add-?ons|add seats|missed adding meal|add meals|add extra luggage",
                        text or "",
                        re.I,
                    )
                )
            )
        )
        if skip_now:
            log.append("addons_fast")
            try:
                settle_visible_sheet(page, budget_ms=4000)
            except Exception:
                pass
            left = _leave_addons(page, log, rounds=3)
            text, url, stage, facts = _snapshot()
            if not left or stage == "addons":
                # Seat map often needs a second Skip after scroll; try once more before stopping.
                try:
                    settle_visible_sheet(page, budget_ms=3000)
                except Exception:
                    pass
                left = _leave_addons(page, log, rounds=2)
                text, url, stage, facts = _snapshot()
                left = bool(left) and stage != "addons" and not re.search(
                    r"choose add-ons|add seats|missed adding meal", text or "", re.I
                )
                log.append(f"addons_fast_dom:{stage}")
            if left and stage != "addons":
                log.append(f"addons_fast_left:{stage}")
            else:
                # Keep walking — deadline still bounds the loop. Do not freeze on one missed Skip.
                log.append("addons_fast_still")
                if sum(1 for line in log if line == "addons_fast_still") >= 3:
                    handoff = (
                        "That click did not leave this sheet. "
                        "Nothing paid."
                    )
                    return CheckoutOutcome("addons", text, url, log, facts=facts, checks=checks, handoff=handoff)
                action = {"action": "click", "target": "Skip add-ons", "value": ""}
                kind = action["action"]
                log.append(f"{step}:{stage}:{kind}:{action.get('target')}")
                if _click_label(page, "Skip add-ons") or _click_label(page, "Skip add ons"):
                    log.append("addons_retry_skip")
                continue
        # Vision on every step: screenshot overrides a wrong DOM toast / sold-out miss.
        # One network miss disables vision for the rest of this checkout (avoids 28s×N hangs).
        # Past Cleartrip create, DOM Skip/fill is enough — Kernel screenshots can stall guest checkout.
        if (
            not vision_down
            and "cleartrip.com" in (url or "").lower()
            and re.search(r"/flights/itinerary/[A-Za-z0-9_-]+", url or "")
            and "failure" not in (url or "").lower()
            and stage in {"addons", "passenger", "review", "pay", "other", "login", "otp"}
        ):
            vision_down = True
            log.append("vision_skip_cleartrip_itin")
        try:
            from app.vision_agent import capture_page_shot, classify_with_vision, vision_enabled

            if vision_down:
                pass
            elif vision_enabled():
                shot = capture_page_shot(page)
                if not shot:
                    log.append(f"{step}:vision_shot_miss")
                else:
                    new_stage, vision_seen = classify_with_vision(
                        stage, shot, seller=seller, hint=hint, page_text=text
                    )
                    if vision_seen:
                        log.append(
                            f"{step}:vision:{vision_seen.get('kind')}:"
                            f"fares={int(bool(vision_seen.get('fares_visible')))}"
                            f":sold={int(bool(vision_seen.get('sold_out')))}"
                        )
                    else:
                        log.append(f"{step}:vision_empty")
                        vision_down = True
                        log.append("vision_down")
                    if new_stage and new_stage != stage:
                        # Never demote a live Skip-add-ons sheet to review.
                        if stage == "addons" and new_stage == "review" and re.search(
                            r"skip add-?ons|add meals|choose add-ons", text or "", re.I
                        ):
                            log.append(f"{step}:vision_keep_addons")
                        else:
                            log.append(f"{step}:vision_override:{stage}->{new_stage}")
                            stage = new_stage
                    try:
                        Path(shot).unlink(missing_ok=True)
                    except Exception:
                        pass
            else:
                log.append(f"{step}:vision_off")
        except Exception as exc:
            vision_down = True
            log.append(f"{step}:vision_err:{type(exc).__name__}")
            log.append("vision_down")
        from app.step_read import form_sheet as _form_sheet

        if _form_sheet(text) and stage not in {"pay", "otp", "login", "error", "unavailable"}:
            if stage != "other":
                log.append(f"{step}:form_sheet:{stage}->other")
            stage = "other"
        if tap_pending and "defer_continue_for_fill" not in log[-4:]:
            label = tap_pending
            tap_pending = ""
            if label in {"__select_traveller__", "__pick_traveller__"}:
                note = _select_saved_traveller(page, str(profile.get("name") or ""))
                log.append(f"user_pick_traveller:{note or 'miss'}")
                if note.startswith("traveller_clicked") or note.startswith("traveller_already") or note.startswith(
                    "traveller_card"
                ):
                    try:
                        page.wait_for_timeout(500)
                    except Exception:
                        pass
                    continue
                if note.startswith("traveller_unticked") or note == "behind_sheet" or not note:
                    handoff = (
                        "Saved travellers are on screen. I could not tick one yet — "
                        "reply click one of the names again, or say delete one of the profiles. "
                        "I will not create a new traveller. Nothing paid."
                    )
                    return CheckoutOutcome("passenger", text, url, log, facts=facts, checks=checks, handoff=handoff)
            elif label in {"__delete_traveller__", "__remove_traveller__"}:
                note = _delete_saved_traveller(page, str(profile.get("name") or ""))
                log.append(f"user_delete_traveller:{note or 'miss'}")
                handoff = (
                    "I removed one saved traveller profile."
                    if note.startswith("traveller_deleted")
                    else "I could not find a delete control on the traveller list. Nothing paid."
                )
                return CheckoutOutcome("passenger", text, url, log, facts=facts, checks=checks, handoff=handoff)
            elif label.startswith("__"):
                log.append("user_tap:recommended")
            elif re.search(r"pay now|pay to complete|make payment", label, re.I) and not allow_pay:
                log.append("user_tap_refused")
            elif _click_label(page, label):
                log.append(f"user_tap:{label}")
                try:
                    page.wait_for_timeout(800)
                except Exception:
                    pass
                continue
            else:
                log.append("user_tap_miss")
        if pause_on_auth and stage in {"login", "otp"}:
            log.append(f"{step}:{stage}:auth_pause")
            handoff = (
                "Authentication required in the browser. Do not automate OTP. "
                f"Verified fare so far {facts.get('total') or '?'}. Nothing paid."
            )
            return CheckoutOutcome("auth_required", text, url, log, facts=facts, checks=checks, handoff=handoff)
        if stage in {"review", "passenger", "addons", "pay"} and (facts.get("flight") or facts.get("total")):
            ok, problems = verify_trip_facts(
                facts,
                expect_hint=hint,
                expect_origin=origin_code,
                expect_dest=dest_code,
                expect_flight=expect_flight,
            )
            check = f"{stage}:{'ok' if ok else 'mismatch'}:{facts}"
            checks.append(check)
            if not ok:
                log.append(f"{step}:verify_fail:{';'.join(problems)}")
                return CheckoutOutcome(
                    "wrong_flight",
                    text,
                    url,
                    log,
                    facts=facts,
                    checks=checks,
                    handoff="Stopped: on-screen itinerary did not match the requested flight.",
                )
        if stage == "error" and recovers >= max_recovers:
            log.append(f"{step}:error:stop")
            break
        signature = f"{stage}|{url}|{text[:180]}"
        repeat = signature in seen
        if stage == "passenger" and sum(1 for line in log if line.startswith("first:")) < 2:
            repeat = False
        # Book hit the card but Cleartrip kept the results URL — do not freeze as "ask which button".
        book_retries = sum(1 for line in log if line in {"book_still_results", "after_book:mouse_book"})
        if stage == "results" and book_retries < 2 and any(
            line.startswith("card:") and len(line) > 12 for line in log[-8:]
        ):
            repeat = False
        seen.add(signature)
        action = None if repeat else deterministic_action(stage, hint=hint, recovers=recovers, url=url)
        loading_waits = sum(1 for line in log if ":loading:wait" in line)
        if stage == "loading" and loading_waits < 8:
            action = {"action": "wait", "target": "", "value": ""}
        elif stage == "loading" and loading_waits >= 8 and hint:
            # Results shell stuck on skeleton — force a Book attempt.
            action = {"action": "click_time", "target": hint, "value": ""}
        if action is None and stage == "fare_sheet" and sum(1 for line in log if line.startswith("confirm_fare")) < 2:
            action = {"action": "click", "target": "Continue", "value": ""}
        if (
            action is None
            and stage == "results"
            and hint
            and book_retries < 2
            and any(line.startswith("card:") and len(line) > 12 for line in log[-8:])
        ):
            # Prefer confirming the open fare drawer over another blind Book.
            action = {"action": "click", "target": "Continue", "value": ""}
            log.append("results_after_book:try_continue")
        if action is None and stage == "review" and sum(1 for line in log if "review_continue" in line) < 3:
            action = {"action": "review_continue", "target": "Continue", "value": ""}
        contact_tries = sum(1 for line in log if line.startswith("contact_try:"))
        if action is None and stage == "other" and contact_tries < 2:
            from app.step_read import form_sheet, plan_unknown_step

            if form_sheet(text):
                from app.step_read import format_unknown_plan

                # Cleartrip guest holds die in ~20s — sprint contact→traveller→pay.
                if seller == "cleartrip" or "cleartrip.com" in (url or "").lower():
                    log.append(f"contact_try:{contact_tries + 1}")
                    sprint = _cleartrip_guest_sprint(page, profile, log)
                    text, url, stage, facts = _snapshot()
                    # Never announce pay while contact / empty mobile is still on screen.
                    if _CONTACT_SHEET.search(text or "") or re.search(
                        r"please enter mobile number", text or "", re.I
                    ):
                        sprint = "miss"
                        stage = "other"
                    if sprint == "pay" and stage == "pay" and payment_sheet_open(text, stage):
                        handoff = (
                            f"Payment page ready. Flight {facts.get('flight') or '?'} "
                            f"{facts.get('depart_time') or hint} "
                            f"{facts.get('origin')}-{facts.get('destination')} "
                            f"total {facts.get('total') or '?'}. Link: {url}. No payment clicked."
                        )
                        return CheckoutOutcome("pay", text, url, log, facts=facts, checks=checks, handoff=handoff)
                    if sprint == "expired" and recovers < max_recovers and hint:
                        recovers += 1
                        if _reopen_cleartrip_results(page, origin, destination, depart, log):
                            log.append("expired_rebook")
                            continue
                    if sprint == "passenger":
                        continue
                    if sprint == "miss":
                        ask = plan_unknown_step(
                            text, control_digest(page), profile, intent="give_facts", use_model=False
                        )
                        handoff = ""
                        try:
                            from app.checkout_brain import decide_checkout
                            from app.config import get_settings

                            if get_settings().checkout_brain_ready:
                                brain = decide_checkout(
                                    text,
                                    url=url,
                                    seller="Cleartrip",
                                    hint=hint,
                                    profile=profile,
                                    prefer_llm=True,
                                )
                                if brain.get("source") in {"llm", "llm_vision"} and brain.get("reply"):
                                    handoff = str(brain["reply"])
                                    log.append(f"brain:{brain.get('kind')}:{brain.get('action')}")
                        except Exception:
                            handoff = ""
                        if ask.get("handled") and ask.get("action") == "ask":
                            return CheckoutOutcome(
                                "passenger",
                                text,
                                url,
                                log,
                                facts=facts,
                                checks=checks,
                                handoff=handoff
                                or format_unknown_plan(ask)
                                or "Send an Indian mobile (+91) for contact. Nothing paid.",
                            )
                        return CheckoutOutcome(
                            "passenger",
                            text,
                            url,
                            log,
                            facts=facts,
                            checks=checks,
                            handoff=handoff
                            or "Contact still needs an Indian mobile (+91). Send the 10 digits. Nothing paid.",
                        )
                    tap_pending = ""
                    continue
                plan = plan_unknown_step(text, control_digest(page), profile, intent="give_facts", use_model=False)
                if plan.get("handled") and plan.get("action") == "ask":
                    return CheckoutOutcome(
                        "passenger",
                        text,
                        url,
                        log,
                        facts=facts,
                        checks=checks,
                        handoff=format_unknown_plan(plan) or "Send the missing contact detail. Nothing paid.",
                    )
                if plan.get("handled") and plan.get("action") in {"fill", "click"}:
                    log.append(f"contact_try:{contact_tries + 1}")
                    applied = _apply_unknown_plan(page, plan)
                    log.extend(applied)
                    tap_pending = ""
                    if any(line.startswith("contact_unready") for line in applied) and contact_tries == 0:
                        continue
                    if any(line.startswith("contact_unready") for line in applied):
                        return CheckoutOutcome(
                            "passenger",
                            text,
                            url,
                            log,
                            facts=facts,
                            checks=checks,
                            handoff=contact_report(" | ".join(applied), text)
                            or "The contact step is open, but a field did not stick. Nothing paid.",
                        )
                    continue
        if action is None:
            if not chosen_tap:
                return _pause_unknown(
                    page,
                    seller=seller,
                    hint=hint,
                    text=text,
                    url=url,
                    log=log,
                    facts=facts,
                    checks=checks,
                    step=step,
                )
            buttons = ", ".join(_buttons(page)[:12])
            digest = control_digest(page)
            action = _ask_model(
                f"Seller {seller}. Trip {origin} to {destination} on {depart}. Time {hint or 'any'}.\n"
                f"Stage {stage}. URL {url}.\nButtons: {buttons}\n"
                f"Controls:\n{digest[:800]}\nPage:\n{text[:1000]}"
            ) or {"action": "stuck", "target": stage, "value": ""}
        kind = action["action"]
        log.append(f"{step}:{stage}:{kind}:{action.get('target')}")
        logger.info("Checkout agent %s", log[-1])
        if kind == "done":
            if stage == "pay":
                handoff = (
                    f"Payment page ready. Flight {facts.get('flight') or '?'} "
                    f"{facts.get('depart_time') or hint} {facts.get('origin')}-{facts.get('destination')} "
                    f"total {facts.get('total') or '?'}. Link: {url}. No payment clicked."
                )
            elif stage == "otp":
                handoff = (
                    "Ixigo sent or is asking for an OTP. Ask the traveller for the code; "
                    "do not invent one. Nothing paid."
                )
            break
        if kind == "stuck":
            if stage == "unavailable" or action.get("target") == "unavailable":
                handoff = (
                    f"Flight no longer available for {hint or 'that departure'} on {seller}. "
                    "Search Again is on screen. Nothing paid."
                )
            break
        if kind == "wait":
            page.wait_for_timeout(1200 if stage == "loading" else 600)
            continue
        if kind == "review_continue":
            # Escape closes the Ixigo review sheet, so the Continue control disappears.
            # Conscience reads the covering sheet; rails never pay.
            # If meals/Skip is actually on screen, leave add-ons — do not Continue past them.
            # Cleartrip flex "Add ₹329" cards are NOT Choose add-ons — Continue past them.
            real_addons = bool(
                re.search(r"skip add-?ons|choose add-ons|add seats|missed adding meal", text or "", re.I)
            ) and not re.search(r"review your itinerary", text or "", re.I)
            if real_addons:
                log.append("review_was_addons")
                left = _leave_addons(page, log, rounds=4)
                text, url, stage, facts = _snapshot()
                log.append(f"review_addons_left:{int(bool(left))}:{stage}")
                continue
            cleartrip_review = "cleartrip.com" in (url or "").lower() or seller == "cleartrip"
            if cleartrip_review:
                try:
                    page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
                    page.wait_for_timeout(500)
                except Exception:
                    pass
                declined = _decline_cleartrip_protect(page)
                if declined:
                    log.append(f"ct_protect_decline:{declined}")
                    try:
                        page.wait_for_timeout(900)
                    except Exception:
                        pass
            else:
                surface = read_surface(page)
                if surface.get("overlay") or _ixigo_buy_this_visible(page):
                    note = _clear_front_sheet(page, chosen_tap, seller=seller, hint=hint)
                    log.append(note or "front_clear_miss")
                    if note.startswith("front_tap:"):
                        continue
                    return _pause_unknown(
                        page,
                        seller=seller,
                        hint=hint,
                        text=text,
                        url=url,
                        log=log,
                        facts=facts,
                        checks=checks,
                        step=step,
                        chosen_tap=chosen_tap,
                    )
                # Tick one saved person and leave extras off before Continue. Do not add another.
                log.append(_ixigo_opt_out_cancellation(page))
                log.append(_select_saved_traveller(page, str(profile.get("name") or "")))
                try:
                    page.wait_for_timeout(400)
                except Exception:
                    pass
            # One accurate click per attempt. A miss can be retried; Pay is never clicked.
            moved = _js_continue(page) or _safe_continue(page)
            log.append(f"continue_click:{int(bool(moved))}")
            if not moved:
                try:
                    page.mouse.wheel(0, 1200)
                    page.wait_for_timeout(400)
                except Exception:
                    pass
                moved = _js_continue(page) or _safe_continue(page)
                log.append(f"continue_click_scroll:{int(bool(moved))}")
            _poll_ready(
                page,
                {"login", "otp", "passenger", "addons", "pay", "error", "other", "review"},
                budget_ms=8000,
            )
            if not cleartrip_review:
                # Continue often opens a name Confirm sheet — conscience clears it.
                after = _clear_front_sheet(page, seller=seller, hint=hint)
                if after and after.startswith("front_tap:"):
                    log.append(after)
                elif after == "front_ask":
                    return _pause_unknown(
                        page,
                        seller=seller,
                        hint=hint,
                        text=text,
                        url=url,
                        log=log,
                        facts=facts,
                        checks=checks,
                        step=step,
                    )
            continue
        if kind == "login_phone":
            # Do not press Escape here — it closes the Ixigo login drawer.
            try:
                page.wait_for_selector(
                    "input[placeholder*='Mobile' i], input[placeholder*='mobile' i], text=/Log in to ixigo/i",
                    timeout=1500,
                )
            except Exception:
                log.append("login_ui_wait_timeout")
            phone = str(profile.get("phone") or "")
            note = _fill_ixigo_login_phone(page, phone)
            log.append(note)
            if note.startswith("bad") or note.endswith("miss"):
                handoff = (
                    "Ixigo opened login but the mobile field was not fillable. "
                    f"Verified fare {facts.get('total') or '?'}. Link: {url}. Nothing paid."
                )
                break
            # OTPless may construct here — refresh merchant-callback bridge.
            log.append(install_ixigo_otpless_callback_bridge(page))
            if not _safe_continue(page):
                log.append("login_continue_miss")
                break
            text, url, stage, facts = _poll_ready(
                page, {"otp", "passenger", "review", "addons", "pay", "error"}, budget_ms=3500
            )
            if stage == "otp":
                handoff = (
                    f"OTP sent for login. Verified {facts.get('flight') or '?'} "
                    f"{facts.get('depart_time') or hint} total {facts.get('total') or '?'}. "
                    "Ask the traveller for the SMS code. Nothing paid."
                )
                log.append("otp_handoff")
                return CheckoutOutcome("otp", text, url, log, facts=facts, checks=checks, handoff=handoff)
            continue
        if kind == "fill_passenger":
            front = _clear_front_sheet(page, chosen_tap)
            if front.startswith("front_tap:"):
                log.append(front)
                continue
            saved = _select_saved_traveller(page, str(profile.get("name") or ""))
            if saved == "behind_sheet":
                log.append(saved)
                from app.step_read import is_name_confirm, is_paid_upsell

                hay = _surface_hay(read_surface(page))
                if is_name_confirm(hay):
                    note = _clear_front_sheet(page)
                    log.append(note or "name_confirm_miss")
                    continue
                if is_paid_upsell(hay) and not chosen_tap:
                    return _pause_unknown(
                        page,
                        seller=seller,
                        hint=hint,
                        text=text,
                        url=url,
                        log=log,
                        facts=facts,
                        checks=checks,
                        step=step,
                    )
                note = _clear_front_sheet(page, chosen_tap)
                log.append(note or "behind_sheet_miss")
                continue
            if saved.startswith("traveller_unticked") or (
                not saved and re.search(r"\b(mr|ms|mrs|mx)\b", text or "", re.I)
            ):
                log.append(saved or "traveller_list_visible")
                # Logged-in saved list: never create another duplicate via Add New Traveller.
                handoff = (
                    "Your Ixigo account already lists saved travellers. "
                    "Reply click one of the names and I'll tick it, or say delete one of the profiles. "
                    "I will not create a new traveller while those are on screen. Nothing paid."
                )
                return CheckoutOutcome("passenger", text, url, log, facts=facts, checks=checks, handoff=handoff)
            if saved.startswith("traveller_"):
                log.append(saved)
                log.append("kept_saved_traveller")
                if not _safe_continue(page):
                    log.append("passenger_continue_miss")
                _poll_ready(page, {"addons", "review", "pay", "passenger", "error"}, budget_ms=3000)
                after = _clear_front_sheet(page)
                if after:
                    log.append(after)
                continue
            notes = _fill_passenger_fields(page, profile)
            for extra in fill_profile_slots(page, profile):
                tag = extra.split(":", 1)[0]
                if _field_state(notes, tag) in {"", "miss", "none"}:
                    notes = [line for line in notes if not line.startswith(f"{tag}:")]
                    notes.append(extra)
            log.extend(notes)
            first = _field_state(notes, "first")
            email = _field_state(notes, "email")
            if first not in {"ok", "typed", "none", "skip"} or (
                first in {"none", "skip", ""} and email not in {"ok", "typed"}
            ):
                text, url, stage, facts = _snapshot()
                if (
                    sum(1 for line in log if line == "review_not_form") < 2
                    and re.search(r"fare summary", text or "", re.I)
                    and re.search(r"\bcontinue\b", text or "", re.I)
                ):
                    log.append("review_not_form")
                    if not _safe_continue(page):
                        log.append("passenger_continue_miss")
                    _poll_ready(page, {"passenger", "addons", "pay", "review", "error"}, budget_ms=4000)
                    after = _clear_front_sheet(page)
                    if after:
                        log.append(after)
                    continue
                log.append("passenger_unfilled")
                handoff = contact_report(" | ".join(notes), text) or (
                    "Passenger form is open, but the name did not go into the fields. Nothing paid."
                )
                return CheckoutOutcome("passenger", text, url, log, facts=facts, checks=checks, handoff=handoff)
            if not _passenger_ready(notes):
                text, url, stage, facts = _snapshot()
                missing = _passenger_missing(notes)
                handoff = (
                    "The traveller form is open, but "
                    + (", ".join(missing) if missing else "a field")
                    + " did not stick. Nothing paid."
                )
                return CheckoutOutcome("passenger", text, url, log, facts=facts, checks=checks, handoff=handoff)
            # Cleartrip traveller uses Continue to payment; plain Continue is the contact leftover.
            ctp = _continue_to_payment(page)
            log.append(f"continue_to_payment:{ctp}")
            if ctp == "expired":
                log.append("itinerary_expired")
                # Guest holds die in ~60s — one same-session rebook, then race to pay.
                if seller == "cleartrip" and recovers < max_recovers and hint:
                    recovers += 1
                    if _reopen_cleartrip_results(page, origin, destination, depart, log):
                        log.append("expired_rebook")
                        continue
                handoff = (
                    "Cleartrip's hold expired before payment. "
                    "Say the time again and I'll reopen the fare. Nothing paid."
                )
                text, url, stage, facts = _snapshot()
                return CheckoutOutcome("error", text, url, log, facts=facts, checks=checks, handoff=handoff)
            if ctp == "miss" and not _safe_continue(page):
                log.append("passenger_continue_miss")
            _poll_ready(page, {"addons", "review", "pay", "passenger", "error"}, budget_ms=4000)
            text, url, stage, facts = _snapshot()
            if stage == "pay" or payment_sheet_open(text, stage):
                log.append(f"pay_handoff:{stage}")
                handoff = (
                    f"Payment page ready. Flight {facts.get('flight') or '?'} "
                    f"{facts.get('depart_time') or hint} {facts.get('origin')}-{facts.get('destination')} "
                    f"total {facts.get('total') or '?'}. Link: {url}. No payment clicked."
                )
                return CheckoutOutcome("pay", text, url, log, facts=facts, checks=checks, handoff=handoff)
            after = _clear_front_sheet(page)
            if after:
                log.append(after)
            continue
        if kind == "click_time":
            card = ""
            try:
                pick = nearest_depart_hint(text, hint) if hint else hint
                if pick and pick != hint:
                    log.append(f"nearest:{hint}->{pick}")
                    hint = pick
                _prepare_ixigo_results(page, hint)
                card = str(page.evaluate(_CLICK_TIME, hint) or "")
                if not card and "ixigo.com" in (url or ""):
                    card = _click_ixigo_depart(page, hint)
                    log.append("ixigo_playwright_book")
            except Exception:
                card = ""
            log.append(f"card:{card[:120]}")
            after, after_url, after_stage, _af = _poll_ready(
                page,
                {"fare_sheet", "review", "passenger", "login", "otp", "pay", "error", "unavailable", "addons"},
                budget_ms=7000,
            )
            # Drawer over results still looks like a fare list in the DOM — re-read the front sheet.
            if after_stage == "results":
                try:
                    surface = read_surface(page)
                    front = apply_front_text(after, surface)
                    after_stage = classify_page(front, after_url, book_count=_book_count(page))
                    after = front
                except Exception:
                    pass
            if after_stage == "unavailable":
                return CheckoutOutcome("unavailable", after, after_url, log, checks=checks)
            if after_stage in {"addons", "review", "passenger", "pay"}:
                log.append(f"after_book_direct:{after_stage}")
                continue
            if after_stage == "fare_sheet":
                if hint and not (
                    card_matches_depart_hint(card, hint)
                    or card_matches_depart_hint(after, hint)
                ):
                    from app.browser_hands import time_in_card

                    if not time_in_card(after, hint):
                        return CheckoutOutcome("wrong_flight", after, after_url, log, checks=checks)
                ok = _confirm_fare(page)
                log.append(f"confirm_fare:{ok}")
                if not ok and seller == "cleartrip" and recovers < max_recovers:
                    recovers += 1
                    log.append("confirm_fare_rebook")
                    if _reopen_cleartrip_results(page, origin, destination, depart, log):
                        continue
                continue
            if card and after_stage in {"results", "other", "loading"}:
                if _advance_after_book(page, hint, log):
                    continue
                log.append("book_still_results")
                continue
            if not card and after_stage == "results":
                # One mouse Book before giving up — Cleartrip often ignores JS click.
                if hint and "cleartrip.com" in (after_url or url or "") and _mouse_book_depart(page, hint):
                    log.append("results:mouse_book")
                    _poll_ready(
                        page,
                        {"fare_sheet", "review", "passenger", "login", "otp", "pay", "error", "addons"},
                        budget_ms=5000,
                    )
                    continue
                return CheckoutOutcome("results", after, after_url, log, checks=checks)
            if after_stage not in {"review", "passenger", "fare_sheet", "login", "otp", "pay", "error", "addons"}:
                _poll_ready(
                    page,
                    {"review", "passenger", "fare_sheet", "login", "otp", "pay", "error", "addons"},
                    budget_ms=2500,
                )
            continue
        if kind == "search":
            _search(page, origin, destination)
            page.wait_for_timeout(2000)
            continue
        if kind == "click":
            target = action.get("target") or ""
            # "Continue to payment" is Cleartrip's traveller handoff — not a charge click.
            if _PAY_REFUSE.search(target) or (
                not allow_pay
                and re.search(r"\bpay\b", target, re.I)
                and not re.search(r"continue to payment", target, re.I)
            ):
                handoff = f"Refused pay control '{target}'. Link: {url}"
                return CheckoutOutcome(stage, text, url, log, facts=facts, checks=checks, handoff=handoff)
            if re.search(r"search again", target, re.I):
                return CheckoutOutcome(stage, text, url, log, facts=facts, checks=checks)
            if re.fullmatch(r"accept", target, re.I):
                for label in ("Accept", "Allow", "Agree", "Got it", "OK"):
                    if _click_label(page, label):
                        break
                page.wait_for_timeout(400)
                continue
            if stage == "addons" or re.search(r"skip add|continue without add", target, re.I):
                if not _leave_addons(page, log, rounds=3) and target and not re.search(
                    r"skip add", target, re.I
                ):
                    _click_label(page, target)
                continue
            if re.search(r"continue", target, re.I) and (
                stage == "fare_sheet"
                or _looks_like_fare_sheet(text)
                or any(line.startswith(("card:", "after_book:", "results_after_book")) for line in log[-10:])
            ):
                ok = _confirm_fare(page)
                text, url, left, facts = _snapshot()
                log.append(f"confirm_fare:{ok}")
                log.append(f"after_continue:{left}")
                if not ok and left in {"fare_sheet", "error"}:
                    log.append("continue_blocked")
                    if seller == "cleartrip" and recovers < max_recovers:
                        recovers += 1
                        if _reopen_cleartrip_results(page, origin, destination, depart, log):
                            continue
                    return CheckoutOutcome("error", text, url, log, facts=facts, checks=checks)
                continue
            if stage == "error":
                recovers += 1
                tapped = leave_site_error(page)
                log.append(f"error_tap:{tapped or 'miss'}")
                left = _wait_until(page, "error", rounds=6)
                log.append(f"after_retry:{left}")
                # Go to home often lands on empty search — still reopen fares and Book the hint.
                if seller == "cleartrip" and left in {"error", "home", "cookie", "other"}:
                    if not _reopen_cleartrip_results(page, origin, destination, depart, log):
                        break
                    continue
                if seller == "cleartrip" and left in {"results", "loading", "fare_sheet"}:
                    continue
                if recovers >= max_recovers:
                    break
                continue
            _click_label(page, target)
            _poll_ready(
                page,
                {"review", "passenger", "addons", "pay", "login", "otp", "fare_sheet", "error"},
                budget_ms=2500,
            )
            continue
    text, url, stage, facts = _snapshot()
    if stage == "pay" and not handoff:
        handoff = f"Payment handoff. Total {facts.get('total') or '?'}. Link: {url}. No payment clicked."
    if stage == "otp" and not handoff:
        handoff = "OTP required from traveller. Nothing paid."
    if stage == "login" and not handoff:
        handoff = "Login / mobile verification required before traveller form. Nothing paid."
    return CheckoutOutcome(stage, text, url, log, facts=facts, checks=checks, handoff=handoff)
