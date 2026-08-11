"""Live Mac console — session status + recent bot activity."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

from .config import load_config
from .context import format_context_line, get_context_info
from .webconsole import _err_log_path, _load_all_sessions

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REFRESH_S = 2
LOG_LINES = 22


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _bot_pid(state_dir: Path) -> int | None:
    path = state_dir / "cursor_bridge.pid"
    if not path.is_file():
        return None
    try:
        pid = int(path.read_text().strip())
    except ValueError:
        return None
    return pid if _pid_alive(pid) else None


def _tail(path: Path, n: int) -> list[str]:
    if not path.is_file():
        return ["(no log yet — bot may be starting)"]
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return [f"(could not read log: {exc})"]
    if not lines:
        return ["(log empty)"]
    return lines[-n:]


def _badge(status: str) -> str:
    if status == "running":
        return "\U0001F7E2"
    if status == "error":
        return "\U0001F534"
    return "\U0001F7E1"


def _render(cfg, *, log_path: Path) -> str:
    pid = _bot_pid(cfg.state_dir)
    active_ids, sessions, any_running = _load_all_sessions(cfg)

    if pid is None:
        headline = "\U0001F534 cursor-telegram-bridge is not running"
        hint = "Start it with start.command or launchctl load ~/Library/LaunchAgents/com.cursor-telegram-bridge.bot.plist"
    elif any_running:
        headline = "\U0001F7E2 SESSION LIVE — agent is working"
        hint = "Watch tool/prompt lines below. Ctrl-C closes this window only (bot keeps running)."
    else:
        headline = "\U0001F7E1 cursor-telegram-bridge online — waiting for Telegram"
        hint = "Send a message or /status from Telegram. Ctrl-C closes this window only."

    now = datetime.now().strftime("%H:%M:%S")
    lines = [
        "",
        "\u250c\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2510",
        f"\u2502  cursor-bridge live console        {now} \u2502",
        "\u2514\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2518",
        "",
        headline,
        f"Bot pid: {pid or '—'}",
        hint,
        "",
        "Sessions:",
    ]

    if not sessions:
        lines.append("  (none — use /new in Telegram)")
    else:
        for s in sessions:
            sid = s.get("short_id", "?")
            star = " \u2605" if sid in active_ids else ""
            badge = _badge(str(s.get("status", "idle")))
            ctx = get_context_info(s.get("agent_id"), s.get("cwd", ""))
            prompt = (s.get("last_prompt") or "")[:60]
            if len(s.get("last_prompt") or "") > 60:
                prompt += "\u2026"
            cwd = s.get("cwd", "")
            folder = s.get("custom_name") or Path(cwd).name
            lines.append(
                f"  {badge} [{sid}]{star} {s.get('status', 'idle')} \u00b7 "
                f"{s.get('model', '')} \u00b7 {folder}"
            )
            lines.append(f"     {format_context_line(ctx)}")
            if prompt:
                lines.append(f"     last: {prompt}")

    lines.extend(["", f"Recent activity ({log_path.name}):", "\u2500" * 47])
    lines.extend(_tail(log_path, LOG_LINES))
    lines.append("")
    lines.append(f"Refreshing every {REFRESH_S}s\u2026")
    if cfg.console_enabled:
        url = f"http://{cfg.console_host}:{cfg.console_port}"
        lines.append(f"Web console: {url}")
    return "\n".join(lines)


def main() -> None:
    os.chdir(PROJECT_ROOT)
    cfg = load_config(PROJECT_ROOT)
    log_path = _err_log_path(cfg.state_dir)

    try:
        while True:
            print("\033[2J\033[H", end="")
            print(_render(cfg, log_path=log_path))
            time.sleep(REFRESH_S)
    except KeyboardInterrupt:
        print("\nConsole closed. Bot keeps running in the background.")


if __name__ == "__main__":
    main()
