#!/usr/bin/env python3
"""Tests for the persistent per-bot Telegram outbox."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from telegram.error import BadRequest, NetworkError

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cursor_bridge import bot as bot_mod
from cursor_bridge.outbox import TelegramOutbox


def test_enqueue_persists_and_reloads(tmp_path: Path) -> None:
    path = tmp_path / "outbox.jsonl"
    box = TelegramOutbox(path)
    box.enqueue(chat_id=42, text="hello", parse_mode="HTML")
    assert box.pending_count() == 1

    reloaded = TelegramOutbox(path)
    assert reloaded.pending_count() == 1
    item = reloaded._items[0]
    assert item.chat_id == 42
    assert item.text == "hello"
    assert item.parse_mode == "HTML"


def test_deliver_once_clears_on_success(tmp_path: Path) -> None:
    box = TelegramOutbox(tmp_path / "outbox.jsonl")
    box.enqueue(chat_id=42, text="ok")
    sent: list[tuple[int, str, str | None]] = []

    async def send(chat_id: int, text: str, parse_mode: str | None) -> bool:
        sent.append((chat_id, text, parse_mode))
        return True

    box._send_cb = send

    async def run() -> None:
        assert await box._deliver_once() == 1

    asyncio.run(run())
    assert sent == [(42, "ok", None)]
    assert box.pending_count() == 0
    assert not (tmp_path / "outbox.jsonl").read_text().strip()


def test_deliver_once_retries_failed_item(tmp_path: Path) -> None:
    box = TelegramOutbox(tmp_path / "outbox.jsonl")
    box.enqueue(chat_id=42, text="nope")

    async def send(chat_id: int, text: str, parse_mode: str | None) -> bool:
        return False

    box._send_cb = send
    asyncio.run(box._deliver_once())
    assert box.pending_count() == 1
    item = box._items[0]
    assert item.attempts == 1
    assert item.last_error


def test_deliver_once_drops_expired(tmp_path: Path) -> None:
    box = TelegramOutbox(
        tmp_path / "outbox.jsonl",
        retry_interval_sec=10,
        max_age_sec=300,
    )
    box.enqueue(chat_id=42, text="old")
    box._items[0].created_at = 0.0

    async def send(chat_id: int, text: str, parse_mode: str | None) -> bool:
        raise AssertionError("expired item must not be delivered")

    box._send_cb = send
    asyncio.run(box._deliver_once())
    assert box.pending_count() == 0


def test_load_skips_malformed_lines(tmp_path: Path) -> None:
    path = tmp_path / "outbox.jsonl"
    path.write_text("not-json\n", encoding="utf-8")
    box = TelegramOutbox(path)
    assert box.pending_count() == 0


def test_concurrent_enqueue_during_delivery_is_retained(tmp_path: Path) -> None:
    box = TelegramOutbox(tmp_path / "outbox.jsonl")
    box.enqueue(chat_id=1, text="item1")

    async def send_cb(chat_id: int, text: str, parse_mode: str | None) -> bool:
        if text == "item1":
            # Simulate a new message being queued mid-send
            box.enqueue(chat_id=2, text="item2")
        return True

    box._send_cb = send_cb

    async def run() -> None:
        await box._deliver_once()

    asyncio.run(run())
    # item1 was delivered, item2 was added mid-send and must be retained
    assert box.pending_count() == 1
    assert box._items[0].text == "item2"


def test_stop_cancels_loop_and_persists(tmp_path: Path) -> None:
    box = TelegramOutbox(
        tmp_path / "outbox.jsonl",
        retry_interval_sec=10,
    )
    box.enqueue(chat_id=42, text="persist me")

    async def run() -> None:
        box.start(send_cb=lambda chat_id, text, parse_mode: _never())
        await asyncio.sleep(0.05)
        await box.stop()

    asyncio.run(run())
    reloaded = TelegramOutbox(tmp_path / "outbox.jsonl")
    assert reloaded.pending_count() == 1


async def _never() -> bool:
    return False


def test_send_html_chunks_enqueues_remaining_on_network_error(tmp_path: Path) -> None:
    class _FakeBot:
        async def edit_message_text(self, *a, **k):
            raise NetworkError("httpx.ConnectError: boom")

        async def send_message(self, *a, **k):
            raise NetworkError("httpx.ConnectError: boom")

    box = TelegramOutbox(tmp_path / "outbox.jsonl")

    async def run() -> None:
        with patch.object(bot_mod.asyncio, "sleep", AsyncMock()):
            await bot_mod._send_html_chunks(
                _FakeBot(),
                42,
                ["part-0", "part-1", "part-2"],
                message_id=7,
                outbox=box,
            )

    asyncio.run(run())
    # Edit path fails first (BadRequest on missing message), then send_message
    # fails on part 0 -> remaining parts are enqueued.
    assert box.pending_count() == 3
    texts = [i.text for i in box._items]
    assert texts == ["part-0", "part-1", "part-2"]


def test_outbox_sender_drops_bad_request_retries_network(tmp_path: Path) -> None:
    class _FakeApp:
        def __init__(self) -> None:
            self.bot = _FakeBot()

    class _FakeBot:
        async def send_message(self, *a, **k):
            raise BadRequest("can't parse entities")

    app = _FakeApp()
    send = bot_mod._make_outbox_sender(app)  # type: ignore[arg-type]
    assert asyncio.run(send(42, "x", "HTML")) is True  # permanent -> drop

    class _NetBot:
        async def send_message(self, *a, **k):
            raise NetworkError("httpx.ConnectError: boom")

    app2 = _FakeApp()
    app2.bot = _NetBot()
    send2 = bot_mod._make_outbox_sender(app2)  # type: ignore[arg-type]
    with patch.object(bot_mod.asyncio, "sleep", AsyncMock()):
        assert asyncio.run(send2(42, "x", None)) is False  # transient -> retry
