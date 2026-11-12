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
    "Act like Instinct: say what the page really is, list visible button labels, recommend the next move.\n"
    "Return JSON only with keys:\n"
    '{"kind":"upsell|form|login|otp|review|results|pay|unavailable|addons|other",'
    '"what":"","why":"","recommend":"","tap":"","auto":false,'
    '"fares_visible":false,"sold_out":false,"toast_only":false,'
    '"seat_map":false,"meals_offered":false,'
    '"controls":[],'
    '"facts":[],'
    '"errors":[],'
    '"fields":[],'
    '"best_depart":"","best_price":""}\n'
    "Rules:\n"
    "- kind is the PRIMARY sheet. A fare summary ₹ on Review Details is NOT results.\n"
    "- kind=results ONLY when a multi-row flight card list with Book/Select is the main UI. "
    "Set fares_visible=true only then. A toast 'No flight data' on a real list: fares_visible=true, toast_only=true.\n"
    "- kind=review for Review Details / name check / billing address / Review your itinerary. "
    "fares_visible=false even if Total ₹ is shown. tap=Confirm if visible else Continue.\n"
    "- kind=unavailable when a modal says the flight is no longer available / Search Again. sold_out=true.\n"
    "- kind=addons for Choose add-ons / seat map / meals / baggage / 'missed adding Meal(s) and Seat(s)'. "
    "Set seat_map=true when a cabin seat map is visible; meals_offered=true when meals are offered. "
    "Default tap=Skip add-ons and auto=true unless the traveller clearly wants a paid seat.\n"
    "- kind=results: recommend a departure time (best_depart); tap empty (bot Books by time). auto=false.\n"
    "- kind=upsell: optional paid extra. Tap Continue or the decline label. Never Buy/Book now on the extra.\n"
    "- kind=form: empty/partial traveller fields. kind=otp/login/pay as usual. kind=pay: tap empty; auto=false.\n"
    "- controls: short labels of EVERY visible primary button on this sheet "
    "(Confirm, Continue, Edit, Skip add-ons, No Thanks, Book, …). Max 8. Exact on-screen text.\n"
    "- tap MUST be one of controls (or empty). Never invent a button.\n"
    "- Never set tap to Pay, Buy, Secure My Trip, Assure My Trip, Insure My Trip, or anything that spends money.\n"
    "- facts: 1-4 short phrases. what/why/recommend: one short sentence each.\n"
    "- errors: visible validation lines, exact text (e.g. Please enter a valid pincode). Empty if none.\n"
    "- fields: labels of visible inputs that are empty or showing an error (Pin code, Address, City). Max 6."
)

_SPEND_TAP = re.compile(
    r"\b(pay now|pay to complete|make payment|buy this|buy now|secure my trip|assure my trip|insure)\b",
    re.I,
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
    controls_raw = data.get("controls") if isinstance(data.get("controls"), list) else []
    controls: list[str] = []
    for item in controls_raw[:8]:
        label = re.sub(r"\s+", " ", str(item or "")).strip()[:80]
        if label and not _SPEND_TAP.search(label) and label.lower() not in {c.lower() for c in controls}:
            controls.append(label)
    # Merge DOM-visible labels when the model omitted controls.
    if not controls and page_text:
        from app.step_read import parse_control_labels

        controls = parse_control_labels(page_text)[:8]
    tap = re.sub(r"\s+", " ", str(data.get("tap") or "")).strip()[:80]
    if _SPEND_TAP.search(tap):
        tap = ""
    out = {
        "kind": kind,
        "what": what[:220],
        "why": why[:220],
        "recommend": recommend[:220],
        "tap": tap,
        "auto": "1" if data.get("auto") in (True, "1", "true", "True") else "",
        "fares_visible": bool(data.get("fares_visible")),
        "sold_out": bool(data.get("sold_out") or kind == "unavailable"),
        "toast_only": bool(data.get("toast_only")),
        "seat_map": seat_map,
        "meals_offered": meals,
        "controls": controls,
        "facts": facts,
        "errors": [
            re.sub(r"\s+", " ", str(item or "")).strip()[:120]
            for item in (data.get("errors") if isinstance(data.get("errors"), list) else [])[:4]
            if str(item or "").strip()
        ],
        "fields": [
            re.sub(r"\s+", " ", str(item or "")).strip()[:60]
            for item in (data.get("fields") if isinstance(data.get("fields"), list) else [])[:6]
            if str(item or "").strip()
        ],
        "best_depart": re.sub(r"\s+", "", str(data.get("best_depart") or ""))[:8],
        "best_price": re.sub(r"\s+", " ", str(data.get("best_price") or "")).strip()[:24],
        "source": "vision",
    }
    if out["sold_out"]:
        out["kind"] = "unavailable"
    # Review / form / upsell must never be wiped to results because a Total ₹ is visible.
    if out["fares_visible"] and out["kind"] not in {
        "unavailable",
        "pay",
        "login",
        "otp",
        "addons",
        "review",
        "form",
        "upsell",
        "passenger",
    }:
        out["kind"] = "results"
    elif out["kind"] in {"review", "form", "upsell", "passenger"}:
        out["fares_visible"] = False
    # DOM text can correct a wrong vision kind on Review Details.
    if re.search(r"\breview details\b|review your itinerary", page_text or "", re.I) and out["kind"] in {
        "results",
        "other",
        "loading",
        "",
    }:
        out["kind"] = "review"
        out["fares_visible"] = False
    if out["kind"] == "addons" and not out["tap"] and not re.search(r"review your itinerary", page_text or "", re.I):
        out["tap"] = "Skip add-ons"
        out["auto"] = "1"
    from app.step_read import addons_sheet, ground_tap

    if addons_sheet(page_text) and re.search(r"skip add-?ons|choose add-ons", page_text or "", re.I):
        out["kind"] = "addons"
        out["what"] = "Choose add-ons"
        out["why"] = "seats and bags are optional"
        out["recommend"] = "press Skip add-ons"
        out["tap"] = "Skip add-ons"
        out["auto"] = "1"
    elif re.search(r"\bcontinue\b", page_text or "", re.I) and not re.search(r"skip add-?ons", page_text or "", re.I):
        if out["kind"] == "addons":
            out["kind"] = "review"
        out["tap"] = out["tap"] or "Continue"
        out["recommend"] = out["recommend"] or "press Continue"
        out["auto"] = "1"
    # Ground tap to controls / page — never invent.
    grounded = ground_tap(out["tap"], out["controls"], page_text, kind=out["kind"])
    out["tap"] = grounded
    if grounded and grounded.lower() not in {c.lower() for c in out["controls"]}:
        out["controls"] = [grounded, *[c for c in out["controls"] if c.lower() != grounded.lower()]][:8]
    return out


def _clear_page_for_user_shot(page) -> None:
    """Drop text selection, focus rings, and Guide overlays so traveller shots stay readable.

    Internal vision may still call settle/scroll separately — this only cleans paint.
    """
    try:
        page.evaluate(
            """() => {
              try { window.getSelection() && window.getSelection().removeAllRanges(); } catch (e) {}
              const ae = document.activeElement;
              if (ae && typeof ae.blur === 'function') ae.blur();
              // Remove any prior Guide debug overlays (never leave them for the traveller).
              document.querySelectorAll(
                '[data-guide-hl],[data-guide-debug-overlay],[data-guide-fill],[data-guide-temp]'
              ).forEach((el) => {
                if (el.hasAttribute('data-guide-fill') || el.hasAttribute('data-guide-temp')) {
                  el.style.outline = '';
                  el.removeAttribute('data-guide-fill');
                  el.removeAttribute('data-guide-temp');
                } else {
                  el.remove();
                }
              });
              // Neutralize selection paint if a prior read left a range selected.
              let style = document.getElementById('guide-user-shot-style');
              if (!style) {
                style = document.createElement('style');
                style.id = 'guide-user-shot-style';
                style.setAttribute('data-guide-debug-overlay', '1');
                style.textContent = '::selection,::-moz-selection{background:transparent!important;color:inherit!important;}';
                (document.head || document.documentElement).appendChild(style);
              }
            }"""
        )
    except Exception:
        pass


def capture_page_shot(page) -> str:
    """Write a viewport PNG for *internal* vision. May settle/scroll the sheet first.

    Not for WhatsApp/lab delivery — use capture_user_shot for traveller-facing images.
    """
    if page is None or not vision_enabled():
        return ""
    import os

    name = ""
    try:
        fd, name = tempfile.mkstemp(prefix="guide-vision-", suffix=".png")
        os.close(fd)
        # Keep the empty file so Windows allows Playwright to overwrite the path.
        try:
            from app.page_agent import settle_visible_sheet

            settle_visible_sheet(page)
        except Exception:
            pass
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


def capture_user_shot(page, *, dest: str | Path = "", tag: str = "user") -> str:
    """Clean traveller-facing viewport shot — no debug overlays, no text selection.

    Higher JPEG quality than Kernel OTP thumbs so prices and labels stay legible.
    Does not settle or scroll the sheet; that scroll is for internal vision only.
    """
    if page is None:
        return ""
    import os

    path_str = ""
    try:
        _clear_page_for_user_shot(page)
        try:
            page.wait_for_timeout(120)
        except Exception:
            pass
        if dest:
            path = Path(dest)
            path.parent.mkdir(parents=True, exist_ok=True)
        else:
            from app.media_files import ensure_media_dir

            path = ensure_media_dir() / f"{tag}-{os.getpid()}.jpg"
        # Prefer JPEG for WhatsApp size limits; quality high enough to read ₹ amounts.
        kwargs = {
            "path": str(path),
            "full_page": False,
            "timeout": 12000,
            "type": "jpeg",
            "quality": 92,
        }
        try:
            page.screenshot(**kwargs, animations="disabled", caret="hide")
        except TypeError:
            page.screenshot(**kwargs)
        if path.is_file() and path.stat().st_size > 200:
            return str(path)
        path.unlink(missing_ok=True)
        return ""
    except Exception as exc:
        logger.info("user screenshot skipped: %s", type(exc).__name__)
        if path_str:
            try:
                Path(path_str).unlink(missing_ok=True)
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
    from app.page_agent import site_error_page
    from app.step_read import addons_sheet

    if site_error_page(page_text or "", ""):
        seen["kind"] = "error"
        seen["what"] = "site system error"
        seen["recommend"] = "start fresh or search again"
        seen["tap"] = ""
        return "error", seen
    if re.search(r"review your itinerary", page_text or "", re.I):
        seen["kind"] = "review"
        seen["tap"] = "Continue" if re.search(r"\bcontinue\b", page_text or "", re.I) else seen.get("tap") or ""
        return "review", seen
    if addons_sheet(page_text) and re.search(r"skip add-?ons|choose add-ons|add seats", page_text or "", re.I):
        seen["kind"] = "addons"
        if re.search(r"skip add-?ons", page_text or "", re.I):
            seen["tap"] = "Skip add-ons"
        elif re.search(r"\bcontinue\b", page_text or "", re.I):
            seen["tap"] = "Continue"
        seen["auto"] = "1"
        return "addons", seen
    if seen.get("sold_out"):
        return "unavailable", seen
    # A header Log in/Sign up must not rename the open fare/review sheet.
    if mapped == "login" and re.search(
        r"fare summary|review & traveller|refund on cancellation|add free cancellation|total amount",
        page_text or "",
        re.I,
    ) and re.search(r"\bcontinue\b", page_text or "", re.I) and not re.search(
        r"enter otp|otp sent|enter mobile number|log in to ixigo|get otp",
        page_text or "",
        re.I,
    ):
        seen["kind"] = "review"
        seen["tap"] = seen.get("tap") or "Continue"
        return "review", seen
    # Review / form / upsell / passenger never become results via fares_visible.
    if kind in {"review", "form", "upsell", "passenger"}:
        return mapped, seen
    if seen.get("fares_visible") and mapped == "results" and kind not in {
        "review",
        "form",
        "upsell",
        "passenger",
        "addons",
    }:
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
