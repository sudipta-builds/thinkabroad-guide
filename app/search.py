from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlparse

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)


def find_sponsored_jobs(country: str, role: str = "", *, limit: int = 5) -> list[dict[str, Any]]:
    """Live Brave Search for sponsor-friendly roles. No seeded catalog, no other engines."""
    country = (country or "").strip()
    role = (role or "").strip() or "jobs"
    query = f"{role} job visa sponsorship OR skilled worker OR work permit {country}"

    hits = brave_web_search(query, count=max(limit * 2, 8))
    out: list[dict[str, Any]] = []
    for hit in hits:
        url = hit.get("url") or ""
        title = hit.get("title") or "Job"
        if not url:
            continue
        out.append(
            {
                "id": _job_id(url),
                "title": title[:160],
                "company": _guess_company(title, url),
                "country": country.lower(),
                "city": "",
                "apply_url": url,
                "sponsor_status": "likely",
                "tags": [],
                "description": (hit.get("description") or "")[:500],
                "source": "brave",
            }
        )
        if len(out) >= limit:
            break
    return out


def brave_web_search(query: str, *, count: int = 8) -> list[dict[str, str]]:
    settings = get_settings()
    if not settings.brave_api_key:
        logger.warning("BRAVE_API_KEY missing")
        return []
    try:
        with httpx.Client(timeout=20.0) as client:
            resp = client.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "count": count},
                headers={
                    "Accept": "application/json",
                    "X-Subscription-Token": settings.brave_api_key,
                },
            )
        resp.raise_for_status()
        results = resp.json().get("web", {}).get("results") or []
        return [
            {
                "title": r.get("title") or "",
                "url": r.get("url") or "",
                "description": r.get("description") or "",
            }
            for r in results
        ]
    except Exception as exc:
        logger.warning("Brave search failed: %s", type(exc).__name__)
        return []


def _job_id(url: str) -> str:
    host = urlparse(url).netloc.replace("www.", "")
    path = urlparse(url).path.rstrip("/")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", f"{host}{path}").strip("-").lower()
    return (slug or "job")[:80]


def _guess_company(title: str, url: str) -> str:
    if " at " in title.lower():
        return title.split(" at ")[-1][:80].strip()
    host = urlparse(url).netloc.replace("www.", "").split(".")[0]
    return host[:80] or "Employer"
