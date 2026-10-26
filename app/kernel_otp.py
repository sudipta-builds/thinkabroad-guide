"""WhatsApp login on Kernel for Ixigo (6-digit OTP) and Cleartrip (4-digit OTP).

The browser stays open until the traveller replies with the code. OTP digits are
never written to logs. Payment is not part of this flow.
"""

from __future__ import annotations

import os
import re
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


def kernel_configured() -> bool:
    return bool((get_settings().kernel_api_key or os.getenv("KERNEL_API_KEY") or "").strip())


def should_resume_kernel_otp(state: dict[str, Any], text: str) -> bool:
    if not state.get("kernel_awaiting_otp") or not state.get("kernel_session_id"):
        return False
    return bool(re.fullmatch(r"\d{4,8}", (text or "").strip()))


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
    for candidate in (explicit, whatsapp_phone, os.getenv("IXIGO_LOGIN_PHONE") or ""):
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
    metrics = _metrics(page)
    chrome_top = max(0, metrics["outerH"] - metrics["innerH"])
    chrome_left = max(0, (metrics["outerW"] - metrics["innerW"]) / 2)
    x = int((metrics["screenX"] + chrome_left + box["x"]) * metrics["dpr"])
    y = int((metrics["screenY"] + chrome_top + box["y"]) * metrics["dpr"])
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
            let boxes = inputs.filter(i => {
              const r = i.getBoundingClientRect();
              const ml = i.getAttribute('maxlength');
              const small = r.width >= 16 && r.width < 80 && r.height >= 16 && r.height < 80;
              if (seller === 'cleartrip') return r.width > 40 && r.width < 70 && r.height > 40 && r.height < 70;
              return small && (ml === '1' || /otp|one-time|numeric/i.test((i.getAttribute('inputmode')||'')+(i.autocomplete||'')+(i.name||'')+(i.id||'')));
            });
            boxes.sort((a,b) => a.getBoundingClientRect().x - b.getBoundingClientRect().x || a.getBoundingClientRect().y - b.getBoundingClientRect().y);
            const row = [];
            for (const el of boxes) {
              const r = el.getBoundingClientRect();
              const prev = row.length ? row[row.length - 1].getBoundingClientRect() : null;
              if (prev && (Math.abs(r.y - prev.y) > 24 || r.x - prev.x > 90)) {
                if (row.length >= (seller === 'cleartrip' ? 4 : 6)) break;
                row.length = 0;
              }
              row.push(el);
              if (row.length === (seller === 'cleartrip' ? 4 : 6)) break;
            }
            const picked = row.length ? row : boxes.slice(0, seller === 'cleartrip' ? 4 : 6);
            return picked.length ? picked.map(el => {
              const b = box(el);
              b.v = (el.value || '').slice(0, 4);
              return b;
            }) : null;
          }
          if (kind === 'verify') {
            const hits = [...document.querySelectorAll('button,[role=button],div,span,a')].filter(vis).filter(b => /^verify$/i.test(text(b)));
            if (!hits.length) return null;
            hits.sort((a,b) => {
              const ar = a.getBoundingClientRect(), br = b.getBoundingClientRect();
              return (ar.width * ar.height) - (br.width * br.height);
            });
            return box(hits[0]);
          }
          return null;
        }""",
        {"kind": kind, "seller": seller},
    )


def _otp_visible(page, seller: str) -> bool:
    text, _url = _read(page)
    if seller == "cleartrip":
        return bool(re.search(r"enter otp", text or "", re.I))
    stage = classify_page(text, _url, book_count=_book_count(page))
    return stage == "otp" or bool(re.search(r"enter otp|verify your mobile|otp sent", text or "", re.I))


def _logged_in(page, seller: str) -> bool:
    text, _url = _read(page)
    if seller == "ixigo":
        if ixigo_auth_session_ready(page):
            return True
        return bool(re.search(r"log\s*out|view profile|my profile", text or "", re.I))
    low = (text or "").lower()
    return "my account" in low and not re.search(r"enter otp|enter mobile number", low)


def _open_browser(client, profile: str):
    try:
        client.profiles.create(name=profile)
    except Exception:
        pass
    return client.browsers.create(
        stealth=True,
        kiosk_mode=True,
        viewport={"width": 1280, "height": 800, "refresh_rate": 25},
        timeout_seconds=900,
        profile={"name": profile, "save_changes": True},
    )


def _drop(client, session_id: str) -> None:
    try:
        client.browsers.delete_by_id(session_id)
    except Exception:
        pass


def begin_login(
    *,
    seller: str,
    whatsapp_phone: str,
    origin: str = "Guwahati",
    destination: str = "Delhi",
    depart: str = "2026-12-04",
    hint: str = "1:15",
    explicit: str = "",
) -> dict[str, str]:
    """Open the seller and request one OTP. Leaves the Kernel browser running."""
    if seller not in KERNEL_SELLERS:
        return {"status": "unsupported", "detail": seller}
    login_phone = login_phone_for(whatsapp_phone, explicit)
    national = indian_national(login_phone)
    if len(national) != 10:
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
            if seller == "ixigo":
                page.goto(ixigo_results_url(origin, destination, depart), wait_until="domcontentloaded", timeout=120000)
                for _ in range(50):
                    if _book_count(page) >= 2:
                        break
                    page.wait_for_timeout(400)
                run_checkout_agent(
                    page,
                    hint=hint or "1:15",
                    seller="ixigo",
                    origin=origin,
                    destination=destination,
                    depart=depart,
                    expect_flight="IX1513" if (hint or "1:15") == "1:15" else "",
                    allow_pay=False,
                    pause_on_auth=True,
                    max_steps=16,
                )
                page = _latest(page)
                for _ in range(4):
                    page = _latest(page)
                    if _find(page, "phone", seller) or _otp_visible(page, seller):
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
                if not _find(page, "phone", seller):
                    hit = _find(page, "login", seller)
                    if not hit:
                        _drop(client, sid)
                        return {"status": "login_miss", "detail": "Cleartrip login button was not on screen."}
                    _click(client, sid, page, hit)
                    page.wait_for_timeout(1500)
            phone_box = _find(page, "phone", seller)
            if not phone_box:
                _drop(client, sid)
                return {"status": "phone_miss", "detail": "Mobile field did not open."}
            _click(client, sid, page, phone_box)
            page.wait_for_timeout(300)
            client.browsers.computer.type_text(sid, text=national, delay=40)
            page.wait_for_timeout(300)
            send = _find(page, "send", seller)
            if send:
                _click(client, sid, page, send)
            client.browsers.computer.press_key(sid, keys=["Return"])
            ok = False
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
            try:
                pw.close()
            except Exception:
                pass
        if not ok:
            _drop(client, sid)
            return {"status": "otp_ui_miss", "detail": "OTP screen did not open. Nothing was charged."}
        return {"status": "otp_sent", "session_id": sid, "seller": seller, "screenshot": shot}
    except Exception as exc:
        _drop(client, sid)
        return {"status": "error", "detail": type(exc).__name__}


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


def _click_verify(client, session_id: str, page, boxes: list[dict[str, Any]]) -> bool:
    """Click the Verify control under the OTP boxes. Falls back to that spot if the label is missed."""
    anchor = verify_fallback(boxes)
    hit = page.evaluate(
        """(anchor) => {
          const vis = (el) => {
            const r = el.getBoundingClientRect();
            const s = getComputedStyle(el);
            return r.width > 8 && r.height > 8 && s.visibility !== 'hidden' && s.display !== 'none' && r.top < innerHeight && r.bottom > 0;
          };
          const label = (el) => (el.innerText || el.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim();
          const center = (el) => {
            const r = el.getBoundingClientRect();
            return {x: r.x + r.width/2, y: r.y + r.height/2, w: r.width, h: r.height};
          };
          const orange = (el) => {
            const m = getComputedStyle(el).backgroundColor.match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/);
            if (!m) return false;
            const r = +m[1], g = +m[2], b = +m[3];
            return r > 180 && g > 60 && g < 190 && b < 90;
          };
          const nodes = [...document.querySelectorAll('button,[role=button],div,a,span')].filter(vis);
          let hits = nodes.filter(el => /^verify$/i.test(label(el)));
          if (anchor) {
            const below = hits.filter(el => center(el).y >= anchor.y - 140);
            if (below.length) hits = below;
          }
          const wide = hits.filter(el => center(el).w > 80);
          let pick = wide[0] || hits[0] || null;
          if (!pick && anchor) {
            const bars = nodes.filter(el => orange(el) && center(el).w > 80 && center(el).y >= anchor.y - 40 && center(el).y < anchor.y + 220);
            bars.sort((a,b) => Math.abs(center(a).y - anchor.y) - Math.abs(center(b).y - anchor.y));
            pick = bars[0] || null;
          }
          if (!pick) return anchor ? {x: anchor.x, y: anchor.y, t: 'below-otp'} : null;
          const c = center(pick);
          return {x: c.x, y: c.y, t: label(pick).slice(0, 20) || 'verify'};
        }""",
        anchor,
    )
    if not hit:
        return False
    _click(client, session_id, page, hit)
    return True


def _type_otp(client, session_id: str, page, seller: str, digits: str) -> None:
    boxes = _find(page, "otp", seller) or []
    if not boxes:
        return
    for i, ch in enumerate(digits):
        if i >= len(boxes):
            break
        _click(client, session_id, page, boxes[i])
        page.wait_for_timeout(150)
        client.browsers.computer.type_text(session_id, text=ch, delay=40)
    page.wait_for_timeout(400)
    filled_boxes = _find(page, "otp", seller) or []
    if boxes and filled_boxes and not any(str(box.get("v") or "") for box in filled_boxes):
        _click(client, session_id, page, boxes[0])
        page.wait_for_timeout(200)
        client.browsers.computer.type_text(session_id, text=digits, delay=80)


def submit_otp(*, session_id: str, seller: str, code: str) -> dict[str, str]:
    """Type a chat OTP into the still-open Kernel browser and check the real session."""
    digits = re.sub(r"\D", "", code or "")
    if not otp_accepted(seller, digits):
        need = "4" if seller == "cleartrip" else "6"
        return {"status": "bad_code", "detail": f"{seller} needs {need} digits. Reply with only those digits."}
    client = _client()
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
            boxes = _find(page, "otp", seller) or []
            if not boxes:
                shot = _shot(page, seller, session_id, "otp-miss")
                try:
                    pw.close()
                except Exception:
                    pass
                _drop(client, session_id)
                return {
                    "status": "boxes_miss",
                    "detail": "OTP boxes were not on screen. Say the trip again if you want a new code.",
                    "screenshot": shot,
                }
            _type_otp(client, session_id, page, seller, digits)
            if seller == "ixigo":
                fresh = _find(page, "otp", seller) or boxes
                _click_verify(client, session_id, page, fresh)
                page.wait_for_timeout(1200)
                if _otp_visible(page, seller):
                    _click_verify(client, session_id, page, fresh)
            logged = False
            still = False
            for _ in range(16):
                page.wait_for_timeout(700)
                try:
                    if page.is_closed():
                        page = _latest(page)
                except Exception:
                    page = _latest(page)
                if _logged_in(page, seller):
                    logged = True
                    break
                if _otp_visible(page, seller):
                    still = True
                    continue
                still = False
            if seller == "ixigo" and not logged and not still:
                try:
                    page.goto("https://www.ixigo.com/", wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(3000)
                except Exception:
                    pass
                logged = _logged_in(page, seller)
            shot = _shot(page, seller, session_id, "otp-after")
            try:
                pw.close()
            except Exception:
                pass
        if logged:
            _drop(client, session_id)
            return {"status": "logged_in", "seller": seller, "screenshot": shot}
        if still:
            return {
                "status": "still_otp",
                "detail": "The code screen is still open. Reply with the same digits again.",
                "screenshot": shot,
            }
        _drop(client, session_id)
        return {
            "status": "no_session",
            "detail": "The code was typed, but the site did not show a logged-in account.",
            "screenshot": shot,
        }
    except Exception as exc:
        return {"status": "error", "detail": type(exc).__name__}
