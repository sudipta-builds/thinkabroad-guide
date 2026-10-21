from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Reply:
    """WhatsApp response: text, optional screenshots, and a reaction emoji."""

    text: str
    media_urls: list[str] = field(default_factory=list)
    emoji: str = ""

    @classmethod
    def of(cls, text: str, *media: str, emoji: str = "") -> "Reply":
        return cls(
            text=(text or "").strip(),
            media_urls=[m for m in media if m],
            emoji=(emoji or "").strip(),
        )
