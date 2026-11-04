"""Live book-site prices after Google Flights.

Opens Cleartrip / Ixigo / EaseMyTrip on one Kernel browser, matches a departure
hint, and picks the cheapest site that painted a fare. Airline deep links for
GAU→DEL (IndiGo) are attempted only when that route is in play; silent skip if
the page has no fares.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from app.page_agent import _clock_needles

logger = logging.getLogger(__name__)

_BOOK_QUOTE_SELLERS = ("cleartrip", "ixigo", "easemytrip")
_AIRLINE_QUOTE_SELLERS = ("indigo",)


def is_gau_del(origin: str, destination: str) -> bool:
    """True for Guwahati (GAU) → Delhi (DEL), the only airline-site probe route."""
    from app.search import resolve_place

    o = resolve_place(origin)
    d = resolve_place(destination)
    if not o or not d:
        return False
    return o[0].upper() == "GAU" and d[0].upper() == "DEL"


def quote_sellers_for_route(origin: str, destination: str) -> list[str]:
    """Bookable OTAs always; IndiGo only for GAU→DEL (attempt — may yield nothing)."""
    sellers = list(_BOOK_QUOTE_SELLERS)
    if is_gau_del(origin, destination):
        sellers.extend(_AIRLINE_QUOTE_SELLERS)
    return sellers


def price_for_hint(options: list[dict[str, Any]], hint: str = "") -> dict[str, Any] | None:
    """Best row for a departure clock, else cheapest painted fare."""
    if not options:
        return None
    needles = set(_clock_needles(hint)) if hint else set()
    matched = [row for row in options if needles and str(row.get("depart") or "") in needles]
    pool = matched or list(options)
    return min(pool, key=lambda row: int(row.get("price_n") or 10**9))


def format_book_quotes(
    quotes: list[dict[str, Any]],
    *,
    hint: str = "",
    trip: str = "",
) -> str:
    """WhatsApp lines: per-site prices + cheapest bookable recommendation."""
    usable = [q for q in quotes if int(q.get("price_n") or 0) > 0]
    if not usable:
        return (
            f"I couldn't pull live book-site prices{(f' for {trip}' if trip else '')}. "
            "Say a departure time and I'll book on Cleartrip, or name Ixigo or EaseMyTrip."
        )
    when = hint or "that flight"
    bits = [f"{q['label']} ₹{int(q['price_n']):,}" for q in usable]
    lines = [
        f"Live book prices for {when}"
        + (f" ({trip})" if trip else "")
        + ": "
        + " · ".join(bits)
        + "."
    ]
    bookable = [q for q in usable if q.get("bookable")]
    pick = min(bookable or usable, key=lambda q: int(q["price_n"]))
    if pick.get("bookable"):
        lines.append(
            f"Cheapest to book is {pick['label']} at ₹{int(pick['price_n']):,}. "
            f"Say {when} to book there, or name another site."
        )
    else:
        lines.append(
            f"{pick['label']} shows ₹{int(pick['price_n']):,} but I book via Cleartrip or Ixigo. "
            f"Say {when} and I'll open the cheapest bookable site."
        )
    return " ".join(lines)


def collect_book_quotes(
    *,
    phone: str,
    origin: str,
    destination: str,
    depart: str,
    hint: str = "",
) -> list[dict[str, Any]]:
    """Open each quote seller once on Kernel; return only sites that painted ₹."""
    from app.kernel_otp import kernel_configured

    if not kernel_configured():
        return []
    sellers = quote_sellers_for_route(origin, destination)
    try:
        return _kernel_collect(phone=phone, origin=origin, destination=destination, depart=depart, hint=hint, sellers=sellers)
    except Exception:
        logger.exception("Book-site quote pass failed")
        return []


def _kernel_collect(
    *,
    phone: str,
    origin: str,
    destination: str,
    depart: str,
    hint: str,
    sellers: list[str],
) -> list[dict[str, Any]]:
    from playwright.sync_api import sync_playwright

    from app.kernel_otp import _client, _drop, _open_browser, _profile, _read_visible
    from app.search import seller_can_book, seller_results_url
    from app.travel_agent import fare_options, prices_in

    client = _client()
    profile = _profile("quote", phone, nonce=str(int(time.time()) % 100000))
    browser = _open_browser(client, profile, ephemeral=True)
    sid = browser.session_id
    quotes: list[dict[str, Any]] = []
    try:
        with sync_playwright() as p:
            pw = p.chromium.connect_over_cdp(browser.cdp_ws_url, timeout=90_000)
            context = pw.contexts[0] if pw.contexts else pw.new_context()
            page = context.pages[0] if context.pages else context.new_page()
            for seller_id in sellers:
                url = seller_results_url(seller_id, origin, destination, depart)
                if not url:
                    continue
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=120_000)
                except Exception:
                    logger.warning("Quote goto failed for %s", seller_id)
                    continue
                _dismiss_noise(page)
                text = _wait_fares(page, seller_id)
                live = ""
                try:
                    text, live = _read_visible(page)
                except Exception:
                    pass
                options = fare_options(text or "")
                pick = price_for_hint(options, hint)
                price_n = int(pick.get("price_n") or 0) if pick else 0
                if price_n < 1500:
                    rupees = [n for n in prices_in(text or "") if n >= 3000]
                    price_n = min(rupees) if rupees else 0
                if price_n < 1500:
                    continue
                if re.search(r"something went wrong|too many requests|502 bad|servers are stumped|no data available", text or "", re.I):
                    continue
                from app.travel_agent import _seller_label

                quotes.append(
                    {
                        "seller": seller_id,
                        "label": _seller_label(seller_id),
                        "price_n": price_n,
                        "price": f"{price_n:,}",
                        "depart": str((pick or {}).get("depart") or hint or ""),
                        "airline": str((pick or {}).get("airline") or ""),
                        "bookable": seller_can_book(seller_id),
                        "url": live or url,
                    }
                )
            try:
                pw.close()
            except Exception:
                pass
    finally:
        _drop(client, sid)
    return quotes


def _dismiss_noise(page) -> None:
    try:
        page.evaluate(
            """() => {
                const btn = [...document.querySelectorAll('button,a,[role=button]')].find(b =>
                    /^(accept|accept all|i agree|agree|got it|allow all|ok)$/i.test((b.innerText || '').trim())
                );
                if (btn) btn.click();
            }"""
        )
        page.wait_for_timeout(400)
    except Exception:
        pass


def _wait_fares(page, seller_id: str) -> str:
    from app.kernel_otp import _book_count, _read_visible
    from app.travel_agent import fare_options, prices_in

    rounds = 90 if seller_id == "cleartrip" else (50 if seller_id == "ixigo" else 40)
    text = ""
    for _ in range(rounds):
        try:
            text, _url = _read_visible(page)
        except Exception:
            text = ""
        if fare_options(text) or [n for n in prices_in(text) if n >= 3000]:
            return text
        if seller_id in {"cleartrip", "ixigo"} and _book_count(page) >= 2:
            try:
                text, _url = _read_visible(page)
            except Exception:
                pass
            return text
        if re.search(r"something went wrong|too many requests|502|stumped|no data available", text or "", re.I):
            return text
        try:
            page.wait_for_timeout(400)
        except Exception:
            break
    return text
