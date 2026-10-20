from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from app import llm


def find_sponsored_jobs(country: str, role: str = "", *, limit: int = 5) -> list[dict[str, Any]]:
    """Live job search via Gemini + Google Search grounding only."""
    country = (country or "").strip()
    role = (role or "").strip() or "jobs"

    grounded = llm.gemini_search(
        f"Find up to {limit} LIVE job openings in {country} for: {role or 'professionals'}.\n"
        "Priority: visa sponsorship / skilled worker / work permit / relocation for internationals. "
        "Skip pure internship spam unless relevant.\n"
        "For each job ONE line:\n"
        "TITLE | COMPANY | CITY | APPLY_URL | sponsorship note\n"
        "Only real https apply URLs (Greenhouse, Lever, Ashby, company careers, LinkedIn jobs). Never invent URLs."
    )
    return _parse_pipe_lines(grounded, country)[:limit]


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
        title = re.sub(r"^[\d\.\)\-\*\s]+", "", title).strip()
        out.append(
            {
                "id": _job_id(url),
                "title": title[:160],
                "company": (company[:120] or _guess_company(title, url)),
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
