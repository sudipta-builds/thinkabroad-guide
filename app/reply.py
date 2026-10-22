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


def strip_leading_emoji(text: str) -> str:
    """Remove a leading emoji (and spaces) so tap-backs are not duplicated in the bubble."""
    spoken = (text or "").strip()
    if not spoken or spoken[0].isalnum():
        return spoken
    i = 0
    while i < len(spoken) and not spoken[i].isalnum():
        if spoken[i] in ".!?\"'(":
            break
        i += 1
    cleaned = spoken[i:].lstrip()
    return cleaned if cleaned else spoken


def for_send(reply: Reply, *, reacted: bool) -> Reply:
    """Emoji is a tap-back when reacted; only fall back into text when reaction failed."""
    text = strip_leading_emoji(reply.text)
    emoji = (reply.emoji or "").strip()
    if emoji and text.startswith(emoji):
        text = text[len(emoji) :].lstrip()
    if emoji and not reacted and text and not text.startswith(emoji):
        text = f"{emoji} {text}".strip()
    return Reply(text=text, media_urls=list(reply.media_urls or []), emoji=emoji)
