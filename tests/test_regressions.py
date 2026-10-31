"""Locks the WhatsApp failures: wrong month, forgotten trip, blank pages, re-asking."""

from __future__ import annotations

import os
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
        self.assertIn("No, Thanks", cancel)
        self.assertIn("skip", cancel.lower())
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
        self.assertIn("saved traveller", radio.lower())
        self.assertIn("skip", radio.lower())
        assured = _booking_page_reply(
            {**booking, "selected_hint": "12:30"},
            "12:30",
            "Assured Free Cancellation only ₹1,599 With Assured Without Assured No, Thanks Secure My Trip",
            _Session(),  # type: ignore[arg-type]
        )
        self.assertIn("Assured", assured)
        self.assertIn("No, Thanks", assured)
        self.assertIn("skip", assured.lower())
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
        self.assertEqual(_addon_upsell(behind), "radio")
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
        fill = inspect.getsource(run_checkout_agent).split('if kind == "fill_passenger"')[1][:1600]
        self.assertLess(fill.index("kept_saved_traveller"), fill.index("_fill_passenger_fields"))
        self.assertIn("behind_sheet", fill)

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
        self.assertIn("_open_browser(client, browser_profile)", source)

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
        # A leftover Ixigo preference must not steal a bare "1:15" after Google fares.
        with_fares["preferred_seller"] = "ixigo"
        with_fares["seller"] = "ixigo"
        self.assertEqual(_booking_seller_id(with_fares, "1:15"), "cleartrip")
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

        self.assertIn("in the boxes", otp_failure_detail(boxes_open=True))
        self.assertIn("Log in", otp_failure_detail(boxes_open=False))
        self.assertIn("checking", otp_failure_detail(boxes_open=False, verifying=True))
        waiting_live = {"kernel_awaiting_otp": True, "kernel_session_id": "sess", "kernel_seller": "ixigo"}
        self.assertEqual(resume_kernel_otp(waiting_live, "1234"), "need_digits")
        self.assertEqual(resume_kernel_otp(waiting_live, "123456"), "closed")

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
        self.assertIn("continue|confirm|proceed", click)
        self.assertIn("_ixigo_buy_this_visible", click)
        self.assertNotIn("pay now", click.lower())
        opt = inspect.getsource(_ixigo_opt_out_cancellation)
        self.assertIn("buy this", opt.lower())
        self.assertIn("continue without", opt.lower())
        self.assertIn("don.?t want Free Cancellation", inspect.getsource(_select_saved_traveller) + inspect.getsource(_ixigo_opt_out_cancellation))
        pick = inspect.getsource(_select_saved_traveller)
        self.assertIn("input[type=checkbox]", pick)
        review = inspect.getsource(run_checkout_agent)
        self.assertIn("Escape closes the Ixigo review sheet", review)
        self.assertIn("One accurate click per attempt", review)
        self.assertIn('sum(1 for line in log if "review_continue" in line) < 3', review)
        self.assertNotIn('{"action": "done", "target": "review"', review)
        self.assertNotIn("_dismiss_overlays(page)", review.split("review_continue")[1][:700])
        handler = review.split('if kind == "review_continue"')[1][:2200]
        self.assertEqual(handler.count("_safe_continue(page)"), 1)
        self.assertIn("_ixigo_opt_out_cancellation", handler)
        self.assertIn("_select_saved_traveller", handler)
        self.assertIn("_ixigo_buy_this_visible", handler)
        self.assertIn("_pause_unknown", handler)
        self.assertLess(handler.index("_select_saved_traveller"), handler.index("_safe_continue(page)"))
        self.assertLess(handler.index("_pause_unknown"), handler.index("_safe_continue(page)"))


if __name__ == "__main__":
    unittest.main()
