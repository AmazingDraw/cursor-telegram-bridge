#!/usr/bin/env python3
"""Tests for Stage-6: file browsing handlers keep working via to_thread."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cursor_bridge import bot as bot_mod
from cursor_bridge.folders import TokenStore


def _ctx() -> MagicMock:
    context = MagicMock()
    context.application.bot_data = {
        "cfg": SimpleNamespace(browser_page_size=20, projects_root=Path("/tmp")),
        "tokens": SimpleNamespace(path=lambda tok: "/tmp/foo.txt"),
    }
    return context


def _update() -> MagicMock:
    update = MagicMock()
    update.effective_chat.id = 42
    update.effective_user.id = 1
    update.message.reply_text = AsyncMock()
    return update


def _mgr(session: object) -> MagicMock:
    mgr = MagicMock()
    mgr.get_active.return_value = session
    return mgr


def test_cmd_files_lists_via_to_thread() -> None:
    update = _update()
    context = _ctx()
    session = SimpleNamespace(cwd="/tmp/proj", name="proj", short_id="s1")
    files = [Path("/tmp/proj/a.py"), Path("/tmp/proj/b.txt")]

    with patch.object(bot_mod, "_guard", AsyncMock(return_value=True)), \
         patch.object(bot_mod, "_mgr", return_value=_mgr(session)), \
         patch.object(bot_mod, "_cfg", return_value=SimpleNamespace(browser_page_size=20)), \
         patch.object(bot_mod, "_tokens", return_value=TokenStore()), \
         patch.object(bot_mod, "list_session_files", return_value=files) as mock_list:
        asyncio.run(bot_mod.cmd_files(update, context))

    mock_list.assert_called_once()
    update.message.reply_text.assert_awaited_once()
    kwargs = update.message.reply_text.call_args.kwargs
    assert "reply_markup" in kwargs


def test_cmd_files_find_uses_query() -> None:
    update = _update()
    context = _ctx()
    context.args = ["find", "invoice"]
    session = SimpleNamespace(cwd="/tmp/proj", name="proj", short_id="s1")

    with patch.object(bot_mod, "_guard", AsyncMock(return_value=True)), \
         patch.object(bot_mod, "_mgr", return_value=_mgr(session)), \
         patch.object(bot_mod, "_cfg", return_value=SimpleNamespace(browser_page_size=20)), \
         patch.object(bot_mod, "_tokens", return_value=TokenStore()), \
         patch.object(bot_mod, "search_session_files", return_value=[]) as mock_search:
        asyncio.run(bot_mod.cmd_files(update, context))

    mock_search.assert_called_once()
    args, kwargs = mock_search.call_args
    assert args[1] == "invoice"
    assert kwargs.get("limit") == 20


def test_cmd_files_no_active_session() -> None:
    update = _update()
    context = _ctx()
    with patch.object(bot_mod, "_guard", AsyncMock(return_value=True)), \
         patch.object(bot_mod, "_mgr", return_value=_mgr(None)):
        asyncio.run(bot_mod.cmd_files(update, context))
    update.message.reply_text.assert_awaited_once()


def test_cmd_browse_uses_to_thread() -> None:
    update = _update()
    context = _ctx()
    with patch.object(bot_mod, "_guard", AsyncMock(return_value=True)), \
         patch.object(bot_mod, "_cfg", return_value=SimpleNamespace(
             browser_page_size=20,
             projects_root=Path("/tmp"),
         )), \
         patch.object(bot_mod, "_tokens", return_value=TokenStore()), \
         patch.object(bot_mod, "browser_keyboard", return_value=MagicMock()) as mock_kb:
        asyncio.run(bot_mod.cmd_browse(update, context))

    mock_kb.assert_called_once()
    update.message.reply_text.assert_awaited_once()
