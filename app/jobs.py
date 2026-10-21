from __future__ import annotations

from datetime import date
from typing import Any

from app import db, search


def search_and_cache(
    origin: str,
    destination: str,
    depart: date | str,
    *,
    round_trip: bool = False,
) -> list[dict[str, Any]]:
    """Resolve a route to live booking links and cache them for book 1/2/3."""
    found = search.find_flights(origin, destination, depart, round_trip=round_trip)
    for flight in found:
        db.upsert_job(flight)
    return found


def format_flight_card(index: int, flight: dict[str, Any]) -> str:
    return (
        f"{index}. *{flight.get('company')}*\n"
        f"   {flight.get('title')}\n"
        f"   {flight.get('description') or ''}\n"
        f"   {flight.get('apply_url') or ''}"
    )
