"""Block agent shell commands that restart or kill cursor-telegram-bridge itself."""

from __future__ import annotations

import os
import re

from .formatting import _arg_text, _normalize_tool, parse_tool_args

LAUNCHD_LABEL = "com.cursor-telegram-bridge.bot"

_LAUNCHCTL = re.compile(
    r"launchctl\s+(?:kickstart|bootout|stop|unload|load|bootstrap|enable|disable|kill|remove|start)",
    re.IGNORECASE,
)
_KILL = re.compile(r"(?:pkill|killall|kill)\b", re.IGNORECASE)
_OS_KILL = re.compile(r"\bos\.kill\s*\(", re.IGNORECASE)
_LABEL = re.compile(re.escape(LAUNCHD_LABEL), re.IGNORECASE)
_CURSOR_BRIDGE = re.compile(r"cursor[_-]?bridge|cursor-telegram-bridge", re.IGNORECASE)
_PID_FILE = re.compile(r"cursor_bridge\.pid", re.IGNORECASE)
_KICKSTART_KILL = re.compile(r"launchctl\s+kickstart\s+-k", re.IGNORECASE)
_PLIST = re.compile(r"com\.cursor-telegram-bridge\.bot\.plist", re.IGNORECASE)


def shell_command_from_args(name: str, args: object) -> str:
    if _normalize_tool(name) not in ("shell", "runterminalcmd", "terminal"):
        return ""
    return _arg_text(parse_tool_args(args), "command", "cmd")


def _targets_bot_pid(text: str, bot_pid: int) -> bool:
    if str(bot_pid) not in text:
        return False
    return bool(_KILL.search(text) or _OS_KILL.search(text))


def is_blocked_self_management(
    command: str,
    *,
    bot_pid: int | None = None,
) -> bool:
    """True when a shell command would stop or restart this bot instance."""
    if not command or not command.strip():
        return False
    pid = bot_pid if bot_pid is not None else os.getpid()
    text = re.sub(r"\s+", " ", command.strip())
    if _LABEL.search(text) or _PLIST.search(text):
        if _LAUNCHCTL.search(text) or _KILL.search(text) or _OS_KILL.search(text):
            return True
    if _CURSOR_BRIDGE.search(text) and (_LAUNCHCTL.search(text) or _KILL.search(text)):
        return True
    if _PID_FILE.search(text) and (_KILL.search(text) or _OS_KILL.search(text)):
        return True
    if _targets_bot_pid(text, pid):
        return True
    # launchctl kickstart -k "gui/$(id -u)/com.cursor-telegram-bridge.bot" and similar.
    if _KICKSTART_KILL.search(text) and (
        _LABEL.search(text) or "gui/" in text.lower() or "$(id -u)" in text
    ):
        return True
    return False


def blocked_shell_message(command: str) -> str:
    return (
        "Blocked: agent shell cannot restart or stop the service.\n"
        "Use /reload (full code reload) or /restart (soft restart) from Telegram.\n"
        "Tests and scripts can run while the bot is up — no need to stop it first.\n"
        f"Command: {command[:240]}"
    )
