from __future__ import annotations

import json
from typing import Any

from app import llm


SYSTEM = """You are Guide, a career agent for international talent targeting sponsored jobs
in the United Kingdom, Norway, and Finland.

Build a concise, ATS-friendly CV in Markdown from the user's stated interests and background.
No fluff. Prefer facts the user gave; mark unknowns as [TODO].
Output ONLY the Markdown CV — no preamble."""


def build_cv_markdown(interests: dict[str, Any], history: list[dict[str, str]]) -> str:
    transcript = "\n".join(f"{m['role']}: {m['body']}" for m in history[-12:])
    user_prompt = (
        "User interests JSON:\n"
        f"{json.dumps(interests, indent=2)}\n\n"
        "Recent chat:\n"
        f"{transcript}\n\n"
        "Write the CV now."
    )
    text = llm.chat(
        [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.3,
        max_tokens=1500,
    )
    if text:
        return text
    # Offline fallback when LLM is not configured
    name = interests.get("name") or "Candidate"
    role = interests.get("target_role") or "Professional"
    countries = ", ".join(interests.get("countries") or ["UK", "Norway", "Finland"])
    skills = ", ".join(interests.get("skills") or []) or "[TODO: skills]"
    return f"""# {name}

**Target:** {role} (sponsored roles — {countries})

## Summary
Motivated professional seeking visa-sponsoring employers in {countries}.

## Skills
{skills}

## Experience
[TODO: add roles from chat]

## Education
[TODO: add education]

## Languages
[TODO]
"""
