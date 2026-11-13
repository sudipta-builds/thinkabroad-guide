"""WhatsApp login on Kernel for Ixigo (6-digit OTP) and Cleartrip (4-digit OTP).

The browser stays open until the traveller replies with the code. OTP digits are
never written to logs. Payment is not part of this flow.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

from app.config import get_settings
from app.page_agent import (
    _book_count,
    _latest,
    classify_page,
    ixigo_auth_session_ready,
    read_visible_page,
    run_checkout_agent,
    site_error_page,
)


def _read_visible(page) -> tuple[str, str]:
    text, url, _surface = read_visible_page(page)
    return text, url
from app.search import cleartrip_results_url, ixigo_results_url

KERNEL_SELLERS = {"ixigo", "cleartrip"}
_MID_BOOKING = {"passenger", "review", "addons", "pay", "login", "otp"}


class _RetryBeginLogin(Exception):
    """Leave sync_playwright, then call begin_login again with these kwargs."""

    def __init__(self, kwargs: dict[str, Any]) -> None:
        super().__init__("retry begin_login")
        self.kwargs = kwargs


def should_hunt_login(stage: str) -> bool:
    """Open a mobile field only when this sheet is login. Continue on extras is not login."""
    return stage in {"login", "otp"}


def should_type_login_phone(stage: str, text: str) -> bool:
    """Type the saved mobile on the Ixigo sign-in drawer.

    That drawer says Enter Mobile Number while Fare Summary / Review & Traveller
    stays behind it. The words "mobile number" must not be treated as a contact form.
    """
    hay = text or ""
    if re.search(r"log in to ixigo", hay, re.I) and not re.search(
        r"verify your mobile|enter the otp|enter otp|otp sent", hay, re.I
    ):
        return True
    if stage not in {"login", "otp"}:
        return False
    from app.step_read import form_sheet

    if form_sheet(hay) or re.search(r"add contact details|email address|review traveller", hay, re.I):
        return False
    return True


def after_checkout_status(stage: str) -> str:
    """Kernel success is the live page after the click, not a stored OTP step."""
    if should_hunt_login(stage):
        return "auth"
    if stage == "error":
        return "dead"
    return "held"
# Legacy second connection. The prod login keeps the original CDP socket instead.
_HOLDS: dict[str, dict[str, Any]] = {}
_HOLD_LOCK = threading.Lock()
# session_id -> {"event", "code"} for the thread that still holds the first CDP connection.
_WAITERS: dict[str, dict[str, Any]] = {}
_WAIT_LOCK = threading.Lock()
# session_id -> monotonic time once that connection already accepted a code.
_HANDED: dict[str, float] = {}


def kernel_configured() -> bool:
    return bool((get_settings().kernel_api_key or os.getenv("KERNEL_API_KEY") or "").strip())


def should_resume_kernel_otp(state: dict[str, Any], text: str) -> bool:
    if not state.get("kernel_awaiting_otp") or not state.get("kernel_session_id"):
        return False
    return bool(re.fullmatch(r"\d{4,8}", (text or "").strip()))


def arm_live_otp(session_id: str) -> None:
    """The open login thread will type whatever offer_live_otp stores."""
    with _WAIT_LOCK:
        _HANDED.pop(session_id, None)
        _WAITERS[session_id] = {"event": threading.Event(), "code": ""}


def offer_live_otp(session_id: str, code: str) -> bool:
    """Hand a WhatsApp code to the still-open login. False when that connection is gone."""
    digits = re.sub(r"\D", "", code or "")
    if not session_id or not digits:
        return False
    with _WAIT_LOCK:
        slot = _WAITERS.get(session_id)
        if slot is not None:
            if not slot.get("code"):
                slot["code"] = digits
                slot["event"].set()
            return True
        handed = _HANDED.get(session_id)
    return bool(handed and (time.monotonic() - handed) < 120)


def poll_live_otp(session_id: str) -> str | None:
    """Digits once the traveller has replied. None while the login is still waiting."""
    with _WAIT_LOCK:
        slot = _WAITERS.get(session_id)
        if not slot or not slot["event"].is_set():
            return None
        _WAITERS.pop(session_id, None)
        digits = str(slot.get("code") or "")
        if digits:
            _HANDED[session_id] = time.monotonic()
    return digits


def cancel_live_otp(session_id: str) -> None:
    with _WAIT_LOCK:
        slot = _WAITERS.pop(session_id, None)
    if slot is not None:
        slot["event"].set()


def handoff_whatsapp_otp(state: dict[str, Any], text: str) -> bool:
    """True when the original login connection will type this code itself."""
    if not should_resume_kernel_otp(state, text):
        return False
    return offer_live_otp(str(state.get("kernel_session_id") or ""), text)


def viewport_click(metrics: dict[str, float], box: dict[str, Any]) -> tuple[int, int]:
    """Kernel mouse point. The CSS center of the element.

    The local login landed when the click equalled that center. Scaling by
    devicePixelRatio or the window chrome offset misses the small OTP boxes
    while still hitting the wide mobile field.
    """
    del metrics
    return (int(float(box["x"])), int(float(box["y"])))


def _ixigo_otp_finished_on_fares(text: str, url: str = "") -> bool:
    """The code drawer closed onto the fare list. That is a finished login, not a new code.

    Ixigo keeps loginVisible=true on the results URL after the drawer closes.
    That query, and a Log in header, must not veto a visible fare list.
    Callers still ignore this while the OTP boxes are on screen: the list
    behind an open drawer also has Book rows.
    """
    del url
    hay = text or ""
    drawer = bool(
        re.search(
            r"enter the otp|verify your mobile|otp sent to|enter mobile number|log in to ixigo",
            hay,
            re.I,
        )
    )
    books = len(re.findall(r"\bbook\b", hay, re.I))
    clocks = bool(re.search(r"\b\d{1,2}:\d{2}\b", hay))
    if books >= 1 and clocks and not drawer:
        return True
    # Leftover drawer copy in the same text loses to several Book rows.
    return bool(books >= 2 and clocks)


def otp_failure_detail(*, boxes_open: bool, verifying: bool = False) -> str:
    if boxes_open:
        return (
            "That OTP is incorrect. The code boxes are still open. "
            "Reply with a new code and I'll type it on this same screen. Nothing paid."
        )
    if verifying:
        return "Ixigo is still checking that code. The account did not appear."
    return "The code screen closed, but Ixigo still shows Log in. Say Ixigo again for a new code."


def resume_kernel_otp(state: dict[str, Any], text: str) -> str:
    """What WhatsApp should do with a code. Prefer the live login connection."""
    if not should_resume_kernel_otp(state, text):
        return ""
    seller = str(state.get("kernel_seller") or "ixigo")
    if seller in KERNEL_SELLERS and not otp_accepted(seller, text):
        return "need_digits"
    if handoff_whatsapp_otp(state, text):
        return "type"
    # Lab restart drops the in-memory waiter; reconnect and type on the same Kernel tab.
    return "reconnect"


def keep_kernel_otp_session(status: str) -> bool:
    """The login page is still waiting, so the same Kernel browser should stay up."""
    return status in {"bad_code", "still_otp", "boxes_miss"}


def checkout_step_budget(seller: str, chosen_tap: str, passenger: dict | None) -> int:
    """How many checkout clicks this pass may take. Direct Ixigo checkout is one turn."""
    if re.search(r"skip add", chosen_tap or "", re.I):
        return 6
    if seller == "cleartrip":
        return 28
    if str((passenger or {}).get("direct_checkout") or "") == "1":
        return 32
    return 16


def keep_kernel_browser(status: str) -> bool:
    """Leave the Kernel Chrome up so the next WhatsApp turn can click the same sheet."""
    return status in {"otp_sent", "held", "already_logged_in", "fares"}


def otp_accepted(seller: str, code: str) -> bool:
    digits = re.sub(r"\D", "", code or "")
    if seller == "cleartrip":
        return len(digits) == 4
    if seller == "ixigo":
        return len(digits) == 6
    return False


def indian_national(value: str) -> str:
    """10-digit Indian mobile, or empty when the text is not one.

    A long WhatsApp user id is not treated as a phone.
    """
    compact = re.sub(r"[\s\-()]", "", value or "")
    match = re.search(r"(?:\+91|91)?([6-9]\d{9})(?!\d)", compact)
    if not match:
        return ""
    start = match.start(1)
    prefix = compact[:start]
    if prefix and prefix[-1].isdigit() and not prefix.endswith("91"):
        return ""
    return match.group(1)


def login_phone_for(whatsapp_phone: str, explicit: str = "") -> str:
    """Indian mobile from the traveller's saved number or WhatsApp id.

    A Meta WhatsApp id that is not a 10-digit Indian mobile returns empty so
    the chat can ask for the number. Server env IXIGO_LOGIN_PHONE is not used —
    that would force one number onto every traveller.
    """
    for candidate in (explicit, whatsapp_phone):
        national = indian_national(candidate)
        if national:
            return "+91" + national
    return ""


def _client():
    from kernel import Kernel

    key = (get_settings().kernel_api_key or os.getenv("KERNEL_API_KEY") or "").strip()
    return Kernel(api_key=key)


def is_resume_booking_url(url: str, seller: str = "") -> bool:
    """A checkout link we can reopen. A fare-list URL or failure page is not one."""
    raw = (url or "").strip()
    if re.search(r"itinerary/failure|acting.?up|access.?denied", raw, re.I):
        return False
    ixigo = bool(re.search(r"https?://(?:www\.)?ixigo\.com/flight/booking/\S+", raw, re.I))
    cleartrip = bool(
        re.search(r"https?://(?:www\.)?cleartrip\.com/\S*(?:itinerary|/flights/)\S*", raw, re.I)
    )
    if seller == "ixigo":
        return ixigo
    if seller == "cleartrip":
        return cleartrip
    return ixigo or cleartrip


def _profile(seller: str, whatsapp_phone: str, *, nonce: str = "") -> str:
    digits = re.sub(r"\D", "", whatsapp_phone or "")[-12:] or "user"
    base = f"wa-{seller}-{digits}"
    if nonce:
        return f"{base}-e{nonce}"[:60]
    return base[:60]


def _metrics(page) -> dict[str, float]:
    return page.evaluate(
        """() => ({
          innerW: window.innerWidth, innerH: window.innerHeight,
          outerW: window.outerWidth, outerH: window.outerHeight,
          screenX: window.screenX || 0, screenY: window.screenY || 0,
          dpr: window.devicePixelRatio || 1
        })"""
    )


def verify_fallback(boxes: list[dict[str, Any]]) -> dict[str, float] | None:
    """Point on the orange Verify bar under the OTP row when the button text is missed."""
    if not boxes:
        return None
    return {
        "x": sum(float(box["x"]) for box in boxes) / len(boxes),
        "y": max(float(box["y"]) for box in boxes) + 110,
    }


def _click(client, sid: str, page, box: dict[str, Any]) -> None:
    x, y = viewport_click(_metrics(page), box)
    client.browsers.computer.click_mouse(sid, x=x, y=y, button="left", num_clicks=1)


def _find(page, kind: str, seller: str):
    return page.evaluate(
        """({kind, seller}) => {
          const vis = (el) => {
            const r = el.getBoundingClientRect();
            const s = getComputedStyle(el);
            return r.width > 8 && r.height > 8 && s.visibility !== 'hidden' && s.display !== 'none' && r.top < innerHeight && r.bottom > 0;
          };
          const text = (el) => (el.innerText || el.getAttribute('aria-label') || el.placeholder || '').replace(/\\s+/g, ' ').trim();
          const box = (el) => {
            const r = el.getBoundingClientRect();
            return {x: r.x + r.width/2, y: r.y + r.height/2, t: text(el).slice(0, 40)};
          };
          const nodes = [...document.querySelectorAll('button,a,[role=button],input,div,span')].filter(vis);
          if (kind === 'login') {
            const hits = nodes.filter(el => /^(log\\s*in\\s*\\/?\\s*sign\\s*up|log\\s*in|sign\\s*up|sign\\s*in|login)$/i.test(text(el)));
            const header = seller === 'cleartrip' ? hits.filter(el => el.getBoundingClientRect().y < 180) : hits;
            const pool = header.length ? header : [];
            pool.sort((a,b) => text(a).length - text(b).length || a.getBoundingClientRect().y - b.getBoundingClientRect().y);
            return pool[0] ? box(pool[0]) : null;
          }
          if (kind === 'phone') {
            const inputs = [...document.querySelectorAll('input')].filter(vis);
            const hit = inputs.find(i => /mobile|phone/i.test((i.placeholder||'')+(i.name||'')+(i.id||'')))
              || inputs.find(i => (i.type||'') === 'tel');
            return hit ? box(hit) : null;
          }
          if (kind === 'send') {
            const inputs = [...document.querySelectorAll('input')].filter(vis);
            const phone = inputs.find(i => /mobile|phone/i.test((i.placeholder||'')+(i.name||'')));
            const hits = [...document.querySelectorAll('button,[role=button]')].filter(vis).filter(b => {
              const t = text(b);
              return seller === 'cleartrip' ? /^get otp$/i.test(t) : /^(continue|get otp|send otp)$/i.test(t);
            });
            if (!hits.length) return null;
            if (phone) {
              const pr = phone.getBoundingClientRect();
              const py = pr.y + pr.height/2, px = pr.x + pr.width/2;
              hits.sort((a,b) => {
                const ar = a.getBoundingClientRect(), br = b.getBoundingClientRect();
                return (Math.abs(ar.y-py)+Math.abs(ar.x-px)) - (Math.abs(br.y-py)+Math.abs(br.x-px));
              });
            }
            return box(hits[0]);
          }
          if (kind === 'otp') {
            const inputs = [...document.querySelectorAll('input')].filter(vis);
            const need = seller === 'cleartrip' ? 4 : 6;
            let boxes = inputs.filter(i => {
              const r = i.getBoundingClientRect();
              const ml = i.getAttribute('maxlength');
              const mode = (i.getAttribute('inputmode') || '') + (i.autocomplete || '') + (i.name || '') + (i.id || '');
              if (seller === 'cleartrip') return r.width > 40 && r.width < 70 && r.height > 40 && r.height < 70;
              return ml === '1' || /one-time|otp|numeric/i.test(mode)
                || (r.width >= 24 && r.width <= 72 && r.height >= 24 && r.height <= 72);
            });
            boxes.sort((a,b) => a.getBoundingClientRect().x - b.getBoundingClientRect().x);
            if (boxes.length > need) boxes = boxes.slice(-need);
            return boxes.length ? boxes.map(el => {
              const b = box(el);
              b.v = (el.value || '').slice(0, 4);
              return b;
            }) : null;
          }
          if (kind === 'verify') {
            const hits = [...document.querySelectorAll('button,[role=button]')].filter(vis).filter(b => /^verify$/i.test(text(b)));
            if (!hits.length) return null;
            hits.sort((a,b) => b.getBoundingClientRect().x - a.getBoundingClientRect().x);
            return box(hits[0]);
          }
          return null;
        }""",
        {"kind": kind, "seller": seller},
    )


def _code_boxes_open(page, seller: str) -> bool:
    found = _find(page, "otp", seller) or []
    need = 4 if seller == "cleartrip" else 6
    return len(found) >= need


def ixigo_verify_in_progress(text: str) -> bool:
    """Ixigo's spinner after Verify. The code screen is already gone."""
    return bool(re.search(r"please wait while we verify|verifying your details", text or "", re.I))


def _otp_visible(page, seller: str) -> bool:
    text, _url = _read_visible(page)
    if seller == "ixigo" and ixigo_verify_in_progress(text):
        return False
    if seller == "cleartrip":
        return bool(re.search(r"enter otp", text or "", re.I))
    stage = classify_page(text, _url, book_count=_book_count(page))
    return stage == "otp" or bool(re.search(r"enter otp|verify your mobile|otp sent", text or "", re.I))


def cleartrip_looks_logged_in(text: str, url: str = "") -> bool:
    """Header My Account is on the logged-out homepage. Logout is the real session."""
    hay = f"{text or ''}\n{url or ''}"
    if re.search(r"enter otp|enter mobile number", hay, re.I):
        return False
    if re.search(r"log\s*in|sign\s*up", text or "", re.I) and not re.search(r"\blog\s*out\b", text or "", re.I):
        return False
    if re.search(r"one way|search flights|where from|book domestic", text or "", re.I):
        if not re.search(r"/flights/results|itinerary", url or "", re.I):
            return False
    return bool(re.search(r"\blog\s*out\b", text or "", re.I))


def _logged_in(page, seller: str) -> bool:
    text, url = _read_visible(page)
    if seller == "ixigo":
        if ixigo_auth_session_ready(page):
            return True
        if re.search(r"loginVisible=true", url or "", re.I):
            return False
        if re.search(r"log\s*in\s*/?\s*sign\s*up|login/signup", text or "", re.I):
            return False
        return bool(re.search(r"\blog\s*out\b|view profile|my profile|ixigoer", text or "", re.I))
    return cleartrip_looks_logged_in(text, url)


def _reload_booking_without_login_visible(page) -> None:
    try:
        cur = str(page.url or "")
        if "loginVisible=true" not in cur:
            return
        cleaned = re.sub(r"([?&])loginVisible=true&?", r"\1", cur)
        cleaned = cleaned.replace("?&", "?").rstrip("?&")
        page.goto(cleaned or cur, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)
    except Exception:
        pass


def _open_browser(client, profile: str, *, ephemeral: bool = False):
    try:
        client.profiles.create(name=profile)
    except Exception:
        pass
    return client.browsers.create(
        stealth=True,
        kiosk_mode=True,
        viewport={"width": 1280, "height": 800, "refresh_rate": 25},
        timeout_seconds=1000,
        # Ephemeral profiles skip cookie reuse after a dead itinerary / stumped page.
        profile={"name": profile, "save_changes": not ephemeral},
    )


def _connect_browser(client, profile: str, session_id: str = "", *, ephemeral: bool = False):
    """Reuse the open Kernel tab when WhatsApp is still on the same booking."""
    if session_id and not ephemeral:
        try:
            browser = client.browsers.retrieve(session_id)
            if browser is not None:
                return browser, True
        except Exception:
            pass
    return _open_browser(client, profile, ephemeral=ephemeral), False


def _drop(client, session_id: str) -> None:
    _stop_hold(session_id)
    try:
        client.browsers.delete_by_id(session_id)
    except Exception:
        pass


def release_saved_kernel(session_id: str) -> None:
    """Delete the remote browser. Chat memory is cleared by the caller."""
    sid = (session_id or "").strip()
    if not sid or not kernel_configured():
        return
    try:
        _drop(_client(), sid)
    except Exception:
        pass


def kernel_failure_line(seller_label: str, detail: str) -> str:
    """WhatsApp line for a Kernel failure. A CDP timeout is not a login form."""
    if re.search(r"Timeout|connect_over_cdp|TimeoutError", detail or "", re.I):
        return (
            f"{seller_label} browser didn't connect. "
            "Say the flight time again and I'll open a new one. Nothing paid."
        )
    return f"I couldn't open {seller_label} ({detail}). Nothing paid."


def _start_hold(session_id: str, cdp_ws_url: str) -> None:
    """Hold a Playwright CDP socket open until the traveller replies with the code."""
    _stop_hold(session_id)
    stop = threading.Event()
    ready = threading.Event()

    def _run() -> None:
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as p:
                browser = p.chromium.connect_over_cdp(cdp_ws_url)
                ready.set()
                while not stop.wait(12):
                    try:
                        contexts = browser.contexts
                        if contexts and contexts[0].pages:
                            _ = contexts[0].pages[0].url
                    except Exception:
                        break
                # Disconnect only. browser.close() shuts the Kernel Chrome and
                # kills the Ixigo page before the OTP can be submitted.
        except Exception:
            ready.set()

    thread = threading.Thread(target=_run, name=f"kernel-hold-{session_id[:8]}", daemon=True)
    with _HOLD_LOCK:
        _HOLDS[session_id] = {"stop": stop, "thread": thread}
    thread.start()
    ready.wait(timeout=25)


def _stop_hold(session_id: str) -> None:
    with _HOLD_LOCK:
        hold = _HOLDS.pop(session_id, None)
    if not hold:
        return
    stop = hold.get("stop")
    thread = hold.get("thread")
    if stop is not None:
        stop.set()
    if thread is not None:
        thread.join(timeout=8)


def _await_live_otp(page, session_id: str, timeout_sec: int) -> str:
    """Block on the same page until WhatsApp hands over a code. Empty if it never arrives."""
    deadline = time.monotonic() + max(1, timeout_sec)
    while time.monotonic() < deadline:
        found = poll_live_otp(session_id)
        if found:
            return found
        if found == "":
            return ""
        with _WAIT_LOCK:
            slot = _WAITERS.get(session_id)
            event = slot["event"] if slot else None
        if event is None:
            return ""
        try:
            _ = page.url
        except Exception:
            cancel_live_otp(session_id)
            return ""
        event.wait(min(1.2, max(0.05, deadline - time.monotonic())))
    cancel_live_otp(session_id)
    return ""


def begin_login(
    *,
    seller: str,
    whatsapp_phone: str,
    origin: str = "Guwahati",
    destination: str = "Delhi",
    depart: str = "2026-12-04",
    hint: str = "1:15",
    explicit: str = "",
    profile: dict[str, str] | None = None,
    on_code_requested: Callable[[dict[str, str]], None] | None = None,
    on_otp_result: Callable[[dict[str, str]], None] | None = None,
    otp_wait_sec: int = 300,
    resume_url: str = "",
    chosen_tap: str = "",
    session_id: str = "",
    error_retry: int = 0,
    fresh_browser: bool = False,
    require_otp: bool = False,
) -> dict[str, str]:
    """Open the seller and request one OTP.

    With on_code_requested, the same CDP connection waits for the WhatsApp code,
    types it, and clicks Verify. That is the path that produced an Ixigo account.
    Site error / expired itinerary pages reopen on a fresh Kernel tab (no cookies)
    instead of surfacing the dead sheet to WhatsApp.
    """
    if seller not in KERNEL_SELLERS:
        return {"status": "unsupported", "detail": seller}
    login_phone = login_phone_for(whatsapp_phone, explicit)
    national = indian_national(login_phone)
    # A fare list does not need a mobile. The code is only for a chosen flight.
    if hint and len(national) != 10:
        return {"status": "bad_phone", "detail": "Need a 10-digit Indian mobile."}
    passenger = dict(profile or {})
    auth_mode = str(passenger.pop("auth_mode", "") or "").strip().lower()
    # Guest checkout must not hunt OTP. Login mode (or blank legacy) may request a code.
    hunt_otp = auth_mode != "guest"
    # Stumped / expired / empty shell: drop the tab and reopen cookieless. Same seller.
    max_error_retry = 2 if hint else 0
    guest_profile = {**dict(profile or {}), "auth_mode": auth_mode} if auth_mode else dict(profile or {})
    client = _client()
    ephemeral = bool(fresh_browser or error_retry > 0)
    browser_profile = _profile(
        seller,
        whatsapp_phone,
        nonce=f"{error_retry}{int(time.time()) % 100000}" if ephemeral else "",
    )
    browser, reused = _connect_browser(
        client, browser_profile, "" if ephemeral else session_id, ephemeral=ephemeral
    )
    sid = browser.session_id
    os.environ["IXIGO_OTPLESS_BRIDGE"] = "0"
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            try:
                pw = p.chromium.connect_over_cdp(browser.cdp_ws_url, timeout=90_000)
            except Exception:
                _drop(client, sid)
                browser = _open_browser(client, browser_profile)
                reused = False
                sid = browser.session_id
                pw = p.chromium.connect_over_cdp(browser.cdp_ws_url, timeout=90_000)
            context = pw.contexts[0] if pw.contexts else pw.new_context()
            page = context.pages[0] if context.pages else context.new_page()
            outcome: dict[str, str] | None = None
            checkout = None
            checkout_notes = ""
            resume_kept = False
            dead_error = False
            skip_tap = bool(re.search(r"skip add", chosen_tap or "", re.I))
            # "Book" means pick a clock on the fare list — never reopen a stale itinerary
            # (that is how 1:15 go-on landed on a leftover 20:15 Review page).
            book_tap = bool(re.fullmatch(r"book", (chosen_tap or "").strip(), re.I))
            text, url = _read_visible(page)
            stage = classify_page(text, url, book_count=_book_count(page))
            dead_resume = bool(
                reused
                and session_id
                and not re.search(r"go to home", chosen_tap or "", re.I)
                and (
                    stage == "error"
                    or site_error_page(text, url)
                    or "failure" in (url or "").lower()
                )
            )
            def _fresh_kwargs(*, bump: bool) -> dict[str, Any]:
                return {
                    "seller": seller,
                    "whatsapp_phone": whatsapp_phone,
                    "explicit": explicit,
                    "origin": origin,
                    "destination": destination,
                    "depart": depart,
                    "hint": hint,
                    "profile": guest_profile,
                    "on_code_requested": on_code_requested,
                    "on_otp_result": on_otp_result,
                    "otp_wait_sec": otp_wait_sec,
                    "resume_url": "",
                    "chosen_tap": "",
                    "session_id": "",
                    "error_retry": error_retry + (1 if bump else 0),
                    "fresh_browser": True,
                    "require_otp": require_otp,
                }

            if dead_resume:
                try:
                    pw.close()
                except Exception:
                    pass
                _drop(client, sid)
                # Dead tab on resume — same retry budget, always cookieless reopen.
                raise _RetryBeginLogin(_fresh_kwargs(bump=False))

            def _checkout_now() -> None:
                nonlocal checkout, checkout_notes, page, text, url, stage
                checkout = run_checkout_agent(
                    page,
                    hint=hint,
                    seller=seller,
                    origin=origin,
                    destination=destination,
                    depart=depart,
                    expect_flight="",
                    allow_pay=False,
                    pause_on_auth=True,
                    max_steps=checkout_step_budget(seller, chosen_tap, passenger),
                    profile=passenger,
                    chosen_tap=chosen_tap,
                )
                checkout_notes = ((checkout.handoff + " | ") if checkout.handoff else "") + " | ".join(checkout.log)[:1500]
                page = _latest(page)
                text, url = _read_visible(page)
                stage = classify_page(text, url, book_count=_book_count(page))

            # Same Kernel tab: click on this sheet. OTP is not the success test.
            if reused and hint and should_hunt_login(stage):
                resume_kept = True
            elif reused and hint and stage != "error":
                if skip_tap and stage in {"results", "loading", "fare_sheet"}:
                    chosen_tap = ""
                    skip_tap = False
                try:
                    _checkout_now()
                    resume_kept = after_checkout_status(stage) != "dead"
                except Exception:
                    resume_kept = False
            elif reused and stage == "error":
                if re.search(r"go to home", chosen_tap or "", re.I):
                    try:
                        _checkout_now()
                    except Exception:
                        pass
                    text, url = _read_visible(page)
                    stage = classify_page(text, url, book_count=_book_count(page))
                    if stage == "error" or site_error_page(text, url):
                        # Go to home did not leave the dead sheet. Do not keep it or reopen it.
                        dead_error = True
                        chosen_tap = ""
                        skip_tap = False
                        resume_kept = False
                    else:
                        resume_kept = True
                else:
                    dead_error = True
                    chosen_tap = ""
                    skip_tap = False
            # Saved checkout URL is a fallback when that tab is gone.
            # Book a clock from results — do not reopen a leftover Review Details URL.
            if book_tap:
                resume_url = ""
            if not resume_kept and not dead_error and hint and resume_url and is_resume_booking_url(resume_url, seller):
                try:
                    page.goto(resume_url, wait_until="domcontentloaded", timeout=45000 if chosen_tap else 120000)
                    page.wait_for_timeout(1500)
                    text, url = _read_visible(page)
                    stage = classify_page(text, url, book_count=_book_count(page))
                    if stage == "error":
                        chosen_tap = ""
                        skip_tap = False
                    elif stage != "error":
                        _checkout_now()
                        checkout_notes = ((checkout.handoff + " | ") if checkout and checkout.handoff else "") + "resume | " + " | ".join((checkout.log if checkout else []))[:1500]
                        resume_kept = after_checkout_status(stage) != "dead"
                except Exception:
                    resume_kept = False
                    try:
                        text, url = _read_visible(page)
                        stage = classify_page(text, url, book_count=_book_count(page))
                    except Exception:
                        stage = stage or ""
            # Skip must not open a login form. A server-error itinerary is a new search.
            if not resume_kept and skip_tap and stage in {"login", "otp"}:
                shot = ""
                try:
                    shot = _shot(page, seller, sid, "resume-miss")
                except Exception:
                    shot = ""
                try:
                    pw.close()
                except Exception:
                    pass
                _drop(client, sid)
                return {
                    "status": "resume_lost",
                    "detail": "Saved add-ons page did not reopen.",
                    "session_id": sid,
                    "seller": seller,
                    "screenshot": shot,
                    "notes": checkout_notes,
                }
            if not resume_kept:
                results_url = (
                    ixigo_results_url(origin, destination, depart)
                    if seller == "ixigo"
                    else cleartrip_results_url(origin, destination, depart)
                )
                page.goto(results_url, wait_until="domcontentloaded", timeout=120000)
                wait_rounds = 90 if seller == "cleartrip" else 50
                for _ in range(wait_rounds):
                    if _book_count(page) >= 2:
                        break
                    page.wait_for_timeout(400)
                # Empty fare shell = bad browser session. Fresh Kernel tab, same trip.
                if hint and _book_count(page) < 2 and error_retry < max_error_retry:
                    try:
                        pw.close()
                    except Exception:
                        pass
                    _drop(client, sid)
                    raise _RetryBeginLogin(_fresh_kwargs(bump=True))
                if not hint:
                    # Leave the results alone. Clicking Log in here texts a code
                    # before the traveller has chosen a flight.
                    text, url = _read_visible(page)
                    outcome = {
                        "status": "fares" if require_otp else ("already_logged_in" if _logged_in(page, seller) else "fares"),
                        "session_id": sid,
                        "seller": seller,
                        "screenshot": _shot(page, seller, sid, "fares"),
                        "page": (text or "")[:8000],
                        "url": url or "",
                    }
                else:
                    checkout = run_checkout_agent(
                        page,
                        hint=hint,
                        seller=seller,
                        origin=origin,
                        destination=destination,
                        depart=depart,
                        expect_flight="",
                        allow_pay=False,
                        pause_on_auth=True,
                        max_steps=checkout_step_budget(seller, chosen_tap, passenger),
                        profile=passenger,
                        chosen_tap=chosen_tap,
                    )
                    checkout_notes = ((checkout.handoff + " | ") if checkout.handoff else "") + " | ".join(checkout.log)[:1500]
                    page = _latest(page)
                if seller == "ixigo" and (require_otp or auth_mode == "login"):
                    from app.page_agent import _click_ixigo_header_login, ixigo_header_needs_login

                    text, url = _read_visible(page)
                    # A saved mobile must reach the OTP sheet. Click Log in even when
                    # the fare text did not include the header words.
                    if require_otp or ixigo_header_needs_login(text):
                        _click_ixigo_header_login(page)
                        page.wait_for_timeout(1000)
                        text, url = _read_visible(page)
                        stage = classify_page(text, url, book_count=_book_count(page))
                        hunt_otp = True
                        national = indian_national(explicit) or national
                if hint and checkout and should_hunt_login(classify_page(*_read_visible(page), book_count=_book_count(page))):
                    for _ in range(8):
                        page = _latest(page)
                        if _logged_in(page, seller) or _find(page, "phone", seller) or _otp_visible(page, seller):
                            break
                        if _find(page, "send", seller):
                            _click(client, sid, page, _find(page, "send", seller))
                            page.wait_for_timeout(1800)
                            continue
                        login = _find(page, "login", seller)
                        if login:
                            _click(client, sid, page, login)
                            page.wait_for_timeout(1500)
                            continue
                        break
            # A saved profile can already be signed in. A fare preview must not be overwritten.
            # A traveller form is not a login screen, even when the header has no Logout text.
            # A Cleartrip homepage "My Account" header is not a finished booking.
            if outcome is None:
                text, url = _read_visible(page)
                stage = classify_page(text, url, book_count=_book_count(page))
                # Acting-up / stumped / empty home after a timed book is not a hold.
                if hint and error_retry < max_error_retry and (
                    stage == "error" or site_error_page(text, url) or stage == "home"
                ):
                    try:
                        pw.close()
                    except Exception:
                        pass
                    _drop(client, sid)
                    raise _RetryBeginLogin(_fresh_kwargs(bump=True))
                if stage == "error" or site_error_page(text, url):
                    outcome = {
                        "status": "error",
                        "detail": "site_error_recoverable",
                        "session_id": sid,
                        "seller": seller,
                        "screenshot": _shot(page, seller, sid, "error"),
                        "page": (text or "")[:8000],
                        "url": url or "",
                        "notes": checkout_notes,
                    }
                elif after_checkout_status(stage) == "held":
                    outcome = {
                        "status": "held",
                        "session_id": sid,
                        "seller": seller,
                        "screenshot": _shot(page, seller, sid, "held"),
                        "page": (text or "")[:8000],
                        "url": url or "",
                        "notes": checkout_notes,
                    }
            if (
                outcome is None
                and _logged_in(page, seller)
                and not _otp_visible(page, seller)
                and not require_otp
            ):
                text, url = _read_visible(page)
                stage = classify_page(text, url, book_count=_book_count(page))
                if stage not in {"error", "home"} and not site_error_page(text, url):
                    outcome = {
                        "status": "already_logged_in" if not hint else "held",
                        "session_id": sid,
                        "seller": seller,
                        "screenshot": _shot(page, seller, sid, "otp-after"),
                        "page": (text or "")[:8000],
                        "url": url or "",
                    }
            elif outcome is None and require_otp and _logged_in(page, seller) and not _otp_visible(page, seller):
                # Someone else's Ixigo session. Do not skip OTP for this new number.
                try:
                    page.evaluate(
                        """() => {
                          const els = [...document.querySelectorAll('a,button,[role=button]')];
                          const hit = els.find(e => /^log\\s*out$|^sign\\s*out$/i.test((e.innerText || '').trim()));
                          if (hit) hit.click();
                        }"""
                    )
                    page.wait_for_timeout(1200)
                except Exception:
                    pass
            ok = False
            if outcome is None and should_hunt_login(stage) and not hunt_otp:
                text, url = _read_visible(page)
                saved_mobile = indian_national(explicit)
                otp_sheet = stage == "otp" or bool(
                    re.search(r"verify your mobile|enter the otp|enter otp|otp sent", text or "", re.I)
                )
                mobile_sheet = (not otp_sheet) and (
                    stage == "login"
                    or bool(re.search(r"log in to ixigo|enter mobile number", text or "", re.I))
                )
                if saved_mobile and (mobile_sheet or otp_sheet):
                    # Remembered mobile: type it (if still on the number sheet) and wait for the OTP.
                    hunt_otp = True
                    national = saved_mobile
                elif mobile_sheet or otp_sheet:
                    # No saved mobile yet: keep the sign-in sheet and let WhatsApp ask for it.
                    outcome = {
                        "status": "held",
                        "session_id": sid,
                        "seller": seller,
                        "screenshot": _shot(page, seller, sid, "held"),
                        "page": (text or "")[:8000],
                        "url": url or "",
                        "notes": (checkout_notes + " | login_need_mobile").strip(" |"),
                    }
            if outcome is None and should_hunt_login(stage):
                ok = _otp_visible(page, seller)
            if outcome is None and not ok:
                text, url = _read_visible(page)
                stage = classify_page(text, url, book_count=_book_count(page))
                # The Ixigo sign-in drawer says Enter Mobile Number on top of Review & Traveller.
                # That is the login field. A contact form with the same words is not.
                if should_type_login_phone(stage, text) and len(national) == 10:
                    phone_box = _find(page, "phone", seller)
                    if not phone_box:
                        from app.page_agent import _fill_ixigo_login_phone

                        note = _fill_ixigo_login_phone(page, national)
                        if str(note).startswith("filled_mobile"):
                            send = _find(page, "send", seller)
                            if send:
                                _click(client, sid, page, send)
                            else:
                                client.browsers.computer.press_key(sid, keys=["Return"])
                            phone_box = {"x": 0, "y": 0}
                        else:
                            shot = _shot(page, seller, sid, "otp-miss")
                            try:
                                pw.close()
                            except Exception:
                                pass
                            _drop(client, sid)
                            return {
                                "status": "phone_miss",
                                "detail": "Mobile field did not open.",
                                "session_id": sid,
                                "seller": seller,
                                "screenshot": shot,
                            }
                    if phone_box.get("x") or phone_box.get("y"):
                        _click(client, sid, page, phone_box)
                        page.wait_for_timeout(300)
                        client.browsers.computer.type_text(sid, text=national, delay=40)
                        page.wait_for_timeout(300)
                        send = _find(page, "send", seller)
                        if send:
                            _click(client, sid, page, send)
                        client.browsers.computer.press_key(sid, keys=["Return"])
                    for _ in range(45):
                        page = _latest(page)
                        if _otp_visible(page, seller):
                            ok = True
                            break
                        page.wait_for_timeout(400)
                    if not ok and require_otp:
                        from app.page_agent import _click_ixigo_header_login, _fill_ixigo_login_phone

                        _click_ixigo_header_login(page)
                        page.wait_for_timeout(800)
                        note = _fill_ixigo_login_phone(page, national)
                        if str(note).startswith("filled_mobile"):
                            send = _find(page, "send", seller)
                            if send:
                                _click(client, sid, page, send)
                            else:
                                client.browsers.computer.press_key(sid, keys=["Return"])
                        for _ in range(30):
                            page = _latest(page)
                            if _otp_visible(page, seller):
                                ok = True
                                break
                            page.wait_for_timeout(400)
                elif should_hunt_login(stage):
                    outcome = {
                        "status": "held",
                        "session_id": sid,
                        "seller": seller,
                        "screenshot": _shot(page, seller, sid, "held"),
                        "page": (text or "")[:8000],
                        "url": url or "",
                        "notes": checkout_notes,
                    }
            shot = ""
            if ok:
                try:
                    from app.media_files import ensure_media_dir

                    dest = ensure_media_dir() / f"otp-{seller}-{sid[:8]}.jpg"
                    from app.vision_agent import capture_user_shot

                    shot = capture_user_shot(page, dest=dest, tag="otp") or ""
                except Exception:
                    shot = ""
            if outcome is None and not should_hunt_login(stage):
                text, url = _read_visible(page)
                stage = classify_page(text, url, book_count=_book_count(page))
                if stage == "error" or site_error_page(text, url):
                    if hint and error_retry < max_error_retry:
                        try:
                            pw.close()
                        except Exception:
                            pass
                        _drop(client, sid)
                        raise _RetryBeginLogin(_fresh_kwargs(bump=True))
                    outcome = {
                        "status": "error",
                        "detail": "site_error_recoverable",
                        "session_id": sid,
                        "seller": seller,
                        "screenshot": _shot(page, seller, sid, "error"),
                        "page": (text or "")[:8000],
                        "url": url or "",
                        "notes": checkout_notes,
                    }
                else:
                    outcome = {
                        "status": "held",
                        "session_id": sid,
                        "seller": seller,
                        "screenshot": _shot(page, seller, sid, "held"),
                        "page": (text or "")[:8000],
                        "url": url or "",
                        "notes": checkout_notes,
                    }
            if outcome is None and ok and on_code_requested is not None:
                # Stay on this CDP connection through Verify. A second connection
                # reaches the spinner and never becomes an Ixigo account.
                arm_live_otp(sid)
                on_code_requested(
                    {"status": "otp_sent", "session_id": sid, "seller": seller, "screenshot": shot}
                )
                code = _await_live_otp(page, sid, otp_wait_sec)
                if not code:
                    cancel_live_otp(sid)
                    outcome = {
                        "status": "otp_timeout",
                        "detail": "The code didn't arrive in time. Say the trip again for a new code.",
                        "session_id": sid,
                        "seller": seller,
                    }
                else:
                    outcome = _apply_otp(client, sid, page, seller, code)
                    outcome["session_id"] = sid
                    outcome["seller"] = seller
                    # Tell WhatsApp now — do not wait for the post-login checkout loop.
                    if on_otp_result is not None:
                        try:
                            on_otp_result(dict(outcome))
                        except Exception:
                            logger.exception("on_otp_result failed")
                    if hint and seller == "ixigo" and outcome.get("status") == "logged_in":
                        page = _latest(page)
                        try:
                            stayed_on_booking = is_resume_booking_url(page.url or "", seller)
                        except Exception:
                            stayed_on_booking = False
                        if not stayed_on_booking:
                            page.goto(
                                ixigo_results_url(origin, destination, depart),
                                wait_until="domcontentloaded",
                                timeout=120000,
                            )
                            for _ in range(40):
                                if _book_count(page) >= 2:
                                    break
                                page.wait_for_timeout(400)
                        checkout = run_checkout_agent(
                            page,
                            hint=hint,
                            seller="ixigo",
                            origin=origin,
                            destination=destination,
                            depart=depart,
                            expect_flight="",
                            allow_pay=False,
                            pause_on_auth=True,
                            max_steps=checkout_step_budget(seller, chosen_tap, passenger),
                            profile=passenger,
                            chosen_tap=chosen_tap,
                        )
                        checkout_notes = " | ".join(checkout.log)[:1500]
                        page = _latest(page)
                        text, url = _read_visible(page)
                        outcome = {
                            "status": "held",
                            "session_id": sid,
                            "seller": seller,
                            "screenshot": _shot(page, seller, sid, "held"),
                            "page": (text or "")[:8000],
                            "url": url or "",
                            "notes": checkout_notes,
                        }
            elif ok:
                outcome = {
                    "status": "error",
                    "detail": "Login keeps one browser connection, and this call did not.",
                    "session_id": sid,
                    "seller": seller,
                }
        if outcome is None:
            _drop(client, sid)
            return {"status": "otp_ui_miss", "detail": "OTP screen did not open. Nothing was charged."}
        if not keep_kernel_browser(str(outcome.get("status") or "")):
            _drop(client, sid)
        return outcome
    except _RetryBeginLogin as retry:
        # Playwright context is closed; safe to open a fresh Kernel tab.
        return begin_login(**retry.kwargs)
    except Exception as exc:
        cancel_live_otp(sid)
        _drop(client, sid)
        detail = f"{type(exc).__name__}: {str(exc)[:180]}"
        # Transient Kernel/CDP failures: cookieless reopen on the same seller.
        if error_retry < max_error_retry and hint:
            try:
                return begin_login(
                    seller=seller,
                    whatsapp_phone=whatsapp_phone,
                    explicit=explicit,
                    origin=origin,
                    destination=destination,
                    depart=depart,
                    hint=hint,
                    profile=guest_profile,
                    on_code_requested=on_code_requested,
                    on_otp_result=on_otp_result,
                    otp_wait_sec=otp_wait_sec,
                    resume_url="",
                    chosen_tap="",
                    session_id="",
                    error_retry=error_retry + 1,
                    fresh_browser=True,
                    require_otp=require_otp,
                )
            except Exception as retry_exc:
                detail = f"{detail} | retry {type(retry_exc).__name__}: {str(retry_exc)[:120]}"
        return {"status": "error", "detail": detail, "page": "", "url": "", "notes": detail}


def _shot(page, seller: str, session_id: str, tag: str) -> str:
    """Traveller-facing Kernel screenshot — clean, readable (not vision debug)."""
    try:
        from app.media_files import ensure_media_dir
        from app.vision_agent import capture_user_shot

        dest = ensure_media_dir() / f"{tag}-{seller}-{session_id[:8]}.jpg"
        return capture_user_shot(page, dest=dest, tag=tag) or ""
    except Exception:
        return ""


def _otp_page(context, seller: str):
    pages = list(context.pages or [])
    for page in reversed(pages):
        try:
            if _otp_visible(page, seller) or _find(page, "otp", seller):
                return page
        except Exception:
            continue
    if pages:
        return pages[-1]
    return context.new_page()


def _press_verify(client, session_id: str, page) -> bool:
    """Click the Verify button on the element, then fall back to one OS click."""
    try:
        button = page.get_by_role("button", name=re.compile(r"^verify$", re.I)).last
        if button.count() and button.is_visible():
            button.click(timeout=3000)
            return True
    except Exception:
        pass
    hit = _find(page, "verify", "ixigo")
    if not hit:
        return False
    _click(client, session_id, page, hit)
    return True


def _otp_box_text(page, seller: str) -> str:
    boxes = _find(page, "otp", seller) or []
    return "".join(re.sub(r"\D", "", str(box.get("v") or "")) for box in boxes)


def _type_otp_fields(page, digits: str, need: int) -> bool:
    """Type into the OTP inputs themselves. A mouse click misses these small boxes."""
    try:
        handles = page.locator(
            "input[maxlength='1'], input[inputmode='numeric'], input[autocomplete='one-time-code']"
        )
        visible = []
        count = min(handles.count(), 12)
        for index in range(count):
            box = handles.nth(index)
            try:
                if box.is_visible():
                    visible.append(box)
            except Exception:
                continue
        if len(visible) > need:
            visible = visible[-need:]
        if len(visible) < need:
            return False
        visible[0].click(timeout=2000)
        page.keyboard.type(digits[:need], delay=60)
        page.wait_for_timeout(200)
        return True
    except Exception:
        return False


def _type_otp(client, session_id: str, page, seller: str, digits: str) -> None:
    """Type one character per OTP box. Element typing first; OS click only if that misses."""
    need = 6 if seller == "ixigo" else 4
    _type_otp_fields(page, digits, need)
    if _otp_box_text(page, seller) == digits[:need]:
        return
    boxes = _find(page, "otp", seller) or []
    if not boxes:
        return
    for i, ch in enumerate(digits):
        if i >= len(boxes):
            break
        _click(client, session_id, page, boxes[i])
        page.wait_for_timeout(120)
        client.browsers.computer.type_text(session_id, text=ch, delay=30)


def _apply_otp(client, session_id: str, page, seller: str, code: str) -> dict[str, str]:
    """Type and verify on the page that is already open. Does not close the browser."""
    digits = re.sub(r"\D", "", code or "")
    if not otp_accepted(seller, digits):
        need = "4" if seller == "cleartrip" else "6"
        return {"status": "bad_code", "detail": f"{seller} needs {need} digits. Reply with only those digits."}
    boxes = _find(page, "otp", seller) or []
    if not boxes:
        shot = _shot(page, seller, session_id, "otp-miss")
        return {
            "status": "boxes_miss",
            "detail": "OTP boxes were not on screen. Say the trip again if you want a new code.",
            "screenshot": shot,
        }
    _type_otp(client, session_id, page, seller, digits)
    page.wait_for_timeout(400)
    if _otp_box_text(page, seller) != digits:
        shot = _shot(page, seller, session_id, "otp-empty")
        return {
            "status": "boxes_miss",
            "detail": "The code did not land in the OTP boxes. Reply with the same digits again.",
            "screenshot": shot,
            "session_id": session_id,
            "seller": seller,
        }
    if seller == "ixigo":
        _press_verify(client, session_id, page)
    logged = False
    still = False
    verifying = False
    empty_rounds = 0
    # The working login stayed on this page through "Please wait while we verify".
    # Leaving, or closing the browser, while that text is up drops the account.
    for _attempt in range(20):
        page.wait_for_timeout(1000)
        try:
            if page.is_closed():
                page = _latest(page)
        except Exception:
            page = _latest(page)
        text, _url = _read_visible(page)
        if seller == "ixigo" and ixigo_verify_in_progress(text):
            verifying = True
            still = False
            empty_rounds = 0
            continue
        fares_after_code = False
        if seller == "ixigo":
            try:
                boxes_open = _code_boxes_open(page, seller)
            except Exception:
                boxes_open = True
            if not boxes_open:
                try:
                    fares_after_code = _ixigo_otp_finished_on_fares(text, _url) or _book_count(page) >= 2
                except Exception:
                    fares_after_code = _ixigo_otp_finished_on_fares(text, _url)
        if _logged_in(page, seller) or fares_after_code:
            logged = True
            verifying = False
            break
        if re.search(
            r"incorrect otp|invalid otp|wrong otp|otp is (?:incorrect|invalid|wrong)|"
            r"entered otp is incorrect|please enter (?:a )?valid otp",
            text or "",
            re.I,
        ):
            shot = _shot(page, seller, session_id, "otp-rejected")
            return {
                "status": "still_otp",
                "detail": (
                    "That OTP is incorrect. Reply with a new code and I'll type it on this same screen. "
                    "Nothing paid."
                ),
                "screenshot": shot,
                "session_id": session_id,
                "seller": seller,
            }
        if _code_boxes_open(page, seller):
            still = True
            if _otp_box_text(page, seller) != digits:
                empty_rounds += 1
                # Digits never stuck — stop early instead of waiting in silence.
                if empty_rounds >= 8:
                    break
            else:
                empty_rounds = 0
            continue
        still = False
        if verifying:
            # The account name replaces Log in a moment after the spinner. Keep waiting.
            verifying = False
            continue
    # The list can paint after the last read. One more look before calling it a failed login.
    if not logged and seller == "ixigo":
        try:
            boxes_open = _code_boxes_open(page, seller)
        except Exception:
            boxes_open = True
        if not boxes_open:
            try:
                text, _url = _read_visible(page)
            except Exception:
                text, _url = "", ""
            try:
                on_fares = _ixigo_otp_finished_on_fares(text, _url) or _book_count(page) >= 2
            except Exception:
                on_fares = _ixigo_otp_finished_on_fares(text, _url)
            if on_fares:
                logged = True
    # Stay on the open flight. A hop to the Ixigo homepage drops the chosen fare
    # and leaves WhatsApp silent until that navigation finishes.
    shot = _shot(page, seller, session_id, "otp-after")
    if logged:
        return {"status": "logged_in", "seller": seller, "screenshot": shot}
    if still:
        # Same Kernel tab stays up so the next digits retry this OTP step only.
        return {
            "status": "still_otp",
            "detail": otp_failure_detail(boxes_open=True),
            "screenshot": shot,
            "session_id": session_id,
            "seller": seller,
        }
    return {
        "status": "no_session",
        "detail": otp_failure_detail(boxes_open=False, verifying=verifying),
        "screenshot": shot,
    }


def submit_otp(*, session_id: str, seller: str, code: str) -> dict[str, str]:
    """Fallback when the original login thread is gone. Reconnects, then types."""
    digits = re.sub(r"\D", "", code or "")
    if not otp_accepted(seller, digits):
        need = "4" if seller == "cleartrip" else "6"
        return {"status": "bad_code", "detail": f"{seller} needs {need} digits. Reply with only those digits."}
    client = _client()
    _stop_hold(session_id)
    try:
        browser = client.browsers.retrieve(session_id)
    except Exception:
        return {"status": "session_gone", "detail": "The login page closed. Say book again and I'll request a new code."}
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            pw = p.chromium.connect_over_cdp(browser.cdp_ws_url)
            context = pw.contexts[0] if pw.contexts else pw.new_context()
            page = _otp_page(context, seller)
            result = _apply_otp(client, session_id, page, seller, digits)
            try:
                pw.close()
            except Exception:
                pass
        _drop(client, session_id)
        result["seller"] = seller
        return result
    except Exception as exc:
        _stop_hold(session_id)
        return {"status": "error", "detail": f"{type(exc).__name__}: {str(exc)[:180]}"}
