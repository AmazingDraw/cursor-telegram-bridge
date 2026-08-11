#!/usr/bin/env python3
"""Telegram command integrity regression (Stage 7).

Guards the "commands keep working" requirement: every command the README
documents must stay registered on every bot app, bound to an async callback,
and the text/media/callback routes must be present.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cursor_bridge import bot as bot_mod
from cursor_bridge.config import BotConfig, Config


EXPECTED_COMMANDS = {
    "start": "cmd_start",
    "help": "cmd_start",
    "new": "cmd_new",
    "browse": "cmd_browse",
    "cd": "cmd_cd",
    "sessions": "cmd_sessions",
    "use": "cmd_use",
    "status": "cmd_status",
    "compact": "cmd_compact",
    "context": "cmd_context",
    "model": "cmd_model",
    "effort": "cmd_effort",
    "mode": "cmd_mode",
    "busy": "cmd_busy",
    "rename": "cmd_rename",
    "title": "cmd_rename",
    "cancel": "cmd_cancel",
    "end": "cmd_end",
    "files": "cmd_files",
    "usage": "cmd_usage",
    "restart": "cmd_restart",
    "reload": "cmd_reload",
}


def _cfg(tmp_path: Path) -> Config:
    state = tmp_path / "state"
    state.mkdir()
    return Config(
        telegram_token="123:abc",
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


def _bot(name: str) -> BotConfig:
    return BotConfig(name=name, token="123:abc", allowed_user_id=1, model="composer-2.5")


def _command_map(app) -> dict[str, str]:
    found: dict[str, str] = {}
    for group in app.handlers.values():
        for handler in group:
            if isinstance(handler, CommandHandler):
                for cmd in handler.commands:
                    found[cmd] = getattr(handler.callback, "__name__", str(handler.callback))
    return found


@pytest.mark.parametrize("bot_name", ["default", "secondary"])
def test_all_commands_registered_on_every_bot(tmp_path: Path, bot_name: str) -> None:
    app = bot_mod._build_app(_cfg(tmp_path), _bot(bot_name))
    found = _command_map(app)

    for cmd, expected_cb in EXPECTED_COMMANDS.items():
        assert cmd in found, f"missing command /{cmd} on bot {bot_name}"
        assert found[cmd] == expected_cb, (
            f"/{cmd} bound to {found[cmd]}, expected {expected_cb}"
        )


def test_all_command_callbacks_are_async(tmp_path: Path) -> None:
    app = bot_mod._build_app(_cfg(tmp_path), _bot("default"))
    for group in app.handlers.values():
        for handler in group:
            if isinstance(handler, CommandHandler):
                assert inspect.iscoroutinefunction(handler.callback), (
                    f"{handler.callback} must be async"
                )


def test_route_handlers_present(tmp_path: Path) -> None:
    app = bot_mod._build_app(_cfg(tmp_path), _bot("default"))
    has_callback = False
    has_text = False
    has_media = False
    for group in app.handlers.values():
        for handler in group:
            if isinstance(handler, CallbackQueryHandler):
                has_callback = True
            elif isinstance(handler, MessageHandler):
                if str(handler.filters).startswith("<filters.TEXT"):
                    has_text = True
                else:
                    has_media = True
    assert has_callback
    assert has_text
    assert has_media


def test_cmd_start_returns_help() -> None:
    update = AsyncMock()
    update.effective_user.id = 1
    update.effective_chat.id = 42
    update.message.reply_text = AsyncMock()
    context = AsyncMock()
    context.application.bot_data = {"health_probe": None}

    with patch.object(bot_mod, "_guard", AsyncMock(return_value=True)):
        asyncio.run(bot_mod.cmd_start(update, context))

    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args.args[0]
    assert "cursor-telegram-bridge 命令指南" in text


def test_restart_and_reload_still_registered(tmp_path: Path) -> None:
    app = bot_mod._build_app(_cfg(tmp_path), _bot("default"))
    found = _command_map(app)
    assert found["restart"] == "cmd_restart"
    assert found["reload"] == "cmd_reload"
