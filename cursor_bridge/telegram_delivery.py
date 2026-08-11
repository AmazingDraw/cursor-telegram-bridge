"""Telegram-specific prompt wrapping and delivery helpers."""

from __future__ import annotations

from cursor_sdk import UserMessage

TELEGRAM_DELIVERY_PREFIX = (
    "[cursor-telegram-bridge · Telegram] "
    "用户在 Telegram 聊天中阅读回复 — 没有桌面端 Plan 面板。"
    "请将完整的计划、方案或回答直接呈现在回复中，不要停留在“我将为您起草”等预告性语句。"
    "Plan 模式下 createPlan 全文由 bridge 自动投递到聊天；"
    "你在回复里给短结论即可，勿再贴 createPlan 全文以免重复。"
    "禁止主动阅读 ~/.cursor/**/agent-transcripts/ 或其他会话的 "
    ".cursor_bridge/prior-context-*.md；需要历史时等用户 /context。"
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
