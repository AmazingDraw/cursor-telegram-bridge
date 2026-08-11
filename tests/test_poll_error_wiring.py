#!/usr/bin/env python3
"""Tests for Stage-1 poll error wiring: start_polling error_callback -> _on_error.

PTB only routes polling failures into the application error handlers when going
through ``Application.run_polling``. This bridge drives ``Updater.start_polling``
directly, so the explicit callback must exist and must be a plain (non-coroutine)
function that never raises (PTB aborts the poll retry loop otherwise).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from telegram.error import Conflict, NetworkError

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cursor_bridge import bot as bot_mod


class _FakeApp:
    """Minimal app double: records scheduled tasks, processes errors."""

    def __init__(self) -> None:
        self.scheduled: list[object] = []

    def create_task(self, coro, **kwargs):
        self.scheduled.append(coro)

    async def process_error(self, error, update=None):
        self.scheduled.append(("processed", error, update))


def test_poll_error_callback_is_sync_callable() -> None:
    app = _FakeApp()
    cb = bot_mod._make_poll_error_callback(app)
    assert callable(cb)
    assert not asyncio.iscoroutinefunction(cb)


def test_poll_error_callback_routes_to_process_error() -> None:
    app = _FakeApp()
    cb = bot_mod._make_poll_error_callback(app)
    exc = NetworkError("httpx.ConnectError: boom")

    cb(exc)

    assert len(app.scheduled) == 1
    coro = app.scheduled[0]
    assert asyncio.iscoroutine(coro)
    asyncio.run(coro)  # type: ignore[arg-type]
    assert app.scheduled[-1] == ("processed", exc, None)


def test_poll_error_callback_never_raises() -> None:
    class _BoomApp:
        def create_task(self, coro, **kwargs):
            raise RuntimeError("create_task failed")

    cb = bot_mod._make_poll_error_callback(_BoomApp())
    cb(NetworkError("x"))  # must not raise into the poll retry loop


def test_async_run_apps_passes_error_callback(tmp_path: Path) -> None:
    app = AsyncMock()
    app.bot_data = {"cfg": SimpleNamespace(state_dir=tmp_path)}
    app.post_init = AsyncMock()
    app.post_shutdown = AsyncMock()
    app.updater = MagicMock()
    app.updater.running = False
    app.updater.start_polling = AsyncMock()
    cfg = SimpleNamespace(state_dir=tmp_path)
    bot_mod._request_restart(cfg)  # pre-set so the app loop exits right away
    try:
        asyncio.run(bot_mod._async_run_apps(cfg, [app], drop_pending=False))
    finally:
        bot_mod._clear_restart_request(cfg)

    kwargs = app.updater.start_polling.call_args.kwargs
    assert "error_callback" in kwargs
    assert callable(kwargs["error_callback"])
    assert not asyncio.iscoroutinefunction(kwargs["error_callback"])


def test_on_error_network_error_counts_on_probe() -> None:
    app = MagicMock()
    probe = MagicMock()
    app.bot_data = {"health_probe": probe, "cfg": None}
    context = MagicMock()
    context.error = NetworkError("httpx.ConnectError: boom")
    context.application = app

    asyncio.run(bot_mod._on_error(None, context))

    probe.note_poll_error.assert_called_once()


def test_on_error_conflict_requests_restart_and_counts(tmp_path: Path) -> None:
    app = MagicMock()
    probe = MagicMock()
    cfg = SimpleNamespace(state_dir=tmp_path)
    app.bot_data = {"health_probe": probe, "cfg": cfg}
    context = MagicMock()
    context.error = Conflict("terminated by other getUpdates request")
    context.application = app

    try:
        asyncio.run(bot_mod._on_error(None, context))
        assert bot_mod._restart_wanted(cfg)
        probe.note_poll_error.assert_called_once()
    finally:
        bot_mod._clear_restart_request(cfg)
