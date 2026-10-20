from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Reply:
    """WhatsApp response: text plus optional image URLs (job page screenshots)."""

    text: str
    media_urls: list[str] = field(default_factory=list)

    @classmethod
    def of(cls, text: str, *media: str) -> "Reply":
        return cls(text=text, media_urls=[m for m in media if m])
