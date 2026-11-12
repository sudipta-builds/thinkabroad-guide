"""Ixigo checkout decision engine for logged-in returning travellers.

Classifies each checkout item as AUTO_FILL, ASK_USER, or REQUIRE_APPROVAL.
Does not click, type, or pay. Callers execute AUTO_FILL with existing page helpers
and stop when the plan says continue is not allowed.

Authorised default: do not purchase optional extras (free cancellation, insurance,
seats, meals). Buying those, and any payment, stays REQUIRE_APPROVAL.
OTP or a "code accepted" flag never counts as logged-in by itself.
"""

from __future__ import annotations

import re
from typing import Any

AUTO_FILL = "AUTO_FILL"
ASK_USER = "ASK_USER"
REQUIRE_APPROVAL = "REQUIRE_APPROVAL"

_MANDATORY = (
    "traveller",
    "email",
    "phone",
    "pincode",
    "billing_address",
    "city",
    "billing_state",
)


def session_after_otp(*, otp_status: str = "", logged_in: bool = False, still_otp: bool = False) -> str:
    """Authenticated only when the live session shows a logged-in Ixigo account.

    A submitted OTP, a held code, or a fares page is not authentication.
    """
    status = (otp_status or "").strip().lower()
    if still_otp or status in {"still_otp", "bad_code", "otp_sent", "held", "boxes_miss", "resume_lost"}:
        return "unauthenticated"
    if status in {"ok", "accepted", "verified", "typed", "fares"} and not logged_in:
        return "unauthenticated"
    if logged_in:
        return "authenticated"
    return "unknown"


def extras_mode(profile: dict[str, Any] | None) -> str:
    """decline = authorised skip (do not purchase). buy = user already approved a paid extra. ask = unknown."""
    pref = str((profile or {}).get("extras_preference") or "decline").strip().lower()
    if pref in {"buy", "assured", "with_assured", "secure"}:
        return "buy"
    if pref in {"ask", "unknown", "unset"}:
        return "ask"
    return "decline"


def _money(text: str) -> list[str]:
    return re.findall(r"₹\s*[\d,]+", text or "")[:4]


def plan_ixigo_checkout(obs: dict[str, Any], profile: dict[str, Any] | None = None) -> dict[str, Any]:
    """Pure plan from DOM observations + saved profile. No browser calls."""
    profile = profile or {}
    obs = obs or {}
    items: list[dict[str, str]] = []
    mode = extras_mode(profile)
    stage = str(obs.get("stage") or "")
    page_text = str(obs.get("text") or "")

    if str(obs.get("session") or "") == "unauthenticated" or stage in {"login", "otp"}:
        items.append(
            {
                "field": "session",
                "class": ASK_USER,
                "action": "authenticate",
                "detail": "Ixigo session is not authenticated. Send the login mobile or the OTP digits. "
                "A typed OTP is not login until the account session is visible.",
            }
        )

    gaps = [str(g) for g in (obs.get("traveller_gaps") or []) if str(g)]
    saved_name = str(profile.get("name") or "").strip()
    if not gaps:
        items.append({"field": "traveller", "class": AUTO_FILL, "action": "verified", "detail": "Saved traveller is on the form."})
    elif saved_name and gaps:
        # First try the saved profile. If it is still empty after that click, the caller asks.
        items.append(
            {
                "field": "traveller",
                "class": AUTO_FILL,
                "action": "select_saved",
                "detail": f"Select saved traveller {saved_name}. Missing: {', '.join(gaps)}.",
            }
        )
    elif saved_name:
        items.append(
            {
                "field": "traveller",
                "class": AUTO_FILL,
                "action": "select_saved",
                "detail": f"Select saved traveller {saved_name}.",
            }
        )
    else:
        items.append(
            {
                "field": "traveller",
                "class": ASK_USER,
                "action": "missing",
                "detail": "Passenger name is not saved. Send the traveller name and title.",
            }
        )

    def _contact(field: str, *, empty: bool, invalid: bool = False, label: str) -> None:
        saved = str(profile.get(field) or "").strip()
        if invalid:
            items.append(
                {
                    "field": field,
                    "class": ASK_USER,
                    "action": "invalid",
                    "detail": f"{label} is invalid. Send a corrected {label.lower()}.",
                }
            )
            return
        if empty and not saved:
            items.append(
                {
                    "field": field,
                    "class": ASK_USER,
                    "action": "missing",
                    "detail": f"{label} is missing. Send {label.lower()} and I will type it.",
                }
            )
            return
        if empty and saved:
            items.append(
                {
                    "field": field,
                    "class": AUTO_FILL,
                    "action": "fill",
                    "detail": f"Fill saved {label.lower()}.",
                }
            )
            return
        items.append({"field": field, "class": AUTO_FILL, "action": "verified", "detail": f"{label} is on the form."})

    on_form = stage in {"review", "passenger", "name_confirm", ""} or (
        obs.get("on_review") and stage not in {"pay", "addons"}
    )
    if on_form:
        _contact("email", empty=bool(obs.get("email_empty")), label="Email")
        _contact("phone", empty=bool(obs.get("phone_empty")), label="Mobile")
        _contact(
            "pincode",
            empty=bool(obs.get("pincode_empty")),
            invalid=bool(obs.get("pincode_invalid")),
            label="Pincode",
        )
        _contact("billing_address", empty=bool(obs.get("address_empty")), label="Billing address")
        _contact("city", empty=bool(obs.get("city_empty")), label="City")
        _contact("billing_state", empty=bool(obs.get("state_empty")), label="State")

    needs_choice = bool(
        obs.get("assured_open")
        or obs.get("secure_trip_open")
        or obs.get("cancellation_unselected")
    )
    if needs_choice and not obs.get("cancellation_declined"):
        prices = " ".join(_money(page_text))
        price_bit = f" Prices on the page: {prices}." if prices else ""
        if mode == "decline":
            items.append(
                {
                    "field": "free_cancellation",
                    "class": AUTO_FILL,
                    "action": "decline",
                    "detail": "Authorised preference is no paid extras. Press No, Thanks or Without Assured."
                    + price_bit,
                }
            )
        elif mode == "buy":
            items.append(
                {
                    "field": "free_cancellation",
                    "class": REQUIRE_APPROVAL,
                    "action": "approved_buy",
                    "detail": "Paid Free Cancellation was approved earlier." + price_bit,
                }
            )
        else:
            items.append(
                {
                    "field": "free_cancellation",
                    "class": REQUIRE_APPROVAL,
                    "action": "choose",
                    "detail": (
                        "Free Cancellation needs a choice (With Assured costs extra; "
                        "Without Assured / No, Thanks does not)."
                        + price_bit
                        + " Reply no thanks to decline, or name the paid option to approve it. Nothing paid until you do."
                    ),
                }
            )

    if stage == "addons" or obs.get("on_addons"):
        if mode == "decline":
            items.append(
                {
                    "field": "addons",
                    "class": AUTO_FILL,
                    "action": "skip",
                    "detail": "Authorised preference is to skip seats, meals, and other paid add-ons.",
                }
            )
        elif mode == "ask":
            items.append(
                {
                    "field": "addons",
                    "class": REQUIRE_APPROVAL,
                    "action": "choose",
                    "detail": "Add-ons are optional paid services. Reply skip to leave them, or name a seat or meal to approve it.",
                }
            )
        else:
            items.append(
                {
                    "field": "addons",
                    "class": REQUIRE_APPROVAL,
                    "action": "approved_buy",
                    "detail": "A paid add-on was approved earlier. I will not invent a seat or meal.",
                }
            )

    if stage == "pay" or obs.get("payment_open"):
        total = str(obs.get("total") or "").strip() or "?"
        items.append(
            {
                "field": "payment",
                "class": REQUIRE_APPROVAL,
                "action": "stop",
                "detail": (
                    f"Payment page is open. Final fare {total}. "
                    "Reply to approve payment. I will not click Pay."
                ),
            }
        )

    if obs.get("validation_error") and not any(i["field"] == "pincode" and i["action"] == "invalid" for i in items):
        items.append(
            {
                "field": "validation",
                "class": ASK_USER,
                "action": "invalid",
                "detail": str(obs.get("validation_error")),
            }
        )

    asks = [i for i in items if i["class"] == ASK_USER]
    # Decline/skip are AUTO. Unapproved purchases and payment block Continue / Pay.
    blocks = [
        i
        for i in items
        if i["class"] == REQUIRE_APPROVAL and i["action"] in {"choose", "stop"}
    ]
    # Saved values still need a successful fill before Continue.
    pending_fill = [
        i
        for i in items
        if i["class"] == AUTO_FILL and i["action"] in {"fill", "select_saved", "decline"}
    ]
    continue_ok = not asks and not blocks and not pending_fill and stage not in {"login", "otp", "pay"}
    if obs.get("payment_open"):
        continue_ok = False
    handoff = ""
    if asks:
        handoff = " ".join(i["detail"] for i in asks[:3]) + " Nothing paid."
    elif blocks:
        handoff = " ".join(i["detail"] for i in blocks[:2]) + " Nothing paid."
    elif pending_fill:
        handoff = " ".join(i["detail"] for i in pending_fill[:3]) + " Nothing paid."
    elif stage == "pay" or obs.get("payment_open"):
        pay = next((i for i in items if i["field"] == "payment"), None)
        handoff = (pay or {}).get("detail") or "Payment page is open. I will not click Pay. Nothing paid."
        if "nothing paid" not in handoff.lower():
            handoff += " Nothing paid."
    return {
        "items": items,
        "asks": asks,
        "blocks": blocks,
        "pending_fill": pending_fill,
        "continue_ok": continue_ok,
        "handoff": handoff,
        "extras": mode,
        "classes": sorted({i["class"] for i in items}),
    }


def observe_ixigo_checkout(page, profile: dict[str, Any] | None = None) -> dict[str, Any]:
    """DOM-first snapshot. Screenshots are not used here."""
    from app.page_agent import (
        _adult_traveller_gaps,
        _decline_radio_checked,
        _email_input_empty,
        _ixigo_assured_comparison_open,
        _ixigo_buy_this_visible,
        _ixigo_pin_invalid,
        _on_addons_page,
        _read,
        classify_page,
        extract_trip_facts,
        payment_sheet_open,
    )

    profile = profile or {}
    try:
        text, url = _read(page)
    except Exception:
        text, url = "", ""
    try:
        stage = classify_page(text or "", url or "")
    except Exception:
        stage = ""
    try:
        facts = extract_trip_facts(text or "", url or "") or {}
    except Exception:
        facts = {}
    try:
        gaps = list(_adult_traveller_gaps(page) or [])
    except Exception:
        gaps = []
    try:
        email_empty = bool(_email_input_empty(page))
    except Exception:
        email_empty = not bool(profile.get("email"))
    try:
        pin_invalid = bool(_ixigo_pin_invalid(page))
    except Exception:
        pin_invalid = False
    try:
        secure = bool(_ixigo_buy_this_visible(page))
    except Exception:
        secure = False
    try:
        assured = bool(_ixigo_assured_comparison_open(page))
    except Exception:
        assured = False
    try:
        declined = bool(_decline_radio_checked(page))
    except Exception:
        declined = False
    hay = text or ""
    phone_empty = bool(re.search(r"please enter (?:a )?mobile|please enter (?:a )?phone", hay, re.I)) and not str(
        profile.get("phone") or ""
    ).strip()
    # When the prompt is on screen and we have no evidence of a value, treat as empty.
    if re.search(r"please enter (?:a )?mobile|please enter (?:a )?phone", hay, re.I):
        phone_empty = True
    address_empty = bool(re.search(r"please enter (?:a |your )?address", hay, re.I))
    city_empty = bool(re.search(r"please enter (?:a |your )?city", hay, re.I))
    state_empty = bool(re.search(r"please (?:enter|select).{0,20}state", hay, re.I))
    pincode_empty = bool(re.search(r"please enter (?:a )?pin\s*code|pincode is required", hay, re.I))
    validation = ""
    if pin_invalid:
        validation = "Pincode is invalid."
    elif re.search(r"please enter a valid email", hay, re.I):
        validation = "Email is invalid."
    session = "unknown"
    if stage in {"login", "otp"} or re.search(r"enter otp|log in to ixigo|login/signup", hay, re.I):
        session = "unauthenticated"
    elif re.search(r"\blog\s*out\b|view profile|ixigoer", hay, re.I):
        session = "authenticated"
    return {
        "text": hay[:4000],
        "url": url or "",
        "stage": "addons" if _on_addons_page(hay, stage) else stage,
        "on_review": stage in {"review", "passenger", "name_confirm"} or bool(
            re.search(r"review & traveller|review details", hay, re.I)
        ),
        "on_addons": bool(_on_addons_page(hay, stage)),
        "traveller_ok": not gaps,
        "traveller_gaps": gaps,
        "email_empty": email_empty,
        "phone_empty": phone_empty and not re.search(r"\b[6-9]\d{9}\b", hay),
        "pincode_empty": pincode_empty,
        "pincode_invalid": pin_invalid,
        "address_empty": address_empty,
        "city_empty": city_empty,
        "state_empty": state_empty,
        "assured_open": assured,
        "secure_trip_open": secure,
        "cancellation_unselected": bool(
            re.search(r"please select an option before proceeding|don.?t want free cancellation", hay, re.I)
        )
        and not declined,
        "cancellation_declined": declined or (not assured and not secure and not re.search(
            r"please select an option before proceeding", hay, re.I
        )),
        "payment_open": bool(payment_sheet_open(hay, stage) and stage == "pay") or stage == "pay",
        "total": str(facts.get("total") or ""),
        "validation_error": validation,
        "session": session,
    }
