from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - fallback for 3.9/3.10
    import tomli as tomllib  # type: ignore

from dotenv import load_dotenv

# Values accepted by cursor_sdk LocalAgentOptions.setting_sources.
_SETTING_SOURCES = frozenset({"project", "user", "team", "mdm", "plugins", "all"})


@dataclass
class Bookmark:
    name: str
    path: str


_PERMISSIONS = frozenset({"full", "readonly"})


@dataclass
class BotConfig:
    name: str
    token: str
    allowed_user_id: int | None
    model: str
    models: list[str] = field(default_factory=list)
    effort: str | None = None
    # Tool capability gate: "full" (default) or "readonly" (read/search + cwd only).
    permission: str = "full"
    # Extra Telegram chats (typically group/supergroup ids) that may talk to this bot.
    # Private chats still require allowed_user_id. Admin cmds (/reload,/restart) stay owner-only.
    allowed_chat_ids: list[int] = field(default_factory=list)


@dataclass
class Config:
    telegram_token: str
    cursor_api_key: str
    allowed_user_id: int | None
    projects_root: Path
    model: str
    browser_page_size: int
    bookmarks: list[Bookmark]
    state_dir: Path
    event_log_max: int
    console_enabled: bool
    console_host: str
    console_port: int
    console_token: str
    models: list[str] = field(default_factory=list)
    effort: str | None = None
    bots: list[BotConfig] = field(default_factory=list)
    # When a prompt is already running: "queue" (default) or "interrupt".
    busy_policy: str = "queue"
    # Cursor ambient settings for local agents (MCP/hooks/file rules under
    # `.cursor/` and `~/.cursor/`). Does NOT load Customize → User Rules
    # (those are cloud/IDE-only). Empty list = inline config only.
    setting_sources: list[str] = field(default_factory=lambda: ["user", "project"])
    # After a mid-stream bridge crash, try to resume the same agent on the new
    # bridge (keeps conversation context). Disable if resume reliably errors.
    try_resume_first: bool = True
    # Markdown/text injected on /new, resume, recreate, and after compact.
    rules_text: str = ""
    rules_file: Path | None = None
    # Abort a run when no assistant/tool progress for this many seconds.
    run_stall_timeout_sec: float = 300.0
    # Longer allowance while a tool call is in flight but emits no new events.
    run_tool_stall_timeout_sec: float = 600.0
    # Max auto-continue retries when a run stalls (no progress for run_stall_timeout_sec).
    stall_auto_continue_max: int = 3
    # Prompt automatically sent on watchdog stall.
    stall_auto_continue_prompt: str = "继续"
    # Health probe: how often to check Telegram poll liveness.
    health_check_interval_sec: float = 60.0
    # Consecutive poll failures before soft restart is considered.
    health_poll_fail_threshold: int = 8
    # Gap that splits poll-error bursts and confirms post-restart recovery.
    health_quiet_sec: float = 180.0
    # Per-bot getMe reachability telemetry; kept separate from getUpdates health.
    health_heartbeat_interval_sec: float = 30.0
    # Soft restarts before escalating to launchd kickstart (0 = never kickstart).
    health_kickstart_after_soft: int = 2
    # Display-only context window denominators keyed by model id / series
    # (see resolve_context_window). Does not change Cursor truncation.
    model_context_windows: dict[str, int] = field(default_factory=dict)

    @property
    def sessions_file(self) -> Path:
        # Legacy alias: primary bot registry (now under state/bots/default/).
        return self.state_dir / "bots" / "default" / "sessions.json"


def _expand(p: str, *, base: Path | None = None) -> Path:
    expanded = Path(os.path.expanduser(os.path.expandvars(p)))
    if base is not None and not expanded.is_absolute():
        expanded = base / expanded
    return expanded.resolve()


def load_config(project_root: Path) -> Config:
    load_dotenv(project_root / ".env")

    token = os.environ.get("TELEGRAM_BOT_TOKEN_1", "").strip()
    api_key = os.environ.get("CURSOR_API_KEY", "").strip()
    allowed_raw = os.environ.get("ALLOWED_TELEGRAM_USER_ID", "").strip()
    if allowed_raw:
        try:
            allowed: int | None = int(allowed_raw)
        except ValueError:
            raise SystemExit(
                "ALLOWED_TELEGRAM_USER_ID must be a numeric Telegram user id "
                f"(got {allowed_raw!r}). Fix it in .env."
            )
    else:
        allowed = None

    data: dict = {}
    cfg_path = project_root / "config.toml"
    if cfg_path.exists():
        with cfg_path.open("rb") as fh:
            data = tomllib.load(fh)

    projects_root = _expand(data.get("projects_root", "~"), base=project_root)
    model = str(data.get("model", "composer-2.5"))
    raw_effort = data.get("effort")
    effort = str(raw_effort).strip().lower() if raw_effort else None
    raw_models = data.get("models")
    if isinstance(raw_models, list):
        models = [str(m).strip() for m in raw_models if str(m).strip()]
    else:
        models = []
    page_size = int(data.get("browser_page_size", 20))
    event_log_max = int(data.get("event_log_max", 500))
    console_enabled = bool(data.get("console_enabled", True))
    console_host = str(data.get("console_host", "127.0.0.1"))
    console_port = int(data.get("console_port", 9477))
    console_token = os.environ.get("CONSOLE_TOKEN", "").strip()
    raw_busy = str(data.get("busy_policy", "queue")).strip().lower()
    busy_policy = raw_busy if raw_busy in ("interrupt", "queue") else "queue"
    setting_sources = _parse_setting_sources(data.get("setting_sources"))
    try_resume_first = bool(data.get("try_resume_first", True))
    rules_text, rules_file = _load_rules(data, project_root)
    run_stall_timeout_sec = _positive_float(
        data.get("run_stall_timeout_sec"), default=300.0, minimum=30.0,
    )
    run_tool_stall_timeout_sec = _positive_float(
        data.get("run_tool_stall_timeout_sec"), default=600.0, minimum=30.0,
    )
    if run_tool_stall_timeout_sec < run_stall_timeout_sec:
        run_tool_stall_timeout_sec = run_stall_timeout_sec
    stall_auto_continue_max = _positive_int(
        data.get("stall_auto_continue_max"), default=3, minimum=1,
    )
    raw_stall_prompt = data.get("stall_auto_continue_prompt")
    stall_auto_continue_prompt = (
        str(raw_stall_prompt).strip() if raw_stall_prompt is not None else "继续"
    ) or "继续"
    health_check_interval_sec = _positive_float(
        data.get("health_check_interval_sec"), default=60.0, minimum=10.0,
    )
    health_poll_fail_threshold = _positive_int(
        data.get("health_poll_fail_threshold"), default=8, minimum=2,
    )
    health_quiet_sec = _positive_float(
        data.get("health_quiet_sec"), default=180.0, minimum=60.0,
    )
    health_heartbeat_interval_sec = _positive_float(
        data.get("health_heartbeat_interval_sec"), default=30.0, minimum=10.0,
    )
    health_kickstart_after_soft = _nonneg_int(
        data.get("health_kickstart_after_soft"), default=2,
    )
    model_context_windows = _parse_model_context_windows(
        data.get("model_context_windows"),
    )
    bookmarks = [
        Bookmark(name=str(b["name"]), path=str(_expand(b["path"], base=project_root)))
        for b in data.get("bookmarks", [])
        if b.get("name") and b.get("path")
    ]

    state_dir = project_root / "state"
    state_dir.mkdir(exist_ok=True)

    raw_bots = data.get("bots")
    bots: list[BotConfig] = []
    if isinstance(raw_bots, list) and raw_bots:
        for idx, b_item in enumerate(raw_bots, 1):
            if not isinstance(b_item, dict):
                continue
            b_name = str(b_item.get("name", f"bot_{idx}")).strip()
            from .state_layout import sanitize_bot_name

            b_name = sanitize_bot_name(b_name, fallback=f"bot_{idx}")
            b_token = _resolve_bot_token(b_item, default_token=token, bot_name=b_name)
            b_allowed_raw = b_item.get("allowed_user_id")
            if b_allowed_raw is not None:
                try:
                    b_allowed: int | None = int(b_allowed_raw)
                except ValueError:
                    b_allowed = allowed
            else:
                b_allowed = allowed
            b_model = str(b_item.get("model", model))
            b_raw_effort = b_item.get("effort")
            b_effort = str(b_raw_effort).strip().lower() if b_raw_effort else effort
            b_raw_models = b_item.get("models")
            if isinstance(b_raw_models, list):
                b_models = [str(m).strip() for m in b_raw_models if str(m).strip()]
            else:
                b_models = list(models)
            b_permission = _parse_permission(b_item.get("permission"))
            b_chat_ids = _parse_int_list(b_item.get("allowed_chat_ids"))
            bots.append(
                BotConfig(
                    name=b_name,
                    token=b_token,
                    allowed_user_id=b_allowed,
                    model=b_model,
                    models=b_models,
                    effort=b_effort,
                    permission=b_permission,
                    allowed_chat_ids=b_chat_ids,
                )
            )

    if not bots:
        bots.append(
            BotConfig(
                name="default",
                token=token,
                allowed_user_id=allowed,
                model=model,
                models=models,
                effort=effort,
                permission="full",
                allowed_chat_ids=[],
            )
        )

    return Config(
        telegram_token=token,
        cursor_api_key=api_key,
        allowed_user_id=allowed,
        projects_root=projects_root,
        model=model,
        models=models,
        effort=effort,
        bots=bots,
        browser_page_size=page_size,
        bookmarks=bookmarks,
        state_dir=state_dir,
        event_log_max=event_log_max,
        console_enabled=console_enabled,
        console_host=console_host,
        console_port=console_port,
        console_token=console_token,
        busy_policy=busy_policy,
        setting_sources=setting_sources,
        try_resume_first=try_resume_first,
        rules_text=rules_text,
        rules_file=rules_file,
        run_stall_timeout_sec=run_stall_timeout_sec,
        run_tool_stall_timeout_sec=run_tool_stall_timeout_sec,
        stall_auto_continue_max=stall_auto_continue_max,
        stall_auto_continue_prompt=stall_auto_continue_prompt,
        health_check_interval_sec=health_check_interval_sec,
        health_poll_fail_threshold=health_poll_fail_threshold,
        health_quiet_sec=health_quiet_sec,
        health_heartbeat_interval_sec=health_heartbeat_interval_sec,
        health_kickstart_after_soft=health_kickstart_after_soft,
        model_context_windows=model_context_windows,
    )


def _resolve_bot_token(
    b_item: dict,
    *,
    default_token: str,
    bot_name: str,
) -> str:
    """Resolve a bot token from ``token_env``, ``token``, or the default env token.

    Prefer ``token_env`` so secrets stay in ``.env`` instead of ``config.toml``.
    """
    raw_env = b_item.get("token_env")
    if isinstance(raw_env, str) and raw_env.strip():
        env_name = raw_env.strip()
        value = os.environ.get(env_name, "").strip()
        if not value:
            raise SystemExit(
                f"Bot [{bot_name}] token_env={env_name!r} is missing or empty in .env."
            )
        return value

    if "token" in b_item and b_item.get("token") is not None:
        return str(b_item.get("token") or "").strip()

    return default_token


def _load_rules(data: dict, project_root: Path) -> tuple[str, Path | None]:
    """Load bridge-owned rules from ``rules`` string and/or ``rules_file``."""
    parts: list[str] = []
    inline = data.get("rules")
    if isinstance(inline, str) and inline.strip():
        parts.append(inline.strip())

    rules_file: Path | None = None
    raw_path = data.get("rules_file")
    if isinstance(raw_path, str) and raw_path.strip():
        rules_file = _expand(raw_path.strip(), base=project_root)
        if rules_file.is_file():
            parts.append(rules_file.read_text(encoding="utf-8").strip())

    text = "\n\n".join(p for p in parts if p)
    return text, rules_file


def _positive_float(raw: object, *, default: float, minimum: float) -> float:
    try:
        value = float(raw) if raw is not None else default
    except (TypeError, ValueError):
        return default
    if value < minimum:
        return default
    return value


def _positive_int(raw: object, *, default: int, minimum: int) -> int:
    try:
        value = int(raw) if raw is not None else default
    except (TypeError, ValueError):
        return default
    if value < minimum:
        return default
    return value


def _nonneg_int(raw: object, *, default: int) -> int:
    try:
        value = int(raw) if raw is not None else default
    except (TypeError, ValueError):
        return default
    return max(0, value)


def _parse_setting_sources(raw: object) -> list[str]:
    """Parse ``setting_sources`` from config.toml.

    Default (key omitted): ``["user", "project"]`` so on-disk ``.cursor/`` and
    ``~/.cursor/`` configs apply. Explicit empty list disables ambient settings.

    Note: Cursor Customize → User Rules are cloud/IDE-synced and are **not**
    loaded by this option — use ``rules_file`` / ``rules`` for those.
    """
    if raw is None:
        return ["user", "project"]
    if isinstance(raw, str):
        items = [raw]
    elif isinstance(raw, list):
        items = [str(x) for x in raw]
    else:
        return ["user", "project"]
    out: list[str] = []
    for item in items:
        val = item.strip().lower()
        if not val:
            continue
        if val not in _SETTING_SOURCES:
            continue
        if val not in out:
            out.append(val)
    return out


def _parse_model_context_windows(raw: object) -> dict[str, int]:
    """Parse ``[model_context_windows]`` — display-only denominators for UI."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    for key, value in raw.items():
        name = str(key).strip().lower()
        if not name:
            continue
        try:
            window = int(value)
        except (TypeError, ValueError):
            continue
        if window <= 0:
            continue
        out[name] = window
    return out


def _parse_permission(raw: object) -> str:
    if raw is None:
        return "full"
    val = str(raw).strip().lower()
    if val in _PERMISSIONS:
        return val
    allowed = ", ".join(sorted(_PERMISSIONS))
    raise SystemExit(
        f"Invalid bot permission {raw!r}. Use one of: {allowed}."
    )


def _parse_int_list(raw: object) -> list[int]:
    if not isinstance(raw, list):
        return []
    out: list[int] = []
    for item in raw:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return out
