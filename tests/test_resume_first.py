#!/usr/bin/env python3
"""Tests for Stage-5: resume the same agent after a bridge crash (context kept)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cursor_bridge.config import BotConfig, Config, load_config
from cursor_bridge.sessions import Session, SessionManager, STATUS_IDLE


def _cfg(tmp_path: Path, **overrides: object) -> Config:
    state = tmp_path / "state"
    state.mkdir()
    base: dict[str, object] = dict(
        telegram_token="t",
        cursor_api_key="k",
        allowed_user_id=1,
        projects_root=tmp_path,
        model="composer-2.5",
        browser_page_size=20,
        bookmarks=[],
        state_dir=state,
        event_log_max=50,
        console_enabled=False,
        console_host="127.0.0.1",
        console_port=9477,
        console_token="",
    )
    base.update(overrides)
    return Config(**base)  # type: ignore[arg-type]


def _mgr(tmp_path: Path, **cfg_overrides: object) -> SessionManager:
    cfg = _cfg(tmp_path, **cfg_overrides)
    return SessionManager(cfg, BotConfig(
        name="default",
        token="t",
        allowed_user_id=1,
        model="composer-2.5",
    ))


def _session(mgr: SessionManager, *, agent_id: str = "agent-old") -> Session:
    s = Session(
        short_id="s1",
        cwd="/tmp/proj",
        agent_id=agent_id,
        model="composer-2.5",
        mode="agent",
        agent=MagicMock(),
    )
    mgr.sessions["s1"] = s
    return s


def test_recover_resumes_same_agent_when_resume_ok(tmp_path: Path) -> None:
    mgr = _mgr(tmp_path)
    s = _session(mgr)
    mgr._drop_bridge = AsyncMock()
    mgr._close_agent = AsyncMock()
    mgr._resume = AsyncMock()
    mgr._recreate_agent = AsyncMock()
    mgr._persist = MagicMock()
    mgr.log_session_event = MagicMock()

    async def run() -> None:
        await mgr._recover_bridge_session(s, RuntimeError("Bridge request failed"))

    asyncio.run(run())

    mgr._resume.assert_awaited_once()
    mgr._recreate_agent.assert_not_called()
    assert s.status == STATUS_IDLE


def test_recover_falls_back_to_recreate_when_resume_fails(tmp_path: Path) -> None:
    mgr = _mgr(tmp_path)
    s = _session(mgr)
    mgr._drop_bridge = AsyncMock()
    mgr._close_agent = AsyncMock()
    mgr._resume = AsyncMock(side_effect=RuntimeError("internal error"))
    mgr._recreate_agent = AsyncMock()
    mgr._persist = MagicMock()
    mgr.log_session_event = MagicMock()

    async def run() -> None:
        await mgr._recover_bridge_session(s, RuntimeError("Bridge request failed"))

    asyncio.run(run())

    mgr._resume.assert_awaited_once()
    mgr._recreate_agent.assert_awaited_once()


def test_recover_recreates_directly_when_try_resume_disabled(tmp_path: Path) -> None:
    mgr = _mgr(tmp_path, try_resume_first=False)
    s = _session(mgr)
    mgr._drop_bridge = AsyncMock()
    mgr._close_agent = AsyncMock()
    mgr._resume = AsyncMock()
    mgr._recreate_agent = AsyncMock()
    mgr._persist = MagicMock()
    mgr.log_session_event = MagicMock()

    async def run() -> None:
        await mgr._recover_bridge_session(s, RuntimeError("Bridge request failed"))

    asyncio.run(run())

    mgr._resume.assert_not_called()
    mgr._recreate_agent.assert_awaited_once()


def test_config_parses_try_resume_first(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_1", "telegram-token")
    monkeypatch.setenv("CURSOR_API_KEY", "cursor-key")
    (tmp_path / "config.toml").write_text("try_resume_first = false\n", encoding="utf-8")
    cfg = load_config(tmp_path)
    assert cfg.try_resume_first is False

    (tmp_path / "config.toml").write_text("", encoding="utf-8")
    assert load_config(tmp_path).try_resume_first is True


def test_get_bridge_version_mismatch_logs_hint(tmp_path: Path) -> None:
    mgr = _mgr(tmp_path)
    with patch("cursor_bridge.sessions.AsyncClient.launch_bridge", AsyncMock(side_effect=RuntimeError(
        "cursor-sdk-bridge failed: Missing value for --tool-callback-auth-token",
    ))), patch("cursor_bridge.sessions.logger") as mock_logger:
        async def run() -> None:
            try:
                await mgr._get_bridge("/tmp/proj")
            except RuntimeError:
                pass

        asyncio.run(run())
        assert any(
            call.kwargs.get("exc_info") is not None
            and "cursor-sdk-bridge binary rejected SDK args" in str(call)
            for call in mock_logger.error.call_args_list
        ) or any(
            "cursor-sdk-bridge binary rejected SDK args"
            in str(call) for call in mock_logger.error.call_args_list
        )
