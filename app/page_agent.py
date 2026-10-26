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
    # Ixigo review also shows the "Add-ons" stepper label — require real add-on UI.
    if re.search(r"choose your seat|add meal|skip add-?ons|extra baggage", hay, re.I):
        return "addons"
    if re.search(r"first name|last name|date of birth|add adult|primary passenger", hay, re.I) and re.search(
        r"mobile|email|gender", hay, re.I
    ):
        return "passenger"
    if re.search(r"review & traveller|fare summary|total amount", hay, re.I) and re.search(
        r"\b(?:IX|AI|6E|SG|QP)\s?\d+", hay, re.I
    ):
        return "review"
    if re.search(r"passenger details|traveller details", hay, re.I):
        return "passenger"
    if re.search(r"select your fare", hay, re.I):
        return "fare_sheet"
    priced = bool(re.search(r"₹\s*[0-9]", hay))
    has_time = bool(re.search(r"\b\d{1,2}:\d{2}\b", hay))
    if re.search(r"we respect your privacy|accept all cookies|cookie policy", hay, re.I) and not (priced and has_time):
        return "cookie"
    if has_time and priced and (book_count >= 1 or re.search(r"\b(Book|Select|View fares)\b", hay)):
        return "results"
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
        return {"action": "click", "target": "Continue", "value": ""}
    if stage == "results" and hint:
        return {"action": "click_time", "target": hint, "value": ""}
    if stage == "loading":
        return {"action": "wait", "target": "", "value": ""}
    if stage == "home" and not _results_url(url):
        return {"action": "search", "target": "", "value": ""}
    return None


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


def _click_label(page, label: str) -> bool:
    wanted = (label or "").strip()
    if not wanted:
        return False
    try:
        return bool(
            page.evaluate(
                """(label) => {
                    const want = label.trim().toLowerCase();
                    const nodes = [...document.querySelectorAll('button,a,[role=button]')];
                    const exact = nodes.find(b => (b.innerText || '').trim().toLowerCase() === want);
                    const fuzzy = exact || nodes.find(b => (b.innerText || '').trim().toLowerCase().includes(want));
                    if (!fuzzy) return false;
                    fuzzy.scrollIntoView({block:'center'});
                    fuzzy.click();
                    return true;
                }""",
                wanted,
            )
        )
    except Exception:
        return False


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
        return "optout_miss"


def _safe_continue(page) -> bool:
    """Click Continue but never a Pay control."""
    try:
        btn = page.get_by_role("button", name=re.compile(r"^Continue", re.I)).last
        label = (btn.inner_text(timeout=2000) or "").strip()
        if re.search(r"\bpay\b", label, re.I):
            return False
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


def _fill_passenger_fields(page, profile: dict[str, str]) -> list[str]:
    notes: list[str] = []
    name = (profile.get("name") or "").strip()
    parts = name.split()
    first, last = (parts[0], " ".join(parts[1:])) if len(parts) >= 2 else (name, "")
    pairs = [
        (r"first\s*name|given", first),
        (r"last\s*name|surname", last),
        (r"email", profile.get("email") or ""),
        (r"mobile|phone|contact", profile.get("phone") or ""),
        (r"date of birth|dob|birth", profile.get("dob") or ""),
    ]
    for pattern, value in pairs:
        if not value:
            continue
        ok = False
        for getter in (
            lambda p=pattern: page.get_by_placeholder(re.compile(p, re.I)),
            lambda p=pattern: page.get_by_label(re.compile(p, re.I)),
        ):
            try:
                loc = getter()
                if loc.count():
                    loc.first.fill(value, timeout=3000)
                    ok = True
                    break
            except Exception:
                continue
        notes.append(f"{pattern}:{'ok' if ok else 'miss'}")
    gender = (profile.get("gender") or "").strip()
    if gender:
        try:
            page.get_by_text(re.compile(rf"^{re.escape(gender)}$", re.I)).first.click(timeout=2000)
            notes.append("gender:ok")
        except Exception:
            notes.append("gender:miss")
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

    def _snapshot() -> tuple[str, str, str, dict[str, str]]:
        t, u = _read(page)
        s = classify_page(t, u, book_count=_book_count(page))
        f = extract_trip_facts(t, u)
        return t, u, s, f

    for step in range(max_steps):
        page = _latest(page)
        text, url, stage, facts = _snapshot()
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
        if action is None and stage == "review" and sum(1 for line in log if "review_continue" in line) < 2:
            action = {"action": "review_continue", "target": "Continue", "value": ""}
        if action is None:
            buttons = ", ".join(_buttons(page)[:12])
            action = _ask_model(
                f"Seller {seller}. Trip {origin} to {destination} on {depart}. Time {hint or 'any'}.\n"
                f"Stage {stage}. URL {url}.\nButtons: {buttons}\nPage:\n{text[:1200]}"
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
            break
        if kind == "wait":
            page.wait_for_timeout(1200 if stage == "loading" else 600)
            continue
        if kind == "review_continue":
            _dismiss_overlays(page)
            log.append(_ixigo_opt_out_cancellation(page))
            if not _safe_continue(page):
                log.append("continue_refused_or_missing")
                break
            _poll_ready(page, {"login", "otp", "passenger", "addons", "pay", "error"}, budget_ms=3000)
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
            notes = _fill_passenger_fields(page, profile)
            log.extend(notes)
            if not _safe_continue(page):
                log.append("passenger_continue_miss")
            _poll_ready(page, {"addons", "review", "pay", "passenger", "error"}, budget_ms=3000)
            continue
        if kind == "click_time":
            card = ""
            try:
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
                {"fare_sheet", "review", "passenger", "login", "otp", "pay", "error"},
                budget_ms=4000,
            )
            if after_stage == "fare_sheet":
                if hint and not (
                    card_matches_depart_hint(card, hint)
                    or card_matches_depart_hint(after, hint)
                ):
                    from app.browser_hands import time_in_card

                    if not time_in_card(after, hint):
                        return CheckoutOutcome("wrong_flight", after, after_url, log, checks=checks)
            if not card and after_stage == "results":
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
            if stage == "fare_sheet" and re.search(r"continue", target, re.I):
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
