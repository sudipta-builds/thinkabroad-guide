"""Compare rules brain vs LLM/vision brain on checkout fixtures.

Reports kind / action / recommend / reply diffs and whether impact is real.

  python scripts/checkout_brain_smoke.py
  python scripts/checkout_brain_smoke.py --vision   # also try screenshot when present
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

FIXTURES = [
    {
        "id": "contact_payment_sidebar",
        "text": (
            "Add Contact details Please enter Mobile Number +91 Email address Continue "
            "Payment options Unlock price Base fare Taxes Apply coupon"
        ),
        "url": "https://www.cleartrip.com/flights/itinerary/x",
        "seller": "Cleartrip",
        "hint": "14:20",
        "trip": "Guwahati → Delhi on 4 Dec",
        "profile": {"email": "sudipta@example.com"},
        "expect_kind": "form",
        "forbid_kind": {"pay"},
        "expect_action": "ask",
        "expect_reply_has": ["mobile", "nothing paid"],
        "forbid_reply": ["ready to pay", "pay yourself"],
    },
    {
        "id": "contact_empty_mobile",
        "text": "Add Contact details +91 Email address Mobile number Continue Review traveller details",
        "url": "https://www.cleartrip.com/flights/itinerary/x",
        "seller": "Cleartrip",
        "hint": "10:20",
        "trip": "Guwahati → Delhi on 4 Dec",
        "profile": {},
        "expect_kind": "form",
        "forbid_kind": {"pay"},
        "expect_action": "ask",
        "expect_reply_has": ["nothing paid"],
    },
    {
        "id": "contact_ready_fill",
        "text": "Add Contact details +91 Email address Mobile number Continue Review traveller details",
        "url": "https://www.cleartrip.com/flights/itinerary/x",
        "seller": "Cleartrip",
        "hint": "10:20",
        "trip": "Guwahati → Delhi on 4 Dec",
        "profile": {"email": "sudipta@example.com", "phone": "8638586171"},
        "expect_kind": "form",
        "forbid_kind": {"pay"},
        # rules may fill+Continue; LLM should not say pay
        "forbid_reply": ["ready to pay", "upi"],
    },
    {
        "id": "true_pay_sheet",
        "text": "Pay to complete your booking. Card number. UPI. Net banking. Pay now.",
        "url": "https://www.cleartrip.com/flights/itinerary/pay",
        "seller": "Cleartrip",
        "hint": "10:45",
        "trip": "Guwahati → Delhi on 4 Dec",
        "profile": {"email": "a@b.com", "phone": "8638586171"},
        "expect_kind": "pay",
        "expect_action": "stop",
        "expect_reply_has": ["nothing paid"],
        "forbid_tap": ["pay now", "buy"],
    },
    {
        "id": "addons_skip",
        "text": "Choose add-ons Add meals Skip add-ons Seat map Free cancellation",
        "url": "https://www.cleartrip.com/flights/itinerary/addons",
        "seller": "Cleartrip",
        "hint": "10:45",
        "trip": "Guwahati → Delhi on 4 Dec",
        "profile": {},
        "expect_kind": "addons",
        "expect_reply_has": ["nothing paid"],
    },
    {
        "id": "review_continue",
        "text": "Review your itinerary Continue Guwahati Delhi IndiGo 10:45",
        "url": "https://www.cleartrip.com/flights/itinerary/1",
        "seller": "Cleartrip",
        "hint": "10:45",
        "trip": "Guwahati → Delhi on 4 Dec",
        "profile": {},
        "expect_kind": "review",
        "expect_reply_has": ["nothing paid"],
    },
    {
        "id": "results_list",
        "text": "IndiGo 10:45 Non-stop ₹5,419 Book SpiceJet 12:30 ₹6,100 Book",
        "url": "https://www.cleartrip.com/flights/results",
        "seller": "Cleartrip",
        "hint": "10:45",
        "trip": "Guwahati → Delhi on 4 Dec",
        "profile": {},
        "expect_kind": "results",
        "expect_reply_has": ["nothing paid"],
    },
]

VISION_CASES = [
    {
        "id": "vision_review_form",
        "path": ASSETS / "WhatsApp_Image_2026-10-31_at_17.33.10-ec835f96-45b2-40f8-b97e-6a0524f29b89.jpg",
        "text": "Review traveller details Email Mobile Continue",
        "url": "https://www.ixigo.com/flight/booking/1",
        "seller": "Ixigo",
        "hint": "12:30",
        "trip": "Guwahati → Delhi",
        "profile": {"email": "a@b.com", "phone": "8638586171"},
        "forbid_kind": {"pay"},
        "forbid_tap": ["pay now", "secure my trip"],
        "expect_reply_has": ["nothing paid"],
    },
    {
        "id": "vision_results",
        "path": ASSETS / "Screenshot_2026-10-31_181509-64978d1a-cc2b-42fe-b427-7bc047a8564d.png",
        "text": "18:20 Book ₹ IndiGo Non-stop",
        "url": "https://www.cleartrip.com/flights/results",
        "seller": "Cleartrip",
        "hint": "18:20",
        "trip": "Guwahati → Delhi",
        "profile": {},
        "forbid_tap": ["pay now"],
        "expect_reply_has": ["nothing paid"],
    },
]


def _clip(s: str, n: int = 140) -> str:
    s = re.sub(r"\s+", " ", (s or "")).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _diff_fields(rules: dict, neural: dict) -> list[str]:
    out = []
    for key in ("kind", "action", "tap", "recommend"):
        a, b = str(rules.get(key) or ""), str(neural.get(key) or "")
        if a.lower() != b.lower():
            out.append(f"{key}:{a!r}->{b!r}")
    ra, rb = _clip(str(rules.get("reply") or "")), _clip(str(neural.get("reply") or ""))
    if ra.lower() != rb.lower():
        out.append("reply:changed")
    return out


def _check(case: dict, decision: dict) -> list[str]:
    fails: list[str] = []
    kind = str(decision.get("kind") or "")
    action = str(decision.get("action") or "")
    tap = str(decision.get("tap") or "")
    reply = str(decision.get("reply") or "").lower()
    if case.get("expect_kind") and kind != case["expect_kind"]:
        fails.append(f"kind={kind} want {case['expect_kind']}")
    if case.get("forbid_kind") and kind in case["forbid_kind"]:
        fails.append(f"forbidden kind={kind}")
    if case.get("expect_action") and action != case["expect_action"]:
        fails.append(f"action={action} want {case['expect_action']}")
    for needle in case.get("expect_reply_has") or []:
        if needle.lower() not in reply:
            fails.append(f"reply missing {needle!r}")
    for bad in case.get("forbid_reply") or []:
        if bad.lower() in reply:
            fails.append(f"reply has {bad!r}")
    for bad in case.get("forbid_tap") or []:
        if bad.lower() in tap.lower():
            fails.append(f"tap has {bad!r}")
    return fails


def run_case(case: dict, *, use_vision: bool) -> dict:
    from app.checkout_brain import decide_checkout, llm_brain, rules_brain
    from app.config import get_settings

    settings = get_settings()
    kwargs = dict(
        url=case.get("url") or "",
        seller=case.get("seller") or "Cleartrip",
        hint=case.get("hint") or "",
        trip=case.get("trip") or "",
        profile=case.get("profile") or {},
    )
    t0 = time.perf_counter()
    rules = rules_brain(case["text"], **kwargs)
    rules_ms = int((time.perf_counter() - t0) * 1000)

    image = ""
    if use_vision and case.get("path"):
        p = Path(case["path"])
        if p.is_file() and settings.vision_ready:
            image = str(p)

    t1 = time.perf_counter()
    neural = llm_brain(case["text"], **kwargs, image_path=image)
    llm_ms = int((time.perf_counter() - t1) * 1000)

    hybrid = decide_checkout(case["text"], **kwargs, image_path=image, prefer_llm=True)
    # Compare production hybrid vs pure rules (what travellers actually get).
    compare = hybrid if hybrid.get("source") in {"llm", "llm_vision"} else neural
    rules_fails = _check(case, rules)
    hybrid_fails = _check(case, hybrid) if hybrid else ["empty_hybrid"]
    neural_fails = _check(case, neural) if neural else ["empty_llm"]
    diffs = _diff_fields(rules, compare) if compare else ["llm_empty"]

    # Impact: hybrid changes kind/action vs rules, or wording-only.
    impact = "none"
    if not compare or compare.get("source") == "rules":
        impact = "no_llm" if not neural else "rules_fallback"
    elif "kind:" in " ".join(diffs) or "action:" in " ".join(diffs):
        impact = "decision"
    elif "reply:changed" in diffs or "recommend:" in " ".join(diffs):
        impact = "wording"
    if case["id"] == "contact_payment_sidebar":
        if rules.get("kind") == "pay" and hybrid.get("kind") == "form":
            impact = "safety_fix"
        elif hybrid.get("kind") == "form" and rules.get("kind") == "form" and impact == "none":
            impact = "same_safe"

    return {
        "id": case["id"],
        "rules": rules,
        "llm": neural,
        "hybrid": hybrid,
        "rules_ms": rules_ms,
        "llm_ms": llm_ms,
        "diffs": diffs,
        "impact": impact,
        "rules_ok": not rules_fails,
        "llm_ok": not neural_fails,
        "hybrid_ok": not hybrid_fails,
        "rules_fails": rules_fails,
        "llm_fails": neural_fails,
        "hybrid_fails": hybrid_fails,
        "vision": bool(image),
        "keys": {
            "gemini": bool(settings.gemini_api_key),
            "llm": bool(settings.llm_ready),
            "vision": bool(settings.vision_ready),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vision", action="store_true", help="Include screenshot vision cases")
    parser.add_argument("--json", action="store_true", help="Print raw JSON")
    args = parser.parse_args()

    cases = list(FIXTURES)
    if args.vision:
        cases.extend(VISION_CASES)

    rows = [run_case(c, use_vision=args.vision) for c in cases]
    if args.json:
        print(json.dumps(rows, indent=2, default=str))
        return 0

    print("Checkout brain smoke: rules vs LLM/vision\n")
    keys = rows[0]["keys"] if rows else {}
    print(f"Keys: gemini={keys.get('gemini')} llm={keys.get('llm')} vision={keys.get('vision')}\n")

    decision_impacts = 0
    wording_impacts = 0
    llm_empty = 0
    llm_pass = 0
    rules_pass = 0

    hybrid_pass = 0
    for row in rows:
        rules, neural, hybrid = row["rules"], row["llm"], row["hybrid"]
        print(f"== {row['id']} ==")
        print(
            f"  rules  [{row['rules_ms']}ms] kind={rules.get('kind')} action={rules.get('action')} "
            f"tap={rules.get('tap')!r} rec={_clip(str(rules.get('recommend') or ''), 60)}"
        )
        print(f"          reply: {_clip(str(rules.get('reply') or ''))}")
        if not neural:
            print(f"  llm    [{row['llm_ms']}ms] EMPTY")
            llm_empty += 1
        else:
            print(
                f"  llm    [{row['llm_ms']}ms] kind={neural.get('kind')} action={neural.get('action')} "
                f"tap={neural.get('tap')!r} src={neural.get('source')} "
                f"rec={_clip(str(neural.get('recommend') or ''), 60)}"
            )
            print(f"          reply: {_clip(str(neural.get('reply') or ''))}")
        print(
            f"  hybrid       kind={hybrid.get('kind')} action={hybrid.get('action')} "
            f"tap={hybrid.get('tap')!r} src={hybrid.get('source')} "
            f"rec={_clip(str(hybrid.get('recommend') or ''), 60)}"
        )
        print(f"          reply: {_clip(str(hybrid.get('reply') or ''))}")
        print(f"  diffs (rules→hybrid): {row['diffs'] or ['identical']}")
        print(
            f"  check: rules={'PASS' if row['rules_ok'] else 'FAIL ' + str(row['rules_fails'])} | "
            f"llm={'PASS' if row['llm_ok'] else 'FAIL ' + str(row['llm_fails'])} | "
            f"hybrid={'PASS' if row['hybrid_ok'] else 'FAIL ' + str(row['hybrid_fails'])} | "
            f"impact={row['impact']} vision={row['vision']}"
        )
        print()
        if row["rules_ok"]:
            rules_pass += 1
        if row["llm_ok"]:
            llm_pass += 1
        if row["hybrid_ok"]:
            hybrid_pass += 1
        if row["impact"] in {"decision", "safety_fix"}:
            decision_impacts += 1
        elif row["impact"] == "wording":
            wording_impacts += 1

    n = len(rows)
    print("--- summary ---")
    print(
        f"cases={n} rules_pass={rules_pass}/{n} llm_pass={llm_pass}/{n} "
        f"hybrid_pass={hybrid_pass}/{n} llm_empty={llm_empty}"
    )
    print(f"decision_impact={decision_impacts} wording_impact={wording_impacts}")
    if llm_empty == n:
        print("VERDICT: no LLM/vision available — brain falls back to rules; zero live impact until keys are set.")
        return 2
    if decision_impacts:
        print(
            "VERDICT: real impact on hybrid path — kind/action differs from pure rules on some fixtures "
            "(vision/unknown sheets). Rails still own contact≠pay, ready-fill, and never auto-Book."
        )
    elif wording_impacts:
        print(
            "VERDICT: soft impact — same kind/action as rules after rails, but reply/recommend wording differs. "
            "Contact≠pay was already fixed in rules; LLM mainly improves WhatsApp phrasing."
        )
    else:
        print(
            "VERDICT: little incremental impact on these fixtures — rules and hybrid agree. "
            "Value shows up on novel/unknown sheets not covered by regex."
        )
    hard = [r for r in rows if r["id"] in {"contact_payment_sidebar", "true_pay_sheet", "contact_ready_fill"}]
    if hard and not all(r["hybrid_ok"] for r in hard):
        print("HARD FAIL: contact≠pay, ready-fill, or true-pay stop broke under hybrid.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
