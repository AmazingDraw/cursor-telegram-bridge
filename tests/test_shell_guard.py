#!/usr/bin/env python3
"""Smoke tests for shell_guard."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cursor_bridge.shell_guard import is_blocked_self_management

BOT_PID = 4242

BLOCKED = [
    'launchctl kickstart -k "gui/$(id -u)/com.cursor-telegram-bridge.bot"',
    "launchctl bootout gui/501/com.cursor-telegram-bridge.bot",
    "launchctl unload ~/Library/LaunchAgents/com.cursor-telegram-bridge.bot.plist",
    "launchctl kill SIGTERM gui/501/com.cursor-telegram-bridge.bot",
    "launchctl remove com.cursor-telegram-bridge.bot",
    "pkill -f cursor_bridge",
    "kill $(pgrep -f 'python -m cursor_bridge')",
    "kill $(cat state/cursor_bridge.pid)",
    f"kill -9 {BOT_PID}",
    f'python -c "import os; os.kill({BOT_PID}, 9)"',
]

ALLOWED = [
    ".venv/bin/python scripts/test_context_restore.py",
    "launchctl list | grep cursor_bridge",
    "pytest cursor_bridge/",
    "cd /tmp && python foo.py",
    f"ps -p {BOT_PID}",
]


def test_self_management_commands_are_blocked() -> None:
    for cmd in BLOCKED:
        assert is_blocked_self_management(cmd, bot_pid=BOT_PID), f"should block: {cmd}"


def test_unrelated_shell_commands_are_allowed() -> None:
    for cmd in ALLOWED:
        assert not is_blocked_self_management(cmd, bot_pid=BOT_PID), f"should allow: {cmd}"


def test_current_bot_pid_is_blocked() -> None:
    assert is_blocked_self_management(
        f"kill {os.getpid()}",
        bot_pid=os.getpid(),
    )
