"""Open Cleartrip GAU-DEL through the India proxy. Run on the Hetzner VPS."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.browser_hands import BrowserSession  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.search import cleartrip_results_url  # noqa: E402


def main() -> int:
    settings = get_settings()
    if not settings.proxy_ready:
        print("FAIL: PROXY_HOST/PORT/USERNAME/PASSWORD not set")
        return 1
    url = cleartrip_results_url("Guwahati", "Delhi", "2026-12-04")
    print("proxy server:", settings.playwright_proxy()["server"] if settings.playwright_proxy() else None)
    print("opening:", url)
    with BrowserSession("smoke", fresh=True, headed=False, use_proxy=True) as session:
        # Cleartrip often paints the shell first; Book buttons land ~30–45s later.
        page = session.open_url(url, goto_timeout=60000, airline_wait=45000)
        if session.page is not None:
            try:
                session.page.wait_for_function(
                    """() => [...document.querySelectorAll('button,a,[role=button]')]
                        .some(b => /^book$/i.test((b.innerText || '').trim()))""",
                    timeout=60000,
                )
            except Exception:
                session.page.wait_for_timeout(5000)
            page = session.read_page()
        live = str(session.page.url if session.page else "")
        print("final url:", live)
        print("page sample:", (page or "")[:500].replace("\n", " | "))
        books = 0
        if session.page is not None:
            books = int(
                session.page.evaluate(
                    """() => [...document.querySelectorAll('button,a,[role=button]')]
                        .filter(b => /^book$/i.test((b.innerText || '').trim())).length"""
                )
                or 0
            )
        print("book_buttons:", books)
        if "cleartrip" not in live.lower():
            print("FAIL: not on Cleartrip")
            return 2
        if books < 1 and "₹" not in (page or "") and "Rs" not in (page or ""):
            print("FAIL: no Book buttons and no fares visible")
            return 3
        print("OK: Cleartrip opened on residential proxy from this host")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
