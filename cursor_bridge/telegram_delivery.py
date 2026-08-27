"""Telegram-specific prompt wrapping and delivery helpers."""

from __future__ import annotations

from cursor_sdk import UserMessage

TELEGRAM_DELIVERY_PREFIX = (
    "[cursor-telegram-bridge · Telegram] "
    "回复发到 Telegram（无桌面 Plan 面板）。完整答案写进这条回复，不要只预告。"
    "Plan：createPlan 由 bridge 转发，你只给短结论，勿再贴全文。"
    "\n\n"
)


def strip_telegram_delivery_prefix(text: str) -> str:
    """Return the user's message without the Telegram delivery wrapper."""
    if not text:
        return ""
    if text.startswith(TELEGRAM_DELIVERY_PREFIX):
        return text[len(TELEGRAM_DELIVERY_PREFIX):]
    if text.startswith("[cursor-telegram-bridge"):
        sep = text.find("\n\n")
        if sep != -1:
            return text[sep + 2 :]
    return text


def wrap_telegram_prompt(prompt: str | UserMessage) -> str | UserMessage:
    """Prefix user prompts so agents deliver full plans in-chat."""
    if isinstance(prompt, UserMessage):
        text = prompt.text or ""
        if text.startswith("[cursor-telegram-bridge"):
            return prompt
        # SDK UserMessage carries images (not attachments); preserve them when wrapping.
        return UserMessage(text=TELEGRAM_DELIVERY_PREFIX + text, images=prompt.images)
    if prompt.startswith("[cursor-telegram-bridge"):
        return prompt
    return TELEGRAM_DELIVERY_PREFIX + prompt
