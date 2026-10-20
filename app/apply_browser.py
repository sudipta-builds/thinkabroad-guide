from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_MEDIA = Path("data/media")


@dataclass
class ApplyResult:
    ok: bool
    message: str
    screenshot_path: Path | None = None
    final_url: str = ""


def apply_to_job(
    *,
    apply_url: str,
    cv_pdf: Path | None,
    cv_docx: Path | None,
    cover: str,
    profile: dict[str, str],
    credentials: dict[str, str] | None = None,
) -> ApplyResult:
    """Open the apply page in Chromium and submit where the form is fillable.

    Supports common Greenhouse / Lever patterns and a generic best-effort form fill.
    Requires Playwright + Chromium installed on the host (see Render build).
    """
    apply_url = (apply_url or "").strip()
    if not apply_url.startswith("http"):
        return ApplyResult(False, "Missing apply URL.")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return ApplyResult(False, "Playwright not installed on server.")

    name = (profile.get("name") or "Candidate").strip()
    email = (profile.get("email") or "").strip()
    phone = (profile.get("phone") or "").strip()
    linkedin = (profile.get("linkedin") or "").strip()
    resume = cv_pdf if cv_pdf and cv_pdf.is_file() else cv_docx
    if not email:
        return ApplyResult(False, "Need an email first. Send: SAVE PROFILE Your Name | you@email.com | +phone")

    host = urlparse(apply_url).netloc.lower()
    _MEDIA.mkdir(parents=True, exist_ok=True)
    shot = _MEDIA / f"apply-{uuid.uuid4().hex[:10]}.jpg"

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                ),
            )
            page = context.new_page()
            page.goto(apply_url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(1500)

            # Optional login if credentials provided and page looks gated
            if credentials and credentials.get("username") and credentials.get("password"):
                _try_login(page, credentials["username"], credentials["password"])

            if "greenhouse" in host or "boards.greenhouse" in host:
                ok, msg = _fill_greenhouse(page, name, email, phone, linkedin, cover, resume)
            elif "lever.co" in host:
                ok, msg = _fill_lever(page, name, email, phone, linkedin, cover, resume)
            else:
                ok, msg = _fill_generic(page, name, email, phone, linkedin, cover, resume)

            page.wait_for_timeout(1200)
            page.screenshot(path=str(shot), full_page=False, type="jpeg", quality=70)
            final = page.url
            browser.close()
            return ApplyResult(ok, msg, screenshot_path=shot if shot.is_file() else None, final_url=final)
    except Exception as exc:
        logger.exception("Browser apply failed")
        return ApplyResult(False, f"Apply automation error: {type(exc).__name__}")


def _try_login(page: Any, username: str, password: str) -> None:
    for sel_user in ['input[type="email"]', 'input[name="session_key"]', 'input[name="username"]', "#username"]:
        if page.locator(sel_user).count():
            page.locator(sel_user).first.fill(username)
            break
    for sel_pass in ['input[type="password"]', 'input[name="session_password"]', "#password"]:
        if page.locator(sel_pass).count():
            page.locator(sel_pass).first.fill(password)
            break
    for sel_btn in ['button[type="submit"]', 'button:has-text("Sign in")', 'button:has-text("Log in")']:
        if page.locator(sel_btn).count():
            page.locator(sel_btn).first.click()
            page.wait_for_timeout(2000)
            break


def _fill_greenhouse(
    page: Any, name: str, email: str, phone: str, linkedin: str, cover: str, resume: Path | None
) -> tuple[bool, str]:
    _fill_by_labelish(page, name, email, phone, linkedin, cover)
    if resume:
        _upload_resume(page, resume)
    return _click_submit(page, labels=("Submit application", "Submit", "Apply"))


def _fill_lever(
    page: Any, name: str, email: str, phone: str, linkedin: str, cover: str, resume: Path | None
) -> tuple[bool, str]:
    # Lever often has Apply button first
    for sel in ['a:has-text("Apply")', 'button:has-text("Apply")', 'a.postings-btn']:
        if page.locator(sel).count():
            try:
                page.locator(sel).first.click()
                page.wait_for_timeout(1000)
            except Exception:
                pass
            break
    _fill_by_labelish(page, name, email, phone, linkedin, cover)
    if resume:
        _upload_resume(page, resume)
    return _click_submit(page, labels=("Submit application", "Submit", "Send application", "Apply"))


def _fill_generic(
    page: Any, name: str, email: str, phone: str, linkedin: str, cover: str, resume: Path | None
) -> tuple[bool, str]:
    _fill_by_labelish(page, name, email, phone, linkedin, cover)
    if resume:
        _upload_resume(page, resume)
    return _click_submit(page, labels=("Submit", "Apply", "Send application", "Submit application"))


def _fill_by_labelish(
    page: Any, name: str, email: str, phone: str, linkedin: str, cover: str
) -> None:
    mapping = [
        (["full name", "full_name", "name", "applicant.name"], name),
        (["email", "e-mail"], email),
        (["phone", "mobile", "tel"], phone),
        (["linkedin", "linked-in", "linked_in"], linkedin),
        (["cover", "cover letter", "cover_letter", "additional", "message", "comments"], cover),
    ]
    # name / email / phone common ids
    _try_fill(page, ['input[name*="name" i]', "#first_name", 'input[autocomplete="name"]'], name)
    parts = name.split(None, 1)
    if len(parts) == 2:
        _try_fill(page, ['input[name*="first" i]', "#first_name"], parts[0])
        _try_fill(page, ['input[name*="last" i]', "#last_name"], parts[1])
    _try_fill(page, ['input[type="email"]', 'input[name*="email" i]'], email)
    if phone:
        _try_fill(page, ['input[type="tel"]', 'input[name*="phone" i]'], phone)
    if linkedin:
        _try_fill(page, ['input[name*="linkedin" i]', 'input[placeholder*="LinkedIn" i]'], linkedin)
    if cover:
        for sel in ["textarea", 'div[contenteditable="true"]']:
            locs = page.locator(sel)
            if locs.count():
                try:
                    locs.first.fill(cover[:4000])
                    break
                except Exception:
                    try:
                        locs.first.click()
                        page.keyboard.type(cover[:1500])
                        break
                    except Exception:
                        continue
    # label-based fill for leftovers
    for keywords, value in mapping:
        if not value:
            continue
        for kw in keywords:
            try:
                loc = page.get_by_label(re.compile(kw, re.I), exact=False)
                if loc.count():
                    loc.first.fill(value[:4000])
                    break
            except Exception:
                continue


def _try_fill(page: Any, selectors: list[str], value: str) -> None:
    if not value:
        return
    for sel in selectors:
        try:
            loc = page.locator(sel)
            if loc.count():
                loc.first.fill(value)
                return
        except Exception:
            continue


def _upload_resume(page: Any, resume: Path) -> None:
    for sel in ['input[type="file"]', 'input[name*="resume" i]', 'input[name*="cv" i]']:
        try:
            loc = page.locator(sel)
            if loc.count():
                loc.first.set_input_files(str(resume))
                page.wait_for_timeout(800)
                return
        except Exception:
            continue


def _click_submit(page: Any, labels: tuple[str, ...]) -> tuple[bool, str]:
    for label in labels:
        try:
            btn = page.get_by_role("button", name=re.compile(label, re.I))
            if btn.count():
                btn.first.click()
                page.wait_for_timeout(2500)
                return True, f"Submitted (clicked “{label}”)."
        except Exception:
            continue
        try:
            link = page.get_by_role("link", name=re.compile(label, re.I))
            if link.count():
                link.first.click()
                page.wait_for_timeout(2500)
                return True, f"Submitted (clicked “{label}”)."
        except Exception:
            continue
    # last resort CSS
    for sel in ['button[type="submit"]', 'input[type="submit"]']:
        try:
            if page.locator(sel).count():
                page.locator(sel).first.click()
                page.wait_for_timeout(2500)
                return True, "Submitted (form submit control)."
        except Exception:
            continue
    return False, "Opened the page and filled what I could, but no clear Submit button — check the screenshot."
