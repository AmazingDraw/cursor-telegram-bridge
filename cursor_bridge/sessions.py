from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

from cursor_sdk import (
    AgentOptions,
    AsyncClient,
    LocalAgentOptions,
    ModelParameterValue,
    ModelSelection,
    SendOptions,
    UserMessage,
)
from cursor_sdk.errors import AgentBusyError, InternalServerError

from .attachments import (
    collect_run_attachments,
    deliver_generate_image_live,
    tool_auto_sends,
)
from .config import BotConfig, Config
from .context import (
    ContextInfo,
    build_context_restore_prompt,
    build_prior_context_markdown,
    condense_transcript,
    find_session_transcript,
    get_context_info,
    list_prior_agents,
    prior_context_path,
    PriorAgentInfo,
    resolve_context_window,
    resolve_prior_agent,
)
from .events import SessionEventLog
from .state_layout import bot_name_for, bot_state_dir, migrate_legacy_default_state
from .formatting import (
    LIVE_TIMER_INTERVAL_SEC,
    apply_plan_to_text_parts,
    build_live_html,
    format_sdk_status_activity,
    format_tool_activity,
    format_tool_error_snippet,
    format_tool_result_snippet,
    is_create_plan_tool,
    resolve_final_body,
)
from .models import (
    EFFORT_PARAM,
    PROBLEMATIC_MODELS,
    RECOMMENDED_MODEL,
    format_model_display,
    instant_empty_user_message,
)
from .rules import strip_rules_prefix
from .telegram_delivery import strip_telegram_delivery_prefix
from .permission_guard import evaluate_tool_call

logger = logging.getLogger("cursor_bridge.sessions")

# ``AsyncClient.launch_bridge`` does not expose a subprocess cwd parameter, so
# _get_bridge temporarily wraps asyncio.create_subprocess_exec. That symbol is
# process-global: serialize the wrapper across all bot SessionManagers.
_PROCESS_BRIDGE_SPAWN_LOCK = threading.Lock()


async def _acquire_process_bridge_spawn_lock() -> None:
    while not _PROCESS_BRIDGE_SPAWN_LOCK.acquire(blocking=False):
        await asyncio.sleep(0.01)


class SessionBusyError(Exception):
    """Raised when a second prompt tries to start while a session run is active."""

STATUS_IDLE = "idle"
STATUS_RUNNING = "running"
STATUS_ERROR = "error"
STATUS_GLITCH = "glitch"

MODE_AGENT = "agent"
MODE_PLAN = "plan"
MODES = (MODE_AGENT, MODE_PLAN)

# Max follow-up prompts kept per session while one run is in flight (queue policy).
MAX_PROMPT_QUEUE = 20

# How many times to try a prompt when the Cursor bridge dies (send or mid-stream).
BRIDGE_RUN_ATTEMPTS = 3

# After hung-run watchdog abort: auto-send this once (same as user typing 继续).
STALL_AUTO_CONTINUE_PROMPT = "继续"
STALL_AUTO_CONTINUE_MAX = 1
# Mid-stream bridge drop after a successful agent resume: do not re-send the
# original user prompt (would duplicate tool work). Same continue cue as stall.
BRIDGE_RESUME_CONTINUE_PROMPT = STALL_AUTO_CONTINUE_PROMPT

# Default stall watchdog (overridden by Config.run_stall_timeout_sec).
DEFAULT_RUN_STALL_TIMEOUT_SEC = 300.0
# Longer allowance while a tool call is in flight (no new events).
DEFAULT_RUN_TOOL_STALL_TIMEOUT_SEC = 600.0
# While consuming SDK events: wake periodically so a stalled+cancelled run
# cannot hang forever on a silent async iterator.
EVENT_POLL_TIMEOUT_SEC = 20.0
# After stall/tool cancel: bound how long we wait for run.wait() to finish.
POST_CANCEL_WAIT_TIMEOUT_SEC = 45.0


async def _iter_events_until_stopped(
    event_iter: Any,
    *,
    should_stop: Callable[[], bool],
    poll_timeout: float = EVENT_POLL_TIMEOUT_SEC,
) -> AsyncIterator[Any]:
    """Poll one persistent ``anext`` task until an event or stop condition.

    ``asyncio.wait_for(event_iter.__anext__(), timeout)`` cancels ``__anext__``
    on every quiet interval. For the Cursor SDK that cancellation closes the
    underlying async generator, silently ending all subsequent live events.
    Reusing one task preserves the stream; it is cancelled only when the run is
    deliberately stopping.
    """
    pending: asyncio.Task | None = None
    try:
        while not should_stop():
            if pending is None:
                pending = asyncio.create_task(event_iter.__anext__())
            done, _ = await asyncio.wait({pending}, timeout=poll_timeout)
            if not done:
                continue

            completed = pending
            pending = None
            try:
                event = completed.result()
            except StopAsyncIteration:
                return
            yield event
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


def _final_with_partial(text_parts: list[str], message: str) -> str:
    """Keep streamed assistant text when appending a stall/abort notice."""
    partial = "".join(text_parts).strip()
    return f"{partial}\n\n---\n\n{message}" if partial else message


# Async callback the bot passes in to receive live transcript updates (force=True bypasses throttle).
UpdateCb = Callable[..., Awaitable[None]]
AttachmentCb = Callable[[Path], Awaitable[bool]]


@dataclass
class QueuedPrompt:
    """A user prompt waiting to run after the current session run finishes."""

    prompt: str | UserMessage
    chat_id: int
    thinking_label: str = "\U0001F4AD thinking\u2026"
    log_kind: str = "Prompt"
    token: str = field(default_factory=lambda: secrets.token_hex(4))
    # False until the user taps 排队 / 发送 on the ack keyboard — drain must skip.
    confirmed: bool = True


@dataclass
class Session:
    short_id: str
    cwd: str
    agent_id: Optional[str] = None
    status: str = STATUS_IDLE
    model: str = ""
    mode: str = MODE_AGENT
    model_params: dict[str, str] = field(default_factory=dict)
    last_prompt: str = ""
    last_activity: float = field(default_factory=time.time)
    run_id: Optional[str] = None
    prior_agent_ids: list[str] = field(default_factory=list)
    context_restored_from: Optional[str] = None
    # Set when a recreate dropped prior context but no completed run has
    # surfaced the /context hint yet (e.g. the run was cancelled mid-way).
    context_lost_notice_pending: bool = False
    custom_name: Optional[str] = None
    # Runtime-only handles (never persisted).
    agent: object = field(default=None, repr=False)
    run: object = field(default=None, repr=False)

    @property
    def name(self) -> str:
        if self.custom_name and self.custom_name.strip():
            return self.custom_name.strip()
        return Path(self.cwd).name or self.cwd

    def to_dict(self) -> dict:
        return {
            "short_id": self.short_id,
            "cwd": self.cwd,
            "custom_name": self.custom_name,
            "agent_id": self.agent_id,
            "status": self.status,
            "model": self.model,
            "mode": self.mode,
            "model_params": dict(self.model_params),
            "last_prompt": self.last_prompt,
            "last_activity": self.last_activity,
            "prior_agent_ids": list(self.prior_agent_ids),
            "context_restored_from": self.context_restored_from,
            "context_lost_notice_pending": self.context_lost_notice_pending,
        }


def _build_live(
    s: Session,
    text_parts: list[str],
    live: dict[str, object],
    *,
    elapsed: int | None = None,
) -> str:
    if elapsed is not None:
        live["elapsed"] = elapsed
    return build_live_html(
        s.short_id,
        s.name,
        s.model,
        text_parts,
        str(live["activity"]),
        str(live["snippet"]),
        elapsed=live.get("elapsed") if isinstance(live.get("elapsed"), int) else None,
    )


def _is_bridge_down(exc: BaseException) -> bool:
    """True when the error means the bridge subprocess is unreachable."""
    text = str(exc).lower()
    return (
        "connecterror" in text
        or "all connection attempts failed" in text
        or "bridge request failed" in text
        or "connection refused" in text
        or "connection reset" in text
        or "remoteprotocolerror" in text
        or "incomplete chunked" in text
    )


def _is_stuck_agent(exc: BaseException) -> bool:
    """True when resume/send failed because the agent handle is corrupted or busy."""
    if isinstance(exc, (InternalServerError, AgentBusyError)):
        return True
    text = str(exc).lower()
    # Avoid bare "internal" — matches unrelated errors (e.g. "internal timeout").
    return (
        "internal server error" in text
        or "internal error" in text
        or "agent busy" in text
        or "agent_busy" in text
    )


class SessionManager:
    """Owns the Cursor SDK bridge plus the registry of live agent sessions."""

    def __init__(self, cfg: Config, bot_cfg: BotConfig | None = None):
        self.cfg = cfg
        self.bot_cfg = bot_cfg
        # One-time: lift legacy state/sessions.json into state/bots/default/.
        migrate_legacy_default_state(cfg)
        self.state_dir = bot_state_dir(cfg, bot_name_for(bot_cfg))

        self.default_model = bot_cfg.model if bot_cfg else cfg.model
        self.allowed_models = bot_cfg.models if bot_cfg else cfg.models
        self.default_effort = bot_cfg.effort if bot_cfg else cfg.effort
        self._try_resume_first = bool(getattr(cfg, "try_resume_first", True))

        self._bridges: dict[str, tuple[AsyncClient, object]] = {}
        self._bridge_lock = asyncio.Lock()
        self._bridge_recovery_locks: dict[str, asyncio.Lock] = {}
        self._bridge_stderr_tasks: dict[str, asyncio.Task] = {}
        self._background_tasks: set[asyncio.Task] = set()
        self._stopping = False
        self.sessions: dict[str, Session] = {}
        self.active: dict[int, str] = {}  # telegram chat_id -> session short_id
        self._counter = 0
        self.event_log = SessionEventLog(self.state_dir, max_events=cfg.event_log_max)
        self._sdk_models_cache: tuple[float, list[Any]] | None = None
        self._sdk_models_cache_ttl = 300.0
        self._run_locks: dict[str, asyncio.Lock] = {}
        self._agent_locks: dict[str, asyncio.Lock] = {}
        self._run_tasks: dict[str, asyncio.Task] = {}
        # Bumped on each depth-0 run start and on force-unstick so a stale
        # run_prompt finally cannot clear a successor's lock/task/run.
        self._run_generations: dict[str, int] = {}
        # Sid set while a depth-0 run_prompt is between busy-check and lock.acquire
        # (closes the locked()/await acquire TOCTOU window).
        self._run_lock_claiming: set[str] = set()
        self._prompt_queues: dict[str, deque[QueuedPrompt]] = {}
        self._load()

    @property
    def sessions_file(self) -> Path:
        return self.state_dir / "sessions.json"

    def _run_lock(self, sid: str) -> asyncio.Lock:
        lock = self._run_locks.get(sid)
        if lock is None:
            lock = asyncio.Lock()
            self._run_locks[sid] = lock
        return lock

    def _bump_run_generation(self, sid: str) -> int:
        nxt = self._run_generations.get(sid, 0) + 1
        self._run_generations[sid] = nxt
        return nxt

    def _agent_lock(self, sid: str) -> asyncio.Lock:
        """Serialize resume/recreate for one session (vs background resume)."""
        lock = self._agent_locks.get(sid)
        if lock is None:
            lock = asyncio.Lock()
            self._agent_locks[sid] = lock
        return lock

    def _bridge_recovery_lock(self, cwd: str) -> asyncio.Lock:
        key = self._norm(cwd)
        lock = self._bridge_recovery_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._bridge_recovery_locks[key] = lock
        return lock

    def _track_background_task(self, task: asyncio.Task) -> None:
        self._background_tasks.add(task)

        def _done(completed: asyncio.Task) -> None:
            self._background_tasks.discard(completed)
            if completed.cancelled():
                return
            try:
                exc = completed.exception()
            except asyncio.CancelledError:
                return
            if exc is not None:
                logger.warning("Session background task failed: %s", exc, exc_info=exc)

        task.add_done_callback(_done)

    @staticmethod
    def _norm(cwd: str) -> str:
        return os.path.realpath(os.path.expanduser(cwd))

    async def _get_bridge(self, cwd: str) -> AsyncClient:
        """Return (launching if needed) the bridge rooted at ``cwd``."""
        if self._stopping:
            raise RuntimeError("Session manager is stopping; bridge launch refused")
        key = self._norm(cwd)
        async with self._bridge_lock:
            if self._stopping:
                raise RuntimeError("Session manager is stopping; bridge launch refused")
            existing = self._bridges.get(key)
            if existing is not None:
                return existing[0]
            # SDK bridge inherits the parent process cwd at spawn time, but
            # os.chdir() is process-global and unsafe across awaits. Inject cwd=
            # into the subprocess spawn instead.
            await _acquire_process_bridge_spawn_lock()
            real_exec = asyncio.create_subprocess_exec

            async def _exec_with_cwd(*args: Any, **kwargs: Any):
                kwargs.setdefault("cwd", key)
                return await real_exec(*args, **kwargs)

            asyncio.create_subprocess_exec = _exec_with_cwd  # type: ignore[assignment]
            try:
                client = await AsyncClient.launch_bridge(workspace=key)
                if self._stopping:
                    await client.__aexit__(None, None, None)
                    raise RuntimeError("Session manager stopped during bridge launch")
                # Client owns the bridge; __aexit__/aclose terminates it.
                self._bridges[key] = (client, client)
                self._start_bridge_stderr_drain(key, client)
                logger.info("Launched bridge for %s", key)
                return client
            except Exception as exc:  # noqa: BLE001
                msg = str(exc).lower()
                if "auth-token" in msg or "unknown argument" in msg:
                    logger.error(
                        "cursor-sdk-bridge binary rejected SDK args (%s). "
                        "This is usually a version mismatch between the "
                        "installed `cursor-sdk` Python package and its "
                        "vendored bridge binary — reinstall/upgrade "
                        "cursor-sdk to fix.", exc,
                    )
                raise
            finally:
                asyncio.create_subprocess_exec = real_exec  # type: ignore[assignment]
                _PROCESS_BRIDGE_SPAWN_LOCK.release()

    async def _drain_bridge_stderr(self, key: str, stream: Any) -> None:
        """Continuously drain bridge stderr so the child cannot block on a full pipe."""
        while True:
            line = await stream.readline()
            if not line:
                return
            text = line.decode(errors="replace").rstrip()
            if text:
                logger.debug("Bridge[%s] stderr: %s", Path(key).name, text)

    def _start_bridge_stderr_drain(self, key: str, client: AsyncClient) -> None:
        bridge = getattr(client, "_owned_bridge", None)
        process = getattr(bridge, "process", None)
        stream = getattr(process, "stderr", None)
        if stream is None:
            return
        task = asyncio.create_task(
            self._drain_bridge_stderr(key, stream),
            name=f"bridge-stderr-{Path(key).name}",
        )
        self._bridge_stderr_tasks[key] = task

        def _done(completed: asyncio.Task) -> None:
            if self._bridge_stderr_tasks.get(key) is completed:
                self._bridge_stderr_tasks.pop(key, None)
            if completed.cancelled():
                return
            try:
                exc = completed.exception()
            except asyncio.CancelledError:
                return
            if exc is not None:
                logger.debug("Bridge stderr drain ended with error", exc_info=exc)

        task.add_done_callback(_done)

    async def _close_bridge_entry(
        self,
        key: str,
        entry: tuple[AsyncClient, object],
        stderr_task: asyncio.Task | None = None,
    ) -> None:
        try:
            await asyncio.wait_for(
                entry[1].__aexit__(None, None, None),  # type: ignore[attr-defined]
                timeout=15.0,
            )
        except asyncio.TimeoutError:
            logger.warning("Timed out closing bridge for %s", key)
        except Exception:  # noqa: BLE001
            logger.debug("Bridge close failed for %s", key, exc_info=True)
        if stderr_task is not None and not stderr_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(stderr_task), timeout=1.0)
            except asyncio.TimeoutError:
                stderr_task.cancel()
                await asyncio.gather(stderr_task, return_exceptions=True)

    async def _control_client(self) -> AsyncClient:
        """Dedicated bridge for folder-independent API calls (models, usage, …).

        Never borrow a session bridge — those can block for the whole run while
        the agent is working, which made /effort and /model appear to do nothing.
        """
        return await self._get_bridge(str(self.cfg.state_dir))

    async def _maybe_close_bridge(self, cwd: str) -> None:
        key = self._norm(cwd)
        async with self._bridge_lock:
            if any(self._norm(s.cwd) == key for s in self.sessions.values()):
                return
            entry = self._bridges.pop(key, None)
            stderr_task = self._bridge_stderr_tasks.pop(key, None)
        if entry is not None:
            await self._close_bridge_entry(key, entry, stderr_task)
            logger.info("Closed bridge for %s", key)

    # ---- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Load the session registry. Agent resume is done separately (see resume_sessions).

        Resume launches Cursor bridges and can block for a long time — Telegram
        polling must not wait on it.
        """
        self._stopping = False
        path = self.sessions_file
        should_recover = not path.exists()
        if not should_recover:
            try:
                data = json.loads(path.read_text())
                should_recover = not data.get("sessions")
            except (json.JSONDecodeError, OSError):
                should_recover = True

        if should_recover:
            logger.info("No saved sessions found; starting with an empty registry.")
        self._load()
        self._run_locks.clear()
        self._agent_locks.clear()
        self._run_tasks.clear()
        self._run_generations.clear()
        self._run_lock_claiming.clear()
        self._prompt_queues.clear()
        for s in list(self.sessions.values()):
            # Stale "running" from a crash/reload — no run is active until a new prompt starts.
            if s.status == STATUS_RUNNING:
                s.status = STATUS_IDLE
        self._persist()

    async def resume_sessions(self) -> None:
        """Re-attach Cursor agents for persisted sessions (may take a while)."""
        for s in list(self.sessions.values()):
            if self._stopping:
                return
            try:
                async with self._agent_lock(s.short_id):
                    await self._resume(s)
            except Exception as exc:  # noqa: BLE001 - degrade gracefully
                logger.warning("Could not resume session %s (%s): %s", s.short_id, s.cwd, exc)
                s.status = STATUS_ERROR
        self._persist()

    async def stop(self) -> None:
        self._stopping = True
        current = asyncio.current_task()
        tasks = set(self._background_tasks)
        tasks.update(self._run_tasks.values())
        tasks.discard(current)
        live_tasks = [task for task in tasks if not task.done()]
        for task in live_tasks:
            task.cancel()
        if live_tasks:
            await asyncio.gather(*live_tasks, return_exceptions=True)
        self._background_tasks.clear()

        for s in self.sessions.values():
            try:
                await asyncio.wait_for(self._close_agent(s), timeout=10.0)
            except asyncio.TimeoutError:
                logger.warning("Timed out closing agent for session %s", s.short_id)

        async with self._bridge_lock:
            entries = list(self._bridges.items())
            self._bridges.clear()
            stderr_tasks = dict(self._bridge_stderr_tasks)
            self._bridge_stderr_tasks.clear()
        if entries:
            await asyncio.gather(
                *(
                    self._close_bridge_entry(key, entry, stderr_tasks.get(key))
                    for key, entry in entries
                ),
                return_exceptions=True,
            )

    async def _close_agent(self, s: Session) -> None:
        if s.agent is not None:
            try:
                await s.agent.close()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
            s.agent = None

    # ---- session creation / resume ----------------------------------------

    def _local_options(self, cwd: str) -> LocalAgentOptions:
        """Local agent options, including Cursor User Rules / project rules."""
        sources = list(self.cfg.setting_sources)
        return LocalAgentOptions(
            cwd=cwd,
            setting_sources=sources or None,
        )

    async def create_session(self, cwd: str) -> Session:
        client = await self._get_bridge(cwd)
        self._counter += 1
        sid = f"s{self._counter}"
        model = self.default_model or self.cfg.model
        s = Session(
            short_id=sid,
            cwd=cwd,
            agent_id=None,
            model=model,
            mode=MODE_AGENT,
            agent=None,
        )
        await self.apply_default_effort(s)
        agent = await client.agents.create(
            AgentOptions(
                model=self.model_for_sdk(s),
                api_key=self.cfg.cursor_api_key,
                mode=MODE_AGENT,
                local=self._local_options(cwd),
            ),
        )
        s.agent = agent
        s.agent_id = getattr(agent, "agent_id", None)
        self.sessions[sid] = s
        self._persist()
        self.log_session_event(sid, "session_created", cwd=cwd, agent_id=s.agent_id)
        return s

    async def _resume(self, s: Session) -> None:
        if not s.agent_id:
            return
        if s.agent is not None:
            return
        client = await self._get_bridge(s.cwd)
        agent = await client.agents.resume(
            s.agent_id,
            AgentOptions(
                api_key=self.cfg.cursor_api_key,
                local=self._local_options(s.cwd),
            ),
        )
        s.agent = agent
        if s.status == STATUS_RUNNING:
            s.status = STATUS_IDLE
        logger.info("Resumed session %s (agent %s)", s.short_id, s.agent_id)

    async def _ensure_agent(self, s: Session) -> bool:
        """Make sure ``s.agent`` is usable. Returns True if a new agent was created."""
        async with self._agent_lock(s.short_id):
            if s.agent is None:
                await self._resume(s)
            if s.agent is None:
                await self._recreate_agent(s)
                return True
            return False

    async def _recreate_agent(self, s: Session) -> None:
        """Replace a stuck agent with a fresh one (conversation context is lost).

        Caller must hold ``_agent_lock(s.short_id)`` when concurrent resume is possible,
        or call via ``_ensure_agent`` / locked recovery paths.
        """
        old_agent_id = s.agent_id
        await self._close_agent(s)
        client = await self._get_bridge(s.cwd)
        agent = await client.agents.create(
            AgentOptions(
                model=self.model_for_sdk(s),
                api_key=self.cfg.cursor_api_key,
                mode=s.mode,
                local=self._local_options(s.cwd),
            ),
        )
        s.agent = agent
        if old_agent_id and old_agent_id not in s.prior_agent_ids:
            s.prior_agent_ids.append(old_agent_id)
        if old_agent_id:
            # Context is objectively lost from this moment — mark it so the
            # /context hint survives cancels/retries until a run surfaces it.
            s.context_lost_notice_pending = True
        s.agent_id = getattr(agent, "agent_id", None)
        s.status = STATUS_IDLE
        s.run = None
        s.run_id = None
        s.context_restored_from = None
        self._persist()
        self.log_session_event(
            s.short_id,
            "agent_recreated",
            old_agent_id=old_agent_id,
            new_agent_id=s.agent_id,
        )
        logger.warning(
            "Recreated agent for session %s (new agent %s)",
            s.short_id,
            s.agent_id,
        )

    def log_session_event(self, sid: str, event: str, **data: Any) -> None:
        self.event_log.append(sid, event, **data)
        self.event_log.append_audit(event, sid=sid, **data)
        if data:
            tail = "  ".join(f"{k}={v}" for k, v in data.items())
            logger.info("Session %s  %s  %s", sid, event, tail)
        else:
            logger.info("Session %s  %s", sid, event)

    async def end_session(self, sid: str, **audit: Any) -> bool:
        s = self.sessions.get(sid)
        if s is None:
            return False
        await self.cancel(s)
        self.clear_prompt_queue(sid)
        s = self.sessions.pop(sid, None)
        if s is None:
            return False
        await self._close_agent(s)
        for chat_id, active_sid in list(self.active.items()):
            if active_sid == sid:
                del self.active[chat_id]
        await self._maybe_close_bridge(s.cwd)
        self._persist()
        payload = {
            "cwd": s.cwd,
            "agent_id": s.agent_id,
            "status": s.status,
            "model": s.model,
            "mode": s.mode,
            "last_prompt": (s.last_prompt or "")[:200],
            **audit,
        }
        self.log_session_event(sid, "session_ended", **payload)
        return True

    # ---- active session per chat ------------------------------------------

    def set_active(self, chat_id: int, sid: str) -> None:
        self.active[chat_id] = sid
        self._persist()

    def get_active(self, chat_id: int | str) -> Optional[Session]:
        try:
            chat_key = int(chat_id)
        except (TypeError, ValueError):
            return None
        sid = self.active.get(chat_key)
        if sid and sid in self.sessions:
            return self.sessions[sid]
        if sid:
            del self.active[chat_key]
            self._persist()
        return None

    def is_busy(self, s: Session) -> bool:
        """True when a prompt run is in flight for this session."""
        if s.status == STATUS_RUNNING:
            return True
        if s.short_id in self._run_lock_claiming:
            return True
        task = self._run_tasks.get(s.short_id)
        if task is not None and not task.done():
            return True
        return self._run_lock(s.short_id).locked()

    def enqueue_prompt(self, sid: str, item: QueuedPrompt) -> int:
        """Append a follow-up prompt. Returns queue length after enqueue.

        Raises ``ValueError`` if the queue is full.
        """
        q = self._prompt_queues.setdefault(sid, deque())
        if len(q) >= MAX_PROMPT_QUEUE:
            raise ValueError(f"Queue full (max {MAX_PROMPT_QUEUE}).")
        q.append(item)
        return len(q)

    def push_front_prompt(self, sid: str, item: QueuedPrompt) -> None:
        self._prompt_queues.setdefault(sid, deque()).appendleft(item)

    def peek_queued_prompt(self, sid: str) -> QueuedPrompt | None:
        q = self._prompt_queues.get(sid)
        if not q:
            return None
        return q[0]

    def pop_queued_prompt(self, sid: str) -> QueuedPrompt | None:
        q = self._prompt_queues.get(sid)
        if not q:
            return None
        item = q.popleft()
        if not q:
            self._prompt_queues.pop(sid, None)
        return item

    def pop_first_confirmed_prompt(self, sid: str) -> QueuedPrompt | None:
        """Remove and return the first confirmed item; leave unconfirmed heads in place."""
        q = self._prompt_queues.get(sid)
        if not q:
            return None
        kept: deque[QueuedPrompt] = deque()
        found: QueuedPrompt | None = None
        for item in q:
            if found is None and item.confirmed:
                found = item
                continue
            kept.append(item)
        if found is None:
            return None
        if kept:
            self._prompt_queues[sid] = kept
        else:
            self._prompt_queues.pop(sid, None)
        return found

    def confirm_queued_by_token(self, sid: str, token: str) -> QueuedPrompt | None:
        """Mark a queued item confirmed so drain may run it. Returns the item or None."""
        item = self.find_queued_by_token(sid, token)
        if item is None:
            return None
        item.confirmed = True
        return item

    def clear_prompt_queue(self, sid: str) -> int:
        q = self._prompt_queues.pop(sid, None)
        return len(q) if q else 0

    def find_queued_by_token(self, sid: str, token: str) -> QueuedPrompt | None:
        q = self._prompt_queues.get(sid)
        if not q:
            return None
        for item in q:
            if item.token == token:
                return item
        return None

    def remove_queued_by_token(self, sid: str, token: str) -> QueuedPrompt | None:
        """Remove one queued item by token. Returns the item, or None if missing."""
        q = self._prompt_queues.get(sid)
        if not q:
            return None
        kept: deque[QueuedPrompt] = deque()
        found: QueuedPrompt | None = None
        for item in q:
            if found is None and item.token == token:
                found = item
                continue
            kept.append(item)
        if found is None:
            return None
        if kept:
            self._prompt_queues[sid] = kept
        else:
            self._prompt_queues.pop(sid, None)
        return found

    def queued_count(self, sid: str) -> int:
        q = self._prompt_queues.get(sid)
        return len(q) if q else 0

    async def _sdk_models(self) -> list[Any]:
        now = time.time()
        if (
            self._sdk_models_cache is not None
            and now - self._sdk_models_cache[0] < self._sdk_models_cache_ttl
        ):
            return self._sdk_models_cache[1]
        client = await self._control_client()
        models = await client.models.list(api_key=self.cfg.cursor_api_key)
        self._sdk_models_cache = (now, models)
        return models

    @staticmethod
    def _model_def(models: list[Any], model_id: str) -> Any | None:
        for model in models:
            if getattr(model, "id", None) == model_id:
                return model
        return None

    def _desired_effort(self) -> str | None:
        raw = self.default_effort or self.cfg.effort
        if not raw:
            return None
        value = str(raw).strip().lower()
        return value or None

    def model_for_sdk(self, s: Session) -> ModelSelection:
        params_dict = dict(s.model_params)
        if "fast" not in params_dict:
            params_dict["fast"] = "false"
        # Config default effort must reach the SDK even when session state
        # predates effort support or /model cleared model_params.
        desired = self._desired_effort()
        has_effort = any(k in params_dict for k in ("effort", "reasoning", "thinking"))
        if desired and not has_effort:
            params_dict[EFFORT_PARAM] = desired
        params = [
            ModelParameterValue(id=key, value=value)
            for key, value in sorted(params_dict.items())
        ]
        return ModelSelection(id=s.model, params=params)

    async def list_models(self) -> list[str]:
        models = await self._sdk_models()
        all_ids = [m.id for m in models]
        allowed = self.allowed_models or self.cfg.models
        if allowed:
            allowed_set = {m_id.lower() for m_id in allowed}
            filtered = [m_id for m_id in all_ids if m_id.lower() in allowed_set]
            return filtered if filtered else allowed
        return all_ids

    async def effort_param_and_options(self, s: Session) -> tuple[str, list[str]] | None:
        """Get (param_id, allowed_values) for the session model, or None if unsupported."""
        models = await self._sdk_models()
        model_def = self._model_def(models, s.model)
        if model_def is None:
            return None
        for param in getattr(model_def, "parameters", ()) or ():
            param_id = getattr(param, "id", None)
            if param_id in ("effort", "reasoning", "thinking"):
                values = [v.value for v in getattr(param, "values", ()) or ()]
                return (param_id, values)
        return None

    async def effort_options(self, s: Session) -> list[str] | None:
        """Allowed effort values for the session model, or None if unsupported."""
        res = await self.effort_param_and_options(s)
        return res[1] if res else None

    async def set_effort(self, s: Session, value: str) -> str | None:
        res = await self.effort_param_and_options(s)
        if not res:
            return None
        param_id, options = res
        normalized = value.strip().lower()
        if normalized not in options:
            return None
        for k in ("effort", "reasoning", "thinking"):
            s.model_params.pop(k, None)
        s.model_params[param_id] = normalized
        s.last_activity = time.time()
        self._persist()
        return normalized

    async def apply_default_effort(self, s: Session) -> str | None:
        """Apply config default effort to a session (create / model switch).

        Prefers the SDK-reported param id/values. If the model catalog is
        unavailable or the model has no effort param yet, still seed
        ``effort=<default>`` so SendOptions / create carry the configured level.
        """
        desired = self._desired_effort()
        if not desired:
            return None
        normalized = await self.set_effort(s, desired)
        if normalized:
            return normalized
        # Fallback: persist configured value under the common param id.
        for k in ("effort", "reasoning", "thinking"):
            s.model_params.pop(k, None)
        s.model_params[EFFORT_PARAM] = desired
        s.last_activity = time.time()
        self._persist()
        return desired

    async def set_model(self, s: Session, model: str) -> None:
        s.model = model.strip()
        s.model_params.clear()
        await self.apply_default_effort(s)
        s.last_activity = time.time()
        self._persist()

    def set_mode(self, s: Session, mode: str) -> str | None:
        normalized = mode.strip().lower()
        if normalized not in MODES:
            return None
        s.mode = normalized
        s.last_activity = time.time()
        self._persist()
        return normalized

    def rename_session(self, s: Session, name: str | None) -> str:
        cleaned = (name or "").strip()
        s.custom_name = cleaned if cleaned else None
        s.last_activity = time.time()
        self._persist()
        return s.name

    def session_context(self, s: Session) -> ContextInfo | None:
        window = resolve_context_window(s.model, self.cfg.model_context_windows)
        return get_context_info(s.agent_id, s.cwd, window=window)

    def _backfill_prior_agents(self, s: Session) -> None:
        """Recover prior agent ids from this session's event log only."""
        changed = False
        for row in self.event_log.read(s.short_id, limit=500):
            if row.get("event") != "agent_recreated":
                continue
            old_id = row.get("old_agent_id")
            if (
                isinstance(old_id, str)
                and old_id
                and old_id != s.agent_id
                and old_id not in s.prior_agent_ids
            ):
                s.prior_agent_ids.append(old_id)
                changed = True
        if changed:
            self._persist()

    def prior_agents_for_session(self, s: Session) -> list[PriorAgentInfo]:
        self._backfill_prior_agents(s)
        return list_prior_agents(
            s.prior_agent_ids,
            s.cwd,
            exclude_agent_id=s.agent_id,
        )

    def can_restore_context(self, s: Session) -> bool:
        """True when at least one prior agent still has a local transcript."""
        self._backfill_prior_agents(s)
        return (
            resolve_prior_agent(
                s.prior_agent_ids,
                s.cwd,
                None,
                exclude_agent_id=s.agent_id,
            )
            is not None
        )

    def _context_lost_note(self, s: Session, recreated: bool) -> str:
        """/context hint to prepend when this run lost prior context ("" if none).

        Sticky: a recreate marks ``context_lost_notice_pending`` at the moment
        context is dropped, so the hint still surfaces on the next completed
        run even when the recreating run was cancelled or retried. The flag is
        cleared (and persisted) once the note is actually emitted here.
        """
        if not (recreated or s.context_lost_notice_pending):
            return ""
        if self.can_restore_context(s):
            note = (
                "(Session was reset after a stuck agent — prior chat context was lost. "
                "Send /context to restore it for this session only.)\n\n"
            )
        else:
            note = (
                "(Session was reset after a stuck agent — prior chat context was lost. "
                "No recoverable transcript found; please restate your task.)\n\n"
            )
        if s.context_lost_notice_pending:
            s.context_lost_notice_pending = False
            self._persist()
        return note

    def prepare_context_restore(
        self,
        s: Session,
        *,
        agent_id: str | None = None,
        force: bool = False,
    ) -> tuple[str, str, str]:
        """Write a session-scoped prior-context file. Returns (prompt, summary, agent_id)."""
        self._backfill_prior_agents(s)
        chosen = resolve_prior_agent(
            s.prior_agent_ids,
            s.cwd,
            agent_id,
            exclude_agent_id=s.agent_id,
        )
        if chosen is None:
            if agent_id:
                raise ValueError(
                    f"Agent `{agent_id}` is not in this session's prior history "
                    f"for this project folder.",
                )
            tracked = [
                aid for aid in s.prior_agent_ids
                if aid and aid != s.agent_id
            ]
            if not tracked:
                raise ValueError(
                    "No prior agents tracked for this session. "
                    "Context restore only works after an agent reset in this session.",
                )
            raise ValueError(
                f"This session has {len(tracked)} prior agent id(s), but no "
                "transcript file was found for this project folder — cannot restore. "
                "Please restate your task instead of /context.",
            )
        if not force and s.context_restored_from == chosen:
            raise ValueError(
                f"Context from `{chosen}` was already restored for this session. "
                "Use `/context refresh` to reload it.",
            )

        path = find_session_transcript(chosen, s.cwd)
        if path is None:
            raise ValueError(
                f"Transcript for `{chosen}` was not found under this session's project folder.",
            )

        condensed = condense_transcript(path)
        out_path = prior_context_path(s.cwd, s.short_id)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            build_prior_context_markdown(
                session_id=s.short_id,
                cwd=s.cwd,
                agent_id=chosen,
                condensed=condensed,
            ),
            encoding="utf-8",
        )
        self.log_session_event(
            s.short_id,
            "context_restore_prepared",
            agent_id=chosen,
            user_turns=condensed.user_turns,
            chars=condensed.chars,
        )
        summary = (
            f"Prepared prior context from `{chosen}` "
            f"({condensed.user_turns} user turns, ~{condensed.chars // 4} tokens)."
        )
        if s.context_lost_notice_pending:
            # Restore supersedes the pending loss hint.
            s.context_lost_notice_pending = False
            self._persist()
        return build_context_restore_prompt(s.short_id, s.cwd), summary, chosen

    # ---- running prompts ---------------------------------------------------

    async def _drop_bridge(
        self,
        cwd: str,
        *,
        expected_client: AsyncClient | None = None,
    ) -> bool:
        """Close one failed bridge generation and invalidate all peer agents.

        ``expected_client`` is a compare-and-swap guard: a concurrent recovery
        must never remove the fresh bridge installed by an earlier waiter.
        """
        key = self._norm(cwd)
        # Every agent bound to the failed client is stale, including sessions
        # other than the one that first observed the disconnect.
        for peer in self.sessions.values():
            if self._norm(peer.cwd) != key or peer.agent is None:
                continue
            peer_client = getattr(peer.agent, "client", None)
            if expected_client is None or peer_client is expected_client:
                peer.agent = None

        async with self._bridge_lock:
            current = self._bridges.get(key)
            if current is None:
                return False
            if expected_client is not None and current[0] is not expected_client:
                return False
            entry = self._bridges.pop(key)
            stderr_task = self._bridge_stderr_tasks.pop(key, None)
        if entry is not None:
            await self._close_bridge_entry(key, entry, stderr_task)
            return True
        return False

    async def _recover_bridge_session(self, s: Session, exc: BaseException) -> str:
        """Drop a dead bridge, then resume the same agent (or recreate as fallback).

        Mid-stream bridge crashes previously always recreated the agent, losing
        the conversation context. When ``try_resume_first`` is enabled (default)
        we first try to resume the old agent on the fresh bridge; only if that
        fails do we fall back to creating a new agent.

        Returns ``\"resumed\"`` or ``\"recreated\"`` so callers can choose whether
        to re-send the original prompt or a short continue cue.
        """
        logger.warning(
            "Bridge down for session %s (%s); rebuilding and retrying",
            s.short_id,
            exc,
        )
        if self._stopping:
            raise RuntimeError("Session manager is stopping; recovery refused")
        failed_client = getattr(s.agent, "client", None) if s.agent is not None else None
        if failed_client is None and s.run is not None:
            failed_client = getattr(s.run, "client", None)
        async with self._bridge_recovery_lock(s.cwd):
            await self._drop_bridge(s.cwd, expected_client=failed_client)
            async with self._agent_lock(s.short_id):
                if self._try_resume_first and s.agent_id:
                    await self._close_agent(s)
                    try:
                        await self._resume(s)
                        s.status = STATUS_IDLE
                        s.run = None
                        s.run_id = None
                        self._persist()
                        self.log_session_event(
                            s.short_id,
                            "agent_resumed_after_bridge_rebuild",
                            agent_id=s.agent_id,
                        )
                        logger.warning(
                            "Resumed session %s after bridge rebuild (agent %s)",
                            s.short_id,
                            s.agent_id,
                        )
                        return "resumed"
                    except Exception as resume_exc:  # noqa: BLE001
                        logger.warning(
                            "Resume after bridge rebuild failed for %s (%s); "
                            "recreating agent instead",
                            s.short_id,
                            resume_exc,
                        )
                await self._recreate_agent(s)
                return "recreated"

    async def run_prompt(
        self,
        s: Session,
        prompt: str | UserMessage,
        on_update: UpdateCb,
        on_attachment: AttachmentCb | None = None,
        *,
        _depth: int = 0,
        _recreated: bool = False,
    ) -> tuple[str, str, list[Path]]:
        owned_lock: asyncio.Lock | None = None
        my_gen: int | None = None
        if _depth == 0:
            sid = s.short_id
            lock = self._run_lock(sid)
            # Never wait on the run lock — a second caller must get Busy, not queue.
            if lock.locked() or sid in self._run_lock_claiming:
                raise SessionBusyError(
                    "Already running — wait or /cancel first.",
                )
            self._run_lock_claiming.add(sid)
            try:
                await lock.acquire()
            finally:
                self._run_lock_claiming.discard(sid)
            owned_lock = lock
            my_gen = self._bump_run_generation(sid)
            current = asyncio.current_task()
            if current is not None:
                self._run_tasks[sid] = current
            s.status = STATUS_RUNNING
            self._persist()

        try:
            return await self._run_prompt_inner(
                s,
                prompt,
                on_update,
                on_attachment,
                _depth=_depth,
                _recreated=_recreated,
                _stall_retries=0,
            )
        finally:
            if _depth == 0:
                still_owner = (
                    my_gen is not None
                    and self._run_generations.get(s.short_id) == my_gen
                )
                if still_owner:
                    current = asyncio.current_task()
                    if self._run_tasks.get(s.short_id) is current:
                        self._run_tasks.pop(s.short_id, None)
                    if s.status == STATUS_RUNNING:
                        s.status = STATUS_IDLE
                    # Only cancel an in-flight SDK run we still own.
                    # Successful paths clear s.run before returning.
                    active_run = s.run
                    s.run = None
                    s.run_id = None
                    self._persist()
                    if active_run is not None:
                        try:
                            await active_run.cancel()  # type: ignore[attr-defined]
                        except Exception:  # noqa: BLE001
                            pass
                # Always release the lock object THIS call acquired — never the
                # possibly-replaced lock in _run_locks after force-unstick.
                if owned_lock is not None and owned_lock.locked():
                    try:
                        owned_lock.release()
                    except RuntimeError:
                        pass

    async def _run_prompt_inner(
        self,
        s: Session,
        prompt: str | UserMessage,
        on_update: UpdateCb,
        on_attachment: AttachmentCb | None = None,
        *,
        _depth: int = 0,
        _recreated: bool = False,
        _stall_retries: int = 0,
        _carry_text_parts: list[str] | None = None,
    ) -> tuple[str, str, list[Path]]:
        # Backfill config default for sessions created before effort support,
        # or after /model cleared params without re-applying.
        desired = self._desired_effort()
        if desired and not any(
            k in s.model_params for k in ("effort", "reasoning", "thinking")
        ):
            await self.apply_default_effort(s)
        send_opts = SendOptions(model=self.model_for_sdk(s), mode=s.mode)  # type: ignore[arg-type]
        run = None
        recreated = _recreated
        # Retry with recovery: dead bridge -> rebuild+recreate; stuck agent -> recreate.
        for attempt in range(1, BRIDGE_RUN_ATTEMPTS + 1):
            try:
                if await self._ensure_agent(s):
                    recreated = True
                run = await s.agent.send(prompt, send_opts)  # type: ignore[attr-defined]
                break
            except Exception as exc:  # noqa: BLE001
                if attempt >= BRIDGE_RUN_ATTEMPTS:
                    raise
                if _is_bridge_down(exc):
                    outcome = await self._recover_bridge_session(s, exc)
                    if outcome == "recreated":
                        recreated = True
                    continue
                if _is_stuck_agent(exc):
                    logger.warning(
                        "Stuck agent for session %s (%s); recreating and retrying",
                        s.short_id,
                        exc,
                    )
                    async with self._agent_lock(s.short_id):
                        await self._recreate_agent(s)
                    recreated = True
                    continue
                raise
        assert run is not None
        s.run = run
        s.run_id = getattr(run, "id", None)
        s.status = STATUS_RUNNING
        raw_prompt = prompt.text if isinstance(prompt, UserMessage) else prompt
        s.last_prompt = strip_telegram_delivery_prefix(
            strip_rules_prefix(raw_prompt),
        )
        s.last_activity = time.time()
        self._persist()
        prompt_chars = len(prompt.text) if isinstance(prompt, UserMessage) else len(prompt)
        run_started_at = time.time()
        self.event_log.append(
            s.short_id,
            "run_start",
            run_id=s.run_id,
            mode=s.mode,
            model=s.model,
            prompt_chars=prompt_chars,
        )
        logger.info("Session %s run %s started", s.short_id, s.run_id)

        # Keep streamed assistant text across stall auto-continue retries.
        # On the first new assistant event after continue, replace (don't append)
        # so live UI does not stack pre-stall text with the continuation.
        text_parts: list[str] = list(_carry_text_parts or [])
        replace_text_on_next_assistant = bool(_carry_text_parts)
        tool_hits: list[tuple[str, object, object]] = []
        sent_paths: set[str] = set()
        running_tools: set[str] = set()
        live: dict[str, object] = {
            "activity": "starting\u2026",
            "snippet": "",
            "elapsed": None,
        }
        stop_heartbeat = asyncio.Event()
        stall_timeout = float(
            getattr(self.cfg, "run_stall_timeout_sec", DEFAULT_RUN_STALL_TIMEOUT_SEC)
            or DEFAULT_RUN_STALL_TIMEOUT_SEC
        )
        stall_timeout = max(30.0, stall_timeout)
        tool_stall_timeout = float(
            getattr(
                self.cfg,
                "run_tool_stall_timeout_sec",
                DEFAULT_RUN_TOOL_STALL_TIMEOUT_SEC,
            )
            or DEFAULT_RUN_TOOL_STALL_TIMEOUT_SEC
        )
        tool_stall_timeout = max(stall_timeout, tool_stall_timeout)
        auto_continue_max = int(
            getattr(self.cfg, "stall_auto_continue_max", STALL_AUTO_CONTINUE_MAX)
            or STALL_AUTO_CONTINUE_MAX
        )
        auto_continue_prompt = str(
            getattr(self.cfg, "stall_auto_continue_prompt", STALL_AUTO_CONTINUE_PROMPT)
            or STALL_AUTO_CONTINUE_PROMPT
        )
        last_progress = time.monotonic()
        stalled = False
        stalled_limit = stall_timeout

        def _mark_progress() -> None:
            nonlocal last_progress
            last_progress = time.monotonic()
            s.last_activity = time.time()

        async def _heartbeat() -> None:
            t0 = time.monotonic()
            while not stop_heartbeat.is_set():
                try:
                    await asyncio.wait_for(
                        stop_heartbeat.wait(), timeout=LIVE_TIMER_INTERVAL_SEC,
                    )
                except asyncio.TimeoutError:
                    # Do NOT mark progress here — a hung tool used to reset the
                    # stall clock forever via running_tools. Only real SDK events
                    # (assistant/tool/status/summary) count as progress.
                    elapsed = int(time.monotonic() - t0)
                    await on_update(
                        _build_live(s, text_parts, live, elapsed=elapsed),
                        force=True,
                    )

        async def _stall_watchdog() -> None:
            """Cancel the SDK run if it stops producing events (noon hang class)."""
            nonlocal stalled, stalled_limit
            while not stop_heartbeat.is_set():
                try:
                    await asyncio.wait_for(stop_heartbeat.wait(), timeout=15.0)
                    return
                except asyncio.TimeoutError:
                    idle = time.monotonic() - last_progress
                    limit = tool_stall_timeout if running_tools else stall_timeout
                    if idle < limit:
                        continue
                    stalled = True
                    stalled_limit = limit
                    logger.error(
                        "Session %s run stalled for %.0fs (timeout=%.0fs, "
                        "tools_running=%s) — cancelling",
                        s.short_id,
                        idle,
                        limit,
                        bool(running_tools),
                    )
                    self.event_log.append(
                        s.short_id,
                        "run_stall",
                        idle_sec=int(idle),
                        timeout_sec=int(limit),
                        tools_running=bool(running_tools),
                        run_id=s.run_id,
                    )
                    live["snippet"] = ""
                    live["activity"] = (
                        f"\u26a0\ufe0f stalled {int(idle)}s — aborting hung run"
                    )
                    try:
                        await on_update(
                            _build_live(s, text_parts, live), force=True,
                        )
                    except Exception:  # noqa: BLE001
                        logger.warning(
                            "stall notify failed for %s", s.short_id, exc_info=True,
                        )
                    try:
                        await run.cancel()
                    except Exception:  # noqa: BLE001
                        logger.warning(
                            "stall cancel failed for %s", s.short_id, exc_info=True,
                        )
                    return

        hb_task = asyncio.create_task(_heartbeat())
        stall_task = asyncio.create_task(_stall_watchdog())
        blocked_tool_msg: str | None = None
        permission = (
            (self.bot_cfg.permission if self.bot_cfg else None) or "full"
        )
        # Defer mid-stream bridge retry until after heartbeat/stall are stopped.
        # ``return await`` inside this try would run nested work before finally.
        pending_bridge_retry = False
        pending_retry_prompt: str | UserMessage = prompt
        pending_recreated = True
        try:
            event_iter = run.events().__aiter__()
            async for event in _iter_events_until_stopped(
                event_iter,
                should_stop=lambda: blocked_tool_msg is not None or stalled,
            ):
                if blocked_tool_msg is not None:
                    break
                message = event.sdk_message
                if message is not None:
                    mtype = getattr(message, "type", None)
                    if mtype == "assistant":
                        if replace_text_on_next_assistant:
                            text_parts.clear()
                            replace_text_on_next_assistant = False
                        content = getattr(getattr(message, "message", None), "content", []) or []
                        for block in content:
                            if getattr(block, "type", None) == "text":
                                text_parts.append(getattr(block, "text", ""))
                        _mark_progress()
                        live["activity"] = ""
                        live["snippet"] = ""
                        await on_update(_build_live(s, text_parts, live))
                    elif mtype == "tool_call":
                        name = getattr(message, "name", "tool")
                        tstatus = getattr(message, "status", "")
                        args = getattr(message, "args", None)
                        call_id = getattr(message, "call_id", None) or getattr(message, "id", None) or f"{name}:{len(tool_hits)}"
                        if tstatus in ("running", "", None):
                            running_tools.add(call_id)
                            deny = evaluate_tool_call(
                                name,
                                args,
                                permission=permission,
                                cwd=s.cwd,
                                session_id=s.short_id,
                            )
                            if deny:
                                blocked_tool_msg = deny
                                logger.warning(
                                    "Blocked tool in session %s: %s — %s",
                                    s.short_id,
                                    name,
                                    deny.splitlines()[0][:120],
                                )
                                live["snippet"] = blocked_tool_msg
                                live["activity"] = "\u274c blocked: tool permission denied"
                                await on_update(
                                    _build_live(s, text_parts, live), force=True,
                                )
                                try:
                                    await run.cancel()
                                except Exception:  # noqa: BLE001
                                    pass
                                break
                            apply_plan_to_text_parts(name, args, text_parts)
                            _mark_progress()
                            live["snippet"] = ""
                            live["activity"] = format_tool_activity(name, args)
                            logger.info("Session %s tool %s", s.short_id, name)
                            await on_update(_build_live(s, text_parts, live))
                        elif tstatus in ("completed", "error"):
                            running_tools.discard(call_id)
                            result = getattr(message, "result", None)
                            tool_hits.append((name, args, result))
                            _mark_progress()

                            if tstatus == "completed":
                                self.event_log.append(
                                    s.short_id, "tool", name=name, status=tstatus,
                                )
                                apply_plan_to_text_parts(name, args, text_parts)
                                if is_create_plan_tool(name):
                                    live["snippet"] = ""
                                    live["activity"] = format_tool_activity(
                                        name, args, done=True,
                                    )
                                else:
                                    live["snippet"] = format_tool_result_snippet(
                                        name, result, args,
                                    )
                                    live["activity"] = format_tool_activity(
                                        name, args, done=True,
                                    )
                                await on_update(
                                    _build_live(s, text_parts, live), force=True,
                                )
                            if on_attachment and tstatus == "completed" and tool_auto_sends(name):
                                sid = s.short_id
                                event_log = self.event_log

                                async def _poll_image() -> None:
                                    paths = await deliver_generate_image_live(
                                        name,
                                        args,
                                        result,
                                        s.cwd,
                                        on_attachment,
                                        sent_paths,
                                        run_started_at=run_started_at,
                                    )
                                    for p in paths:
                                        event_log.append(
                                            sid, "attachment", file=p.name, live=True,
                                        )
                                        logger.info(
                                            "Session %s live attachment: %s", sid, p.name,
                                        )

                                self._track_background_task(
                                    asyncio.create_task(_poll_image())
                                )

                            if tstatus == "error":
                                live["activity"] = (
                                    format_tool_activity(name, args)
                                    .replace("\U0001f7e1", "\u274c", 1) + " failed"
                                )
                                live["snippet"] = format_tool_error_snippet(result)
                                await on_update(
                                    _build_live(s, text_parts, live), force=True,
                                )
                    elif mtype == "status":
                        activity = format_sdk_status_activity(
                            str(getattr(message, "status", "") or ""),
                            str(getattr(message, "message", "") or ""),
                        )
                        if activity:
                            _mark_progress()
                            live["snippet"] = ""
                            live["activity"] = activity
                            await on_update(_build_live(s, text_parts, live), force=True)
                    continue

                update = event.interaction_update
                if update is None:
                    continue
                utype = getattr(update, "type", None)
                if utype == "summary-started":
                    _mark_progress()
                    live["snippet"] = ""
                    live["activity"] = "compacting context\u2026"
                    await on_update(_build_live(s, text_parts, live), force=True)
                elif utype == "summary":
                    summary = getattr(update, "summary", "")
                    if summary:
                        text_parts.append(summary)
                        _mark_progress()
                        live["activity"] = ""
                        live["snippet"] = ""
                        await on_update(_build_live(s, text_parts, live))
                elif utype == "summary-completed":
                    _mark_progress()
                    live["activity"] = ""
                    live["snippet"] = ""
                    await on_update(_build_live(s, text_parts, live))
                elif utype == "partial-tool-call":
                    tool_call = getattr(update, "tool_call", None)
                    if isinstance(tool_call, dict):
                        tc_name = str(tool_call.get("type") or tool_call.get("name") or "")
                        tc_args = tool_call.get("args")
                        deny = evaluate_tool_call(
                            tc_name,
                            tc_args,
                            permission=permission,
                            cwd=s.cwd,
                            session_id=s.short_id,
                        )
                        if deny:
                            blocked_tool_msg = deny
                            logger.warning(
                                "Blocked tool in session %s: %s — %s",
                                s.short_id,
                                tc_name,
                                deny.splitlines()[0][:120],
                            )
                            live["snippet"] = blocked_tool_msg
                            live["activity"] = "\u274c blocked: tool permission denied"
                            await on_update(
                                _build_live(s, text_parts, live), force=True,
                            )
                            try:
                                await run.cancel()
                            except Exception:  # noqa: BLE001
                                pass
                            break
                        if apply_plan_to_text_parts(tc_name, tc_args, text_parts):
                            _mark_progress()
                            live["snippet"] = ""
                            live["activity"] = format_tool_activity(tc_name, tc_args)
                            await on_update(
                                _build_live(s, text_parts, live), force=True,
                            )
        except Exception as exc:
            if _is_bridge_down(exc) and _depth + 1 < BRIDGE_RUN_ATTEMPTS:
                logger.warning(
                    "Bridge down mid-stream for session %s "
                    "(attempt %s/%s): %s; rebuilding and retrying",
                    s.short_id,
                    _depth + 1,
                    BRIDGE_RUN_ATTEMPTS,
                    exc,
                )
                outcome = await self._recover_bridge_session(s, exc)
                pending_bridge_retry = True
                if outcome == "resumed":
                    # Agent kept prior turns — do not re-send the full user prompt.
                    pending_retry_prompt = BRIDGE_RESUME_CONTINUE_PROMPT
                    pending_recreated = False
                else:
                    pending_retry_prompt = prompt
                    pending_recreated = True
            else:
                s.status = STATUS_ERROR
                s.run = None
                self._persist()
                raise
        finally:
            stop_heartbeat.set()
            for task in (hb_task, stall_task):
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "heartbeat/stall task ended with error for %s",
                        s.short_id,
                        exc_info=True,
                    )

        if pending_bridge_retry:
            return await self._run_prompt_inner(
                s,
                pending_retry_prompt,
                on_update,
                on_attachment,
                _depth=_depth + 1,
                _recreated=pending_recreated,
                _stall_retries=_stall_retries,
            )

        try:
            if stalled or blocked_tool_msg is not None:
                result = await asyncio.wait_for(
                    run.wait(),
                    timeout=POST_CANCEL_WAIT_TIMEOUT_SEC,
                )
            else:
                result = await run.wait()
        except asyncio.TimeoutError:
            logger.error(
                "Session %s run.wait timed out after cancel/stall "
                "(timeout=%.0fs) — forcing cancelled",
                s.short_id,
                POST_CANCEL_WAIT_TIMEOUT_SEC,
            )
            self.event_log.append(
                s.short_id,
                "run_wait_timeout",
                timeout_sec=int(POST_CANCEL_WAIT_TIMEOUT_SEC),
                run_id=s.run_id,
            )
            result = SimpleNamespace(status="cancelled", result=None)
            stalled = True
        except Exception as exc:
            if _is_bridge_down(exc) and _depth + 1 < BRIDGE_RUN_ATTEMPTS:
                logger.warning(
                    "Bridge down waiting for session %s "
                    "(attempt %s/%s): %s; rebuilding and retrying",
                    s.short_id,
                    _depth + 1,
                    BRIDGE_RUN_ATTEMPTS,
                    exc,
                )
                outcome = await self._recover_bridge_session(s, exc)
                retry_prompt: str | UserMessage = (
                    BRIDGE_RESUME_CONTINUE_PROMPT if outcome == "resumed" else prompt
                )
                return await self._run_prompt_inner(
                    s,
                    retry_prompt,
                    on_update,
                    on_attachment,
                    _depth=_depth + 1,
                    _recreated=(outcome == "recreated"),
                    _stall_retries=_stall_retries,
                )
            s.status = STATUS_ERROR
            s.run = None
            self._persist()
            raise
        rstatus = getattr(result, "status", "finished")
        s.run = None
        s.run_id = None
        s.last_activity = time.time()

        if stalled and rstatus not in ("error",):
            # Cancelled-by-watchdog should surface clearly even if SDK says finished.
            rstatus = "cancelled"

        if blocked_tool_msg is not None:
            rstatus = "cancelled"
            final = blocked_tool_msg
        elif stalled:
            if _stall_retries < auto_continue_max:
                logger.warning(
                    "Session %s stalled — auto-continuing with %r (retry %s/%s)",
                    s.short_id,
                    auto_continue_prompt,
                    _stall_retries + 1,
                    auto_continue_max,
                )
                self.event_log.append(
                    s.short_id,
                    "run_stall_auto_continue",
                    timeout_sec=int(stalled_limit),
                    retry=_stall_retries + 1,
                    run_id=s.run_id,
                )
                try:
                    await on_update(
                        _build_live(
                            s,
                            text_parts,
                            {
                                "activity": (
                                    f"\u26a0\ufe0f stalled — auto-continuing ({_stall_retries + 1}/{auto_continue_max})\u2026"
                                ),
                                "snippet": "",
                                "elapsed": live.get("elapsed"),
                            },
                        ),
                        force=True,
                    )
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "stall auto-continue notify failed for %s",
                        s.short_id,
                        exc_info=True,
                    )
                s.run = None
                s.run_id = None
                s.status = STATUS_RUNNING
                self._persist()
                # Same agent +「继续」— keep any streamed text from before the stall.
                return await self._run_prompt_inner(
                    s,
                    auto_continue_prompt,
                    on_update,
                    on_attachment,
                    _depth=_depth,
                    _recreated=recreated,
                    _stall_retries=_stall_retries + 1,
                    _carry_text_parts=text_parts,
                )
            abort_msg = (
                f"**Run aborted:** no agent progress for {int(stalled_limit)}s "
                "(hung run watchdog).\n\n"
                f"Already auto-retried {auto_continue_max} time(s) with「{auto_continue_prompt}」. "
                "Send the prompt again, or `/reload` if this keeps happening."
            )
            final = _final_with_partial(text_parts, abort_msg)
        else:
            sdk_final = getattr(result, "result", None)
            final = resolve_final_body(
                sdk_final=sdk_final if isinstance(sdk_final, str) else None,
                text_parts=text_parts,
                tool_hits=tool_hits,
            )
        if rstatus == "error":
            logger.warning(
                "Session %s run error: duration_ms=%s output=%r",
                s.short_id,
                getattr(result, "duration_ms", None),
                (final or "")[:300],
            )

        instant_empty_error = (
            not stalled
            and rstatus == "error"
            and not (final or "").strip()
            and not text_parts
            and not tool_hits
            and (time.time() - run_started_at) < 8
        )
        if instant_empty_error:
            logger.warning(
                "Session %s instant empty error (model=%s mode=%s) — recreating and auto-retrying",
                s.short_id,
                s.model,
                s.mode,
            )
            try:
                await on_update(
                    _build_live(
                        s,
                        text_parts,
                        {
                            "activity": (
                                "\u26a0\ufe0f instant empty error — recreating agent and retrying\u2026"
                            ),
                            "snippet": "",
                            "elapsed": live.get("elapsed"),
                        },
                    ),
                    force=True,
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "instant-empty notify failed for %s", s.short_id, exc_info=True,
                )
            try:
                async with self._agent_lock(s.short_id):
                    await self._recreate_agent(s)
            except Exception:
                logger.warning(
                    "Recreate after instant error failed for %s",
                    s.short_id,
                    exc_info=True,
                )
                final = (
                    f"{instant_empty_user_message(s.model, s.mode)}\n\n"
                    "Could not reset the session — try /end and /new."
                )
                rstatus = STATUS_GLITCH
                s.status = STATUS_IDLE
            else:
                if _depth == 0:
                    # Auto-retry the prompt with the fresh agent (once only).
                    return await self._run_prompt_inner(
                        s,
                        prompt,
                        on_update,
                        on_attachment,
                        _depth=1,
                        _stall_retries=_stall_retries,
                    )
                # Second glitch in a row — give up and tell the user.
                final = instant_empty_user_message(s.model, s.mode)
                rstatus = STATUS_GLITCH
                s.status = STATUS_IDLE
        else:
            s.status = STATUS_ERROR if rstatus == "error" else STATUS_IDLE

        self._persist()
        logger.info("Session %s run finished: %s", s.short_id, rstatus)
        note = self._context_lost_note(s, recreated)
        if note:
            final = note + (final or "")
        attachments = collect_run_attachments(
            cwd=s.cwd,
            tool_hits=tool_hits,
            texts=[*text_parts, final or ""],
            run_started_at=run_started_at,
        )
        attachments = [p for p in attachments if str(p.resolve()) not in sent_paths]
        self.event_log.append(
            s.short_id,
            "run_end",
            status=rstatus,
            attachments=len(attachments),
        )
        if attachments:
            logger.info(
                "Session %s attachments: %s",
                s.short_id,
                ", ".join(p.name for p in attachments),
            )
        return rstatus, final, attachments

    async def cancel(self, s: Session) -> bool:
        """Stop the in-flight prompt run, or clear a stuck lock."""
        run = s.run
        if run is not None:
            try:
                await run.cancel()
            except Exception as exc:  # noqa: BLE001
                logger.debug("run.cancel failed for %s: %s", s.short_id, exc)

        task = self._run_tasks.get(s.short_id)
        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=15.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):  # noqa: BLE001
                pass

        if not self.is_busy(s):
            return True

        if self.is_busy(s):
            self._force_unstick(s)
            logger.warning("Force-cleared stuck run lock for session %s", s.short_id)
            return True
        return False

    def _force_unstick(self, s: Session) -> None:
        """Drop an orphaned run lock when cancel cannot finish the coroutine.

        Bumps the run generation first so a stale ``run_prompt`` finally cannot
        clear a successor's task/run or release the replacement lock.
        """
        self._bump_run_generation(s.short_id)
        self._run_lock_claiming.discard(s.short_id)
        if self._run_lock(s.short_id).locked():
            self._run_locks[s.short_id] = asyncio.Lock()
        self._run_tasks.pop(s.short_id, None)
        s.status = STATUS_IDLE
        s.run = None
        s.run_id = None
        self._persist()

    # ---- persistence -------------------------------------------------------

    def _persist(self) -> None:
        data = {
            "counter": self._counter,
            "active": {str(k): v for k, v in self.active.items()},
            "sessions": [s.to_dict() for s in self.sessions.values()],
        }
        path = self.sessions_file
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(path)

    def _load(self) -> None:
        path = self.sessions_file
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read %s: %s", path, exc)
            return
        self._counter = int(data.get("counter", 0))
        self.active = {int(k): v for k, v in data.get("active", {}).items()}
        for sd in data.get("sessions", []):
            s = Session(
                short_id=sd["short_id"],
                cwd=sd["cwd"],
                custom_name=sd.get("custom_name"),
                agent_id=sd.get("agent_id"),
                status=sd.get("status", STATUS_IDLE),
                model=sd.get("model") or self.cfg.model,
                mode=sd.get("mode") or MODE_AGENT,
                model_params=dict(sd.get("model_params") or {}),
                last_prompt=sd.get("last_prompt", ""),
                last_activity=sd.get("last_activity", time.time()),
                prior_agent_ids=list(sd.get("prior_agent_ids") or []),
                context_restored_from=sd.get("context_restored_from"),
                context_lost_notice_pending=bool(
                    sd.get("context_lost_notice_pending")
                ),
            )
            self.sessions[s.short_id] = s
