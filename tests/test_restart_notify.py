"""Restart notify must be delivered by the triggering bot only."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from cursor_bridge.bot import _save_restart_notify, _send_restart_notify
from cursor_bridge.config import BotConfig, Config


def _cfg(tmp_path: Path) -> Config:
    state = tmp_path / "state"
    state.mkdir()
    return Config(
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


def _app(cfg: Config, name: str, *, primary: bool) -> MagicMock:
    bot = MagicMock()
    bot.send_message = AsyncMock()
    app = MagicMock()
    app.bot = bot
    app.bot_data = {
        "cfg": cfg,
        "bot_cfg": BotConfig(
            name=name,
            token="tok",
            allowed_user_id=1,
            model="m",
        ),
        "is_primary_bot": primary,
    }
    return app


def test_save_restart_notify_includes_bot(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    _save_restart_notify(cfg, 42, mode="reload", bot="secondary")
    data = json.loads((cfg.state_dir / "restart_notify.json").read_text())
    assert data == {"chat_id": 42, "mode": "reload", "bot": "secondary"}


def test_wrong_bot_does_not_claim_notify(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    _save_restart_notify(cfg, 42, mode="reload", bot="secondary")

    async def run() -> None:
        default_app = _app(cfg, "default", primary=True)
        await _send_restart_notify(default_app)
        default_app.bot.send_message.assert_not_called()
        assert (cfg.state_dir / "restart_notify.json").exists()

        secondary_app = _app(cfg, "secondary", primary=False)
        await _send_restart_notify(secondary_app)
        secondary_app.bot.send_message.assert_awaited_once()
        assert not (cfg.state_dir / "restart_notify.json").exists()

    asyncio.run(run())


def test_legacy_notify_only_primary_claims(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    (cfg.state_dir / "restart_notify.json").write_text(
        json.dumps({"chat_id": 42, "mode": "restart"}),
        encoding="utf-8",
    )

    async def run() -> None:
        secondary = _app(cfg, "secondary", primary=False)
        await _send_restart_notify(secondary)
        secondary.bot.send_message.assert_not_called()
        assert (cfg.state_dir / "restart_notify.json").exists()

        primary = _app(cfg, "default", primary=True)
        await _send_restart_notify(primary)
        primary.bot.send_message.assert_awaited_once()
        assert not (cfg.state_dir / "restart_notify.json").exists()

    asyncio.run(run())
