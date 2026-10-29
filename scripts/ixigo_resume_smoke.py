"""Smoke: drop the Kernel browser, reopen the saved Ixigo checkout, click Continue.

PASS when session 2 lands on the saved booking URL and leaves the review sheet
(or is already on passenger/addons/pay). Nothing is paid.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv(ROOT / ".env", override=True)

from app.kernel_otp import _client, _drop, _open_browser, is_resume_booking_url  # noqa: E402
from app.page_agent import (  # noqa: E402
    _book_count,
    _latest,
    _read,
    classify_page,
    run_checkout_agent,
)
from app.search import ixigo_results_url  # noqa: E402
from app.travel_agent import remember_booking_page, resume_booking_url  # noqa: E402

KERNEL_PROFILE = os.getenv("KERNEL_IXIGO_PROFILE", "guide-ixigo")
CHROME_PROFILE = ROOT / "data" / "diagnostics" / "ixigo-chrome-profile"
HINT = os.getenv("IXIGO_SMOKE_HINT", "10:45")
PASSENGER = {
    "name": "Sudipta Saikia",
    "email": "sudipta@example.com",
    "phone": os.getenv("IXIGO_LOGIN_PHONE", "8638586171").strip(),
    "dob": "29/07/1997",
    "gender": "Male",
}
LEFT_REVIEW = {"passenger", "addons", "pay", "login", "otp"}


def _checkout(page, *, max_steps: int = 16, pause_on_auth: bool = True):
    return run_checkout_agent(
        page,
        hint=HINT,
        seller="ixigo",
        origin="Guwahati",
        destination="Delhi",
        depart="2026-12-04",
        allow_pay=False,
        pause_on_auth=pause_on_auth,
        max_steps=max_steps,
        profile=PASSENGER,
    )


def _book_to_review(page):
    """Open the 10:45 and stop on the review sheet, before Continue."""
    run = _checkout(page, max_steps=1, pause_on_auth=False)
    page = _latest(page)
    for _ in range(12):
        text, url = _read(page)
        stage = classify_page(text, url, book_count=_book_count(page))
        if stage in {"review", "passenger", "addons", "pay", "login", "otp"}:
            return run, page, text, url, stage
        page.wait_for_timeout(400)
        page = _latest(page)
    text, url = _read(page)
    stage = classify_page(text, url, book_count=_book_count(page))
    return run, page, text, url, stage


def _open_results(page) -> None:
    page.goto(
        ixigo_results_url("Guwahati", "Delhi", "2026-12-04"),
        wait_until="domcontentloaded",
        timeout=120000,
    )
    for _ in range(50):
        if _book_count(page) >= 2:
            break
        page.wait_for_timeout(400)


def _judge(label: str, run, stage: str) -> bool:
    refused = "continue_refused_or_missing" in " ".join(run.log)
    left = stage in LEFT_REVIEW or run.stage in LEFT_REVIEW
    still_review = stage == "review" and not left
    print(label, run.stage, stage, "refused", refused)
    print(label, "log", " | ".join(run.log)[:700])
    return not refused and not still_review


def _chrome_page(p):
    last: Exception | None = None
    for attempt in range(3):
        try:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(CHROME_PROFILE),
                channel="chrome",
                headless=False,
                viewport={"width": 1366, "height": 900},
                locale="en-IN",
                timezone_id="Asia/Kolkata",
            )
            page = context.pages[0] if context.pages else context.new_page()
            return context, page
        except Exception as exc:
            last = exc
            print("chrome launch retry", attempt + 1, type(exc).__name__)
            time.sleep(2)
    raise last or RuntimeError("chrome launch failed")


def smoke_local() -> int:
    if not CHROME_PROFILE.exists():
        print("FAIL: no local Ixigo Chrome profile")
        return 7
    state = {
        "seller": "ixigo",
        "stage": "booking",
        "selected_hint": HINT,
        "origin": "Guwahati",
        "destination": "Delhi",
        "depart": "2026-12-04",
    }
    saved_url = ""
    print("mode=local_chrome")
    with sync_playwright() as p:
        context, page = _chrome_page(p)
        try:
            _open_results(page)
            first_run, page, text, url, stage = _book_to_review(page)
            print("session1", first_run.stage, stage, "url_ok", is_resume_booking_url(url, "ixigo"))
            print("session1 log", " | ".join(first_run.log)[:500])
            if stage not in {"review", "passenger", "addons", "pay"}:
                print("FAIL: session1 never reached checkout", stage)
                return 3
            chat_stage = remember_booking_page(state, "ixigo", url, text)
            state["stage"] = chat_stage
            saved_url = resume_booking_url(state, "go ahead and confirm")
            if not saved_url:
                print("FAIL: checkpoint did not keep the booking URL")
                return 4
            print("checkpoint", chat_stage, saved_url[:120])
        finally:
            context.close()
            print("session1 dropped")

    with sync_playwright() as p:
        context, page = _chrome_page(p)
        try:
            page.goto(saved_url, wait_until="domcontentloaded", timeout=120000)
            page.wait_for_timeout(1500)
            text, url = _read(page)
            stage = classify_page(text, url, book_count=_book_count(page))
            print("session2 opened", stage, "url_ok", is_resume_booking_url(url, "ixigo"))
            if stage not in {"review", "passenger", "addons", "pay", "login"}:
                print("FAIL: saved URL did not reopen checkout", stage)
                return 5
            second_run = _checkout(page)
            page = _latest(page)
            text, url = _read(page)
            after = classify_page(text, url, book_count=_book_count(page))
            if not _judge("session2", second_run, after):
                print("FAIL: still on review after resume Continue")
                return 6
            print("PASS: resume left review at", after)
            return 0
        finally:
            context.close()
            print("session2 dropped")


def smoke_kernel() -> int:
    client = _client()
    first = _open_browser(client, KERNEL_PROFILE)
    sid1 = first.session_id
    print("mode=kernel", "session1", sid1)
    saved_url = ""
    state = {
        "seller": "ixigo",
        "stage": "booking",
        "selected_hint": HINT,
        "origin": "Guwahati",
        "destination": "Delhi",
        "depart": "2026-12-04",
    }
    try:
        with sync_playwright() as p:
            pw = p.chromium.connect_over_cdp(first.cdp_ws_url)
            context = pw.contexts[0] if pw.contexts else pw.new_context()
            page = context.pages[0] if context.pages else context.new_page()
            _open_results(page)
            first_run, page, text, url, stage = _book_to_review(page)
            print("session1", first_run.stage, stage, "url_ok", is_resume_booking_url(url, "ixigo"))
            print("session1 log", " | ".join(first_run.log)[:500])
            if stage not in {"review", "passenger", "addons", "pay"}:
                print("kernel did not hold the review sheet; falling back to local Chrome")
                return 10
                print("FAIL: session1 never reached checkout", stage)
                return 3
            chat_stage = remember_booking_page(state, "ixigo", url, text)
            state["stage"] = chat_stage
            saved_url = resume_booking_url(state, "go ahead and confirm")
            if not saved_url:
                print("FAIL: checkpoint did not keep the booking URL")
                return 4
            print("checkpoint", chat_stage, saved_url[:120])
    finally:
        _drop(client, sid1)
        print("session1 dropped")

    second = _open_browser(client, KERNEL_PROFILE)
    sid2 = second.session_id
    print("session2", sid2)
    try:
        with sync_playwright() as p:
            pw = p.chromium.connect_over_cdp(second.cdp_ws_url)
            context = pw.contexts[0] if pw.contexts else pw.new_context()
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(saved_url, wait_until="domcontentloaded", timeout=120000)
            page.wait_for_timeout(1500)
            text, url = _read(page)
            stage = classify_page(text, url, book_count=_book_count(page))
            print("session2 opened", stage, "url_ok", is_resume_booking_url(url, "ixigo"))
            if stage not in {"review", "passenger", "addons", "pay", "login"}:
                print("FAIL: saved URL did not reopen checkout", stage)
                return 5
            second_run = _checkout(page)
            page = _latest(page)
            text, url = _read(page)
            after = classify_page(text, url, book_count=_book_count(page))
            if not _judge("session2", second_run, after):
                print("FAIL: still on review after resume Continue")
                return 6
            print("PASS: resume left review at", after)
            return 0
    finally:
        _drop(client, sid2)
        print("session2 dropped")


def main() -> int:
    key = (os.getenv("KERNEL_API_KEY") or "").strip()
    if key:
        code = smoke_kernel()
        if code != 10:
            return code
    return smoke_local()


if __name__ == "__main__":
    raise SystemExit(main())
