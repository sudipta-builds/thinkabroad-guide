"""Run Gemini vision on known failure screenshots and print Instinct-style judgements.

Usage (from repo root, with GEMINI_API_KEY in .env):
  python scripts/vision_fixture_test.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

ASSETS = Path(r"C:\Users\Sudipta Saikia\.cursor\projects\c-thinkabroad-guide\assets")

# Expected from the Oct 31 failure screenshots the traveller sent.
CASES = [
    {
        "id": "cleartrip_results_toast",
        "path": ASSETS / "Screenshot_2026-10-31_181509-64978d1a-cc2b-42fe-b427-7bc047a8564d.png",
        "seller": "cleartrip",
        "hint": "18:20",
        "expect_kind": "results",
        "expect_fares": True,
        "expect_sold": False,
        "dom_trap": "No flight data available toast over live Books",
    },
    {
        "id": "cleartrip_whatsapp_stuck",
        "path": ASSETS / "WhatsApp_Image_2026-10-31_at_18.13.42-34e013a7-5ea1-472b-9aa9-d1c72f789b65.jpg",
        "seller": "cleartrip",
        "hint": "18:20",
        "expect_kind": "results",
        "expect_fares": True,
        "expect_sold": False,
        "dom_trap": "Bot asked which button; fare list with Book was visible",
    },
    {
        "id": "ixigo_sold_out_modal",
        "path": ASSETS / "Screenshot_2026-10-31_181549-27bc3e90-500a-464e-912a-f1e81b6eea8f.png",
        "seller": "ixigo",
        "hint": "12:30",
        "expect_kind": "unavailable",
        "expect_fares": False,
        "expect_sold": True,
        "dom_trap": "Flight no longer available — not a filled traveller form",
    },
    {
        "id": "ixigo_filled_lie",
        "path": ASSETS / "WhatsApp_Image_2026-10-31_at_18.11.26__1_-66caf8db-14f0-4a6e-971b-8937c552a8a4.jpg",
        "seller": "ixigo",
        "hint": "12:30",
        "expect_kind": "unavailable",
        "expect_fares": False,
        "expect_sold": True,
        "dom_trap": "Bot said Filled your details while sold-out modal was open",
    },
]


def main() -> int:
    from app.config import get_settings
    from app.vision_agent import see_step, vision_enabled

    get_settings.cache_clear()
    settings = get_settings()
    print(f"vision_ready={settings.vision_ready} model={settings.gemini_model} guide_vision={settings.guide_vision}")
    if not vision_enabled():
        print("FAIL: vision not enabled (need GEMINI_API_KEY and GUIDE_VISION not 0)")
        return 2

    passed = 0
    rows = []
    for case in CASES:
        path = case["path"]
        print(f"\n=== {case['id']} ===")
        print(f"file: {path.name if path.exists() else 'MISSING'}")
        print(f"trap: {case['dom_trap']}")
        if not path.is_file():
            rows.append({**case, "ok": False, "error": "missing file"})
            print("FAIL: missing file")
            continue
        t0 = time.monotonic()
        seen = see_step(
            str(path),
            seller=case["seller"],
            hint=case["hint"],
            page_text=case["dom_trap"],
        )
        ms = int((time.monotonic() - t0) * 1000)
        if not seen:
            print(f"FAIL: empty vision ({ms}ms)")
            rows.append({**case, "ok": False, "error": "empty", "ms": ms})
            continue
        kind_ok = seen.get("kind") == case["expect_kind"]
        fares_ok = bool(seen.get("fares_visible")) == case["expect_fares"]
        sold_ok = bool(seen.get("sold_out")) == case["expect_sold"]
        # WhatsApp composite shots may embed the site screenshot; allow results|unavailable only as kinds we care about.
        if case["id"].startswith("cleartrip") and seen.get("fares_visible"):
            kind_ok = seen.get("kind") in {"results", "other"} and not seen.get("sold_out")
            if seen.get("kind") == "results":
                kind_ok = True
        if case["id"].startswith("ixigo") and seen.get("sold_out"):
            kind_ok = seen.get("kind") == "unavailable"
        ok = kind_ok and sold_ok and (fares_ok or case["expect_sold"])
        if ok:
            passed += 1
        print(json.dumps(seen, indent=2))
        print(
            f"{'PASS' if ok else 'FAIL'} kind={seen.get('kind')} "
            f"fares={seen.get('fares_visible')} sold={seen.get('sold_out')} "
            f"depart={seen.get('best_depart')} ({ms}ms)"
        )
        print(f"recommend: {seen.get('recommend')}")
        rows.append({"id": case["id"], "ok": ok, "ms": ms, "seen": seen})

    print(f"\n=== SUMMARY {passed}/{len(CASES)} passed ===")
    out = ROOT / "data" / "diagnostics" / "vision_fixture_results.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(f"wrote {out}")
    return 0 if passed == len(CASES) else 1


if __name__ == "__main__":
    raise SystemExit(main())
