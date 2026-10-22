from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)


def complete(
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    temperature: float = 0.3,
    max_tokens: int = 700,
    timeout: float = 40.0,
) -> dict[str, Any]:
    """One DeepSeek turn. Tool calls are returned, not run."""
    settings = get_settings()
    if not settings.llm_ready:
        return {"content": "", "tool_calls": []}
    url = settings.deepseek_base_url.rstrip("/") + "/chat/completions"
    payload: dict[str, Any] = {
        "model": settings.deepseek_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if tools:
        payload["tools"] = tools
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                url,
                headers={
                    "Authorization": f"Bearer {settings.deepseek_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        resp.raise_for_status()
        message = resp.json()["choices"][0]["message"] or {}
    except Exception as exc:
        logger.warning("DeepSeek failed: %s", type(exc).__name__)
        return {"content": "", "tool_calls": []}
    calls = []
    for call in message.get("tool_calls") or []:
        fn = call.get("function") or {}
        raw_args = fn.get("arguments") or "{}"
        if isinstance(raw_args, str):
            try:
                args = json.loads(raw_args)
            except json.JSONDecodeError:
                args = {}
        elif isinstance(raw_args, dict):
            args = raw_args
        else:
            args = {}
        calls.append(
            {
                "id": str(call.get("id") or f"call-{len(calls)}"),
                "name": str(fn.get("name") or ""),
                "arguments": args if isinstance(args, dict) else {},
            }
        )
    return {"content": (message.get("content") or "").strip(), "tool_calls": calls}


def reason(prompt: str, *, timeout: float = 40.0) -> str:
    """Judge live evidence. Uses the reasoning model, then the chat model if that fails."""
    settings = get_settings()
    if not settings.llm_ready or not (prompt or "").strip():
        return ""
    messages = [
        {
            "role": "system",
            "content": (
                "You are judging a live flight search for a WhatsApp traveller. "
                "Use only prices, times, and stops that appear in the evidence. "
                "Say which site failed if the evidence says so. Recommend one option and why. "
                "Say nothing has been paid. Do not put emoji in the text. Keep it under 8 short lines."
            ),
        },
        {"role": "user", "content": prompt[:12000]},
    ]
    models = [settings.deepseek_reason_model, settings.deepseek_model]
    seen: set[str] = set()
    for model in models:
        if not model or model in seen:
            continue
        seen.add(model)
        text = _raw_chat(model, messages, max_tokens=900, timeout=timeout, temperature=0.2)
        if text:
            return text
    return ""


def _raw_chat(
    model: str,
    messages: list[dict[str, Any]],
    *,
    max_tokens: int,
    timeout: float,
    temperature: float,
) -> str:
    settings = get_settings()
    url = settings.deepseek_base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                url,
                headers={
                    "Authorization": f"Bearer {settings.deepseek_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        resp.raise_for_status()
        message = resp.json()["choices"][0]["message"] or {}
    except Exception as exc:
        logger.warning("DeepSeek %s failed: %s", model, type(exc).__name__)
        return ""
    return (message.get("content") or "").strip()


def chat(
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.4,
    max_tokens: int = 1200,
    timeout: float = 45.0,
) -> str:
    """DeepSeek chat only."""
    settings = get_settings()
    if not settings.llm_ready:
        return ""
    url = settings.deepseek_base_url.rstrip("/") + "/chat/completions"
    payload: dict[str, Any] = {
        "model": settings.deepseek_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                url,
                headers={
                    "Authorization": f"Bearer {settings.deepseek_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        resp.raise_for_status()
        return (resp.json()["choices"][0]["message"]["content"] or "").strip()
    except Exception as exc:
        logger.warning("DeepSeek failed: %s", type(exc).__name__)
        return ""


def gemini_chat(prompt: str, *, timeout: float = 8.0, max_tokens: int = 220) -> str:
    """Short Gemini reply with no search tool, used when DeepSeek is down."""
    settings = get_settings()
    if not settings.gemini_api_key:
        return ""
    model = (settings.gemini_model or "gemini-flash-lite-latest").strip()
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body: dict[str, Any] = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": max_tokens},
    }
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                url,
                headers={
                    "x-goog-api-key": settings.gemini_api_key.strip(),
                    "Content-Type": "application/json",
                },
                json=body,
            )
        resp.raise_for_status()
        data = resp.json()
        parts = data["candidates"][0]["content"]["parts"]
        return "".join(p.get("text") or "" for p in parts).strip()
    except Exception as exc:
        logger.warning("Gemini chat failed: %s", type(exc).__name__)
        return ""


def converse(system: str, user: str, *, timeout: float = 6.0) -> dict[str, Any]:
    """One short turn from a single model, so WhatsApp is not left waiting on two calls."""
    if get_settings().llm_ready:
        raw = chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.4,
            max_tokens=220,
            timeout=timeout,
        )
        data = _parse_json_obj(raw)
        if data:
            return data
        # DeepSeek spoke in plain text. Use it instead of starting Gemini.
        if raw:
            return {"reply": raw[:500]}
        return {}
    raw = gemini_chat(system + "\n\n" + user, timeout=timeout, max_tokens=220)
    return _parse_json_obj(raw) or ({"reply": raw[:500]} if raw else {})


def _parse_json_obj(raw: str) -> dict[str, Any]:
    import json
    import re

    if not raw:
        return {}
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I | re.S).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if not match:
            return {}
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}
    return data if isinstance(data, dict) else {}


def gemini_search(prompt: str) -> str:
    """Gemini + Google Search grounding — live job discovery (ThinkAbroad model)."""
    settings = get_settings()
    if not settings.gemini_api_key:
        return ""
    model = (settings.gemini_model or "gemini-flash-lite-latest").strip()
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body: dict[str, Any] = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 2048},
    }
    try:
        with httpx.Client(timeout=90.0) as client:
            resp = client.post(
                url,
                headers={
                    "x-goog-api-key": settings.gemini_api_key.strip(),
                    "Content-Type": "application/json",
                },
                json=body,
            )
        resp.raise_for_status()
        data = resp.json()
        parts = data["candidates"][0]["content"]["parts"]
        return "".join(p.get("text") or "" for p in parts).strip()
    except Exception as exc:
        logger.warning("Gemini search failed: %s", type(exc).__name__)
        return ""
