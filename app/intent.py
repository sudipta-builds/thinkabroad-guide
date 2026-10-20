from __future__ import annotations

import json
import re
from typing import Any

from app import llm


# Heuristic intents — fast, no LLM
_GREET = re.compile(r"^(hi|hello|hey|yo|hola|start|help)\b", re.I)
_STATUS = re.compile(r"\b(status|my applications?|where did i apply|application tracker)\b", re.I)
_CV = re.compile(r"\b(cv|resume|curriculum)\b", re.I)
_VAULT = re.compile(r"\b(vault|save (my )?login|password|credentials)\b", re.I)
_SEARCH = re.compile(
    r"\b(find|search|look(ing)? for|show|get|any|need)\b.*\b(job|jobs|role|roles|opening|openings|position|sponsored|sponsor|visa)\b"
    r"|\b(job|jobs|sponsored jobs|visa sponsorship)\b.*\b(in|for|near)\b"
    r"|\b(sponsored|sponsorship)\b",
    re.I,
)
_APPLY = re.compile(
    r"\b(apply|submit|send (my )?(cv|application)|go ahead and apply)\b",
    re.I,
)
_CONFIRM = re.compile(r"^(yes|yep|yeah|y|ok|okay|sure|do it|go ahead|confirm|apply now)\b", re.I)
_CANCEL = re.compile(r"^(no|nope|cancel|stop|never ?mind)\b", re.I)
_APPLY_NUM = re.compile(
    r"(?:apply\s*(?:to|for)?\s*(?:job\s*)?(?:#|number\s*)?(\d+))"
    r"|(?:apply\s+(?:to\s+)?(?:the\s+)?(first|second|third|1st|2nd|3rd))"
    r"|^(?:#)?(\d+)$",
    re.I,
)


def _ordinal_to_idx(token: str) -> int | None:
    t = token.lower()
    mapping = {
        "1": 0, "1st": 0, "first": 0,
        "2": 1, "2nd": 1, "second": 1,
        "3": 2, "3rd": 2, "third": 2,
    }
    return mapping.get(t)


def classify_heuristic(text: str) -> dict[str, Any] | None:
    raw = (text or "").strip()
    if not raw:
        return {"intent": "greet"}

    if _CANCEL.match(raw):
        return {"intent": "cancel"}
    if _CONFIRM.match(raw):
        return {"intent": "confirm_apply", "auto_submit": "apply now" in raw.lower() or "do it" in raw.lower()}

    m = _APPLY_NUM.search(raw)
    if m:
        n = m.group(1) or m.group(3)
        ord_ = m.group(2)
        if n:
            return {"intent": "apply", "index": max(0, int(n) - 1), "auto_submit": False}
        if ord_:
            idx = _ordinal_to_idx(ord_)
            if idx is not None:
                return {"intent": "apply", "index": idx, "auto_submit": False}

    if re.match(r"^APPLY\s+(\d+)$", raw.upper()):
        return {"intent": "apply", "index": int(re.match(r"^APPLY\s+(\d+)$", raw.upper()).group(1)) - 1}

    lower = raw.lower()
    if _GREET.match(raw) and len(raw) < 40:
        return {"intent": "greet"}
    if raw.upper() in {"CV", "RESUME"} or (_CV.search(raw) and "apply" not in lower and len(raw) < 80):
        return {"intent": "cv"}
    if _STATUS.search(raw):
        return {"intent": "status"}
    if raw.upper() == "VAULT" or (_VAULT.search(raw) and "save linkedin" not in lower):
        return {"intent": "vault"}
    if raw.upper() == "JOBS" or _SEARCH.search(raw):
        return {"intent": "search"}
    if _APPLY.search(raw):
        # "apply for me" / "apply to these" without number → first match or pending
        auto = bool(re.search(r"\b(for me|on my behalf|just apply|apply now|go ahead)\b", lower))
        return {"intent": "apply", "index": 0, "auto_submit": auto}

    return None


def classify(text: str, interests: dict[str, Any]) -> dict[str, Any]:
    """Return {intent, country?, role?, index?, auto_submit?, reply?}."""
    hit = classify_heuristic(text)
    if hit:
        # Enrich search/greet with country/role from free text when present
        if hit["intent"] in {"search", "greet"} or hit["intent"] == "apply":
            enriched = _extract_slots(text)
            hit.update({k: v for k, v in enriched.items() if v})
        return hit

    # LLM only when heuristics miss — keep token budget tiny
    context = {
        "country": interests.get("country"),
        "role": interests.get("target_role"),
        "has_matches": bool(interests.get("_last_matches")),
        "pending_apply": bool(interests.get("_pending_apply")),
        "has_email": bool(interests.get("email")),
    }
    prompt = (
        "You are Guide intent classifier for a WhatsApp sponsored-jobs agent.\n"
        "Return ONLY JSON with keys: intent, country, role, index, auto_submit, notes.\n"
        "intent one of: greet,search,apply,confirm_apply,cancel,cv,status,vault,set_profile,chat\n"
        "index is 0-based job number when applying. auto_submit true if user wants immediate submit.\n"
        f"Known context: {json.dumps(context)}\n"
        f"User message: {text[:500]}\n"
    )
    raw = llm.chat(
        [
            {"role": "system", "content": "Output JSON only. No markdown."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.0,
        max_tokens=180,
        timeout=20.0,
    )
    parsed = _parse_json(raw)
    if parsed and parsed.get("intent"):
        return parsed

    # Default: treat as search/update if they mentioned work, else chat
    slots = _extract_slots(text)
    if slots.get("country") or slots.get("role") or len(text) > 15:
        return {"intent": "search", **slots}
    return {"intent": "chat", **slots}


def _extract_slots(text: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    lower = text.lower()
    mapping = [
        ("united kingdom", "United Kingdom"), ("uk", "United Kingdom"), ("england", "United Kingdom"),
        ("norway", "Norway"), ("finland", "Finland"), ("germany", "Germany"),
        ("netherlands", "Netherlands"), ("ireland", "Ireland"), ("sweden", "Sweden"),
        ("denmark", "Denmark"), ("canada", "Canada"), ("australia", "Australia"),
        ("usa", "United States"), ("united states", "United States"),
    ]
    for key, label in mapping:
        if re.search(rf"\b{re.escape(key)}\b", lower):
            out["country"] = label
            break
    # crude role: strip country words and common verbs
    role = text.strip()
    for key, _ in mapping:
        role = re.sub(rf"\b{re.escape(key)}\b", " ", role, flags=re.I)
    role = re.sub(
        r"\b(find|search|looking for|jobs?|roles?|sponsored|sponsorship|visa|please|want|need|in|for)\b",
        " ",
        role,
        flags=re.I,
    )
    role = re.sub(r"\s+", " ", role).strip(" .,")
    if 2 < len(role) < 120:
        out["role"] = role
    return out


def _parse_json(raw: str) -> dict[str, Any] | None:
    if not raw:
        return None
    raw = raw.strip()
    try:
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I | re.S)
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, flags=re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None
