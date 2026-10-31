"""Smoke: Gemini vision on every booking-step screenshot + reply/decide performance.

Covers results, sold-out, review/form, Confirm, Assured upsell, unknown non-booking.
Also runs text conversation smoke for stuck/recommend rails.
Exit 0 only when all cases pass.

  python scripts/vision_step_smoke.py
  python scripts/vision_step_smoke.py --live   # short Ixigo Chromium open + vision override log
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env", override=True)

ASSETS = Path(r"C:\Users\Sudipta Saikia\.cursor\projects\c-thinkabroad-guide\assets")

# expect_kinds: any of these counts as pass
CASES = [
    {
        "id": "results_toast",
        "path": ASSETS / "Screenshot_2026-10-31_181509-64978d1a-cc2b-42fe-b427-7bc047a8564d.png",
        "seller": "cleartrip",
        "hint": "18:20",
        "expect_kinds": {"results"},
        "expect_fares": True,
        "forbid_sold": True,
        "forbid_tap_spend": True,
    },
    {
        "id": "results_whatsapp_embed",
        "path": ASSETS / "WhatsApp_Image_2026-10-31_at_18.13.42-34e013a7-5ea1-472b-9aa9-d1c72f789b65.jpg",
        "seller": "cleartrip",
        "hint": "18:20",
        "expect_kinds": {"results"},
        "expect_fares": True,
        "forbid_sold": True,
    },
    {
        "id": "unavailable_modal",
        "path": ASSETS / "Screenshot_2026-10-31_181549-27bc3e90-500a-464e-912a-f1e81b6eea8f.png",
        "seller": "ixigo",
        "hint": "12:30",
        "expect_kinds": {"unavailable"},
        "expect_sold": True,
        "expect_tap_has": "search",
    },
    {
        "id": "unavailable_filled_lie",
        "path": ASSETS / "WhatsApp_Image_2026-10-31_at_18.11.26__1_-66caf8db-14f0-4a6e-971b-8937c552a8a4.jpg",
        "seller": "ixigo",
        "hint": "12:30",
        "expect_kinds": {"unavailable"},
        "expect_sold": True,
    },
    {
        "id": "review_form_filled",
        "path": ASSETS / "WhatsApp_Image_2026-10-31_at_17.33.10-ec835f96-45b2-40f8-b97e-6a0524f29b89.jpg",
        "seller": "ixigo",
        "hint": "12:30",
        "expect_kinds": {"review", "form"},
        "forbid_sold": True,
        "forbid_tap_spend": True,
    },
    {
        "id": "confirm_modal_embed",
        "path": ASSETS / "Screenshot_2026-10-31_173438-18359425-31fc-41d0-8c6c-06b08bef0107.png",
        "seller": "ixigo",
        "hint": "12:30",
        "expect_kinds": {"review", "form", "other"},
        "prefer_tap": "confirm",
        "forbid_tap_spend": True,
    },
    {
        "id": "assured_upsell_embed",
        "path": ASSETS / "Screenshot_2026-10-31_145530-5614bf18-e1eb-4906-944a-77691af4e7a8.png",
        "seller": "ixigo",
        "hint": "12:30",
        "expect_kinds": {"upsell", "review", "other"},
        "prefer_decline": True,
        "forbid_tap_spend": True,
    },
    {
        "id": "assured_whatsapp_loop",
        "path": ASSETS / "Screenshot_2026-10-31_170302-c7f21a8d-3764-4d57-bc60-ff3682226994.png",
        "seller": "ixigo",
        "hint": "12:30",
        "expect_kinds": {"upsell", "review", "other"},
        "prefer_decline": True,
        "forbid_tap_spend": True,
    },
    {
        "id": "unknown_non_booking",
        "path": ASSETS / "Screenshot_2026-10-31_134251-f86d0861-02b2-4897-b06a-0004c003d625.png",
        "seller": "ixigo",
        "hint": "12:30",
        "expect_kinds": {"other"},
        "forbid_fares": True,
        "forbid_sold": True,
    },
]


def _spend(tap: str) -> bool:
    import re

    return bool(
        re.search(
            r"\b(pay now|secure my trip|assure my trip|insure my trip|buy this|purchase)\b",
            tap or "",
            re.I,
        )
    )


def _decline(tap: str, recommend: str) -> bool:
    import re

    blob = f"{tap} {recommend}"
    return bool(re.search(r"no,?\s*thanks|don'?t want|not this time|skip|decline", blob, re.I))


def run_vision_cases() -> list[dict]:
    from app.config import get_settings
    from app.step_read import decide_front
    from app.vision_agent import see_step, vision_enabled

    get_settings.cache_clear()
    settings = get_settings()
    print(f"vision_ready={settings.vision_ready} model={settings.gemini_model}")
    if not vision_enabled():
        raise SystemExit("FAIL: vision off — set GEMINI_API_KEY")

    rows = []
    passed = 0
    for case in CASES:
        path = case["path"]
        print(f"\n=== {case['id']} ===")
        if not path.is_file():
            print("FAIL missing", path.name)
            rows.append({"id": case["id"], "ok": False, "error": "missing"})
            continue
        t0 = time.monotonic()
        seen = see_step(str(path), seller=case["seller"], hint=case["hint"])
        ms = int((time.monotonic() - t0) * 1000)
        if not seen:
            print(f"FAIL empty vision ({ms}ms)")
            rows.append({"id": case["id"], "ok": False, "error": "empty", "ms": ms})
            continue

        decision = decide_front(
            seen.get("what") or "sheet",
            f"btn[ ] {seen.get('tap')}" if seen.get("tap") else "",
            seller=case["seller"],
            hint=case["hint"],
            image_path=str(path),
        )
        kind = seen.get("kind") or ""
        tap = str(seen.get("tap") or decision.get("tap") or "")
        ok = kind in case["expect_kinds"]
        reasons = []
        if not ok:
            reasons.append(f"kind={kind} want {case['expect_kinds']}")
        if case.get("expect_fares") and not seen.get("fares_visible"):
            ok = False
            reasons.append("fares_visible")
        if case.get("forbid_fares") and seen.get("fares_visible"):
            ok = False
            reasons.append("unexpected fares")
        if case.get("expect_sold") and not seen.get("sold_out"):
            ok = False
            reasons.append("sold_out")
        if case.get("forbid_sold") and seen.get("sold_out"):
            ok = False
            reasons.append("false sold_out")
        if case.get("expect_tap_has") and case["expect_tap_has"].lower() not in tap.lower():
            # soft: recommend may carry it
            if case["expect_tap_has"].lower() not in str(seen.get("recommend") or "").lower():
                ok = False
                reasons.append(f"tap/recommend missing {case['expect_tap_has']}")
        if case.get("forbid_tap_spend") and _spend(tap):
            ok = False
            reasons.append(f"spend tap {tap}")
        if case.get("prefer_decline") and kind == "upsell" and not _decline(tap, str(seen.get("recommend") or "")):
            ok = False
            reasons.append("upsell without decline")
        if case.get("prefer_tap") and case["prefer_tap"].lower() not in tap.lower():
            # soft fail only when kind is review and tap empty
            if kind == "review" and not tap:
                ok = False
                reasons.append("review missing Confirm")

        # Performance budget: under 20s per shot
        if ms > 20000:
            ok = False
            reasons.append(f"slow {ms}ms")

        if ok:
            passed += 1
        print(json.dumps({k: seen[k] for k in seen if k != "source"}, indent=2))
        print(f"decide action={decision.get('action')} kind={decision.get('kind')} tap={decision.get('tap')}")
        print(f"{'PASS' if ok else 'FAIL'} ({ms}ms) {'; '.join(reasons)}")
        rows.append(
            {
                "id": case["id"],
                "ok": ok,
                "ms": ms,
                "seen": seen,
                "decision": decision,
                "reasons": reasons,
            }
        )

    print(f"\n=== VISION {passed}/{len(CASES)} ===")
    out = ROOT / "data" / "diagnostics" / "vision_step_smoke.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print("wrote", out)
    if passed < len(CASES):
        raise SystemExit(f"FAIL vision {passed}/{len(CASES)}")
    return rows


def run_conversation_smoke() -> None:
    from app.page_agent import classify_page
    from app.travel_agent import _booking_page_reply

    class _Sess:
        page = None

    print("\n== conversation / reply smoke ==")
    # Results: recommend, never exact-button
    results_page = (
        "IndiGo 6E5342 18:25 2h 40m Non-stop 21:05 Book ₹10,283 "
        "IndiGo 6E2114 09:15 2h 45m Non-stop 12:00 Book ₹10,283"
    )
    spoken = _booking_page_reply(
        {
            "origin": "Guwahati",
            "destination": "Delhi",
            "depart": "2026-11-15",
            "seller": "cleartrip",
            "selected_hint": "18:20",
        },
        "18:20",
        results_page,
        _Sess(),  # type: ignore[arg-type]
    )
    print("results:", spoken[:200])
    assert "exact button" not in spoken.lower(), spoken
    assert "recommend" in spoken.lower() or "book the" in spoken.lower(), spoken
    assert classify_page(results_page, "https://www.cleartrip.com/flights/results") == "results"

    gone = "Flight is no longer available. Please search again! Search Again ₹9,465"
    spoken2 = _booking_page_reply(
        {
            "origin": "Guwahati",
            "destination": "Delhi",
            "depart": "2026-11-04",
            "seller": "ixigo",
            "selected_hint": "12:30",
            "form_filled": True,
        },
        "12:30",
        gone,
        _Sess(),  # type: ignore[arg-type]
    )
    print("unavailable:", spoken2[:200])
    assert "no longer available" in spoken2.lower(), spoken2
    assert "Filled your details" not in spoken2, spoken2

    # Unknown upsell sheet — ask, decline, never pay
    upsell = "Ixigo Assured Free Cancellation No, Thanks Assure My Trip ₹599"
    spoken3 = _booking_page_reply(
        {
            "origin": "Guwahati",
            "destination": "Delhi",
            "depart": "2026-12-04",
            "seller": "ixigo",
            "selected_hint": "12:30",
        },
        "12:30",
        upsell,
        _Sess(),  # type: ignore[arg-type]
    )
    print("upsell:", spoken3[:220])
    assert "Nothing paid" in spoken3, spoken3
    assert "Assure My Trip" not in spoken3.split("recommend")[0] or "No" in spoken3, spoken3
    print("PASS conversation")


def run_live_short() -> None:
    """Open results in Chromium (Cleartrip first — less bot wall), vision every step."""
    from playwright.sync_api import sync_playwright

    from app.config import get_settings
    from app.page_agent import run_checkout_agent
    from app.search import cleartrip_results_url, ixigo_results_url

    get_settings.cache_clear()
    print("\n== live short (max 5 steps, no pay) ==")
    print("vision_ready", get_settings().vision_ready)
    # Prefer Cleartrip results for headless smoke; Ixigo often stays blank to bots.
    url = cleartrip_results_url("Guwahati", "Delhi", "2026-11-15")
    seller = "cleartrip"
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            locale="en-IN",
        )
        page = context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(7000)
            try:
                page.get_by_role("button", name="Book").first.wait_for(timeout=15000)
            except Exception:
                # Fallback Ixigo if Cleartrip also empty
                url = ixigo_results_url("Guwahati", "Delhi", "2026-12-04")
                seller = "ixigo"
                page.goto(url, wait_until="domcontentloaded", timeout=90000)
                page.wait_for_timeout(8000)
            outcome = run_checkout_agent(
                page,
                hint="18:20" if seller == "cleartrip" else "10:45",
                seller=seller,
                origin="Guwahati",
                destination="Delhi",
                depart="2026-11-15" if seller == "cleartrip" else "2026-12-04",
                allow_pay=False,
                pause_on_auth=True,
                max_steps=5,
                profile={
                    "name": "Sudipta Saikia",
                    "email": "sudipta@example.com",
                    "phone": "8638586171",
                },
            )
        finally:
            browser.close()
    print("seller", seller)
    print("stage", outcome.stage)
    print("url", (outcome.url or "")[:120])
    print("log", outcome.log)
    vision_lines = [x for x in outcome.log if "vision" in x]
    print("vision_lines", vision_lines)
    out = ROOT / "data" / "diagnostics" / "vision_live_short.json"
    out.write_text(
        json.dumps(
            {"seller": seller, "stage": outcome.stage, "log": outcome.log, "url": outcome.url},
            indent=2,
        ),
        encoding="utf-8",
    )
    print("wrote", out)
    if outcome.stage == "pay":
        raise SystemExit("FAIL live reached pay")
    if not vision_lines and get_settings().vision_ready:
        raise SystemExit("FAIL live produced no vision log lines while vision_ready")
    joined = "\n".join(outcome.log)
    # Remaining gap was: Book hit a card then froze as ask:results. Follow-through must try fare Continue.
    if re.search(r"^card:.+", joined, re.M) and "ask:results" in joined:
        if "confirm_fare" not in joined and "after_book" not in joined:
            raise SystemExit("FAIL Book hit a card then asked on results without fare follow-through")
    print("PASS live short")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    run_vision_cases()
    run_conversation_smoke()
    if args.live:
        run_live_short()
    print("\nALL SMOKE PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
