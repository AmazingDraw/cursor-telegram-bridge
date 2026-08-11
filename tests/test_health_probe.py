#!/usr/bin/env python3
"""Tests for health probe recovery logic."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cursor_bridge.health import HealthProbe, should_count_as_poll_error


def test_should_count_as_poll_error() -> None:
    assert should_count_as_poll_error(TimeoutError("PoolTimeout"))
    assert should_count_as_poll_error(RuntimeError("httpx.ConnectError: boom"))
    assert should_count_as_poll_error(RuntimeError("Server disconnected without sending a response."))
    assert not should_count_as_poll_error(ValueError("bad parse"))
    assert not should_count_as_poll_error(None)


def test_health_probe_soft_restarts_then_kickstarts() -> None:
    soft_calls: list[int] = []
    kick_calls: list[int] = []

    def soft() -> None:
        soft_calls.append(1)

    async def kick() -> None:
        kick_calls.append(1)

    probe = HealthProbe(
        check_interval_sec=10,
        poll_fail_threshold=3,
        quiet_sec=60,
        kickstart_after_soft=2,
        soft_restart_cb=soft,
        kickstart_cb=kick,
        notify_cb=AsyncMock(),
        is_updater_running=lambda: False,
    )
    # Pretend we have been quiet long enough.
    probe.state.last_ok_at = 0.0
    probe.state.consecutive_poll_failures = 5

    async def run() -> None:
        await probe._tick()
        assert soft_calls == [1]
        assert kick_calls == []
        # Clear cooldown so next tick can escalate.
        probe.state.last_recovery_at = 0.0
        await probe._tick()
        assert soft_calls == [1, 1]
        probe.state.last_recovery_at = 0.0
        await probe._tick()
        assert kick_calls == [1]

    asyncio.run(run())


def test_health_probe_note_ok_resets_failures() -> None:
    probe = HealthProbe(poll_fail_threshold=2)
    probe.note_poll_error(RuntimeError("timeout"))
    probe.note_poll_error(RuntimeError("timeout"))
    assert probe.state.consecutive_poll_failures == 2
    probe.note_ok()
    assert probe.state.consecutive_poll_failures == 0


def test_heartbeat_success_resets_failure_clock() -> None:
    async def ok() -> None:
        return None

    probe = HealthProbe(heartbeat_cb=ok)
    probe.note_poll_error(RuntimeError("httpx.ConnectError: boom"))
    probe.state.last_ok_at = 0.0

    asyncio.run(probe._heartbeat_tick())

    assert probe.state.consecutive_poll_failures == 0
    assert probe.state.last_ok_at > 0.0


def test_heartbeat_failure_counts_as_poll_error() -> None:
    calls = {"n": 0}

    async def fail() -> None:
        calls["n"] += 1
        raise RuntimeError("httpx.ConnectError: boom")

    probe = HealthProbe(heartbeat_cb=fail)
    asyncio.run(probe._heartbeat_tick())
    asyncio.run(probe._heartbeat_tick())

    assert calls["n"] == 2
    assert probe.state.consecutive_poll_failures == 2


def test_heartbeat_stop_cancels_loop() -> None:
    calls = {"n": 0}

    async def ok() -> None:
        calls["n"] += 1
        return None

    async def run() -> None:
        probe = HealthProbe(
            heartbeat_cb=ok,
            heartbeat_interval_sec=10,
            check_interval_sec=10,
        )
        probe.start()
        await asyncio.sleep(0.05)
        await probe.stop()
        first = calls["n"]
        await asyncio.sleep(0.05)
        assert calls["n"] == first  # no more pings after stop

    asyncio.run(run())
