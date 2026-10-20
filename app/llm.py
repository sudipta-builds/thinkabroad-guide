from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)


def chat(messages: list[dict[str, str]], *, temperature: float = 0.4, max_tokens: int = 1200) -> str:
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
        logger.warning("DeepSeek failed: %s", type(exc).__name__)
        return ""


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
