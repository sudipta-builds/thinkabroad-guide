"""Prod-identical Kernel booking from this machine (no WhatsApp, no port 8000).

This is the same call WhatsApp uses in app.travel_agent._run_kernel_login:
  begin_login(...) → _booking_page_reply(...)

It loads the repo .env (KERNEL_API_KEY, GEMINI_*, etc.), opens a real Kernel
browser, and prints status / url / screenshot / reply. Nothing is paid.

  # Same as typing Cleartrip 10:45 after trip is already known
  python scripts/prod_kernel_replay.py --seller cleartrip --hint 10:45

  # With contact facts (matches saved WhatsApp profile fields)
  python scripts/prod_kernel_replay.py --seller cleartrip --hint 10:45 --email sudipta@example.com

Exit codes:
  0 ok
  1 bad reply (home/error called payment, or empty search after a timed book)
  2 KERNEL_API_KEY missing
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env", override=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Prod Kernel booking path, local")
    parser.add_argument("--seller", default="cleartrip", choices=["cleartrip", "ixigo"])
    parser.add_argument("--hint", default="10:45")
    parser.add_argument("--origin", default="Guwahati")
    parser.add_argument("--destination", default="Delhi")
    parser.add_argument("--depart", default="2026-12-04")
    parser.add_argument("--phone", default="8638586171")
    parser.add_argument("--email", default="")
    parser.add_argument("--name", default="")
    parser.add_argument("--session-id", default="", help="Reuse a Kernel tab (prod resume)")
    parser.add_argument("--resume-url", default="", help="Saved itinerary URL if any")
    parser.add_argument("--chosen-tap", default="", help="e.g. 'Go to home' to mimic that tap")
    parser.add_argument("--otp-wait", type=int, default=20)
    args = parser.parse_args()

    from app.kernel_otp import begin_login, kernel_configured
    from app.page_agent import classify_page, site_error_page
    from app.travel_agent import _booking_page_reply

    if not kernel_configured():
        print("KERNEL_API_KEY missing — set it in .env (same key as Render)")
        return 2

    # Same profile keys WhatsApp passes into begin_login.
    profile = {
        "name": args.name,
        "email": args.email,
        "phone": args.phone,
        "dob": "",
        "gender": "",
    }
    print("prod_kernel_replay", args.seller, args.hint, "session", (args.session_id or "new")[:12])
    result = begin_login(
        seller=args.seller,
        whatsapp_phone=args.phone,
        explicit=args.phone,
        origin=args.origin,
        destination=args.destination,
        depart=args.depart,
        hint=args.hint,
        profile=profile,
        otp_wait_sec=args.otp_wait,
        resume_url=args.resume_url,
        chosen_tap=args.chosen_tap,
        session_id=args.session_id,
    )
    page = str(result.get("page") or "")
    detail = str(result.get("detail") or "")
    status = str(result.get("status") or "")
    url = str(result.get("url") or "")
    notes = str(result.get("notes") or "")
    stage = classify_page(page, url)
    print("status", status)
    print("stage", stage)
    print("url", url)
    print("detail", detail[:300])
    print("notes", notes[:500])
    print("screenshot", result.get("screenshot") or "")
    print("page", page[:500])

    state = {
        "origin": args.origin,
        "destination": args.destination,
        "depart": args.depart,
        "seller": args.seller,
        "selected_hint": args.hint,
        "phone": args.phone,
        "email": args.email,
        "name": args.name,
    }
    spoken = ""
    if page:
        spoken = _booking_page_reply(state, args.hint, page, type("S", (), {"page": None})())
        print("reply", spoken)

    home_like = stage == "home" or bool(
        re.search(r"where from|search flights|enter departure and arrival", page, re.I)
    )
    error_like = stage == "error" or site_error_page(page, url)
    if "payment page" in spoken.lower() and (home_like or error_like):
        print("FAIL home/error called payment page")
        return 1
    if args.hint and home_like and status in {"held", "already_logged_in", "fares"}:
        print("FAIL empty Cleartrip search after a timed book — recovery did not reopen results")
        return 1
    if error_like and status in {"held", "already_logged_in"}:
        print("FAIL acting-up / error sheet returned as a successful hold")
        return 1
    if "Timeout" in detail or "connect_over_cdp" in detail:
        print("FAIL kernel connect")
        return 1
    print("replay finished")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
