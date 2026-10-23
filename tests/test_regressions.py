"""Locks the WhatsApp failures: wrong month, forgotten trip, blank pages, re-asking."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from app.browser_hands import (
    BrowserSession,
    _alt_clock,
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


if __name__ == "__main__":
    unittest.main()
