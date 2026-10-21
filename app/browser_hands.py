"""Live Chromium. DeepSeek decides what to open; this only carries it out."""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import socket
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_PAY = re.compile(
    r"\b(pay|pay now|make payment|place order|purchase|complete payment|submit payment|pay securely)\b",
    re.I,
)
_CARD_FIELD = re.compile(r"\b(card|cvv|cvc|otp|pan|upi pin)\b", re.I)
_PRICE = re.compile(
    r"(?:₹|Rs\.?|INR)\s*([0-9]{1,3}(?:,[0-9]{2,3})+|[0-9]{4,6})",
    re.I,
)
_BLOCK = re.compile(
    r"captcha|not a robot|unusual traffic|access denied|security check|automated traffic|"
    r"pardon our interruption|please verify you are",
    re.I,
)
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
_LAUNCH_ARGS = ["--disable-dev-shm-usage", "--no-sandbox", "--disable-gpu"]
REFUSE_PAY = "Refused. That button would take payment. Leave it unclicked and tell the traveller the total."
REFUSE_CARD = "Refused. Card numbers, CVV, and OTP are not typed or saved."


def chromium_ready() -> bool:
    """True when a Chromium binary is already on disk. A miss is not cached."""
    if getattr(chromium_ready, "_ok", False):
        return True
    root = _find_browser_root()
    if root is None:
        return False
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(root)
    setattr(chromium_ready, "_ok", True)
    logger.info("Chromium found at %s", root)
    return True


def ensure_chromium() -> None:
    """Download Chromium onto the persistent disk if the build did not ship it."""
    if chromium_ready():
        return
    dest = _install_dir()
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("Cannot create Chromium dir %s: %s", dest, exc)
        return
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(dest)
    logger.info("Installing Chromium into %s", dest)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            timeout=600,
            check=False,
        )
    except Exception as exc:
        logger.warning("Chromium install failed: %s", type(exc).__name__)
        return
    if result.returncode != 0:
        logger.warning("Chromium install exited %s", result.returncode)
        return
    setattr(chromium_ready, "_ok", False)
    chromium_ready()


def _install_dir() -> Path:
    data = Path("/var/data")
    if data.is_dir() and os.access(data, os.W_OK):
        return data / "ms-playwright"
    return Path(__file__).resolve().parents[1] / ".playwright"


def _find_browser_root() -> Path | None:
    roots: list[Path] = []
    env = (os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "").strip()
    if env and env != "0":
        roots.append(Path(env))
    app_root = Path(__file__).resolve().parents[1]
    roots.extend(
        [
            _install_dir(),
            app_root / ".playwright",
            Path("/opt/render/project/src/.playwright"),
            Path.home() / ".cache" / "ms-playwright",
            Path.home() / "AppData" / "Local" / "ms-playwright",
        ]
    )
    seen: set[str] = set()
    for root in roots:
        key = str(root)
        if key in seen:
            continue
        seen.add(key)
        if _has_chromium(root):
            return root
    return None


def _has_chromium(root: Path) -> bool:
    if not root.is_dir():
        return False
    for name in ("chrome", "chrome.exe", "headless_shell"):
        if any(root.glob(f"chromium*/**/{name}")):
            return True
    return False


def prices_in(text: str) -> list[int]:
    found: list[int] = []
    for raw in _PRICE.findall(text or ""):
        value = int(raw.replace(",", ""))
        if 1500 <= value <= 200000 and value not in found:
            found.append(value)
    return found[:12]


def payment_control(label: str) -> bool:
    return bool(_PAY.search(label or ""))


def page_blocked(text: str) -> bool:
    return bool(_BLOCK.search(text or ""))


def looks_like_card(value: str) -> bool:
    digits = re.sub(r"\D", "", value or "")
    return 13 <= len(digits) <= 19 and len(digits) >= len(re.sub(r"\s", "", value or "")) - 2


def url_allowed(url: str, *, allow_local: bool = False) -> str | None:
    """Public http(s) only. Blocks local and private hosts."""
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if len(url) > 600:
        return None
    host = parsed.hostname.lower()
    if allow_local and host in {"127.0.0.1", "localhost"}:
        return url
    if host in {"localhost", "metadata.google.internal"} or host.endswith(".local"):
        return None
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror:
            return None
        addresses = []
        for info in infos:
            try:
                addresses.append(ipaddress.ip_address(info[4][0]))
            except ValueError:
                continue
    if not addresses or any(not ip.is_global for ip in addresses):
        return None
    return url


def _prefer_render_browsers() -> None:
    root = _find_browser_root()
    if root is not None:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(root)


def _profile_dir(phone: str) -> Path:
    from app.config import get_settings

    root = Path(get_settings().guide_db_path).parent / "browser-profiles"
    safe = re.sub(r"\W", "", phone or "")[:20] or "anon"
    path = root / safe
    path.mkdir(parents=True, exist_ok=True)
    return path


def _shot_path() -> Path:
    from app.media_files import ensure_media_dir

    return ensure_media_dir() / f"shot-{uuid.uuid4().hex[:12]}.jpg"


class BrowserSession:
    def __init__(self, phone: str = ""):
        self.phone = phone
        self.unsent: list[Path] = []
        self.last_text = ""
        self.allow_local = False
        self._pw: object | None = None
        self._browser: object | None = None
        self._context: object | None = None
        self.page: object | None = None

    def __enter__(self) -> "BrowserSession":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def start(self) -> None:
        _prefer_render_browsers()
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        playwright = self._pw
        kwargs = {
            "headless": True,
            "args": _LAUNCH_ARGS,
            "viewport": {"width": 1200, "height": 1400},
            "user_agent": _UA,
        }
        try:
            self._context = playwright.chromium.launch_persistent_context(str(_profile_dir(self.phone)), **kwargs)
            self.page = self._context.pages[0] if self._context.pages else self._context.new_page()
        except Exception:
            logger.warning("Persistent browser profile failed; opening a fresh window")
            self._browser = playwright.chromium.launch(headless=True, args=_LAUNCH_ARGS)
            self._context = self._browser.new_context(viewport=kwargs["viewport"], user_agent=_UA)
            self.page = self._context.new_page()
        self.page.set_default_timeout(20000)
        self._context.route("**/*", self._gate)

    def close(self) -> None:
        for obj in (self._context, self._browser):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass
        self.page = None

    def _gate(self, route: object) -> None:
        url = route.request.url
        if url.startswith(("about:", "data:", "blob:")) or url_allowed(url, allow_local=self.allow_local):
            route.continue_()
            return
        route.abort()

    def open_url(self, url: str) -> str:
        allowed = url_allowed(url, allow_local=self.allow_local)
        if not allowed:
            return "That address is not a public web page, so it was not opened."
        assert self.page is not None
        self.page.goto(allowed, wait_until="domcontentloaded", timeout=45000)
        self._dismiss()
        try:
            self.page.wait_for_selector("text=/₹|Rs\\.?\\s*\\d|INR\\s*\\d/", timeout=8000)
        except Exception:
            self.page.wait_for_timeout(2500)
        return self._observe(shoot=True)

    def read_page(self) -> str:
        return self._observe(shoot=False)

    def click(self, text: str) -> str:
        label = (text or "").strip()
        if not label:
            return "Need the visible text of the button or link."
        if payment_control(label):
            return REFUSE_PAY
        locator = self._find_clickable(label)
        if locator is None:
            return f"Could not find a button or link named {label}."
        try:
            visible = locator.inner_text(timeout=2000)
        except Exception:
            visible = label
        if payment_control(visible):
            return REFUSE_PAY
        locator.click(timeout=8000)
        self.page.wait_for_timeout(1200)
        return self._observe(shoot=True)

    def press(self, key: str) -> str:
        if key not in {"Enter", "Tab", "Escape"}:
            return "Only Enter, Tab, or Escape."
        assert self.page is not None
        self.page.keyboard.press(key)
        self.page.wait_for_timeout(800)
        return self._observe(shoot=True)

    def fill(self, label: str, value: str) -> str:
        if _CARD_FIELD.search(label or "") or looks_like_card(value):
            return REFUSE_CARD
        if not (label or "").strip() or not (value or "").strip():
            return "Need both a field label and a value."
        if not self._fill_first([label], value):
            return f"Could not find a field labeled {label}."
        return f"Filled {label}."

    def fill_saved_login(self, username: str, password: str) -> str:
        if not username or not password:
            return "No saved login for that site. Ask the traveller for the email and password."
        assert self.page is not None
        password_box = self.page.locator("input[type=password]")
        if password_box.count() == 0:
            return "No password field is visible yet."
        typed_user = False
        for css in ("input[type=email]", "input[type=text]"):
            box = self.page.locator(css)
            if box.count() and self._visible(box.first):
                box.first.fill(username)
                typed_user = True
                break
        password_box.first.fill(password)
        note = "Typed the saved login. The password is not in this result."
        if not typed_user:
            note = "Typed the password. No username field was visible."
        for name in ("Log in", "Login", "Sign in", "Continue"):
            if payment_control(name):
                continue
            locator = self._find_clickable(name)
            if locator is None:
                continue
            try:
                visible = locator.inner_text(timeout=1000)
            except Exception:
                visible = name
            if payment_control(visible):
                continue
            locator.click(timeout=5000)
            self.page.wait_for_timeout(1500)
            break
        return note + "\n" + self._observe(shoot=True)

    def fill_saved_profile(self, profile: dict[str, str]) -> str:
        notes: list[str] = []
        name = (profile.get("name") or "").strip()
        email = (profile.get("email") or "").strip()
        phone = (profile.get("phone") or "").strip()
        dob = (profile.get("dob") or "").strip()
        gender = (profile.get("gender") or "").strip()
        if email and not self._fill_first(["Email", "E-mail"], email, css="input[type=email]"):
            notes.append("Email field not found.")
        elif email:
            notes.append("Filled email.")
        if phone and looks_like_card(phone):
            notes.append(REFUSE_CARD)
        elif phone and not self._fill_first(["Mobile", "Phone", "Contact number"], phone, css="input[type=tel]"):
            notes.append("Phone field not found.")
        elif phone:
            notes.append("Filled phone.")
        if name:
            self._fill_first(["Full name", "Traveller name", "Name"], name)
            parts = name.split()
            if len(parts) >= 2:
                self._fill_first(["First name"], parts[0])
                self._fill_first(["Last name", "Surname"], " ".join(parts[1:]))
            notes.append("Filled name.")
        if dob and not self._fill_first(["Date of birth", "DOB", "Birth date"], dob):
            notes.append("Date of birth field not found.")
        elif dob:
            notes.append("Filled date of birth.")
        if gender:
            try:
                assert self.page is not None
                self.page.get_by_text(re.compile(rf"^{re.escape(gender)}$", re.I)).first.click(timeout=2000)
                notes.append(f"Selected {gender}.")
            except Exception:
                notes.append(f"Could not select {gender}.")
        return " ".join(notes) or "No saved passenger details to type."

    def _fill_first(self, labels: list[str], value: str, css: str = "") -> bool:
        assert self.page is not None
        for label in labels:
            for getter in (
                lambda label=label: self.page.get_by_label(label, exact=False),
                lambda label=label: self.page.get_by_placeholder(re.compile(re.escape(label), re.I)),
            ):
                try:
                    locator = getter()
                    if locator.count() and self._visible(locator.first):
                        locator.first.fill(value, timeout=4000)
                        return True
                except Exception:
                    continue
        if css:
            try:
                locator = self.page.locator(css)
                if locator.count() and self._visible(locator.first):
                    locator.first.fill(value, timeout=4000)
                    return True
            except Exception:
                return False
        return False

    def _find_clickable(self, text: str):
        assert self.page is not None
        pattern = re.compile(re.escape(text), re.I)
        for role in ("button", "link"):
            locator = self.page.get_by_role(role, name=pattern)
            if locator.count() and self._visible(locator.first):
                return locator.first
        locator = self.page.get_by_text(pattern)
        if locator.count() and self._visible(locator.first):
            return locator.first
        return None

    def _visible(self, locator: object) -> bool:
        try:
            return bool(locator.is_visible())
        except Exception:
            return False

    def _dismiss(self) -> None:
        assert self.page is not None
        for label in ("Accept", "I agree", "Got it", "OK", "Close"):
            try:
                button = self.page.get_by_role("button", name=re.compile(rf"^{label}$", re.I))
                if button.count() and button.first.is_visible():
                    button.first.click(timeout=800)
            except Exception:
                continue

    def _observe(self, *, shoot: bool) -> str:
        assert self.page is not None
        try:
            text = self.page.inner_text("body", timeout=8000)
        except Exception:
            text = ""
        self.last_text = text or ""
        try:
            title = self.page.title()
        except Exception:
            title = ""
        prices = prices_in(self.last_text)
        blocked = bool(_BLOCK.search(self.last_text))
        flag = ""
        if blocked:
            flag = (
                "\nThis page looks like a bot check or an access block. "
                "Open a different seller now. Do not ask the traveller to repeat the trip."
            )
        elif not prices:
            flag = (
                "\nNO FARES RENDERED. Open a different seller in this same run. "
                "Do not ask the traveller for the route, date, or preference."
            )
            shoot = False
        shot_note = ""
        if shoot:
            path = self._screenshot()
            if path is not None:
                self.unsent.append(path)
                shot_note = "\nScreenshot captured."
        body = re.sub(r"\n{3,}", "\n\n", self.last_text).strip()[:3500]
        price_line = f"\nPrices visible: {prices}" if prices else ""
        return f"URL: {self.page.url}\nTitle: {title}{price_line}{flag}\n{body}{shot_note}"

    def _screenshot(self) -> Path | None:
        assert self.page is not None
        path = _shot_path()
        try:
            self.page.screenshot(path=str(path), type="jpeg", quality=55, full_page=False)
        except Exception as exc:
            logger.warning("Screenshot failed: %s", type(exc).__name__)
            return None
        return path if path.is_file() else None


def read_url(url: str) -> str:
    """Open one public page and return its text. Used by the fare watcher."""
    allowed = url_allowed(url)
    if not allowed:
        return ""
    session = BrowserSession("watch")
    try:
        session.start()
        observed = session.open_url(allowed)
    except Exception as exc:
        logger.warning("Fare recheck failed: %s", type(exc).__name__)
        return ""
    finally:
        session.close()
    return observed
