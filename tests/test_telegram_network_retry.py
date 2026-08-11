#!/usr/bin/env python3
"""Tests for Telegram network retry / live-message resilience."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from telegram.error import BadRequest, NetworkError, RetryAfter

from cursor_bridge import bot as bot_mod


def test_is_retryable_excludes_bad_request() -> None:
    assert bot_mod._is_retryable_telegram_network(NetworkError("boom"))
    assert not bot_mod._is_retryable_telegram_network(BadRequest("bad"))
    assert not bot_mod._is_retryable_telegram_network(ValueError("x"))


def test_await_telegram_retries_network_then_succeeds() -> None:
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise NetworkError("Server disconnected without sending a response.")
        return "ok"

    async def run():
        with patch.object(bot_mod.asyncio, "sleep", AsyncMock()):
            return await bot_mod._await_telegram(flaky, max_network_retries=5)

    assert asyncio.run(run()) == "ok"
    assert calls["n"] == 3


def test_await_telegram_does_not_retry_bad_request() -> None:
    async def bad():
        raise BadRequest("can't parse entities")

    async def run():
        await bot_mod._await_telegram(bad, max_network_retries=5)

    try:
        asyncio.run(run())
        raise AssertionError("expected BadRequest")
    except BadRequest:
        pass


def test_await_telegram_exhausts_and_raises() -> None:
    async def always():
        raise NetworkError("Server disconnected without sending a response.")

    async def run():
        with patch.object(bot_mod.asyncio, "sleep", AsyncMock()):
            await bot_mod._await_telegram(always, max_network_retries=2)

    try:
        asyncio.run(run())
        raise AssertionError("expected NetworkError")
    except NetworkError:
        pass


def test_await_telegram_respects_retry_after() -> None:
    calls = {"n": 0}

    async def once():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RetryAfter(1)
        return "ok"

    async def run():
        with patch.object(bot_mod.asyncio, "sleep", AsyncMock()):
            return await bot_mod._await_telegram(once)

    assert asyncio.run(run()) == "ok"
    assert calls["n"] == 2


def test_live_message_flush_swallows_network_error() -> None:
    bot = MagicMock()
    live = bot_mod.LiveMessage(bot, chat_id=1, message_id=2)
    live._pending = "hello"

    async def run():
        with patch.object(
            bot_mod,
            "_await_telegram",
            AsyncMock(side_effect=NetworkError("Server disconnected without sending a response.")),
        ):
            await live._flush()

    asyncio.run(run())
    assert live._pending == "hello"
    assert live._last == ""


def test_send_html_chunks_falls_back_when_edit_fails() -> None:
    bot = MagicMock()

    async def run():
        with patch.object(bot_mod, "_edit_html_message", AsyncMock(return_value=False)):
            with patch.object(
                bot_mod, "_await_telegram", AsyncMock(return_value=MagicMock()),
            ) as await_tg:
                await bot_mod._send_html_chunks(
                    bot, 1, ["part-a", "part-b"], message_id=99,
                )
                assert await_tg.await_count == 2

    asyncio.run(run())
