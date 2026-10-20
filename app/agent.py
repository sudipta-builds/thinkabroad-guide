from __future__ import annotations

import json
import re
from typing import Any

from app import cv_builder, db, jobs, llm, screenshots, vault
from app.config import get_settings
from app.reply import Reply


WELCOME = (
    "Hello — I'm *Guide*, your personal AI assistant to help you find jobs "
    "(including visa-sponsored roles).\n\n"
    "Tell me which *country* you're interested in."
)


def _load_interests(user: dict[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(user.get("interests_json") or "{}")
    except json.JSONDecodeError:
        return {}


def _set_pending(phone: str, interests: dict[str, Any], pending: dict[str, Any] | None) -> dict[str, Any]:
    data = dict(interests)
    if pending:
        data["_pending_apply"] = pending
    else:
        data.pop("_pending_apply", None)
    db.update_user(phone, interests=data)
    return data


def _extract_country(text: str) -> str | None:
    lower = text.lower().strip()
    mapping = [
        ("united kingdom", "United Kingdom"),
        ("great britain", "United Kingdom"),
        ("england", "United Kingdom"),
        ("scotland", "United Kingdom"),
        ("wales", "United Kingdom"),
        ("uk", "United Kingdom"),
        ("norway", "Norway"),
        ("norwegian", "Norway"),
        ("finland", "Finland"),
        ("finnish", "Finland"),
        ("germany", "Germany"),
        ("netherlands", "Netherlands"),
        ("ireland", "Ireland"),
        ("sweden", "Sweden"),
        ("denmark", "Denmark"),
        ("canada", "Canada"),
        ("australia", "Australia"),
        ("usa", "United States"),
        ("united states", "United States"),
        ("america", "United States"),
    ]
    for key, label in mapping:
        if re.search(rf"\b{re.escape(key)}\b", lower):
            return label
    if 2 <= len(text.strip()) <= 40 and "\n" not in text and not text.lower().startswith("save "):
        if text.strip().upper() in {
            "HI", "HELLO", "START", "HELP", "CV", "JOBS", "STATUS", "YES", "NO", "VAULT",
        }:
            return None
        if re.match(r"^APPLY\s+\d+$", text.strip().upper()):
            return None
        if re.fullmatch(r"[A-Za-z][A-Za-z\s\-']{1,38}", text.strip()):
            return text.strip().title()
    return None


def handle_message(phone: str, body: str) -> Reply:
    text = (body or "").strip()
    if not text:
        return Reply.of(WELCOME)

    user = db.upsert_user(phone)
    interests = _load_interests(user)
    db.add_message(phone, "user", text)
    cmd = text.strip().upper()

    save_m = re.match(
        r"^SAVE\s+(\w+)\s+(\S+)\s+(.+)$",
        text.strip(),
        flags=re.IGNORECASE | re.DOTALL,
    )
    if save_m:
        reply = vault.save_login(phone, save_m.group(1), save_m.group(2), save_m.group(3).strip())
        db.add_message(phone, "assistant", reply)
        return Reply.of(reply)

    if cmd in {"HI", "HELLO", "START", "HELP"}:
        db.add_message(phone, "assistant", WELCOME)
        return Reply.of(WELCOME)

    if cmd == "VAULT":
        sites = vault.list_sites(phone)
        reply = vault.vault_help()
        if sites:
            reply += "\n\nSaved: " + ", ".join(sites)
        db.add_message(phone, "assistant", reply)
        return Reply.of(reply)

    if cmd == "CV":
        history = db.recent_messages(phone, 20)
        cv = cv_builder.build_cv_markdown(interests, history)
        db.update_user(phone, cv_markdown=cv)
        reply = "Here's your CV draft:\n\n" + cv[:3500]
        db.add_message(phone, "assistant", reply)
        return Reply.of(reply)

    if cmd == "STATUS":
        apps = db.list_applications(phone)
        if not apps:
            reply = "No applications yet."
        else:
            lines = [
                f"• {a.get('title') or a.get('job_id')} @ {a.get('company') or '?'} — *{a.get('status')}*"
                for a in apps[:10]
            ]
            reply = "Your applications:\n" + "\n".join(lines)
        db.add_message(phone, "assistant", reply)
        return Reply.of(reply)

    if cmd == "JOBS" or "find job" in text.lower() or "search job" in text.lower():
        out = _run_job_search(phone, interests, with_screenshots=True)
        db.add_message(phone, "assistant", out.text)
        return out

    apply_m = re.match(r"^APPLY\s+(\d+)$", cmd)
    if apply_m:
        out = _start_apply(phone, interests, int(apply_m.group(1)) - 1)
        db.add_message(phone, "assistant", out.text)
        return out

    if cmd == "YES":
        out = _confirm_apply(phone, interests)
        db.add_message(phone, "assistant", out.text)
        return out

    if cmd == "NO":
        interests = _set_pending(phone, interests, None)
        reply = "Cancelled."
        db.add_message(phone, "assistant", reply)
        return Reply.of(reply)

    if not interests.get("country"):
        country = _extract_country(text)
        if country:
            interests["country"] = country
            interests["countries"] = [country]
            db.update_user(phone, interests=interests)
            reply = (
                f"Got it — *{country}*.\n\n"
                "What kind of job are you looking for? "
                "(role, skills, experience — write it in your own words)"
            )
            db.add_message(phone, "assistant", reply)
            return Reply.of(reply)
        db.add_message(phone, "assistant", WELCOME)
        return Reply.of(WELCOME)

    interests["target_role"] = text.strip()[:200]
    interests["raw_notes"] = ((interests.get("raw_notes") or "") + "\n" + text.strip()).strip()[-2000:]
    skills = list(interests.get("skills") or [])
    for token in re.findall(
        r"\b(python|java|react|node|typescript|sql|aws|azure|nursing|excel|salesforce|sap)\b",
        text.lower(),
    ):
        if token not in skills:
            skills.append(token)
    if skills:
        interests["skills"] = skills
    db.update_user(phone, interests=interests)

    history = db.recent_messages(phone, 20)
    cv = cv_builder.build_cv_markdown(interests, history)
    db.update_user(phone, cv_markdown=cv)

    search_out = _run_job_search(phone, interests, with_screenshots=True)
    intro = (
        f"Thanks — searching live for *{interests.get('target_role')}* in "
        f"*{interests.get('country')}* (preferring sponsorship where possible).\n\n"
    )
    out = Reply(text=intro + search_out.text, media_urls=search_out.media_urls)
    db.add_message(phone, "assistant", out.text)
    return out


def _run_job_search(phone: str, interests: dict[str, Any], *, with_screenshots: bool) -> Reply:
    settings = get_settings()
    country = interests.get("country") or ""
    role = interests.get("target_role") or ""
    if not country:
        return Reply.of(WELCOME)
    if not settings.search_ready and not settings.llm_ready:
        return Reply.of(
            "Search isn't configured yet (need GEMINI_API_KEY + DEEPSEEK_API_KEY on the server)."
        )

    matches = jobs.search_and_cache(str(country), str(role), limit=5)
    if not matches:
        return Reply.of(
            f"I searched the live web for *{role or 'jobs'}* in *{country}* "
            "but didn't get clean listings. Try a more specific role, or send *JOBS* again."
        )

    interests = dict(interests)
    interests["_last_matches"] = [m["id"] for m in matches]
    db.update_user(phone, interests=interests)

    cards = "\n\n".join(jobs.format_job_card(i + 1, m) for i, m in enumerate(matches))
    vault_hint = ""
    if not vault.list_sites(phone):
        vault_hint = "\n\nTo apply with your login later: send *VAULT*."

    media: list[str] = []
    if with_screenshots:
        # Screenshot top 2 listing pages so user can preview before APPLY
        for m in matches[:2]:
            url = m.get("apply_url") or ""
            shot = screenshots.screenshot_public_url(url) if url else None
            if shot:
                media.append(shot)

    text = (
        "Live web results (links below). I've attached page screenshots when available — "
        "review them, then reply *APPLY 1* (or another number).\n\n"
        + cards
        + vault_hint
    )
    return Reply(text=text, media_urls=media)


def _start_apply(phone: str, interests: dict[str, Any], idx: int) -> Reply:
    match_ids = interests.get("_last_matches") or []
    if idx < 0 or idx >= len(match_ids):
        return Reply.of("Send *JOBS* first, then *APPLY 1*.")
    job = db.get_job(match_ids[idx])
    if not job:
        return Reply.of("That listing expired. Send *JOBS* to search again.")

    cover = _draft_cover(interests, job)
    db.upsert_application(phone, job["id"], "drafted", draft_cover=cover)
    _set_pending(
        phone,
        interests,
        {"job_id": job["id"], "title": job.get("title"), "company": job.get("company")},
    )

    apply_url = job.get("apply_url") or ""
    shot = screenshots.screenshot_public_url(apply_url) if apply_url else None
    sites = vault.list_sites(phone)
    vault_line = (
        f"Vault logins ready: {', '.join(sites)}."
        if sites
        else "No vault login yet — send *VAULT* to save one, or continue with the link."
    )
    text = (
        f"Before you confirm — here's the job page preview for "
        f"*{job.get('title')}* @ {job.get('company')} ({job.get('country')}).\n\n"
        f"Link: {apply_url or 'n/a'}\n\n"
        f"Cover draft:\n{cover[:1000]}\n\n"
        f"{vault_line}\n\n"
        "Reply *YES* to confirm apply, or *NO* to cancel."
    )
    return Reply(text=text, media_urls=[shot] if shot else [])


def _confirm_apply(phone: str, interests: dict[str, Any]) -> Reply:
    pending = interests.get("_pending_apply") or {}
    if not pending.get("job_id"):
        return Reply.of("Nothing pending. Search jobs, then *APPLY N*.")
    job_id = pending["job_id"]
    job = db.get_job(job_id) or {}
    db.upsert_application(phone, job_id, "applied", notes="Confirmed via WhatsApp YES")
    _set_pending(phone, interests, None)
    text = (
        f"Confirmed for *{pending.get('title')}* @ {pending.get('company')}.\n"
        f"{job.get('apply_url') or ''}\n\n"
        "Logged. Full browser submit with your vault login is the next cloud step "
        "(Browserbase) once Guide is live on Render."
    )
    return Reply.of(text)


def _draft_cover(interests: dict[str, Any], job: dict[str, Any]) -> str:
    prompt = (
        f"Write a short cover note for this role.\n"
        f"Job: {job.get('title')} at {job.get('company')} ({job.get('country')})\n"
        f"Candidate: {json.dumps({k: v for k, v in interests.items() if not str(k).startswith('_')})}\n"
        "Plain text, 3 short paragraphs max."
    )
    text = llm.chat(
        [
            {"role": "system", "content": "You write tight job cover notes."},
            {"role": "user", "content": prompt},
        ],
        max_tokens=400,
    )
    if text:
        return text
    return (
        f"I am applying for {job.get('title')} at {job.get('company')}. "
        f"I am interested in opportunities in {interests.get('country')}."
    )
