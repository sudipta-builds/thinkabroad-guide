"""Smoke the unknown-step loop the way checkout uses it.

Rails plan on the Cleartrip contact sheet, then a local Chromium form
(fill + verified Continue). Optional Gemini read of the real screenshot
when a vision key is configured.

  python scripts/unknown_step_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env", override=True)

CONTACT = (
    "Add-ons No meal, additional check-in baggage and seats selected. "
    "Add Contact details +91 Email address Continue Review traveller details"
)
PROFILE = {"email": "sudipta@example.com", "phone": "8638586171", "name": "Sudipta Saikia"}
SHOT = Path(
    r"C:\Users\Sudipta Saikia\.cursor\projects\c-thinkabroad-guide\assets"
    r"\Screenshot_2026-11-01_121855-042ffe1b-54d8-41fc-b407-c2cdc9f5f396.png"
)
HTML = """<!doctype html><html><body>
<div>Email address<input id="email"></div>
<div>Mobile number<input id="mobile"></div>
<button id="go" onclick="this.remove(); document.getElementById('next').hidden=false">Continue</button>
<p id="next" hidden>Review traveller details</p>
</body></html>"""


def rails() -> None:
    from app.step_read import format_unknown_plan, ground_unknown_plan

    plan = ground_unknown_plan(CONTACT, "", PROFILE, intent="give_facts")
    spoken = format_unknown_plan(plan)
    assert plan["action"] == "fill", plan
    assert plan["tap"] == "Continue", plan
    assert "skip" not in spoken.lower(), spoken
    assert "Nothing paid" in spoken
    print("rails", spoken)


def browser() -> None:
    from playwright.sync_api import sync_playwright

    from app.browser_hands import chromium_ready
    from app.page_agent import _apply_unknown_plan
    from app.step_read import ground_unknown_plan

    if not chromium_ready():
        print("browser SKIP chromium missing")
        return
    plan = ground_unknown_plan(CONTACT, "", PROFILE, intent="give_facts")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(HTML)
        notes = _apply_unknown_plan(page, plan)
        email = page.input_value("#email") if page.locator("#email").count() else ""
        body = page.inner_text("body")
        browser.close()
    print("browser", notes, "email", email, "body", body[:80])
    assert email == PROFILE["email"], (notes, email)
    assert "Review traveller details" in body, body


def vision() -> None:
    from app.config import get_settings
    from app.vision_agent import see_step, vision_enabled

    if not vision_enabled() or not SHOT.is_file():
        print("vision SKIP", "ready" if get_settings().vision_ready else "no key", SHOT.exists())
        return
    seen = see_step(str(SHOT), seller="cleartrip", hint="10:20", page_text=CONTACT)
    print("vision", seen)
    assert seen, "vision returned nothing"
    assert str(seen.get("kind")) != "pay"
    tap = str(seen.get("tap") or "")
    assert "pay" not in tap.lower()
    assert "skip add" not in tap.lower()


if __name__ == "__main__":
    rails()
    browser()
    vision()
    print("unknown-step smoke ok")
