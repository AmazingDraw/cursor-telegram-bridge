#!/usr/bin/env python3
"""Tests for Stage-3 per-bot self-healing: updater restart + escalation wiring."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from telegram.error import NetworkError

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cursor_bridge import bot as bot_mod
from cursor_bridge.health import HealthProbe


def _health_cfg(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        health_check_interval_sec=60.0,
        health_poll_fail_threshold=8,
        health_quiet_sec=180.0,
        health_heartbeat_interval_sec=30.0,
        health_kickstart_after_soft=2,
        allowed_user_id=123456789,
        state_dir=tmp_path,
    )


def test_probe_awaits_async_soft_restart_cb() -> None:
    soft = AsyncMock()

    async def run() -> None:
        probe = HealthProbe(
            check_interval_sec=10,
            poll_fail_threshold=3,
            quiet_sec=60,
            kickstart_after_soft=2,
            soft_restart_cb=soft,
            notify_cb=AsyncMock(),
        )
        probe.state.last_ok_at = 0.0
        probe.state.consecutive_poll_failures = 5
        await probe._tick()
        assert soft.await_count == 1

    asyncio.run(run())


def test_probe_still_supports_sync_soft_restart_cb() -> None:
    calls = {"n": 0}

    def soft() -> None:
        calls["n"] += 1

    async def run() -> None:
        probe = HealthProbe(
            check_interval_sec=10,
            poll_fail_threshold=3,
            quiet_sec=60,
            soft_restart_cb=soft,
            notify_cb=AsyncMock(),
        )
        probe.state.last_ok_at = 0.0
        probe.state.consecutive_poll_failures = 5
        await probe._tick()
        assert calls["n"] == 1

    asyncio.run(run())


def test_restart_updater_stops_and_restarts_polling() -> None:
    app = MagicMock()
    app.bot_data = {"bot_cfg": SimpleNamespace(name="secondary")}
    app.updater = MagicMock()
    app.updater.running = True
    app.updater.stop = AsyncMock()
    app.updater.start_polling = AsyncMock()

    async def run() -> None:
        ok = await bot_mod._restart_updater(app)
        assert ok

    asyncio.run(run())
    app.updater.stop.assert_awaited_once()
    kwargs = app.updater.start_polling.call_args.kwargs
    assert kwargs.get("drop_pending_updates") is False
    assert callable(kwargs["error_callback"])


def test_restart_updater_returns_false_on_failure() -> None:
    app = MagicMock()
    app.bot_data = {"bot_cfg": SimpleNamespace(name="secondary")}
    app.updater = MagicMock()
    app.updater.running = False
    app.updater.start_polling = AsyncMock(side_effect=NetworkError("boom"))

    async def run() -> None:
        ok = await bot_mod._restart_updater(app)
        assert not ok

    asyncio.run(run())


def test_start_health_probe_wires_secondary_without_kickstart(tmp_path: Path) -> None:
    app = MagicMock()
    app.bot_data = {"is_primary_bot": False}
    app.bot.get_me = AsyncMock()
    app.updater = MagicMock()
    app.updater.running = True
    cfg = _health_cfg(tmp_path)
    bot_cfg = SimpleNamespace(name="secondary", allowed_user_id=123456789)

    async def run() -> None:
        bot_mod._start_health_probe(app, cfg, bot_cfg)
        probe = app.bot_data["health_probe"]
        try:
            assert probe.soft_restart_cb is not None  # heals its own updater
            assert probe.heartbeat_cb is not None
            assert probe.kickstart_cb is None  # must not nuke the whole process
        finally:
            await probe.stop()

    asyncio.run(run())


def test_start_health_probe_primary_keeps_kickstart(tmp_path: Path) -> None:
    app = MagicMock()
    app.bot_data = {"is_primary_bot": True}
    app.bot.get_me = AsyncMock()
    app.updater = MagicMock()
    app.updater.running = True
    cfg = _health_cfg(tmp_path)
    bot_cfg = SimpleNamespace(name="default", allowed_user_id=123456789)

    async def run() -> None:
        bot_mod._start_health_probe(app, cfg, bot_cfg)
        probe = app.bot_data["health_probe"]
        try:
            assert probe.kickstart_cb is not None
        finally:
            await probe.stop()

    asyncio.run(run())
