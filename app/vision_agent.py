"""Screenshot vision for every booking step (Gemini). DOM still clicks; vision names the step."""

from __future__ import annotations

import logging
import re
import tempfile
from pathlib import Path
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)

_VISION_READ = (
    "You are looking at ONE screenshot of a live Indian flight-booking page "
    "(Ixigo, Cleartrip, EaseMyTrip, or similar).\n"
    "Act like Instinct: say what the page really is, list visible facts, recommend the next move.\n"
    "Return JSON only with keys:\n"
    '{"kind":"upsell|form|login|otp|review|results|pay|unavailable|addons|other",'
    '"what":"","why":"","recommend":"","tap":"","auto":false,'
    '"fares_visible":false,"sold_out":false,"toast_only":false,'
    '"seat_map":false,"meals_offered":false,'
    '"facts":[],'
    '"best_depart":"","best_price":""}\n'
    "Rules:\n"
    "- kind=results when flight cards with Book/Select and prices are visible, even if a small toast says "
    "'No flight data available' or a filter tip is pre-applied. Set fares_visible=true, toast_only=true for that chip.\n"
    "- kind=unavailable when a modal says the flight is no longer available / Search Again. sold_out=true. "
    "Do not treat this as a filled traveller form.\n"
    "- kind=addons for Choose add-ons / seat map / meals / baggage / 'missed adding Meal(s) and Seat(s)'. "
    "Set seat_map=true when a cabin seat map is visible; meals_offered=true when meals are offered. "
    "facts MUST mention seats if a seat map is shown (e.g. 'seat map open, no seat selected'). "
    "Default tap=Skip add-ons and auto=true unless the traveller clearly wants a paid seat. "
    "Never invent a seat purchase.\n"
    "- kind=results: recommend a departure time (best_depart) and price if readable; tap stays empty "
    "(the bot clicks Book by time, not a button label). auto=false.\n"
    "- kind=upsell: any optional paid extra (cancellation, insurance, flexibility, protect). "
    "Tap Continue or the decline label. Never tap Buy / Book now on that extra.\n"
    "- kind=review: Review Details name check with Confirm/Edit — tap Confirm, auto=true. "
    "Or review/traveller page with Continue and filled name — tap Continue, auto=true only if Continue is safe.\n"
    "- kind=form: empty or partial traveller fields (title/name/mobile/email) needing fill — tap empty or Continue.\n"
    "- kind=otp / login / pay as usual. kind=other for unknown sheets (still recommend a safe next move).\n"
    "- kind=pay: stop; tap empty; auto=false.\n"
    "- facts: 1-4 short phrases of what is actually visible (seat map, meals, Skip, toast, sold-out).\n"
    "- Never set tap to Pay, Buy, Secure My Trip, Assure My Trip, Insure My Trip, or anything that spends money.\n"
    "- what/why/recommend: one short sentence each. Do not invent flights not in the image."
)

_KINDS = {
    "upsell",
    "form",
    "login",
    "otp",
    "review",
    "results",
    "pay",
    "unavailable",
    "other",
    "passenger",
    "addons",
    "loading",
    "home",
    "cookie",
    "fare_sheet",
}


def vision_enabled() -> bool:
    return bool(get_settings().vision_ready)


def see_step(
    image_path: str,
    *,
    seller: str = "",
    hint: str = "",
    page_text: str = "",
) -> dict[str, Any] | None:
    """Read one booking screenshot. None when vision is off or the model returns nothing usable."""
    if not vision_enabled() or not image_path:
        return None
    from app import llm

    user = (
        f"Seller: {seller or 'unknown'}. Chosen departure: {hint or 'unknown'}.\n"
        f"DOM text (may be wrong if a toast covered the page):\n{(page_text or '')[:900]}"
    )
    data = llm.vision_converse(_VISION_READ, user, image_path, timeout=28.0)
    if not data:
        return None
    kind = str(data.get("kind") or "other").strip().lower()
    if kind == "passenger":
        kind = "form"
    if kind not in _KINDS:
        kind = "other"
    what = re.sub(r"\s+", " ", str(data.get("what") or "")).strip()
    why = re.sub(r"\s+", " ", str(data.get("why") or "")).strip()
    recommend = re.sub(r"\s+", " ", str(data.get("recommend") or "")).strip()
    if not what and not recommend and not data.get("sold_out") and not data.get("fares_visible"):
        return None
    facts_raw = data.get("facts") if isinstance(data.get("facts"), list) else []
    facts = []
    for item in facts_raw[:6]:
        line = re.sub(r"\s+", " ", str(item or "")).strip()
        if line:
            facts.append(line[:140])
    seat_map = bool(data.get("seat_map"))
    meals = bool(data.get("meals_offered"))
    if seat_map or re.search(r"choose add-ons|missed adding|add seats", what, re.I):
        kind = "addons"
    if seat_map and not any(re.search(r"seat", f, re.I) for f in facts):
        facts.append("seat map is visible")
    if meals and not any(re.search(r"meal", f, re.I) for f in facts):
        facts.append("meals are offered")
    out = {
        "kind": kind,
        "what": what[:220],
        "why": why[:220],
        "recommend": recommend[:220],
        "tap": re.sub(r"\s+", " ", str(data.get("tap") or "")).strip()[:80],
        "auto": "1" if data.get("auto") in (True, "1", "true", "True") else "",
        "fares_visible": bool(data.get("fares_visible")),
        "sold_out": bool(data.get("sold_out") or kind == "unavailable"),
        "toast_only": bool(data.get("toast_only")),
        "seat_map": seat_map,
        "meals_offered": meals,
        "facts": facts,
        "best_depart": re.sub(r"\s+", "", str(data.get("best_depart") or ""))[:8],
        "best_price": re.sub(r"\s+", " ", str(data.get("best_price") or "")).strip()[:24],
        "source": "vision",
    }
    if out["sold_out"]:
        out["kind"] = "unavailable"
    if out["fares_visible"] and out["kind"] not in {"unavailable", "pay", "login", "otp", "addons"}:
        out["kind"] = "results"
    if out["kind"] == "addons" and not out["tap"]:
        out["tap"] = "Skip add-ons"
        out["auto"] = "1"
    return out


def capture_page_shot(page) -> str:
    """Write a viewport PNG for vision. Empty string on failure."""
    if page is None or not vision_enabled():
        return ""
    import os

    name = ""
    try:
        fd, name = tempfile.mkstemp(prefix="guide-vision-", suffix=".png")
        os.close(fd)
        # Keep the empty file so Windows allows Playwright to overwrite the path.
        page.screenshot(path=name, full_page=False, timeout=12000, type="png")
        path = Path(name)
        if path.is_file() and path.stat().st_size > 200:
            return name
        path.unlink(missing_ok=True)
        return ""
    except Exception as exc:
        logger.info("vision screenshot skipped: %s", type(exc).__name__)
        if name:
            try:
                Path(name).unlink(missing_ok=True)
            except Exception:
                pass
        return ""


def classify_with_vision(
    dom_stage: str,
    image_path: str,
    *,
    seller: str = "",
    hint: str = "",
    page_text: str = "",
) -> tuple[str, dict[str, Any] | None]:
    """Override DOM stage when the screenshot clearly disagrees (every step)."""
    seen = see_step(image_path, seller=seller, hint=hint, page_text=page_text)
    if not seen:
        return dom_stage, None
    kind = seen["kind"]
    # Map vision kinds onto checkout stages.
    stage_map = {
        "unavailable": "unavailable",
        "results": "results",
        "pay": "pay",
        "login": "login",
        "otp": "otp",
        "review": "review",
        "form": "passenger",
        "upsell": "review",
        "addons": "addons",
        "loading": "loading",
        "home": "home",
        "cookie": "cookie",
        "fare_sheet": "fare_sheet",
    }
    mapped = stage_map.get(kind, dom_stage)
    if seen.get("sold_out"):
        return "unavailable", seen
    if seen.get("fares_visible") and mapped == "results":
        return "results", seen
    # Toast-only false empty: keep results if vision says so over DOM "other"/loading.
    if mapped in {"unavailable", "results", "pay", "login", "otp", "addons", "review"} and mapped != dom_stage:
        return mapped, seen
    if seen.get("kind") == "upsell":
        return mapped, seen
    if seen.get("seat_map") or (seen.get("meals_offered") and mapped == "addons"):
        return "addons", seen
    if dom_stage in {"other", "loading"} and mapped != "other":
        return mapped, seen
    return mapped, seen
