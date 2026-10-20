from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app import apply_browser, cv_builder, cv_export, db, intent, jobs, llm, media_files, screenshots, vault
from app.config import get_settings
from app.reply import Reply


WELCOME = (
    "Hello — I'm *Guide*, your personal AI for *sponsored jobs*.\n\n"
    "Message me naturally, e.g.\n"
    "_Software engineer jobs in Finland with visa sponsorship_\n"
    "_Apply to the first one_\n"
    "_Build my CV_\n\n"
    "I search live, build a real PDF/Word CV, and can submit applications for you."
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


def _merge_slots(interests: dict[str, Any], slots: dict[str, Any]) -> dict[str, Any]:
    data = dict(interests)
    if slots.get("country"):
        data["country"] = slots["country"]
        data["countries"] = [slots["country"]]
    if slots.get("role"):
        data["target_role"] = str(slots["role"])[:200]
    return data


def handle_message(phone: str, body: str) -> Reply:
    text = (body or "").strip()
    if not text:
        return Reply.of(WELCOME)

    user = db.upsert_user(phone)
    interests = _load_interests(user)
    db.add_message(phone, "user", text)

    # Structured SAVE commands stay exact
    profile_m = re.match(r"^SAVE\s+PROFILE\s+(.+)$", text, flags=re.I | re.S)
    if profile_m:
        reply = _save_profile(phone, interests, profile_m.group(1))
        db.add_message(phone, "assistant", reply)
        return Reply.of(reply)

    save_m = re.match(r"^SAVE\s+(\w+)\s+(\S+)\s+(.+)$", text, flags=re.I | re.S)
    if save_m:
        site = save_m.group(1)
        if site.lower() == "profile":
            reply = _save_profile(phone, interests, f"{save_m.group(2)} | {save_m.group(3)}")
        else:
            reply = vault.save_login(phone, site, save_m.group(2), save_m.group(3).strip())
        db.add_message(phone, "assistant", reply)
        return Reply.of(reply)

    parsed = intent.classify(text, interests)
    intent_name = str(parsed.get("intent") or "chat").lower()
    interests = _merge_slots(interests, parsed)
    if parsed.get("country") or parsed.get("role"):
        db.update_user(phone, interests=interests)

    # Pending YES short-circuit
    if intent_name == "confirm_apply" or (
        intent_name == "apply" and interests.get("_pending_apply") and parsed.get("auto_submit")
    ):
        out = _confirm_apply(phone, interests)
        db.add_message(phone, "assistant", out.text)
        return out

    if intent_name == "cancel":
        interests = _set_pending(phone, interests, None)
        reply = "Cancelled."
        db.add_message(phone, "assistant", reply)
        return Reply.of(reply)

    if intent_name == "greet":
        db.add_message(phone, "assistant", WELCOME)
        return Reply.of(WELCOME)

    if intent_name == "vault":
        reply = vault.vault_help()
        sites = vault.list_sites(phone)
        if sites:
            reply += "\n\nSaved logins: " + ", ".join(sites)
        if interests.get("email"):
            reply += f"\nProfile: {interests.get('name') or ''} {interests.get('email')}"
        db.add_message(phone, "assistant", reply)
        return Reply.of(reply)

    if intent_name == "cv":
        out = _build_and_send_cv(phone, interests, user)
        db.add_message(phone, "assistant", out.text)
        return out

    if intent_name == "status":
        apps = db.list_applications(phone)
        if not apps:
            reply = "No applications yet. Tell me a country + role and I'll find sponsored jobs."
        else:
            lines = [
                f"• {a.get('title') or a.get('job_id')} @ {a.get('company') or '?'} — *{a.get('status')}*"
                for a in apps[:10]
            ]
            reply = "Your applications:\n" + "\n".join(lines)
        db.add_message(phone, "assistant", reply)
        return Reply.of(reply)

    if intent_name == "apply":
        idx = int(parsed.get("index") or 0)
        auto = bool(parsed.get("auto_submit"))
        out = _start_apply(phone, interests, idx)
        # Agentic: user said apply for me / apply now → submit immediately if profile ready
        if auto and interests.get("email") and interests.get("_pending_apply"):
            out2 = _confirm_apply(phone, _load_interests(db.get_user(phone) or {}))
            combined = Reply(
                text=out.text + "\n\nSubmitting now…\n\n" + out2.text,
                media_urls=(out.media_urls or []) + (out2.media_urls or []),
            )
            db.add_message(phone, "assistant", combined.text)
            return combined
        db.add_message(phone, "assistant", out.text)
        return out

    if intent_name == "search":
        if not interests.get("country"):
            reply = (
                "Which *country* should I search for sponsored roles?\n"
                "(You can also send everything in one line: _data analyst, Norway, visa sponsorship_)"
            )
            db.add_message(phone, "assistant", reply)
            return Reply.of(reply)
        if not interests.get("target_role"):
            # Use raw message as role if classifier didn't extract
            interests["target_role"] = text[:200]
            db.update_user(phone, interests=interests)
        out = _run_job_search(phone, interests)
        db.add_message(phone, "assistant", out.text)
        return out

    # chat / set_profile / unknown — if looks like a job request, search; else short agent reply
    if not interests.get("country") and intent._extract_slots(text).get("country"):
        interests = _merge_slots(interests, intent._extract_slots(text))
        db.update_user(phone, interests=interests)

    slots = intent._extract_slots(text)
    if slots.get("country") or slots.get("role") or re.search(r"\b(job|sponsor|visa|role)\b", text, re.I):
        interests = _merge_slots(interests, slots)
        if not interests.get("target_role"):
            interests["target_role"] = text[:200]
        db.update_user(phone, interests=interests)
        if not interests.get("country"):
            reply = "Got it. Which *country* for sponsored jobs?"
            db.add_message(phone, "assistant", reply)
            return Reply.of(reply)
        out = _run_job_search(phone, interests)
        db.add_message(phone, "assistant", out.text)
        return out

    # Lightweight chat (sponsored-jobs domain), no long essays
    reply = _quick_chat(text, interests)
    db.add_message(phone, "assistant", reply)
    return Reply.of(reply)


def _quick_chat(text: str, interests: dict[str, Any]) -> str:
    country = interests.get("country") or "your target country"
    role = interests.get("target_role") or "your target role"
    answer = llm.chat(
        [
            {
                "role": "system",
                "content": (
                    "You are Guide, a WhatsApp agent that FINDS and APPLIES to sponsored jobs. "
                    "Be short. Push toward action: search or apply. No fluff."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"User: {text}\nKnown: country={country}, role={role}\n"
                    "Reply under 500 chars. If they want jobs, tell them to name country+role."
                ),
            },
        ],
        temperature=0.3,
        max_tokens=220,
        timeout=20.0,
    )
    return answer or (
        "I can search sponsored roles and apply for you. "
        "Try: _software engineer in Finland with visa sponsorship_"
    )


def _run_job_search(phone: str, interests: dict[str, Any]) -> Reply:
    settings = get_settings()
    country = interests.get("country") or ""
    role = interests.get("target_role") or ""
    if not country:
        return Reply.of("Which country should I search?")
    if not settings.search_ready:
        return Reply.of("Search isn't configured (need GEMINI_API_KEY).")

    matches = jobs.search_and_cache(str(country), str(role), limit=3)
    if not matches:
        return Reply.of(
            f"No clean sponsored listings for *{role or 'jobs'}* in *{country}* right now. "
            "Try a more specific role."
        )

    interests = dict(interests)
    interests["_last_matches"] = [m["id"] for m in matches]
    db.update_user(phone, interests=interests)

    cards = "\n\n".join(jobs.format_job_card(i + 1, m) for i, m in enumerate(matches))
    hint = (
        "\n\nSay *apply to the first one* or *apply for me* — I'll prepare CV + submit."
        if interests.get("email")
        else "\n\nBefore I can submit: *SAVE PROFILE Name | email | phone*"
    )
    text = f"Sponsored / sponsor-likely roles in *{country}*:\n\n{cards}{hint}"
    return Reply.of(text)


def _save_profile(phone: str, interests: dict[str, Any], raw: str) -> str:
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) < 2:
        return "Format: SAVE PROFILE Full Name | email@x.com | +phone"
    name, email = parts[0], parts[1]
    phone_n = parts[2] if len(parts) > 2 else ""
    interests = dict(interests)
    interests["name"] = name
    interests["email"] = email
    if phone_n:
        interests["phone"] = phone_n
    db.update_user(phone, interests=interests, display_name=name)
    return f"Profile saved: *{name}* · {email}" + (f" · {phone_n}" if phone_n else "") + "\nI can apply for you now."


def _ensure_cv_files(phone: str, interests: dict[str, Any], user: dict[str, Any]) -> dict[str, Path]:
    md = user.get("cv_markdown") or ""
    if not md:
        history = db.recent_messages(phone, 12)
        md = cv_builder.build_cv_markdown(interests, history)
        db.update_user(phone, cv_markdown=md)
    stem = re.sub(r"[^a-zA-Z0-9]+", "-", (interests.get("name") or "guide-cv"))[:30]
    files = cv_export.export_cv_files(md, stem=stem or "guide-cv")
    interests = dict(interests)
    interests["_cv_pdf"] = str(files["pdf"])
    interests["_cv_docx"] = str(files["docx"])
    db.update_user(phone, interests=interests)
    return files


def _build_and_send_cv(phone: str, interests: dict[str, Any], user: dict[str, Any]) -> Reply:
    history = db.recent_messages(phone, 12)
    cv = cv_builder.build_cv_markdown(interests, history)
    db.update_user(phone, cv_markdown=cv)
    files = _ensure_cv_files(phone, interests, {**user, "cv_markdown": cv})
    media = []
    for key in ("pdf", "docx"):
        url = media_files.public_url_for(files[key])
        if url:
            media.append(url)
    text = "CV ready (PDF + Word attached when possible).\n\n" + cv[:2200]
    if not media:
        text += "\n\n_Set GUIDE_PUBLIC_URL on Render to attach files on WhatsApp._"
    return Reply(text=text, media_urls=media)


def _start_apply(phone: str, interests: dict[str, Any], idx: int) -> Reply:
    match_ids = interests.get("_last_matches") or []
    if not match_ids:
        return Reply.of("No jobs loaded yet. Tell me a country + role first (e.g. engineer in Norway).")
    if idx < 0 or idx >= len(match_ids):
        idx = 0
    job = db.get_job(match_ids[idx])
    if not job:
        return Reply.of("That listing expired — ask me to search again.")

    user = db.get_user(phone) or {}
    files = _ensure_cv_files(phone, interests, user)
    cover = _draft_cover(interests, job)
    db.upsert_application(phone, job["id"], "drafted", draft_cover=cover)
    interests = _set_pending(
        phone,
        interests,
        {"job_id": job["id"], "title": job.get("title"), "company": job.get("company")},
    )

    apply_url = job.get("apply_url") or ""
    shot = screenshots.mshots_url(apply_url) if apply_url else None
    media = [m for m in [shot, media_files.public_url_for(files["pdf"]), media_files.public_url_for(files["docx"])] if m]

    if not interests.get("email"):
        return Reply(
            text=(
                f"Selected *{job.get('title')}* @ {job.get('company')}.\n{apply_url}\n\n"
                "I need your details to submit:\n"
                "*SAVE PROFILE Full Name | you@email.com | +phone*\n"
                "Then say *apply for me* or *YES*."
            ),
            media_urls=media,
        )

    return Reply(
        text=(
            f"Ready to submit *{job.get('title')}* @ {job.get('company')} ({job.get('country')}).\n"
            f"{apply_url}\n\n"
            f"CV attached. Cover note:\n{cover[:700]}\n\n"
            "Reply *YES* / *apply now* and I'll submit in the browser."
        ),
        media_urls=media,
    )


def _confirm_apply(phone: str, interests: dict[str, Any]) -> Reply:
    pending = interests.get("_pending_apply") or {}
    if not pending.get("job_id"):
        # Agentic fallback: apply to first cached match
        match_ids = interests.get("_last_matches") or []
        if match_ids:
            prepared = _start_apply(phone, interests, 0)
            interests = _load_interests(db.get_user(phone) or {})
            if not interests.get("email"):
                return prepared
            pending = interests.get("_pending_apply") or {}
        if not pending.get("job_id"):
            return Reply.of("Nothing to apply to yet. Search jobs first, then say *apply to the first one*.")

    job_id = pending["job_id"]
    job = db.get_job(job_id) or {}
    user = db.get_user(phone) or {}
    files = _ensure_cv_files(phone, interests, user)
    cover = ""
    for a in db.list_applications(phone):
        if a.get("job_id") == job_id and a.get("draft_cover"):
            cover = a["draft_cover"]
            break
    if not cover:
        cover = _draft_cover(interests, job)

    profile = {
        "name": str(interests.get("name") or user.get("display_name") or "Candidate"),
        "email": str(interests.get("email") or ""),
        "phone": str(interests.get("phone") or ""),
        "linkedin": "",
    }
    if not profile["email"]:
        return Reply.of("I need *SAVE PROFILE Name | email | phone* before I can submit.")

    creds = vault.unlock_login(phone, "linkedin") or vault.unlock_login(phone, "email")
    if creds and "@" in creds.get("username", "") and not profile["email"]:
        profile["email"] = creds["username"]

    result = apply_browser.apply_to_job(
        apply_url=str(job.get("apply_url") or ""),
        cv_pdf=files.get("pdf"),
        cv_docx=files.get("docx"),
        cover=cover,
        profile=profile,
        credentials=creds,
    )
    db.upsert_application(
        phone,
        job_id,
        "applied" if result.ok else "failed",
        draft_cover=cover,
        notes=result.message[:500],
    )
    _set_pending(phone, interests, None)

    media = []
    if result.screenshot_path:
        url = media_files.public_url_for(result.screenshot_path)
        if url:
            media.append(url)
    for key in ("pdf", "docx"):
        u = media_files.public_url_for(files[key])
        if u:
            media.append(u)

    text = (
        f"{'Submitted' if result.ok else 'Tried to submit'} — "
        f"*{pending.get('title')}* @ {pending.get('company')}.\n"
        f"{result.message}\n"
        f"{result.final_url or job.get('apply_url') or ''}"
    )
    return Reply(text=text, media_urls=media)


def _draft_cover(interests: dict[str, Any], job: dict[str, Any]) -> str:
    # Prefer fast template; LLM only if ready and quick
    base = (
        f"I am applying for {job.get('title')} at {job.get('company')}. "
        f"I am seeking opportunities in {interests.get('country')} and am open to visa sponsorship."
    )
    if not get_settings().llm_ready:
        return base
    text = llm.chat(
        [
            {"role": "system", "content": "Write a tight 2-paragraph cover note. Plain text only."},
            {
                "role": "user",
                "content": (
                    f"Job: {job.get('title')} @ {job.get('company')} ({job.get('country')})\n"
                    f"Candidate role: {interests.get('target_role')}\nSkills: {interests.get('skills')}"
                ),
            },
        ],
        max_tokens=250,
        timeout=15.0,
    )
    return text or base
