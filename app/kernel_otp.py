"""WhatsApp login on Kernel for Ixigo (6-digit OTP) and Cleartrip (4-digit OTP).

The browser stays open until the traveller replies with the code. OTP digits are
never written to logs. Payment is not part of this flow.
"""

from __future__ import annotations

import os
import re
import threading
import time
from collections.abc import Callable
from typing import Any

from app.config import get_settings
from app.page_agent import (
    _book_count,
    _latest,
    _read,
    classify_page,
    ixigo_auth_session_ready,
    run_checkout_agent,
)
from app.search import ixigo_results_url

KERNEL_SELLERS = {"ixigo", "cleartrip"}
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


def otp_failure_detail(*, boxes_open: bool, verifying: bool = False) -> str:
    if boxes_open:
        return "The code is in the boxes, but Verify did not submit. Say Ixigo again for a new code."
    if verifying:
        return "Ixigo is still checking that code. The account did not appear."
    return "The code screen closed, but Ixigo still shows Log in. Say Ixigo again for a new code."


def resume_kernel_otp(state: dict[str, Any], text: str) -> str:
    """What WhatsApp should do with a code. Never opens a second browser connection."""
    if not should_resume_kernel_otp(state, text):
        return ""
    seller = str(state.get("kernel_seller") or "ixigo")
    if seller in KERNEL_SELLERS and not otp_accepted(seller, text):
        return "need_digits"
    if handoff_whatsapp_otp(state, text):
        return "type"
    return "closed"


def keep_kernel_otp_session(status: str) -> bool:
    """The login page is still waiting, so the same Kernel browser should stay up."""
    return status in {"bad_code", "still_otp"}


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


def _profile(seller: str, whatsapp_phone: str) -> str:
    digits = re.sub(r"\D", "", whatsapp_phone or "")[-12:] or "user"
    return f"wa-{seller}-{digits}"[:60]


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
              return ml === '1' || /one-time|otp|numeric/i.test(mode);
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
    text, _url = _read(page)
    if seller == "ixigo" and ixigo_verify_in_progress(text):
        return False
    if seller == "cleartrip":
        return bool(re.search(r"enter otp", text or "", re.I))
    stage = classify_page(text, _url, book_count=_book_count(page))
    return stage == "otp" or bool(re.search(r"enter otp|verify your mobile|otp sent", text or "", re.I))


def _logged_in(page, seller: str) -> bool:
    text, url = _read(page)
    if seller == "ixigo":
        if ixigo_auth_session_ready(page):
            return True
        if re.search(r"loginVisible=true", url or "", re.I):
            return False
        if re.search(r"log\s*in\s*/?\s*sign\s*up|login/signup", text or "", re.I):
            return False
        return bool(re.search(r"\blog\s*out\b|view profile|my profile|ixigoer", text or "", re.I))
    low = (text or "").lower()
    return "my account" in low and not re.search(r"enter otp|enter mobile number", low)


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


def _open_browser(client, profile: str):
    try:
        client.profiles.create(name=profile)
    except Exception:
        pass
    return client.browsers.create(
        stealth=True,
        kiosk_mode=True,
        viewport={"width": 1280, "height": 800, "refresh_rate": 25},
        timeout_seconds=1000,
        profile={"name": profile, "save_changes": True},
    )


def _drop(client, session_id: str) -> None:
    _stop_hold(session_id)
    try:
        client.browsers.delete_by_id(session_id)
    except Exception:
        pass


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
    otp_wait_sec: int = 300,
) -> dict[str, str]:
    """Open the seller and request one OTP.

    With on_code_requested, the same CDP connection waits for the WhatsApp code,
    types it, and clicks Verify. That is the path that produced an Ixigo account.
    """
    if seller not in KERNEL_SELLERS:
        return {"status": "unsupported", "detail": seller}
    login_phone = login_phone_for(whatsapp_phone, explicit)
    national = indian_national(login_phone)
    # A fare list does not need a mobile. The code is only for a chosen flight.
    if hint and len(national) != 10:
        return {"status": "bad_phone", "detail": "Need a 10-digit Indian mobile."}
    client = _client()
    profile = _profile(seller, whatsapp_phone)
    browser = _open_browser(client, profile)
    sid = browser.session_id
    os.environ["IXIGO_OTPLESS_BRIDGE"] = "0"
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            pw = p.chromium.connect_over_cdp(browser.cdp_ws_url)
            context = pw.contexts[0] if pw.contexts else pw.new_context()
            page = context.pages[0] if context.pages else context.new_page()
            outcome: dict[str, str] | None = None
            if seller == "ixigo":
                page.goto(ixigo_results_url(origin, destination, depart), wait_until="domcontentloaded", timeout=120000)
                for _ in range(50):
                    if _book_count(page) >= 2:
                        break
                    page.wait_for_timeout(400)
                if not hint:
                    # Leave the results alone. Clicking Log in here texts a code
                    # before the traveller has chosen a flight.
                    text, url = _read(page)
                    outcome = {
                        "status": "already_logged_in" if _logged_in(page, seller) else "fares",
                        "session_id": sid,
                        "seller": seller,
                        "screenshot": _shot(page, seller, sid, "fares"),
                        "page": (text or "")[:8000],
                        "url": url or "",
                    }
                else:
                    run_checkout_agent(
                        page,
                        hint=hint,
                        seller="ixigo",
                        origin=origin,
                        destination=destination,
                        depart=depart,
                        expect_flight="IX1513" if hint == "1:15" else "",
                        allow_pay=False,
                        pause_on_auth=True,
                        max_steps=16,
                        profile=profile,
                    )
                    page = _latest(page)
                if hint:
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
            else:
                page.goto("https://www.cleartrip.com/", wait_until="domcontentloaded", timeout=120000)
                page.wait_for_timeout(3000)
                if not _find(page, "phone", seller) and not _logged_in(page, seller):
                    hit = _find(page, "login", seller)
                    if not hit:
                        _drop(client, sid)
                        return {"status": "login_miss", "detail": "Cleartrip login button was not on screen."}
                    _click(client, sid, page, hit)
                    page.wait_for_timeout(1500)
            # A saved profile can already be signed in. A fare preview must not be overwritten.
            if outcome is None and _logged_in(page, seller) and not _otp_visible(page, seller):
                text, url = _read(page)
                outcome = {
                    "status": "already_logged_in" if not hint else "held",
                    "session_id": sid,
                    "seller": seller,
                    "screenshot": _shot(page, seller, sid, "otp-after"),
                    "page": (text or "")[:8000],
                    "url": url or "",
                }
            ok = False
            if outcome is None:
                ok = _otp_visible(page, seller)
            if outcome is None and not ok:
                phone_box = _find(page, "phone", seller)
                if not phone_box:
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
                _click(client, sid, page, phone_box)
                page.wait_for_timeout(300)
                client.browsers.computer.type_text(sid, text=national, delay=40)
                page.wait_for_timeout(300)
                send = _find(page, "send", seller)
                if send:
                    _click(client, sid, page, send)
                client.browsers.computer.press_key(sid, keys=["Return"])
                for _ in range(30):
                    page = _latest(page)
                    if _otp_visible(page, seller):
                        ok = True
                        break
                    page.wait_for_timeout(400)
            shot = ""
            if ok:
                try:
                    from app.media_files import ensure_media_dir

                    dest = ensure_media_dir() / f"otp-{seller}-{sid[:8]}.jpg"
                    page.screenshot(path=str(dest), type="jpeg", quality=55)
                    shot = str(dest)
                except Exception:
                    shot = ""
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
                    if hint and seller == "ixigo" and outcome.get("status") == "logged_in":
                        page = _latest(page)
                        page.goto(
                            ixigo_results_url(origin, destination, depart),
                            wait_until="domcontentloaded",
                            timeout=120000,
                        )
                        for _ in range(40):
                            if _book_count(page) >= 2:
                                break
                            page.wait_for_timeout(400)
                        run_checkout_agent(
                            page,
                            hint=hint,
                            seller="ixigo",
                            origin=origin,
                            destination=destination,
                            depart=depart,
                            expect_flight="IX1513" if hint == "1:15" else "",
                            allow_pay=False,
                            pause_on_auth=True,
                            max_steps=16,
                            profile=profile,
                        )
                        page = _latest(page)
                        text, url = _read(page)
                        outcome = {
                            "status": "held",
                            "session_id": sid,
                            "seller": seller,
                            "screenshot": _shot(page, seller, sid, "held"),
                            "page": (text or "")[:8000],
                            "url": url or "",
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
        if outcome.get("status") != "otp_sent":
            _drop(client, sid)
        return outcome
    except Exception as exc:
        cancel_live_otp(sid)
        _drop(client, sid)
        return {"status": "error", "detail": f"{type(exc).__name__}: {str(exc)[:180]}"}


def _shot(page, seller: str, session_id: str, tag: str) -> str:
    try:
        from app.media_files import ensure_media_dir

        dest = ensure_media_dir() / f"{tag}-{seller}-{session_id[:8]}.jpg"
        page.screenshot(path=str(dest), type="jpeg", quality=55)
        return str(dest)
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
    """One OS click on the Verify button. No guessed coordinate."""
    hit = _find(page, "verify", "ixigo")
    if not hit:
        return False
    _click(client, session_id, page, hit)
    return True


def _type_otp(client, session_id: str, page, seller: str, digits: str) -> None:
    """One Kernel click and one character per box. Same as the local login."""
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
    if seller == "ixigo":
        _press_verify(client, session_id, page)
    logged = False
    still = False
    verifying = False
    for _attempt in range(30):
        page.wait_for_timeout(800)
        try:
            if page.is_closed():
                page = _latest(page)
        except Exception:
            page = _latest(page)
        text, _url = _read(page)
        if seller == "ixigo" and ixigo_verify_in_progress(text):
            verifying = True
            still = False
            continue
        if _logged_in(page, seller):
            logged = True
            break
        if _code_boxes_open(page, seller):
            still = True
            continue
        still = False
        if verifying:
            break
    if verifying and not logged:
        for _ in range(45):
            text, _url = _read(page)
            if not ixigo_verify_in_progress(text):
                break
            page.wait_for_timeout(1000)
        logged = _logged_in(page, seller)
    if seller == "ixigo" and not logged and not _code_boxes_open(page, seller):
        text, _url = _read(page)
        if not ixigo_verify_in_progress(text):
            try:
                page.goto("https://www.ixigo.com/", wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(2500)
            except Exception:
                pass
            logged = _logged_in(page, seller)
    if seller == "ixigo" and logged:
        try:
            page.goto("https://www.ixigo.com/", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2500)
        except Exception:
            pass
        logged = _logged_in(page, seller)
    shot = _shot(page, seller, session_id, "otp-after")
    if logged:
        return {"status": "logged_in", "seller": seller, "screenshot": shot}
    return {
        "status": "no_session",
        "detail": otp_failure_detail(boxes_open=still, verifying=verifying),
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
