from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)


def chat(messages: list[dict[str, str]], *, temperature: float = 0.4, max_tokens: int = 1200) -> str:
    """Chat completion. Prefers DeepSeek for dialogue; falls back to Gemini text."""
    settings = get_settings()
    if settings.deepseek_ready:
        text = _deepseek(messages, temperature=temperature, max_tokens=max_tokens)
        if text:
            return text
    if settings.gemini_ready:
        return _gemini_text(messages, temperature=temperature, max_tokens=max_tokens)
    return ""


def _deepseek(messages: list[dict[str, str]], *, temperature: float, max_tokens: int) -> str:
    settings = get_settings()
    url = settings.deepseek_base_url.rstrip("/") + "/chat/completions"
    payload: dict[str, Any] = {
        "model": settings.deepseek_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    try:
        with httpx.Client(timeout=60.0) as client:
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
        logger.warning("DeepSeek failed: %s", exc)
        return ""


def _gemini_text(messages: list[dict[str, str]], *, temperature: float, max_tokens: int) -> str:
    settings = get_settings()
    # Flatten OpenAI-style messages into Gemini contents
    system = ""
    contents: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role") or "user"
        if role == "system":
            system += (m.get("content") or "") + "\n"
            continue
        g_role = "model" if role == "assistant" else "user"
        contents.append({"role": g_role, "parts": [{"text": m.get("content") or ""}]})
    body: dict[str, Any] = {
        "contents": contents or [{"role": "user", "parts": [{"text": "hi"}]}],
        "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
    }
    if system.strip():
        body["systemInstruction"] = {"parts": [{"text": system.strip()}]}
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{settings.gemini_model}:generateContent"
    )
    try:
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(url, params={"key": settings.gemini_api_key}, json=body)
        resp.raise_for_status()
        data = resp.json()
        parts = data["candidates"][0]["content"]["parts"]
        return "".join(p.get("text") or "" for p in parts).strip()
    except Exception as exc:
        logger.warning("Gemini text failed: %s", exc)
        return ""


def gemini_search(prompt: str) -> str:
    """Gemini with Google Search grounding — best path for live job discovery."""
    settings = get_settings()
    if not settings.gemini_ready:
        return ""
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{settings.gemini_model}:generateContent"
    )
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 2048},
    }
    try:
        with httpx.Client(timeout=90.0) as client:
            resp = client.post(url, params={"key": settings.gemini_api_key}, json=body)
        resp.raise_for_status()
        data = resp.json()
        parts = data["candidates"][0]["content"]["parts"]
        return "".join(p.get("text") or "" for p in parts).strip()
    except Exception as exc:
        logger.warning("Gemini search grounding failed: %s", exc)
        return ""
