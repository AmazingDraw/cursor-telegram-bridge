from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import signal
import socket
import subprocess
import time
import httpx
from pathlib import Path

# Prefer IPv4 for Telegram — IPv6 paths on this Mac often fail with SSL EOF / TimedOut.
_orig_getaddrinfo = socket.getaddrinfo


def _ipv4_first_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if host == "api.telegram.org" and family == socket.AF_UNSPEC:
        family = socket.AF_INET
    return _orig_getaddrinfo(host, port, family, type, proto, flags)


socket.getaddrinfo = _ipv4_first_getaddrinfo

from telegram import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, Conflict, NetworkError, RetryAfter
from telegram.ext import (
    AIORateLimiter,
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    Defaults,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

from cursor_sdk import CursorAgentError, UserMessage

from . import __version__
from .attachments import (
    classify_attachment,
    files_dir_keyboard,
    files_keyboard,
    list_dir_files,
    list_session_files,
    max_bytes_for,
    resolve_attachment,
    search_session_files,
)
from .config import BotConfig, Config, load_config
from .context import format_context_html, format_context_line, fmt_tokens
from .folders import TokenStore, browser_keyboard, projects_keyboard
from .formatting import (
    build_final_html,
    build_live_html,
    chunk_telegram_html,
    html_to_plain_preview,
    looks_like_plan_document,
)
from .rules import strip_rules_prefix, wrap_with_rules
from .telegram_delivery import strip_telegram_delivery_prefix, wrap_telegram_prompt
from .inbound import (
    INBOUND_BATCH_DELAY_SEC,
    InboundBatcher,
    PendingInbound,
    build_combined_user_message,
    describe_inbound,
    save_inbound_attachment,
)
from .models import format_model_display, model_picker_label, model_set_notice
from .sessions import (
    MODES,
    STATUS_ERROR,
    STATUS_GLITCH,
    STATUS_IDLE,
    STATUS_RUNNING,
    QueuedPrompt,
    Session,
    SessionBusyError,
    SessionManager,
)
from .usage import UsageError, fetch_usage, format_usage_html
from .health import HealthProbe, should_count_as_poll_error
from .outbox import TelegramOutbox
from .state_layout import bot_name_for, bot_state_dir
from .webconsole import WebConsole

logger = logging.getLogger("cursor_bridge")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TG_LIMIT = 4096

# Set by /restart; also persisted to state/restart_requested on disk.
_pending_restart = False

HELP = (
    "<b>cursor-telegram-bridge 命令指南</b>\n\n"
    "<b>项目与会话管理</b>\n"
    "/new [路径] — 启动新会话（留空打开目录选择器）\n"
    "/browse [路径] — 逐级浏览选择项目目录\n"
    "/cd &lt;路径&gt; — 在指定绝对路径启动新会话\n"
    "/sessions — 查看会话列表、切换/取消/结束会话\n"
    "/use &lt;id&gt; — 切换当前激活的会话\n"
    "/status — 查看当前激活会话、运行状态与上下文\n"
    "/rename &lt;名称&gt; — 修改当前激活会话的自定义名称\n"
    "/model — 切换当前会话的模型\n"
    "/effort — 设置思考等级 (支持 Reasoning 的模型)\n"
    "/mode [agent|plan] — 查看或切换 agent/plan 模式\n"
    "/busy [interrupt|queue] — 忙碌时新消息：排队/发送/取消(默认) 或 打断\n"
    "/cancel — 取消当前正在运行的任务\n"
    "/end &lt;id&gt; — 关闭并释放指定会话\n"
    "/usage — 查看订阅与额度使用情况\n"
    "/restart — 软重启（重新加载 .env/config）\n"
    "/reload — 重新加载后台守护进程（更新代码）\n\n"
    "直接发送文字消息即可向当前激活的会话发起对话。"
)

RESTART_DELAY_S = 2.0
LAUNCHD_LABEL = "com.cursor-telegram-bridge.bot"
BUSY_POLICIES = ("interrupt", "queue")
BUSY_POLICY_FILE = "busy_policy"


# --------------------------------------------------------------------------
# logging & process management
# --------------------------------------------------------------------------

def _setup_logging() -> None:
    """Only cursor_bridge logs at INFO — everything else is WARNING+."""
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s  %(levelname)-5s  %(name)-18s  %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )
    for name in ("cursor_bridge", "cursor_bridge.sessions"):
        logging.getLogger(name).setLevel(logging.INFO)


def _log_action(action: str, **details: object) -> None:
    if details:
        tail = "  ".join(f"{k}={v}" for k, v in details.items())
        logger.info("%s  %s", action, tail)
    else:
        logger.info(action)


def _telegram_actor(update: Update) -> dict[str, int | None]:
    user = update.effective_user
    chat = update.effective_chat
    return {
        "user_id": user.id if user else None,
        "chat_id": chat.id if chat else None,
    }


def _callback_origin(query) -> dict[str, object]:
    msg = query.message
    out: dict[str, object] = {
        "user_id": query.from_user.id if query.from_user else None,
        "chat_id": msg.chat_id if msg else None,
        "callback_data": query.data or "",
    }
    if msg:
        out["message_id"] = msg.message_id
        if msg.date:
            out["message_age_s"] = round(time.time() - msg.date.timestamp(), 1)
    return out


def _stash_end_request(context: ContextTypes.DEFAULT_TYPE, sid: str, via: str, **extra: object) -> None:
    context.user_data["pending_end"] = {"sid": sid, "via": via, **extra}


def _pop_end_request(context: ContextTypes.DEFAULT_TYPE, sid: str) -> dict[str, object]:
    pending = context.user_data.pop("pending_end", None)
    if isinstance(pending, dict) and pending.get("sid") == sid:
        return {k: v for k, v in pending.items() if k != "sid"}
    return {}


def _log_sessions_view(mgr: SessionManager, via: str, **extra: object) -> None:
    chat_id = extra.get("chat_id")
    active = mgr.active.get(chat_id) if isinstance(chat_id, int) else None
    ids = ", ".join(sorted(mgr.sessions))
    _log_action("Sessions listed", via=via, active=active or "(none)", sessions=ids or "(none)", **extra)


def _pid_file(cfg: Config) -> Path:
    return cfg.state_dir / "cursor_bridge.pid"


def _restart_flag(cfg: Config) -> Path:
    return cfg.state_dir / "restart_requested"


def _restart_wanted(cfg: Config) -> bool:
    return _pending_restart or _restart_flag(cfg).exists()


def _request_restart(
    cfg: Config,
    chat_id: int | None = None,
    mode: str = "restart",
    *,
    bot: str | None = None,
) -> None:
    global _pending_restart
    _pending_restart = True
    _restart_flag(cfg).write_text("1")
    if chat_id is not None:
        _save_restart_notify(cfg, chat_id, mode=mode, bot=bot)


def _clear_restart_request(cfg: Config) -> None:
    global _pending_restart
    _pending_restart = False
    _restart_flag(cfg).unlink(missing_ok=True)


def _stop_pid(pid: int, label: str) -> None:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    except PermissionError:
        logger.warning("Cannot signal %s (pid %d) — permission denied", label, pid)
        return

    logger.warning("Stopping %s (pid %d)", label, pid)
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return

    deadline = time.time() + 5.0
    while time.time() < deadline:
        time.sleep(0.25)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            logger.info("%s stopped (pid %d)", label, pid)
            return

    logger.warning("Force-killing %s (pid %d)", label, pid)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _other_bridge_pids() -> list[int]:
    """Find other python -m cursor_bridge processes (excluding this one)."""
    try:
        out = subprocess.run(
            ["ps", "-ax", "-o", "pid=,command="],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return []
    pids: list[int] = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if "-m cursor_bridge" not in line:
            continue
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        pid_str, cmd = parts
        exe = cmd.split()[0]
        # Match real Python processes only — skip shell wrappers that embed the command.
        if not exe.endswith("python") and "/python" not in exe:
            continue
        try:
            pid = int(pid_str)
        except ValueError:
            continue
        if pid != os.getpid():
            pids.append(pid)
    return pids


def _claim_singleton(cfg: Config) -> None:
    """Ensure only one bridge instance is polling Telegram."""
    path = _pid_file(cfg)
    if path.exists():
        try:
            old_pid = int(path.read_text().strip())
        except ValueError:
            old_pid = 0
        if old_pid and old_pid != os.getpid():
            _stop_pid(old_pid, "previous bridge instance (pid file)")

    for pid in _other_bridge_pids():
        _stop_pid(pid, "duplicate bridge instance")

    path.write_text(str(os.getpid()))
    logger.debug("Wrote pid file %s (pid %d)", path, os.getpid())


def _release_pid_file(cfg: Config) -> None:
    path = _pid_file(cfg)
    if not path.exists():
        return
    try:
        if int(path.read_text().strip()) == os.getpid():
            path.unlink()
    except ValueError:
        path.unlink(missing_ok=True)


def _banner_line(label: str, value: str, width: int = 42) -> str:
    text = f"{label} {value}"
    if len(text) > width:
        text = text[: width - 1] + "\u2026"
    return f"\u2502  {text:<{width}}\u2502"


def _print_startup_banner(
    cfg: Config,
    mgr: SessionManager | None = None,
    bot_cfg: BotConfig | None = None,
) -> None:
    session_count = len(mgr.sessions) if mgr else 0
    session_ids = ", ".join(mgr.sessions) if mgr and mgr.sessions else "(none)"
    active = next(iter(mgr.active.values()), None) if mgr and mgr.active else None

    bot_name = bot_cfg.name if bot_cfg else "default"
    model = (bot_cfg.model if bot_cfg else cfg.model) or cfg.model
    user_id = (bot_cfg.allowed_user_id if bot_cfg else cfg.allowed_user_id) or cfg.allowed_user_id

    lines = [
        "",
        "\u250c\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2510",
        _banner_line("cursor-bridge", f"{__version__} [{bot_name}]"),
        _banner_line("Projects:", str(cfg.projects_root)),
        _banner_line("Model:", model),
        _banner_line("Sessions:", f"{session_count} restored ({session_ids})"),
        _banner_line("Active:", active or "(none)"),
        _banner_line("User:", str(user_id or "unconfigured")),
        "\u2514\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2518",
        "",
    ]
    for line in lines:
        logger.info(line)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _cfg(context: ContextTypes.DEFAULT_TYPE) -> Config:
    return context.application.bot_data["cfg"]


def _mgr(context: ContextTypes.DEFAULT_TYPE) -> SessionManager:
    mgr = context.application.bot_data.get("manager")
    if mgr is None:
        # Fallback if post_init has not run yet — should be rare after startup fix.
        cfg = _cfg(context)
        bot_cfg = _bot_cfg(context)
        mgr = SessionManager(cfg, bot_cfg)
        context.application.bot_data["manager"] = mgr
        logger.warning("SessionManager created lazily — post_init may have been skipped")
    return mgr


def _tokens(context: ContextTypes.DEFAULT_TYPE) -> TokenStore:
    return context.application.bot_data["tokens"]


def _bot_cfg(context: ContextTypes.DEFAULT_TYPE) -> BotConfig | None:
    return context.application.bot_data.get("bot_cfg")


async def _guard(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    require_owner: bool = False,
) -> bool:
    """Allow the bot owner, or (for non-admin traffic) members of whitelisted groups.

    Private chats always require ``allowed_user_id``. Group/supergroup messages are
    allowed when ``chat.id`` is listed in the bot's ``allowed_chat_ids``. Admin
    actions (``require_owner=True``) stay owner-only even inside those groups.
    """
    cfg = _cfg(context)
    bot_cfg = _bot_cfg(context)
    allowed_id = bot_cfg.allowed_user_id if bot_cfg else cfg.allowed_user_id
    bot_name = bot_cfg.name if bot_cfg else "default"
    allowed_chats = list(bot_cfg.allowed_chat_ids) if bot_cfg else []

    user = update.effective_user
    if user is None:
        return False
    if allowed_id is None:
        msg = update.effective_message
        if msg is not None:
            await msg.reply_text(
                f"Bot [{esc(bot_name)}] is not locked to a user yet.\nYour Telegram id is {user.id}.\n"
                f"Add ALLOWED_TELEGRAM_USER_ID={user.id} to .env and restart me."
            )
        logger.warning("[%s] Unconfigured allowlist; message from id=%s @%s", bot_name, user.id, user.username)
        return False

    is_owner = user.id == allowed_id
    if require_owner and not is_owner:
        logger.warning(
            "[%s] Ignoring admin action from unauthorized id=%s", bot_name, user.id,
        )
        return False
    if is_owner:
        probe: HealthProbe | None = context.application.bot_data.get("health_probe")
        if probe is not None:
            probe.note_ok()
        return True

    chat = update.effective_chat
    if (
        not require_owner
        and chat is not None
        and chat.type in ("group", "supergroup")
        and chat.id in allowed_chats
    ):
        probe = context.application.bot_data.get("health_probe")
        if probe is not None:
            probe.note_ok()
        return True

    logger.warning("[%s] Ignoring message from unauthorized id=%s chat=%s", bot_name, user.id, getattr(chat, "id", None))
    return False


def _is_owner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    cfg = _cfg(context)
    bot_cfg = _bot_cfg(context)
    allowed_id = bot_cfg.allowed_user_id if bot_cfg else cfg.allowed_user_id
    user = update.effective_user
    return allowed_id is not None and user is not None and user.id == allowed_id


def esc(text: object) -> str:
    """HTML-escape any dynamic value before embedding it in a message."""
    return html.escape(str(text))


def _badge(status: str) -> str:
    if status == STATUS_RUNNING:
        return "\U0001F7E2"  # green
    if status == STATUS_ERROR:
        return "\U0001F534"  # red
    return "\U0001F7E1"  # yellow (idle)


def _chunks(text: str, size: int = TG_LIMIT) -> list[str]:
    if len(text) <= size:
        return [text]
    out: list[str] = []
    while text:
        out.append(text[:size])
        text = text[size:]
    return out


# --------------------------------------------------------------------------
# inline keyboard builders
# --------------------------------------------------------------------------

def _end_confirm_kb(sid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("确认关闭", callback_data=f"endyes:{sid}"),
        InlineKeyboardButton("保留会话", callback_data=f"endno:{sid}"),
    ]])


def _end_confirm_text(s: Session) -> str:
    return (
        f"确认关闭会话 <b>{esc(s.name)}</b>？\n"
        f"目录：<code>{esc(s.cwd)}</code>\n"
        f"此操作不可撤销。"
    )


def _status_kb(sid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("\U0001F9E0 选择模型", callback_data="menu:model"),
            InlineKeyboardButton("\u2699\uFE0F 切换模式", callback_data="menu:mode"),
        ],
        [
            InlineKeyboardButton("\U0001F4AA 思考等级", callback_data="menu:effort"),
            InlineKeyboardButton("\U0001F4CA 额度用量", callback_data="usage:refresh"),
        ],
        [
            InlineKeyboardButton("\U0001F5DC 压缩上下文", callback_data=f"compact:{sid}"),
            InlineKeyboardButton("\U0001F4CB 会话列表", callback_data="menu:sessions"),
        ],
    ])


def _mode_kb(current: str) -> InlineKeyboardMarkup:
    row = []
    for m in MODES:
        label = f"\u2713 {m}" if m == current else m
        row.append(InlineKeyboardButton(label, callback_data=f"mode:{m}"))
    return InlineKeyboardMarkup([row])


def _effort_kb(values: list[str], current: str | None) -> InlineKeyboardMarkup:
    row: list[InlineKeyboardButton] = []
    rows: list[list[InlineKeyboardButton]] = []
    for value in values:
        label = f"\u2713 {value}" if value == current else value
        row.append(InlineKeyboardButton(label, callback_data=f"effort:{value}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def _effort_picker_text(s: Session, options: list[str]) -> str:
    current = s.model_params.get("effort")
    if current:
        current_line = f"当前思考等级：<b>{esc(current)}</b>"
    else:
        current_line = "当前思考等级：<i>(模型默认)</i>"
    return (
        f"<code>{esc(s.model)}</code> 的思考等级设置\n"
        f"{current_line}\n\n点击选择等级："
    )


def _menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("\u2795 新建会话", callback_data="menu:new"),
            InlineKeyboardButton("\U0001F5C2 浏览目录", callback_data="menu:browse"),
        ],
        [
            InlineKeyboardButton("\U0001F4CB 会话列表", callback_data="menu:sessions"),
            InlineKeyboardButton("\U0001F4CA 额度用量", callback_data="usage:refresh"),
        ],
        [
            InlineKeyboardButton("\U0001F4C1 文件管理", callback_data="menu:files"),
        ],
        [
            InlineKeyboardButton("\u2139\uFE0F 当前状态", callback_data="menu:status"),
        ],
        [
            InlineKeyboardButton("\u267B\uFE0F 重启服务", callback_data="menu:restart"),
            InlineKeyboardButton("⚛️ 重新加载", callback_data="menu:reload"),
        ],
    ])


# Telegram allows ~20 edits/min per chat; stay under that (≈1 every 3s).
_TELEGRAM_EDIT_MIN_INTERVAL = 3.0
_TELEGRAM_RETRY_BUFFER = 1.5
_CHAT_ACTION_MIN_INTERVAL = 5.0
# Transient disconnects (RemoteProtocolError / ConnectError / TimedOut) are common
# on flaky paths to api.telegram.org — retry with backoff instead of failing the run.
_TELEGRAM_NETWORK_RETRIES = 3
_TELEGRAM_NETWORK_BASE_DELAY = 0.8
# Live mid-run edits: fail fast. update() never awaits network — a long retry
# cascade previously blocked the SDK event consumer and caused bridge disconnects.
_LIVE_TELEGRAM_NETWORK_RETRIES = 1


def _is_retryable_telegram_network(exc: BaseException) -> bool:
    """True for transient transport failures; BadRequest must not be retried."""
    if not isinstance(exc, NetworkError):
        return False
    # BadRequest is a NetworkError subclass in python-telegram-bot.
    if isinstance(exc, BadRequest):
        return False
    return True


async def _await_telegram(
    coro_factory,
    *,
    max_network_retries: int = _TELEGRAM_NETWORK_RETRIES,
) -> object | None:
    """Retry flood limits and transient Telegram network failures."""
    attempt = 0
    while True:
        try:
            return await coro_factory()
        except RetryAfter as exc:
            wait = max(1.0, float(exc.retry_after) + _TELEGRAM_RETRY_BUFFER)
            logger.info("Telegram flood limit, waiting %.0fs", wait)
            await asyncio.sleep(wait)
        except NetworkError as exc:
            if not _is_retryable_telegram_network(exc):
                raise
            if attempt >= max_network_retries:
                raise
            wait = min(20.0, _TELEGRAM_NETWORK_BASE_DELAY * (2 ** attempt))
            attempt += 1
            logger.warning(
                "Telegram network error (%s/%s): %s; retry in %.1fs",
                attempt,
                max_network_retries,
                exc,
                wait,
            )
            await asyncio.sleep(wait)


class LiveMessage:
    """Edits a single Telegram message in place, coalesced to avoid rate limits.

    ``update()`` never awaits Telegram I/O — it only schedules a background flush.
    That keeps the Cursor SDK event consumer from stalling on flaky Telegram paths.
    Call ``flush()`` after the run finishes to push any remaining pending text.
    """

    def __init__(self, bot, chat_id: int, message_id: int) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.message_id = message_id
        self._last = ""
        self._last_edit = 0.0
        self._pending: str | None = None
        self._flush_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        # True while edit_message_text is in flight — force must not cancel it.
        self._flushing = False

    async def update(self, text: str, force: bool = False) -> None:
        """Queue live text. Never blocks on Telegram (force only shortens delay)."""
        self._pending = text[:TG_LIMIT]
        now = time.time()
        elapsed = now - self._last_edit
        if force or elapsed >= _TELEGRAM_EDIT_MIN_INTERVAL:
            delay = 0.0
        else:
            delay = max(0.1, _TELEGRAM_EDIT_MIN_INTERVAL - elapsed)
        self._schedule_flush(delay)

    def _schedule_flush(self, delay: float) -> None:
        """Ensure a background flush runs; coalesce instead of canceling in-flight I/O."""
        if delay <= 0.0:
            if self._flushing:
                # Network edit already running — keep newest _pending; that flush
                # (or a follow-up) will push it when done.
                return
            if self._flush_task is not None and not self._flush_task.done():
                # Only cancel a *delayed* sleep, not an active Telegram edit.
                self._flush_task.cancel()
            self._flush_task = asyncio.create_task(self._flush_after(0.0))
            return
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush_after(delay))

    async def flush(self) -> None:
        """Push any pending text (call before replacing this message)."""
        if self._flush_task is not None and not self._flush_task.done():
            try:
                await self._flush_task
            except asyncio.CancelledError:
                # Swallow only when the *flush task* was cancelled (coalesce).
                # If *this* caller is being cancelled (/cancel), propagate.
                me = asyncio.current_task()
                if me is not None and me.cancelling():
                    raise
            except Exception:  # noqa: BLE001
                logger.debug("live flush task failed", exc_info=True)
        if self._pending and self._pending != self._last:
            await self._flush()

    async def _flush_after(self, delay: float) -> None:
        if delay > 0:
            await asyncio.sleep(delay)
        await self._flush()
        # If newer text arrived while we were editing, schedule another pass.
        if self._pending and self._pending != self._last and not self._flushing:
            self._schedule_flush(0.0)

    async def _flush(self) -> None:
        # Mark before any await so force cannot cancel us while we wait on the lock.
        self._flushing = True
        try:
            async with self._lock:
                out = self._pending
                if not out or out == self._last:
                    return
                try:
                    await _await_telegram(lambda: self.bot.edit_message_text(
                        out,
                        chat_id=self.chat_id,
                        message_id=self.message_id,
                        parse_mode=ParseMode.HTML,
                    ), max_network_retries=_LIVE_TELEGRAM_NETWORK_RETRIES)
                    self._last = out
                    self._last_edit = time.time()
                except BadRequest as exc:
                    err = str(exc).lower()
                    if "not modified" in err:
                        self._last = out
                        self._last_edit = time.time()
                    elif "can't parse" in err or "parse" in err:
                        plain = _plain_fallback(out)
                        try:
                            await _await_telegram(lambda: self.bot.edit_message_text(
                                plain,
                                chat_id=self.chat_id,
                                message_id=self.message_id,
                                parse_mode=None,
                            ), max_network_retries=_LIVE_TELEGRAM_NETWORK_RETRIES)
                            self._last = plain
                            self._last_edit = time.time()
                        except BadRequest as exc2:
                            logger.debug("edit_message_text plain fallback failed: %s", exc2)
                        except NetworkError as exc2:
                            logger.warning(
                                "Live message plain fallback failed after retries: %s",
                                exc2,
                            )
                    else:
                        logger.debug("edit_message_text failed: %s", exc)
                except NetworkError as exc:
                    # Keep _pending so a later flush / final send can retry.
                    # Never raise — heartbeat live edits must not abort the agent run.
                    logger.warning(
                        "Live message edit failed after retries (will retry later): %s",
                        exc,
                    )
        finally:
            self._flushing = False


def _plain_fallback(text: str) -> str:
    """Readable text when Telegram rejects HTML (avoids showing &quot; entities)."""
    return html_to_plain_preview(text)[:TG_LIMIT]


async def _edit_html_message(
    bot,
    chat_id: int,
    message_id: int,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> bool:
    """Edit a message in place. Returns False if the edit could not be applied."""
    try:
        await _await_telegram(lambda: bot.edit_message_text(
            text,
            chat_id=chat_id,
            message_id=message_id,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        ))
        return True
    except BadRequest as exc:
        err = str(exc).lower()
        if "not modified" in err:
            return True
        if "can't parse" in err or "parse" in err:
            plain = _plain_fallback(text)
            try:
                await _await_telegram(lambda: bot.edit_message_text(
                    plain,
                    chat_id=chat_id,
                    message_id=message_id,
                    parse_mode=None,
                    reply_markup=reply_markup,
                ))
                return True
            except (BadRequest, NetworkError):
                return False
        try:
            await _await_telegram(lambda: bot.edit_message_text(
                text,
                chat_id=chat_id,
                message_id=message_id,
                parse_mode=None,
                reply_markup=reply_markup,
            ))
            return True
        except (BadRequest, NetworkError):
            return False
    except NetworkError as exc:
        logger.warning("edit_message_text failed after retries: %s", exc)
        return False


async def _send_html_chunks(
    bot,
    chat_id: int,
    parts: list[str],
    *,
    message_id: int | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
    outbox: TelegramOutbox | None = None,
) -> None:
    if not parts:
        return
    start = 0
    if message_id is not None:
        edited = await _edit_html_message(
            bot, chat_id, message_id, parts[0],
            reply_markup=reply_markup if len(parts) == 1 else None,
        )
        if edited:
            start = 1
        else:
            # Edit path exhausted — deliver full reply as new messages.
            logger.warning(
                "Falling back to send_message after edit failure (chat_id=%s)",
                chat_id,
            )
            start = 0
    for i, part in enumerate(parts[start:], start=start):
        is_last = i == len(parts) - 1
        try:
            await _await_telegram(lambda p=part: bot.send_message(
                chat_id,
                p,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup if is_last else None,
            ))
        except BadRequest as exc:
            err = str(exc).lower()
            fallback = (
                _plain_fallback(part)
                if "can't parse" in err or "parse" in err
                else part
            )
            try:
                await _await_telegram(lambda fb=fallback: bot.send_message(
                    chat_id,
                    fb,
                    parse_mode=None,
                    reply_markup=reply_markup if is_last else None,
                ))
            except (BadRequest, NetworkError):
                pass
        except NetworkError as exc:
            logger.warning("send_message failed after retries: %s", exc)
            if outbox is not None and i < len(parts):
                for p in parts[i:]:
                    outbox.enqueue(
                        chat_id=chat_id,
                        text=p,
                        parse_mode=ParseMode.HTML,
                    )
                logger.info(
                    "Enqueued %d part(s) to outbox  chat_id=%s",
                    len(parts) - i,
                    chat_id,
                )
            return


async def _create_and_activate(update: Update, context: ContextTypes.DEFAULT_TYPE, path: str) -> None:
    mgr = _mgr(context)
    chat_id = update.effective_chat.id
    p = Path(path)
    if not p.is_dir():
        await context.bot.send_message(chat_id, f"Not a folder: <code>{esc(path)}</code>")
        return
    try:
        s = await mgr.create_session(str(p))
    except CursorAgentError as exc:
        await context.bot.send_message(chat_id, f"\u274C Failed to start session: {esc(exc)}")
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("create_session failed")
        await context.bot.send_message(chat_id, f"\u274C Failed to start session: {esc(exc)}")
        return
    mgr.set_active(chat_id, s.short_id)
    _log_action("Session created", id=s.short_id, cwd=str(p))
    await context.bot.send_message(
        chat_id,
        f"\U0001F7E2 Started session in <code>{esc(p)}</code>\n"
        "Fresh agent \u2014 empty conversation. Project files are still visible; "
        "use /context to restore prior chat on purpose.\n"
        "It's now active \u2014 send a message to prompt it.",
    )


async def _handle_new_folder_name(
    update: Update, context: ContextTypes.DEFAULT_TYPE, parent: str, name: str
) -> None:
    chat_id = update.effective_chat.id
    name = (name or "").strip()
    # Single, safe path component only.
    if not name or "/" in name or name in (".", "..") or name.startswith("."):
        context.user_data["pending_mkdir"] = parent  # re-arm so they can retry
        await update.message.reply_text(
            "Invalid name. Send a simple folder name like <code>my-project</code> "
            "(no slashes), or /new to cancel."
        )
        return
    target = Path(parent) / name
    try:
        target.mkdir(parents=False, exist_ok=True)
    except OSError as exc:
        await update.message.reply_text(f"\u274C Couldn't create folder: {esc(exc)}")
        return
    _log_action("Folder created", path=str(target))
    await update.message.reply_text(
        f"\U0001F4C1 Created <code>{esc(target)}</code>\nStarting session\u2026"
    )
    await _create_and_activate(update, context, str(target))


# --------------------------------------------------------------------------
# command handlers
# --------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    _log_action("Command /start", user=update.effective_user.id)
    await update.message.reply_text(HELP, reply_markup=_menu_kb())


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    context.user_data.pop("pending_mkdir", None)
    if context.args:
        path = Path(" ".join(context.args)).expanduser()
        await _create_and_activate(update, context, str(path))
        return
    kb = await asyncio.to_thread(projects_keyboard, _cfg(context), _tokens(context))
    await update.message.reply_text("Pick a folder for the new session:", reply_markup=kb)


async def cmd_browse(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    cfg = _cfg(context)
    start = Path(" ".join(context.args)).expanduser() if context.args else cfg.projects_root
    if not start.is_dir():
        start = Path.home()
    kb = await asyncio.to_thread(
        browser_keyboard, str(start), _tokens(context), cfg.browser_page_size,
    )
    await update.message.reply_text(f"\U0001F4C2 <code>{esc(start)}</code>", reply_markup=kb)


async def cmd_cd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    if not context.args:
        await update.message.reply_text("Usage: /cd &lt;absolute path&gt;")
        return
    path = Path(" ".join(context.args)).expanduser()
    await _create_and_activate(update, context, str(path))


def _last_prompt_summary(prompt: str, *, max_len: int = 120) -> str:
    """One-line preview of the last user message sent to a session."""
    text = " ".join(
        strip_telegram_delivery_prefix(strip_rules_prefix(prompt or "")).split()
    )
    if not text:
        return "(none)"
    if len(text) > max_len:
        return text[: max_len - 1] + "\u2026"
    return text


def _sessions_view(mgr: SessionManager, chat_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    if not mgr.sessions:
        return None
    active = mgr.active.get(chat_id)
    lines: list[str] = []
    rows: list[list[InlineKeyboardButton]] = []
    for s in mgr.sessions.values():
        star = " ⭐" if s.short_id == active else ""
        ctx = format_context_line(mgr.session_context(s))
        lines.append(
            f"{_badge(s.status)} <b>{s.short_id}</b>{star} · {esc(s.name)} ({esc(s.status)})\n"
            f"   <code>{esc(s.cwd)}</code>\n"
            f"   {esc(format_model_display(s.model, s.model_params))} · {esc(s.mode)}\n"
            f"   {esc(ctx)}\n"
            f"   最近：<i>{esc(_last_prompt_summary(s.last_prompt))}</i>"
        )
        rows.append([
            InlineKeyboardButton(f"使用 {s.short_id}", callback_data=f"sess:{s.short_id}"),
            InlineKeyboardButton("✖ 取消", callback_data=f"cancel:{s.short_id}"),
            InlineKeyboardButton("🗑️ 关闭", callback_data=f"end:{s.short_id}"),
        ])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def cmd_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    mgr = _mgr(context)
    _log_sessions_view(mgr, via="command:/sessions", **_telegram_actor(update))
    view = _sessions_view(mgr, update.effective_chat.id)
    if view is None:
        await update.message.reply_text("暂无会话。请使用 /new 开启新会话。", reply_markup=_menu_kb())
        return
    text, kb = view
    await update.message.reply_text(text, reply_markup=kb)


async def cmd_use(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    mgr = _mgr(context)
    if not context.args:
        await update.message.reply_text("用法：/use <会话ID>")
        return
    sid = context.args[0]
    if sid not in mgr.sessions:
        await update.message.reply_text(f"找不到会话 <code>{esc(sid)}</code>。可用 /sessions 查看列表。")
        return
    mgr.set_active(update.effective_chat.id, sid)
    await update.message.reply_text(
        f"已切换到会话 <code>[{esc(sid)}]</code>。\n"
        "已恢复该会话自己的对话历史。"
    )


def _status_text(s: Session, mgr: SessionManager | None = None) -> str:
    ctx = format_context_line(mgr.session_context(s)) if mgr else "上下文：(未知)"
    return (
        f"{_badge(s.status)} <b>{esc(s.name)}</b>\n"
        f"目录：<code>{esc(s.cwd)}</code>\n"
        f"模型：<code>{esc(format_model_display(s.model, s.model_params))}</code>\n"
        f"模式：<code>{esc(s.mode)}</code>\n"
        f"状态：{esc(s.status)}\n"
        f"{esc(ctx)}"
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    _log_action("Command /status", user=update.effective_user.id)
    mgr = _mgr(context)
    s = mgr.get_active(update.effective_chat.id)
    if s is None:
        await update.message.reply_text("暂无激活的会话。请使用 /new 开启。", reply_markup=_menu_kb())
        return
    await update.message.reply_text(_status_text(s, mgr), reply_markup=_status_kb(s.short_id))


def _model_callback(m: str) -> str:
    """Embed model id directly — avoids stale token lookups after restart."""
    data = f"model:{m}"
    return data[:64]


def _models_keyboard(models: list[str], current: str) -> InlineKeyboardMarkup:
    row: list[InlineKeyboardButton] = []
    rows: list[list[InlineKeyboardButton]] = []
    for m in models:
        label = model_picker_label(m, current=(m == current))
        row.append(InlineKeyboardButton(label, callback_data=_model_callback(m)))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


async def _send_effort_picker(context: ContextTypes.DEFAULT_TYPE, chat_id: int, s: Session) -> None:
    mgr = _mgr(context)
    try:
        options = await mgr.effort_options(s)
    except Exception as exc:  # noqa: BLE001
        logger.exception("effort_options failed")
        await context.bot.send_message(
            chat_id,
            f"获取思考等级失败：{esc(exc)}",
        )
        return
    if not options:
        await context.bot.send_message(
            chat_id,
            f"模型 <code>{esc(s.model)}</code> "
            "不支持思考等级设置。\n"
            "可通过 /model 切换为支持的 Claude 模型 (例如 <code>claude-opus-4-6</code>)。",
        )
        return
    kb = _effort_kb(options, s.model_params.get("effort"))
    await context.bot.send_message(
        chat_id,
        _effort_picker_text(s, options),
        reply_markup=kb,
    )


async def _send_model_picker(context: ContextTypes.DEFAULT_TYPE, chat_id: int, s: Session) -> None:
    mgr = _mgr(context)
    try:
        available = await mgr.list_models()
    except Exception as exc:  # noqa: BLE001
        logger.exception("list_models failed")
        await context.bot.send_message(
            chat_id,
            f"Current model: <code>{esc(s.model)}</code>\n"
            f"(Could not fetch model list: {esc(exc)})",
        )
        return

    if not available:
        await context.bot.send_message(
            chat_id,
            f"Current model: <code>{esc(s.model)}</code>\n(No models returned by the API.)",
        )
        return

    kb = _models_keyboard(available, s.model)
    await context.bot.send_message(
        chat_id,
        f"Current model: <code>{esc(s.model)}</code>\n\nPick a model:",
        reply_markup=kb,
    )


def _track_bg_task(context: ContextTypes.DEFAULT_TYPE, task: asyncio.Task) -> None:
    """Keep a strong ref so background prompt tasks are not GC'd mid-run."""
    bag: set[asyncio.Task] = context.application.bot_data.setdefault("_bg_tasks", set())
    bag.add(task)

    def _done(t: asyncio.Task) -> None:
        bag.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            logger.error("Background task failed: %s", exc, exc_info=exc)

    task.add_done_callback(_done)


def _busy_policy(context: ContextTypes.DEFAULT_TYPE) -> str:
    override = context.application.bot_data.get("busy_policy")
    if override in BUSY_POLICIES:
        return str(override)
    cfg: Config | None = context.application.bot_data.get("cfg")
    if cfg is not None and cfg.busy_policy in BUSY_POLICIES:
        return cfg.busy_policy
    return "queue"


def _busy_policy_path(cfg: Config, bot_cfg: BotConfig | None) -> Path:
    """Per-bot busy policy file (legacy shared path used only as fallback on load)."""
    return bot_state_dir(cfg, bot_name_for(bot_cfg)) / BUSY_POLICY_FILE


def _set_busy_policy(context: ContextTypes.DEFAULT_TYPE, policy: str) -> None:
    context.application.bot_data["busy_policy"] = policy
    cfg: Config | None = context.application.bot_data.get("cfg")
    if cfg is not None:
        try:
            path = _busy_policy_path(cfg, context.application.bot_data.get("bot_cfg"))
            path.write_text(policy + "\n", encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not persist busy_policy: %s", exc)


def _load_busy_policy(app: Application, cfg: Config) -> None:
    bot_cfg: BotConfig | None = app.bot_data.get("bot_cfg")
    candidates = [_busy_policy_path(cfg, bot_cfg), cfg.state_dir / BUSY_POLICY_FILE]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            raw = path.read_text(encoding="utf-8").strip().lower()
        except OSError:
            continue
        if raw in BUSY_POLICIES:
            app.bot_data["busy_policy"] = raw
            return
    app.bot_data["busy_policy"] = cfg.busy_policy if cfg.busy_policy in BUSY_POLICIES else "queue"


def _queue_prompt_preview(prompt: str | UserMessage, limit: int = 80) -> str:
    text = prompt.text if isinstance(prompt, UserMessage) else str(prompt)
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "\u2026"


def _queue_ack_kb(sid: str, token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📋 排队",
                    callback_data=f"qkeep:{sid}:{token}",
                ),
                InlineKeyboardButton(
                    "⚡ 发送",
                    callback_data=f"qsend:{sid}:{token}",
                ),
                InlineKeyboardButton(
                    "❎ 取消",
                    callback_data=f"qdrop:{sid}:{token}",
                ),
            ]
        ]
    )


async def _wait_until_idle(mgr: SessionManager, s: Session, *, ticks: int = 50) -> None:
    for _ in range(ticks):
        if not mgr.is_busy(s):
            return
        await asyncio.sleep(0.1)
    if mgr.is_busy(s):
        mgr._force_unstick(s)


async def _interrupt_and_run(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    s: Session,
    prompt: str | UserMessage,
    *,
    thinking_label: str,
    log_kind: str,
    notice_html: str | None = None,
) -> None:
    """Cancel the current run (and other queued items), then start ``prompt``."""
    mgr = _mgr(context)
    if notice_html:
        await context.bot.send_message(chat_id, notice_html, parse_mode=ParseMode.HTML)
    await mgr.cancel(s)
    await _wait_until_idle(mgr, s)
    if not _schedule_session_work(
        context,
        s,
        _execute_prompt(
            context, chat_id, s, prompt,
            thinking_label=thinking_label, log_kind=log_kind,
        ),
        name="prompt",
    ):
        await context.bot.send_message(
            chat_id,
            "无法启动新任务，请再试一次或发送 /cancel。",
        )


async def _drain_prompt_queue(context: ContextTypes.DEFAULT_TYPE, s: Session) -> None:
    """Start the next queued prompt if the session is idle."""
    mgr = _mgr(context)
    if mgr.is_busy(s):
        return
    # Prefer the first confirmed item; unconfirmed heads (待确认) must not block it.
    item = mgr.pop_first_confirmed_prompt(s.short_id)
    if item is None:
        return
    remaining = mgr.queued_count(s.short_id)
    try:
        await context.bot.send_message(
            item.chat_id,
            f"📋 开始执行排队任务"
            + (f"（剩余 {remaining}）" if remaining else "")
            + "\u2026",
        )
    except Exception:  # noqa: BLE001
        logger.debug("queue drain notice failed", exc_info=True)
    if not _schedule_session_work(
        context,
        s,
        _execute_prompt(
            context,
            item.chat_id,
            s,
            item.prompt,
            thinking_label=item.thinking_label,
            log_kind=item.log_kind,
        ),
        name="prompt",
    ):
        mgr.push_front_prompt(s.short_id, item)
        logger.warning("Could not start queued prompt for %s; re-queued", s.short_id)


async def _start_user_prompt(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    s: Session,
    prompt: str | UserMessage,
    *,
    thinking_label: str = "\U0001F4AD thinking\u2026",
    log_kind: str = "Prompt",
) -> None:
    """Start a prompt, or apply busy_policy (queue / interrupt) when one is running."""
    mgr = _mgr(context)
    if not mgr.is_busy(s):
        if _schedule_session_work(
            context,
            s,
            _execute_prompt(
                context, chat_id, s, prompt,
                thinking_label=thinking_label, log_kind=log_kind,
            ),
            name="prompt",
        ):
            return

    policy = _busy_policy(context)
    if policy == "queue":
        item = QueuedPrompt(
            prompt=prompt,
            chat_id=chat_id,
            thinking_label=thinking_label,
            log_kind=log_kind,
            confirmed=False,
        )
        try:
            n = mgr.enqueue_prompt(s.short_id, item)
        except ValueError as exc:
            await context.bot.send_message(chat_id, f"⏳ {esc(exc)}")
            return
        preview = esc(_queue_prompt_preview(prompt))
        await context.bot.send_message(
            chat_id,
            f"📋 <b>新消息待确认</b>（队列第 {n} 条）\n"
            f"<i>{preview}</i>\n\n"
            "📋 <b>排队</b> — 确认后等当前任务结束再执行\n"
            "⚡ <b>发送</b> — 中止当前任务，立刻跑这条\n"
            "❎ <b>取消</b> — 丢掉这条新命令（不影响正在跑的任务）",
            parse_mode=ParseMode.HTML,
            reply_markup=_queue_ack_kb(s.short_id, item.token),
        )
        return

    # interrupt: stop current run, drop queued follow-ups, start the new prompt.
    cleared = mgr.clear_prompt_queue(s.short_id)
    notice = "⚡ <b>中止当前任务，改跑新指令</b>"
    if cleared:
        notice += f"\n（已清空排队 {cleared} 条）"
    await _interrupt_and_run(
        context, chat_id, s, prompt,
        thinking_label=thinking_label, log_kind=log_kind, notice_html=notice,
    )


def _schedule_session_work(
    context: ContextTypes.DEFAULT_TYPE,
    s: Session,
    coro,
    *,
    name: str,
) -> bool:
    """Run session work off the Telegram update handler so /cancel stays responsive.

    Returns False if the session is already busy (caller should notify the user).
    Pre-registers the task in SessionManager so ``is_busy`` is true immediately.
    """
    mgr = _mgr(context)
    if mgr.is_busy(s):
        return False

    async def runner() -> None:
        try:
            await coro
        except asyncio.CancelledError:
            logger.info("%s cancelled  session=%s", name, s.short_id)
            raise
        except Exception:  # noqa: BLE001
            logger.exception("%s crashed  session=%s", name, s.short_id)
        finally:
            if mgr._run_tasks.get(s.short_id) is asyncio.current_task():
                mgr._run_tasks.pop(s.short_id, None)
                if s.status == STATUS_RUNNING and s.run is None:
                    s.status = STATUS_IDLE
                    mgr._persist()
            try:
                await _drain_prompt_queue(context, s)
            except Exception:  # noqa: BLE001
                logger.exception("prompt queue drain failed  session=%s", s.short_id)

    task = asyncio.get_running_loop().create_task(
        runner(),
        name=f"{name}-{s.short_id}",
    )
    mgr._run_tasks[s.short_id] = task
    _track_bg_task(context, task)
    return True


async def _run_compact(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    s: Session,
    *,
    placeholder_message_id: int | None = None,
) -> None:
    mgr = _mgr(context)
    if mgr.is_busy(s) and mgr._run_tasks.get(s.short_id) is not asyncio.current_task():
        await context.bot.send_message(
            chat_id,
            f"Already running \u2014 /cancel first.",
        )
        return

    if placeholder_message_id is None:
        placeholder_text = build_live_html(
            s.short_id, s.name, s.model, [], "\U0001F5DC compacting context\u2026", "",
        )
        placeholder = await context.bot.send_message(
            chat_id,
            placeholder_text,
            parse_mode=ParseMode.HTML,
        )
        placeholder_message_id = placeholder.message_id

    live = LiveMessage(context.bot, chat_id, placeholder_message_id)

    async def on_update(text: str, *, force: bool = False) -> None:
        await live.update(text, force=force)

    try:
        rstatus, final, _attachments = await mgr.run_prompt(s, "/compact", on_update)
    except asyncio.CancelledError:
        try:
            await live.update("\u270B cancelled", force=True)
            await live.flush()
        except Exception:  # noqa: BLE001
            pass
        raise
    except SessionBusyError as exc:
        await live.update(f"\u23f3 {esc(exc)}", force=True)
        await live.flush()
        return
    except CursorAgentError as exc:
        await live.update(f"\u274C compact failed: {esc(exc)}", force=True)
        await live.flush()
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("compact error")
        await live.update(f"\u274C error: {esc(exc)}", force=True)
        await live.flush()
        return

    status_icon = "\u2705 compacted" if rstatus == "finished" else (
        "\u270B cancelled" if rstatus == "cancelled" else f"\U0001F534 {esc(rstatus)}"
    )
    ctx = format_context_line(mgr.session_context(s))
    body = (final or "(done)").strip()
    final_html = build_final_html(
        s.short_id, s.name, s.model, status_icon, body, mode=s.mode,
    )
    final_html += f"\n<code>{esc(ctx)}</code>"
    parts = chunk_telegram_html(final_html)
    await live.flush()
    await _send_html_chunks(
        context.bot, chat_id, parts,
        message_id=placeholder_message_id,
        outbox=context.application.bot_data.get("outbox"),
    )


def _format_prior_agents_list(s: Session, mgr: SessionManager) -> str:
    priors = mgr.prior_agents_for_session(s)
    if not priors:
        tracked = [aid for aid in s.prior_agent_ids if aid and aid != s.agent_id]
        if tracked:
            return (
                f"Prior agent id(s) exist, but no "
                "transcript files were found for this project folder.\n"
                "<i>Restate your task — /context cannot restore without a transcript.</i>"
            )
        return (
            f"No prior agents tracked for this session.\n"
            "<i>Context restore only works after an agent reset in this session.</i>"
        )
    lines = [
        f"<b>Prior agents for this session only</b>",
        f"<i>Project: <code>{esc(s.cwd)}</code></i>",
        "",
    ]
    for info in priors:
        short = info.agent_id.removeprefix("agent-")[:8]
        lines.append(
            f"\u2022 <code>{esc(info.agent_id)}</code> "
            f"({info.user_turns} turns, ~{fmt_tokens(info.tokens)} tokens)"
        )
        lines.append(f"  <code>/context {esc(short)}</code>")
    if s.context_restored_from:
        lines.append("")
        lines.append(
            f"<i>Last restored from: <code>{esc(s.context_restored_from)}</code></i>"
        )
    return "\n".join(lines)


async def _run_context(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    s: Session,
    *,
    agent_id: str | None = None,
    force: bool = False,
    placeholder_message_id: int | None = None,
) -> None:
    mgr = _mgr(context)
    if mgr.is_busy(s) and mgr._run_tasks.get(s.short_id) is not asyncio.current_task():
        await context.bot.send_message(
            chat_id,
            f"Already running \u2014 /cancel first.",
        )
        return

    try:
        prompt, summary, restored_from = mgr.prepare_context_restore(
            s, agent_id=agent_id, force=force,
        )
    except ValueError as exc:
        await context.bot.send_message(chat_id, f"\u274C {esc(exc)}")
        return

    if placeholder_message_id is None:
        placeholder_text = build_live_html(
            s.short_id, s.name, s.model, [], "\U0001F4DC restoring context\u2026", summary,
        )
        placeholder = await context.bot.send_message(
            chat_id,
            placeholder_text,
            parse_mode=ParseMode.HTML,
        )
        placeholder_message_id = placeholder.message_id

    live = LiveMessage(context.bot, chat_id, placeholder_message_id)

    async def on_update(text: str, *, force: bool = False) -> None:
        await live.update(text, force=force)

    try:
        rstatus, final, _attachments = await mgr.run_prompt(s, prompt, on_update)
    except asyncio.CancelledError:
        try:
            await live.update("\u270B cancelled", force=True)
            await live.flush()
        except Exception:  # noqa: BLE001
            pass
        raise
    except SessionBusyError as exc:
        await live.update(f"\u23f3 {esc(exc)}", force=True)
        await live.flush()
        return
    except CursorAgentError as exc:
        await live.update(f"\u274C context restore failed: {esc(exc)}", force=True)
        await live.flush()
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("context restore error")
        await live.update(f"\u274C error: {esc(exc)}", force=True)
        await live.flush()
        return

    status_icon = "\u2705 context restored" if rstatus == "finished" else (
        "\u270B cancelled" if rstatus == "cancelled" else f"\U0001F534 {esc(rstatus)}"
    )
    if rstatus == "finished":
        s.context_restored_from = restored_from
        mgr._persist()
    ctx = format_context_line(mgr.session_context(s))
    body = (final or "(done)").strip()
    final_html = build_final_html(
        s.short_id, s.name, s.model, status_icon, body, mode=s.mode,
    )
    final_html += f"\n<code>{esc(ctx)}</code>"
    parts = chunk_telegram_html(final_html)
    await live.flush()
    await _send_html_chunks(
        context.bot, chat_id, parts,
        message_id=placeholder_message_id,
        outbox=context.application.bot_data.get("outbox"),
    )


async def cmd_compact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    _log_action("Command /compact", user=update.effective_user.id)
    mgr = _mgr(context)
    s = mgr.get_active(update.effective_chat.id)
    if s is None:
        await update.message.reply_text("No active session. Use /new.", reply_markup=_menu_kb())
        return
    if not _schedule_session_work(
        context,
        s,
        _run_compact(context, update.effective_chat.id, s),
        name="compact",
    ):
        await update.message.reply_text(
            f"Already running \u2014 /cancel first.",
        )


async def cmd_context(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    _log_action("Command /context", user=update.effective_user.id)
    mgr = _mgr(context)
    s = mgr.get_active(update.effective_chat.id)
    if s is None:
        await update.message.reply_text("No active session. Use /new.", reply_markup=_menu_kb())
        return

    args = [a.strip() for a in (context.args or []) if a.strip()]
    if not args:
        if not _schedule_session_work(
            context,
            s,
            _run_context(context, update.effective_chat.id, s),
            name="context",
        ):
            await update.message.reply_text(
                f"Already running \u2014 /cancel first.",
            )
        return

    head = args[0].lower()
    if head == "list":
        await update.message.reply_text(
            _format_prior_agents_list(s, mgr),
            parse_mode=ParseMode.HTML,
        )
        return

    force = head == "refresh"
    agent_id: str | None = None
    if not force:
        token = args[0]
        if token.startswith("agent-"):
            agent_id = token
        else:
            matches = [
                aid for aid in s.prior_agent_ids
                if aid.removeprefix("agent-").startswith(token)
            ]
            if len(matches) == 1:
                agent_id = matches[0]
            elif len(matches) > 1:
                await update.message.reply_text(
                    f"Ambiguous agent id `{esc(token)}` for this session. "
                    "Use `/context list` or the full agent id.",
                    parse_mode=ParseMode.HTML,
                )
                return
            else:
                await update.message.reply_text(
                    f"Agent `{esc(token)}` is not in this session's prior history.",
                    parse_mode=ParseMode.HTML,
                )
                return

    if not _schedule_session_work(
        context,
        s,
        _run_context(
            context,
            update.effective_chat.id,
            s,
            agent_id=agent_id,
            force=force,
        ),
        name="context",
    ):
        await update.message.reply_text(
            f"Already running \u2014 /cancel first.",
        )


async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    mgr = _mgr(context)
    s = mgr.get_active(update.effective_chat.id)
    if s is None:
        await update.message.reply_text("No active session. Use /new.", reply_markup=_menu_kb())
        return

    if context.args:
        model = " ".join(context.args).strip()
        await mgr.set_model(s, model)
        await update.message.reply_text(
            model_set_notice(model),
            parse_mode=ParseMode.HTML,
        )
        return

    await _send_model_picker(context, update.effective_chat.id, s)


async def cmd_effort(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    _log_action("Command /effort", user=update.effective_user.id)
    mgr = _mgr(context)
    s = mgr.get_active(update.effective_chat.id)
    if s is None:
        await update.message.reply_text("No active session. Use /new.", reply_markup=_menu_kb())
        return

    if context.args:
        value = " ".join(context.args).strip().lower()
        normalized = await mgr.set_effort(s, value)
        if normalized is None:
            options = await mgr.effort_options(s)
            if not options:
                await update.message.reply_text(
                    f"Model <code>{esc(s.model)}</code> does not support effort.",
                )
            else:
                allowed = ", ".join(options)
                await update.message.reply_text(
                    f"Unknown effort {esc(value)!r} for <code>{esc(s.model)}</code>.\n"
                    f"Use one of: {esc(allowed)} — or send /effort for buttons.",
                )
            return
        await update.message.reply_text(
            f"Effort set to <b>{esc(normalized)}</b> for <code>{esc(s.model)}</code>.",
            reply_markup=_effort_kb(await mgr.effort_options(s) or [], normalized),
        )
        return

    await _send_effort_picker(context, update.effective_chat.id, s)


async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    mgr = _mgr(context)
    s = mgr.get_active(update.effective_chat.id)
    if s is None:
        await update.message.reply_text("No active session. Use /new.", reply_markup=_menu_kb())
        return

    if not context.args:
        await update.message.reply_text(
            f"Current mode: <b>{esc(s.mode)}</b>\n\nTap to switch:",
            reply_markup=_mode_kb(s.mode),
        )
        return

    mode = context.args[0].lower()
    normalized = mgr.set_mode(s, mode)
    if normalized is None:
        options = ", ".join(MODES)
        await update.message.reply_text(f"Unknown mode {esc(mode)!r}. Use one of: {esc(options)}")
        return
    await update.message.reply_text(
        f"Mode set to <b>{esc(normalized)}</b>.",
        reply_markup=_mode_kb(normalized),
    )


async def cmd_rename(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    _log_action("Command /rename", user=update.effective_user.id)
    mgr = _mgr(context)
    s = mgr.get_active(update.effective_chat.id)
    if s is None:
        await update.message.reply_text("暂无激活会话。请使用 /new 开启新会话。", reply_markup=_menu_kb())
        return

    if not context.args:
        current_name = esc(s.name)
        await update.message.reply_text(
            f"当前名称：<b>{current_name}</b>\n\n"
            "<b>用法</b>：<code>/rename &lt;新名称&gt;</code>\n"
            "<b>重置默认</b>：<code>/rename reset</code>\n\n"
            "<b>示例</b>：<code>/rename 淘宝平铺图精修</code>"
        )
        return

    new_name = " ".join(context.args).strip()
    if new_name.lower() in ("reset", "clear", "default", "重置"):
        display_name = mgr.rename_session(s, None)
        await update.message.reply_text(
            f"✅ 会话已重置为默认名称：<b>{esc(display_name)}</b>"
        )
    else:
        display_name = mgr.rename_session(s, new_name)
        await update.message.reply_text(
            f"✏️ 会话已重命名为：<b>{esc(display_name)}</b>"
        )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    mgr = _mgr(context)
    s = mgr.get_active(update.effective_chat.id)
    if s is None:
        await update.message.reply_text("No active session.")
        return
    queued = mgr.queued_count(s.short_id)
    ok = await mgr.cancel(s)
    # Successful cancel: the live thinking message becomes "✋ cancelled".
    # Do not leave a separate "Cancelling…" bubble that never updates.
    if ok:
        if queued:
            await update.message.reply_text(
                f"✅ 已取消。\n"
                f"📋 {queued} queued prompt(s) will run next "
                f"(or /busy interrupt + new msg to discard)."
            )
        return
    if mgr.is_busy(s):
        text = "Clearing stuck run\u2026"
    else:
        text = "Nothing is running."
    if queued:
        text += (
            f"\n📋 {queued} queued prompt(s) will run next "
            f"(or /busy interrupt + new msg to discard)."
        )
    await update.message.reply_text(text)


async def cmd_busy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    current = _busy_policy(context)
    args = [a.strip().lower() for a in (context.args or []) if a.strip()]
    if not args:
        await update.message.reply_text(
            f"忙碌策略：<b>{esc(current)}</b>\n\n"
            "<code>queue</code> — 新消息待确认：排队 / 立刻发送 / 取消新命令（默认）\n"
            "<code>interrupt</code> — 每条新消息都中止当前任务并立刻执行\n\n"
            "切换：<code>/busy queue</code> 或 <code>/busy interrupt</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    policy = args[0]
    if policy not in BUSY_POLICIES:
        await update.message.reply_text(
            "用法：<code>/busy queue</code> 或 <code>/busy interrupt</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    _set_busy_policy(context, policy)
    _log_action("Busy policy set", policy=policy, user=update.effective_user.id)
    await update.message.reply_text(
        f"忙碌策略已设为 <b>{esc(policy)}</b>。",
        parse_mode=ParseMode.HTML,
    )


async def cmd_end(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    mgr = _mgr(context)
    sid = context.args[0] if context.args else (mgr.active.get(update.effective_chat.id) or "")
    if not sid:
        await update.message.reply_text("Usage: /end &lt;session id&gt;")
        return
    s = mgr.sessions.get(sid)
    if s is None:
        await update.message.reply_text(f"<code>[{esc(sid)}]</code> not found.")
        return
    actor = _telegram_actor(update)
    _stash_end_request(context, sid, via="command:/end", **actor)
    mgr.log_session_event(sid, "end_requested", via="command:/end", **actor)
    await update.message.reply_text(_end_confirm_text(s), reply_markup=_end_confirm_kb(sid))


async def _send_usage(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    try:
        summary = await fetch_usage()
    except UsageError as exc:
        await context.bot.send_message(chat_id, f"\u274C {esc(exc)}")
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("fetch_usage failed")
        await context.bot.send_message(chat_id, f"\u274C Could not fetch usage: {esc(exc)}")
        return
    mgr = _mgr(context)
    s = mgr.get_active(chat_id)
    text = format_usage_html(summary)
    if s is not None:
        ctx = mgr.session_context(s)
        text += "\n\n" + format_context_html(ctx, session_id=s.short_id, esc=esc)
    await context.bot.send_message(
        chat_id,
        text,
    )


async def cmd_usage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    _log_action("Command /usage", user=update.effective_user.id)
    await _send_usage(context, update.effective_chat.id)


async def cmd_files(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    mgr = _mgr(context)
    chat_id = update.effective_chat.id
    s = mgr.get_active(chat_id)
    if s is None:
        await update.message.reply_text("No active session. Use /new.")
        return
    cfg = _cfg(context)
    tokens = _tokens(context)
    args = context.args or []
    if args and args[0].lower() == "find":
        if len(args) < 2:
            await update.message.reply_text(
                "Usage: <code>/files find &lt;name&gt;</code>\n"
                "Example: <code>/files find invoice</code>",
            )
            return
        query = " ".join(args[1:])
        files = await asyncio.to_thread(
            search_session_files, s.cwd, query, limit=cfg.browser_page_size,
        )
        header = (
            f"Files matching "
            f"<code>{esc(query)}</code> in <code>{esc(s.name)}</code>. Tap to send."
        )
    else:
        files = await asyncio.to_thread(
            list_session_files, s.cwd, limit=cfg.browser_page_size,
        )
        header = (
            f"Files in <code>{esc(s.name)}</code> "
            f"(newest first). Tap to send."
        )
    kb = files_keyboard(files, s.cwd, tokens, cfg.browser_page_size)
    await update.message.reply_text(header, reply_markup=kb)


async def _send_one_attachment(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    sid: str,
    path: Path,
    *,
    live: bool = False,
) -> bool:
    """Send a single file. Returns True if delivered (or skipped as too-large with user notified)."""
    try:
        size = path.stat().st_size
    except OSError:
        return False
    limit = max_bytes_for(path)
    if size > limit:
        await context.bot.send_message(
            chat_id,
            f"[{sid}] {path.name} is too large for Telegram "
            f"({size // (1024 * 1024)}MB). Path: {path}",
            parse_mode=None,
        )
        return True
    caption = f"[{sid}] {path.name}"
    kind = classify_attachment(path)
    try:
        if kind == "photo":
            await context.bot.send_photo(chat_id, photo=str(path), caption=caption)
        elif kind == "animation":
            await context.bot.send_animation(chat_id, animation=str(path), caption=caption)
        else:
            await context.bot.send_document(chat_id, document=str(path), caption=caption)
        _log_action("Attachment sent", session=sid, file=path.name, kind=kind, live=live)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to send attachment %s: %s", path, exc)
        await context.bot.send_message(
            chat_id,
            f"[{sid}] Could not send {path.name}: {exc}",
            parse_mode=None,
        )
        return False


async def _send_attachments(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    sid: str,
    paths: list[Path],
) -> None:
    """Send images and other files produced during an agent run."""
    for path in paths:
        await _send_one_attachment(context, chat_id, sid, path, live=False)


async def _execute_prompt(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    s: Session,
    prompt: str | UserMessage,
    *,
    thinking_label: str = "\U0001F4AD thinking\u2026",
    log_kind: str = "Prompt",
) -> None:
    mgr = _mgr(context)
    prompt_len = len(prompt.text) if isinstance(prompt, UserMessage) else len(prompt)
    _log_action(log_kind, session=s.short_id, chars=prompt_len, mode=s.mode, model=s.model)
    placeholder_text = build_live_html(
        s.short_id, s.name, s.model, [], thinking_label, "",
    )
    placeholder = await context.bot.send_message(
        chat_id,
        placeholder_text,
        parse_mode=ParseMode.HTML,
    )
    live = LiveMessage(context.bot, chat_id, placeholder.message_id)
    last_chat_action = 0.0

    try:
        await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
        last_chat_action = time.time()
    except Exception:  # noqa: BLE001
        pass

    async def on_update(text: str, *, force: bool = False) -> None:
        nonlocal last_chat_action
        try:
            await live.update(text, force=force)
        except Exception:  # noqa: BLE001 — never abort the agent run on live UI failure
            logger.warning("live update failed", exc_info=True)
        if force:
            now = time.time()
            if now - last_chat_action >= _CHAT_ACTION_MIN_INTERVAL:
                last_chat_action = now

                async def _typing() -> None:
                    try:
                        await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
                    except Exception:  # noqa: BLE001
                        pass

                asyncio.create_task(_typing())

    async def on_attachment(path: Path) -> bool:
        return await _send_one_attachment(
            context, chat_id, s.short_id, path, live=True,
        )

    try:
        rstatus, final, attachments = await mgr.run_prompt(
            s,
            wrap_with_rules(wrap_telegram_prompt(prompt), mgr.cfg.rules_text),
            on_update,
            on_attachment=on_attachment,
        )
    except asyncio.CancelledError:
        try:
            await live.update("\u270B cancelled", force=True)
            await live.flush()
        except Exception:  # noqa: BLE001
            pass
        raise
    except SessionBusyError as exc:
        await live.update(f"\u23f3 {esc(exc)}", force=True)
        await live.flush()
        return
    except CursorAgentError as exc:
        await live.update(f"\u274C failed to run: {esc(exc)}", force=True)
        await live.flush()
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("run_prompt error")
        await live.update(f"\u274C error: {esc(exc)}", force=True)
        await live.flush()
        return

    body = (final or "(no output)").strip()
    status_icon = (
        "\u2705" if rstatus == "finished"
        else "\u26a0\ufe0f model failed" if rstatus == STATUS_GLITCH
        else "\u270B cancelled" if rstatus == "cancelled"
        else f"\U0001F534 {esc(rstatus)}"
    )
    _log_action("Prompt done", session=s.short_id, status=rstatus, attachments=len(attachments))
    final_html = build_final_html(
        s.short_id, s.name, s.model, status_icon, body, mode=s.mode,
    )
    parts = chunk_telegram_html(final_html)
    await live.flush()
    await _send_html_chunks(
        context.bot, chat_id, parts,
        message_id=placeholder.message_id,
        outbox=context.application.bot_data.get("outbox"),
    )
    if attachments:
        await _send_attachments(context, chat_id, s.short_id, attachments)
        await context.bot.send_message(
            chat_id,
            f"Sent {len(attachments)} file(s).",
            parse_mode=None,
        )


# --------------------------------------------------------------------------
# plain-text + inbound attachment routing
# --------------------------------------------------------------------------

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    mgr = _mgr(context)
    chat_id = update.effective_chat.id

    pending_parent = context.user_data.pop("pending_mkdir", None)
    if pending_parent:
        await _handle_new_folder_name(update, context, pending_parent, update.message.text)
        return

    s = mgr.get_active(chat_id)
    if s is None:
        await update.message.reply_text(
            "⚠️ <b>暂无激活会话</b>\n\n请发送 /new 选择项目目录开启新会话。",
            parse_mode=ParseMode.HTML,
        )
        return

    await _start_user_prompt(context, chat_id, s, update.message.text)


def _inbound_thinking_label(items: list[PendingInbound]) -> str:
    images = sum(1 for item in items if describe_inbound(item.path) == "image")
    files = len(items) - images
    if images and not files:
        if images == 1:
            return "\U0001F5BC reviewing image\u2026"
        return f"\U0001F5BC reviewing {images} images\u2026"
    if files and not images:
        if files == 1:
            return "\U0001F4CE reviewing file\u2026"
        return f"\U0001F4CE reviewing {files} files\u2026"
    return f"\U0001F4CE reviewing {len(items)} attachments\u2026"


async def _flush_inbound_batch(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    sid: str,
    items: list[PendingInbound],
) -> None:
    mgr = _mgr(context)
    s = mgr.sessions.get(sid)
    if s is None or mgr.active.get(chat_id) != sid:
        if items:
            names = ", ".join(item.path.name for item in items)
            await context.bot.send_message(
                chat_id,
                f"⚠️ <b>忽略文件</b>\n\n会话已改变，已丢弃 {len(items)} 个接收文件 ({names})。",
                parse_mode=ParseMode.HTML,
            )
        return
    prompt = build_combined_user_message(items, s.cwd)
    names = ", ".join(item.path.name for item in items)
    _log_action(
        "Inbound batch",
        session=s.short_id,
        count=len(items),
        files=names,
    )
    await _start_user_prompt(
        context,
        chat_id,
        s,
        prompt,
        thinking_label=_inbound_thinking_label(items),
        log_kind="Inbound prompt",
    )


async def handle_inbound_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    mgr = _mgr(context)
    chat_id = update.effective_chat.id

    s = mgr.get_active(chat_id)
    if s is None:
        await update.message.reply_text(
            "⚠️ <b>暂无激活会话</b>\n\n请发送 /new 选择项目目录开启新会话。",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        pending = await save_inbound_attachment(update, context, s.cwd)
    except ValueError as exc:
        await update.message.reply_text(f"\u274C {exc}")
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("inbound download failed")
        await update.message.reply_text(f"\u274C Could not save attachment: {exc}")
        return

    _log_action("Inbound queued", session=s.short_id, kind=pending.kind, file=pending.path.name)
    batcher: InboundBatcher = context.application.bot_data["inbound_batcher"]
    count = await batcher.add(
        chat_id,
        s.short_id,
        pending,
        lambda batch: _flush_inbound_batch(context, chat_id, s.short_id, batch),
    )
    if count == 1:
        logger.info(
            "Inbound batch started for %s (%.1fs window)",
            s.short_id,
            INBOUND_BATCH_DELAY_SEC,
        )


# --------------------------------------------------------------------------
# inline keyboard callbacks
# --------------------------------------------------------------------------

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    query = update.callback_query
    data = query.data or ""
    _log_action("Callback", data=data, user=update.effective_user.id)
    cfg = _cfg(context)
    mgr = _mgr(context)
    tokens = _tokens(context)
    chat_id = query.message.chat_id

    # Any action other than naming a new folder abandons a pending name capture.
    if not data.startswith("mkdir:"):
        context.user_data.pop("pending_mkdir", None)

    if data == "noop":
        await query.answer()
        return
    elif data.startswith("qkeep:") or data.startswith("qsend:") or data.startswith("qdrop:"):
        # qkeep|qsend|qdrop:<sid>:<token>
        parts = data.split(":", 2)
        if len(parts) != 3:
            await query.answer("Invalid queue action.", show_alert=True)
            return
        action, sid, token = parts
        s = mgr.sessions.get(sid)
        if s is None:
            await query.answer("Session gone.", show_alert=True)
            return

        if action == "qkeep":
            # Confirm stay in queue — drain may run it once the session is idle.
            item = mgr.confirm_queued_by_token(sid, token)
            if item is None:
                await query.answer("已不在队列中", show_alert=True)
                try:
                    await query.edit_message_reply_markup(reply_markup=None)
                except BadRequest:
                    pass
                return
            await query.answer("已确认排队")
            remaining = mgr.queued_count(sid)
            try:
                await query.edit_message_text(
                    f"📋 <b>已排队</b>（共 {remaining} 条）\n"
                    f"<i>{esc(_queue_prompt_preview(item.prompt))}</i>\n"
                    "当前任务结束后按顺序执行。",
                    parse_mode=ParseMode.HTML,
                )
            except BadRequest:
                pass
            try:
                await _drain_prompt_queue(context, s)
            except Exception:  # noqa: BLE001
                logger.exception("prompt queue drain after qkeep failed  session=%s", sid)
            return

        item = mgr.remove_queued_by_token(sid, token)
        if item is None:
            await query.answer("已不在队列中", show_alert=True)
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except BadRequest:
                pass
            return
        if action == "qdrop":
            # Cancel the *new* command only — running task keeps going.
            await query.answer("已取消新命令")
            remaining = mgr.queued_count(sid)
            try:
                await query.edit_message_text(
                    f"❎ <b>已取消新命令</b>（不影响正在执行的任务）"
                    + (f"\n队列仍有 {remaining} 条" if remaining else "")
                    + f"\n<i>{esc(_queue_prompt_preview(item.prompt))}</i>",
                    parse_mode=ParseMode.HTML,
                )
            except BadRequest:
                pass
            return
        # qsend — Send now: drop other queued items, cancel current, run this.
        others = mgr.clear_prompt_queue(sid)
        await query.answer("立刻发送…")
        try:
            await query.edit_message_text(
                "⚡ <b>立刻发送</b> — 中止当前任务并执行这条"
                + (f"\n（已丢弃其余排队 {others} 条）" if others else "")
                + f"\n<i>{esc(_queue_prompt_preview(item.prompt))}</i>",
                parse_mode=ParseMode.HTML,
            )
        except BadRequest:
            pass
        await _interrupt_and_run(
            context,
            item.chat_id,
            s,
            item.prompt,
            thinking_label=item.thinking_label,
            log_kind=item.log_kind,
        )
        return
    elif data.startswith("fsend:"):
        tok = data[6:]
        raw = tokens.path(tok)
        s = mgr.get_active(chat_id)
        if s is None:
            await query.answer("No active session.", show_alert=True)
            return
        p = await asyncio.to_thread(resolve_attachment, raw, s.cwd) if raw else None
        if p is None:
            await query.answer("File not found.", show_alert=True)
            return
        ok = await _send_one_attachment(context, chat_id, s.short_id, p, live=False)
        await query.answer("Sent." if ok else "Send failed.", show_alert=not ok)
        return
    elif data.startswith("ffiles:"):
        tok = data[7:]
        raw = tokens.path(tok)
        s = mgr.get_active(chat_id)
        if not s or not raw:
            await query.answer("No active session.", show_alert=True)
            return
        await query.answer()
        files = await asyncio.to_thread(
            list_dir_files, raw, s.cwd, limit=cfg.browser_page_size,
        )
        kb = files_keyboard(files, s.cwd, tokens, cfg.browser_page_size)
        await query.edit_message_text(
            f"Files in <code>{esc(Path(raw).name)}</code>",
            reply_markup=kb,
        )
        return
    elif data.startswith("fdir:"):
        tok = data[5:]
        raw = tokens.path(tok)
        s = mgr.get_active(chat_id)
        if not s or not raw:
            await query.answer("No active session.", show_alert=True)
            return
        await query.answer()
        kb = files_dir_keyboard(raw, s.cwd, tokens, cfg.browser_page_size)
        await query.edit_message_text(
            f"<code>{esc(raw)}</code>",
            reply_markup=kb,
        )
        return

    await query.answer()

    if data.startswith("mkdir:"):
        path = tokens.path(data[6:])
        if path:
            context.user_data["pending_mkdir"] = path
            await query.edit_message_text(
                f"\U0001F4C2 <code>{esc(path)}</code>\n"
                "\u2795 Send a name for the new folder \u2014 I'll create it here and "
                "start a session in it.\nSend /new to cancel.",
            )
    elif data.startswith("pick:"):
        path = tokens.path(data[5:])
        if path:
            await query.edit_message_text(f"\U0001F4C2 <code>{esc(path)}</code>\nStarting session\u2026")
            await _create_and_activate(update, context, path)
    elif data.startswith("nav:"):
        path = tokens.path(data[4:])
        if path:
            kb = browser_keyboard(path, tokens, cfg.browser_page_size)
            await query.edit_message_text(f"\U0001F4C2 <code>{esc(path)}</code>", reply_markup=kb)
    elif data.startswith("use:"):
        path = tokens.path(data[4:])
        if path:
            await query.edit_message_text(f"\U0001F4C2 <code>{esc(path)}</code>\nStarting session\u2026")
            await _create_and_activate(update, context, path)
    elif data.startswith("sess:"):
        sid = data[5:]
        s = mgr.sessions.get(sid)
        if s:
            mgr.set_active(chat_id, sid)
            await query.edit_message_text(
                f"已切换到会话 <b>{esc(s.name)}</b>。\n"
                "已恢复该会话自己的对话历史。"
            )
    elif data.startswith("cancel:"):
        sid = data[7:]
        s = mgr.sessions.get(sid)
        queued = mgr.queued_count(sid) if s else 0
        ok = await mgr.cancel(s) if s else False
        # Toast only — live message shows ✋ cancelled; avoid a stuck "Cancelling…" bubble.
        if ok:
            toast = "已取消"
            if queued:
                toast = f"已取消（{queued} 条排队将继续）"
            await query.answer(toast)
            return
        if s and mgr.is_busy(s):
            await query.answer("Clearing stuck run…")
        else:
            await query.answer("Nothing is running.")
    elif data.startswith("endyes:"):
        sid = data[7:]
        s = mgr.sessions.get(sid)
        name = esc(s.name) if s else sid
        origin = _callback_origin(query)
        audit = _pop_end_request(context, sid)
        audit.update(origin)
        audit["confirmed"] = True
        ok = await mgr.end_session(sid, **audit)
        notice = f"✅ 已关闭会话 <b>{name}</b>。" if ok else "会话未找到。"
        try:
            await query.edit_message_text(notice)
        except BadRequest:
            await context.bot.send_message(chat_id, notice)
    elif data.startswith("endno:"):
        sid = data[6:]
        s = mgr.sessions.get(sid)
        name = esc(s.name) if s else sid
        audit = _pop_end_request(context, sid)
        audit.update(_callback_origin(query))
        mgr.log_session_event(sid, "end_declined", **audit)
        notice = f"已保留会话 <b>{name}</b>。"
        try:
            await query.edit_message_text(notice)
        except BadRequest:
            await context.bot.send_message(chat_id, notice)
    elif data.startswith("end:"):
        sid = data[4:]
        s = mgr.sessions.get(sid)
        if s is None:
            await context.bot.send_message(chat_id, "会话未找到。")
            return
        origin = _callback_origin(query)
        _stash_end_request(context, sid, via="button:end", **origin)
        mgr.log_session_event(sid, "end_requested", via="button:end", **origin)
        await context.bot.send_message(chat_id, _end_confirm_text(s), reply_markup=_end_confirm_kb(sid))
    elif data.startswith("model:"):
        model_id = data[6:]
        if not model_id:
            await context.bot.send_message(chat_id, "Unknown model — send /model again.")
            return
        s = mgr.get_active(chat_id)
        if s is None:
            await query.edit_message_text("No active session. Use /new.")
            return
        await mgr.set_model(s, model_id)
        await query.edit_message_text(
            model_set_notice(model_id),
            parse_mode=ParseMode.HTML,
        )
    elif data.startswith("effort:"):
        value = data[7:]
        s = mgr.get_active(chat_id)
        if s is None:
            await query.edit_message_text("No active session. Use /new.")
            return
        normalized = await mgr.set_effort(s, value)
        if normalized is None:
            await context.bot.send_message(
                chat_id,
                f"Effort {esc(value)!r} is not supported for "
                f"<code>{esc(s.model)}</code>. Send /effort for options.",
            )
            return
        options = await mgr.effort_options(s) or []
        try:
            await query.edit_message_text(
                f"Effort set to <b>{esc(normalized)}</b> "
                f"for <code>{esc(s.model)}</code>.",
                reply_markup=_effort_kb(options, normalized),
            )
        except BadRequest:
            pass
    elif data.startswith("mode:"):
        mode = data[5:]
        s = mgr.get_active(chat_id)
        if s is None:
            await query.edit_message_text("No active session. Use /new.")
            return
        normalized = mgr.set_mode(s, mode)
        if normalized is None:
            return
        try:
            await query.edit_message_text(
                f"Mode set to <b>{esc(normalized)}</b>.",
                reply_markup=_mode_kb(normalized),
            )
        except BadRequest:
            pass
    elif data.startswith("usage:"):
        await _send_usage(context, chat_id)
    elif data.startswith("compact:"):
        sid = data[8:]
        s = mgr.sessions.get(sid)
        if s is None:
            await context.bot.send_message(chat_id, f"<code>[{esc(sid)}]</code> not found.")
            return
        mgr.set_active(chat_id, sid)
        if not _schedule_session_work(
            context,
            s,
            _run_compact(context, chat_id, s),
            name="compact",
        ):
            await context.bot.send_message(
                chat_id,
                f"Already running \u2014 /cancel first.",
            )
    elif data.startswith("status:"):
        sid = data[7:]
        s = mgr.sessions.get(sid)
        if s is None:
            await context.bot.send_message(chat_id, f"<code>[{esc(sid)}]</code> not found.")
            return
        await context.bot.send_message(chat_id, _status_text(s, mgr), reply_markup=_status_kb(s.short_id))
    elif data.startswith("menu:"):
        action = data[5:]
        if action == "new":
            kb = projects_keyboard(cfg, tokens)
            await context.bot.send_message(chat_id, "Pick a folder for the new session:", reply_markup=kb)
        elif action == "browse":
            start = cfg.projects_root if cfg.projects_root.is_dir() else Path.home()
            kb = await asyncio.to_thread(
                browser_keyboard, str(start), tokens, cfg.browser_page_size,
            )
            await context.bot.send_message(chat_id, f"\U0001F4C2 <code>{esc(start)}</code>", reply_markup=kb)
        elif action == "sessions":
            _log_sessions_view(mgr, via="menu:sessions", user_id=update.effective_user.id, chat_id=chat_id)
            view = _sessions_view(mgr, chat_id)
            if view is None:
                await context.bot.send_message(chat_id, "No sessions yet. Use /new to start one.", reply_markup=_menu_kb())
            else:
                text, kb = view
                await context.bot.send_message(chat_id, text, reply_markup=kb)
        elif action == "files":
            s = mgr.get_active(chat_id)
            if s is None:
                await context.bot.send_message(chat_id, "No active session. Use /new.", reply_markup=_menu_kb())
            else:
                files = await asyncio.to_thread(
                    list_session_files, s.cwd, limit=cfg.browser_page_size,
                )
                kb = files_keyboard(files, s.cwd, tokens, cfg.browser_page_size)
                await context.bot.send_message(
                    chat_id,
                    f"Files in <code>{esc(s.name)}</code> (newest first). Tap to send.",
                    reply_markup=kb,
                )
        elif action == "status":
            s = mgr.get_active(chat_id)
            if s is None:
                await context.bot.send_message(chat_id, "No active session. Use /new.", reply_markup=_menu_kb())
            else:
                await context.bot.send_message(chat_id, _status_text(s, mgr), reply_markup=_status_kb(s.short_id))
        elif action == "model":
            s = mgr.get_active(chat_id)
            if s is None:
                await context.bot.send_message(chat_id, "No active session. Use /new.", reply_markup=_menu_kb())
            else:
                await _send_model_picker(context, chat_id, s)
        elif action == "mode":
            s = mgr.get_active(chat_id)
            if s is None:
                await context.bot.send_message(chat_id, "No active session. Use /new.", reply_markup=_menu_kb())
            else:
                await context.bot.send_message(
                    chat_id,
                    f"Current mode: <b>{esc(s.mode)}</b>\n\nTap to switch:",
                    reply_markup=_mode_kb(s.mode),
                )
        elif action == "effort":
            s = mgr.get_active(chat_id)
            if s is None:
                await context.bot.send_message(chat_id, "No active session. Use /new.", reply_markup=_menu_kb())
            else:
                await _send_effort_picker(context, chat_id, s)
        elif action == "restart":
            if not _is_owner(update, context):
                await query.answer("仅主人可重启服务。", show_alert=True)
                return
            bot_name = (_bot_cfg(context).name if _bot_cfg(context) else "default")
            _request_restart(cfg, chat_id, mode="restart", bot=bot_name)
            _log_action("Restart requested (menu)", user=update.effective_user.id, bot=bot_name)
            await context.bot.send_message(
                chat_id,
                "♻️ <b>正在重启 Bot 服务…</b>\n\n"
                "重新加载配置与会话状态，恢复在线时会自动通知您。",
                parse_mode=ParseMode.HTML,
            )
        elif action == "reload":
            if not _is_owner(update, context):
                await query.answer("仅主人可重载守护进程。", show_alert=True)
                return
            bot_name = (_bot_cfg(context).name if _bot_cfg(context) else "default")
            _request_restart(cfg, chat_id, mode="reload", bot=bot_name)
            _log_action("Reload requested (menu)", user=update.effective_user.id, bot=bot_name)
            await context.bot.send_message(
                chat_id,
                "⚛️ <b>正在重载守护进程…</b>\n\n"
                "重新加载 Python 源代码与环境，恢复在线时会自动通知您。",
                parse_mode=ParseMode.HTML,
            )
            asyncio.create_task(_kickstart_after_reply(cfg))


def _app_bot_name(app: Application) -> str:
    bot_cfg: BotConfig | None = app.bot_data.get("bot_cfg")
    return bot_cfg.name if bot_cfg else "default"


def _save_restart_notify(
    cfg: Config,
    chat_id: int,
    mode: str = "restart",
    *,
    bot: str | None = None,
) -> None:
    """Persist a post-restart Telegram notify, tagged with the triggering bot.

    Private chats share the same numeric ``chat_id`` across bots (the user id),
    so the ``bot`` field is required to deliver the ack on the correct bot.
    """
    path = cfg.state_dir / "restart_notify.json"
    payload = {
        "chat_id": chat_id,
        "mode": mode,
        "bot": (bot or "default").strip() or "default",
    }
    path.write_text(json.dumps(payload))


async def _send_restart_notify(app: Application) -> None:
    cfg: Config = app.bot_data["cfg"]
    path = cfg.state_dir / "restart_notify.json"
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text())
        chat_id = int(data["chat_id"])
        mode = str(data.get("mode", "restart"))
        target_bot = str(data.get("bot") or "").strip()
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        path.unlink(missing_ok=True)
        return

    this_bot = _app_bot_name(app)
    # Legacy files (no bot field): only the primary bot may claim them.
    if not target_bot:
        if not app.bot_data.get("is_primary_bot", True):
            return
        target_bot = this_bot
    if target_bot != this_bot:
        return

    if mode == "reload":
        msg = (
            "✅ <b>守护进程重载成功</b>\n\n"
            "最新 Python 源码与系统环境已恢复上线。"
        )
    else:
        msg = (
            "✅ <b>Bot 服务重启成功</b>\n\n"
            "最新配置与会话状态已加载完成。"
        )

    for attempt in range(1, 4):
        try:
            await app.bot.send_message(
                chat_id,
                msg,
                parse_mode=ParseMode.HTML,
            )
            logger.info(
                "Restart notification (%s) sent to chat_id=%s bot=%s (attempt %d)",
                mode, chat_id, this_bot, attempt,
            )
            _log_action(
                "Restart complete notify sent",
                chat=chat_id, mode=mode, bot=this_bot,
            )
            path.unlink(missing_ok=True)
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Attempt %d/3 failed to send restart notify to chat %s bot=%s: %s",
                attempt, chat_id, this_bot, exc,
            )
            await asyncio.sleep(1.0)

    path.unlink(missing_ok=True)


async def _delayed_restart_notify(app: Application) -> None:
    await asyncio.sleep(1.5)
    await _send_restart_notify(app)


def _launchd_target() -> str:
    """launchctl domain path for this user's LaunchAgent."""
    return f"gui/{os.getuid()}/{LAUNCHD_LABEL}"


async def _kickstart_after_reply(cfg: Config) -> None:
    """Ask launchd to kill and respawn the service (full code reload)."""
    await asyncio.sleep(0.5)
    target = _launchd_target()
    logger.info("launchctl kickstart -k %s", target)
    proc = await asyncio.create_subprocess_exec(
        "launchctl",
        "kickstart",
        "-k",
        target,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    # kickstart -k normally terminates this process. If it returns, report the
    # failure and let the already-written restart flag drive graceful recovery.
    if proc.returncode:
        logger.error(
            "launchctl kickstart failed rc=%s stdout=%s stderr=%s",
            proc.returncode,
            stdout.decode(errors="replace").strip(),
            stderr.decode(errors="replace").strip(),
        )


async def cmd_reload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Full launchd restart — equivalent to unload + load, doable from Telegram."""
    if not await _guard(update, context, require_owner=True):
        return
    cfg = _cfg(context)
    bot_name = (_bot_cfg(context).name if _bot_cfg(context) else "default")
    _request_restart(cfg, update.effective_chat.id, mode="reload", bot=bot_name)
    _log_action("Reload requested (launchd kickstart)", user=update.effective_user.id, bot=bot_name)
    await update.message.reply_text(
        "⚛️ <b>正在重载守护进程…</b>\n\n"
        "重新加载 Python 源代码与环境，恢复在线时会自动通知您。",
        parse_mode=ParseMode.HTML,
    )
    asyncio.create_task(_kickstart_after_reply(cfg))


async def cmd_restart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context, require_owner=True):
        return
    cfg = _cfg(context)
    bot_name = (_bot_cfg(context).name if _bot_cfg(context) else "default")
    _request_restart(cfg, update.effective_chat.id, mode="restart", bot=bot_name)
    _log_action("Restart requested", user=update.effective_user.id, bot=bot_name)
    await update.message.reply_text(
        "♻️ <b>正在重启 Bot 服务…</b>\n\n"
        "重新加载配置与会话状态，恢复在线时会自动通知您。",
        parse_mode=ParseMode.HTML,
    )


# --------------------------------------------------------------------------
# application lifecycle
# --------------------------------------------------------------------------

async def _resume_sessions_bg(mgr: SessionManager) -> None:
    try:
        await mgr.resume_sessions()
        logger.info("Session resume finished (%d session(s))", len(mgr.sessions))
    except Exception:  # noqa: BLE001
        logger.exception("Background session resume failed")


def _make_outbox_sender(app: Application):
    """Return the per-app outbox delivery callback."""

    async def send(chat_id: int, text: str, parse_mode: str | None) -> bool:
        try:
            await _await_telegram(
                lambda: app.bot.send_message(
                    chat_id,
                    text,
                    parse_mode=parse_mode,
                ),
            )
            return True
        except BadRequest as exc:
            # Permanently undeliverable (bad entities, deleted chat, …) — drop.
            logger.warning("outbox drop (permanent) chat_id=%s: %s", chat_id, exc)
            return True
        except NetworkError as exc:
            logger.warning("outbox send failed (retry later) chat_id=%s: %s", chat_id, exc)
            return False

    return send


async def _post_init(app: Application) -> None:
    try:
        await app.bot.delete_webhook(drop_pending_updates=False)
    except Exception as exc:  # noqa: BLE001
        logger.warning("delete_webhook failed: %s", exc)

    cfg = app.bot_data["cfg"]
    bot_cfg = app.bot_data.get("bot_cfg")
    _load_busy_policy(app, cfg)
    mgr = SessionManager(cfg, bot_cfg)
    await mgr.start()
    app.bot_data["manager"] = mgr
    # Resume Cursor bridges in the background so Telegram polling can start immediately.
    app.bot_data["resume_task"] = asyncio.create_task(
        _resume_sessions_bg(mgr),
        name="resume-sessions",
    )
    if app.bot_data.get("is_primary_bot", True) and cfg.console_enabled:
        web = WebConsole(cfg)
        try:
            web.start()
            app.bot_data["web_console"] = web
        except Exception as exc:  # noqa: BLE001
            logger.warning("WebConsole failed to start: %s", exc)

    _print_startup_banner(cfg, mgr, bot_cfg)
    _clear_restart_request(cfg)
    _log_action("Bridge ready", bot=bot_cfg.name if bot_cfg else "default", sessions=len(mgr.sessions))
    outbox: TelegramOutbox | None = app.bot_data.get("outbox")
    if outbox is not None:
        outbox.start(_make_outbox_sender(app))
        if outbox.pending_count():
            logger.info("Outbox resumed with %d pending item(s)", outbox.pending_count())
    try:
        commands = [
            BotCommand("new", "🆕 新建：创建项目新会话"),
            BotCommand("browse", "📁 目录：逐级选择项目"),
            BotCommand("sessions", "📋 会话：管理所有活动会话"),
            BotCommand("status", "📊 状态：查看系统运行状态"),
            BotCommand("rename", "✏️ 命名：修改当前会话名称"),
            BotCommand("model", "🧩 模型：切换 AI 模型"),
            BotCommand("effort", "🧠 思考：设置思考推理等级"),
            BotCommand("mode", "⚡ 模式：切换 agent/plan 模式"),
            BotCommand("busy", "🚦 忙碌：排队或打断新消息"),
            BotCommand("usage", "🔋 配额：查看额度与用量"),
            BotCommand("cancel", "⏸️ 停止：中断当前运行任务"),
            BotCommand("restart", "♻️ 重启：刷新服务与配置"),
            BotCommand("reload", "⚛️ 重载：载入最新程序代码"),
        ]
        try:
            await app.bot.delete_my_commands(scope=BotCommandScopeDefault())
            await app.bot.delete_my_commands(scope=BotCommandScopeAllPrivateChats())
            await app.bot.delete_my_commands(scope=BotCommandScopeDefault(), language_code="zh")
            await app.bot.delete_my_commands(scope=BotCommandScopeAllPrivateChats(), language_code="zh")
            await app.bot.delete_my_commands(scope=BotCommandScopeDefault(), language_code="en")
            await app.bot.delete_my_commands(scope=BotCommandScopeAllPrivateChats(), language_code="en")
        except Exception:
            pass

        await app.bot.set_my_commands(commands, scope=BotCommandScopeDefault())
        await app.bot.set_my_commands(commands, scope=BotCommandScopeAllPrivateChats())
        await app.bot.set_my_commands(commands, scope=BotCommandScopeDefault(), language_code="zh")
        await app.bot.set_my_commands(commands, scope=BotCommandScopeAllPrivateChats(), language_code="zh")
        await app.bot.set_my_commands(commands, scope=BotCommandScopeDefault(), language_code="en")
        await app.bot.set_my_commands(commands, scope=BotCommandScopeAllPrivateChats(), language_code="en")
    except Exception as exc:  # noqa: BLE001 - non-fatal cosmetic setup
        logger.warning("set_my_commands failed: %s", exc)


async def _stop_health_probe(app: Application) -> None:
    probe: HealthProbe | None = app.bot_data.get("health_probe")
    if probe is not None:
        await probe.stop()
        app.bot_data["health_probe"] = None


async def _cancel_app_background_tasks(app: Application) -> None:
    """Cancel tasks owned by this app before closing its Cursor bridges."""
    current = asyncio.current_task()
    tasks: set[asyncio.Task] = set(app.bot_data.get("_bg_tasks", set()))
    resume_task = app.bot_data.get("resume_task")
    if isinstance(resume_task, asyncio.Task):
        tasks.add(resume_task)
    tasks.discard(current)
    live_tasks = [task for task in tasks if not task.done()]
    for task in live_tasks:
        task.cancel()
    if live_tasks:
        await asyncio.gather(*live_tasks, return_exceptions=True)
    app.bot_data["_bg_tasks"] = set()
    app.bot_data["resume_task"] = None

    batcher: InboundBatcher | None = app.bot_data.get("inbound_batcher")
    if batcher is not None:
        await batcher.stop()


async def _post_shutdown(app: Application) -> None:
    await _stop_health_probe(app)
    await _cancel_app_background_tasks(app)
    web = app.bot_data.get("web_console")
    if web is not None:
        web.stop()
    mgr = app.bot_data.get("manager")
    if mgr is not None:
        await mgr.stop()
        _log_action("Bridge stopped")
    outbox: TelegramOutbox | None = app.bot_data.get("outbox")
    if outbox is not None:
        await outbox.stop()


async def _restart_updater(app: Application) -> bool:
    """Stop and restart one bot's Telegram poller in-process.

    Used by the health probe so a wedged poller recovers without tearing down
    the whole process (and the other bots sharing it). Returns True when the
    poller is running again.
    """
    updater = app.updater
    if updater is None:
        logger.error("Health probe: no updater to restart")
        return False
    bot_name = (app.bot_data.get("bot_cfg").name
                if app.bot_data.get("bot_cfg") else "default")
    try:
        if updater.running:
            await updater.stop()
        await updater.start_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=False,
            error_callback=_make_poll_error_callback(app),
        )
        logger.warning("Health probe: poller restarted  bot=%s", bot_name)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "Health probe: updater restart failed  bot=%s  err=%s",
            bot_name,
            exc,
        )
        return False


def _start_health_probe(
    app: Application,
    cfg: Config,
    bot_cfg: BotConfig | None,
) -> None:
    """Per-bot health probe + heartbeat; primary bot owns process recovery.

    Every bot app gets its own probe and getMe heartbeat (multi-bot parity).
    Process-level recovery (soft restart / launchd kickstart) is only wired on
    the primary bot so a secondary poll wedge cannot tear down all bots.
    """
    is_primary = app.bot_data.get("is_primary_bot", True)
    bot_name = bot_cfg.name if bot_cfg else "default"

    async def _soft() -> bool:
        """Per-bot in-process updater restart; other bots stay untouched."""
        ok = await _restart_updater(app)
        if not ok:
            logger.error("Health probe: updater restart failed  bot=%s", bot_name)
        return ok

    async def _kick() -> None:
        # Health incidents are log-only. Manual /restart and /reload retain
        # their Telegram acknowledgements through their own command paths.
        await _kickstart_after_reply(cfg)

    async def _heartbeat() -> None:
        await app.bot.get_me()

    def _updater_ok() -> bool:
        updater = app.updater
        return bool(updater and updater.running)

    probe = HealthProbe(
        check_interval_sec=cfg.health_check_interval_sec,
        poll_fail_threshold=cfg.health_poll_fail_threshold,
        quiet_sec=cfg.health_quiet_sec,
        heartbeat_interval_sec=cfg.health_heartbeat_interval_sec,
        kickstart_after_soft=cfg.health_kickstart_after_soft,
        # Every bot heals its own updater in-process; only the primary bot may
        # escalate to process-level launchd kickstart.
        soft_restart_cb=_soft,
        kickstart_cb=_kick if is_primary else None,
        notify_cb=None,
        is_updater_running=_updater_ok,
        heartbeat_cb=_heartbeat,
    )
    app.bot_data["health_probe"] = probe
    probe.start()
    logger.info(
        "Health probe started  bot=%s  interval=%.0fs fail_threshold=%s "
        "heartbeat=%.0fs kickstart_after=%s  primary=%s",
        bot_name,
        cfg.health_check_interval_sec,
        cfg.health_poll_fail_threshold,
        cfg.health_heartbeat_interval_sec,
        cfg.health_kickstart_after_soft,
        is_primary,
    )


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    if isinstance(err, Conflict):
        _log_action("Telegram poll conflict — scheduling restart")
        cfg = context.application.bot_data.get("cfg")
        if cfg is not None:
            _request_restart(cfg)
        else:
            global _pending_restart
            _pending_restart = True
        return
    if isinstance(err, (NetworkError, RetryAfter)) or should_count_as_poll_error(err):
        # Handler/send failures are not getUpdates failures. The polling
        # callback records poll health directly so unrelated Bot API traffic
        # cannot poison the poller recovery counter.
        logger.warning("Telegram handler network event: %s", err)
        return
    logger.error("Unhandled error: %s", err, exc_info=err)


def _proxy_url_from_env() -> str | None:
    """Optional explicit HTTP(S) proxy for Telegram (HTTPS_PROXY / HTTP_PROXY / ALL_PROXY).

    Leave unset under Stash/Clash TUN + fake-ip. Do not rely on macOS system
    proxy auto-detection — it often points at :7890 and breaks long-polling.
    """
    for key in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy"):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return None


def _make_poll_error_callback(app: Application):
    """Return PTB ``start_polling`` ``error_callback`` for one bot app.

    This is the only source allowed to increment poll-failure state. Application
    error handlers also see outbound send/edit errors, which must not trigger a
    getUpdates restart.
    """

    def error_callback(exc: BaseException) -> None:
        try:
            logger.warning("Telegram poll event: %s", exc)
            if isinstance(exc, Conflict):
                _log_action("Telegram poll conflict — scheduling restart")
                cfg = app.bot_data.get("cfg")
                if cfg is not None:
                    _request_restart(cfg)
                else:
                    global _pending_restart
                    _pending_restart = True
                return
            if isinstance(exc, (NetworkError, RetryAfter)) or should_count_as_poll_error(exc):
                probe: HealthProbe | None = app.bot_data.get("health_probe")
                if probe is not None:
                    probe.note_poll_error(exc)
        except Exception:  # noqa: BLE001 - must never raise into the poll loop
            logger.exception("Poll error callback failed")

    return error_callback


def _telegram_httpx_kwargs(limits: httpx.Limits) -> dict:
    # trust_env=False: ignore macOS/scutil HTTP proxy. TUN already routes us;
    # trusting env/system proxy to :7890 causes ConnectError/TimedOut on getUpdates.
    kwargs: dict = {"limits": limits, "trust_env": False}
    proxy = _proxy_url_from_env()
    if proxy:
        safe = proxy.split("@")[-1] if "@" in proxy else proxy
        logger.info("Telegram HTTP client using explicit proxy %s", safe)
        kwargs["proxy"] = proxy
    return kwargs


def _build_app(cfg: Config, bot_cfg: BotConfig | None = None) -> Application:
    target_bot = bot_cfg or (
        cfg.bots[0]
        if cfg.bots
        else BotConfig(
            name="default",
            token=cfg.telegram_token,
            allowed_user_id=cfg.allowed_user_id,
            model=cfg.model,
            models=cfg.models,
            effort=cfg.effort,
        )
    )
    rate_limiter = AIORateLimiter(
        overall_max_rate=25,
        group_max_rate=18,
        group_time_period=60,
        max_retries=5,
    )
    # Short keepalive on getUpdates (long-poll holds one connection).
    # Separate pools: long-polling must not starve sendMessage / editMessage.
    api_limits = httpx.Limits(
        max_keepalive_connections=4,
        max_connections=32,
        keepalive_expiry=5.0,
    )
    poll_limits = httpx.Limits(
        max_keepalive_connections=1,
        max_connections=4,
        keepalive_expiry=5.0,
    )
    request = HTTPXRequest(
        connection_pool_size=32,
        connect_timeout=15.0,
        read_timeout=35.0,
        write_timeout=30.0,
        pool_timeout=30.0,
        http_version="1.1",
        httpx_kwargs=_telegram_httpx_kwargs(api_limits),
    )
    get_updates_request = HTTPXRequest(
        connection_pool_size=4,
        connect_timeout=15.0,
        read_timeout=60.0,
        write_timeout=15.0,
        pool_timeout=30.0,
        http_version="1.1",
        httpx_kwargs=_telegram_httpx_kwargs(poll_limits),
    )
    app = (
        Application.builder()
        .token(target_bot.token)
        .request(request)
        .get_updates_request(get_updates_request)
        .concurrent_updates(False)
        .rate_limiter(rate_limiter)
        .defaults(Defaults(
            parse_mode=ParseMode.HTML,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        ))
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    app.bot_data["cfg"] = cfg
    app.bot_data["bot_cfg"] = target_bot
    app.bot_data["tokens"] = TokenStore()
    app.bot_data["inbound_batcher"] = InboundBatcher()
    app.bot_data["outbox"] = TelegramOutbox(
        bot_state_dir(cfg, target_bot.name) / "outbox.jsonl",
    )
    app.add_error_handler(_on_error)

    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("browse", cmd_browse))
    app.add_handler(CommandHandler("cd", cmd_cd))
    app.add_handler(CommandHandler("sessions", cmd_sessions))
    app.add_handler(CommandHandler("use", cmd_use))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("compact", cmd_compact))
    app.add_handler(CommandHandler("context", cmd_context))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("effort", cmd_effort))
    app.add_handler(CommandHandler("mode", cmd_mode))
    app.add_handler(CommandHandler("busy", cmd_busy))
    app.add_handler(CommandHandler(["rename", "title"], cmd_rename))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("end", cmd_end))
    app.add_handler(CommandHandler("files", cmd_files))
    app.add_handler(CommandHandler("usage", cmd_usage))
    app.add_handler(CommandHandler("restart", cmd_restart))
    app.add_handler(CommandHandler("reload", cmd_reload))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(
        (filters.PHOTO | filters.Document.ALL | filters.ANIMATION | filters.VIDEO | filters.AUDIO)
        & ~filters.COMMAND,
        handle_inbound_media,
    ))
    return app


async def _async_run_apps(cfg: Config, apps: list[Application], drop_pending: bool) -> None:
    initialized: list[Application] = []
    try:
        for app in apps:
            await app.initialize()
            initialized.append(app)
            # PTB only runs post_init inside run_polling/run_webhook — we must call it ourselves.
            if app.post_init:
                await app.post_init(app)
            await app.start()
            if app.updater:
                await app.updater.start_polling(
                    allowed_updates=Update.ALL_TYPES,
                    drop_pending_updates=drop_pending,
                    error_callback=_make_poll_error_callback(app),
                )
            # The probe must not observe updater_dead during startup.
            _start_health_probe(app, cfg, app.bot_data.get("bot_cfg"))
            await _send_restart_notify(app)

        while not _restart_wanted(cfg):
            await asyncio.sleep(0.5)
    finally:
        for app in reversed(initialized):
            # Stop the probe before intentionally stopping the updater, or it
            # can race shutdown and relaunch polling.
            try:
                await _stop_health_probe(app)
            except Exception:  # noqa: BLE001
                logger.warning("Error stopping health probe", exc_info=True)
            try:
                await _cancel_app_background_tasks(app)
            except Exception:  # noqa: BLE001
                logger.warning("Error cancelling app tasks", exc_info=True)
            try:
                if app.updater and app.updater.running:
                    await app.updater.stop()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Error stopping updater: %s", exc)
            try:
                if app.running:
                    await app.stop()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Error stopping application: %s", exc)
            try:
                if app.post_shutdown:
                    await app.post_shutdown(app)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Error in post_shutdown: %s", exc)
            try:
                await app.shutdown()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Error shutting down app: %s", exc)


def _run_once(cfg: Config) -> bool:
    """Run one polling cycle. Returns True when a restart was requested."""
    drop_pending = _restart_wanted(cfg)
    _clear_restart_request(cfg)
    _claim_singleton(cfg)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        logger.info("Polling Telegram (%d bot(s))\u2026", len(cfg.bots))
        apps: list[Application] = []
        for idx, b_cfg in enumerate(cfg.bots):
            if not b_cfg.token:
                logger.warning("Bot [%s] missing token; skipping.", b_cfg.name)
                continue
            app = _build_app(cfg, b_cfg)
            app.bot_data["is_primary_bot"] = (idx == 0)
            apps.append(app)
        if not apps:
            raise SystemExit("No valid bot tokens configured.")
        loop.run_until_complete(_async_run_apps(cfg, apps, drop_pending))
    finally:
        if not _restart_wanted(cfg):
            _release_pid_file(cfg)
        loop.close()
    return _restart_wanted(cfg)


def main() -> None:
    os.chdir(PROJECT_ROOT)
    _setup_logging()
    cfg = load_config(PROJECT_ROOT)
    if not cfg.telegram_token:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN_1 is missing. Copy .env.example to .env and fill it in."
        )
    if not cfg.cursor_api_key:
        raise SystemExit("CURSOR_API_KEY is missing. Copy .env.example to .env and fill it in.")

    logger.info("cursor-telegram-bridge %s starting (pid %d)", __version__, os.getpid())

    while True:
        restart = _run_once(cfg)
        if not restart:
            _clear_restart_request(cfg)
            logger.info("cursor-telegram-bridge stopped.")
            break
        logger.info("\u21bb Restarting in %.0fs (releasing Telegram poll)\u2026", RESTART_DELAY_S)
        time.sleep(RESTART_DELAY_S)
        # Soft /restart must pick up .env / config.toml changes (not just re-attach).
        cfg = load_config(PROJECT_ROOT)
        if not cfg.telegram_token or not cfg.cursor_api_key:
            logger.error(
                "Reload failed: missing TELEGRAM_BOT_TOKEN_1 or CURSOR_API_KEY in .env"
            )
            break
        logger.info("Reloaded .env / config.toml")
