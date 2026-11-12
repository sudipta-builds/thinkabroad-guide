"""Locks the WhatsApp failures: wrong month, forgotten trip, blank pages, re-asking."""

from __future__ import annotations

import os
import re
import tempfile
import unittest

os.environ["GUIDE_STEP_READ"] = "0"
from pathlib import Path

from app.page_agent import (
    card_matches_depart_hint,
    classify_page,
    complete_ixigo_login_after_otp,
    control_digest,
    deterministic_action,
    extract_trip_facts,
    install_ixigo_otpless_tap,
    interpret_itinerary_create,
    ixigo_auth_session_signals,
    parse_action,
    parse_login_phone,
    run_checkout_agent,
    verify_trip_facts,
)
from app.browser_hands import (
    BrowserSession,
    _alt_clock,
    _clock_needles,
    time_in_card,
    known_flight_host,
    looks_like_card,
    payment_control,
    should_capture,
    url_allowed,
)
from app.search import (
    cleartrip_results_url,
    easemytrip_results_url,
    flight_results_url,
    ixigo_results_url,
    named_seller,
    next_bookable_seller,
    search_seller,
    seller_order,
    seller_results_url,
)
from app.twilio_wa import TYPING_REFRESH_SECONDS, composing, remember_inbound, typing_form
from app.config import get_settings
from app.db import choose_db_path
from app.intent import explicit_date, week_window
from app.travel_agent import (
    _booking_page_reply,
    _booking_seller_id,
    _cached_fares_reply,
    _browse_is_search,
    _coerce_kind,
    _direct_results,
    _bookable_live_url,
    _wants_other_seller,
    _hold_live,
    _search_ack,
    _force_browse,
    _human_reply,
    _indian_mobile,
    _options_reply,
    _pick_best,
    _should_browse,
    absorb_details,
    fare_options,
    infer_intent,
    _memory_lines,
    _reasks,
    _recommendation,
    _recover_trip,
    redact_card,
    redact_for_history,
)


class DateAndMemory(unittest.TestCase):
    def test_november_is_not_read_as_october(self) -> None:
        self.assertEqual(explicit_date("check for 30th Nov"), "2026-11-30")
        self.assertEqual(explicit_date("guwahati to delhi, 23rd nov, cheapest"), "2026-11-23")
        self.assertEqual(explicit_date("Nov 30"), "2026-11-30")

    def test_first_week_of_december_is_not_1_november(self) -> None:
        self.assertIsNone(explicit_date("1st week"))
        self.assertEqual(explicit_date("guwahati to delhi, 1st week of december"), "2026-12-01")
        self.assertEqual(week_window("first week of dec"), ("2026-12-01", "2026-12-07"))
        prior = [{"role": "user", "body": "guwahati to delhi, 1st week of december"}]
        state = _recover_trip({"depart": "2026-11-01"}, prior, "1st week")
        self.assertEqual(state["depart"], "2026-12-01")
        self.assertEqual(state["depart_until"], "2026-12-07")
        self.assertIn("2026-12-01 through 2026-12-07", " ".join(_memory_lines(state)))

    def test_birth_date_does_not_become_the_flight_date(self) -> None:
        self.assertIsNone(explicit_date("sudipta@example.com, 29/07/1997, Male"))
        state = _recover_trip(
            {
                "origin": "Guwahati",
                "destination": "Delhi",
                "depart": "2026-12-04",
                "stage": "booking",
                "selected_hint": "10:45",
            },
            [],
            "sudipta@example.com, 29/07/1997, Male",
        )
        self.assertEqual(state["depart"], "2026-12-04")
        self.assertEqual(state["dob"], "29/07/1997")
        self.assertEqual(state["email"], "sudipta@example.com")
        self.assertEqual(state["gender"], "Male")
        named = absorb_details(
            {"stage": "booking"},
            "Sudipta Saikia, sudiptasaikia6@gmail.com, 29/07/1997",
        )
        self.assertEqual(named.get("name"), "Sudipta Saikia")
        self.assertEqual(named.get("email"), "sudiptasaikia6@gmail.com")
        self.assertEqual(named.get("dob"), "29/07/1997")
        named = absorb_details(named, "Sudipta Saikia, Male")
        self.assertEqual(named.get("name"), "Sudipta Saikia")
        self.assertEqual(named.get("gender"), "Male")
        self.assertEqual(infer_intent(state, "sudipta@example.com, 29/07/1997, Male"), "details")
        stuck = _recover_trip(
            {
                "origin": "Guwahati",
                "destination": "Delhi",
                "depart": "2027-07-29",
                "dob": "29/07/1997",
                "stage": "booking",
            },
            [],
            "Sudipta Saikia",
        )
        self.assertEqual(stuck["depart"], "2026-12-04")
        self.assertEqual(stuck["name"], "Sudipta Saikia")
        self.assertEqual(infer_intent(stuck, "Sudipta Saikia"), "details")
        casual = absorb_details(
            {"phone": "8638586171"},
            "sudipta saikia, 29/07/1997/male, jorhat, assam, 785001",
        )
        self.assertEqual(casual.get("name"), "Sudipta Saikia")
        self.assertEqual(casual.get("dob"), "29/07/1997")
        self.assertEqual(casual.get("gender"), "Male")
        self.assertEqual(casual.get("city"), "Jorhat")
        self.assertEqual(casual.get("billing_state"), "Assam")
        self.assertEqual(casual.get("pincode"), "785001")
        self.assertIn("Jorhat", str(casual.get("billing_address") or ""))
        mailed = absorb_details(casual, "sudipta saikia, sudiptasaikia6@gmail.com, India, Jorhat, 785001")
        self.assertEqual(mailed.get("email"), "sudiptasaikia6@gmail.com")
        self.assertEqual(mailed.get("name"), "Sudipta Saikia")
        self.assertEqual(mailed.get("city"), "Jorhat")

    def test_half_details_are_named_and_full_details_are_ready(self) -> None:
        from app.travel_agent import passenger_details_reply, passenger_gaps

        half = {"name": "Sudipta Saikia", "email": "sudipta@example.com", "stage": "booking"}
        spoken = passenger_details_reply(half)
        self.assertIn("date of birth", spoken)
        self.assertIn("gender", spoken)
        self.assertIn("I have your name, email", spoken)
        ready = {
            "name": "Sudipta Saikia",
            "email": "sudipta@example.com",
            "dob": "29/07/1997",
            "gender": "Male",
            "phone": "8638586171",
        }
        self.assertEqual(passenger_gaps(ready), [])
        self.assertEqual(passenger_details_reply(ready), "")

    def test_a_fill_question_reopens_ixigo_and_does_not_claim_the_form(self) -> None:
        from app.travel_agent import (
            _asks_for_filled_form,
            _form_status_reply,
            _hold_live,
            _merge_intent,
            _usable_reply,
        )

        trip = {"origin": "Guwahati", "destination": "Delhi", "depart": "2026-12-04", "stage": "booking"}
        for message in (
            "did you fill up all the details?",
            "show me the ss, what you filled?",
            "in ixigo?",
            "ixigo",
        ):
            self.assertTrue(_asks_for_filled_form(message))
            self.assertEqual(_merge_intent("search", "chat", message, trip), "status")
        self.assertFalse(_hold_live({**trip, "busy_at": "1"}, "in ixigo?", busy=True, changed=False))
        saved = {
            **trip,
            "name": "Sudipta Saikia",
            "email": "sudipta@example.com",
            "phone": "8638586171",
            "dob": "29/07/1997",
            "gender": "Male",
        }
        spoken = _form_status_reply(saved)
        self.assertIn("8638586171", spoken)
        self.assertIn("won't call that filled", spoken)
        self.assertNotIn("8638586272", spoken)
        self.assertEqual(
            _usable_reply(saved, "Yes, all passenger details are filled. Nothing has been charged.", "Opening Ixigo."),
            "Opening Ixigo.",
        )
        self.assertEqual(
            _usable_reply(saved, "Ixigo isn't usable for booking right now.", "Opening Ixigo."),
            "Opening Ixigo.",
        )

    def test_click_continue_stays_on_the_review_step_without_a_link(self) -> None:
        from app.travel_agent import _booking_page_reply, _merge_intent, infer_intent

        booking = {
            "stage": "booking",
            "seller": "ixigo",
            "origin": "Guwahati",
            "destination": "Delhi",
            "depart": "2026-12-04",
            "selected_hint": "10:45",
        }
        self.assertEqual(infer_intent(booking, "click continue"), "book")
        self.assertEqual(_merge_intent("chat", "search", "click continue", booking), "book")

        class _Page:
            url = "https://www.ixigo.com/flight/booking/1044/abc?fareKey=GAU-DEL-6E5342"

        class _Session:
            page = _Page()

        spoken = _booking_page_reply(
            booking,
            "10:45",
            "Fare Summary Review Details Sudipta Saikia Continue",
            _Session(),  # type: ignore[arg-type]
        )
        self.assertIn("Stuck:", spoken)
        self.assertIn("recommend", spoken.lower())
        self.assertNotIn("look at the screenshot", spoken.lower())
        self.assertNotIn("http", spoken)
        self.assertNotIn("moved", spoken.lower())
        cancel = _booking_page_reply(
            {**booking, "selected_hint": "12:30"},
            "12:30",
            "Fare Summary Total Amount ₹9,280 Free Cancellation Buy This Continue",
            _Session(),  # type: ignore[arg-type]
        )
        self.assertIn("Stuck:", cancel)
        self.assertIn("Free Cancellation", cancel)
        self.assertTrue("No, Thanks" in cancel or "No Thanks" in cancel)
        self.assertTrue("skip" in cancel.lower() or "yes" in cancel.lower())
        self.assertNotIn("look at the screenshot", cancel.lower())
        self.assertNotIn("Continue is on the screenshot", cancel)
        radio = _booking_page_reply(
            {**booking, "selected_hint": "12:30", "name": "Sudipta Saikia"},
            "12:30",
            "Add Free Cancellation to your trip Please select an option before proceeding "
            "Assured Free Cancellation Assured Flex I don't want Free Cancellation. Pay ₹3,200 fee "
            "Traveller Details Choose from the saved list Mr Sudipta Saikia Continue",
            _Session(),  # type: ignore[arg-type]
        )
        self.assertIn("I don't want Free Cancellation", radio)
        self.assertTrue("yes" in radio.lower() or "skip" in radio.lower())
        # Empty Adult 1 wins over Assured — fill traveller first, not Free Cancellation yes.
        blank_adult = _booking_page_reply(
            {**booking, "selected_hint": "6:20", "name": "Sudipta Saikia"},
            "6:20",
            "Add Free Cancellation to your trip Please select an option before proceeding "
            "I don't want Free Cancellation. Pay ₹3,999 fee if you cancel or reschedule "
            "Traveller Details Choose from the saved list Mr Sudipta Saikia "
            "Adult 1 Please select your title First & Middle Name Last Name Continue",
            _Session(),  # type: ignore[arg-type]
        )
        self.assertRegex(blank_adult, r"fill|tick|saved traveller|Adult 1", re.I)
        self.assertIn("Continue", blank_adult)
        self.assertNotIn("I'll press I don't want Free Cancellation", blank_adult)
        assured = _booking_page_reply(
            {**booking, "selected_hint": "12:30"},
            "12:30",
            "Assured Free Cancellation only ₹1,599 With Assured Without Assured No, Thanks Secure My Trip",
            _Session(),  # type: ignore[arg-type]
        )
        self.assertIn("Assured", assured)
        self.assertIn("No, Thanks", assured)
        self.assertTrue("skip" in assured.lower() or "yes" in assured.lower() or "no, thanks" in assured.lower())
        asking = {**booking, "stage": "booking", "stuck_kind": "addon_buy", "options": [{"depart": "12:30", "price": "9,722", "airline": "IndiGo"}]}
        self.assertEqual(infer_intent(asking, "?"), "status")
        self.assertEqual(_cached_fares_reply(asking, "?", "chat") or "", "")
        self.assertEqual(_cached_fares_reply(asking, "?", "status") or "", "")
        again = _booking_page_reply(
            {**booking, "stuck_kind": "addon_buy", "selected_hint": "12:30"},
            "12:30",
            "Fare Summary Total Amount ₹9,280 Free Cancellation Buy This Continue",
            _Session(),  # type: ignore[arg-type]
        )
        self.assertIn("Still the same block", again)
        from app.travel_agent import _mid_booking_ack

        ack = _mid_booking_ack({**booking, "stage": "review", "stuck_kind": "addon_buy"}, "ixigo", "12:30", "continue")
        self.assertIn("stuck", ack.lower())
        self.assertNotIn("Continuing the 12:30", ack)

    def test_an_unknown_button_is_read_from_the_page(self) -> None:
        from unittest.mock import patch

        from app.step_read import safe_tap

        page = "Trip Shield cover for this flight. Not this time. Add shield now."
        self.assertEqual(safe_tap("Not this time", page), "Not this time")
        self.assertEqual(safe_tap("Secure My Trip", "Assured Secure My Trip No, Thanks"), "")
        self.assertEqual(safe_tap("A button we invented", page), "")
        os.environ["GUIDE_STEP_READ"] = "1"
        try:
            with (
                patch("app.step_read.step_read_enabled", return_value=True),
                patch(
                    "app.llm.converse",
                    return_value={
                        "kind": "upsell",
                        "what": "Ixigo is selling Trip Shield on this fare",
                        "why": "an optional extra, not the passenger form",
                        "recommend": "skip it",
                        "tap": "Not this time",
                    },
                ),
            ):
                spoken = _booking_page_reply(
                    {
                        "origin": "Guwahati",
                        "destination": "Delhi",
                        "depart": "2026-12-04",
                        "seller": "ixigo",
                        "selected_hint": "12:30",
                    },
                    "12:30",
                    page,
                    type("S", (), {"page": type("P", (), {"url": "https://www.ixigo.com/flight/booking/1"})()})(),
                )
        finally:
            os.environ["GUIDE_STEP_READ"] = "0"
        self.assertIn("Trip Shield", spoken)
        self.assertIn("Not this time", spoken)
        self.assertIn("haven't tapped", spoken.lower())
        self.assertNotIn("Buy This", spoken)
        from app.step_read import accept_choice

        pending = {"tap": "Not this time", "page": page, "kind": "upsell"}
        self.assertEqual(accept_choice("yes", pending), "Not this time")
        self.assertEqual(accept_choice("continue", pending), "Not this time")
        self.assertEqual(accept_choice("yes", {"tap": "", "page": page, "kind": "upsell"}), "Not this time")
        self.assertEqual(
            accept_choice("yes", {"tap": "", "page": "Fare Summary Continue Traveller Details", "kind": "upsell"}),
            "Continue",
        )
        self.assertEqual(
            accept_choice("yes", {"tap": "", "page": "Fare Summary Traveller Details", "kind": "upsell"}),
            "__recommended__",
        )
        radio = "Add Free Cancellation I don't want Free Cancellation Assured Continue Mr Sudipta Saikia"
        self.assertEqual(accept_choice("yes", {"tap": "Decline", "page": radio, "kind": "upsell"}), "I don't want Free Cancellation")
        self.assertEqual(accept_choice("Secure My Trip", {"tap": "No, Thanks", "page": "No, Thanks Secure My Trip", "kind": "upsell"}), "")
        self.assertEqual(accept_choice("pay now", {"tap": "No, Thanks", "page": "Pay now No, Thanks", "kind": "upsell"}), "")
        self.assertEqual(
            accept_choice("Click 'No thanks'", {"tap": "", "page": "Free Cancellation No Thanks Secure My Trip", "kind": "addon_buy"}),
            "No Thanks",
        )
        confirm_page = "Review Details Please ensure that your name matches your govt ID Confirm Edit Mr Sudipta"
        self.assertEqual(accept_choice("confirm", {"tap": "", "page": confirm_page, "kind": "name_confirm"}), "Confirm")
        self.assertEqual(accept_choice("edit", {"tap": "Confirm", "page": confirm_page, "kind": "name_confirm"}), "Edit")
        self.assertEqual(accept_choice("exit", {"tap": "Confirm", "page": confirm_page, "kind": "name_confirm"}), "Edit")
        from app.step_read import decide_front, decide_sheet, forward_tap, is_name_confirm, is_paid_upsell, upsell_choice_labels
        from app.travel_agent import extract_user_ask, infer_intent

        self.assertTrue(is_name_confirm(confirm_page))
        self.assertFalse(is_paid_upsell(confirm_page))
        self.assertEqual(forward_tap(confirm_page), "Confirm")
        named_sheet = decide_sheet(confirm_page, {})
        self.assertEqual(named_sheet.get("action"), "ask")
        self.assertEqual(named_sheet.get("tap"), "Confirm")
        self.assertIn("Edit", named_sheet.get("fills") or [])
        named = decide_front(confirm_page, "btn[ ] Confirm\nbtn[ ] Edit", seller="ixigo", hint="12:30")
        self.assertEqual(named.get("action"), "ask")
        self.assertEqual(named.get("tap"), "Confirm")
        radio_choices = (
            "Add Free Cancellation Please select an option before proceeding "
            "Assured Free Cancellation Assured Flex I don't want Free Cancellation Continue"
        )
        self.assertEqual(
            upsell_choice_labels(radio_choices),
            [
                "Assured Free Cancellation",
                "Assured Flex",
                "I don't want Free Cancellation",
            ],
        )
        self.assertEqual(
            accept_choice(
                "Assured Free Cancellation",
                {"tap": "I don't want Free Cancellation", "page": radio_choices, "kind": "upsell"},
            ),
            "Assured Free Cancellation",
        )
        self.assertEqual(
            accept_choice("flex", {"tap": "", "page": radio_choices, "kind": "upsell"}),
            "Assured Flex",
        )
        self.assertEqual(
            accept_choice("I don't want free cancellation", {"tap": "", "page": radio_choices, "kind": "upsell"}),
            "I don't want Free Cancellation",
        )
        upsell = decide_front(
            "Free Cancellation No, Thanks Secure My Trip",
            "btn[ ] No, Thanks\nbtn[ ] Secure My Trip",
            seller="ixigo",
        )
        self.assertEqual(upsell.get("action"), "ask")
        booking = {
            "stage": "booking",
            "stuck_kind": "name_confirm",
            "stuck_page": confirm_page,
            "selected_hint": "12:30",
            "origin": "Guwahati",
            "destination": "Delhi",
            "depart": "2026-12-04",
        }
        self.assertEqual(infer_intent(booking, "confirm"), "book")
        self.assertEqual(extract_user_ask("Click 'Confirm'", booking).get("tap"), "Confirm")
        self.assertEqual(extract_user_ask("go to add-ons", booking).get("intent"), "book")
        self.assertEqual(extract_user_ask("why stuck?", booking).get("intent"), "status")
        from app.step_read import arm_pending_step, choice_phrase

        self.assertEqual(choice_phrase("Click 'No thanks'"), "No thanks")
        st: dict = {}
        arm_pending_step(st, "Assured No Thanks Continue", "addon_buy", tap="No Thanks")
        self.assertEqual(st["pending_step"]["tap"], "No Thanks")
        self.assertIn("stuck_page", st)

    def test_control_digest_and_unknown_steps_wait(self) -> None:
        import inspect
        from unittest.mock import patch

        from app.step_read import read_step
        from app.travel_agent import _booking_page_reply

        class _Page:
            url = "https://www.ixigo.com/flight/booking/unknown"

            def evaluate(self, _script):
                return (
                    "radio[ ] Assured\n"
                    "radio[ ] I don't want Free Cancellation\n"
                    "check[ ] Mr Sudipta Saikia\n"
                    "btn[ ] Continue"
                )

        digest = control_digest(_Page())
        self.assertIn("radio[ ] Assured", digest)
        self.assertIn("check[ ] Mr Sudipta Saikia", digest)
        src = inspect.getsource(control_digest)
        self.assertIn("input[type=radio]", src)
        self.assertIn("[role=checkbox]", src)
        os.environ["GUIDE_STEP_READ"] = "1"
        try:
            with (
                patch("app.step_read.step_read_enabled", return_value=True),
                patch("app.llm.converse") as converse,
            ):
                converse.return_value = {
                    "kind": "other",
                    "what": "Ixigo is asking for trip insurance I have not seen before",
                    "why": "not a known login or traveller form",
                    "recommend": "skip it",
                    "tap": "Not this time",
                }
                heard = read_step(
                    "Trip cover Not this time",
                    seller="ixigo",
                    hint="12:30",
                    controls="btn[ ] Not this time\ncheck[ ] Mr X",
                )
                prompt = converse.call_args[0][1]
                spoken = _booking_page_reply(
                    {
                        "origin": "Guwahati",
                        "destination": "Delhi",
                        "depart": "2026-12-04",
                        "seller": "ixigo",
                        "selected_hint": "12:30",
                    },
                    "12:30",
                    "Unknown insurance sheet Not this time",
                    type("S", (), {"page": _Page()})(),
                )
        finally:
            os.environ["GUIDE_STEP_READ"] = "0"
        self.assertIsNotNone(heard)
        self.assertEqual(heard["kind"], "other")
        self.assertIn("Controls", prompt)
        self.assertIn("check[ ] Mr X", prompt)
        self.assertIn("insurance", spoken.lower())
        self.assertIn("haven't tapped", spoken.lower())
        loop = inspect.getsource(run_checkout_agent)
        self.assertIn("return _pause_unknown", loop)
        pause = loop.split("if action is None:")[-1]
        self.assertIn("_pause_unknown", pause)
        self.assertLess(pause.index("if not chosen_tap"), pause.index("_ask_model"))

    def test_front_sheet_is_the_step_and_saved_people_are_not_duplicated(self) -> None:
        import inspect

        from app.page_agent import _SURFACE_JS, apply_front_text, read_surface
        from app.travel_agent import _addon_upsell, _booking_page_reply

        behind = (
            "Fare Summary I don't want Free Cancellation Traveller Details "
            "Choose from the saved list Mr Sudipta Saikia Mr Sudipta Saikia Continue"
        )
        surface = {"overlay": True, "text": "Free Cancellation No, Thanks", "controls": "btn[ ] No, Thanks"}
        front = apply_front_text(behind, surface)
        self.assertEqual(front, "Free Cancellation No, Thanks")
        self.assertNotEqual(_addon_upsell(front), "radio")
        # Saved-list copy owns the full page — buried "I don't want" is not the Assured gate.
        self.assertEqual(_addon_upsell(behind), "")
        radio_gate = (
            "Add Free Cancellation Please select an option before proceeding "
            "Assured Free Cancellation Assured Flex I don't want Free Cancellation Continue"
        )
        self.assertEqual(_addon_upsell(radio_gate), "radio")
        self.assertIn("[role=dialog]", _SURFACE_JS)
        self.assertIn("overlay", inspect.getsource(read_surface))

        class _Sheet:
            url = "https://www.ixigo.com/flight/booking/1"

            def evaluate(self, _script):
                return {"overlay": True, "text": "Free Cancellation No, Thanks", "controls": "btn[ ] No, Thanks"}

        spoken = _booking_page_reply(
            {
                "origin": "Guwahati",
                "destination": "Delhi",
                "depart": "2026-12-04",
                "seller": "ixigo",
                "selected_hint": "12:30",
            },
            "12:30",
            behind,
            type("S", (), {"page": _Sheet()})(),
        )
        self.assertIn("No, Thanks", spoken)
        self.assertNotIn("I don't want Free Cancellation vs", spoken)
        fill = inspect.getsource(run_checkout_agent).split('if kind == "fill_passenger"')[1][:2800]
        self.assertIn("kept_saved_traveller", fill)
        self.assertIn("_fill_passenger_fields", fill)
        self.assertLess(fill.index("kept_saved_traveller"), fill.index("_fill_passenger_fields"))
        self.assertIn("behind_sheet", fill)
        self.assertIn("_clear_front_sheet", fill)

    def test_checkout_links_stay_out_of_whatsapp(self) -> None:
        from app.travel_agent import _clean, _hide_checkout_link, _usable_reply

        booking = {"origin": "Guwahati", "destination": "Delhi", "depart": "2026-12-04", "seller": "ixigo"}

        dumped = (
            "I moved Guwahati to Delhi on 2026-12-04 forward on Ixigo. Nothing paid. "
            "The screenshot is this step. You can also open "
            "https://www.ixigo.com/flight/booking/1044/abc?fareKey=GAU-DEL-6E5342-04122026"
        )
        hidden = _hide_checkout_link(dumped)
        self.assertNotIn("http", hidden)
        self.assertNotIn("fareKey", hidden)
        self.assertEqual(_usable_reply(booking, dumped, "Continuing the 10:45 on Ixigo."), "Continuing the 10:45 on Ixigo.")
        self.assertNotIn("http", _clean(dumped, []))
        self.assertNotIn("also open", _clean(dumped, []).lower())

    def test_form_is_ready_only_when_title_and_email_stick(self) -> None:
        from app.page_agent import _passenger_missing, _passenger_ready

        ready = ["title:ok", "first:ok", "last:ok", "email:ok", "phone:ok", "dob:none"]
        self.assertTrue(_passenger_ready(ready))
        self.assertEqual(_passenger_missing(ready), [])
        half = ["title:miss", "first:ok", "last:ok", "email:miss", "phone:ok", "dob:none"]
        self.assertFalse(_passenger_ready(half))
        self.assertEqual(_passenger_missing(half), ["title", "email"])
        # A value that did not stick is not a filled email, even when the name did.
        soft = ["title:ok", "first:ok", "last:ok", "email:set", "phone:ok", "dob:none"]
        self.assertFalse(_passenger_ready(soft))
        self.assertIn("email", _passenger_missing(soft))

    def test_deepseek_is_read_before_the_rules(self) -> None:
        from unittest.mock import patch

        from app.travel_agent import _merge_intent, _usable_reply, resolve_intent

        booking = {"stage": "booking", "origin": "Guwahati", "destination": "Delhi", "depart": "2026-12-04"}
        message = "sudipta@example.com, 29/07/1997, Male"
        self.assertEqual(_merge_intent("details", "search", message, booking), "details")
        self.assertEqual(_merge_intent("chat", "select", "the 10:45", {"stage": "results"}), "select")
        self.assertEqual(_merge_intent("pay", "book", "pay now", booking), "pay")
        self.assertEqual(
            _usable_reply(booking, "That fare is ₹9,000.", "Saved. I'll put that on the form."),
            "Saved. I'll put that on the form.",
        )
        with patch("app.travel_agent.get_settings") as settings, patch(
            "app.llm.converse",
            return_value={"intent": "details", "flight_hint": "", "reply": "Got those. I'll fill the form and stop before payment."},
        ) as asked:
            settings.return_value.llm_ready = True
            kind, spoken = resolve_intent(message, booking, [])
        asked.assert_called_once()
        self.assertEqual(kind, "details")
        self.assertIn("fill the form", spoken)

    def test_passenger_profile_is_not_the_kernel_browser_name(self) -> None:
        import inspect

        from app.kernel_otp import begin_login

        source = inspect.getsource(begin_login)
        self.assertIn("passenger = dict(profile or {})", source)
        self.assertIn("browser_profile = _profile(", source)
        self.assertIn("profile=passenger", source)
        self.assertIn("_connect_browser(", source)
        self.assertIn("ephemeral=ephemeral", source)
        self.assertIn('"" if ephemeral else session_id', source)

    def test_a_second_route_does_not_delete_the_first(self) -> None:
        state = _recover_trip({}, [], "guwahati to delhi, 1st week of december")
        state = _recover_trip(state, [], "also delhi to mumbai")
        self.assertEqual(state["origin"].lower(), "delhi")
        self.assertEqual(state["destination"].lower(), "mumbai")
        self.assertFalse(state.get("depart"))
        text = " ".join(_memory_lines(state)).lower()
        self.assertIn("mumbai", text)
        self.assertIn("guwahati", text)
        self.assertIn("2026-12-01", text)
        restored = _recover_trip(state, [], "guwahati")
        self.assertEqual(restored["depart"], "2026-12-01")
        self.assertIn("delhi", restored["destination"].lower())

    def test_repo_database_is_not_the_one_that_survives_deploy(self) -> None:
        project = Path("/opt/render/project/src")
        mount = Path("/var/data")
        self.assertEqual(choose_db_path(Path("data/guide.db"), project=project, mount=mount), mount / "guide.db")
        self.assertEqual(
            choose_db_path(project / "data" / "guide.db", project=project, mount=mount),
            mount / "guide.db",
        )
        outside = Path("C:/outside/custom.db") if os.name == "nt" else Path("/tmp/custom.db")
        self.assertEqual(choose_db_path(outside, project=project, mount=mount), outside)
        self.assertEqual(choose_db_path(Path("data/guide.db"), project=project, mount=None), Path("data/guide.db"))

    def test_same_ones_keeps_the_saved_trip(self) -> None:
        prior = [{"role": "user", "body": "guwahati to delhi, 23rd nov, cheapest"}]
        state = _recover_trip({}, prior, "same ones")
        self.assertEqual(state["depart"], "2026-11-23")
        self.assertEqual(state["preference"], "cheapest")
        self.assertTrue(state.get("origin"))
        self.assertTrue(state.get("destination"))

    def test_memory_lists_trip_and_notes(self) -> None:
        lines = _memory_lines(
            {
                "origin": "Guwahati",
                "destination": "Delhi",
                "depart": "2026-11-23",
                "preference": "cheapest",
                "email": "a@b.com",
                "notes": ["www.google.com showed no fares."],
            }
        )
        text = " ".join(lines)
        self.assertIn("2026-11-23", text)
        self.assertIn("cheapest", text)
        self.assertIn("a@b.com", text)
        self.assertIn("showed no fares", text)


class IntentGuards(unittest.TestCase):
    def setUp(self) -> None:
        self.trip = {"origin": "Guwahati", "destination": "Delhi", "depart": "2026-11-23"}

    def test_known_trip_is_not_asked_again(self) -> None:
        self.assertTrue(_force_browse(self.trip, "same ones"))
        self.assertTrue(_force_browse(self.trip, "what about cleartrip?"))
        self.assertTrue(_force_browse(self.trip, "yes, yes and cheapest"))
        self.assertFalse(_force_browse(self.trip, "stop"))
        self.assertFalse(_force_browse({}, "delhi"))

    def test_named_seller_restarts_while_a_search_is_open(self) -> None:
        self.assertTrue(_should_browse(self.trip, "what about cleartrip?", busy=True, changed=False))
        self.assertFalse(_should_browse(self.trip, "?", busy=True, changed=False))
        self.assertFalse(_should_browse(self.trip, "what did you get?", busy=True, changed=False))

    def test_reask_wording_is_caught(self) -> None:
        self.assertTrue(_reasks("What route and date should I search, and do you care most about cheapest?"))
        self.assertFalse(_reasks("Checking live sites for Guwahati to Delhi on 2026-11-23."))
        self.assertTrue(_reasks("Whenever you're ready, tell me where you'd like to fly from and to, and your travel dates."))

    def test_known_trip_is_not_asked_back(self) -> None:
        trip = {"origin": "Guwahati", "destination": "Delhi", "depart": "2026-11-23", "preference": "cheapest"}
        spoken = _human_reply(trip, "What route and date should I search?", browsing=True, busy=False)
        self.assertNotIn("What route", spoken)
        self.assertIn("Guwahati", spoken)
        self.assertIn("2026-11-23", spoken)

    def test_busy_keeps_the_agents_own_sentence(self) -> None:
        trip = {"origin": "Guwahati", "destination": "Delhi", "depart": "2026-12-01", "depart_until": "2026-12-07"}
        spoken = _human_reply(trip, "Cleartrip is open and I'm reading the fares.", browsing=False, busy=True)
        self.assertEqual(spoken, "Cleartrip is open and I'm reading the fares.")

    def test_booking_words_do_not_reopen_search(self) -> None:
        trip = {"origin": "Guwahati", "destination": "Delhi", "depart": "2026-12-01", "depart_until": "2026-12-07", "stage": "results"}
        self.assertEqual(infer_intent(trip, "can you book the flight?"), "book")
        self.assertEqual(infer_intent(trip, "12:30 PM done"), "select")
        self.assertEqual(infer_intent(trip, "want to fly"), "search")
        self.assertEqual(infer_intent({}, "ghy to delhi, first week of december"), "search")
        self.assertEqual(infer_intent({}, "want to fly"), "search")
        self.assertEqual(infer_intent(trip, "I'll pay myself, send the link"), "pay_self")
        self.assertFalse(_direct_results(trip, "can you book the flight?"))
        self.assertFalse(_direct_results(trip, "12:30 PM done"))
        self.assertTrue(_direct_results(trip, "want to fly"))
        self.assertTrue(_should_browse(trip, "can you book the flight?", busy=True, changed=False))
        self.assertTrue(_should_browse(trip, "12:30 PM done", busy=True, changed=False))
        self.assertEqual(redact_for_history("482910"), "[otp]")
        self.assertIn("[card omitted]", redact_for_history("4111 1111 1111 1111"))

    def test_cleartrip_rows_become_a_choice_not_a_disclaimer(self) -> None:
        from app.travel_agent import _options_reply, cleartrip_options

        page = (
            "SpiceJet SG-762 12:30 2h 45m Non-stop 15:15 ₹9,472 Book "
            "SpiceJet SG-199 08:55 3h Non-stop 11:55 ₹9,472 Book "
            "IndiGo 6E-6133 18:20 2h 45m Non-stop 21:05 ₹9,516 Book"
        )
        options = cleartrip_options(page)
        self.assertEqual(options[0]["flight"], "SG-762")
        self.assertEqual(options[0]["depart"], "12:30")
        spoken = _options_reply(
            {"origin": "Guwahati", "destination": "Delhi", "depart": "2026-12-01"},
            options,
        )
        self.assertIn("12:30", spoken)
        self.assertIn("I'd take", spoken)
        self.assertNotIn("Nothing is booked", spoken)
        empty = _options_reply({"origin": "Guwahati", "destination": "Delhi", "depart": "2026-12-01"}, [])
        self.assertIn("didn't list a flight", empty)

    def test_denial_is_replaced_with_the_saved_week(self) -> None:
        trip = {"origin": "Guwahati", "destination": "Delhi", "depart": "2026-12-01", "depart_until": "2026-12-07"}
        spoken = _human_reply(
            trip,
            "I don't have those details saved on my side yet. Please send them once more.",
            browsing=False,
            busy=True,
        )
        self.assertIn("2026-12-01 through 2026-12-07", spoken)
        self.assertNotIn("once more", spoken.lower())


class MetaWebhookVerify(unittest.TestCase):
    def test_verify_challenge_matches_token(self) -> None:
        from app import meta_wa
        from app.config import get_settings

        get_settings.cache_clear()
        os.environ["WHATSAPP_VERIFY_TOKEN"] = "guide_wa_verify_2026"
        get_settings.cache_clear()
        try:
            self.assertEqual(
                meta_wa.verify_challenge("subscribe", "guide_wa_verify_2026", "CHALLENGE99"),
                "CHALLENGE99",
            )
            self.assertIsNone(meta_wa.verify_challenge("subscribe", "wrong", "CHALLENGE99"))
            self.assertIsNone(meta_wa.verify_challenge("unsubscribe", "guide_wa_verify_2026", "CHALLENGE99"))
        finally:
            os.environ.pop("WHATSAPP_VERIFY_TOKEN", None)
            get_settings.cache_clear()


class LocalLabMatchesProdAgent(unittest.TestCase):
    """GUIDE_LOCAL captures WhatsApp; agent path stays the same as prod."""

    def test_send_whatsapp_goes_to_lab_outbox_when_local(self) -> None:
        from app.config import get_settings
        from app.lab import lab_enabled, outbox_since, set_typing, typing_active
        from app.reply import Reply
        from app.twilio_wa import send_typing, send_whatsapp

        get_settings.cache_clear()
        os.environ["GUIDE_LOCAL"] = "1"
        get_settings.cache_clear()
        try:
            self.assertTrue(lab_enabled())
            before = max((item["id"] for item in outbox_since(0)), default=0)
            set_typing(True)
            self.assertTrue(typing_active())
            self.assertTrue(send_typing("lab_sid"))
            ok = send_whatsapp("919999000001", Reply.of("Lab capture check.", "http://127.0.0.1:8000/media/x.png"))
            self.assertTrue(ok)
            items = outbox_since(before)
            self.assertTrue(items)
            self.assertEqual(items[-1]["text"], "Lab capture check.")
            self.assertIn("http://127.0.0.1:8000/media/x.png", items[-1]["media_urls"])
            # A real WhatsApp send clears the typing bubble.
            self.assertFalse(typing_active())
        finally:
            os.environ.pop("GUIDE_LOCAL", None)
            get_settings.cache_clear()

    def test_lab_ui_paints_outbox_once_not_reply_and_poll(self) -> None:
        """Regression: painting data.reply AND /lab/outbox duplicated every bot line."""
        from app.lab import _LAB_HTML

        self.assertIn("seen.has", _LAB_HTML)
        self.assertIn("data.typing", _LAB_HTML)
        self.assertIn("Guide is typing", _LAB_HTML)
        self.assertNotIn("data.reply.text", _LAB_HTML)
        self.assertIn("send_whatsapp(phone, reply)", Path("app/lab.py").read_text(encoding="utf-8"))
        # Stale after=N after server restart must resync (otherwise replies never show).
        self.assertIn("data.boot", _LAB_HTML)
        self.assertIn("max_id", _LAB_HTML)
        self.assertIn("serverBoot !== boot", _LAB_HTML)

    def test_lab_routes_and_runner_exist(self) -> None:
        from app.lab import router as lab_router

        paths = {getattr(route, "path", "") for route in lab_router.routes}
        self.assertIn("/lab", paths)
        self.assertIn("/lab/message", paths)
        self.assertIn("/lab/outbox", paths)
        self.assertIn("/lab/health", paths)
        main_src = Path("app/main.py").read_text(encoding="utf-8")
        self.assertIn("lab_router", main_src)
        self.assertTrue(Path("scripts/run_local.py").is_file())
        self.assertTrue(Path("LOCAL.md").is_file())
        self.assertIn("GUIDE_LOCAL", Path(".env.example").read_text(encoding="utf-8"))


class MetaReactionOutbound(unittest.TestCase):
    def test_strip_leading_emoji_keeps_plain_text(self) -> None:
        from app.reply import for_send, strip_leading_emoji
        from app.reply import Reply

        self.assertEqual(strip_leading_emoji("Where are you flying?"), "Where are you flying?")
        self.assertEqual(strip_leading_emoji("✈️ Opening live pages."), "Opening live pages.")
        self.assertEqual(strip_leading_emoji("👍 Got it."), "Got it.")

        reacted = for_send(Reply.of("✈️ Opening live pages.", emoji="✈️"), reacted=True)
        self.assertEqual(reacted.text, "Opening live pages.")
        self.assertEqual(reacted.emoji, "✈️")

        fallback = for_send(Reply.of("Opening live pages.", emoji="✈️"), reacted=False)
        self.assertTrue(fallback.text.startswith("✈️"))
        self.assertIn("Opening live pages.", fallback.text)

    def test_meta_reaction_posts_cloud_api_payload(self) -> None:
        from unittest.mock import patch

        from app import meta_wa
        from app.config import get_settings
        from app.reply import Reply

        get_settings.cache_clear()
        os.environ["WHATSAPP_TOKEN"] = "test-token"
        os.environ["WHATSAPP_PHONE_NUMBER_ID"] = "1419600664569017"
        get_settings.cache_clear()
        posted: list[dict] = []

        class FakeResp:
            status_code = 200
            text = "{}"

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def post(self, url, headers=None, json=None):
                posted.append({"url": url, "json": json, "headers": headers})
                return FakeResp()

        try:
            with patch("app.meta_wa.httpx.Client", FakeClient):
                ok = meta_wa.send_reaction("33759341005", "wamid.TEST123", "👍")
                send_ok = meta_wa.send_whatsapp("33759341005", Reply.of("Opening live pages.", emoji="👍"))
        finally:
            os.environ.pop("WHATSAPP_TOKEN", None)
            os.environ.pop("WHATSAPP_PHONE_NUMBER_ID", None)
            get_settings.cache_clear()

        self.assertTrue(ok)
        self.assertTrue(send_ok)
        reaction = next(p for p in posted if (p["json"] or {}).get("type") == "reaction")
        self.assertEqual(reaction["json"]["reaction"]["emoji"], "👍")
        self.assertEqual(reaction["json"]["reaction"]["message_id"], "wamid.TEST123")
        text_msg = next(p for p in posted if (p["json"] or {}).get("type") == "text")
        self.assertEqual(text_msg["json"]["text"]["body"], "Opening live pages.")
        self.assertNotIn("👍", text_msg["json"]["text"]["body"])


class TypingAndSpeed(unittest.TestCase):
    def test_typing_form_is_the_whatsapp_indicator(self) -> None:
        url, payload = typing_form("SMabc123")
        self.assertTrue(url.endswith("/Indicators/Typing.json"))
        self.assertEqual(payload["channel"], "whatsapp")
        self.assertEqual(payload["messageId"], "SMabc123")
        self.assertLessEqual(TYPING_REFRESH_SECONDS, 25)

    def test_typing_is_on_while_the_reply_is_written(self) -> None:
        import app.twilio_wa as wa

        events: list[str] = []

        def fake_send(message_sid: str) -> bool:
            events.append(f"typing:{message_sid}")
            return True

        original = wa.send_typing
        wa.send_typing = fake_send
        try:
            remember_inbound("9199990000", "SMabc123")
            with composing("9199990000"):
                events.append("writing")
            events.append("sent")
        finally:
            wa.send_typing = original
        self.assertEqual(events, ["typing:SMabc123", "writing", "sent"])

    def test_known_trip_has_a_results_url_without_a_model(self) -> None:
        from app.travel_agent import _direct_results

        trip = {"origin": "Guwahati", "destination": "Delhi", "depart": "2026-12-01", "depart_until": "2026-12-07"}
        self.assertTrue(_direct_results(trip, "yes"))
        self.assertFalse(_direct_results(trip, "log in and fill the passenger form"))
        week = flight_results_url("Guwahati", "Delhi", "2026-12-01", "2026-12-07")
        self.assertIn("google.com/travel/flights", week)
        self.assertIn("2026-12-01", week)
        self.assertIn("2026-12-07", week)
        day = flight_results_url("ghy", "delhi", "2026-12-04")
        self.assertIn("google.com/travel/flights", day)
        self.assertIn("curr=INR", day)
        self.assertIn("q=", day)
        self.assertIn("Guwahati", day)
        self.assertIn("Delhi", day)
        self.assertIn("December", day)
        self.assertIn("2026", day)
        self.assertNotIn("tfs=", day)
        clear = cleartrip_results_url("Guwahati", "Delhi", "2026-12-01")
        self.assertIn("cleartrip.com", clear)
        self.assertIn("GAU", clear)
        self.assertIn("DEL", clear)


class LivePageGuards(unittest.TestCase):
    def test_blank_page_is_not_sent(self) -> None:
        self.assertFalse(should_capture(""))
        self.assertFalse(should_capture("Loading"))
        self.assertTrue(should_capture("Google Flights\nWhere from?"))
        self.assertTrue(should_capture("IndiGo 6E 123 Rs 11,620"))
        self.assertTrue(should_capture("Please verify you are not a robot"))

    def test_payment_and_card_are_refused(self) -> None:
        self.assertTrue(payment_control("Pay now"))
        self.assertFalse(payment_control("Continue"))
        self.assertTrue(looks_like_card("4111 1111 1111 1111"))
        self.assertFalse(looks_like_card("8638586171"))
        self.assertIn("[card omitted]", redact_card("card 4111 1111 1111 1111"))

    def test_recommendation_uses_live_evidence_only(self) -> None:
        import app.travel_agent as travel

        seen: dict[str, str] = {}

        def fake_reason(prompt: str, timeout: float = 40.0) -> str:
            seen["prompt"] = prompt
            return "✈️ Cleartrip is the cheapest at Rs 11620. Nothing is paid."

        original = travel.llm.reason
        travel.llm.reason = fake_reason
        try:
            text = _recommendation(
                True,
                [{"role": "tool", "content": "URL: https://www.cleartrip.com\nPrices visible: [11620]"}],
                "fallback",
            )
        finally:
            travel.llm.reason = original
        self.assertIn("11620", text)
        self.assertIn("11620", seen["prompt"])
        self.assertEqual(_recommendation(False, [], "no fares"), "no fares")


class MemoryStore(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("GUIDE_DB_PATH")
        os.environ["GUIDE_DB_PATH"] = str(Path(self.tmp.name) / "guide.db")
        get_settings.cache_clear()
        import app.db as db

        db._resolved_db_path = None
        db.init_db()
        self.db = db

    def tearDown(self) -> None:
        import app.db as db

        db._resolved_db_path = None
        get_settings.cache_clear()
        if self._old is None:
            os.environ.pop("GUIDE_DB_PATH", None)
        else:
            os.environ["GUIDE_DB_PATH"] = self._old
        self.tmp.cleanup()

    def test_note_survives_for_the_same_phone(self) -> None:
        from app.travel_agent import _load, _remember

        phone = "9100001111"
        self.db.upsert_user(phone)
        _remember(
            phone,
            {
                "origin": "Guwahati",
                "destination": "Delhi",
                "depart": "2026-11-30",
                "note": "Google showed no fares.",
            },
        )
        lines = _memory_lines(_load(phone))
        text = " ".join(lines)
        self.assertIn("2026-11-30", text)
        self.assertIn("Google showed no fares.", text)

    def test_payment_vault_is_per_phone_and_skips_cvv(self) -> None:
        from app import vault

        os.environ["VAULT_SECRET_KEY"] = "test-vault-key-16"
        get_settings.cache_clear()
        try:
            phone = "9199990000"
            other = "9188880000"
            self.db.upsert_user(phone)
            self.db.upsert_user(other)
            note = vault.save_payment(phone, upi="traveller@ybl", card="4111111111111111", expiry="12/28")
            self.assertIn("••••1111", note)
            self.assertNotIn("4111111111111111", note)
            saved = vault.unlock_payment(phone)
            self.assertIsNotNone(saved)
            assert saved is not None
            self.assertEqual(saved["upi"], "traveller@ybl")
            self.assertEqual(saved["card"], "4111111111111111")
            self.assertNotIn("cvv", saved)
            self.assertIsNone(vault.unlock_payment(other))
            self.assertEqual(vault.payment_summary(phone), "UPI, card ••••1111, expiry 12/28")
        finally:
            os.environ.pop("VAULT_SECRET_KEY", None)
            get_settings.cache_clear()


class SellerChoiceAndProxy(unittest.TestCase):
    def test_named_seller_and_order(self) -> None:
        self.assertEqual(named_seller("try easemytrip"), "easemytrip")
        self.assertEqual(named_seller("easymytirp"), "easemytrip")
        self.assertEqual(named_seller("ease my trip"), "easemytrip")
        self.assertEqual(named_seller("open cleartrip"), "cleartrip")
        self.assertEqual(named_seller("mmt please"), "makemytrip")
        self.assertEqual(named_seller("check google flights"), "google")
        order = seller_order("easemytrip")
        self.assertEqual(order[0]["id"], "easemytrip")
        self.assertIn("cleartrip", [item["id"] for item in order])
        self.assertEqual(order[-1]["id"], "makemytrip")
        self.assertEqual(search_seller("")["id"], "google")
        self.assertEqual(search_seller("ixigo")["id"], "ixigo")
        self.assertEqual(search_seller("google")["id"], "google")
        self.assertEqual(next_bookable_seller("google"), "easemytrip")
        self.assertEqual(next_bookable_seller("cleartrip"), "easemytrip")
        self.assertEqual(next_bookable_seller("easemytrip"), "ixigo")
        self.assertEqual(next_bookable_seller("ixigo"), "cleartrip")

    def test_another_service_stays_a_booking(self) -> None:
        trip = {"origin": "Guwahati", "destination": "Delhi", "depart": "2026-12-04", "seller": "google", "stage": "results"}
        self.assertTrue(_wants_other_seller("try another service"))
        self.assertEqual(infer_intent(trip, "try another service"), "book")
        self.assertFalse(_browse_is_search("book", trip, "try another service"))
        self.assertTrue(_browse_is_search("search", trip, "4th dec"))
        self.assertTrue(_browse_is_search("search", {}, "4th Dec"))
        self.assertEqual(_coerce_kind("4th Dec", "book"), "search")
        self.assertEqual(_coerce_kind("Ixigo", "book"), "search")
        self.assertEqual(_coerce_kind("1:15", "select"), "select")
        with_fares = dict(trip)
        with_fares["options"] = [{"depart": "1:15", "price": "9,365"}]
        with_fares["selected_hint"] = "1:15"
        with_fares["stage"] = "results"
        self.assertEqual(_coerce_kind("EaseMyTrip", "chat", with_fares), "book")
        self.assertEqual(_coerce_kind("Ixigo", "book", with_fares), "book")
        # A leftover Ixigo preference must not steal a bare "1:15" after a list that cannot ticket.
        with_fares["preferred_seller"] = "ixigo"
        with_fares["seller"] = "google"
        with_fares["last_vision"] = {"kind": "results", "seller": "google", "bookable": False}
        from app.search import default_bookable_seller

        self.assertEqual(_booking_seller_id(with_fares, "1:15"), default_bookable_seller())
        self.assertEqual(_booking_seller_id(with_fares, "Ixigo"), "ixigo")
        stuck = dict(trip)
        stuck["preferred_seller"] = "cleartrip"
        self.assertIn("Google Flights", _search_ack(stuck, "4th Dec"))
        self.assertIn("EaseMyTrip", _search_ack(stuck, "EaseMyTrip"))
        checkout = "https://www.cleartrip.com/flights/itinerary?x=1"
        self.assertEqual(_bookable_live_url(checkout), checkout)
        self.assertEqual(_alt_clock("1:15"), "13:15")
        self.assertEqual(_alt_clock("13:15"), "1:15")
        self.assertIn("01:15", _clock_needles("1:15"))
        self.assertIn("13:15", _clock_needles("1:15"))
        self.assertTrue(time_in_card("Air India Express 1:15 Book ₹9,365", "1:15"))
        self.assertTrue(time_in_card("dep 13:15 Book", "1:15"))
        self.assertFalse(time_in_card("SpiceJet 11:15 Book ₹8,190", "1:15"))
        self.assertFalse(time_in_card("IndiGo 9:10 Book ₹8,063", "1:15"))
        self.assertEqual(_bookable_live_url("https://www.google.com/travel/flights?q=ghy"), "")
        self.assertTrue(_bookable_live_url("https://www.cleartrip.com/flights/results?from=GAU").startswith("https://www.cleartrip.com"))
        self.assertTrue(known_flight_host("www.cleartrip.com"))
        self.assertFalse(known_flight_host("localhost"))
        self.assertTrue(str(url_allowed("https://www.cleartrip.com/flights/results?from=GAU&to=DEL")).startswith("https://www.cleartrip.com"))
        self.assertIsNone(url_allowed("http://127.0.0.1/admin"))

    def test_a_running_search_is_not_restarted(self) -> None:
        trip = {"origin": "Guwahati", "destination": "Delhi", "depart": "2026-12-04", "options": [{"depart": "12:30", "price": "9,472", "airline": "SpiceJet"}]}
        self.assertTrue(_hold_live(trip, "?", busy=True, changed=False))
        self.assertFalse(_hold_live(trip, "?", busy=False, changed=False))
        self.assertFalse(_hold_live(trip, "5 december", busy=True, changed=True))
        ack = _search_ack(trip)
        self.assertIn("Google Flights", ack)
        self.assertIn("screenshot", ack)
        self.assertEqual(infer_intent(trip, "go ahead with 4th december"), "book")
        replay = _cached_fares_reply(trip, "?", "chat")
        self.assertIn("SpiceJet", replay)
        self.assertIn("Cleartrip", replay)

    def test_seller_urls_for_ghy_delhi(self) -> None:
        clear = seller_results_url("cleartrip", "Guwahati", "Delhi", "2026-12-01")
        emt = easemytrip_results_url("Guwahati", "Delhi", "2026-12-01")
        ix = ixigo_results_url("Guwahati", "Delhi", "2026-12-01")
        self.assertIn("cleartrip.com", clear)
        self.assertIn("GAU", clear)
        self.assertIn("easemytrip.com", emt)
        self.assertIn("GAU", emt)
        self.assertIn("ixigo.com", ix)
        self.assertIn("GAU", ix)

    def test_pick_cheapest_and_memory_keeps_seller(self) -> None:
        options = fare_options(
            "SpiceJet SG-762 12:30 2h 45m Non-stop 15:15 ₹9,472 Book "
            "IndiGo 6E-6133 18:20 2h 45m Non-stop 21:05 ₹9,516 Book"
        )
        best = _pick_best(options, "cheapest")
        self.assertEqual(best["depart"], "12:30")
        spoken = _options_reply(
            {"origin": "Guwahati", "destination": "Delhi", "depart": "2026-12-01", "preference": "cheapest"},
            options,
            seller="easemytrip",
        )
        self.assertIn("EaseMyTrip", spoken)
        self.assertIn("I'd take", spoken)
        lines = _memory_lines({"origin": "Guwahati", "destination": "Delhi", "depart": "2026-12-01", "seller": "cleartrip", "stage": "results"})
        self.assertTrue(any("Cleartrip" in line for line in lines))

    def test_french_mobile_is_not_indian(self) -> None:
        self.assertFalse(_indian_mobile("+33 7 59 34 10 05"))
        self.assertTrue(_indian_mobile("+91 98765 43210"))
        self.assertTrue(_indian_mobile("9876543210"))

    def test_contact_step_asks_for_indian_mobile(self) -> None:
        class FakeSession:
            page = type("P", (), {"url": "https://www.cleartrip.com/flights/itinerary"})()

        spoken = _booking_page_reply(
            {
                "origin": "Guwahati",
                "destination": "Delhi",
                "depart": "2026-12-01",
                "seller": "cleartrip",
                "phone": "+33 7 59 34 10 05",
            },
            "12:30",
            "Add Contact details Email Mobile number Continue",
            FakeSession(),  # type: ignore[arg-type]
        )
        self.assertIn("+91", spoken)
        self.assertIn("Nothing paid", spoken)
        self.assertNotIn("skip for extras", spoken.lower())

    def test_saved_contact_on_unknown_form_presses_continue(self) -> None:
        from app.step_read import form_sheet, ground_unknown_plan

        page = "Add Contact details +91 Email address Mobile number Continue Review traveller details"
        self.assertTrue(form_sheet(page))
        plan = ground_unknown_plan(
            page,
            "",
            {"email": "sudipta@example.com", "phone": "8638586171"},
            intent="give_facts",
        )
        self.assertEqual(plan["action"], "fill")
        self.assertEqual(plan["tap"], "Continue")
        self.assertEqual({item["key"] for item in plan["fills"]}, {"email", "phone"})

        class FakeSession:
            page = type("P", (), {"url": "https://www.cleartrip.com/flights/itinerary"})()

        spoken = _booking_page_reply(
            {
                "origin": "Guwahati",
                "destination": "Delhi",
                "depart": "2026-12-04",
                "seller": "cleartrip",
                "email": "sudipta@example.com",
                "phone": "8638586171",
                "selected_hint": "10:20",
            },
            "10:20",
            page,
            FakeSession(),  # type: ignore[arg-type]
        )
        self.assertIn("Continue", spoken)
        self.assertNotIn("stage other", spoken.lower())
        self.assertNotIn("skip for extras", spoken.lower())

    def test_contact_slots_match_labels_types_and_prefix(self) -> None:
        from app.page_agent import _passenger_ready, box_slot, contact_report

        self.assertEqual(box_slot({"label": "Email address", "type": "email", "value": ""}), "email")
        self.assertEqual(box_slot({"label": "Mobile number", "placeholder": "", "value": ""}), "phone")
        self.assertEqual(box_slot({"label": "", "value": "+91"}), "prefix")
        self.assertEqual(box_slot({"placeholder": "Email", "type": "text", "value": ""}), "email")
        self.assertTrue(_passenger_ready(["first:none", "email:ok", "phone:ok", "title:none"]))
        self.assertFalse(_passenger_ready(["first:none", "email:ok", "phone:miss", "title:none"]))
        # Traveller name sheet often has no email/mobile boxes (already saved on contact).
        self.assertTrue(
            _passenger_ready(
                ["title:none", "gender:ok", "first:ok", "last:ok", "email:skip", "phone:skip", "dob:skip"]
            )
        )
        self.assertTrue(
            _passenger_ready(
                ["title:none", "gender:ok", "first:ok", "last:ok", "email:none", "phone:none", "dob:none"]
            )
        )
        from app.page_agent import classify_page as _classify
        from app.step_read import addons_sheet as _addons

        meals = (
            "Review your itinerary Add Meals Fruit Cake Skip add-ons "
            "Add Extra Luggage Continue Review contact details"
        )
        # Review heading wins — flex meal Skip on Review is not Choose add-ons.
        self.assertEqual(_classify(meals, "https://www.cleartrip.com/flights/itinerary/x"), "review")
        self.assertTrue(_addons(meals))
        from app.step_contract import left_addons as _left

        self.assertFalse(_left(meals, "review"))
        self.assertFalse(
            _left(
                "Review your itinerary Add Meals Skip add-ons Free Date Change Continue",
                "review",
            )
        )
        self.assertTrue(_left("Review your itinerary Fare summary Total amount Continue", "review"))
        stuck = contact_report("email:miss | phone:ok", "Add Contact details")
        self.assertIn("email did not stick", stuck)
        moved = contact_report("email:ok | phone:ok | tap:1", "Review traveller details")
        self.assertIn("Review traveller details", moved)
        self.assertNotIn("stage other", moved.lower())

    def test_slot_fill_on_cleartrip_and_ixigo_markup(self) -> None:
        from playwright.sync_api import sync_playwright

        from app.browser_hands import chromium_ready
        from app.page_agent import _apply_unknown_plan, reveal_form_step

        if not chromium_ready():
            self.skipTest("chromium missing")
        cleartrip = """<!doctype html><html><body>
          <div style="height:1600px">Base Fare Taxes</div>
          <h3>Add Contact details</h3>
          <div>+91<input id="cc" value="+91"></div>
          <div><input id="mobile"></div>
          <div>Email address<input id="email" type="email" placeholder="Email address"></div>
          <button onclick="this.remove();document.getElementById('next').hidden=false">Continue</button>
          <p id="next" hidden>Review traveller details</p>
        </body></html>"""
        ixigo = """<!doctype html><html><body>
          <h3>Traveller details</h3>
          <div>Email<input id="email" type="email" placeholder="Email"></div>
          <div>Mobile number<input id="mobile" type="tel"></div>
          <button onclick="this.remove();document.getElementById('next').hidden=false">Continue</button>
          <p id="next" hidden>Review traveller details</p>
        </body></html>"""
        plan = {
            "action": "fill",
            "tap": "Continue",
            "fills": [
                {"key": "email", "value": "sudipta@example.com"},
                {"key": "phone", "value": "8638586171"},
            ],
        }
        # Cleartrip-like React mobile: simple .value= is wiped unless input events fire.
        react_cleartrip = """<!doctype html><html><body>
          <h3>Add Contact details</h3>
          <div>+91<input id="cc" value="+91" readonly></div>
          <div><input id="mobile"></div>
          <div>Email address<input id="email" type="email" placeholder="Email address"></div>
          <button id="go">Continue</button>
          <p id="next" hidden>Review traveller details</p>
          <script>
            const mobile = document.getElementById('mobile');
            let phone = '';
            Object.defineProperty(mobile, 'value', {
              get() { return phone; },
              set(v) { /* ignore raw assigns — only input events count */ }
            });
            mobile.addEventListener('input', (e) => {
              phone = (e.target && e.data != null) ? (phone + (e.data || '')) : phone;
              if (e.inputType === 'insertFromPaste' && e.data) phone = e.data;
            });
            document.getElementById('go').onclick = () => {
              if (phone.replace(/\\D/g,'').length >= 10 && document.getElementById('email').value.includes('@')) {
                document.getElementById('go').remove();
                document.getElementById('next').hidden = false;
              }
            };
          </script>
        </body></html>"""
        # +91 only listed until focus/Tab reveals the national box (Kernel phone:none case).
        hidden_mobile = """<!doctype html><html><body>
          <h3>Add Contact details</h3>
          <div id="row"><input id="cc" value="+91"><input id="mobile" style="width:1px;height:1px;opacity:0.01"></div>
          <div>Email address<input id="email" type="email" placeholder="Email address"></div>
          <button onclick="this.remove();document.getElementById('next').hidden=false">Continue</button>
          <p id="next" hidden>Review traveller details</p>
          <script>
            const cc = document.getElementById('cc');
            const mobile = document.getElementById('mobile');
            cc.addEventListener('click', () => {
              mobile.style.width = '160px';
              mobile.style.height = '40px';
              mobile.style.opacity = '1';
              mobile.focus();
            });
          </script>
        </body></html>"""
        # Prod bug: phone digits landed in Email; mobile stayed empty with validation text.
        swapped = """<!doctype html><html><body>
          <h3>Add Contact details</h3>
          <div style="display:flex">
            <input id="cc" value="+91" style="width:48px">
            <input id="mobile" style="width:160px;margin-left:8px">
          </div>
          <p>Please enter Mobile Number</p>
          <div>Email address<input id="email" type="email" placeholder="Email address" value="8638586171"></div>
          <p>Please enter a valid email address</p>
          <button onclick="
            const m=document.getElementById('mobile').value.replace(/\\D/g,'');
            const e=document.getElementById('email').value;
            if(m.length===10 && e.includes('@')){this.remove();document.getElementById('next').hidden=false;}
          ">Continue</button>
          <p id="next" hidden>Review traveller details</p>
        </body></html>"""
        # Live Kernel DOM: +91 is plain text; mobile is empty text left of email.
        cleartrip_live = """<!doctype html><html><body>
          <h3>Add Contact details</h3>
          <div style="position:relative;height:80px">
            <span style="position:absolute;left:40px;top:20px">+91</span>
            <input id="mobile" style="position:absolute;left:90px;top:12px;width:240px;height:40px">
            <input id="email" type="email" style="position:absolute;left:360px;top:12px;width:280px;height:40px"
              placeholder="Email address">
          </div>
          <button onclick="
            const m=document.getElementById('mobile').value.replace(/\\D/g,'');
            const e=document.getElementById('email').value;
            if(m.length===10 && e.includes('@')){this.remove();document.getElementById('next').hidden=false;}
          ">Continue</button>
          <p id="next" hidden>Review traveller details</p>
        </body></html>"""
        contact_login_trap = (
            "Add Contact details Please enter Mobile Number +91 Email address Continue "
            "Review traveller details"
        )
        self.assertEqual(
            __import__("app.page_agent", fromlist=["classify_page"]).classify_page(
                contact_login_trap, "https://www.cleartrip.com/flights/itinerary/x"
            ),
            "other",
        )
        from app.step_read import form_sheet as _form_sheet

        traveller = (
            "Add traveller details Adult 1 First & middle name (Given name) "
            "Last name (Surname) Male Female sudipta@example.com 8638586171 Continue"
        )
        self.assertFalse(_form_sheet(traveller))
        self.assertEqual(
            classify_page(traveller, "https://www.cleartrip.com/flights/itinerary/x"),
            "passenger",
        )
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            for html in (cleartrip, ixigo, react_cleartrip, hidden_mobile, swapped, cleartrip_live):
                page = browser.new_page()
                page.set_content(html)
                self.assertTrue(reveal_form_step(page))
                notes = _apply_unknown_plan(page, plan)
                body = page.inner_text("body")
                email_val = page.input_value("#email")
                mobile_val = page.input_value("#mobile")
                page.close()
                self.assertIn("email:ok", notes, notes)
                self.assertIn("phone:ok", notes, notes)
                self.assertIn("tap:1", notes, notes)
                self.assertIn("Review traveller details", body)
                self.assertIn("@", email_val)
                self.assertIn("8638586171", re.sub(r"\D", "", mobile_val))
                self.assertNotIn("contact_unready", " ".join(notes))
            browser.close()

    def test_continue_after_contact_reaches_payment(self) -> None:
        """Contact → traveller name → Skip add-ons → payment handoff (never Pay)."""
        from playwright.sync_api import sync_playwright

        from app.browser_hands import chromium_ready
        from app.page_agent import payment_sheet_open, run_checkout_agent

        if not chromium_ready():
            self.skipTest("chromium missing")
        html = """<!doctype html><html><body>
          <div id="sheet">
            <h3>Add Contact details</h3>
            <div>+91 <input id="mobile"></div>
            <div>Email address <input id="email" type="email" placeholder="Email address"></div>
            <button id="go">Continue</button>
          </div>
          <script>
            let step = 0;
            document.getElementById('go').onclick = () => {
              const m = document.getElementById('mobile').value.replace(/\\D/g,'');
              const e = document.getElementById('email').value;
              if (step === 0 && m.length >= 10 && e.includes('@')) {
                step = 1;
                document.getElementById('sheet').innerHTML =
                  '<h3>Add traveller details</h3>' +
                  '<label>First & middle name (Given name)</label>' +
                  '<input placeholder="First & middle name (Given name)">' +
                  '<label>Last name (Surname)</label>' +
                  '<input placeholder="Last name (Surname)">' +
                  '<button>Male</button><button>Female</button>' +
                  '<button id="go2">Continue</button>';
                document.getElementById('go2').onclick = () => {
                  const inputs = [...document.querySelectorAll('input')];
                  if (inputs[0].value && inputs[1].value) {
                    step = 2;
                    document.getElementById('sheet').innerHTML =
                      '<h3>Add-ons</h3><p>Meals and seats</p>' +
                      '<button id="skip">Skip add-ons</button>';
                    document.getElementById('skip').onclick = () => {
                      document.getElementById('sheet').innerHTML =
                        '<h1>Payment options</h1><p>Pay to complete your booking</p>' +
                        '<p>Enter card number</p><button>Pay now</button>';
                    };
                  }
                };
              }
            };
          </script>
        </body></html>"""
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content(html)
            out = run_checkout_agent(
                page,
                hint="10:45",
                seller="cleartrip",
                origin="Guwahati",
                destination="Delhi",
                depart="2026-12-04",
                allow_pay=False,
                pause_on_auth=True,
                max_steps=12,
                profile={
                    "name": "Sudipta Saikia",
                    "email": "sudipta@example.com",
                    "phone": "8638586171",
                    "gender": "male",
                },
            )
            body = page.inner_text("body")
            browser.close()
        self.assertEqual(out.stage, "pay", out.log)
        self.assertTrue(payment_sheet_open(body, out.stage))
        self.assertNotRegex(body, r"(?i)pay now.*clicked|charged")

    def test_cleartrip_homepage_is_not_the_traveller_form(self) -> None:
        from app.kernel_otp import cleartrip_looks_logged_in

        home = (
            "Book Domestic & International Flight Tickets One way Round trip "
            "Where from Where to Search flights My Account Contact We use cookies"
        )
        url = "https://www.cleartrip.com/"
        self.assertEqual(classify_page(home, url), "home")
        self.assertFalse(cleartrip_looks_logged_in(home, url))
        self.assertTrue(
            cleartrip_looks_logged_in("Log out My Account", "https://www.cleartrip.com/flights/itinerary?x=1")
        )

        class FakeSession:
            page = type("P", (), {"url": url})()

        spoken = _booking_page_reply(
            {
                "origin": "Guwahati",
                "destination": "Delhi",
                "depart": "2026-12-04",
                "seller": "cleartrip",
                "selected_hint": "10:45",
            },
            "10:45",
            home,
            FakeSession(),  # type: ignore[arg-type]
        )
        self.assertIn("Stuck:", spoken)
        self.assertIn("search page", spoken.lower())
        self.assertIn("recommend", spoken.lower())
        self.assertNotIn("I'm on the 10:45 step", spoken)
        self.assertNotIn("Indian mobile", spoken)
        booking = {
            "stage": "booking",
            "seller": "cleartrip",
            "origin": "Guwahati",
            "destination": "Delhi",
            "depart": "2026-12-04",
            "selected_hint": "10:45",
        }
        self.assertEqual(infer_intent(booking, "Go on"), "book")
        self.assertEqual(infer_intent({**booking, "stage": "review"}, "skip"), "book")

    def test_payment_step_refuses_upi_self_tap(self) -> None:
        class FakeSession:
            page = type("P", (), {"url": "https://www.cleartrip.com/pay"})()

        spoken = _booking_page_reply(
            {"origin": "Guwahati", "destination": "Delhi", "depart": "2026-12-01", "seller": "cleartrip"},
            "12:30",
            "Pay to complete your booking UPI Pay now ₹12,085",
            FakeSession(),  # type: ignore[arg-type]
        )
        self.assertIn("Nothing charged yet", spoken)
        self.assertIn("UPI", spoken)
        self.assertIn("pay yourself", spoken.lower())

    def test_preferred_seller_is_absorbed(self) -> None:
        state = absorb_details({"origin": "Guwahati", "destination": "Delhi"}, "use ixigo please")
        self.assertEqual(state.get("preferred_seller"), "ixigo")

    def test_proxy_settings_ready(self) -> None:
        get_settings.cache_clear()
        os.environ["PROXY_HOST"] = "gw.dataimpulse.com"
        os.environ["PROXY_PORT"] = "10001"
        os.environ["PROXY_USERNAME"] = "login__cr.in;sessttl.30"
        os.environ["PROXY_PASSWORD"] = "secret"
        get_settings.cache_clear()
        try:
            settings = get_settings()
            self.assertTrue(settings.proxy_ready)
            proxy = settings.playwright_proxy()
            assert proxy is not None
            self.assertEqual(proxy["server"], "http://gw.dataimpulse.com:10001")
            self.assertIn("cr.in", proxy["username"])
        finally:
            for key in ("PROXY_HOST", "PROXY_PORT", "PROXY_USERNAME", "PROXY_PASSWORD"):
                os.environ.pop(key, None)
            get_settings.cache_clear()

    def test_browser_close_stops_playwright_on_same_thread(self) -> None:
        """Close must finish on this thread so the next residential booking can start."""
        import threading

        class _Pw:
            def __init__(self) -> None:
                self.stopped_on: threading.Thread | None = None

            def stop(self) -> None:
                self.stopped_on = threading.current_thread()

        class _Handle:
            def close(self) -> None:
                return None

        session = BrowserSession("close-test")
        pw = _Pw()
        session._pw = pw
        session._browser = _Handle()
        session._context = _Handle()
        session.page = object()
        session.close()
        self.assertIsNone(session._pw)
        self.assertIsNone(session.page)
        self.assertIs(pw.stopped_on, threading.current_thread())

    def test_booking_proxy_username_keeps_india_sticky(self) -> None:
        """Cleartrip checkout needs LOGIN__cr.in;sessid…;sessttl.30 on a sticky port, not rotating 823."""
        get_settings.cache_clear()
        os.environ["PROXY_HOST"] = "gw.dataimpulse.com"
        os.environ["PROXY_PORT"] = "10000"
        os.environ["PROXY_USERNAME"] = "aebd377cf63720321420__cr.in;sessid.guide1;sessttl.30"
        os.environ["PROXY_PASSWORD"] = "secret"
        get_settings.cache_clear()
        try:
            proxy = get_settings().playwright_proxy()
            assert proxy is not None
            self.assertEqual(proxy["server"], "http://gw.dataimpulse.com:10000")
            self.assertIn("__cr.in;sessid.guide1;sessttl.30", proxy["username"])
            self.assertNotIn(":823", proxy["server"])
        finally:
            for key in ("PROXY_HOST", "PROXY_PORT", "PROXY_USERNAME", "PROXY_PASSWORD"):
                os.environ.pop(key, None)
            get_settings.cache_clear()

    def test_busy_window_covers_slow_cleartrip_checkout(self) -> None:
        from app.travel_agent import _busy

        now = __import__("time").time()
        self.assertTrue(_busy({"busy_at": f"{now - 200:.3f}"}))
        self.assertFalse(_busy({"busy_at": f"{now - 400:.3f}"}))

    def test_booking_worker_settings(self) -> None:
        get_settings.cache_clear()
        os.environ["BOOKING_WORKER_URL"] = "http://127.0.0.1:8000"
        os.environ["BOOKING_WORKER_TOKEN"] = "secret-token"
        get_settings.cache_clear()
        try:
            settings = get_settings()
            self.assertTrue(settings.booking_worker_ready)
            from app.remote_booking import worker_configured

            self.assertTrue(worker_configured())
        finally:
            os.environ.pop("BOOKING_WORKER_URL", None)
            os.environ.pop("BOOKING_WORKER_TOKEN", None)
            get_settings.cache_clear()

    def test_cookie_banner_is_not_a_passenger_form(self) -> None:
        banner = "We respect your privacy. We use cookies. Accept all cookies."
        self.assertEqual(classify_page(banner, "https://www.goindigo.in/"), "cookie")

    def test_pay_page_is_recognized(self) -> None:
        self.assertEqual(
            classify_page("Pay to complete your booking. Card number. UPI.", "https://www.cleartrip.com/pay"),
            "pay",
        )

    def test_itinerary_failure_is_an_error(self) -> None:
        self.assertEqual(
            classify_page("Server error", "https://www.cleartrip.com/flights/results/itinerary/failure"),
            "error",
        )

    def test_nav_book_link_is_not_a_fare_list(self) -> None:
        text = "Book | Trips | Check-in | Up to 8 IndiGo BluChips per ₹100 spent"
        self.assertNotEqual(classify_page(text, "https://www.goindigo.in/"), "results")
        self.assertNotEqual(classify_page(text, "https://www.goindigo.in/"), "pay")

    def test_marketing_homepage_is_not_pay(self) -> None:
        text = "EMTMate Corporate travel program Login or Signup"
        self.assertNotEqual(classify_page(text, "https://www.easemytrip.com/"), "pay")

    def test_priced_select_buttons_are_results(self) -> None:
        self.assertEqual(classify_page("SpiceJet 09:10 Select ₹8,190", "https://flight.easemytrip.com/"), "results")

    def test_results_beat_addon_marketing_copy(self) -> None:
        hay = (
            "Add meal Extra baggage Non Stop IndiGo 18:25 21:05 Book ₹10,283 "
            "IndiGo 09:15 12:00 Book ₹10,283"
        )
        self.assertEqual(classify_page(hay, "https://www.cleartrip.com/flights/results"), "results")
        self.assertNotEqual(classify_page(hay, "https://www.cleartrip.com/flights/results"), "addons")

    def test_logged_in_traveller_form_is_not_assured_popup(self) -> None:
        """Adult 1 + Contact + Continue is the traveller form, even with Refund on Cancellation copy."""
        from app.step_read import classify_sheet
        from app.travel_agent import _addon_upsell, _booking_page_reply

        page = (
            "Review & Traveller Details Hey Ixigoer Fare Summary Total Amount ₹9,813 "
            "Refund on Cancellation Add Free Cancellation "
            "Mr Sudipta Saikia Adult 1 Title First & Middle Name Last Name Nationality India "
            "Contact Details +91 8638586171 Email Continue"
        )
        self.assertEqual(classify_sheet(page), "traveller")
        self.assertEqual(_addon_upsell(page), "")

        class _Session:
            page = None

        spoken = _booking_page_reply(
            {
                "seller": "ixigo",
                "selected_hint": "09:10",
                "origin": "Guwahati",
                "destination": "Delhi",
                "depart": "2026-12-04",
                "kernel_logged_in": "ixigo",
                "name": "Sudipta Saikia",
                "phone": "+918638586171",
            },
            "09:10",
            page,
            _Session(),  # type: ignore[arg-type]
        )
        self.assertNotIn("Assured pop-up", spoken)
        self.assertNotIn("I don't want Free Cancellation", spoken)

    def test_skip_free_cancellation_clicks_decline_not_search(self) -> None:
        """On Assured radios, skip means I don't want Free Cancellation + Continue — not a Google search."""
        from app.turn_loop import align_action, has_live_facts

        page = (
            "Add Free Cancellation Please select an option before proceeding "
            "Assured Free Cancellation Assured Flex I don't want Free Cancellation Continue"
        )
        state = {
            "stage": "booking",
            "stuck_kind": "addon_buy",
            "stuck_page": page,
            "selected_hint": "09:10",
            "origin": "Guwahati",
            "destination": "Delhi",
            "depart": "2026-12-04",
            "seller": "ixigo",
            "kernel_session_id": "sess-skip",
            "kernel_auth_mode": "guest",
        }
        self.assertTrue(has_live_facts(state))
        plan = align_action("skip", state)
        self.assertEqual(plan.get("do"), "book")
        self.assertIn("don't want", str(plan.get("tap") or "").lower())
        self.assertNotEqual(plan.get("do"), "search")
        agent = Path("app/page_agent.py").read_text(encoding="utf-8")
        self.assertIn("user_tap_upsell_continue", agent)

    def test_ixigo_login_popup_asks_for_mobile_then_otp(self) -> None:
        """Sign-in drawer over Fare Summary is login. Ask for a mobile, remember it, then the OTP."""
        from app.travel_agent import _booking_page_reply

        popup = (
            "Review & Traveller Details Fare Summary Refund on Cancellation Total Amount ₹10,098 "
            "Log in to Ixigo Enter Mobile Number Continue Log in with Google"
        )
        self.assertEqual(
            classify_page(popup, "https://www.ixigo.com/flight/booking/review"),
            "login",
        )
        otp = (
            "Verify Your Mobile Number Enter the OTP received via SMS on +91 8638586171 "
            "Guwahati New Delhi Fri 04 Dec 09:10"
        )
        self.assertEqual(
            classify_page(otp, "https://www.ixigo.com/flight/booking/review"),
            "otp",
        )

        class _Session:
            page = None

        state = {
            "seller": "ixigo",
            "kernel_auth_mode": "guest",
            "selected_hint": "09:10",
            "origin": "Guwahati",
            "destination": "Delhi",
            "kernel_session_id": "sess",
        }
        spoken = _booking_page_reply(state, "09:10", popup, _Session())  # type: ignore[arg-type]
        self.assertIn("10-digit mobile", spoken)
        self.assertNotIn("close that popup", spoken)
        self.assertTrue(state.get("kernel_awaiting_phone"))
        self.assertEqual(state.get("kernel_auth_mode"), "login")

        remembered = dict(state)
        remembered["login_phone"] = "+918638586171"
        remembered["kernel_awaiting_phone"] = False
        spoken_saved = _booking_page_reply(remembered, "09:10", popup, _Session())  # type: ignore[arg-type]
        self.assertIn("6171", spoken_saved)
        self.assertIn("continue", spoken_saved.lower())
        self.assertFalse(remembered.get("kernel_awaiting_phone"))
        self.assertNotIn("close that popup", spoken_saved)

        from app.turn_loop import align_action

        plan_login = align_action("continue", remembered)
        self.assertEqual(plan_login.get("do"), "book")
        self.assertEqual(plan_login.get("tap") or "", "")
        agent_src = Path("app/page_agent.py").read_text(encoding="utf-8")
        self.assertIn("login_phone_typed", agent_src)
        self.assertIn("login_clear_continue_tap", agent_src)
        from app.kernel_otp import keep_kernel_otp_session, should_type_login_phone
        from app.step_read import form_sheet

        drawer = (
            "Review & Traveller Details Fare Summary Log in to Ixigo "
            "Enter Mobile Number Continue Log in with Google"
        )
        self.assertTrue(form_sheet(drawer))
        self.assertTrue(should_type_login_phone("login", drawer))
        contact = "Add contact details Email address Please enter Mobile Number Continue"
        self.assertFalse(should_type_login_phone("other", contact))
        self.assertTrue(keep_kernel_otp_session("boxes_miss"))
        otp_src = Path("app/kernel_otp.py").read_text(encoding="utf-8")
        self.assertIn("on_otp_result", otp_src)
        agent_src2 = Path("app/travel_agent.py").read_text(encoding="utf-8")
        self.assertIn('action == "reconnect"', agent_src2)

        spoken_otp = _booking_page_reply(remembered, "09:10", otp, _Session())  # type: ignore[arg-type]
        self.assertIn("6 digits", spoken_otp)
        self.assertTrue(remembered.get("kernel_awaiting_otp"))
        agent = Path("app/page_agent.py").read_text(encoding="utf-8")
        self.assertEqual(agent.count("_close_ixigo_login_modal(page)"), 1)

    def test_addons_sheet_with_fare_is_not_a_results_list(self) -> None:
        """Seat map still shows 18:20 and a price. That must not restart login."""
        import inspect

        from app.kernel_otp import begin_login

        hay = (
            "Choose add-ons Add seats 18:20 ₹10,263 Book "
            "You missed adding Meal(s) and Seat(s) Okay, let me select Skip add-ons"
        )
        self.assertEqual(
            classify_page(hay, "https://www.cleartrip.com/flights/itinerary/abc"),
            "addons",
        )
        src = inspect.getsource(classify_page)
        self.assertLess(src.index("skip add"), src.index("if fare_list"))
        login = inspect.getsource(begin_login)
        self.assertLess(login.index("resume_lost"), login.index("Mobile field did not open"))
        self.assertLess(login.index("resume_lost"), login.index("ixigo_results_url"))
        self.assertIn("_connect_browser", login)
        self.assertIn('stage == "error"', login)
        self.assertIn('{"login", "otp"}', login)
        self.assertIn("keep_kernel_browser", login)
        self.assertIn("_RetryBeginLogin", login)
        self.assertGreaterEqual(login.count("raise _RetryBeginLogin"), 2)
        agent = Path("app/travel_agent.py").read_text(encoding="utf-8")
        self.assertIn('status == "resume_lost"', agent)
        self.assertIn("fresh fare list", agent)
        self.assertIn("did not open login", agent)
        self.assertIn('session_id if attempt == 0 else ""', agent)
        self.assertIn("fresh_browser=bool(loaded.get(\"require_otp\")) or attempt > 0", agent)

    def test_fare_drawer_over_results_is_fare_sheet(self) -> None:
        from app.page_agent import _looks_like_fare_sheet

        drawer = (
            "Select your fare Saver ₹10,283 Value ₹11,100 Continue "
            "IndiGo 18:25 Book ₹10,283 IndiGo 09:15 Book ₹10,283"
        )
        self.assertTrue(_looks_like_fare_sheet(drawer))
        self.assertEqual(
            classify_page(drawer, "https://www.cleartrip.com/flights/results"),
            "fare_sheet",
        )
        # Two fare names + Continue over a list, even without the exact header.
        twin = "Saver ₹9,100 Value ₹10,200 Continue Booking IndiGo 18:20 Book ₹9,100"
        self.assertTrue(_looks_like_fare_sheet(twin))
        self.assertEqual(classify_page(twin, "https://www.cleartrip.com/flights/results"), "fare_sheet")
        # A single VALUE badge on review must not become fare_sheet.
        review_badge = (
            "Review & Traveller Details Fare Summary Total Amount ₹9,116 "
            "Air-India Express IX 1513 13:15 VALUE Continue"
        )
        self.assertFalse(_looks_like_fare_sheet(review_badge))
        self.assertEqual(
            classify_page(review_badge, "https://www.ixigo.com/flight/booking/1"),
            "review",
        )
        # Ixigo Review Details with Continue + ₹ + clock must NOT become a fare list.
        ixigo_review = (
            "Review Details Fare Summary Base Fare ₹8,523 Taxes ₹902 Total ₹9,425 "
            "Guwahati 8:55 Delhi Billing Address Street Continue Book flights"
        )
        self.assertEqual(
            classify_page(ixigo_review, "https://www.ixigo.com/flight/booking/review"),
            "review",
        )
        from app.step_read import accept_choice, is_contact_form, is_name_confirm

        # Fare summary + billing address is review, not a govt-id name check.
        self.assertFalse(is_name_confirm(ixigo_review))
        self.assertFalse(is_contact_form(ixigo_review))
        self.assertEqual(
            accept_choice(
                "confirm",
                {"tap": "Confirm", "page": ixigo_review, "kind": "name_confirm"},
            ),
            "Continue",
        )
        # Stuck reply must stay off the fare list.
        from app.travel_agent import _booking_page_reply

        class _Fake:
            page = type("P", (), {"url": "https://www.ixigo.com/flight/booking/review"})()

        spoken = _booking_page_reply(
            {
                "origin": "Guwahati",
                "destination": "Delhi",
                "depart": "2026-12-04",
                "seller": "ixigo",
                "selected_hint": "8:55",
                "last_vision": {"kind": "results", "fares_visible": True},
            },
            "8:55",
            ixigo_review,
            _Fake(),  # type: ignore[arg-type]
        )
        self.assertNotIn("name check", spoken.lower())
        self.assertNotIn("fare list", spoken.lower())
        self.assertRegex(spoken, r"Continue")
        contact = (
            "Review & Traveller Details Contact Details Please enter valid email address "
            "Email Mobile Number Pincode GST Details Billing Address Continue Total Amount ₹9,425"
        )
        self.assertTrue(is_contact_form(contact))
        self.assertFalse(is_name_confirm(contact))
        contact_spoken = _booking_page_reply(
            {
                "origin": "Guwahati",
                "destination": "Delhi",
                "depart": "2026-12-04",
                "seller": "ixigo",
                "selected_hint": "8:55",
                "email": "sudiptasaikia6@gmail.com",
                "last_vision": {"kind": "review", "tap": "Confirm", "controls": ["Confirm"]},
            },
            "8:55",
            contact,
            _Fake(),  # type: ignore[arg-type]
        )
        self.assertIn("contact", contact_spoken.lower())
        self.assertNotIn("name check", contact_spoken.lower())
        self.assertNotIn("wants Confirm", contact_spoken)
        self.assertIn("sudiptasaikia6@gmail.com", contact_spoken)
        self.assertIn("I'll type", contact_spoken)
        bare = _booking_page_reply(
            {
                "origin": "Guwahati",
                "destination": "Delhi",
                "depart": "2026-12-04",
                "seller": "ixigo",
                "selected_hint": "8:55",
            },
            "8:55",
            contact,
            _Fake(),  # type: ignore[arg-type]
        )
        self.assertIn("send your email", bare.lower())
        self.assertNotIn("I'll press Continue", bare)
        from app.step_read import _fare_list_sheet, classify_sheet, decide_sheet

        self.assertEqual(
            decide_sheet(contact, {"email": "sudiptasaikia6@gmail.com"}).get("action"),
            "fill",
        )
        self.assertEqual(decide_sheet(contact, {}).get("action"), "ask")
        self.assertIn("email", decide_sheet(contact, {}).get("missing") or [])
        fare_list = "IndiGo 08:55 Book ₹9,100 Air India 13:15 Book ₹9,400 SpiceJet 18:20 Book ₹8,900"
        self.assertEqual(decide_sheet(fare_list, {}).get("action"), "pick")
        assured_list = (
            fare_list + " Assured Free cancellation I don't want Free Cancellation"
        )
        self.assertEqual(classify_sheet(assured_list), "results")
        self.assertNotEqual(decide_sheet(assured_list, {}).get("sheet"), "upsell")
        one_card = (
            "GAU Guwahati DEL New Delhi Fri 04 Dec Sort by Departure "
            "Akasa Air 09:10 19:10 ₹8,365 Book I don't want Free Cancellation"
        )
        self.assertTrue(_fare_list_sheet(one_card))
        self.assertEqual(classify_sheet(one_card), "results")
        self.assertNotEqual(decide_sheet(one_card, {}).get("sheet"), "upsell")
        real_upsell = "Please select an option before proceeding Assured Free Cancellation I don't want Free Cancellation"
        self.assertEqual(classify_sheet(real_upsell), "upsell")
        list_spoken = _booking_page_reply(
            {
                "origin": "Guwahati",
                "destination": "Delhi",
                "depart": "2026-12-04",
                "seller": "ixigo",
                "selected_hint": "09:10",
            },
            "09:10",
            "Akasa Air 09:10 19:10 ₹8,365 Book IndiGo 18:20 21:05 ₹10,263 Book Assured Free cancellation",
            _Fake(),  # type: ignore[arg-type]
        )
        self.assertNotIn("Assured pop-up", list_spoken)
        self.assertIn("09:10", list_spoken)
        from app.turn_loop import align_action

        go = align_action(
            "go on",
            {
                "origin": "Guwahati",
                "destination": "Delhi",
                "depart": "2026-12-04",
                "seller": "ixigo",
                "selected_hint": "09:10",
                "stage": "booking",
                "stuck_kind": "addon_buy",
                "stuck_page": assured_list,
                "options": [{"depart": "09:10", "airline": "Akasa Air", "price": "8365"}],
                "kernel_auth_mode": "guest",
            },
        )
        self.assertEqual(go["do"], "book")
        picked = align_action(
            "saved traveller",
            {
                "origin": "Guwahati",
                "destination": "Delhi",
                "depart": "2026-12-04",
                "seller": "ixigo",
                "selected_hint": "09:10",
                "stage": "booking",
                "stuck_kind": "addon_buy",
                "stuck_page": one_card,
                "options": [{"depart": "09:10", "airline": "Akasa Air", "price": "8365"}],
                "kernel_auth_mode": "guest",
            },
        )
        self.assertEqual(picked["do"], "book")
        self.assertNotEqual(picked.get("tap"), "__select_traveller__")
        from app.turn_loop import align_action as _align

        fresh = _align(
            "ghy to delhi 4th dec",
            {
                "origin": "Guwahati",
                "destination": "Delhi",
                "depart": "2026-12-04",
                "seller": "ixigo",
                "stage": "login",
                "stuck_kind": "login",
                "kernel_auth_mode": "guest",
                "stuck_page": "Log in to Ixigo Enter Mobile Number Log in with Google",
                "last_vision": {"kind": "login", "what": "Ixigo login"},
                "options": [{"depart": "09:10", "airline": "Akasa Air", "price": "8365"}],
            },
        )
        self.assertEqual(fresh["do"], "search")
        self.assertEqual(fresh["kind"], "search")
        addons = "Choose add-ons No meal selected Skip add-ons"
        self.assertEqual(decide_sheet(addons, {}).get("action"), "click")
        self.assertIn("Skip", str(decide_sheet(addons, {}).get("tap") or ""))
        named = "Add traveller details First & middle name Last name Continue"
        self.assertEqual(decide_sheet(named, {}).get("action"), "ask")
        self.assertIn("full name", decide_sheet(named, {}).get("missing") or [])
        ready = decide_sheet("Review your itinerary Fare summary Continue", {"email": "a@b.co"})
        self.assertEqual(ready.get("action"), "click")
        self.assertEqual(ready.get("tap"), "Continue")
        # Confirm modal covers Contact Details — ask Confirm/Edit, never fill behind it.
        name_over_contact = (
            "Review Details Contact Details Email Mobile Please ensure that your name matches "
            "your govt ID such as Aadhaar Confirm Edit Mr Sudipta"
        )
        self.assertTrue(is_name_confirm(name_over_contact))
        self.assertFalse(is_contact_form(name_over_contact))
        name_ask = decide_sheet(name_over_contact, {"email": "a@b.co"})
        self.assertEqual(name_ask.get("action"), "ask")
        self.assertEqual(name_ask.get("tap"), "Confirm")
        self.assertIn("Edit", name_ask.get("fills") or [])
        # Modal with Title / First & Middle Name still wins when billing OCR is behind it.
        modal_over_billing = (
            "Review Details Please ensure that your name matches your govt. ID such as "
            "Aadhaar, Passport or Driver's License Adult Title Mr First & Middle Name "
            "Sudipta Last Name Saikia Edit Confirm "
            "Billing Address Pin code 785001 City Jorhat State Assam Continue"
        )
        self.assertTrue(is_name_confirm(modal_over_billing))
        self.assertEqual(classify_sheet(modal_over_billing), "name_confirm")
        modal_ask = decide_sheet(modal_over_billing, {"pincode": "785001", "email": "a@b.co"})
        self.assertEqual(modal_ask.get("action"), "ask")
        self.assertEqual(modal_ask.get("tap"), "Confirm")

    def test_saved_traveller_sheet_continues_not_confirm(self) -> None:
        """Saved list: Continue + fill conscience. Never Confirm; free-cancellation stays upsell."""
        import inspect

        from app.step_read import (
            classify_sheet,
            decide_sheet,
            forward_tap,
            is_name_confirm,
            is_saved_traveller_sheet,
        )
        from app.travel_agent import _booking_page_reply

        saved_list = (
            "Review Details Traveller Details Adult 1 Mr Sudipta Saikia Ms Anita Continue Confirm"
        )
        self.assertEqual(classify_sheet(saved_list), "traveller")
        self.assertTrue(is_saved_traveller_sheet(saved_list))
        self.assertFalse(is_name_confirm(saved_list))
        self.assertEqual(forward_tap(saved_list), "Continue")
        ready = decide_sheet(
            saved_list,
            {"email": "a@b.co", "name": "Sudipta Saikia", "phone": "8876233052"},
        )
        self.assertEqual(ready.get("sheet"), "traveller")
        self.assertEqual(ready.get("action"), "click")
        self.assertEqual(ready.get("tap"), "Continue")
        # Empty contact boxes → fill first; never jump to Confirm.
        needs_email = (
            "Review Details Traveller Details Adult 1 Mr Sudipta Saikia "
            "Contact Details Please enter a valid email Continue Confirm"
        )
        self.assertTrue(is_saved_traveller_sheet(needs_email))
        fill_move = decide_sheet(needs_email, {"email": "a@b.co", "name": "Sudipta Saikia"})
        self.assertEqual(fill_move.get("action"), "fill")
        self.assertEqual(fill_move.get("tap"), "Continue")
        # Confirm still wins on a real govt-id modal.
        confirm_page = (
            "Review Details Please ensure that your name matches your govt ID Confirm Edit Continue"
        )
        self.assertFalse(is_saved_traveller_sheet(confirm_page))
        self.assertTrue(is_name_confirm(confirm_page))
        self.assertEqual(forward_tap(confirm_page), "Confirm")
        # Buried govt-ID disclaimer + billing/Adult 1 is Continue work, not Confirm.
        buried = (
            "Review Details Traveller Details Adult 1 Mr Sudipta Saikia "
            "Please ensure that your name matches your govt ID such as Aadhaar "
            "Billing Address Pincode State Continue Confirm Edit"
        )
        self.assertEqual(classify_sheet(buried), "traveller")
        self.assertTrue(is_saved_traveller_sheet(buried))
        self.assertFalse(is_name_confirm(buried))
        self.assertEqual(forward_tap(buried), "Continue")
        billing_ask = decide_sheet(buried, {"email": "a@b.co", "name": "Sudipta Saikia"})
        self.assertEqual(billing_ask.get("sheet"), "traveller")
        self.assertEqual(billing_ask.get("action"), "ask")
        self.assertEqual(set(billing_ask.get("missing") or []), {"pincode", "state", "address"})
        filled_billing = decide_sheet(
            buried,
            {
                "email": "a@b.co",
                "name": "Sudipta Saikia",
                "pincode": "781001",
                "billing_address": "12 Zoo Road",
                "billing_state": "Assam",
            },
        )
        self.assertEqual(filled_billing.get("action"), "fill")
        self.assertEqual(filled_billing.get("tap"), "Continue")
        city_sheet = (
            "Review Details Traveller Details Adult 1 Mr Sudipta Saikia "
            "Billing Address Address Pincode City State Continue"
        )
        city_ask = decide_sheet(city_sheet, {"email": "a@b.co", "name": "Sudipta Saikia"})
        self.assertEqual(
            set(city_ask.get("missing") or []),
            {"pincode", "state", "address", "city"},
        )
        # decide_sheet is the stuck narrative owner — no parallel name-check branch.
        spoken_buried = _booking_page_reply(
            {
                "origin": "Guwahati",
                "destination": "Delhi",
                "depart": "2026-12-04",
                "seller": "ixigo",
                "selected_hint": "2:20",
                "name": "Sudipta Saikia",
                "email": "a@b.co",
            },
            "2:20",
            buried,
            type("S", (), {"page": None})(),
        )
        self.assertIn("saved travellers", spoken_buried.lower())
        self.assertNotIn("name check", spoken_buried.lower())
        self.assertNotIn("wants Confirm", spoken_buried)
        # continue never reuses a stale pick-traveller tap on this sheet.
        from unittest.mock import patch

        from app.travel_agent import absorb_details
        from app.turn_loop import align_action

        absorbed = absorb_details({}, "781001 Assam")
        self.assertEqual(absorbed.get("pincode"), "781001")
        self.assertEqual(absorbed.get("billing_state"), "Assam")
        absorbed_full = absorb_details({}, "address 12 Zoo Road city Guwahati 781001 Assam")
        self.assertEqual(absorbed_full.get("pincode"), "781001")
        self.assertEqual(absorbed_full.get("billing_state"), "Assam")
        self.assertEqual(absorbed_full.get("city"), "Guwahati")
        self.assertIn("Zoo Road", str(absorbed_full.get("billing_address") or ""))
        cont_state = {
            "origin": "Guwahati",
            "destination": "Delhi",
            "depart": "2026-12-04",
            "seller": "ixigo",
            "selected_hint": "2:20",
            "stage": "booking",
            "stuck_kind": "name_confirm",
            "stuck_page": buried,
            "chosen_tap": "__select_traveller__",
            "name": "Sudipta Saikia",
            "email": "a@b.co",
            "pincode": "781001",
            "billing_state": "Assam",
            "last_vision": {"kind": "review"},
        }
        with patch(
            "app.turn_loop.interpret_turn",
            return_value={
                "goal": "forward",
                "intent": "book",
                "kind": "review",
                "reply": "Continuing.",
                "tap": "",
                "facts": [],
                "source": "gemini",
            },
        ):
            cont = align_action("continue", cont_state)
        # Explicit continue presses Continue (fill gaps in checkout) — never a details-only turn
        # and never keep a stale pick-traveller tap.
        self.assertEqual(cont["do"], "book")
        self.assertEqual(cont.get("tap"), "Continue")
        self.assertNotEqual(cont.get("tap"), "__select_traveller__")
        # Free-cancellation radio with a name behind it is not the saved-list sheet.
        radio = (
            "Add Free Cancellation Please select an option before proceeding "
            "I don't want Free Cancellation Traveller Details Mr Sudipta Saikia Continue"
        )
        self.assertFalse(is_saved_traveller_sheet(radio))
        state = {
            "origin": "Guwahati",
            "destination": "Delhi",
            "depart": "2026-12-04",
            "seller": "ixigo",
            "selected_hint": "8:55",
            "name": "Sudipta Saikia",
            "email": "a@b.co",
        }
        spoken = _booking_page_reply(
            state,
            "8:55",
            saved_list,
            type("S", (), {"page": None})(),
        )
        self.assertIn("saved travellers", spoken.lower())
        self.assertIn("Continue", spoken)
        self.assertNotIn("wants Confirm", spoken)
        self.assertEqual(state.get("stuck_kind"), "passenger")
        self.assertEqual((state.get("pending_step") or {}).get("tap"), "Continue")
        checkout = inspect.getsource(
            __import__("app.page_agent", fromlist=["run_checkout_agent"]).run_checkout_agent
        )
        self.assertIn("after_pick_continue", checkout)
        self.assertIn("user_pick_traveller", checkout)

    def test_sheet_decides_fill_ask_or_click(self) -> None:
        """Continue follows the open sheet: ask, fill, or the visible button. Never a stage default."""
        from app.step_read import decide_sheet
        from app.turn_loop import align_action

        base = {
            "origin": "Guwahati",
            "destination": "Delhi",
            "depart": "2026-12-04",
            "seller": "ixigo",
            "selected_hint": "8:55",
            "stage": "booking",
            "last_vision": {"kind": "review"},
        }
        contact = (
            "Review & Traveller Details Contact Details Please enter valid email address "
            "Email Mobile Number Continue"
        )
        asked = align_action("continue", {**base, "stuck_page": contact, "stuck_kind": "review"})
        self.assertEqual(asked["do"], "reply")
        self.assertIn("email", asked["reply"].lower())
        self.assertNotEqual(asked.get("tap"), "Continue")

        filled = align_action(
            "continue",
            {**base, "stuck_page": contact, "stuck_kind": "review", "email": "a@b.co"},
        )
        # Explicit continue presses Continue (checkout fills gaps then clicks) — not a
        # details-only turn that types and never advances.
        self.assertEqual(filled["do"], "book")
        self.assertEqual(filled.get("tap"), "Continue")
        self.assertEqual(decide_sheet(contact, {"email": "a@b.co"})["action"], "fill")

        review = "Review your itinerary Fare summary Continue"
        clicked = align_action("continue", {**base, "stuck_page": review, "stuck_kind": "review"})
        self.assertEqual(clicked["do"], "book")
        self.assertEqual(clicked.get("tap"), "Continue")

        # Gemini "still missing DOB" must not steal an explicit continue into details-only.
        billing_ready = (
            "Review Details Fare Summary Total Amount ₹9,848 "
            "Billing Address Pin code 785001 City Jorhat State Assam Continue "
            "GST Details I would like to add my GST Number"
        )
        gem_dob = {
            "goal": "ask",
            "intent": "details",
            "kind": "form",
            "what": "Missing date of birth",
            "why": "form needs dob",
            "recommend": "send dob",
            "tap": "Continue",
            "reply": "I still need date of birth.",
            "facts": [],
            "source": "gemini",
        }
        from unittest.mock import patch

        with patch("app.turn_loop.interpret_turn", return_value=gem_dob):
            cont_ready = align_action(
                "continue",
                {
                    **base,
                    "stuck_page": billing_ready,
                    "stuck_kind": "form",
                    "email": "a@b.co",
                    "pincode": "785001",
                    "name": "Sudipta Saikia",
                    "live": {"host": "www.ixigo.com"},
                },
            )
        self.assertEqual(cont_ready["do"], "book")
        self.assertEqual(cont_ready.get("tap"), "Continue")

        pay = align_action(
            "continue",
            {**base, "stuck_page": "Pay to complete your booking Pay now", "stuck_kind": "pay", "stage": "payment"},
        )
        self.assertEqual(pay["do"], "reply")
        self.assertNotIn("pay now", (pay.get("tap") or "").lower())

        name_page = (
            "Review Details Contact Details Email Please ensure that your name matches "
            "your govt ID Confirm Edit"
        )
        name_yes = align_action(
            "confirm",
            {
                **base,
                "stuck_page": name_page,
                "stuck_kind": "name_confirm",
                "pending_step": {"tap": "Confirm", "page": name_page, "kind": "name_confirm"},
                "email": "a@b.co",
            },
        )
        self.assertEqual(name_yes["do"], "book")
        self.assertEqual(name_yes.get("tap"), "Confirm")

        # Front sheet wins: overlay Skip is pressed; background Continue is ignored.
        from app.step_read import active_sheet_hay, decide_sheet

        under = (
            "Contact Details Please enter valid email address Email Continue "
            "Skip add-ons Choose add-ons"
        )
        front = "Choose add-ons Skip add-ons"
        hay, cover = active_sheet_hay(under, front=front, controls="btn[ ] Skip add-ons", overlay=True)
        self.assertTrue(cover)
        self.assertIn("Skip add-ons", hay)
        move = decide_sheet(
            under,
            {"email": "a@b.co"},
            front=front,
            controls="btn[ ] Skip add-ons",
            overlay=True,
        )
        self.assertEqual(move.get("action"), "click")
        self.assertIn("Skip", str(move.get("tap") or ""))
        self.assertNotEqual(move.get("action"), "fill")
        upsell_front = "Free Cancellation No, Thanks Secure My Trip"
        upsell_move = decide_sheet(
            under + " " + upsell_front,
            {},
            front=upsell_front,
            controls="btn[ ] No, Thanks",
            overlay=True,
        )
        self.assertEqual(upsell_move.get("action"), "ask")
        self.assertRegex(str(upsell_move.get("tap") or ""), r"No,?\s*Thanks")

        # stuck_front makes yes press the cover button, not fill contact under it.
        skip_yes = align_action(
            "yes",
            {
                **base,
                "stuck_page": under,
                "stuck_kind": "addons",
                "stuck_front": front,
                "stuck_controls": "btn[ ] Skip add-ons",
                "pending_step": {"tap": "Skip add-ons", "page": front, "kind": "addons"},
                "email": "a@b.co",
            },
        )
        self.assertEqual(skip_yes["do"], "book")
        self.assertIn("Skip", str(skip_yes.get("tap") or ""))

    def test_front_sheet_wiring_is_locked(self) -> None:
        """Checkout and replies must prefer the covering dialog over the page under it."""
        import inspect

        from app.page_agent import run_checkout_agent
        from app.step_read import active_sheet_hay, arm_pending_step
        from app.travel_agent import _remember_front_sheet
        from app.turn_loop import align_action

        checkout = inspect.getsource(run_checkout_agent)
        self.assertIn("read_surface(page)", checkout)
        self.assertIn("overlay=", checkout)
        self.assertIn("stuck_front", inspect.getsource(align_action) + inspect.getsource(arm_pending_step))
        self.assertTrue(callable(active_sheet_hay))
        self.assertTrue(callable(_remember_front_sheet))
        state: dict = {}
        _remember_front_sheet(
            state,
            {"overlay": True, "text": "Please ensure name Confirm Edit", "controls": "btn[ ] Confirm"},
        )
        self.assertIn("Confirm", state.get("stuck_front") or "")
        self.assertIn("Confirm", state.get("stuck_controls") or "")
        _remember_front_sheet(state, {"overlay": False, "text": "", "controls": ""})
        self.assertNotIn("stuck_front", state)

    def test_after_book_follow_through_is_wired(self) -> None:
        import inspect

        from app.page_agent import _advance_after_book, _mouse_book_depart, run_checkout_agent

        self.assertTrue(callable(_advance_after_book))
        self.assertTrue(callable(_mouse_book_depart))
        src = inspect.getsource(run_checkout_agent)
        self.assertIn("_advance_after_book", src)
        self.assertIn("book_still_results", src)
        self.assertIn("results_after_book:try_continue", src)
        # Same results signature after a Book hit must not freeze into ask.
        self.assertIn("book_retries", src)
        # After Book, fare_sheet runs confirm_fare instead of pausing on results.
        click = src.split('if kind == "click_time":')[1][:5000]
        self.assertIn("_advance_after_book", click)
        self.assertIn("confirm_fare", click)
        # Continue after a card hit uses confirm_fare even when stage is still results.
        click_branch = src.split('if kind == "click":')[1][:1800]
        self.assertIn("results_after_book", click_branch)
        self.assertIn("_confirm_fare", click_branch)

    def test_vision_smoke_guards_post_book_ask_on_results(self) -> None:
        """Live smoke must fail if Book hits a card then freezes as ask:results."""
        smoke = Path("scripts/vision_step_smoke.py").read_text(encoding="utf-8")
        self.assertIn("ask:results", smoke)
        self.assertIn("confirm_fare", smoke)
        self.assertIn("after_book", smoke)
        self.assertIn("without fare follow-through", smoke)

    def test_addons_contract_mentions_seats_and_skips(self) -> None:
        from app.step_contract import build_contract, format_contract_stuck, left_addons
        from app.travel_agent import _booking_page_reply

        page = (
            "Choose add-ons No meal, additional check-in baggage and seats selected. "
            "Add seats Seats are cheaper when pre-booked. Onward GAU -> DEL No seat added. "
            "Add delicious meals 0/1 selected Skip add-ons Base fare ₹8,241"
        )
        contract = build_contract(page, stage="addons", seller="Cleartrip", hint="18:20", user_goal="skip")
        self.assertEqual(contract["stage"], "addons")
        self.assertEqual(contract["action"], "skip_extras")
        self.assertFalse(contract["requires_user"])
        self.assertTrue(any(re.search(r"seat", f, re.I) for f in contract["facts"]))
        spoken = format_contract_stuck(contract, which="18:20")
        self.assertRegex(spoken.lower(), r"seat")
        self.assertFalse(left_addons(page, "addons"))
        self.assertTrue(left_addons("Review Details Continue Fare Summary", "review"))

        reply = _booking_page_reply(
            {
                "origin": "Guwahati",
                "destination": "Delhi",
                "depart": "2026-11-15",
                "seller": "cleartrip",
                "selected_hint": "18:20",
            },
            "18:20",
            page,
            type("S", (), {"page": None})(),
        )
        self.assertRegex(reply.lower(), r"seat")
        self.assertRegex(reply.lower(), r"skip add-ons")
        self.assertNotIn("exact button", reply.lower())
        self.assertNotIn("haven't tapped", reply.lower())
        self.assertNotIn("trip protect", reply.lower())
        self.assertFalse(left_addons(page, "review"))

    def test_yes_and_decline_press_skip_on_addons(self) -> None:
        from app.step_read import addons_sheet, decide_front
        from app.travel_agent import extract_user_ask
        from app.turn_loop import align_action

        page = "Choose add-ons No meal, additional check-in baggage and seats selected Skip add-ons"
        self.assertTrue(addons_sheet(page))
        decision = decide_front(page, "", seller="cleartrip", hint="10:45")
        self.assertEqual(decision["kind"], "addons")
        self.assertEqual(decision["action"], "click")
        self.assertIn("Skip add-ons", decision.get("tap") or "")
        state = {
            "origin": "Guwahati",
            "destination": "Delhi",
            "depart": "2026-12-04",
            "stuck_page": "Reply yes and I'll press No, I will continue without Trip Protect. " + page,
            "last_vision": {"kind": "upsell", "what": "Trip Protect", "tap": "Continue"},
            "stuck_kind": "upsell",
        }
        asked = extract_user_ask("yes", state)
        self.assertEqual(asked.get("goal"), "skip")
        self.assertEqual(asked.get("tap"), "")
        self.assertEqual(extract_user_ask("Decline", state).get("goal"), "skip")
        yes = align_action("yes", {**state, "last_vision": {"kind": "addons", "tap": "Skip add-ons"}, "stuck_page": page})
        decline = align_action("Decline", {**state, "stuck_page": page, "last_vision": {"kind": "addons", "tap": ""}})
        self.assertEqual(yes["do"], "book")
        self.assertIn("Skip add-ons", yes.get("tap") or "")
        self.assertEqual(decline["do"], "book")
        self.assertIn("Skip add-ons", decline.get("tap") or "")

    def test_skip_click_leaves_addons_for_the_next_step(self) -> None:
        """Smoke: a late-painted Skip is clicked and the sheet actually changes."""
        from playwright.sync_api import sync_playwright

        from app.page_agent import _leave_addons, classify_page

        html = """<!doctype html><html><body>
        <h1>Choose add-ons</h1>
        <div id="box" style="background:#ddd;width:420px;height:240px"></div>
        <script>
          setTimeout(() => {
            document.getElementById('box').outerHTML =
              '<p>No meal, additional check-in baggage and seats selected</p>' +
              '<button id="skip" style="width:180px;height:44px">Skip add-ons</button>';
            document.getElementById('skip').onclick = () => {
              document.body.innerHTML = '<h1>Review your itinerary</h1><p>Fare summary Total Amount Continue</p>';
            };
          }, 900);
        </script></body></html>"""
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.set_content(html)
            log: list[str] = []
            ok = _leave_addons(page, log, rounds=3)
            text = page.inner_text("body")
            stage = classify_page(text, "https://www.cleartrip.com/flights/itinerary/addons")
            browser.close()
        self.assertTrue(ok, log)
        self.assertNotIn("Choose add-ons", text)
        self.assertIn("Review", text)
        self.assertNotEqual(stage, "addons")

    def test_cleartrip_skip_uses_react_fiber_onclick(self) -> None:
        """Cleartrip Skip ignores el.click()/mouse — fiber onClick is what advances."""
        import inspect

        from playwright.sync_api import sync_playwright

        from app.page_agent import _leave_addons, _skip_addons, classify_page

        src = inspect.getsource(_skip_addons)
        self.assertIn("__reactFiber$", src)
        self.assertIn("memoizedProps", src)
        html = """<!doctype html><html><body>
        <h1>Choose add-ons</h1>
        <p>No meal, additional check-in baggage and seats selected</p>
        <button id="skip" style="width:180px;height:44px">Skip add-ons</button>
        <script>
          const btn = document.getElementById('skip');
          // Pretend React fiber — real Cleartrip attaches the same shape.
          btn.__reactFiber$test = {
            memoizedProps: {
              onClick: () => {
                document.body.innerHTML =
                  '<h1>Add Contact details</h1><p>+91 Email address Continue</p>' +
                  '<p>Review traveller details</p>';
              }
            }
          };
        </script></body></html>"""
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.set_content(html)
            log: list[str] = []
            ok = _leave_addons(page, log, rounds=2)
            text = page.inner_text("body")
            stage = classify_page(text, "https://www.cleartrip.com/flights/itinerary/abc")
            browser.close()
        self.assertTrue(ok, log)
        self.assertIn("Add Contact", text)
        self.assertNotIn("Choose add-ons", text)
        self.assertNotEqual(stage, "addons")

    def test_continue_to_payment_is_traveller_handoff_not_charge(self) -> None:
        """Cleartrip 'Continue to payment' advances the form; it is not Pay now."""
        import inspect

        from playwright.sync_api import sync_playwright

        from app.page_agent import (
            _PAY_REFUSE,
            _cleartrip_guest_sprint,
            _fill_passenger_fields,
            _js_continue,
            run_checkout_agent,
        )

        js_src = inspect.getsource(_js_continue).lower()
        self.assertIn("continue to payment", js_src)
        self.assertIn("1-step checkout", js_src)
        self.assertIn("continue\\\\b", js_src)
        self.assertIn("traveller name", inspect.getsource(_fill_passenger_fields).lower())
        self.assertFalse(_PAY_REFUSE.search("Continue to payment"))
        refuse = inspect.getsource(run_checkout_agent)
        self.assertIn("continue to payment", refuse.lower())
        self.assertIn("user_tap_continue", refuse)
        self.assertIn("advance_forward_control", refuse)
        self.assertIn("_cleartrip_guest_sprint", refuse)
        sprint = inspect.getsource(_cleartrip_guest_sprint)
        self.assertIn("data-ct-fill", sprint)
        self.assertIn("_continue_to_payment", sprint)
        html = """<!doctype html><html><body>
        <h1>Add traveller details</h1>
        <label>First &amp; middle name (Given name) <input id="fn" style="width:200px;height:36px" /></label>
        <label>Last name (Surname) <input id="ln" style="width:200px;height:36px" /></label>
        <button id="go" style="width:220px;height:48px">Continue to payment</button>
        <script>
          const btn = document.getElementById('go');
          btn.__reactFiber$test = {
            memoizedProps: {
              onClick: () => {
                const fn = document.getElementById('fn');
                const ln = document.getElementById('ln');
                if (!fn || !ln || !fn.value || !ln.value) {
                  document.body.innerHTML = '<p>Please enter a valid last name</p>';
                  return;
                }
                document.body.innerHTML =
                  '<h1>Payment options</h1><p>Pay to complete your booking Enter card number</p>';
              }
            }
          };
        </script></body></html>"""
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.set_content(html)
            notes = _fill_passenger_fields(
                page, {"name": "Sudipta Saikia", "gender": "male", "email": "", "phone": "", "dob": ""}
            )
            vals = page.evaluate(
                "() => ({fn: document.getElementById('fn').value, ln: document.getElementById('ln').value})"
            )
            moved = _js_continue(page)
            text = page.inner_text("body")
            browser.close()
        self.assertIn("first:ok", " ".join(notes), notes)
        self.assertIn("last:ok", " ".join(notes), notes)
        self.assertEqual(vals.get("fn"), "Sudipta")
        self.assertEqual(vals.get("ln"), "Saikia")
        self.assertTrue(moved)
        self.assertIn("Pay to complete", text)
        self.assertNotIn("valid last name", text.lower())

    def test_continue_opens_review_when_skip_does_not_move(self) -> None:
        """Extras stay put on Skip. Continue is what opens Review your itinerary."""
        from playwright.sync_api import sync_playwright

        from app.page_agent import _leave_addons, classify_page

        html = """<!doctype html><html><body>
        <p>6E 2092 GAU DEL 10:45 Fare summary Add ons ₹449 Extra baggage</p>
        <p>Make your journey fully flexible. Travel insurance.</p>
        <button id="skip" style="width:160px;height:40px">Skip add-ons</button>
        <button id="go" style="width:160px;height:44px">Continue</button>
        <script>
          document.getElementById('go').onclick = () => {
            document.body.innerHTML = '<h1>Review your itinerary</h1><p>Fare summary 6E 2092 Total amount</p>';
          };
        </script></body></html>"""
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.set_content(html)
            log: list[str] = []
            ok = _leave_addons(page, log, rounds=3)
            text = page.inner_text("body")
            stage = classify_page(text, "https://www.cleartrip.com/flights/itinerary/1")
            browser.close()
        self.assertTrue(ok, log)
        self.assertIn("review_continue:1", " ".join(log))
        self.assertIn("Review your itinerary", text)
        self.assertEqual(stage, "review")

    def test_continue_on_extras_is_not_rewritten_to_skip(self) -> None:
        from app.page_agent import classify_page, run_checkout_agent
        from app.step_read import addons_sheet, visible_forward
        from app.travel_agent import _booking_page_reply
        from app.turn_loop import align_action
        from playwright.sync_api import sync_playwright

        extras = (
            "6E 2092 10:45 Fare summary Add ons ₹449 Extra baggage "
            "Make your journey fully flexible Travel insurance Continue"
        )
        self.assertFalse(addons_sheet(extras))
        self.assertNotEqual(
            classify_page(extras, "https://www.cleartrip.com/flights/itinerary/1"),
            "addons",
        )
        self.assertEqual(visible_forward(extras, "Continue (do not buy the extras)"), "Continue")
        state = {
            "origin": "Guwahati",
            "destination": "Delhi",
            "depart": "2026-12-04",
            "seller": "cleartrip",
            "selected_hint": "10:45",
            "stuck_page": "I'll press Skip add-ons. Choose add-ons",
            "last_vision": {"kind": "addons", "tap": "Skip add-ons", "what": "Choose add-ons"},
            "stuck_kind": "addons",
        }
        plan = align_action("Continue (do not buy the extras)", {**state, "stuck_page": extras})
        self.assertEqual(plan["tap"], "Continue")
        reply = _booking_page_reply(state, "10:45", extras, type("S", (), {"page": None})())
        self.assertIn("Continue", reply)
        self.assertNotIn("Skip add-ons", reply)
        html = """<!doctype html><html><body>
        <p>6E 2092 10:45 Fare summary Add ons ₹449 Extra baggage Make your journey fully flexible Travel insurance</p>
        <button id="go" style="width:160px;height:44px">Continue</button>
        <script>
          document.getElementById('go').onclick = () => {
            document.body.innerHTML = '<h1>Review your itinerary</h1><p>Fare summary 6E 2092 Total amount</p>';
          };
        </script></body></html>"""
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.set_content(html)
            outcome = run_checkout_agent(
                page,
                hint="10:45",
                seller="cleartrip",
                origin="Guwahati",
                destination="Delhi",
                depart="2026-12-04",
                max_steps=4,
                chosen_tap="Continue",
            )
            text = page.inner_text("body")
            browser.close()
        self.assertIn("Review your itinerary", text)
        self.assertTrue(any(line.startswith("user_tap_continue:1") for line in outcome.log), outcome.log)
        self.assertFalse(any("skip add" in line.lower() for line in outcome.log), outcome.log)

    def test_continue_on_review_opens_login(self) -> None:
        from app.page_agent import classify_page, run_checkout_agent
        from app.step_read import addons_sheet
        from app.travel_agent import _booking_page_reply
        from playwright.sync_api import sync_playwright

        review = (
            "Review your itinerary Guwahati to New Delhi 6E 2092 10:45 "
            "Make your journey fully flexible Add seats Fare summary Continue Log in"
        )
        self.assertEqual(classify_page(review, "https://www.cleartrip.com/flights/itinerary/1"), "review")
        self.assertFalse(addons_sheet(review))
        state = {
            "origin": "Guwahati",
            "destination": "Delhi",
            "depart": "2026-12-04",
            "seller": "cleartrip",
            "selected_hint": "10:45",
            "stuck_kind": "addons",
        }
        reply = _booking_page_reply(state, "10:45", review, type("S", (), {"page": None})())
        self.assertIn("Review your itinerary", reply)
        self.assertIn("Continue", reply)
        self.assertNotIn("Choose add-ons", reply)
        html = """<!doctype html><html><body>
        <h1>Review your itinerary</h1>
        <p>6E 2092 10:45 Add seats Fare summary</p>
        <button id="go" style="width:160px;height:44px">Continue</button>
        <script>
          document.getElementById('go').onclick = () => {
            document.body.innerHTML = '<h1>Log in to Cleartrip</h1><p>Enter mobile number</p><button>Get OTP</button>';
          };
        </script></body></html>"""
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.set_content(html)
            outcome = run_checkout_agent(
                page,
                hint="10:45",
                seller="cleartrip",
                origin="Guwahati",
                destination="Delhi",
                depart="2026-12-04",
                max_steps=4,
                chosen_tap="Continue",
                pause_on_auth=True,
            )
            text = page.inner_text("body")
            browser.close()
        self.assertIn("Enter mobile number", text)
        self.assertTrue(any(line.startswith("user_tap_continue:1") for line in outcome.log), outcome.log)
        self.assertFalse(any("addons_fast" in line for line in outcome.log), outcome.log)
        self.assertFalse(any("Skip add-ons" in line for line in outcome.log), outcome.log)

    def test_named_control_below_the_fold_is_clicked(self) -> None:
        from app.page_agent import click_verified
        from playwright.sync_api import sync_playwright

        html = """<!doctype html><html><body>
        <p style="height:1400px">Fare summary. Please continue with your booking later.</p>
        <div id="go" style="width:160px;height:44px">Continue</div>
        <script>
          document.getElementById('go').onclick = () => {
            document.body.innerHTML = '<h1>Enter mobile number</h1>';
          };
        </script></body></html>"""
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 700})
            page.set_content(html)
            hit = click_verified(page, "Continue")
            text = page.inner_text("body")
            refused = click_verified(page, "Pay now")
            browser.close()
        self.assertTrue(hit)
        self.assertIn("Enter mobile number", text)
        self.assertFalse(refused)

    def test_acting_up_is_error_not_payment(self) -> None:
        from app.page_agent import classify_page, payment_sheet_open, site_error_page
        from app.travel_agent import _booking_page_reply

        err = (
            "Sorry our system is acting up Go to home "
            "Review your itinerary Pay now UPI card number pay to complete hidden Payment options"
        )
        self.assertTrue(site_error_page(err, "https://www.cleartrip.com/flights/itinerary/x"))
        self.assertEqual(classify_page(err, "https://www.cleartrip.com/flights/itinerary/x"), "error")
        self.assertFalse(payment_sheet_open(err, "error"))
        self.assertFalse(payment_sheet_open(err, "pay"))
        from app.step_read import ground_unknown_plan

        self.assertNotEqual(ground_unknown_plan(err, "btn Payment options", {}).get("action"), "stop")
        state = {
            "origin": "Guwahati",
            "destination": "Delhi",
            "depart": "2026-12-04",
            "seller": "cleartrip",
            "selected_hint": "10:45",
        }
        reply = _booking_page_reply(state, "10:45", err, type("S", (), {"page": None})())
        self.assertNotIn("server error", reply.lower())
        self.assertNotIn("servers are stumped", reply.lower())
        self.assertIn("EaseMyTrip 10:45", reply)
        self.assertNotIn("I'll press Go to home", reply)
        self.assertNotIn("Ready to pay", reply)
        self.assertNotIn("payment page", reply.lower())
        import inspect as _inspect

        from app.page_agent import _confirm_fare, _reopen_cleartrip_results, classify_page, run_checkout_agent
        from app.step_read import format_unknown_plan, ground_unknown_plan

        home = (
            "Where from? Where to? Enter departure and arrival airports / cities "
            "Search Flights Payment options UPI Cards"
        )
        self.assertEqual(classify_page(home, "https://www.cleartrip.com/"), "home")
        self.assertFalse(ground_unknown_plan(home, "", {}).get("handled"))
        real_pay = ground_unknown_plan(
            "Pay to complete your booking Enter card number Payment options", "", {}
        )
        self.assertEqual(real_pay.get("action"), "stop")
        self.assertIn("payment page", format_unknown_plan(real_pay).lower())
        checkout_src = _inspect.getsource(run_checkout_agent)
        self.assertIn("leave_site_error", checkout_src)
        self.assertIn("_reopen_cleartrip_results", checkout_src)
        self.assertIn("error_rebook:results", _inspect.getsource(_reopen_cleartrip_results))
        self.assertIn('left in {"error", "home", "cookie", "other"}', checkout_src)
        self.assertIn('outcome in {"denied", "error"}', _inspect.getsource(_confirm_fare))
        import inspect

        from app.kernel_otp import begin_login, kernel_failure_line

        src = inspect.getsource(begin_login)
        self.assertIn("dead_error", src)
        self.assertLess(src.index("dead_error = True"), src.index("is_resume_booking_url"))
        self.assertIn("timeout=90_000", src)
        self.assertIn("error_retry", src)
        self.assertIn(
            "browser didn't connect",
            kernel_failure_line("Cleartrip", "TimeoutError: BrowserType.connect_over_cdp: Timeout 180000ms exceeded"),
        )

    def test_yes_after_go_to_home_arms_pending_tap(self) -> None:
        from app.travel_agent import extract_user_ask

        page = "Sorry our system is acting up Go to home"
        ask = extract_user_ask(
            "yes",
            {
                "pending_step": {"tap": "Go to home", "page": page, "kind": "error"},
                "stuck_page": page,
            },
        )
        self.assertEqual(ask.get("tap"), "Go to home")
        self.assertEqual(ask.get("goal"), "forward")

    def test_naming_ixigo_on_cleartrip_error_does_not_press_go_home(self) -> None:
        from app.turn_loop import align_action

        state = {
            "origin": "Guwahati",
            "destination": "Delhi",
            "depart": "2026-12-04",
            "seller": "cleartrip",
            "selected_hint": "10:45",
            "stuck_kind": "error",
            "stuck_page": "Sorry our system is acting up Go to home",
            "last_vision": {"kind": "error", "tap": "Go to home"},
            "chosen_tap": "Go to home",
        }
        turn = {
            "kind": "error",
            "intent": "status",
            "goal": "ask",
            "tap": "Go to home",
            "reply": "The site encountered an error, please tap Go to home to start fresh.",
        }
        plan = align_action("Ixigo 10:45", state, turn)
        self.assertEqual(plan["do"], "book")
        self.assertEqual(plan.get("tap") or "", "")
        self.assertNotIn("go to home", str(plan.get("reply") or "").lower())

    def test_yes_accepts_recommended_next_seller_after_error(self) -> None:
        from app.search import next_bookable_seller
        from app.turn_loop import align_action

        for current in ("cleartrip", "easemytrip", "ixigo"):
            nxt = next_bookable_seller(current)
            state = {
                "origin": "Guwahati",
                "destination": "Delhi",
                "depart": "2026-12-04",
                "seller": current,
                "selected_hint": "10:45",
                "stuck_kind": "error",
                "pending_seller_switch": nxt,
                "stuck_page": "Server error itinerary/failure Go to home",
                "last_vision": {"kind": "error", "tap": ""},
            }
            plan = align_action("yes", state)
            self.assertEqual(plan["do"], "book", current)
            self.assertEqual(plan.get("tap") or "", "", current)

    def test_error_reply_recommends_next_travel_site_without_auto_switch(self) -> None:
        from app.search import next_bookable_seller
        from app.travel_agent import _booking_page_reply, _seller_label

        for current in ("cleartrip", "ixigo", "easemytrip"):
            state = {
                "origin": "Guwahati",
                "destination": "Delhi",
                "depart": "2026-12-04",
                "seller": current,
                "selected_hint": "10:45",
            }
            spoken = _booking_page_reply(
                state,
                "10:45",
                "Server error",
                type(
                    "S",
                    (),
                    {
                        "page": type(
                            "P",
                            (),
                            {"url": f"https://www.{current}.com/flights/itinerary/failure"},
                        )()
                    },
                )(),
            )
            nxt = next_bookable_seller(current)
            self.assertEqual(state.get("pending_seller_switch"), nxt, current)
            self.assertIn(_seller_label(nxt).lower(), spoken.lower(), spoken)
            self.assertRegex(spoken.lower(), r"won.?t switch unless|reply yes")

    def test_failure_url_is_not_a_resume_booking_link(self) -> None:
        from app.kernel_otp import is_resume_booking_url

        self.assertFalse(
            is_resume_booking_url(
                "https://www.cleartrip.com/flights/results/itinerary/failure",
                "cleartrip",
            )
        )
        self.assertTrue(
            is_resume_booking_url(
                "https://www.cleartrip.com/flights/itinerary/abc123/info",
                "cleartrip",
            )
        )

    def test_guest_booking_does_not_promise_otp_up_front(self) -> None:
        import inspect

        from app.travel_agent import handle_turn

        src = inspect.getsource(handle_turn)
        self.assertIn("kernel_awaiting_auth_mode", src)
        self.assertIn("book as guest", src.lower())
        self.assertIn('auth_mode == "guest"', src)
        self.assertNotIn("If you're signed out, reply with the", src)

    def test_cleartrip_create_exposes_itinerary_url(self) -> None:
        from app.page_agent import (
            _js_continue,
            _safe_continue,
            cleartrip_itinerary_url,
            classify_page,
            itinerary_id_from_create,
            open_cleartrip_itinerary,
            run_checkout_agent,
        )
        import inspect

        body = '{"responseCode":200,"success":true,"data":{"itineraryId":"NIAH10abc"}}'
        self.assertEqual(itinerary_id_from_create(body=body), "NIAH10abc")
        self.assertEqual(
            itinerary_id_from_create(payload={"data": {"itineraryId": "NIAH10xyz"}}),
            "NIAH10xyz",
        )
        self.assertEqual(
            cleartrip_itinerary_url("NIAH10abc"),
            "https://www.cleartrip.com/flights/itinerary/NIAH10abc/info",
        )
        self.assertEqual(
            cleartrip_itinerary_url("NIAH10abc", info=False),
            "https://www.cleartrip.com/flights/itinerary/NIAH10abc",
        )
        open_src = inspect.getsource(open_cleartrip_itinerary)
        self.assertIn("info=False", open_src)
        self.assertIn("info=True", open_src)
        src = inspect.getsource(__import__("app.page_agent", fromlist=["_confirm_fare"])._confirm_fare)
        self.assertIn("open_cleartrip_itinerary", src)
        self.assertIn("itinerary_id", src)
        self.assertIn("open_cleartrip_itinerary", inspect.getsource(open_cleartrip_itinerary))
        review = (
            "Review your itinerary Guwahati → New Delhi IndiGo 6E-6150 14:20 "
            "Make your journey fully flexible Free Date Change ₹329 Add Continue"
        )
        self.assertEqual(
            classify_page(review, "https://www.cleartrip.com/flights/itinerary/NIAH1"),
            "review",
        )
        checkout = inspect.getsource(run_checkout_agent)
        self.assertIn("cleartrip_review", checkout)
        self.assertIn("_js_continue", checkout)
        self.assertIn("_decline_cleartrip_protect", checkout)
        self.assertIn("ct_protect_decline", checkout)
        self.assertIn("_js_continue", inspect.getsource(_safe_continue))
        self.assertTrue(callable(_js_continue))
        from app.page_agent import _decline_cleartrip_protect

        self.assertIn("without trip protect", inspect.getsource(_decline_cleartrip_protect).lower())

    def test_guest_cleartrip_error_stays_on_cleartrip(self) -> None:
        from app.travel_agent import _booking_page_reply
        from app.turn_loop import align_action

        state = {
            "origin": "Guwahati",
            "destination": "Delhi",
            "depart": "2026-12-04",
            "seller": "cleartrip",
            "selected_hint": "2:20",
            "kernel_auth_mode": "guest",
        }
        spoken = _booking_page_reply(
            state,
            "2:20",
            "Sorry our servers are stumped with your request. Please check your url. Home",
            type(
                "S",
                (),
                {"page": type("P", (), {"url": "https://www.cleartrip.com/flights/itinerary/failure"})()},
            )(),
        )
        self.assertNotIn("EaseMyTrip", spoken)
        self.assertNotIn("Ixigo", spoken)
        self.assertNotIn("server error", spoken.lower())
        self.assertNotIn("servers are stumped", spoken.lower())
        self.assertIsNone(state.get("pending_seller_switch"))
        self.assertIn("reopen", spoken.lower())
        self.assertIn("cleartrip", spoken.lower())

        state["stuck_kind"] = "error"
        state["stuck_page"] = "Sorry our servers are stumped Home"
        plan = align_action("go on", state)
        self.assertEqual(plan["do"], "book")
        self.assertEqual(plan.get("tap") or "", "")

    def test_payment_page_allows_other_site_login_or_price_check(self) -> None:
        from app.travel_agent import _booking_seller_id, _coerce_kind, infer_intent, resume_booking_url
        from app.turn_loop import align_action

        pay = {
            "origin": "Guwahati",
            "destination": "Delhi",
            "depart": "2026-12-04",
            "seller": "cleartrip",
            "selected_hint": "6:20",
            "stage": "payment",
            "stuck_kind": "pay",
            "kernel_auth_mode": "guest",
            "stuck_page": "Pay to complete your booking Scan QR code Pay now ₹10,039",
            "last_vision": {"kind": "pay", "what": "payment page", "tap": "", "seller": "cleartrip"},
        }
        self.assertEqual(align_action("yes", dict(pay))["do"], "pay_ask")
        ixigo = align_action("6:20 PM on Ixigo", dict(pay))
        self.assertEqual(ixigo["do"], "book")
        self.assertEqual(ixigo.get("tap") or "", "")
        logged = dict(pay)
        plan_login = align_action("I'd like to login on Cleartrip", logged)
        self.assertEqual(plan_login["do"], "book")
        self.assertEqual(logged.get("kernel_auth_mode"), "login")
        price = align_action("what's the price on Ixigo", dict(pay))
        self.assertEqual(price["do"], "search")
        # Intent first: fare-compare wording must leave pay — never pay_ask/pay_stop.
        compare = align_action(
            "I would like to check all the fares for all the different sites in cleartrip",
            dict(pay),
        )
        self.assertEqual(compare["do"], "search")
        self.assertTrue(compare.get("aligned", True))
        self.assertEqual(infer_intent(pay, "I'd like to login"), "login")
        from app.turn_loop import declines_login, wants_login

        self.assertTrue(wants_login("I'd like to login"))
        self.assertTrue(declines_login("no need for login click continue"))
        self.assertFalse(wants_login("no need for login click continue"))
        guest_again = {
            **pay,
            "stage": "booking",
            "stuck_kind": "review",
            "kernel_auth_mode": "login",
            "stuck_page": "Fare Summary Total Amount Continue Log in/Sign up Review & Traveller Details",
        }
        plan_guest = align_action("no need for login click continue", guest_again)
        self.assertEqual(plan_guest["do"], "book")
        self.assertEqual(guest_again.get("kernel_auth_mode"), "guest")
        from app.page_agent import classify_page

        self.assertEqual(
            classify_page(
                "Log in/Sign up Fare Summary Total Amount ₹9,813 Continue Review & Traveller Details Refund on Cancellation",
                "https://www.ixigo.com/flight/booking/1",
            ),
            "review",
        )
        self.assertEqual(infer_intent(pay, "how much on ixigo"), "search")
        self.assertEqual(_coerce_kind("6:20 on Ixigo", "select", pay), "book")
        self.assertEqual(_coerce_kind("price on EaseMyTrip", "chat", pay), "search")

        # After "For the 6:20 on Ixigo…", Login must stay on Ixigo — not Cleartrip pay.
        awaiting = {
            **pay,
            "seller": "ixigo",
            "preferred_seller": "ixigo",
            "kernel_seller": "ixigo",
            "kernel_awaiting_auth_mode": True,
            "kernel_auth_mode": "",
            "live": {
                "url": "https://www.cleartrip.com/flights/itinerary/abc/payment",
                "host": "cleartrip.com",
                "stage": "payment",
            },
        }
        self.assertEqual(_booking_seller_id(awaiting, "Login"), "ixigo")
        self.assertEqual(_booking_seller_id(awaiting, "guest"), "ixigo")
        login_state = {
            **awaiting,
            "kernel_auth_mode": "login",
            "kernel_awaiting_auth_mode": False,
            "stage": "booking",
        }
        self.assertEqual(
            resume_booking_url(login_state, "Login"),
            "",
            "login must not resume a Cleartrip payment URL",
        )

    def test_kernel_site_error_reopens_fresh_browser(self) -> None:
        import inspect

        from app.kernel_otp import begin_login, _open_browser
        from app.page_agent import leave_site_error, site_error_page
        from app.travel_agent import _kernel_result_is_site_error, _run_kernel_login

        stumped = (
            "Sorry our servers are stumped with your request. Please check your url. "
            "Looks like you've entered a wrong url Home"
        )
        self.assertTrue(site_error_page(stumped, "https://www.cleartrip.com/flights/itinerary/x"))
        self.assertIn("Home", inspect.getsource(leave_site_error))
        src = inspect.getsource(begin_login)
        self.assertIn("fresh_browser", src)
        self.assertIn("site_error_recoverable", src)
        self.assertIn("ephemeral", inspect.getsource(_open_browser))
        self.assertTrue(
            _kernel_result_is_site_error(
                {
                    "status": "error",
                    "detail": "site_error_recoverable",
                    "page": stumped,
                    "url": "https://www.cleartrip.com/flights/itinerary/failure",
                }
            )
        )
        run_src = inspect.getsource(_run_kernel_login)
        self.assertIn('fresh_browser=bool(loaded.get("require_otp")) or attempt > 0', run_src)
        self.assertIn("_kernel_result_is_site_error", run_src)
        self.assertIn("sellers_to_try", run_src)
        self.assertIn('alt = "ixigo"', run_src)
        self.assertIn("Never attach the stumped", run_src)
        self.assertIn("Still opening the", run_src)
        self.assertIn("review your itinerary", run_src)
        self.assertIn('chosen_tap="Continue"', run_src)
        self.assertNotIn("system error for the", run_src)

    def test_cleartrip_one_step_checkout_continue_clicks(self) -> None:
        from playwright.sync_api import sync_playwright

        from app.page_agent import _js_continue
        from app.step_read import forward_tap

        page_text = (
            "Review your itinerary Guwahati New Delhi Continue with 1-step checkout "
            "Secure your trip Fare summary"
        )
        self.assertEqual(forward_tap(page_text), "Continue with 1-step checkout")
        html = """<!doctype html><html><body>
        <h1>Review your itinerary</h1>
        <button id="go" style="width:260px;height:48px">Continue with 1-step checkout</button>
        <script>
          const btn = document.getElementById('go');
          btn.__reactFiber$test = {
            memoizedProps: {
              onClick: () => {
                document.body.innerHTML = '<h1>Add contact details</h1><p>Email address</p>';
              }
            }
          };
        </script></body></html>"""
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.set_content(html)
            moved = _js_continue(page)
            text = page.inner_text("body")
            browser.close()
        self.assertTrue(moved)
        self.assertIn("Add contact details", text)

    def test_login_mode_error_asks_for_fresh_otp_not_seller_switch(self) -> None:
        from app.travel_agent import _booking_page_reply
        from app.turn_loop import align_action

        state = {
            "origin": "Guwahati",
            "destination": "Delhi",
            "depart": "2026-12-04",
            "seller": "cleartrip",
            "selected_hint": "2:20",
            "kernel_auth_mode": "login",
        }
        spoken = _booking_page_reply(
            state,
            "2:20",
            "Sorry our servers are stumped Home",
            type(
                "S",
                (),
                {"page": type("P", (), {"url": "https://www.cleartrip.com/flights/itinerary/failure"})()},
            )(),
        )
        self.assertNotIn("EaseMyTrip", spoken)
        self.assertNotIn("server error", spoken.lower())
        self.assertIn("otp", spoken.lower())
        self.assertIsNone(state.get("pending_seller_switch"))
        state["stuck_kind"] = "error"
        state["stuck_page"] = "servers are stumped Home"
        plan = align_action("2:20", state)
        self.assertEqual(plan["do"], "book")
        self.assertEqual(plan.get("tap") or "", "")

    def test_kernel_success_is_the_live_page(self) -> None:
        import inspect

        from app.kernel_otp import after_checkout_status, begin_login, should_hunt_login
        from app.page_agent import classify_page

        extras = (
            "6E 2092 10:45 Fare summary Add ons ₹449 Extra baggage "
            "Make your journey fully flexible Travel insurance Continue Log in"
        )
        shown = classify_page(extras, "https://www.cleartrip.com/flights/itinerary/1")
        self.assertNotEqual(shown, "login")
        self.assertFalse(should_hunt_login(shown))
        self.assertFalse(should_hunt_login("other"))
        self.assertFalse(should_hunt_login("review"))
        self.assertTrue(should_hunt_login("login"))
        self.assertEqual(after_checkout_status(shown), "held")
        self.assertEqual(after_checkout_status("other"), "held")
        self.assertEqual(after_checkout_status("login"), "auth")
        self.assertEqual(after_checkout_status("error"), "dead")
        src = inspect.getsource(begin_login)
        self.assertIn("should_hunt_login", src)
        self.assertIn("after_checkout_status", src)
        hunt = src.split("if hint and checkout")[1].split("A saved profile")[0]
        self.assertIn("should_hunt_login", hunt)

    def test_one_gemini_turn_names_wish_not_pending_skip(self) -> None:
        import inspect

        from app.llm import converse
        from app.travel_agent import extract_user_ask, handle_turn
        from app.turn_loop import interpret_turn

        booking = {
            "stage": "booking",
            "stuck_kind": "addons",
            "stuck_page": "Choose add-ons Skip add-ons",
            "selected_hint": "18:20",
            "origin": "Guwahati",
            "destination": "Delhi",
            "depart": "2026-12-04",
            "seller": "cleartrip",
            "last_vision": {
                "kind": "upsell",
                "what": "optional Flexi extras with Continue",
                "recommend": "press Continue without buying extras",
                "tap": "Continue",
                "facts": ["Continue visible"],
            },
        }
        self.assertEqual(extract_user_ask("check and tell me what to do next?", booking).get("intent"), "status")
        turn = interpret_turn("check and tell me what to do next?", booking)
        self.assertEqual(turn["goal"], "ask")
        self.assertEqual(turn["intent"], "status")
        self.assertNotRegex(turn.get("reply") or "", r"Skip add-ons is on screen")
        go = interpret_turn("confirm the fligt", booking)
        self.assertEqual(go["intent"], "book")
        self.assertEqual(go["goal"], "forward")
        addons = {
            **booking,
            "stuck_kind": "addons",
            "last_vision": {"kind": "addons", "what": "seat map", "tap": "Skip add-ons", "facts": []},
            "stuck_page": "Choose add-ons Add seats Skip add-ons",
        }
        saved = interpret_turn("Sudipta Saikia, Male", addons)
        self.assertEqual(saved["intent"], "book")
        self.assertIn("Skip add-ons", saved.get("tap") or "")
        self.assertEqual(extract_user_ask("18:20", addons).get("tap"), "")
        src = inspect.getsource(handle_turn)
        self.assertIn("align_action", src)
        self.assertIn("_form_ask_if_needed", src)
        self.assertLess(src.index("align_action"), src.index("_form_ask_if_needed"))
        self.assertNotIn('kind not in {"details", "pay", "cancel", "pay_self"}', src)
        self.assertNotIn('Reply yes and I\'ll press {tap}', src)
        deliver = inspect.getsource(__import__("app.travel_agent", fromlist=["_deliver_signed_in_page"])._deliver_signed_in_page)
        self.assertIn("_form_ask_if_needed", deliver)
        self.assertIn('"addons"', deliver)
        from app.turn_loop import align_action

        fresh = interpret_turn(
            "Ghy to Delhi 4th December",
            {"origin": "Guwahati", "destination": "Delhi", "depart": "2026-12-04"},
        )
        self.assertEqual(fresh.get("intent") or "search", "search")
        self.assertNotRegex((fresh.get("reply") or "").lower(), r"select your preferred")
        opened = align_action(
            "Ghy to Delhi 4th December",
            {"origin": "Guwahati", "destination": "Delhi", "depart": "2026-12-04"},
        )
        self.assertEqual(opened["do"], "search")
        skip = align_action("Sudipta Saikia, Male", addons)
        self.assertEqual(skip["do"], "book")
        self.assertIn("Skip add-ons", skip.get("tap") or "")
        src = inspect.getsource(handle_turn)
        self.assertIn("align_action", src)
        brain = inspect.getsource(converse)
        self.assertIn("gemini_chat", brain)
        self.assertNotIn("deepseek", brain.lower())

    def test_intent_facts_action_stay_aligned(self) -> None:
        import inspect

        from app.travel_agent import handle_turn
        from app.turn_loop import align_action, has_live_facts

        trip = {"origin": "Guwahati", "destination": "Delhi", "depart": "2026-12-04"}
        self.assertFalse(has_live_facts(trip))
        self.assertEqual(align_action("Ghy to Delhi 4th December", trip)["do"], "search")
        self.assertEqual(align_action("Hii", {})["do"], "ask_trip")
        results = {
            **trip,
            "last_vision": {"kind": "results", "what": "fare list", "tap": "", "facts": ["12:30"]},
            "selected_hint": "",
        }
        self.assertTrue(has_live_facts(results))
        pick = align_action("12:30", results)
        self.assertEqual(pick["do"], "book")
        addons = {
            **trip,
            "last_vision": {"kind": "addons", "what": "seat map", "tap": "Skip add-ons", "facts": []},
            "stuck_page": "Choose add-ons Skip add-ons",
        }
        skip = align_action("skip", addons)
        self.assertEqual(skip["do"], "book")
        self.assertIn("Skip add-ons", skip.get("tap") or "")
        nxt = align_action("what to do next?", addons)
        self.assertEqual(nxt["do"], "reply")
        src = inspect.getsource(handle_turn)
        self.assertIn("align_action", src)
        self.assertIn('do == "search"', src)
        self.assertIn('do == "book"', src)
        self.assertIn("last_vision", inspect.getsource(handle_turn).split("if changed:")[1][:400])
        self.assertIn("clear_checkout_memory", src)
        self.assertIn("release_saved_kernel", src)
        self.assertLess(src.index("release_saved_kernel("), src.index("clear_checkout_memory(state)"))
        self.assertIn("wants_fresh_search", src)
        self.assertEqual(align_action("let's start fresh", addons)["do"], "search")
        self.assertEqual(align_action("Skip add-ons", addons)["do"], "book")
        google_list = {
            **trip,
            "seller": "google",
            "stage": "results",
            "options": [{"airline": "IndiGo", "depart": "10:45", "price": "9,812"}],
        }
        self.assertTrue(has_live_facts(google_list))
        pick_google = align_action("10:45", google_list)
        self.assertEqual(pick_google["do"], "book")
        named = align_action("Cleartrip 10:45", google_list)
        self.assertEqual(named["do"], "book")
        from app.search import default_bookable_seller
        from app.travel_agent import _booking_seller_id, handle_turn as _ht

        self.assertEqual(_booking_seller_id(google_list, "10:45"), default_bookable_seller())
        self.assertEqual(_booking_seller_id(google_list, "Cleartrip 10:45"), "cleartrip")
        src = inspect.getsource(_ht)
        self.assertIn("remember_results_page", inspect.getsource(__import__("app.travel_agent", fromlist=["_publish_fares"])._publish_fares))
        self.assertIn('if kind == "search":', src)
        self.assertIn("_search_ack", src)
        self.assertRegex(src, r'if kind == "search":[\s\S]{0,2500}?_search_ack')

    def test_fare_list_memory_connects_sites(self) -> None:
        from app.search import default_bookable_seller, seller_can_book
        from app.travel_agent import _booking_seller_id, _options_reply, _seller_label
        from app.turn_loop import align_action, has_live_facts, remember_results_page

        self.assertFalse(seller_can_book("google"))
        self.assertTrue(seller_can_book("cleartrip"))
        state: dict = {"origin": "Guwahati", "destination": "Delhi", "depart": "2026-12-04"}
        remember_results_page(
            state,
            seller="google",
            options=[{"airline": "IndiGo", "depart": "10:45", "price": "9,812"}],
        )
        self.assertTrue(has_live_facts(state))
        self.assertEqual(state["last_vision"]["kind"], "results")
        self.assertFalse(state["last_vision"]["bookable"])
        self.assertEqual(align_action("10:45", state)["do"], "book")
        self.assertEqual(_booking_seller_id(state, "10:45"), default_bookable_seller())
        spoken = _options_reply(state, state["options"], seller="google")
        self.assertIn("Reply with a departure time", spoken)
        self.assertIn("Cleartrip", spoken)
        self.assertIn("Ixigo", spoken)
        self.assertNotIn("Checking Cleartrip", spoken)
        self.assertNotIn("Don't pick a site yet", spoken)
        self.assertNotRegex(spoken.lower(), r"where are you flying")
        # Quotes start only after a clock is chosen — not inside _publish_fares.
        import inspect

        from app.travel_agent import _publish_fares

        fares_src = inspect.getsource(_publish_fares)
        self.assertIn("Do not auto-quote", fares_src)
        self.assertNotIn('name="book-quotes"', fares_src)
        # Book-site quote formatter picks the cheapest bookable seller.
        from app.book_quotes import format_book_quotes, is_gau_del, price_for_hint, quote_sellers_for_route
        from app.search import indigo_results_url, named_seller, seller_can_book as can_book
        from app.travel_agent import _book_site_choice_reply, _needs_book_site_choice

        self.assertTrue(is_gau_del("Guwahati", "Delhi"))
        self.assertFalse(is_gau_del("Mumbai", "Delhi"))
        self.assertIn("indigo", quote_sellers_for_route("Guwahati", "Delhi"))
        self.assertNotIn("indigo", quote_sellers_for_route("Mumbai", "Delhi"))
        self.assertIn("GAU", indigo_results_url("Guwahati", "Delhi", "2026-12-04"))
        self.assertEqual(indigo_results_url("Mumbai", "Delhi", "2026-12-04"), "")
        self.assertFalse(can_book("indigo"))
        self.assertEqual(named_seller("check indigo"), "indigo")
        hit = price_for_hint(
            [{"depart": "10:45", "price_n": 9812}, {"depart": "18:20", "price_n": 7000}],
            "10:45",
        )
        self.assertEqual(hit["price_n"], 9812)
        spoken_q = format_book_quotes(
            [
                {"label": "Cleartrip", "price_n": 9950, "bookable": True},
                {"label": "Ixigo", "price_n": 9812, "bookable": True},
            ],
            hint="10:45",
            trip="Guwahati to Delhi on 4 Dec",
        )
        self.assertIn("Ixigo ₹9,812", spoken_q)
        self.assertIn("Cheapest to book is Ixigo", spoken_q)
        self.assertIn("Reply Ixigo to book", spoken_q)
        self.assertEqual(_seller_label(default_bookable_seller()), "Cleartrip")
        # Bare "2:20" after Google must not open Cleartrip — wait for site choice.
        waiting = {
            "origin": "Guwahati",
            "destination": "Delhi",
            "depart": "2026-12-04",
            "seller": "google",
            "awaiting_book_site": True,
            "selected_hint": "2:20",
            "options": state["options"],
        }
        self.assertTrue(_needs_book_site_choice(waiting, "2:20"))
        self.assertFalse(_needs_book_site_choice(waiting, "Ixigo"))
        hold = _book_site_choice_reply(waiting, "2:20")
        self.assertIn("Noted 2:20", hold)
        self.assertIn("which site", hold.lower())
        ready = {
            **waiting,
            "awaiting_book_site": False,
            "book_site_ready": True,
            "preferred_seller": "ixigo",
            "book_quotes": [
                {"label": "Cleartrip", "price_n": 10263, "bookable": True},
                {"label": "Ixigo", "price_n": 8021, "bookable": True},
            ],
        }
        self.assertTrue(_needs_book_site_choice(ready, "2:20"))
        self.assertFalse(_needs_book_site_choice(ready, "Ixigo"))
        ask = _book_site_choice_reply(ready, "2:20")
        self.assertIn("Reply Ixigo", ask)
        # Continue/Confirm on an open Ixigo fare sheet must click — not re-quote sellers.
        from app.travel_agent import _checkout_owns_turn, _commit_bookable_seller

        open_ixigo = {
            **waiting,
            "seller": "ixigo",
            "kernel_seller": "ixigo",
            "stage": "booking",
            "kernel_auth_mode": "guest",
            "stuck_kind": "fare_sheet",
            "stuck_page": "Fare Summary Total Amount ₹9,813 Continue",
            "chosen_tap": "Continue",
            "selected_hint": "10:45",
            "awaiting_book_site": True,
        }
        self.assertTrue(_checkout_owns_turn(open_ixigo, "continue"))
        self.assertFalse(_needs_book_site_choice(open_ixigo, "continue"))
        self.assertFalse(_needs_book_site_choice(open_ixigo, "confirm"))
        # Busy open sheet + continue must click, not soft-hold or re-quote.
        self.assertFalse(_hold_live(open_ixigo, "continue", busy=True, changed=False))
        # A second continue must not start another browser while the first still holds the lock.
        import time

        from app.travel_agent import _LOCK, _defer_live_browse

        self.assertFalse(_defer_live_browse({**open_ixigo, "busy_at": f"{time.time():.3f}"}))
        _LOCK.acquire()
        try:
            self.assertTrue(_defer_live_browse({**open_ixigo, "busy_at": f"{time.time():.3f}"}))
        finally:
            _LOCK.release()
        self.assertFalse(_needs_book_site_choice({**open_ixigo, "busy_at": "1"}, "continue"))
        cleared = dict(waiting)
        _commit_bookable_seller(cleared, "ixigo")
        self.assertEqual(cleared.get("seller"), "ixigo")
        self.assertNotIn("awaiting_book_site", cleared)

    def test_dead_itinerary_opens_fresh_fares_not_login(self) -> None:
        import inspect

        from app.kernel_otp import begin_login, keep_kernel_browser
        from app.page_agent import classify_page
        from app.turn_loop import clear_checkout_memory, wants_fresh_search

        self.assertTrue(wants_fresh_search("let's start fresh"))
        self.assertTrue(wants_fresh_search("search again"))
        self.assertFalse(wants_fresh_search("Skip add-ons"))
        stale = {
            "origin": "Guwahati",
            "destination": "Delhi",
            "depart": "2026-12-04",
            "live": {"url": "https://www.cleartrip.com/flights/itinerary/abc", "stage": "addons"},
            "last_vision": {"kind": "addons", "tap": "Skip add-ons"},
            "selected_hint": "18:20",
            "chosen_tap": "Skip add-ons",
            "kernel_session_id": "dead-cleartrip-tab",
            "kernel_seller": "cleartrip",
        }
        clear_checkout_memory(stale)
        self.assertNotIn("live", stale)
        self.assertNotIn("last_vision", stale)
        self.assertNotIn("chosen_tap", stale)
        self.assertNotIn("kernel_session_id", stale)
        self.assertNotIn("kernel_seller", stale)
        self.assertEqual(
            classify_page(
                "First time flying with us Popular destinations Pay to complete your booking",
                "https://www.cleartrip.com/",
            ),
            "home",
        )
        from app.page_agent import payment_sheet_open

        home = "First time flying with us Popular destinations Pay to complete your booking"
        self.assertFalse(payment_sheet_open(home, "pay"))
        # Contact + sidebar Payment options must never be treated as the pay sheet.
        contact = (
            "Add Contact details Please enter Mobile Number +91 Email address Continue "
            "Payment options Unlock price Base fare Taxes Apply coupon"
        )
        self.assertEqual(classify_page(contact, "https://www.cleartrip.com/flights/itinerary/x"), "other")
        self.assertFalse(payment_sheet_open(contact, "other"))
        self.assertFalse(payment_sheet_open(contact, "pay"))
        from app.travel_agent import infer_intent

        self.assertEqual(
            infer_intent(
                {
                    "stage": "booking",
                    "stuck_kind": "form",
                    "origin": "Guwahati",
                    "destination": "Delhi",
                    "depart": "2026-12-04",
                    "seller": "cleartrip",
                    "selected_hint": "2:20",
                },
                "8876233052",
            ),
            "details",
        )
        self.assertEqual(
            infer_intent(
                {"stage": "booking", "stuck_kind": "pay", "stuck_page": contact, "seller": "cleartrip"},
                "use 8876233052 for mobile",
            ),
            "details",
        )
        from app.turn_loop import align_action

        pay_lie = {
            "origin": "Guwahati",
            "destination": "Delhi",
            "depart": "2026-12-04",
            "seller": "cleartrip",
            "stage": "booking",
            "stuck_kind": "pay",
            "stuck_page": contact,
            "last_vision": {"kind": "pay", "what": "payment page"},
            "selected_hint": "2:20",
        }
        plan = align_action("8876233052", pay_lie)
        self.assertEqual(plan.get("do"), "details")
        self.assertEqual(plan.get("kind"), "details")
        sprint_src = inspect.getsource(
            __import__("app.page_agent", fromlist=["_cleartrip_guest_sprint"])._cleartrip_guest_sprint
        )
        self.assertIn("ct_phone", sprint_src)
        self.assertIn("please enter mobile number", sprint_src.lower())
        self.assertEqual(
            classify_page("Server error Search again", "https://www.cleartrip.com/flights/itinerary/failure"),
            "error",
        )
        self.assertTrue(keep_kernel_browser("held"))
        self.assertTrue(keep_kernel_browser("fares"))
        self.assertFalse(keep_kernel_browser("resume_lost"))
        src = inspect.getsource(begin_login)
        self.assertIn('skip_tap and stage in {"login", "otp"}', src)
        self.assertLess(src.index('stage == "error"'), src.index("ixigo_results_url"))

    def test_addons_yes_arms_skip_and_checkout_leaves(self) -> None:
        import inspect
        import re as _re

        from app.page_agent import _AIM_JS, _click_label, click_verified, deterministic_action, run_checkout_agent
        from app.step_read import accept_choice, decide_front

        page = "Choose add-ons Add seats No seat added Skip add-ons missed adding Meal(s) and Seat(s)"
        self.assertEqual(
            accept_choice("yes", {"tap": "Skip add-ons", "page": page, "kind": "addons"}),
            "Skip add-ons",
        )
        self.assertEqual(
            accept_choice("skip and next step", {"tap": "Skip add-ons", "page": page, "kind": "addons"}),
            "Skip add-ons",
        )
        self.assertIn("click_verified", inspect.getsource(_click_label))
        aim = inspect.getsource(click_verified)
        self.assertIn("page.mouse.click", aim)
        self.assertIn("_aim_mark", aim)
        self.assertIn("0.72", _AIM_JS)
        self.assertIn("role=dialog", _AIM_JS)
        self.assertLess(_AIM_JS.index("textOf(el) !== want"), _AIM_JS.index("innerWidth * 0.72"))
        self.assertEqual(deterministic_action("addons", hint="18:20", recovers=0)["target"], "Skip add-ons")
        decision = decide_front(page, "btn[ ] Skip add-ons", seller="cleartrip", hint="18:20")
        self.assertEqual(decision.get("action"), "click")
        self.assertRegex(decision.get("tap") or "", _re.compile(r"skip add", _re.I))
        src = inspect.getsource(run_checkout_agent)
        self.assertIn("_leave_addons", src)

    def test_itinerary_failure_url_stays_error_not_results(self) -> None:
        self.assertEqual(
            classify_page(
                "Something went wrong",
                "https://www.cleartrip.com/flights/results/itinerary/failure",
            ),
            "error",
        )

    def test_sold_out_modal_is_unavailable_not_review(self) -> None:
        from app.page_agent import flight_unavailable

        hay = (
            "Flight is no longer available. Please search again! Search Again "
            "Fare Summary Base Fare ₹7,893 Total Amount ₹9,465 Review & Traveller Details"
        )
        self.assertTrue(flight_unavailable(hay))
        self.assertEqual(classify_page(hay, "https://www.ixigo.com/flight/booking/1"), "unavailable")
        self.assertEqual(
            deterministic_action("unavailable", hint="12:30", recovers=0)["action"],
            "stuck",
        )

    def test_toast_on_fare_list_does_not_replace_page(self) -> None:
        from app.page_agent import apply_front_text, nearest_depart_hint

        body = (
            "IndiGo 6E 123 18:25 21:05 2h 40m Non-stop Book ₹10,283 "
            "IndiGo 6E 456 09:15 12:00 2h 45m Non-stop Book ₹10,283"
        )
        front = apply_front_text(
            body,
            {"overlay": True, "text": "No flight data available.", "controls": ""},
        )
        self.assertIn("Book", front)
        self.assertIn("₹10,283", front)
        self.assertEqual(nearest_depart_hint(body, "18:20"), "18:25")
        # Arrival 13:15 on a 10:20 card must not satisfy hint 1:15.
        afternoon = (
            "Air India Express I4 2229 10:20 13:15 Book ₹10,263 "
            "Air India Express 17:40 20:45 Book ₹10,263"
        )
        self.assertEqual(nearest_depart_hint(afternoon, "1:15"), "1:15")
        self.assertNotEqual(nearest_depart_hint(afternoon, "1:15"), "13:15")
        self.assertEqual(nearest_depart_hint(afternoon, "10:15"), "10:20")
        morning = "Akasa 09:10 19:10 Book ₹8,021 IndiGo 18:20 21:05 Book ₹10,263"
        self.assertEqual(nearest_depart_hint(morning, "9:40"), "9:40")

    def test_book_tap_skips_stale_itinerary_resume(self) -> None:
        """go on + Book must open the fare list, not a leftover Review Details URL."""
        import inspect

        from app import kernel_otp
        from app.page_agent import extract_trip_facts, verify_trip_facts

        src = inspect.getsource(kernel_otp.begin_login)
        self.assertIn("book_tap", src)
        self.assertLess(src.index("if book_tap:"), src.index("is_resume_booking_url(resume_url, seller)"))
        self.assertIn("resume_url = \"\"", src)
        checkout = inspect.getsource(
            __import__("app.page_agent", fromlist=["run_checkout_agent"]).run_checkout_agent
        )
        self.assertIn("_stop_wrong_itinerary", checkout)
        wrong = (
            "Fare Summary Total Amount ₹9,863 Review & Traveller Details "
            "Air-India Express | IX 1036 Fri, 04 Dec 20:15 GAU - Guwahati "
            "23:25 DEL - New Delhi Adult 1 Please select your title Continue"
        )
        facts = extract_trip_facts(wrong, "https://www.ixigo.com/flight/booking/x?fareKey=GAU-DEL-IX1036")
        ok, problems = verify_trip_facts(facts, expect_hint="1:15", expect_origin="GAU", expect_dest="DEL")
        self.assertFalse(ok)
        self.assertTrue(any("depart=" in p for p in problems))

    def test_results_reply_recommends_not_button_ask(self) -> None:
        from app.travel_agent import _booking_page_reply

        page = (
            "Guwahati to New Delhi IndiGo 6E5342 18:25 2h 40m Non-stop 21:05 Book ₹10,283 "
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
            page,
            type("S", (), {"page": None})(),
        )
        self.assertNotIn("exact button", spoken.lower())
        self.assertTrue("recommend" in spoken.lower() or "book the" in spoken.lower())

    def test_filled_path_blocked_when_flight_unavailable(self) -> None:
        from app.travel_agent import _booking_page_reply

        page = "Flight is no longer available. Please search again! Search Again Fare Summary ₹9,465"
        spoken = _booking_page_reply(
            {
                "origin": "Guwahati",
                "destination": "Delhi",
                "depart": "2026-11-04",
                "seller": "ixigo",
                "selected_hint": "12:30",
                "form_filled": True,
            },
            "12:30",
            page,
            type("S", (), {"page": None})(),
        )
        self.assertIn("no longer available", spoken.lower())
        self.assertNotIn("Filled your details", spoken)

    def test_rejected_pincode_is_not_a_filled_contact(self) -> None:
        """A red 'valid pincode' error is a blocker. A new pin is typed; the old one is not success."""
        from app.page_agent import contact_report
        from app.step_read import decide_sheet, format_sheet_move
        from app.travel_agent import absorb_details

        page = (
            "Billing Address Pin code 537031 Please enter a valid pincode "
            "Address By lane 14 City Jorhat State Assam Continue"
        )
        same = decide_sheet(page, {"pincode": "537031", "email": "a@b.co", "city": "Jorhat", "billing_state": "Assam"})
        self.assertEqual(same.get("action"), "ask")
        self.assertIn("a valid pincode", same.get("missing") or [])
        what, _why, nxt = format_sheet_move(same, seller="Ixigo", which="9:10")
        self.assertIn("6-digit", nxt)
        self.assertNotIn("typed the contact", nxt.lower())

        fresh = decide_sheet(page, {"pincode": "785001", "email": "a@b.co"})
        self.assertEqual(fresh.get("action"), "fill")
        self.assertIn("pincode", fresh.get("fills") or [])

        empty = decide_sheet(
            "Billing Address Pin code Address City State Continue",
            {"email": "a@b.co"},
        )
        self.assertEqual(empty.get("action"), "ask")
        self.assertIn("pincode", empty.get("missing") or [])

        saved = absorb_details({"phone": "+919999000001"}, "Pin code : 785001")
        self.assertEqual(saved.get("pincode"), "785001")
        self.assertNotEqual(saved.get("phone"), "785001")

        spoken = contact_report("email:ok | phone:ok | pincode:ok", page)
        self.assertIn("not valid", spoken.lower())
        self.assertIn("incorrect", spoken.lower())
        self.assertNotIn("typed the contact details", spoken.lower())
        asked = contact_report("email:ok | phone:ok", page)
        self.assertIn("rejected the pin", asked.lower())
        # A Continue click that did not leave Review Details is not "typed the contact".
        stuck = (
            "Review & Traveller Details Billing Address Pin code 785001 "
            "I don't want Free Cancellation Continue"
        )
        blocked = contact_report(
            "email:ok | phone:none | optout_miss | user_tap_upsell_continue:0",
            stuck,
        )
        self.assertIn("free cancellation", blocked.lower())
        self.assertNotIn("typed the contact details", blocked.lower())

    def test_billing_pin_digits_are_not_login_otp(self) -> None:
        """Bare 6 digits on a rejected pincode ask fill billing — they are not an OTP."""
        from unittest.mock import patch

        from app.travel_agent import _capture_private, _needs_billing_pin, absorb_details, handle_turn
        from app.turn_loop import align_action

        page = (
            "Billing Address Pin code 537031 Please enter a valid pincode "
            "Address By lane 14 City Jorhat State Assam Continue"
        )
        state = {
            "origin": "Guwahati",
            "destination": "Delhi",
            "depart": "2026-12-04",
            "seller": "ixigo",
            "kernel_seller": "ixigo",
            "stage": "booking",
            "live": {"host": "www.ixigo.com", "prices": True},
            "email": "a@b.co",
            "phone": "+919999000001",
            "stuck_page": page,
            "stuck_kind": "form",
            "last_vision": {"kind": "form", "errors": ["Please enter a valid pincode"], "fields": ["Pin code"]},
            "selected_hint": "9:10",
        }
        self.assertTrue(_needs_billing_pin(state))
        self.assertEqual(_capture_private("919999000001", "785001", dict(state)), "")
        # Mid-booking without a pin ask must also not invent an OTP from bare digits.
        self.assertEqual(_capture_private("919999000001", "785001", {"stage": "booking"}), "")
        saved = absorb_details(dict(state), "785001")
        self.assertEqual(saved.get("pincode"), "785001")
        plan = align_action("785001", state)
        self.assertEqual(plan.get("do"), "details")

        with (
            patch("app.travel_agent._run_browse") as browse,
            patch("app.travel_agent.db") as fake_db,
            patch("app.travel_agent._save"),
            patch("app.travel_agent.remember_inbound"),
            patch("app.travel_agent.get_settings") as settings,
            patch("app.travel_agent.resolve_intent", return_value=("details", "")),
            patch("app.kernel_otp.kernel_configured", return_value=True),
        ):
            settings.return_value = type("S", (), {"gemini_api_key": "x", "llm_ready": True})()
            fake_db.add_message = lambda *a, **k: None
            reply = handle_turn("919999000001", "785001", dict(state), [])
        spoken = str(reply.text or "")
        self.assertNotIn("OTP", spoken)
        self.assertTrue(browse.called)

        # Real login OTP wait still captures digits when billing is not open.
        otp_state = {"stage": "login", "kernel_awaiting_otp": True}
        note = _capture_private("919999000001", "529779", otp_state)
        self.assertIn("OTP", note)

    def test_ixigo_saved_traveller_not_unknown_form_sheet(self) -> None:
        """Review Details with Mr/Ms + Contact Details stays traveller/review — not unknown other."""
        import inspect

        from app.page_agent import classify_page, deterministic_action
        from app.step_read import classify_sheet, form_sheet, is_saved_traveller_sheet

        saved = (
            "Review Details Traveller Details Mr Sudipta Saikia Adult "
            "Ms Someone Else Contact Details Email Mobile Number +91 Continue"
        )
        self.assertTrue(is_saved_traveller_sheet(saved))
        self.assertFalse(form_sheet(saved))
        self.assertEqual(classify_sheet(saved), "traveller")
        stage = classify_page(saved, "https://www.ixigo.com/")
        self.assertEqual(stage, "review")
        self.assertEqual(
            deterministic_action(stage, hint="9:10", recovers=0, url="https://www.ixigo.com/"),
            {"action": "review_continue", "target": "", "value": ""},
        )
        # Pure contact sheet (no saved names) still uses the form_sheet fill path.
        contact_only = (
            "Add Contact details +91 Email address Mobile number Continue Review traveller details"
        )
        self.assertTrue(form_sheet(contact_only))
        checkout = inspect.getsource(
            __import__("app.page_agent", fromlist=["run_checkout_agent"]).run_checkout_agent
        )
        self.assertIn('tap_pending in {"__select_traveller__", "__pick_traveller__"}', checkout)
        self.assertLess(
            checkout.index('tap_pending in {"__select_traveller__", "__pick_traveller__"}'),
            checkout.index('sheet_move.get("action") == "ask" and sheet_move.get("missing")'),
        )
        self.assertIn("not _saved_travellers(text)", checkout)
        self.assertIn("is_saved_traveller_sheet(hay) or _traveller_form_open(hay)", inspect.getsource(
            __import__("app.step_read", fromlist=["form_sheet"]).form_sheet
        ))
        pick_src = inspect.getsource(
            __import__("app.page_agent", fromlist=["_select_saved_traveller"])._select_saved_traveller
        )
        self.assertIn("scrollIntoView", pick_src)
        self.assertIn("input[type=checkbox]", pick_src)
        self.assertNotIn("input[type=radio]", pick_src.split("querySelectorAll")[1][:120])
        opt = inspect.getsource(
            __import__("app.page_agent", fromlist=["_ixigo_opt_out_cancellation"])._ixigo_opt_out_cancellation
        )
        self.assertIn("optout_mouse", opt)
        self.assertIn("page.mouse.click", opt)
        prep = inspect.getsource(
            __import__("app.page_agent", fromlist=["_ixigo_prepare_review"])._ixigo_prepare_review
        )
        self.assertIn("_ixigo_opt_out_cancellation", prep)
        self.assertIn("_ensure_review_traveller", prep)
        checkout = inspect.getsource(
            __import__("app.page_agent", fromlist=["run_checkout_agent"]).run_checkout_agent
        )
        self.assertIn("_ixigo_prepare_review(page, profile, log)", checkout)
        self.assertIn("ixigo_prepare:after_fill", checkout)

    def test_continue_on_assured_traveller_page_declines_not_reticks(self) -> None:
        """Bare continue on Review Details with Assured radios declines — never re-arms pick."""
        from unittest.mock import patch

        from app.step_read import _assured_radio_gate, classify_sheet
        from app.turn_loop import align_action

        page = (
            "Review Details Add Free Cancellation "
            "Assured Free Cancellation Assured Flex "
            "I don't want Free Cancellation. Pay fee if you cancel "
            "Traveller Details Choose from the saved list "
            "Mr Sudipta Saikia Mr Sudipta Saikia Continue"
        )
        self.assertTrue(_assured_radio_gate(page))
        self.assertEqual(classify_sheet(page), "upsell")
        # Empty Adult 1 + Assured radios → traveller owns the sheet (fill first).
        blank_adult = (
            "Review Details Add Free Cancellation "
            "Free Cancellation + Rescheduling "
            "I don't want Free Cancellation. Pay ₹3,999 fee if you cancel or reschedule "
            "Traveller Details Choose from the saved list Mr Sudipta Saikia "
            "Adult 1 Please select your title First & Middle Name Last Name Continue"
        )
        self.assertTrue(_assured_radio_gate(blank_adult))
        self.assertEqual(classify_sheet(blank_adult), "traveller")
        # Newer Ixigo cards (no Assured Flex wording) still gate Continue.
        from app.step_read import upsell_choice_labels

        modern = (
            "Add Free Cancellation to your trip Most Popular "
            "Free Cancellation @₹1,179/traveller "
            "Free Cancellation + Rescheduling @₹1,479/traveller "
            "I don't want Free Cancellation. Pay ₹3,999 fee if you cancel or reschedule "
            "Traveller Details Choose from the saved list Mr Sudipta Saikia Continue"
        )
        self.assertTrue(_assured_radio_gate(modern))
        self.assertEqual(classify_sheet(modern), "upsell")
        labels = upsell_choice_labels(modern)
        self.assertTrue(any(re.search(r"don.?t want", item, re.I) for item in labels))
        self.assertTrue(any(re.search(r"free cancellation", item, re.I) and not re.search(r"don.?t want", item, re.I) for item in labels))
        state = {
            "origin": "Guwahati",
            "destination": "Delhi",
            "depart": "2026-12-04",
            "seller": "ixigo",
            "selected_hint": "9:10",
            "stage": "booking",
            "stuck_kind": "passenger",
            "stuck_page": page,
            "name": "Sudipta Saikia",
            "chosen_tap": "__select_traveller__",
            "last_vision": {"kind": "review"},
        }
        gemini_pick = {
            "goal": "pick_traveller",
            "intent": "book",
            "kind": "review",
            "what": "saved travellers",
            "why": "tick a name",
            "recommend": "tick Sudipta Saikia",
            "tap": "__select_traveller__",
            "reply": "I'll tick the name",
            "facts": [],
            "source": "gemini",
        }
        with patch("app.turn_loop.interpret_turn", return_value=gemini_pick):
            cont = align_action("continue", dict(state))
        self.assertEqual(cont["do"], "book")
        self.assertNotEqual(cont.get("tap"), "__select_traveller__")
        self.assertRegex(str(cont.get("tap") or ""), r"don.?t want Free Cancellation", re.I)
        with patch("app.turn_loop.interpret_turn", return_value=gemini_pick):
            pick = align_action("pick Mr Sudipta Saikia", dict(state))
        self.assertEqual(pick.get("tap"), "__select_traveller__")

    def test_pick_saved_traveller_not_contact_fill(self) -> None:
        from unittest.mock import patch

        from app.travel_agent import (
            extract_user_ask,
            resolve_intent,
            wants_delete_traveller,
            wants_pick_saved_traveller,
        )
        from app.turn_loop import align_action

        self.assertTrue(wants_pick_saved_traveller("click one of the names"))
        self.assertTrue(wants_pick_saved_traveller("pick one"))
        self.assertTrue(wants_pick_saved_traveller("selecte traveller as sudipta saikia"))
        self.assertTrue(wants_pick_saved_traveller("select traveller as Sudipta Saikia"))
        self.assertTrue(wants_pick_saved_traveller("pick Mr Sudipta Saikia"))
        self.assertTrue(wants_delete_traveller("delete one of the profiles"))
        self.assertTrue(wants_delete_traveller("get rid of the duplicate profiles"))
        self.assertFalse(wants_pick_saved_traveller("8876233052"))
        from app.travel_agent import traveller_name_from_ask

        self.assertEqual(traveller_name_from_ask("selecte traveller as sudipta saikia"), "sudipta saikia")
        self.assertEqual(traveller_name_from_ask("pick traveller as Sudipta Saikia"), "Sudipta Saikia")
        self.assertEqual(traveller_name_from_ask("pick Mr Sudipta Saikia"), "Sudipta Saikia")
        state = {
            "stage": "booking",
            "stuck_kind": "form",
            "stuck_page": "Mr Sudhir Sethi Mr Sudhir Sethi Add New Traveller Contact Details Mobile",
            "seller": "ixigo",
            "selected_hint": "8:55",
            "origin": "Guwahati",
            "destination": "Delhi",
            "depart": "2026-12-04",
            "phone": "8876233052",
            "last_vision": {"kind": "form"},
        }
        asked = extract_user_ask("click one of the names", state)
        self.assertEqual(asked["tap"], "__select_traveller__")
        named_ask = extract_user_ask("selecte traveller as sudipta saikia", state)
        self.assertEqual(named_ask["tap"], "__select_traveller__")
        self.assertEqual(state.get("name"), "sudipta saikia")
        # Gemini goal drives action — even without regex-friendly wording.
        gemini_pick = {
            "goal": "pick_traveller",
            "intent": "book",
            "kind": "form",
            "what": "saved travellers",
            "why": "you asked to use an existing name",
            "recommend": "tick one saved traveller",
            "tap": "",
            "auto": "",
            "reply": "I'll use one of your saved travellers. Nothing paid.",
            "facts": [],
            "source": "gemini",
        }
        with patch("app.turn_loop.interpret_turn", return_value=gemini_pick):
            paraphrased = align_action("pick one", state)
        self.assertEqual(paraphrased["do"], "book")
        self.assertEqual(paraphrased["tap"], "__select_traveller__")
        with patch("app.turn_loop.interpret_turn", return_value=gemini_pick):
            whoever = align_action("just use whoever is already there", state)
        self.assertEqual(whoever["tap"], "__select_traveller__")
        gemini_ask = {
            "goal": "ask",
            "intent": "status",
            "kind": "form",
            "what": "traveller list",
            "why": "unclear",
            "recommend": "say which name",
            "tap": "",
            "reply": "Which saved name should I tick? Nothing paid.",
            "facts": [],
            "source": "gemini",
        }
        with patch("app.turn_loop.interpret_turn", return_value=gemini_ask):
            clarify = align_action("hmm what about that", state)
        self.assertEqual(clarify["do"], "reply")
        # Form sheet + saved phone must NOT become details without Gemini details goal.
        gemini_chatty = {
            "goal": "ask",
            "intent": "status",
            "kind": "form",
            "tap": "",
            "reply": "Say pick one or send a mobile. Nothing paid.",
            "facts": [],
            "source": "gemini",
        }
        with patch("app.turn_loop.interpret_turn", return_value=gemini_chatty):
            no_fill = align_action("ok?", state)
        self.assertNotEqual(no_fill["do"], "details")
        with patch(
            "app.travel_agent.llm.converse",
            return_value={
                "goal": "delete_traveller",
                "intent": "book",
                "wanted_tap": "__delete_traveller__",
                "reply": "Removing a duplicate profile.",
                "flight_hint": "",
            },
        ):
            kind, spoken = resolve_intent("clear out those extra names please", state, [])
        self.assertEqual(kind, "book")
        self.assertEqual(state.get("chosen_tap"), "__delete_traveller__")
        import inspect

        self.assertIn(
            "__select_traveller__",
            inspect.getsource(__import__("app.page_agent", fromlist=["run_checkout_agent"]).run_checkout_agent),
        )
        under = __import__("app.travel_agent", fromlist=["_UNDERSTAND"])._UNDERSTAND
        self.assertIn("goal=pick_traveller", under)
        self.assertIn("You OWN intent", under)

    def test_actionable_intent_always_maps_to_action(self) -> None:
        """Every actionable goal/message must do something this turn — never note-only."""
        from unittest.mock import patch

        from app.turn_loop import _TURN, _GOAL_DO, _align_ok, align_action, _message_implies_action

        self.assertIn("INTENT FIRST", _TURN)
        self.assertIn("SEGMENT", _TURN)
        self.assertIn("REPLY RULE", _TURN)
        self.assertTrue(_align_ok("search", "search"))
        self.assertFalse(_align_ok("search", "pay_ask"))
        self.assertTrue(_align_ok("pay", "pay_ask"))
        self.assertTrue(_align_ok("ask", "reply"))
        self.assertFalse(_align_ok("ask", "book"))
        self.assertIn("pay", _GOAL_DO)
        from app.turn_loop import _wants_clarify

        self.assertTrue(_message_implies_action("sudipta@example.com"))
        self.assertTrue(_message_implies_action("fill my email"))
        self.assertTrue(_message_implies_action("pick one"))
        self.assertTrue(_message_implies_action("continue"))
        self.assertTrue(_message_implies_action("check all the fares for all the different sites"))
        self.assertFalse(_message_implies_action("hmm what about that"))
        self.assertTrue(_wants_clarify("why is the convenience fee so high?"))
        self.assertTrue(_wants_clarify("is this refundable?"))
        self.assertFalse(_wants_clarify("continue"))
        self.assertFalse(_wants_clarify("pick one"))

        base = {
            "stage": "booking",
            "seller": "ixigo",
            "selected_hint": "8:55",
            "origin": "Guwahati",
            "destination": "Delhi",
            "depart": "2026-12-04",
            "email": "old@example.com",
            "phone": "8876233052",
            "stuck_page": "Review Details Contact Details Please enter email Mobile Continue",
            "last_vision": {"kind": "review"},
        }
        # Gemini wrongly "notes" an email → still details action.
        noted = {
            "goal": "ask",
            "intent": "status",
            "kind": "review",
            "reply": "I have noted your email. Nothing paid.",
            "tap": "",
            "facts": [],
            "source": "gemini",
        }
        with patch("app.turn_loop.interpret_turn", return_value=noted):
            plan = align_action("sudipta@example.com", base)
        self.assertEqual(plan["do"], "details")
        with patch("app.turn_loop.interpret_turn", return_value={**noted, "kind": "form"}):
            fill = align_action("fill my email", {**base, "last_vision": {"kind": "form"}})
        self.assertEqual(fill["do"], "details")

        # Forward / skip / pick / delete → book (never reply).
        for goal, tap, msg in (
            ("forward", "Continue", "go on"),
            ("skip", "Skip add-ons", "skip"),
            ("pick_traveller", "__select_traveller__", "pick one"),
            ("delete_traveller", "__delete_traveller__", "delete the duplicates"),
        ):
            turn = {
                "goal": goal,
                "intent": "book",
                "kind": "form" if "traveller" in goal else "addons",
                "tap": "" if "traveller" in goal else tap,
                "reply": "Okay.",
                "facts": [],
                "source": "gemini",
            }
            state = {
                **base,
                "stuck_kind": "form" if "traveller" in goal else "addons",
                "last_vision": {"kind": turn["kind"]},
                "stuck_page": (
                    "Mr Sudhir Sethi Add New Traveller Contact Details"
                    if "traveller" in goal
                    else "Choose add-ons Skip add-ons Continue"
                ),
            }
            with patch("app.turn_loop.interpret_turn", return_value=turn):
                out = align_action(msg, state)
            self.assertEqual(out["do"], "book", msg)
            if "traveller" in goal:
                self.assertEqual(out["tap"], tap)

        # Pure status / doubt stays reply — never forced into book/details/pay.
        ask = {
            "goal": "ask",
            "intent": "status",
            "kind": "form",
            "reply": "Which name should I tick?",
            "tap": "",
            "facts": [],
            "source": "gemini",
        }
        with patch("app.turn_loop.interpret_turn", return_value=ask):
            clarify = align_action("hmm what about that", {**base, "last_vision": {"kind": "form"}})
        self.assertEqual(clarify["do"], "reply")
        # Saved-traveller sheet: yes → Continue (not Confirm), and typo selecte still picks.
        saved_page = (
            "Review Details Traveller Details Adult 1 Mr Sudipta Saikia "
            "a@b.co 8876233052 Continue Confirm"
        )
        yes_turn = {
            "goal": "forward",
            "intent": "book",
            "kind": "passenger",
            "reply": "Continuing.",
            "tap": "",
            "facts": [],
            "source": "gemini",
        }
        passenger_state = {
            **base,
            "stuck_kind": "passenger",
            "stuck_page": saved_page,
            "email": "a@b.co",
            "phone": "8876233052",
            "name": "Sudipta Saikia",
            "last_vision": {"kind": "passenger"},
        }
        with patch("app.turn_loop.interpret_turn", return_value=yes_turn):
            yes_go = align_action("yes", passenger_state)
        self.assertEqual(yes_go["do"], "book")
        self.assertEqual(yes_go["tap"], "Continue")
        # Empty contact boxes → conscience fills first, does not invent Confirm.
        empty_contact = {
            **passenger_state,
            "stuck_page": (
                "Review Details Traveller Details Adult 1 Mr Sudipta Saikia "
                "Contact Details Please enter a valid email Mobile Continue Confirm"
            ),
        }
        with patch("app.turn_loop.interpret_turn", return_value=yes_turn):
            fill_first = align_action("yes", empty_contact)
        self.assertEqual(fill_first["do"], "details")
        pick_typo = {
            "goal": "pick_traveller",
            "intent": "book",
            "kind": "passenger",
            "reply": "Picking.",
            "tap": "",
            "facts": [],
            "source": "gemini",
        }
        with patch("app.turn_loop.interpret_turn", return_value=pick_typo):
            typo = align_action("selecte traveller as sudipta saikia", passenger_state)
        self.assertEqual(typo["do"], "book")
        self.assertEqual(typo["tap"], "__select_traveller__")
        doubt = {
            "goal": "forward",
            "intent": "book",
            "kind": "pay",
            "reply": "Continuing.",
            "tap": "Continue",
            "facts": [],
            "source": "gemini",
        }
        with patch("app.turn_loop.interpret_turn", return_value=doubt):
            fee = align_action(
                "why is the convenience fee so high?",
                {**base, "stage": "payment", "stuck_kind": "pay", "last_vision": {"kind": "pay"}},
            )
        self.assertEqual(fee["do"], "reply")
        self.assertEqual(fee["turn"]["goal"], "ask")

        # handle_turn details path must start browse (Typing…), not note-only.
        import inspect

        from app.travel_agent import _details_action_ack, handle_turn

        src = inspect.getsource(handle_turn)
        self.assertIn("_details_action_ack", src)
        self.assertIn('args=(phone, text, state.get("busy_at"), "details")', src)
        ack = _details_action_ack({**base, "seller": "ixigo"}, "sudipta@example.com")
        self.assertIn("Typing your email", ack)
        self.assertNotRegex(ack.lower(), r"\bnoted\b|\bsaved\b")

        # Bot payment: ask for UPI, or fill when UPI is sent — never ignore fare-compare.
        from app.travel_agent import _pay_ask_reply

        pay_state = {
            **base,
            "stage": "payment",
            "stuck_kind": "pay",
            "seller": "cleartrip",
            "last_vision": {"kind": "pay"},
            "stuck_page": "Pay to complete your booking UPI Pay now",
        }
        with patch(
            "app.turn_loop.interpret_turn",
            return_value={"goal": "pay", "intent": "pay", "kind": "pay", "reply": "", "tap": "", "facts": [], "source": "gemini"},
        ):
            ask = align_action("yes", pay_state)
        self.assertEqual(ask["do"], "pay_ask")
        ask_line = _pay_ask_reply(pay_state)
        self.assertIn("UPI", ask_line)
        with patch(
            "app.turn_loop.interpret_turn",
            return_value={"goal": "pay", "intent": "pay", "kind": "pay", "reply": "", "tap": "", "facts": [], "source": "gemini"},
        ):
            fill = align_action("pay with UPI name@ybl", pay_state)
        self.assertEqual(fill["do"], "pay")

    def test_checkout_brain_rails_contact_not_pay_and_ready_fill(self) -> None:
        from unittest.mock import patch

        from app.checkout_brain import decide_checkout, rules_brain

        contact = (
            "Add Contact details Please enter Mobile Number +91 Email address Continue "
            "Payment options Unlock price Base fare Taxes Apply coupon"
        )
        rules = rules_brain(
            contact,
            url="https://www.cleartrip.com/flights/itinerary/x",
            seller="Cleartrip",
            profile={"email": "a@b.com"},
        )
        self.assertEqual(rules["kind"], "form")
        self.assertEqual(rules["action"], "ask")
        self.assertNotEqual(rules["kind"], "pay")

        ready = "Add Contact details +91 Email address Mobile number Continue"
        ready_rules = rules_brain(
            ready,
            url="https://www.cleartrip.com/flights/itinerary/x",
            profile={"email": "a@b.com", "phone": "8638586171"},
        )
        self.assertEqual(ready_rules["action"], "fill")
        self.assertEqual(ready_rules["tap"], "Continue")

        fake_llm = {
            "kind": "form",
            "action": "ask",
            "tap": "",
            "recommend": "ask for name",
            "reply": "Please share your full name. Nothing paid.",
            "source": "llm",
        }
        with patch("app.checkout_brain.llm_brain", return_value=fake_llm):
            out = decide_checkout(
                ready,
                url="https://www.cleartrip.com/flights/itinerary/x",
                profile={"email": "a@b.com", "phone": "8638586171"},
                prefer_llm=True,
            )
        self.assertEqual(out["action"], "fill")
        self.assertEqual(out["tap"], "Continue")
        self.assertEqual(out["source"], "llm")
        self.assertIn("Nothing paid", out["reply"])

        fake_pay = {
            "kind": "pay",
            "action": "stop",
            "tap": "Pay now",
            "recommend": "pay",
            "reply": "Ready to pay. Nothing paid.",
            "source": "llm",
        }
        with patch("app.checkout_brain.llm_brain", return_value=fake_pay):
            clamped = decide_checkout(
                contact,
                url="https://www.cleartrip.com/flights/itinerary/x",
                profile={"email": "a@b.com"},
                prefer_llm=True,
            )
        self.assertEqual(clamped["kind"], "form")
        self.assertEqual(clamped["action"], "ask")
        self.assertEqual(clamped["tap"], "")

    def test_vision_agent_module_wires_checkout(self) -> None:
        import inspect

        from app import vision_agent
        from app.page_agent import (
            _clear_front_sheet,
            _pause_unknown,
            advance_forward_control,
            run_checkout_agent,
        )
        from app.step_read import decide_front, ground_tap, parse_control_labels

        self.assertTrue(callable(vision_agent.see_step))
        self.assertTrue(callable(vision_agent.classify_with_vision))
        self.assertTrue(callable(advance_forward_control))
        from app.page_agent import _click_confirm_only

        self.assertTrue(callable(_click_confirm_only))
        fwd_src = inspect.getsource(advance_forward_control)
        self.assertIn("_click_confirm_only", fwd_src)
        self.assertIn('kind == "name_confirm"', fwd_src)
        self.assertIn("_sheet_moved", fwd_src)
        self.assertIn('return ""', fwd_src)
        from app.page_agent import _decline_radio_checked, _ixigo_opt_out_cancellation

        self.assertIn("_decline_radio_checked", inspect.getsource(_ixigo_opt_out_cancellation))
        self.assertTrue(callable(_decline_radio_checked))
        self.assertTrue(callable(ground_tap))
        self.assertTrue(callable(parse_control_labels))
        src = inspect.getsource(run_checkout_agent)
        self.assertIn("classify_with_vision", src)
        self.assertIn("vision_enabled", src)
        self.assertIn("vision_shot_miss", src)
        self.assertIn("advance_forward_control", src)
        self.assertIn("image_path", inspect.signature(decide_front).parameters)
        clear_src = inspect.getsource(_clear_front_sheet)
        self.assertIn("image_path", clear_src)
        self.assertIn("advance_forward_control", clear_src)
        self.assertIn("image_path", inspect.getsource(_pause_unknown))
        vision_src = inspect.getsource(vision_agent.see_step)
        self.assertIn("controls", vision_src)
        self.assertIn("ground_tap", vision_src)
        self.assertIn('"controls":[]', vision_agent._VISION_READ)
        classify_src = inspect.getsource(vision_agent.classify_with_vision)
        self.assertIn('kind in {"review", "form", "upsell", "passenger"}', classify_src)
        self.assertTrue(Path("scripts/vision_step_smoke.py").is_file())

    def test_vision_controls_ground_clicks_and_protect_review(self) -> None:
        """Vision owns sheet kind + control allowlist; fares_visible cannot wipe review."""
        from unittest.mock import patch

        from app import vision_agent
        from app.step_read import accept_choice, ground_tap, parse_control_labels

        review = (
            "Review Details Fare Summary Base Fare ₹8,523 Total ₹9,425 "
            "Please ensure that your name matches your govt ID Confirm Edit Continue"
        )
        labels = parse_control_labels("btn[ ] Confirm\nbtn[ ] Edit\nbtn[ ] Continue\n" + review)
        self.assertIn("Confirm", labels)
        self.assertEqual(ground_tap("Confirm", labels, review, kind="review"), "Confirm")
        self.assertEqual(ground_tap("Confirm", ["Continue", "Edit"], "Continue Edit", kind="name_confirm"), "Continue")
        self.assertEqual(
            accept_choice(
                "yes",
                {
                    "tap": "Confirm",
                    "page": "Review Details Continue govt ID",
                    "kind": "name_confirm",
                    "controls": ["Continue", "Edit"],
                },
            ),
            "Continue",
        )
        fake = {
            "kind": "review",
            "what": "Review Details name check",
            "why": "confirm traveller name",
            "recommend": "press Confirm",
            "tap": "Confirm",
            "auto": True,
            "fares_visible": True,
            "sold_out": False,
            "toast_only": False,
            "controls": ["Confirm", "Edit"],
            "facts": ["Confirm visible"],
            "best_depart": "",
            "best_price": "",
        }
        with patch.object(vision_agent, "vision_enabled", return_value=True), patch(
            "app.llm.vision_converse", return_value=fake
        ):
            seen = vision_agent.see_step("/tmp/x.png", page_text=review)
        self.assertIsNotNone(seen)
        assert seen is not None
        self.assertEqual(seen["kind"], "review")
        self.assertFalse(seen["fares_visible"])
        self.assertEqual(seen["tap"], "Confirm")
        self.assertIn("Confirm", seen["controls"])

        with patch.object(vision_agent, "see_step", return_value=seen):
            mapped, again = vision_agent.classify_with_vision("results", "shot.png", page_text=review)
        self.assertEqual(mapped, "review")
        self.assertEqual((again or {}).get("kind"), "review")
        # Spend preferred is ignored; sheet allowlist still grounds a safe forward.
        self.assertEqual(ground_tap("Pay now", ["Confirm", "Continue"], review, kind="review"), "Confirm")

    def test_results_with_book_buttons_choose_the_time(self) -> None:
        action = deterministic_action("results", hint="1:15", recovers=0)
        self.assertEqual(action["action"], "click_time")
        self.assertEqual(action["target"], "1:15")

    def test_fare_sheet_clicks_continue_without_a_model(self) -> None:
        action = deterministic_action("fare_sheet", hint="1:15", recovers=0)
        self.assertEqual(action["action"], "click")
        self.assertEqual(action["target"], "Continue")

    def test_model_action_json_is_parsed(self) -> None:
        parsed = parse_action('Sure\n{"action":"click","target":"Search","value":""}')
        self.assertEqual(parsed["action"], "click")
        self.assertEqual(parsed["target"], "Search")
        self.assertIsNone(parse_action('{"action":"pay","target":"Pay now"}'))

    def test_itinerary_create_akamai_403_is_denied(self) -> None:
        body = "<HTML><TITLE>Access Denied</TITLE><H1>Access Denied</H1></HTML>"
        self.assertEqual(interpret_itinerary_create(403, body), "denied")

    def test_itinerary_create_200_is_ok(self) -> None:
        self.assertEqual(interpret_itinerary_create(200, '{"itineraryId":"x"}'), "ok")

    def test_itinerary_create_missing_is_unknown(self) -> None:
        self.assertEqual(interpret_itinerary_create(None, ""), "unknown")

    def test_ixigo_card_matches_departure_not_arrival(self) -> None:
        spice = (
            "Fastest SpiceJet SG762 12:30 GAU 2h 45m Non-stop 15:15 DEL "
            "₹8,992 Book Lock Price @₹309"
        )
        # Hint 1:15 → 13:15 must not hit this card via arrival 15:15.
        self.assertFalse(card_matches_depart_hint(spice, "1:15"))
        ix = (
            "Air India Express IX-1513 13:15 GAU 2h 50m Non-stop 16:05 DEL "
            "₹9,516 Book Lock Price @₹349"
        )
        self.assertTrue(card_matches_depart_hint(ix, "1:15"))
        # Duplicate Book on one Ixigo card still matches by first clock.
        dup = ix + " Fare Type Regular Fare ₹9,516 Book Lock Price @₹349"
        self.assertTrue(card_matches_depart_hint(dup, "1:15"))
        filtered = (
            "Departure from Guwahati 12:40 - 13:40 17:40 - 18:40 "
            "Air India Express 17:40 3h 5m 20:45 GAU DEL ₹10,263 Book"
        )
        self.assertTrue(card_matches_depart_hint(filtered, "17:40"))
        self.assertFalse(card_matches_depart_hint(filtered, "12:40"))
        from app.page_agent import _depart_clocks_on_fare_list

        self.assertEqual(_depart_clocks_on_fare_list(filtered), ["17:40"])
        from app.travel_agent import _wrong_itinerary_reply

        self.assertIn(
            "17:40 is on the Ixigo list",
            _wrong_itinerary_reply("verify_fail:time", filtered, "17:40"),
        )

    def test_ixigo_review_page_is_review_not_bare_other(self) -> None:
        text = (
            "Flight Selection 2 Review & Traveller Details 3 Add-ons 4 Payment "
            "Fare Summary Total Amount ₹9,116 "
            "Air-India Express | IX 1513 13:15 GAU 16:05 DEL VALUE "
            "Add Free Cancellation to your trip Continue"
        )
        self.assertEqual(classify_page(text, "https://www.ixigo.com/flight/booking/1"), "review")
        self.assertNotEqual(classify_page(text, "https://www.ixigo.com/flight/booking/1"), "addons")
        stepper = (
            "Flight Selection Review & Traveller Details Add-ons Payment "
            "Fare Summary Total Amount ₹9,091 Continue Guwahati New Delhi"
        )
        self.assertEqual(classify_page(stepper, "https://www.ixigo.com/flight/booking/1"), "review")
        sheet = (
            "Fare Summary Total Amount ₹9,091 Review Details Sudipta Saikia "
            "sudipta@example.com Edit Continue 6E5342"
        )
        self.assertEqual(classify_page(sheet, "https://www.ixigo.com/flight/booking/1"), "review")
        form = (
            "Review & Traveller Details Traveller Details Title First & Middle Name Last Name "
            "Please select your title Nationality India Mobile Number Email Continue"
        )
        self.assertEqual(classify_page(form, "https://www.ixigo.com/flight/booking/1"), "passenger")

    def test_ixigo_login_modal_is_login(self) -> None:
        text = "Log in to ixigo Enter Mobile Number Continue Sign in with Google"
        self.assertEqual(
            classify_page(text, "https://www.ixigo.com/flight/booking/1?loginVisible=true"),
            "login",
        )

    def test_otp_page_stops_without_paying(self) -> None:
        self.assertEqual(classify_page("Enter OTP OTP sent to your mobile Resend OTP", "https://www.ixigo.com/"), "otp")
        self.assertEqual(
            classify_page(
                "Verify Your Mobile Number Enter the OTP received via SMS on +91 9876543210 Resend in 52 Sec Verify",
                "https://www.ixigo.com/flight/booking/1",
            ),
            "otp",
        )
        action = deterministic_action("otp", hint="1:15", recovers=0)
        self.assertEqual(action["action"], "done")

    def test_trip_facts_from_ixigo_review(self) -> None:
        text = (
            "Fare Summary Total Amount ₹9,116 Guwahati New Delhi "
            "Air-India Express | IX 1513\n13:15\nGAU - Guwahati\n16:05\nDEL - New Delhi VALUE"
        )
        facts = extract_trip_facts(text, "https://www.ixigo.com/flight/booking/x?fareKey=GAU-DEL-IX1513")
        self.assertEqual(facts["flight"], "IX1513")
        self.assertEqual(facts["depart_time"], "13:15")
        self.assertEqual(facts["origin"], "GAU")
        self.assertEqual(facts["destination"], "DEL")
        self.assertEqual(facts["total"], "₹9116")
        ok, problems = verify_trip_facts(facts, expect_hint="1:15", expect_origin="GAU", expect_dest="DEL", expect_flight="IX1513")
        self.assertTrue(ok, problems)
        bad, problems = verify_trip_facts(facts, expect_flight="6E2179")
        self.assertFalse(bad)
        self.assertTrue(problems)

    def test_review_continues_and_pay_is_done_only(self) -> None:
        self.assertEqual(deterministic_action("review", hint="1:15", recovers=0)["action"], "review_continue")
        self.assertEqual(deterministic_action("addons", hint="10:45", recovers=0)["action"], "click")
        self.assertEqual(deterministic_action("pay", hint="1:15", recovers=0)["action"], "done")

    def test_parse_login_phone_france_and_india(self) -> None:
        self.assertEqual(parse_login_phone("+33759341005"), ("33", "759341005"))
        self.assertEqual(parse_login_phone("+919876543210"), ("91", "9876543210"))
        self.assertEqual(parse_login_phone("9876543210"), ("91", "9876543210"))

    def test_otpless_handoff_helpers_exported(self) -> None:
        from app.page_agent import install_ixigo_otpless_callback_bridge

        self.assertTrue(callable(install_ixigo_otpless_tap))
        self.assertTrue(callable(complete_ixigo_login_after_otp))
        self.assertTrue(callable(install_ixigo_otpless_callback_bridge))

    def test_ixigo_auth_session_signals_reject_otp_and_login(self) -> None:
        self.assertFalse(
            ixigo_auth_session_signals(
                "Enter OTP OTP sent to your mobile",
                "https://www.ixigo.com/flight/booking/1?loginVisible=true",
            )
        )
        self.assertFalse(
            ixigo_auth_session_signals(
                "Log in to ixigo Enter Mobile Number Continue",
                "https://www.ixigo.com/flight/booking/1?loginVisible=true",
            )
        )

    def test_ixigo_auth_session_signals_accept_passenger(self) -> None:
        text = "Primary passenger First name Last name Date of birth Gender Continue"
        url = "https://www.ixigo.com/flight/booking/1"
        self.assertEqual(classify_page(text, url), "passenger")
        self.assertTrue(ixigo_auth_session_signals(text, url))

    def test_kernel_otp_lengths_and_whatsapp_resume(self) -> None:
        from app.kernel_otp import (
            indian_national,
            ixigo_verify_in_progress,
            keep_kernel_otp_session,
            otp_accepted,
            otp_failure_detail,
            resume_kernel_otp,
            should_resume_kernel_otp,
            verify_fallback,
        )

        self.assertIn("incorrect", otp_failure_detail(boxes_open=True).lower())
        self.assertIn("new code", otp_failure_detail(boxes_open=True).lower())
        self.assertIn("Log in", otp_failure_detail(boxes_open=False))
        self.assertIn("checking", otp_failure_detail(boxes_open=False, verifying=True))
        self.assertTrue(keep_kernel_otp_session("still_otp"))
        waiting_live = {"kernel_awaiting_otp": True, "kernel_session_id": "sess", "kernel_seller": "ixigo"}
        self.assertEqual(resume_kernel_otp(waiting_live, "1234"), "need_digits")
        # No live waiter in-process → reconnect the same Kernel tab and type the 6 digits.
        self.assertEqual(resume_kernel_otp(waiting_live, "123456"), "reconnect")

        self.assertTrue(ixigo_verify_in_progress("Please wait while we verify your details."))
        self.assertFalse(ixigo_verify_in_progress("Verify Your Mobile Number Enter the OTP"))
        # Homepage "Login/SignUp" is logged out — do not treat "My Trips" alone as signed in.
        from app.kernel_otp import _HOLDS, _stop_hold

        self.assertNotIn("missing", _HOLDS)
        _stop_hold("missing")

        point = verify_fallback([{"x": 800, "y": 360}, {"x": 900, "y": 360}])
        self.assertEqual(point["x"], 850)
        self.assertEqual(point["y"], 470)
        self.assertFalse(keep_kernel_otp_session("no_session"))

        self.assertTrue(keep_kernel_otp_session("still_otp"))
        self.assertFalse(keep_kernel_otp_session("no_session"))

        self.assertEqual(indian_national("+91 8638586171"), "8638586171")
        self.assertEqual(indian_national("8638586171"), "8638586171")
        self.assertEqual(indian_national("123456789012345"), "")
        self.assertTrue(otp_accepted("ixigo", "123456"))
        self.assertFalse(otp_accepted("ixigo", "1234"))
        self.assertTrue(otp_accepted("cleartrip", "1234"))
        self.assertFalse(otp_accepted("cleartrip", "123456"))
        waiting = {"kernel_awaiting_otp": True, "kernel_session_id": "sess"}
        self.assertTrue(should_resume_kernel_otp(waiting, "529779"))
        self.assertFalse(should_resume_kernel_otp(waiting, "book the 1:15"))
        self.assertFalse(should_resume_kernel_otp({}, "529779"))

    def test_kernel_login_keeps_one_connection_for_the_code(self) -> None:
        import inspect
        import threading

        from app.kernel_otp import (
            _await_live_otp,
            arm_live_otp,
            begin_login,
            cancel_live_otp,
            handoff_whatsapp_otp,
            offer_live_otp,
            viewport_click,
        )

        # Kernel accepted the CSS center. Screen offset, chrome, and dpr must not move it.
        self.assertEqual(
            viewport_click(
                {
                    "outerH": 800,
                    "innerH": 670,
                    "outerW": 1400,
                    "innerW": 1247,
                    "screenX": 16,
                    "screenY": 97,
                    "dpr": 2,
                },
                {"x": 900, "y": 367},
            ),
            (900, 367),
        )
        self.assertIn("on_code_requested", inspect.signature(begin_login).parameters)

        session = "live-conn-test"
        cancel_live_otp(session)
        waiting = {
            "kernel_awaiting_otp": True,
            "kernel_session_id": session,
            "kernel_seller": "ixigo",
        }
        self.assertFalse(handoff_whatsapp_otp(waiting, "123456"))
        arm_live_otp(session)
        self.assertTrue(handoff_whatsapp_otp(waiting, "123456"))
        self.assertTrue(handoff_whatsapp_otp(waiting, "654321"))

        class _Page:
            url = "https://www.ixigo.com/"

        try:
            self.assertEqual(_await_live_otp(_Page(), session, 2), "123456")
            self.assertTrue(offer_live_otp(session, "654321"))
        finally:
            cancel_live_otp(session)

        session_wait = "live-conn-wait"
        cancel_live_otp(session_wait)
        waiting_later = {
            "kernel_awaiting_otp": True,
            "kernel_session_id": session_wait,
            "kernel_seller": "ixigo",
        }
        arm_live_otp(session_wait)

        def _send_later() -> None:
            threading.Event().wait(0.2)
            handoff_whatsapp_otp(waiting_later, "123456")

        threading.Thread(target=_send_later, daemon=True).start()
        try:
            self.assertEqual(_await_live_otp(_Page(), session_wait, 3), "123456")
        finally:
            cancel_live_otp(session_wait)

    def test_pause_on_auth_stops_without_login_phone(self) -> None:
        class _FakePage:
            url = "https://www.ixigo.com/flight/booking/1?loginVisible=true"
            context = type("C", (), {"pages": []})()

            def inner_text(self, _sel, timeout=8000):
                return "Log in to ixigo Enter Mobile Number Continue Sign in with Google"

            def evaluate(self, *_a, **_k):
                return 0

        out = run_checkout_agent(
            _FakePage(),
            hint="1:15",
            seller="ixigo",
            origin="Guwahati",
            destination="Delhi",
            depart="2026-12-04",
            allow_pay=False,
            pause_on_auth=True,
            max_steps=3,
        )
        self.assertEqual(out.stage, "auth_required")
        self.assertTrue(any("auth_pause" in x for x in out.log))

    def test_login_phone_is_per_traveller(self) -> None:
        import os

        from app.kernel_otp import login_phone_for

        previous = os.environ.get("IXIGO_LOGIN_PHONE")
        os.environ["IXIGO_LOGIN_PHONE"] = "+919876543210"
        try:
            self.assertEqual(login_phone_for("15550001111"), "")
            self.assertEqual(login_phone_for("+918638586171"), "+918638586171")
            self.assertEqual(login_phone_for("15550001111", "9876543210"), "+919876543210")
        finally:
            if previous is None:
                os.environ.pop("IXIGO_LOGIN_PHONE", None)
            else:
                os.environ["IXIGO_LOGIN_PHONE"] = previous

    def test_ixigo_choice_stays_on_ixigo_and_recommends(self) -> None:
        from app.travel_agent import _booking_seller_id, _kernel_trip, _options_reply

        state = {
            "seller": "ixigo",
            "kernel_logged_in": "ixigo",
            "origin": "Guwahati",
            "destination": "Delhi",
            "depart": "2026-12-04",
        }
        self.assertEqual(_booking_seller_id(state, "1:15"), "ixigo")
        self.assertEqual(_kernel_trip(state)[3], "")
        state["selected_hint"] = "13:15"
        self.assertEqual(_kernel_trip(state)[3], "13:15")
        spoken = _options_reply(
            state,
            [
                {
                    "airline": "IndiGo",
                    "flight": "6E201",
                    "depart": "13:15",
                    "arrive": "15:40",
                    "price": "9,106",
                    "price_n": 9106,
                    "minutes": 145,
                }
            ],
            seller="ixigo",
        )
        self.assertIn("IndiGo", spoken)
        self.assertIn("4 December", spoken)
        self.assertIn("Reply with the departure time", spoken)
        self.assertIn("stop before payment", spoken)
        self.assertNotIn("These are on the page", spoken)
        self.assertNotIn("code screen", spoken)

    def test_ixigo_results_keep_flight_number_and_arrival(self) -> None:
        from app.travel_agent import fare_options

        text = (
            "Cheapest Akasa Air QP1341, QP1808 09:10 GAU 10h 1 stop 19:10 DEL ₹8,365 Book "
            "Fastest SpiceJet SG762 12:30 GAU 2h 45m Non-stop 15:15 DEL ₹8,992 Book"
        )
        options = fare_options(text)
        self.assertEqual(options[0]["flight"], "QP1341,QP1808")
        self.assertEqual(options[0]["depart"], "09:10")
        self.assertEqual(options[0]["arrive"], "19:10")
        self.assertEqual(options[0]["price"], "8,365")
        self.assertEqual(options[1]["flight"], "SG762")
        self.assertEqual(options[1]["arrive"], "15:15")


    def test_mid_session_reopens_the_saved_booking_url(self) -> None:
        import inspect

        from app.kernel_otp import begin_login, is_resume_booking_url
        from app.page_agent import (
            _ixigo_opt_out_cancellation,
            _safe_continue,
            _select_saved_traveller,
            run_checkout_agent,
        )
        from app.travel_agent import infer_intent, remember_booking_page, resume_booking_url

        url = "https://www.ixigo.com/flight/booking/1044/abc?fareKey=GAU-DEL-6E5342-04122026"
        self.assertTrue(is_resume_booking_url(url, "ixigo"))
        self.assertFalse(is_resume_booking_url("https://www.ixigo.com/search/result/flight", "ixigo"))
        self.assertFalse(is_resume_booking_url(url, "cleartrip"))

        state = {
            "seller": "ixigo",
            "stage": "booking",
            "selected_hint": "10:45",
            "origin": "Guwahati",
            "destination": "Delhi",
        }
        shown = (
            "Fare Summary Total Amount ₹9,967 Review Details Sudipta Saikia "
            "Continue 6E5342 10:45 GAU"
        )
        self.assertEqual(remember_booking_page(state, "ixigo", url, shown), "review")
        self.assertEqual(state["live"]["url"], url)
        self.assertEqual(state["live"]["stage"], "review")
        state["stage"] = "review"
        self.assertEqual(resume_booking_url(state, "go ahead and confirm"), url)
        self.assertEqual(infer_intent(state, "go ahead and confirm"), "book")
        self.assertEqual(resume_booking_url(state, "the 13:15"), "")
        self.assertEqual(resume_booking_url({**state, "stage": "results"}, "go ahead"), "")
        self.assertEqual(remember_booking_page(state, "ixigo", "https://www.ixigo.com/search/result/flight", "Book"), "booking")
        self.assertNotIn("live", state)

        source = inspect.getsource(begin_login)
        self.assertIn("resume_url", source)
        self.assertIn("is_resume_booking_url", source)
        self.assertLess(source.index("resume_url"), source.index("ixigo_results_url"))
        self.assertIn("cleartrip_results_url", source)
        self.assertNotIn('page.goto("https://www.cleartrip.com/"', source)
        click = inspect.getsource(_safe_continue)
        self.assertIn("mouse.click", click)
        self.assertIn("elementFromPoint", click)
        self.assertIn("isForward", click)
        self.assertIn("continue\\\\b", click)
        self.assertIn("_ixigo_buy_this_visible", click)
        # Deny-list real charge buttons; "Continue to payment" is allowed.
        self.assertIn("payOnly", click)
        self.assertIn("continue to payment", click.lower())
        self.assertIn("Continue\\b.*", click)
        self.assertNotIn('name=re.compile(r"^(continue|confirm|proceed|pay now)', click.lower())
        from app.page_agent import _ixigo_click_no_thanks

        self.assertTrue(callable(_ixigo_click_no_thanks))
        no_thanks = inspect.getsource(_ixigo_click_no_thanks)
        self.assertIn("optout_no_thanks", no_thanks)
        self.assertIn("_ixigo_buy_this_visible", no_thanks)
        opt = inspect.getsource(_ixigo_opt_out_cancellation)
        self.assertIn("_ixigo_click_no_thanks", opt)
        self.assertIn("_ixigo_click_without_assured", opt)
        self.assertLess(opt.index("_ixigo_click_no_thanks"), opt.index("optout_exact_pay"))
        self.assertIn("buy this", opt.lower())
        self.assertIn("optout_exact_pay", opt)
        # Decline radio alone must not skip dismissing Secure My Trip / No, Thanks.
        self.assertIn("optout_no_thanks_miss", opt)
        self.assertIn("_ixigo_buy_this_visible(page)", opt)
        self.assertIn("_ixigo_assured_comparison_open(page)", opt)
        self.assertLess(opt.index("optout_no_thanks_miss"), opt.index('return "optout_already"'))
        self.assertIn("Without Assured", inspect.getsource(
            __import__("app.page_agent", fromlist=["_ixigo_click_without_assured"])._ixigo_click_without_assured
        ))
        self.assertIn("Pay", opt)
        self.assertIn("fee", opt.lower())
        self.assertIn("scrollIntoView", opt)
        self.assertIn("add free cancellation", opt.lower())
        self.assertNotIn("input.checked = true", opt)
        self.assertIn(
            "don.?t want free cancellation",
            (inspect.getsource(_select_saved_traveller) + inspect.getsource(_ixigo_opt_out_cancellation)).lower(),
        )
        from app.page_agent import (
            _adult_traveller_gaps,
            _clean_traveller_name,
            _decline_option_text,
            _decline_radio_checked,
            _ensure_review_traveller,
            _press_confirm_then_addons,
            _review_continue_blocker,
            _traveller_form_populated,
        )

        self.assertEqual(_clean_traveller_name("Pick Mr Sudipta Saikia"), "sudipta saikia")
        self.assertTrue(callable(_adult_traveller_gaps))
        self.assertTrue(callable(_traveller_form_populated))
        self.assertTrue(callable(_ensure_review_traveller))
        self.assertTrue(
            _decline_option_text(
                "I don't want Free Cancellation. Pay ₹3,999 fee if you cancel or reschedule"
            )
        )
        self.assertFalse(_decline_option_text("Without Assured"))
        self.assertFalse(_decline_option_text("With Assured"))
        decline_src = inspect.getsource(_decline_radio_checked)
        self.assertIn("selected|active|checked", decline_src)
        self.assertIn("don.?t want free cancellation", decline_src)
        blocker = inspect.getsource(_review_continue_blocker)
        self.assertIn("_adult_traveller_gaps", blocker)
        self.assertIn("Adult traveller details are incomplete", blocker)
        self.assertIn("saved traveller not selected", blocker)
        self.assertIn("_email_input_empty", blocker)
        self.assertIn("Contact email is empty", blocker)
        self.assertIn("Without Assured", blocker)
        self.assertIn("Assured Free Cancellation is still open", blocker)
        self.assertIn("after I tried Without Assured", blocker)
        without_src = inspect.getsource(
            __import__("app.page_agent", fromlist=["_ixigo_click_without_assured"])._ixigo_click_without_assured
        )
        # Card copy is longer than the bare label — exact ^without assured$ alone misses.
        self.assertIn("160", without_src)
        self.assertIn("without assured", without_src.lower())
        self.assertIn("_ixigo_assured_comparison_open", without_src)
        declined_src = inspect.getsource(
            __import__("app.page_agent", fromlist=["_cancellation_declined"])._cancellation_declined
        )
        self.assertIn("optout_without_assured_miss", declined_src)
        from unittest.mock import patch

        from app.travel_agent import resolve_intent

        skip_state = {
            "origin": "Guwahati",
            "destination": "Delhi",
            "depart": "2026-12-04",
            "seller": "ixigo",
            "stage": "booking",
            "stuck_kind": "addon_buy",
            "stuck_page": (
                "Review Details Billing Address PIN code 785001 "
                "Free Cancellation With Assured ₹9,813 Without Assured ₹8,964 Continue"
            ),
        }
        with patch("app.travel_agent.get_settings") as gs:
            gs.return_value.gemini_api_key = ""
            gs.return_value.llm_ready = False
            kind, spoken = resolve_intent("no thanks", skip_state, [])
        self.assertEqual(kind, "book")
        self.assertIn("skip", spoken.lower())
        self.assertRegex(str(skip_state.get("chosen_tap") or ""), re.compile(r"without assured", re.I))
        gaps_src = inspect.getsource(_adult_traveller_gaps)
        self.assertIn("please select your title", gaps_src.lower())
        self.assertIn("_decline_radio_checked", blocker)
        self.assertIn("_ixigo_buy_this_visible", blocker)
        self.assertIn("No, Thanks", blocker)
        self.assertIn("pin code", blocker.lower())
        prep = inspect.getsource(
            __import__("app.page_agent", fromlist=["_ixigo_prepare_review"])._ixigo_prepare_review
        )
        self.assertIn("_ensure_review_traveller", prep)
        self.assertIn("_commit_contact_email", prep)
        pick = inspect.getsource(
            __import__("app.page_agent", fromlist=["_select_saved_traveller"])._select_saved_traveller
        )
        self.assertIn("_traveller_form_populated", pick)
        self.assertIn("traveller_already_empty", pick)
        from app.page_agent import _email_input_empty, _on_addons_page

        self.assertTrue(callable(_on_addons_page))
        self.assertTrue(callable(_email_input_empty))
        self.assertFalse(
            _on_addons_page(
                "Review & Traveller Details Contact Details Please enter an email address "
                "Billing Address Continue Add-ons Payment",
                "review",
            )
        )
        self.assertTrue(
            _on_addons_page("Choose add-ons Skip add-ons Add seats Add meals", "addons")
        )
        confirm_src = inspect.getsource(_press_confirm_then_addons)
        self.assertIn("name_confirm", confirm_src)
        self.assertIn("reached_addons_after_confirm", confirm_src)
        self.assertIn("_on_addons_page", confirm_src)
        self.assertIn("Confirm", confirm_src)
        self.assertIn("_dismiss_post_confirm_secure_trip", confirm_src)
        self.assertIn("_leave_addons", confirm_src)
        self.assertIn("reached_pay_after", confirm_src)
        dismiss_src = inspect.getsource(
            __import__("app.page_agent", fromlist=["_dismiss_post_confirm_secure_trip"])._dismiss_post_confirm_secure_trip
        )
        self.assertIn("_ixigo_click_no_thanks", dismiss_src)
        self.assertIn("post_confirm_no_thanks", dismiss_src)
        deliver_src = inspect.getsource(
            __import__("app.travel_agent", fromlist=["_deliver_signed_in_page"])._deliver_signed_in_page
        )
        self.assertIn("reached_pay_after_", deliver_src)
        self.assertIn("post_confirm_no_thanks", deliver_src)
        # Auto No Thanks / leave-addons must beat the sticky "reached add-ons" ask.
        self.assertLess(
            deliver_src.index("reached_pay_after_"),
            deliver_src.index("reached_addons_after_confirm"),
        )
        pick = inspect.getsource(_select_saved_traveller)
        self.assertIn("input[type=checkbox]", pick)
        review = inspect.getsource(run_checkout_agent)
        self.assertIn("Escape closes the Ixigo review sheet", review)
        self.assertIn("One accurate click per attempt", review)
        self.assertIn('sum(1 for line in log if "review_continue" in line) < 3', review)
        self.assertNotIn('{"action": "done", "target": "review"', review)
        self.assertNotIn("_dismiss_overlays(page)", review.split("review_continue")[1][:700])
        self.assertIn("_press_confirm_then_addons", review)
        self.assertIn("_decline_radio_checked", review)
        handler = review.split('if kind == "review_continue"')[1][:5500]
        self.assertGreaterEqual(handler.count("_safe_continue(page)"), 1)
        self.assertIn("_ixigo_prepare_review", handler)
        self.assertIn("_ixigo_buy_this_visible", handler)
        self.assertIn("_pause_unknown", handler)
        self.assertIn("_clear_front_sheet", handler)
        self.assertIn("_press_confirm_then_addons", handler)
        self.assertIn("Conscience", handler)
        self.assertLess(handler.index("_ixigo_prepare_review"), handler.index("_safe_continue(page)"))
        self.assertLess(handler.index("_safe_continue(page)"), handler.index("_press_confirm_then_addons"))
        # Overlay / Assured must prepare (fill traveller) — never pause-ask first.
        self.assertIn("_ixigo_prepare_review", handler)
        self.assertNotIn(
            "return _pause_unknown(\n                        page,\n                        seller=seller,\n                        hint=hint,\n                        text=text,\n                        url=url,\n                        log=log,\n                        facts=facts,\n                        checks=checks,\n                        step=step,\n                        chosen_tap=chosen_tap,\n                    )",
            handler,
        )
        # No chosen_tap on Ixigo Review: autofill before ask.
        self.assertIn("ixigo_autofill_before_ask", review)

    def test_continue_confirm_addons_handoff_beats_contact_report(self) -> None:
        """Review → Continue → Confirm → Add-ons must announce Add-ons, not 'typed contact'."""
        import inspect

        from app.page_agent import contact_report, run_checkout_agent
        from app.travel_agent import _deliver_signed_in_page

        stuck = (
            "Review & Traveller Details Billing Address Pin code 785001 "
            "I don't want Free Cancellation Continue"
        )
        blocked = contact_report(
            "email:ok | phone:none | optout_miss | user_tap_upsell_continue:0",
            stuck,
        )
        self.assertIn("free cancellation is still unselected", blocked.lower())
        deliver = inspect.getsource(_deliver_signed_in_page)
        self.assertIn("reached_addons_after_confirm", deliver)
        self.assertIn("could not select", deliver)
        self.assertIn("Free Cancellation", deliver)
        checkout = inspect.getsource(run_checkout_agent)
        self.assertIn("_review_continue_blocker", checkout)
        self.assertIn("optout_exact_pay", inspect.getsource(
            __import__("app.page_agent", fromlist=["_ixigo_opt_out_cancellation"])._ixigo_opt_out_cancellation
        ))

    def test_queued_continue_rebuilds_email_profile(self) -> None:
        """Email sent while checkout is in flight must reach the queued Continue fill."""
        import inspect

        from app.page_agent import contact_report
        from app.travel_agent import _run_kernel_login, absorb_details

        src = inspect.getsource(_run_kernel_login)
        self.assertIn("pending_text", src)
        self.assertIn("fresh_profile", src)
        self.assertIn("absorb_details(queued, pending_text)", src)
        self.assertIn("profile=fresh_profile", src)
        self.assertNotIn("profile=profile,\n                on_code_requested=on_code_requested,\n                on_otp_result=on_otp_result,\n                resume_url=str((result or {}).get(\"url\") or resume or \"\"),\n                chosen_tap=queued_tap", src)

        fixed = absorb_details({}, "sudiptasaikia6@gmail.vom")
        self.assertEqual(fixed.get("email"), "sudiptasaikia6@gmail.com")

        empty = contact_report(
            "email:none | phone:ok",
            "Contact Details Please enter an email address Continue",
        )
        self.assertIn("empty", empty.lower())
        # Still on Review with empty-email copy — never claim Continue succeeded.
        stuck_email = contact_report(
            "email:ok | phone:ok | user_tap_continue:1",
            "Review & Traveller Details Contact Details Please enter an email address Continue",
        )
        self.assertIn("empty", stuck_email.lower())
        self.assertNotIn("pressed Continue", stuck_email)
        from app.page_agent import (
            _commit_contact_email,
            _ixigo_prepare_review,
            _react_set_input_value,
            _read_email_value,
        )

        prep = inspect.getsource(_ixigo_prepare_review)
        self.assertIn("_commit_contact_email", prep)
        self.assertNotIn('"email", "phone"', prep.split("fill_profile_slots")[1][:200])
        self.assertLess(prep.index("_ensure_review_traveller"), prep.rindex("_commit_contact_email"))
        commit_src = inspect.getsource(_commit_contact_email)
        self.assertIn("press_sequentially", commit_src)
        self.assertIn("_react_set_input_value", commit_src)
        self.assertTrue(callable(_commit_contact_email))
        self.assertTrue(callable(_read_email_value))
        self.assertTrue(callable(_react_set_input_value))
        checkout = inspect.getsource(
            __import__("app.page_agent", fromlist=["run_checkout_agent"]).run_checkout_agent
        )
        self.assertIn("_commit_contact_email", checkout)
        self.assertIn("please enter an email address", checkout)
        deliver = inspect.getsource(
            __import__("app.travel_agent", fromlist=["_deliver_signed_in_page"])._deliver_signed_in_page
        )
        self.assertIn("contact email is empty", deliver.lower())
        self.assertIn("contact details", deliver.lower())


class IxigoSavedTravellerAutofill(unittest.TestCase):
    """Dirty profile names must tick/fill Adult 1 — never type 'Pick' into First Name."""

    def test_name_parts_strip_pick_and_title(self) -> None:
        from app.page_agent import _clean_traveller_name, _traveller_name_parts

        self.assertEqual(_clean_traveller_name("Pick Mr Sudipta Saikia"), "sudipta saikia")
        title, first, last = _traveller_name_parts("Pick Mr Sudipta Saikia")
        self.assertEqual(title, "Mr")
        self.assertEqual(first.lower(), "sudipta")
        self.assertEqual(last.lower(), "saikia")

    def test_fill_passenger_fields_uses_cleaned_name(self) -> None:
        from playwright.sync_api import sync_playwright

        from app.page_agent import _adult_traveller_gaps, _fill_passenger_fields, _traveller_form_populated

        html = """
        <html><body>
          <label>Title<select id="t"><option value="">Title</option><option value="Mr">Mr</option></select></label>
          <label>First & Middle Name<input id="f" /></label>
          <label>Last Name<input id="l" /></label>
        </body></html>
        """
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content(html)
            notes = _fill_passenger_fields(
                page, {"name": "Pick Mr Sudipta Saikia", "gender": "male"}
            )
            self.assertIn("title:ok", notes)
            self.assertEqual(page.input_value("#t"), "Mr")
            self.assertEqual(page.input_value("#f"), "Sudipta")
            self.assertEqual(page.input_value("#l"), "Saikia")
            self.assertNotEqual(page.input_value("#f").lower(), "pick")
            self.assertTrue(_traveller_form_populated(page))
            self.assertEqual(_adult_traveller_gaps(page), [])
            browser.close()

    def test_prepare_review_autofills_saved_traveller_and_email(self) -> None:
        from playwright.sync_api import sync_playwright

        from app.page_agent import (
            _email_input_empty,
            _ixigo_autonomous_gate,
            _ixigo_prepare_review,
            _traveller_form_populated,
        )

        html = """
        <html><body>
          <div>Review & Traveller Details</div>
          <div>Add Free Cancellation Please select an option before proceeding
            <label><input type="radio" name="fareType" id="standalone-none-fareType" />
            I don't want Free Cancellation. Pay ₹3,999 fee if you cancel</label>
          </div>
          <h2>Traveller Details</h2>
          <p>Choose from the saved list</p>
          <label><input type="checkbox" id="sv" /> Mr Sudipta Saikia</label>
          <div>
            <p>Adult 1 Clear Details</p>
            <label>Title<select id="t"><option value="">Title</option><option value="Mr">Mr</option></select></label>
            <label>First & Middle Name<input id="f" /></label>
            <label>Last Name<input id="l" /></label>
          </div>
          <label>Email<input id="e" type="email" /></label>
          <label>Mobile Number<input id="m" /></label>
          <label>PIN code<input id="p" /></label>
          <script>
            document.getElementById('sv').addEventListener('change', (ev) => {
              if (ev.target.checked) {
                document.getElementById('t').value = 'Mr';
                document.getElementById('f').value = 'Sudipta';
                document.getElementById('l').value = 'Saikia';
              }
            });
          </script>
        </body></html>
        """
        profile = {
            "name": "Pick Mr Sudipta Saikia",
            "email": "sudiptasaikia6@gmail.com",
            "phone": "8638586171",
            "pincode": "781001",
            "auth_mode": "login",
            "extras_preference": "decline",
        }
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content(html)
            notes = _ixigo_prepare_review(page, profile)
            self.assertTrue(any("traveller" in str(n) for n in notes))
            self.assertIn("email:ok", notes)
            self.assertEqual(page.input_value("#t"), "Mr")
            self.assertEqual(page.input_value("#f"), "Sudipta")
            self.assertEqual(page.input_value("#l"), "Saikia")
            self.assertFalse(_email_input_empty(page))
            self.assertTrue(_traveller_form_populated(page))
            self.assertEqual(_ixigo_autonomous_gate(page, profile, []), "")
            browser.close()

    def test_empty_title_select_is_a_gap(self) -> None:
        from playwright.sync_api import sync_playwright

        from app.page_agent import _adult_traveller_gaps

        html = """
        <html><body>
          <label>Title<select><option value="">Title</option><option value="Mr">Mr</option></select></label>
          <label>First & Middle Name<input value="Sudipta" /></label>
          <label>Last Name<input value="Saikia" /></label>
        </body></html>
        """
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content(html)
            self.assertIn("title", _adult_traveller_gaps(page))
            browser.close()

    def test_filled_title_input_is_not_a_gap_from_label(self) -> None:
        """Ixigo keeps a visible 'Title' label beside a filled Mr text box — that is not a gap."""
        from playwright.sync_api import sync_playwright

        from app.page_agent import _adult_traveller_gaps, _traveller_form_populated

        html = """
        <html><body>
          <span>Title</span>
          <label>Title<input value="Mr" /></label>
          <label>First & Middle Name<input value="Sudipta" /></label>
          <label>Last Name<input value="Saikia" /></label>
        </body></html>
        """
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content(html)
            self.assertNotIn("title", _adult_traveller_gaps(page))
            self.assertTrue(_traveller_form_populated(page))
            browser.close()

    def test_select_saved_traveller_clicks_offscreen_listitem_card(self) -> None:
        """Ixigo saved people are role=listitem cards that can sit above the viewport."""
        from playwright.sync_api import sync_playwright

        from app.page_agent import _select_saved_traveller, _traveller_form_populated

        html = """
        <html><body style="margin:0">
          <div style="height:900px">Offers spacer</div>
          <div role="listitem" style="width:231px;height:44px">Mr Sudipta Saikia</div>
          <div>
            <label>Title<input id="t" value="" /></label>
            <label>First & Middle Name<input id="f" value="" /></label>
            <label>Last Name<input id="l" value="" /></label>
          </div>
          <script>
            document.querySelector('[role=listitem]').addEventListener('click', () => {
              document.getElementById('t').value = 'Mr';
              document.getElementById('f').value = 'Sudipta';
              document.getElementById('l').value = 'Saikia';
            });
          </script>
        </body></html>
        """
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content(html)
            page.set_viewport_size({"width": 1280, "height": 720})
            note = _select_saved_traveller(page, "Pick Mr Sudipta Saikia")
            self.assertRegex(note, r"traveller_(card|clicked|retry)")
            self.assertEqual(page.input_value("#f"), "Sudipta")
            self.assertTrue(_traveller_form_populated(page))
            browser.close()


class IxigoSkipToPaymentAndShots(unittest.TestCase):
    """Add-ons Skip to Payment routing + clean user-facing screenshots."""

    def test_skip_to_payment_is_not_a_trip_route(self) -> None:
        from app.intent import parse_updates
        from app.travel_agent import absorb_trip

        self.assertEqual(parse_updates("skip to payment"), {})
        self.assertEqual(parse_updates("continue without add-ons"), {})
        kept = absorb_trip(
            {"origin": "Guwahati", "destination": "Delhi", "depart": "2026-12-04"},
            "skip to payment",
        )
        self.assertEqual(kept.get("origin"), "Guwahati")
        self.assertEqual(kept.get("destination"), "Delhi")

    def test_ixigo_addons_not_misclassified_as_review(self) -> None:
        from app.page_agent import classify_page
        from app.step_contract import left_addons

        page = (
            "Flight Selection Review & Traveller Details Add-ons Payment "
            "Your Flight Guwahati New Delhi Fri, 04 Dec 6E-123 "
            "Fare Summary Base Fare ₹8,241 Total Amount ₹9,813 "
            "Seat Meal Insurance Skip to Payment > Meal Selection > "
            "seat map Select Seat"
        )
        stage = classify_page(page, "https://www.ixigo.com/checkout/addons")
        self.assertEqual(stage, "addons")
        self.assertFalse(left_addons(page, stage))
        self.assertFalse(left_addons(page, "review"))

    def test_skip_to_payment_intent_sets_ixigo_tap(self) -> None:
        from app.travel_agent import extract_user_ask

        state = {
            "origin": "Guwahati",
            "destination": "Delhi",
            "depart": "2026-12-04",
            "seller": "ixigo",
            "stage": "booking",
            "stuck_kind": "addons",
            "stuck_page": (
                "Seat Meal Insurance Skip to Payment > Meal Selection "
                "Choose add-ons seat map"
            ),
        }
        for phrase in (
            "skip to payment",
            "Skip to Payment",
            "skip add-ons",
            "I don't want seats or meals",
            "continue without add-ons",
        ):
            asked = extract_user_ask(phrase, state)
            self.assertEqual(asked.get("goal"), "skip", phrase)
            self.assertEqual(asked.get("intent"), "book", phrase)
        pay = extract_user_ask("skip to payment", state)
        self.assertRegex(str(pay.get("tap") or ""), re.compile(r"skip to payment", re.I))

    def test_skip_addons_recognises_skip_to_payment_label(self) -> None:
        import inspect

        from app.page_agent import _skip_addons
        from app.step_read import addons_sheet, decline_tap, visible_forward

        src = inspect.getsource(_skip_addons)
        self.assertIn("Skip to Payment", src)
        self.assertIn("skip to payment", src.lower())
        page = "Seat Meal Insurance Skip to Payment > Meal Selection seat map"
        self.assertTrue(addons_sheet(page))
        self.assertRegex(decline_tap(page) or "", re.compile(r"skip to payment", re.I))
        self.assertRegex(
            visible_forward(page, "skip to payment") or "",
            re.compile(r"skip to payment", re.I),
        )

    def test_skip_to_payment_clicks_and_leaves_addons(self) -> None:
        from playwright.sync_api import sync_playwright

        from app.page_agent import _leave_addons, _skip_addons, classify_page

        html = """
        <html><body>
          <h1>Add-ons</h1>
          <p>Review &amp; Traveller Details Fare Summary Total Amount ₹9,813 6E-212</p>
          <a id="skip" href="#pay">Skip to Payment &gt;</a>
          <div id="addons">Seat Meal Insurance seat map Meal Selection</div>
          <div id="pay" style="display:none">Payment options Pay now UPI enter card number Total Amount ₹9,813</div>
          <script>
            document.getElementById('skip').addEventListener('click', (e) => {
              e.preventDefault();
              document.getElementById('skip').remove();
              document.getElementById('addons').remove();
              document.body.querySelector('h1').textContent = 'Payment';
              document.getElementById('pay').style.display = 'block';
              history.replaceState({}, '', '/payment');
            });
          </script>
        </body></html>
        """
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content(html)
            before = classify_page(page.inner_text("body"), page.url)
            self.assertEqual(before, "addons")
            log: list[str] = []
            ok = _leave_addons(page, log, rounds=3)
            hit = " ".join(log)
            self.assertTrue(ok, log)
            self.assertRegex(hit, re.compile(r"skip to payment|addons_left:pay", re.I))
            self.assertTrue(page.locator("#pay").is_visible())
            self.assertFalse(page.locator("#addons").is_visible())
            after = classify_page(page.inner_text("body"), page.url)
            self.assertEqual(after, "pay")
            browser.close()

    def test_skip_to_payment_does_not_loop_forever_when_disabled(self) -> None:
        from playwright.sync_api import sync_playwright

        from app.page_agent import _leave_addons

        html = """
        <html><body>
          <h1>Add-ons</h1>
          <p>Seat Meal Insurance seat map Skip to Payment Meal Selection Fare Summary ₹9,813</p>
          <a id="skip" style="pointer-events:none;opacity:0.4">Skip to Payment &gt;</a>
        </body></html>
        """
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content(html)
            log: list[str] = []
            ok = _leave_addons(page, log, rounds=2)
            browser.close()
        self.assertFalse(ok)
        self.assertLessEqual(sum(1 for line in log if line.startswith("addons_skip:")), 4)

    def test_user_shot_clears_selection_and_differs_from_vision_shot(self) -> None:
        import inspect

        from app.vision_agent import _clear_page_for_user_shot, capture_page_shot, capture_user_shot

        user_src = inspect.getsource(capture_user_shot)
        clear_src = inspect.getsource(_clear_page_for_user_shot)
        vision_src = inspect.getsource(capture_page_shot)
        self.assertIn("_clear_page_for_user_shot", user_src)
        self.assertIn("removeAllRanges", clear_src)
        self.assertIn("data-guide-hl", clear_src)
        self.assertRegex(user_src, r'["\']quality["\']\s*:\s*9[02]')
        self.assertIn("settle_visible_sheet", vision_src)
        self.assertNotIn("settle_visible_sheet", user_src)

    def test_user_shot_has_no_debug_overlay_pixels_contract(self) -> None:
        from playwright.sync_api import sync_playwright

        from app.vision_agent import capture_user_shot

        html = """
        <html><body style="background:#fff;color:#111;font:16px sans-serif">
          <div data-guide-hl style="position:fixed;inset:0;background:rgba(0,0,255,0.5)"></div>
          <h1>Fare Summary</h1>
          <p>Total Amount ₹9,813</p>
          <button>Skip to Payment</button>
        </body></html>
        """
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content(html)
            path = capture_user_shot(page, tag="clarity")
            self.assertTrue(path)
            # Overlay node must be removed before capture.
            left = page.evaluate("() => document.querySelectorAll('[data-guide-hl]').length")
            self.assertEqual(left, 0)
            browser.close()


class IxigoCheckoutDecisions(unittest.TestCase):
    """Logged-in Ixigo autonomy: AUTO_FILL, ASK_USER, REQUIRE_APPROVAL. Does not pay."""

    def _profile(self, **extra):
        base = {
            "name": "Sudipta Saikia",
            "email": "sudiptasaikia6@gmail.com",
            "phone": "8638586171",
            "pincode": "781001",
            "billing_address": "12 Zoo Road",
            "city": "Guwahati",
            "billing_state": "Assam",
            "auth_mode": "login",
            "extras_preference": "decline",
        }
        base.update(extra)
        return base

    def _ready_obs(self, **extra):
        base = {
            "stage": "review",
            "on_review": True,
            "text": "Review Details Mr Sudipta Saikia Email Mobile 8638586171 781001 Continue",
            "traveller_gaps": [],
            "traveller_ok": True,
            "email_empty": False,
            "phone_empty": False,
            "pincode_empty": False,
            "pincode_invalid": False,
            "address_empty": False,
            "city_empty": False,
            "state_empty": False,
            "session": "authenticated",
        }
        base.update(extra)
        return base

    def test_auto_fill_when_saved_review_is_complete(self) -> None:
        from app.ixigo_decisions import AUTO_FILL, plan_ixigo_checkout

        plan = plan_ixigo_checkout(self._ready_obs(), self._profile())
        self.assertIn(AUTO_FILL, plan["classes"])
        self.assertTrue(plan["continue_ok"])
        self.assertEqual(plan["handoff"], "")
        self.assertEqual(plan["extras"], "decline")

    def test_ask_user_for_missing_traveller_email_and_invalid_pin(self) -> None:
        from app.ixigo_decisions import ASK_USER, plan_ixigo_checkout

        missing = plan_ixigo_checkout(
            self._ready_obs(traveller_gaps=["title", "first name"], traveller_ok=False),
            self._profile(name=""),
        )
        self.assertIn(ASK_USER, missing["classes"])
        self.assertFalse(missing["continue_ok"])
        self.assertIn("Passenger name", missing["handoff"])

        pin = plan_ixigo_checkout(self._ready_obs(pincode_invalid=True), self._profile())
        self.assertFalse(pin["continue_ok"])
        self.assertIn("Pincode is invalid", pin["handoff"])

        email = plan_ixigo_checkout(
            self._ready_obs(email_empty=True),
            self._profile(email=""),
        )
        self.assertIn("Email is missing", email["handoff"])
        self.assertFalse(email["continue_ok"])

    def test_saved_email_is_auto_fill_until_the_dom_holds_it(self) -> None:
        from app.ixigo_decisions import AUTO_FILL, plan_ixigo_checkout

        plan = plan_ixigo_checkout(self._ready_obs(email_empty=True), self._profile())
        email = next(i for i in plan["items"] if i["field"] == "email")
        self.assertEqual(email["class"], AUTO_FILL)
        self.assertEqual(email["action"], "fill")
        self.assertFalse(plan["continue_ok"])

    def test_unauthorised_extras_require_approval_decline_does_not(self) -> None:
        from app.ixigo_decisions import AUTO_FILL, REQUIRE_APPROVAL, plan_ixigo_checkout

        open_sheet = self._ready_obs(
            assured_open=True,
            cancellation_declined=False,
            text="With Assured ₹9,813 Without Assured ₹8,964",
        )
        decline = plan_ixigo_checkout(open_sheet, self._profile())
        choice = next(i for i in decline["items"] if i["field"] == "free_cancellation")
        self.assertEqual(choice["class"], AUTO_FILL)
        self.assertEqual(choice["action"], "decline")
        self.assertIn("9,813", choice["detail"])
        self.assertFalse(decline["continue_ok"])

        ask = plan_ixigo_checkout(open_sheet, self._profile(extras_preference="ask"))
        paid = next(i for i in ask["items"] if i["field"] == "free_cancellation")
        self.assertEqual(paid["class"], REQUIRE_APPROVAL)
        self.assertIn("no thanks", paid["detail"].lower())
        self.assertFalse(ask["continue_ok"])

    def test_payment_and_addons_and_expired_session(self) -> None:
        from app.ixigo_decisions import REQUIRE_APPROVAL, plan_ixigo_checkout, session_after_otp

        pay = plan_ixigo_checkout(
            self._ready_obs(stage="pay", on_review=False, payment_open=True, total="₹9,813"),
            self._profile(),
        )
        self.assertIn(REQUIRE_APPROVAL, pay["classes"])
        self.assertFalse(pay["continue_ok"])
        self.assertIn("Final fare", pay["handoff"])
        self.assertIn("will not click Pay", pay["handoff"])

        addons = plan_ixigo_checkout(
            {"stage": "addons", "on_addons": True, "text": "Choose add-ons Skip add-ons", "session": "authenticated"},
            self._profile(),
        )
        skip = next(i for i in addons["items"] if i["field"] == "addons")
        self.assertEqual(skip["action"], "skip")

        otp = plan_ixigo_checkout(self._ready_obs(stage="otp", session="unauthenticated"), self._profile())
        self.assertFalse(otp["continue_ok"])
        self.assertIn("not authenticated", otp["handoff"])
        self.assertEqual(session_after_otp(otp_status="fares", logged_in=False), "unauthenticated")
        self.assertEqual(session_after_otp(otp_status="verified", logged_in=False), "unauthenticated")
        self.assertEqual(session_after_otp(otp_status="ok", logged_in=True, still_otp=False), "authenticated")
        self.assertEqual(session_after_otp(otp_status="ok", logged_in=True, still_otp=True), "unauthenticated")

    def test_gate_skips_guests_and_checkout_calls_it(self) -> None:
        import inspect

        from app.page_agent import _ixigo_autonomous_gate, run_checkout_agent

        self.assertEqual(_ixigo_autonomous_gate(None, {"auth_mode": "guest"}, []), "")
        src = inspect.getsource(run_checkout_agent)
        self.assertIn("_ixigo_autonomous_gate", src)
        self.assertIn("allow_pay", src)

    def test_ten_consecutive_local_review_plans_allow_continue(self) -> None:
        """Ten identical ready reviews. This is the decision engine, not a live Ixigo payment."""
        from app.ixigo_decisions import plan_ixigo_checkout

        ready = 0
        for _ in range(10):
            plan = plan_ixigo_checkout(self._ready_obs(), self._profile())
            self.assertTrue(plan["continue_ok"])
            self.assertFalse(plan["blocks"])
            ready += 1
        self.assertEqual(ready, 10)


class DirectIxigoCheckoutTests(unittest.TestCase):
    """A known traveller who says book GHY–Delhi on a date goes straight to Ixigo checkout."""

    def _ready(self) -> dict:
        return {
            "name": "Sudipta Saikia",
            "email": "sudipta@example.com",
            "dob": "1997-07-29",
            "gender": "male",
            "phone": "9810012345",
            "origin": "Guwahati",
            "destination": "Delhi",
            "depart": "2026-12-04",
        }

    def test_book_phrase_parses_ghy_delhi_4_dec(self) -> None:
        from app.intent import parse_updates

        from app.search import place_label

        updates = parse_updates("book a flight from ghy to delhi 4th Dec")
        self.assertEqual(place_label(str(updates.get("origin") or "")), "Guwahati")
        self.assertEqual(place_label(str(updates.get("destination") or "")), "Delhi")
        self.assertTrue(str(updates.get("depart") or "").endswith("-12-04"))
        self.assertFalse(updates.get("depart_until"))

    def test_known_traveller_books_instead_of_searching(self) -> None:
        from app.turn_loop import align_action
        from app.travel_agent import wants_direct_ixigo_checkout

        bare = "book a flight from ghy to delhi 4th Dec"
        named = "book a flight from ghy to delhi 4th Dec on ixigo"
        ready = self._ready()
        turn = {"goal": "search", "intent": "search", "kind": "", "tap": ""}
        self.assertFalse(wants_direct_ixigo_checkout(bare, ready))
        self.assertTrue(wants_direct_ixigo_checkout(named, ready))
        self.assertTrue(wants_direct_ixigo_checkout("book ghy to delhi 4th Dec in ixigo", ready))
        self.assertFalse(wants_direct_ixigo_checkout(named, {}))
        self.assertEqual(align_action(bare, ready, turn)["do"], "search")
        self.assertEqual(align_action(named, ready, turn)["do"], "book")

    def test_ack_names_ixigo_checkout_before_charge(self) -> None:
        from app.travel_agent import _arm_direct_ixigo_checkout, direct_checkout_ack

        state = self._ready()
        state["origin"] = "ghy"
        state["destination"] = "delhi"
        _arm_direct_ixigo_checkout(state, "book a flight from ghy to delhi 4th Dec")
        self.assertEqual(state.get("origin"), "Guwahati")
        self.assertEqual(state.get("destination"), "Delhi")
        self.assertEqual(state.get("seller"), "ixigo")
        self.assertEqual(state.get("kernel_auth_mode"), "login")
        self.assertTrue(state.get("direct_checkout"))
        self.assertEqual(
            direct_checkout_ack(state),
            "On it. I'll set up Guwahati to Delhi on 4 Dec in Ixigo "
            "and show you the checkout page before anything is charged.",
        )

    def test_checkout_lines_match_the_ready_to_pay_brief(self) -> None:
        from app.travel_agent import _checkout_summary_open, checkout_ready_lines

        page = (
            "Fare Summary Akasa Air QP1341 09:10 Guwahati layover in Bagdogra 19:10 Delhi "
            "Total Amount ₹8,331 convenience fee ₹505 coupon ₹364 15 kg checked bag "
            "expires in 9 min"
        )
        state = {
            **self._ready(),
            "direct_checkout": True,
            "seller": "ixigo",
            "direct_pick": {
                "airline": "Akasa Air",
                "depart": "09:10",
                "arrive": "19:10",
                "stops": "1 stop",
                "price_n": 8365,
                "minutes": 600,
            },
            "direct_alt": {
                "airline": "SpiceJet",
                "depart": "08:55",
                "arrive": "11:55",
                "stops": "Non-stop",
                "price_n": 10006,
                "minutes": 180,
            },
        }
        self.assertTrue(_checkout_summary_open(page, "review"))
        self.assertFalse(
            _checkout_summary_open("Please enter your email Fare Summary Total Amount ₹8,331", "review")
        )
        lines = checkout_ready_lines(state, page)
        self.assertEqual(
            lines,
            [
                "Ready to pay on Ixigo, nothing charged. Cheapest on 4 Dec: Akasa Air, "
                "leaves Guwahati 9:10 AM, lands Delhi 7:10 PM, one long layover in Bagdogra. "
                "Total Rs 8,331 (includes a Rs 505 fee, after a Rs 364 coupon). 15 kg checked bag.",
                "If you'd rather skip the layover, SpiceJet nonstop 8:55 AM to 11:55 AM "
                "is about Rs 1,675 more.",
                "Approve Rs 8,331 for the Akasa flight, or want the nonstop? And payment: "
                "I can't do UPI myself. I can send a secure link to save your card and pay with it, "
                "or you pay on the page yourself. The fare hold expires in about 9 minutes, so quick is good.",
            ],
        )

    def test_seat_map_is_addons_not_a_stuck_review_form(self) -> None:
        from app.page_agent import _on_addons_page, _review_continue_blocker

        seat = (
            "Flight Selection Review & Traveller Details Add-ons Payment "
            "Skip to payment Your Flight 09:10 Guwahati 12:00 New Delhi Non-stop "
            "Seat Meal Insurance Fare Summary Total Amount ₹9,813 Mr Sudipta Saikia"
        )
        review = (
            "Review & Traveller Details Billing Address Contact Details "
            "Please enter an email Continue Total Amount ₹9,813"
        )

        class _Page:
            def __init__(self, body: str) -> None:
                self.url = "https://www.ixigo.com/flight/booking/1"
                self._body = body

            def inner_text(self, _sel: str, timeout: int = 0) -> str:
                return self._body

        self.assertTrue(_on_addons_page(seat, "review"))
        self.assertFalse(_on_addons_page(review, "review"))
        self.assertEqual(_review_continue_blocker(_Page(seat)), "")
        from app.travel_agent import _checkout_summary_open

        self.assertFalse(_checkout_summary_open(seat, "addons"))
        self.assertFalse(_checkout_summary_open(seat, "review"))
        self.assertTrue(
            _checkout_summary_open(
                "Amount To Be Paid ₹10,328 Pay via any UPI app Credit/Debit card",
                "pay",
            )
        )
        review_form = (
            "Review & Traveller Details Contact Details Mobile Number 8638586171 Email "
            "GST Details Billing Address Continue Total Amount ₹9,823 Fare Summary"
        )
        self.assertFalse(_checkout_summary_open(review_form, "review"))
        self.assertFalse(_checkout_summary_open(review_form, "pay"))
        name_check = (
            "Review Details Please ensure your name matches your govt ID "
            "Billing Address Edit Continue Fare Summary Total Amount ₹9,823"
        )
        self.assertFalse(_checkout_summary_open(name_check, "pay"))
        popup = "Review Details Edit Continue Fare Summary Total Amount ₹9,823"
        self.assertFalse(_checkout_summary_open(popup, "pay"))
        saved_list = (
            "Fare Summary Total Amount ₹9,823 Traveller Details "
            "Choose from the saved list Mr Sudipta Saikia Continue"
        )
        self.assertFalse(_checkout_summary_open(saved_list, "review"))
        self.assertFalse(_checkout_summary_open(saved_list, "pay"))
        from app.page_agent import contact_report, ixigo_filled_review, ixigo_saved_traveller_sheet

        self.assertTrue(ixigo_saved_traveller_sheet(saved_list))
        self.assertFalse(ixigo_saved_traveller_sheet(
            "Amount To Be Paid ₹10,328 Pay via any UPI app Credit/Debit card"
        ))

        self.assertTrue(ixigo_filled_review(popup))
        self.assertFalse(
            ixigo_filled_review("Amount To Be Paid ₹10,328 Pay via any UPI app Credit/Debit card")
        )
        self.assertNotEqual(
            contact_report("email:ok | phone:ok", popup),
            "I typed the contact details. Nothing paid.",
        )

    def test_payment_sheet_recommends_the_flight_on_screen(self) -> None:
        from app.travel_agent import checkout_ready_lines

        page = (
            "Fare Summary Amount To Be Paid ₹10,328 GST & Conv. fee included "
            "Yay! You saved ₹450 on this booking Your Flight Fri, 04 Dec IndiGo 6E 2174 "
            "09:10 GAU 2h 50m 12:00 DEL Non-Stop Travellers 1. Sudipta Saikia 04:20 "
            "Pay via any UPI app"
        )
        lines = checkout_ready_lines(self._ready(), page)
        self.assertIn(
            "Ready to pay on Ixigo, nothing charged. Cheapest on 4 Dec: IndiGo, "
            "leaves Guwahati 9:10 AM, lands Delhi 12:00 PM, nonstop. "
            "Total Rs 10,328 (GST and convenience fee included, after a Rs 450 saving).",
            lines[0],
        )
        self.assertIn("Approve Rs 10,328 for the IndiGo flight?", lines[-1])
        self.assertIn("I can't do UPI myself", lines[-1])
        self.assertIn("fare hold expires in about 4 minutes", lines[-1])

    def test_payment_total_beats_a_stale_fare_and_names_another_option(self) -> None:
        from app.travel_agent import checkout_ready_lines

        page = (
            "Fare Summary Amount To Be Paid ₹10,328 GST & Conv. fee included "
            "You saved ₹450 Your Flight IndiGo 6E 2174 09:10 GAU 12:00 DEL Non-Stop"
        )
        state = {
            **self._ready(),
            "direct_pick": {
                "airline": "IndiGo",
                "depart": "09:10",
                "arrive": "12:00",
                "stops": "Non-stop",
                "price_n": 8190,
            },
            "options": [
                {
                    "airline": "Air India Express",
                    "depart": "09:40",
                    "arrive": "18:00",
                    "stops": "1 stop",
                    "price_n": 10016,
                }
            ],
        }
        lines = checkout_ready_lines(state, page)
        self.assertIn("Total Rs 10,328", lines[0])
        self.assertNotIn("8,190", " ".join(lines))
        self.assertIn("Approve Rs 10,328", lines[-1])
        self.assertTrue(any("Air India Express" in line for line in lines))

    def test_a_time_after_google_asks_the_three_book_sites(self) -> None:
        from app.travel_agent import _hold_live, _live_hold_reply, _quote_time_after_search

        state = {
            "origin": "Guwahati",
            "destination": "Delhi",
            "depart": "2026-12-04",
            "seller": "google",
            "awaiting_book_site": True,
            "direct_checkout": True,
            "kernel_seller": "ixigo",
        }
        self.assertTrue(_quote_time_after_search("10:45", state))
        self.assertFalse(_quote_time_after_search("10:45 on ixigo", state))
        self.assertFalse(_quote_time_after_search("book 10:45 on ixigo", state))
        self.assertFalse(_hold_live(state, "10:45", busy=True, changed=False))
        quoted = {**state, "book_quotes_busy": True, "selected_hint": "10:45"}
        self.assertTrue(_hold_live(quoted, "10:45", busy=True, changed=False))
        self.assertIn("Cleartrip, Ixigo, and EaseMyTrip", _live_hold_reply(quoted, "10:45"))

    def test_site_quotes_are_not_discarded_at_75_seconds(self) -> None:
        import inspect

        from app.book_quotes import collect_book_quotes

        src = inspect.getsource(collect_book_quotes)
        self.assertNotIn("join(75)", src)
        self.assertIn("quotes_out", src)
        self.assertIn("join(180)", src)

    def test_naming_ixigo_with_saved_details_keeps_the_flight_and_goes_to_payment(self) -> None:
        from app.travel_agent import _arm_ixigo_payment, direct_checkout_ack, ixigo_name_is_direct_book

        from app.travel_agent import ixigo_checkout_step

        ready = {**self._ready(), "selected_hint": "10:45"}
        self.assertEqual(ixigo_checkout_step("Ixigo", ready), "login")
        self.assertFalse(ixigo_name_is_direct_book("Ixigo", ready))
        self.assertEqual(
            ixigo_checkout_step(
                "Ixigo",
                {"origin": "Guwahati", "destination": "Delhi", "depart": "2026-12-04"},
            ),
            "phone",
        )
        full = {
            **ready,
            "kernel_logged_in": "ixigo",
            "pincode": "781001",
            "billing_address": "12 Zoo Road",
            "city": "Guwahati",
            "billing_state": "Assam",
        }
        self.assertTrue(ixigo_name_is_direct_book("Ixigo", full))
        self.assertEqual(ixigo_checkout_step("Ixigo", full), "pay")
        _arm_ixigo_payment(ready, "Ixigo")
        self.assertEqual(ready.get("selected_hint"), "10:45")
        self.assertTrue(ready.get("direct_checkout"))
        spoken = direct_checkout_ack(ready)
        self.assertIn("10:45", spoken)
        self.assertIn("payment page", spoken)

    def test_new_ixigo_user_is_asked_to_log_in_then_for_billing(self) -> None:
        from app.travel_agent import (
            field_correction_line,
            ixigo_checkout_step,
            ixigo_profile_reply,
            reset_lab_traveller,
        )

        trip = {
            "origin": "Guwahati",
            "destination": "Delhi",
            "depart": "2026-12-04",
            "selected_hint": "9:40",
            "name": "Sudipta Saikia",
            "email": "sudipta@example.com",
            "phone": "9810012345",
            "book_quotes": [{"seller": "ixigo"}],
        }
        fresh = reset_lab_traveller(trip)
        self.assertEqual(fresh.get("origin"), "Guwahati")
        self.assertEqual(fresh.get("selected_hint"), "9:40")
        self.assertNotIn("name", fresh)
        self.assertNotIn("phone", fresh)
        self.assertEqual(ixigo_checkout_step("Ixigo", fresh), "phone")
        logged = {**fresh, "kernel_logged_in": "ixigo", "login_phone": "+919810012345"}
        spoken = ixigo_profile_reply(logged)
        self.assertIn("billing address", spoken)
        self.assertIn("pin code", spoken)
        self.assertIn("full name", spoken)
        rejected = field_correction_line(
            {"pincode": "537031", "email": "bad@example.com"},
            "Billing Address Pin code 537031 Please enter a valid pincode Please enter a valid email",
        )
        self.assertIn("wrong", rejected.lower())
        self.assertIn("pin code", rejected)
        self.assertIn("email", rejected)
        self.assertIn("screenshot", rejected.lower())
        missing = field_correction_line(
            {},
            "Please enter an email address Please enter a pin code Please enter your address",
        )
        self.assertIn("Still missing", missing)
        self.assertIn("email", missing)
        self.assertIn("pin code", missing)
        self.assertIn("billing address", missing)

    def test_otp_is_skipped_only_for_the_same_logged_in_number(self) -> None:
        from app.travel_agent import ixigo_session_for_number

        same = {"kernel_logged_in": "ixigo", "kernel_login_phone": "+918876233052"}
        other = {"kernel_logged_in": "ixigo", "kernel_login_phone": "+918638586171"}
        header_only = {"kernel_logged_in": "ixigo"}
        self.assertTrue(ixigo_session_for_number(same, "8876233052"))
        self.assertFalse(ixigo_session_for_number(other, "8876233052"))
        self.assertFalse(ixigo_session_for_number(header_only, "8876233052"))
        self.assertFalse(ixigo_session_for_number({}, "8876233052"))

    def test_new_user_does_not_restore_the_old_mobile_from_chat(self) -> None:
        from app.travel_agent import _recover_trip, ixigo_checkout_step, ixigo_saved_mobile, reset_lab_traveller

        fresh = reset_lab_traveller(
            {
                "origin": "Guwahati",
                "destination": "Delhi",
                "depart": "2026-12-04",
                "selected_hint": "17:40",
                "phone": "8876233052",
                "login_phone": "+918876233052",
                "name": "Sudipta Saikia",
            }
        )
        self.assertNotIn("phone", fresh)
        self.assertNotIn("login_phone", fresh)
        self.assertEqual(fresh.get("selected_hint"), "17:40")
        recovered = _recover_trip(
            fresh,
            [{"role": "user", "body": "8876233052", "created_at": "2020-01-01T00:00:00+00:00"}],
            "Ixigo",
        )
        self.assertEqual(ixigo_saved_mobile(recovered), "")
        self.assertEqual(ixigo_checkout_step("Ixigo", recovered), "phone")

    def test_logged_out_header_is_not_a_filled_account(self) -> None:
        from app.page_agent import ixigo_header_needs_login

        review = (
            "Review & Traveller Details Log in/Sign up Guwahati New Delhi "
            "17:40 Air India Express Continue"
        )
        self.assertTrue(ixigo_header_needs_login(review))
        self.assertFalse(ixigo_header_needs_login(review + " Hey Ixigoer Log out"))

    def test_saved_profile_pays_and_new_profile_logs_in_before_payment(self) -> None:
        """GHY–Delhi 4 Dec: saved login goes to pay; a new number must OTP, then details, then pay."""
        from app.intent import parse_updates
        from app.page_agent import ixigo_header_needs_login
        from app.search import place_label
        from app.travel_agent import (
            _checkout_summary_open,
            _recover_trip,
            ixigo_checkout_gaps,
            ixigo_checkout_step,
            ixigo_profile_reply,
            ixigo_session_for_number,
            reset_lab_traveller,
        )

        route = parse_updates("book a flight from ghy to delhi 4th Dec")
        self.assertEqual(place_label(str(route.get("origin") or "")), "Guwahati")
        self.assertEqual(place_label(str(route.get("destination") or "")), "Delhi")
        self.assertTrue(str(route.get("depart") or "").endswith("-12-04"))

        saved = {
            "origin": "Guwahati",
            "destination": "Delhi",
            "depart": "2026-12-04",
            "selected_hint": "17:40",
            "name": "Sudipta Saikia",
            "email": "sudipta@example.com",
            "dob": "29/07/1997",
            "gender": "Male",
            "phone": "8638586171",
            "login_phone": "+918638586171",
            "pincode": "785001",
            "billing_address": "By lane 14",
            "city": "Jorhat",
            "billing_state": "Assam",
            "kernel_logged_in": "ixigo",
            "kernel_login_phone": "+918638586171",
        }
        self.assertEqual(ixigo_checkout_gaps(saved), [])
        self.assertTrue(ixigo_session_for_number(saved, "8638586171"))
        self.assertEqual(ixigo_checkout_step("Ixigo", saved), "pay")
        self.assertEqual(ixigo_profile_reply(saved), "")
        pay = "Amount To Be Paid ₹10,328 Pay via any UPI app Credit/Debit card"
        self.assertTrue(_checkout_summary_open(pay, "pay"))

        review = (
            "Review & Traveller Details Contact Details Mobile Number 8638586171 Email "
            "GST Details Billing Address Continue Total Amount ₹9,823 Fare Summary"
        )
        self.assertFalse(_checkout_summary_open(review, "review"))
        self.assertTrue(ixigo_header_needs_login("Review & Traveller Details Log in/Sign up Continue"))

        fresh = reset_lab_traveller(saved)
        recovered = _recover_trip(
            fresh,
            [{"role": "user", "body": "8638586171", "created_at": "2020-01-01T00:00:00+00:00"}],
            "Ixigo",
        )
        self.assertEqual(ixigo_checkout_step("Ixigo", recovered), "phone")
        self.assertFalse(ixigo_session_for_number(recovered, "8876233052"))
        logging_in = {
            **recovered,
            "login_phone": "+918876233052",
            "phone": "8876233052",
        }
        self.assertEqual(ixigo_checkout_step("Ixigo", logging_in), "login")
        asked = ixigo_profile_reply(
            {**logging_in, "kernel_logged_in": "ixigo", "kernel_login_phone": "+918876233052"}
        )
        self.assertIn("full name", asked)
        self.assertIn("email", asked)
        filled = {
            **logging_in,
            "kernel_logged_in": "ixigo",
            "kernel_login_phone": "+918876233052",
            "name": "Sudipta Saikia",
            "email": "new@example.com",
            "dob": "29/07/1997",
            "gender": "Male",
            "pincode": "785001",
            "billing_address": "By lane 14",
            "city": "Jorhat",
            "billing_state": "Assam",
        }
        self.assertEqual(ixigo_checkout_gaps(filled), [])
        self.assertEqual(ixigo_checkout_step("Ixigo", filled), "pay")
        self.assertTrue(_checkout_summary_open(pay, "pay"))
        self.assertFalse(_checkout_summary_open(review, "pay"))

    def test_confirm_on_review_does_not_reopen_otp(self) -> None:
        from app.travel_agent import ixigo_should_require_otp

        review = {
            "seller": "ixigo",
            "login_phone": "+918638586171",
            "phone": "8638586171",
            "stuck_page": "Review Details Billing Address Continue Edit",
            "selected_hint": "10:45",
            "direct_checkout": True,
        }
        self.assertFalse(ixigo_should_require_otp(review, "confirm"))
        self.assertFalse(ixigo_should_require_otp(review, "ixigo"))
        from app.travel_agent import ixigo_checkout_step

        known = {
            **review,
            "origin": "Guwahati",
            "destination": "Delhi",
            "depart": "2026-12-04",
            "name": "Sudipta Saikia",
            "email": "a@b.co",
            "dob": "29/07/1997",
            "gender": "Male",
            "pincode": "785001",
            "billing_address": "By lane 14",
            "city": "Jorhat",
            "billing_state": "Assam",
        }
        self.assertEqual(ixigo_checkout_step("Ixigo", known), "pay")
        self.assertTrue(ixigo_should_require_otp(review, "8638586171"))
        self.assertTrue(
            ixigo_should_require_otp(
                {**review, "stuck_page": "Review Details Log in/Sign up Continue"},
                "confirm",
            )
        )

    def test_ixigo_decline_login_stays_on_login(self) -> None:
        from app.turn_loop import align_action

        state = {
            "seller": "ixigo",
            "kernel_seller": "ixigo",
            "kernel_auth_mode": "login",
            "origin": "Guwahati",
            "destination": "Delhi",
            "depart": "2026-12-04",
        }
        align_action("no need for login click continue", state)
        self.assertEqual(state.get("kernel_auth_mode"), "login")

    def test_lab_new_user_route_exists(self) -> None:
        from app.lab import _LAB_HTML
        from app.lab import router as lab_router

        paths = {getattr(route, "path", "") for route in lab_router.routes}
        self.assertIn("/lab/new-user", paths)
        self.assertIn("New user", _LAB_HTML)
        self.assertIn("/lab/new-user", _LAB_HTML)

    def test_wrong_ixigo_itinerary_rebooks_the_asked_time(self) -> None:
        import inspect

        from app.page_agent import run_checkout_agent

        src = inspect.getsource(run_checkout_agent)
        self.assertIn("def _rebook_requested_time", src)
        self.assertLess(src.index("rebook_hint"), src.index("return wrong"))


if __name__ == "__main__":
    unittest.main()
