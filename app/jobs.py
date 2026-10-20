from __future__ import annotations

from typing import Any

from app import db, search


def search_and_cache(country: str, role: str = "", *, limit: int = 5) -> list[dict[str, Any]]:
    """Search the live web and cache results for APPLY N."""
    found = search.find_sponsored_jobs(country, role, limit=limit)
    for job in found:
        db.upsert_job(job)
    return found


def format_job_card(index: int, job: dict[str, Any]) -> str:
    sponsor = job.get("sponsor_status") or "likely"
    city = job.get("city") or ""
    loc = f"{city}, {job.get('country')}" if city else str(job.get("country"))
    url = job.get("apply_url") or ""
    return (
        f"{index}. *{job.get('title')}* @ {job.get('company')}\n"
        f"   {loc} · sponsor: {sponsor}\n"
        f"   {url}\n"
        f"   say “apply to {index}” or “apply to the first one”"
    )
