"""Bridge-owned global rules injection at session edges."""

from __future__ import annotations

from cursor_sdk import UserMessage

RULES_MARKER = "[cursor-telegram-bridge · rules]"


def strip_rules_prefix(text: str) -> str:
    """Remove a leading bridge rules block if present."""
    if not text:
        return ""
    if not text.startswith(RULES_MARKER):
        return text
    # Marker line + rules body + blank line before the rest of the prompt.
    sep = text.find("\n\n")
    if sep == -1:
        return ""
    return text[sep + 2 :]


def wrap_with_rules(prompt: str | UserMessage, rules: str | None) -> str | UserMessage:
    """Prefix ``rules`` once (idempotent) ahead of the user prompt."""
    text_rules = (rules or "").strip()
    if not text_rules:
        return prompt

    block = f"{RULES_MARKER}\n{text_rules}\n\n"

    if isinstance(prompt, UserMessage):
        text = prompt.text or ""
        if text.startswith(RULES_MARKER):
            return prompt
        return UserMessage(text=block + text, images=prompt.images)

    if prompt.startswith(RULES_MARKER):
        return prompt
    return block + prompt
