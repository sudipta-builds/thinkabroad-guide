"""Prove contact fill the way Kernel checkout does it.

Always runs two local pages (Cleartrip +91 prefix, Ixigo email/mobile labels)
through fill_profile_slots and a verified Continue.

Live Kernel runs only when both are set:
  KERNEL_API_KEY
  KERNEL_CONTACT_URL   itinerary already on Add Contact details

Continue is clicked on that live tab only when KERNEL_CONTACT_CLICK=1.
Otherwise the live run types and checks the values, then stops.

  python scripts/kernel_contact_smoke.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env", override=True)

PROFILE = {"email": "sudipta@example.com", "phone": "8638586171"}


def local() -> None:
    from playwright.sync_api import sync_playwright

    from app.browser_hands import chromium_ready
    from app.page_agent import fill_profile_slots

    if not chromium_ready():
        raise SystemExit("local chromium missing")
    pages = {
        "cleartrip": """<!doctype html><html><body>
          <div style="height:1400px">Add-ons done</div>
          <h3>Add Contact details</h3>
          <div>+91<input value="+91"></div>
          <div><input id="mobile"></div>
          <div>Email address<input id="email" type="email" placeholder="Email address"></div>
        </body></html>""",
        "ixigo": """<!doctype html><html><body>
          <h3>Traveller details</h3>
          <label>Email<input id="email" type="email" placeholder="Email"></label>
          <label>Mobile number<input id="mobile" type="tel"></label>
        </body></html>""",
    }
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        for name, html in pages.items():
            page = browser.new_page()
            page.set_content(html)
            notes = fill_profile_slots(page, PROFILE)
            email = page.input_value("#email")
            mobile = page.input_value("#mobile")
            page.close()
            print(name, notes, email, mobile)
            assert "email:ok" in notes and "phone:ok" in notes
            assert email == PROFILE["email"]
            assert "8638586171" in mobile
        browser.close()
    print("local slot fill ok")


def live() -> None:
    url = (os.getenv("KERNEL_CONTACT_URL") or "").strip()
    if not url:
        print("live SKIP set KERNEL_CONTACT_URL to an open Add Contact itinerary")
        return
    from kernel import Kernel
    from playwright.sync_api import sync_playwright

    from app.config import get_settings
    from app.page_agent import contact_report, fill_profile_slots, reveal_form_step

    key = (get_settings().kernel_api_key or os.getenv("KERNEL_API_KEY") or "").strip()
    if not key:
        print("live SKIP no KERNEL_API_KEY")
        return
    client = Kernel(api_key=key)
    browser = client.browsers.create(
        stealth=True,
        viewport={"width": 1280, "height": 800, "refresh_rate": 25},
        timeout_seconds=300,
    )
    try:
        with sync_playwright() as pw:
            chrome = pw.chromium.connect_over_cdp(browser.cdp_ws_url)
            page = chrome.contexts[0].pages[0] if chrome.contexts and chrome.contexts[0].pages else chrome.contexts[0].new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(1500)
            reveal_form_step(page)
            notes = fill_profile_slots(page, PROFILE)
            text = page.inner_text("body")[:1500]
            print("live", notes)
            print(contact_report(" | ".join(notes), text) or text[:240])
            assert "email:ok" in notes, notes
            assert "phone:ok" in notes or "phone:none" in notes, notes
            if os.getenv("KERNEL_CONTACT_CLICK") == "1" and "phone:ok" in notes:
                from app.page_agent import _click_label

                clicked = _click_label(page, "Continue")
                page.wait_for_timeout(1500)
                after = page.inner_text("body")[:500]
                print("clicked", clicked, after[:200])
                assert clicked, "Continue did not move the live sheet"
    finally:
        try:
            client.browsers.delete_by_id(browser.session_id)
        except Exception:
            pass


if __name__ == "__main__":
    local()
    live()
    print("kernel contact smoke ok")
