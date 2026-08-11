#!/usr/bin/env python3
"""Tests for Telegram plan delivery helpers."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cursor_sdk import UserMessage

from cursor_bridge.formatting import markdown_to_telegram_html, resolve_final_body
from cursor_bridge.telegram_delivery import TELEGRAM_DELIVERY_PREFIX, wrap_telegram_prompt


def test_plan_tool_output_becomes_final_body() -> None:
    teaser = "Findings are validated. Drafting a prioritized audit and remediation plan."
    plan = "## Summary\n\nPhase A: run lock\n\nPhase B: display fixes"
    hits = [("createPlan", {"plan": plan}, {})]

    body = resolve_final_body(sdk_final=teaser, text_parts=[teaser], tool_hits=hits)
    assert plan in body and teaser not in body, body

    body2 = resolve_final_body(sdk_final=teaser + "\n\n" + plan, text_parts=[], tool_hits=hits)
    assert plan in body2


def test_chinese_teaser_yields_tool_plan() -> None:
    teaser = "我将为您起草完整方案，稍等。"
    plan = "## 方案\n\n" + ("细节段落。\n" * 20)
    hits = [("createPlan", {"plan": plan}, {})]
    body = resolve_final_body(sdk_final=teaser, text_parts=[teaser], tool_hits=hits)
    assert body == plan.strip()


def test_duplicate_plan_document_prefers_tool() -> None:
    """Assistant rewrote a full plan while createPlan also has one — keep tool only."""
    plan = (
        "## Summary\n\n"
        "Phase A: run lock and watchdog\n\n"
        "Phase B: health probe kickstart\n\n"
        "Phase C: telegram pool isolation\n"
    )
    assistant = (
        "## 总览\n\n"
        "方案 C 落地细节如下，包含 watchdog、health probe 与连接池隔离，"
        "以及 Telegram 投递与排版相关说明，避免和工具文档叠两份。\n\n"
        "### 步骤\n\n"
        "1. 改提示\n"
        "2. 去重\n"
        "3. 排版\n"
    )
    hits = [("createPlan", {"plan": plan}, {})]
    body = resolve_final_body(sdk_final=assistant, text_parts=[assistant], tool_hits=hits)
    assert body == plan.strip()
    assert "---" not in body


def test_short_conclusion_can_preface_plan() -> None:
    plan = "## Summary\n\nPhase A\n\nPhase B\n\nPhase C done."
    assistant = "结论：按方案①落地，细节如下。"
    hits = [("createPlan", {"plan": plan}, {})]
    body = resolve_final_body(sdk_final=assistant, text_parts=[assistant], tool_hits=hits)
    assert body.startswith(assistant)
    assert plan.strip() in body
    assert "---" in body


def test_delivery_prefix_discourages_rewriting_plan() -> None:
    assert "自动投递" in TELEGRAM_DELIVERY_PREFIX
    assert "勿再贴" in TELEGRAM_DELIVERY_PREFIX
    assert "也必须写入聊天回复" not in TELEGRAM_DELIVERY_PREFIX
    assert "agent-transcripts" in TELEGRAM_DELIVERY_PREFIX
    assert "/context" in TELEGRAM_DELIVERY_PREFIX


def test_telegram_prompt_wrapping_is_idempotent() -> None:
    wrapped = wrap_telegram_prompt("audit please")
    assert wrapped.startswith("[cursor-telegram-bridge")
    assert "audit please" in wrapped
    assert wrap_telegram_prompt(wrapped) == wrapped

    assert TELEGRAM_DELIVERY_PREFIX in wrap_telegram_prompt("hi")


def test_wrap_telegram_prompt_preserves_user_message_images() -> None:
    """Regression: wrap must use images=, not nonexistent attachments=."""
    fake_image = MagicMock(name="SDKImage")
    msg = UserMessage(text="see this photo", images=[fake_image])
    wrapped = wrap_telegram_prompt(msg)
    assert isinstance(wrapped, UserMessage)
    assert wrapped.text.startswith("[cursor-telegram-bridge")
    assert "see this photo" in wrapped.text
    assert list(wrapped.images or []) == [fake_image]
    # Idempotent on already-wrapped UserMessage
    assert wrap_telegram_prompt(wrapped) is wrapped


def test_plan_html_headings_and_lists_breathe() -> None:
    md = (
        "## Summary\n"
        "Intro line.\n"
        "### Steps\n"
        "- alpha\n"
        "- beta\n"
        "1. one\n"
        "2. two\n"
    )
    html = markdown_to_telegram_html(md)
    assert "<b>【Summary】</b>\n\nIntro line." in html
    assert "<b><i>Steps</i></b>\n\n" in html
    assert "• alpha\n\n• beta" in html
    assert "1. one\n\n2. two" in html


def test_plan_html_major_heading_brackets_idempotent() -> None:
    assert "【结论】" in markdown_to_telegram_html("# 结论\nbody")
    assert markdown_to_telegram_html("## 【已有框】\nx").count("【") == 1
    assert "<b>【已有框】</b>" in markdown_to_telegram_html("## 【已有框】\nx")
    # Live preview (non-document) stays unbracketed.
    assert "<b>Live</b>" in markdown_to_telegram_html("# Live", max_code_lines=20)
    assert "【Live】" not in markdown_to_telegram_html("# Live", max_code_lines=20)
