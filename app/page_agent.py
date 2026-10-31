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


def classify_page(text: str, url: str, *, book_count: int = 0) -> str:
    """Name the booking step from visible text. A nav 'Book' link is not a fare list."""
    hay = f"{text or ''}\n{url or ''}"
    if flight_unavailable(hay):
        return "unavailable"
    if re.search(r"itinerary/failure|server error|connection lost|been disconnected", hay, re.I):
        return "error"
    if re.search(
        r"enter otp|enter the otp|otp sent|otp received|verify your mobile|"
        r"one.?time password|resend otp|resend in \d+",
        hay,
        re.I,
    ):
        return "otp"
    if re.search(r"log in to ixigo|loginVisible=true|enter mobile number", hay, re.I) and re.search(
        r"continue|google", hay, re.I
    ):
        return "login"
    if re.search(r"pay to complete your booking|name on card|enter card number|scan qr code|payment options", hay, re.I):
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
    fare_list = bool(
        has_time and priced and (book_count >= 1 or re.search(r"\b(Book|Select|View fares)\b", hay))
    )
    # Fare lists win over add-on marketing copy / stepper labels on Cleartrip/Ixigo results.
    if fare_list and not flight_unavailable(hay):
        return "results"
    # Ixigo review also shows the "Add-ons" stepper label — require real add-on UI.
    if re.search(r"choose your seat|add meal|skip add-?ons|extra baggage", hay, re.I):
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
    if re.search(r"review & traveller|fare summary|total amount", hay, re.I) and re.search(
        r"\b(?:IX|AI|6E|SG|QP)[\s-]*\d+", hay, re.I
    ):
        return "review"
    if re.search(r"fare summary", hay, re.I) and re.search(r"\bcontinue\b", hay, re.I) and not re.search(
        r"first & middle|first name|last name", hay, re.I
    ):
        return "review"
    if re.search(r"first & middle|first name|last name|add adult|primary passenger", hay, re.I):
        return "passenger"
    if re.search(r"we respect your privacy|accept all cookies|cookie policy", hay, re.I) and not (priced and has_time):
        return "cookie"
    if _results_url(url):
        return "loading"
    if re.search(r"one way|search flight|where from", hay, re.I):
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
        return {"action": "click", "target": "Try again", "value": ""}
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
    const panels = [...document.querySelectorAll('div,section,aside')].filter((el) => {
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
        return True
    # JS Book sometimes paints no drawer — try a real mouse Book on the departure card.
    if hint and _mouse_book_depart(page, hint):
        log.append("after_book:mouse_book")
        try:
            page.wait_for_timeout(1200)
        except Exception:
            pass
        after, _au, after_stage, _af = _poll_ready(
            page,
            {"fare_sheet", "review", "passenger", "login", "otp", "pay", "error", "unavailable"},
            budget_ms=3500,
        )
        if after_stage == "fare_sheet" or _looks_like_fare_sheet(after):
            log.append("after_book:fare_sheet")
            ok = _confirm_fare(page)
            log.append(f"confirm_fare:{ok}")
            return True
        if after_stage in {"review", "passenger", "login", "otp", "pay"}:
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
    Denied and the sheet stays open.
    """
    create: dict[str, object] = {"status": None, "body": ""}

    def on_response(res) -> None:
        if _ITINERARY_CREATE not in (res.url or ""):
            return
        create["status"] = res.status
        try:
            create["body"] = (res.text() or "")[:2000]
        except Exception:
            create["body"] = ""

    try:
        page.on("response", on_response)
    except Exception:
        pass
    try:
        point = page.evaluate(
            """() => {
                const root = document.querySelector('[role="dialog"]') || document.body;
                const radio = [...root.querySelectorAll('input[type="radio"]')].find(r => !r.checked)
                    || root.querySelector('input[type="radio"]');
                if (radio) radio.click();
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
        return False
    if not isinstance(point, dict):
        return False
    try:
        page.mouse.click(float(point["x"]), float(point["y"]))
    except Exception:
        return False
    for _ in range(30):
        if create["status"] is not None:
            break
        try:
            page.wait_for_timeout(500)
        except Exception:
            break
    outcome = interpret_itinerary_create(
        create["status"] if isinstance(create["status"], int) else None,
        str(create.get("body") or ""),
    )
    logger.info(
        "Continue create status=%s outcome=%s",
        create.get("status"),
        outcome,
    )
    left = _wait_until(page, "fare_sheet", rounds=8)
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
    const nodes = [...root.querySelectorAll('button,a,[role=button],label,span')].filter(vis);
    for (const el of nodes) {
      if (textOf(el) !== want) continue;
      const r = el.getBoundingClientRect();
      if (r.width > innerWidth * 0.72 || r.height > innerHeight * 0.45) continue;
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


def click_verified(page, label: str) -> bool:
    """Mouse-click the smallest real control, then require the sheet to change.

    A dialog-sized box is never a target. Two candidates max. True only when the
    label disappears, the dialog closes, or the booking step changes.
    """
    wanted = (label or "").strip()
    if not wanted or wanted.startswith("__"):
        return False
    try:
        points = page.evaluate(_AIM_JS, wanted) or []
    except Exception:
        points = []
    if not isinstance(points, list) or not points:
        return False
    before = _aim_mark(page, wanted)
    for point in points[:2]:
        if not isinstance(point, dict):
            continue
        try:
            page.mouse.click(float(point["x"]), float(point["y"]))
        except Exception:
            continue
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            try:
                page.wait_for_timeout(250)
            except Exception:
                break
            if _aim_mark(page, wanted) != before:
                return True
    return False


def _click_label(page, label: str) -> bool:
    """Verified click. True only when the page actually changes."""
    return click_verified(page, label)


def _skip_addons(page) -> str:
    """Leave Cleartrip/Ixigo optional extras, including the missed meals/seats nag.

    Scrolls to find Skip when the seat map fills the viewport. Returns the label
    clicked, or empty when nothing matched.
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

    def _try() -> str:
        for label in labels:
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
        for label in ("Skip add-ons", "Skip add ons", "Skip"):
            if _click_label(page, label):
                clicked = label
                try:
                    page.wait_for_timeout(600)
                except Exception:
                    pass
                break
    return clicked


def _leave_addons(page, log: list[str], *, rounds: int = 3) -> bool:
    """Outcome loop: skip extras until Choose add-ons is gone."""
    from app.step_contract import left_addons

    for i in range(rounds):
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
        _poll_ready(
            page,
            {"review", "passenger", "pay", "login", "otp", "error", "unavailable"},
            budget_ms=3500,
        )
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
              const isPerson = (t) => {
                if (!t || t.length > 80) return false;
                if (/pay|assured|cancellation|seat|meal|baggage|continue|gst|email|mobile|contact/.test(t)) return false;
                return /\\b(mr|ms|mrs|mx)\\b/.test(t) || /[a-z]{2,}\\s+[a-z]{2,}/.test(t);
              };
              const rowText = (el) => {
                let node = el;
                for (let i = 0; i < 6 && node; i++) {
                  const t = ((node.innerText || node.getAttribute && node.getAttribute('aria-label')) || '')
                    .replace(/\\s+/g, ' ').trim().toLowerCase();
                  if (isPerson(t)) return t;
                  node = node.parentElement;
                }
                return '';
              };
              const boxes = [...document.querySelectorAll(
                'input[type=checkbox],[role=checkbox],input[type=radio]'
              )].filter(vis);
              const adults = boxes.filter((el) => isPerson(rowText(el)));
              const named = want
                ? adults.filter((el) => rowText(el).includes(want))
                : adults;
              const pool = named.length ? named : adults;
              const alreadyOn = pool.find((el) => el.checked || el.getAttribute('aria-checked') === 'true');
              if (alreadyOn) return 'already';
              const match = pool[0];
              if (!match && adults.length) return 'unticked';
              if (match) {
                match.click();
                return 'clicked';
              }
              const cards = [...document.querySelectorAll('label,button,div,li')].filter(vis);
              const card = cards.find((el) => {
                const t = (el.innerText || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                if (!isPerson(t)) return false;
                return !want || t.includes(want);
              });
              if (card) { card.click(); return 'card'; }
              return '';
            }""",
            wanted,
        )
        if picked:
            return f"traveller_{picked}"
    except Exception:
        return ""
    return ""


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


def _safe_continue(page) -> bool:
    """One mouse click on the visible Continue, Confirm, or Proceed control.

    The target is the smallest on-screen control with that exact label, never a
    Pay button and never a parent that merely contains the word. The center is
    checked with elementFromPoint so the click lands on that control.
    Buy This is an add-on purchase, not Continue, and it covers the real button.
    """
    if _ixigo_buy_this_visible(page):
        return False
    try:
        point = page.evaluate(
            """() => {
              const forward = /^(continue|confirm|proceed)$/i;
              const nodes = [...document.querySelectorAll('button, a, [role=button], div, span')];
              const hits = [];
              for (const el of nodes) {
                const own = [...el.childNodes]
                  .filter(n => n.nodeType === 3)
                  .map(n => n.textContent || '')
                  .join(' ')
                  .replace(/\\s+/g, ' ')
                  .trim();
                const text = (el.innerText || '').replace(/\\s+/g, ' ').trim();
                const label = own || (text.length <= 24 ? text : '');
                if (!forward.test(label) || /\\bpay\\b/i.test(text)) continue;
                const style = window.getComputedStyle(el);
                if (style.visibility === 'hidden' || style.display === 'none' || style.pointerEvents === 'none') continue;
                const rect = el.getBoundingClientRect();
                if (rect.width < 48 || rect.height < 28 || rect.width > 480) continue;
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
        btn = page.get_by_role("button", name=re.compile(r"^(Continue|Confirm|Proceed)$", re.I)).last
        label = (btn.inner_text(timeout=2000) or "").strip()
        if re.search(r"\bpay\b", label, re.I):
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
                return r.width > 8 && r.height > 8 && s.visibility !== 'hidden' && s.display !== 'none';
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
              return [...document.querySelectorAll('input, textarea')].filter(el => vis(el) && !/hidden|radio|checkbox|submit/i.test(el.type || '')).map((el, index) => ({
                index: String(index),
                label: labelOf(el),
                value: el.value || '',
              }));
            }"""
        )
    except Exception:
        return []
    return [row for row in (rows or []) if isinstance(row, dict)]


def _set_box(page, index: int, value: str) -> str:
    """Set one input by its position. ok only when that box keeps the text."""
    try:
        stuck = page.evaluate(
            """({index, value}) => {
              const vis = (el) => {
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 8 && r.height > 8 && s.visibility !== 'hidden' && s.display !== 'none';
              };
              const inputs = [...document.querySelectorAll('input, textarea')].filter(
                el => vis(el) && !/hidden|radio|checkbox|submit/i.test(el.type || '')
              );
              const el = inputs[index];
              if (!el) return 'gone';
              el.focus();
              el.scrollIntoView({block: 'center'});
              const proto = el instanceof HTMLTextAreaElement
                ? Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value')
                : Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');
              if (proto && proto.set) proto.set.call(el, value);
              else el.value = value;
              el.dispatchEvent(new Event('input', {bubbles: true}));
              el.dispatchEvent(new Event('change', {bubbles: true}));
              return (el.value || '') === value ? 'ok' : 'soft';
            }""",
            {"index": index, "value": value},
        )
    except Exception:
        return "miss"
    if stuck == "ok":
        return "ok"
    if stuck == "gone":
        return "miss"
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
              if (inputs[index]) inputs[index].setAttribute('data-guide-fill', '1');
            }""",
            index,
        )
        box = page.locator("[data-guide-fill='1']")
        box.click(timeout=2000)
        page.keyboard.press("Control+A")
        page.keyboard.insert_text(value)
        got = (box.input_value(timeout=1500) or "").strip()
        return "ok" if got == value.strip() else "miss"
    except Exception:
        return "miss"


def _type_labeled(page, labels: tuple[str, ...], value: str, *, optional: bool = False) -> str:
    """Type into the box whose wrapper text is exactly this label."""
    if not value:
        return "skip"
    wanted = {item.lower() for item in labels}
    match = next((row for row in _field_boxes(page) if str(row.get("label") or "").strip().lower() in wanted), None)
    if match is None:
        return "none" if optional else "miss"
    if str(match.get("value") or "").strip() == value.strip():
        return "ok"
    return _set_box(page, int(match["index"]), value)


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
    """Title, name, and email kept their values. A missing birth-date box is not a failure."""
    for tag in ("title", "first", "email"):
        if _field_state(notes, tag) not in {"ok", "typed", "skip", "none"}:
            return False
    if _field_state(notes, "last") in {"miss", "set"}:
        return False
    if _field_state(notes, "dob") in {"miss", "set"}:
        return False
    return True


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
    notes = [
        f"title:{title_status}",
        f"first:{_type_labeled(page, ('first & middle name', 'first name'), first)}",
        f"last:{_type_labeled(page, ('last name',), last)}",
        f"email:{_type_labeled(page, ('email',), profile.get('email') or '')}",
        f"phone:{_type_labeled(page, ('mobile number',), phone)}",
        f"dob:{_type_labeled(page, ('date of birth', 'dob'), dob, optional=True)}",
    ]
    if title_menu:
        notes.append(f"title_menu:{title_menu[:120]}")
    return notes


_PAY_REFUSE = re.compile(r"pay now|pay to complete|make payment|complete payment|pay securely", re.I)


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
    deadline = time.monotonic() + 70

    def _snapshot() -> tuple[str, str, str, dict[str, str]]:
        t, u = _read(page)
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
        if time.monotonic() > deadline:
            log.append("checkout_deadline")
            handoff = handoff or (
                "This step took too long, so I stopped and sent the page. Nothing paid."
            )
            return CheckoutOutcome(stage, text, url, log, facts=facts, checks=checks, handoff=handoff)
        # Add-ons skip is an outcome loop, not a 16-step vision tour. Reply within ~20s.
        skip_now = bool(
            stage == "addons"
            or re.search(r"choose add-ons|add seats|missed adding meal", text or "", re.I)
        ) and (
            stage == "addons"
            or re.search(r"skip add", tap_pending or chosen_tap or "", re.I)
        )
        if skip_now:
            log.append("addons_fast")
            left = _leave_addons(page, log, rounds=3)
            text, url, stage, facts = _snapshot()
            tap_pending = ""
            if left and stage != "addons":
                log.append(f"addons_fast_left:{stage}")
            else:
                handoff = (
                    "Skip add-ons is on screen, but the click did not close that sheet. "
                    "The seat map may still be showing. Nothing paid."
                )
                log.append("addons_fast_still")
                return CheckoutOutcome("addons", text, url, log, facts=facts, checks=checks, handoff=handoff)
        # Vision on every step: screenshot overrides a wrong DOM toast / sold-out miss.
        try:
            from app.vision_agent import capture_page_shot, classify_with_vision, vision_enabled

            if vision_enabled():
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
                    if new_stage and new_stage != stage:
                        log.append(f"{step}:vision_override:{stage}->{new_stage}")
                        stage = new_stage
                    try:
                        Path(shot).unlink(missing_ok=True)
                    except Exception:
                        pass
            else:
                log.append(f"{step}:vision_off")
        except Exception as exc:
            log.append(f"{step}:vision_err:{type(exc).__name__}")
        if tap_pending:
            label = tap_pending
            tap_pending = ""
            if label.startswith("__"):
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
        if stage == "error" and recovers >= 1:
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
            log.append(f"continue_click:{_safe_continue(page)}")
            _poll_ready(
                page,
                {"login", "otp", "passenger", "addons", "pay", "error"},
                budget_ms=6000,
            )
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
            if saved == "traveller_unticked":
                log.append(saved)
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
            log.extend(notes)
            if not any(item.startswith(("first:ok", "first:typed")) for item in notes):
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
                handoff = "Passenger form is open, but the name did not go into the fields. Nothing paid."
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
            if not _safe_continue(page):
                log.append("passenger_continue_miss")
            _poll_ready(page, {"addons", "review", "pay", "passenger", "error"}, budget_ms=3000)
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
                {"fare_sheet", "review", "passenger", "login", "otp", "pay", "error", "unavailable"},
                budget_ms=5000,
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
                        {"fare_sheet", "review", "passenger", "login", "otp", "pay", "error"},
                        budget_ms=4000,
                    )
                    continue
                return CheckoutOutcome("results", after, after_url, log, checks=checks)
            if after_stage not in {"review", "passenger", "fare_sheet", "login", "otp", "pay", "error"}:
                _poll_ready(
                    page,
                    {"review", "passenger", "fare_sheet", "login", "otp", "pay", "error"},
                    budget_ms=2500,
                )
            continue
        if kind == "search":
            _search(page, origin, destination)
            page.wait_for_timeout(2000)
            continue
        if kind == "click":
            target = action.get("target") or ""
            if _PAY_REFUSE.search(target) or (not allow_pay and re.search(r"\bpay\b", target, re.I)):
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
                if not ok and left == "fare_sheet":
                    return CheckoutOutcome("error", text, url, log + ["continue_blocked"], facts=facts, checks=checks)
                continue
            if stage == "error":
                recovers += 1
                _click_label(page, target or "Try again")
                left = _wait_until(page, "error", rounds=8)
                log.append(f"after_retry:{left}")
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
