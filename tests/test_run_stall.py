#!/usr/bin/env python3
"""Tests for hung-run stall watchdog helpers / config wiring."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cursor_bridge.config import load_config
from cursor_bridge.sessions import (
    DEFAULT_RUN_STALL_TIMEOUT_SEC,
    STALL_AUTO_CONTINUE_MAX,
    STALL_AUTO_CONTINUE_PROMPT,
)


def test_default_stall_timeout_constant() -> None:
    assert DEFAULT_RUN_STALL_TIMEOUT_SEC == 180.0
    assert STALL_AUTO_CONTINUE_MAX == 1
    assert STALL_AUTO_CONTINUE_PROMPT == "继续"


def test_load_config_health_and_stall(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_1", "telegram-token")
    monkeypatch.setenv("CURSOR_API_KEY", "cursor-key")
    (tmp_path / "config.toml").write_text(
        "\n".join(
            [
                "run_stall_timeout_sec = 120",
                "stall_auto_continue_max = 5",
                "stall_auto_continue_prompt = '继续进行'",
                "health_check_interval_sec = 30",
                "health_poll_fail_threshold = 5",
                "health_quiet_sec = 90",
                "health_heartbeat_interval_sec = 20",
                "health_kickstart_after_soft = 1",
            ]
        ),
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    assert cfg.run_stall_timeout_sec == 120.0
    assert cfg.stall_auto_continue_max == 5
    assert cfg.stall_auto_continue_prompt == "继续进行"
    assert cfg.health_check_interval_sec == 30.0
    assert cfg.health_poll_fail_threshold == 5
    assert cfg.health_quiet_sec == 90.0
    assert cfg.health_heartbeat_interval_sec == 20.0
    assert cfg.health_kickstart_after_soft == 1


def test_load_config_clamps_heartbeat_minimum(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_1", "telegram-token")
    monkeypatch.setenv("CURSOR_API_KEY", "cursor-key")
    (tmp_path / "config.toml").write_text(
        "health_heartbeat_interval_sec = 3\n", encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    # Below the 10s minimum falls back to the 30s default.
    assert cfg.health_heartbeat_interval_sec == 30.0


def test_load_config_rejects_too_small_stall(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_1", "telegram-token")
    monkeypatch.setenv("CURSOR_API_KEY", "cursor-key")
    (tmp_path / "config.toml").write_text("run_stall_timeout_sec = 5\n", encoding="utf-8")
    cfg = load_config(tmp_path)
    # Below minimum falls back to default.
    assert cfg.run_stall_timeout_sec == 180.0
