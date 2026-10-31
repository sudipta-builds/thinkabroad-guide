"""Local smoke: WhatsApp step-conscience, then live Ixigo until before pay.

Nothing is paid. Exit 0 only when both parts pass.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env", override=True)

from app.page_agent import classify_page  # noqa: E402
from app.travel_agent import (  # noqa: E402
    _booking_page_reply,
    _mid_booking_ack,
    infer_intent,
)


class _Sess:
    def __init__(self, url: str) -> None:
        self.page = type("P", (), {"url": url})()


TRIP = {
    "origin": "Guwahati",
    "destination": "Delhi",
    "depart": "2026-12-04",
    "seller": "ixigo",
    "selected_hint": "12:30",
    "stage": "booking",
}


def _must(ok: bool, label: str) -> None:
    if not ok:
        raise SystemExit(f"FAIL conversation: {label}")


def smoke_conversation() -> None:
    ix = "https://www.ixigo.com/flight/booking/1"
    ct = "https://www.cleartrip.com/"
    cases = [
        (
            "home",
            {**TRIP, "seller": "cleartrip"},
            "Book Domestic One way Where from Search flights",
            ct,
        ),
        (
            "results",
            TRIP,
            "IndiGo 12:30 GAU ₹9065 Book SpiceJet 10:45 GAU ₹9722 Book",
            "https://www.ixigo.com/search/result/flight",
        ),
        (
            "addon_buy",
            TRIP,
            "Assured Free Cancellation No, Thanks Secure My Trip Fare Summary ₹9280 Continue",
            ix,
        ),
        (
            "review",
            TRIP,
            "Fare Summary Review Details Sudipta Saikia Continue",
            ix,
        ),
        (
            "passenger",
            {**TRIP, "name": "Anupam Sahu"},
            "Review & Traveller Details First & Middle Name Last Name Please select your title Mobile Number Email Continue",
            ix,
        ),
        (
            "addons",
            TRIP,
            "Choose your seat Add meal Skip add-ons Extra baggage Continue",
            ix,
        ),
        (
            "otp",
            TRIP,
            "Enter OTP OTP sent to your mobile Resend OTP",
            ix,
        ),
        (
            "pay",
            TRIP,
            "Pay to complete your booking UPI Pay now ₹12085",
            "https://www.ixigo.com/pay",
        ),
    ]
    for kind, state, page, url in cases:
        spoken = _booking_page_reply(dict(state), "12:30", page, _Sess(url))  # type: ignore[arg-type]
        shown = classify_page(page, url)
        print("step", kind, "classified", shown)
        print(" ", spoken[:220].replace("\n", " "))
        if kind == "pay":
            _must("Nothing charged yet" in spoken or "Ready to pay" in spoken, kind)
            _must("Stuck:" not in spoken, "pay must not look stuck")
        else:
            _must("Stuck:" in spoken, f"{kind} missing Stuck:")
            _must("recommend" in spoken.lower(), f"{kind} missing recommend")
            _must("Nothing paid" in spoken, f"{kind} missing nothing paid")
        if kind == "addon_buy":
            _must("No, Thanks" in spoken and "skip" in spoken.lower(), "addon")
            _must("look at the screenshot" not in spoken.lower(), "addon screenshot ask")
            _must("Continue is on the screenshot" not in spoken, "addon continue lie")
    _must(infer_intent({**TRIP, "stage": "booking", "stuck_kind": "addon_buy"}, "?") == "status", "? status")

    booking = {**TRIP, "stage": "review", "stuck_kind": "addon_buy"}
    _must(infer_intent(booking, "continue") == "book", "continue intent")
    _must(infer_intent(booking, "skip") == "book", "skip intent")
    ack = _mid_booking_ack(booking, "ixigo", "12:30", "continue")
    _must("Continuing the 12:30" not in ack, "canned continue")
    _must("stuck" in ack.lower() or "recommend" in ack.lower(), "honest ack")
    print("PASS conversation")


def smoke_live() -> int:
    os.environ["KERNEL_API_KEY"] = ""
    from ixigo_resume_smoke import smoke_local

    return int(smoke_local())


def main() -> int:
    print("== conversation smoke ==")
    smoke_conversation()
    print("== live Ixigo local Chrome (stop before pay) ==")
    return smoke_live()


if __name__ == "__main__":
    raise SystemExit(main())
