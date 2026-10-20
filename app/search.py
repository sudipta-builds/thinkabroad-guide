from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlparse

import httpx

from app import llm
from app.config import get_settings

logger = logging.getLogger(__name__)


def find_sponsored_jobs(country: str, role: str = "", *, limit: int = 5) -> list[dict[str, Any]]:
    """Live web search for sponsor-friendly roles. No seeded catalog."""
    country = (country or "").strip()
    role = (role or "").strip() or "jobs"
    query = f"{role} job visa sponsorship OR skilled worker OR work permit {country} 2025 OR 2026"

    # 1) Gemini + Google Search grounding (best for live jobs)
    grounded = llm.gemini_search(
        f"Search the live web for up to {limit} currently open jobs in {country} "
        f"related to: {role or 'professional roles'}. Prefer roles that mention visa sponsorship, "
        f"skilled worker, work permit, or relocation for internationals.\n"
        "For each job return exactly one line in this format:\n"
        "TITLE | COMPANY | CITY | APPLY_URL | sponsorship note\n"
        "Only real https URLs (LinkedIn, Greenhouse, Lever, company careers). Never invent URLs."
    )
    parsed = _parse_pipe_lines(grounded, country)
    if parsed:
        return parsed[:limit]

    # 2) Brave web search + light parse
    brave_hits = brave_web_search(query, count=8)
    out: list[dict[str, Any]] = []
    for hit in brave_hits:
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
        logger.warning("Brave search failed: %s", exc)
        return []


def _parse_pipe_lines(text: str, country: str) -> list[dict[str, Any]]:
    if not text:
        return []
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        if "|" not in line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 4:
            continue
        title, company, city, url = parts[0], parts[1], parts[2], parts[3]
        url = _extract_url(url)
        if not url or not title:
            continue
        # strip leading bullets / numbers
        title = re.sub(r"^[\d\.\)\-\*\s]+", "", title).strip()
        out.append(
            {
                "id": _job_id(url),
                "title": title[:160],
                "company": company[:120] or _guess_company(title, url),
                "country": country.lower(),
                "city": city[:80],
                "apply_url": url,
                "sponsor_status": "likely",
                "tags": [],
                "description": parts[4] if len(parts) > 4 else "",
                "source": "gemini",
            }
        )
    return out


def _extract_url(raw: str) -> str:
    m = re.search(r"https?://\S+", raw or "")
    return (m.group(0).rstrip(").,]\"'") if m else "").strip()


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
