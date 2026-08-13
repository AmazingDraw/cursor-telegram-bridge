"""Process health probe — Telegram poll liveness + auto recovery.

Tracks consecutive Telegram poll failures and quiet periods, then triggers a
soft restart (in-process) or escalates to launchd kickstart when soft recovery
is not enough.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

logger = logging.getLogger("cursor_bridge.health")

KickstartCb = Callable[[], Awaitable[None]]
SoftRestartCb = Callable[[], None]
NotifyCb = Callable[[str], Awaitable[None]]


@dataclass
class HealthState:
    """Mutable counters shared by the error handler and probe task."""

    last_ok_at: float = field(default_factory=time.time)
    last_poll_error_at: float = 0.0
    consecutive_poll_failures: int = 0
    soft_restarts: int = 0
    last_recovery_at: float = 0.0
    updater_alive: bool = True
    # True while a wedge episode is ongoing (first alert already sent).
    episode_open: bool = False


class HealthProbe:
    """Background probe that heals a wedged Telegram poller.

    Notifications are episode-based: the first wedge of an outage notifies
    once, further soft restarts in the same episode stay silent, and only
    kickstart escalation or sustained recovery notifies again — a long
    network outage must not spam the owner every check cycle.
    """

    def __init__(
        self,
        *,
        check_interval_sec: float = 60.0,
        poll_fail_threshold: int = 8,
        quiet_sec: float = 300.0,
        heartbeat_interval_sec: float = 30.0,
        kickstart_after_soft: int = 2,
        soft_restart_cb: SoftRestartCb | None = None,
        kickstart_cb: KickstartCb | None = None,
        notify_cb: NotifyCb | None = None,
        is_updater_running: Callable[[], bool] | None = None,
        heartbeat_cb: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.check_interval_sec = max(10.0, float(check_interval_sec))
        self.poll_fail_threshold = max(2, int(poll_fail_threshold))
        self.quiet_sec = max(60.0, float(quiet_sec))
        self.heartbeat_interval_sec = max(10.0, float(heartbeat_interval_sec))
        self.kickstart_after_soft = max(0, int(kickstart_after_soft))
        self.soft_restart_cb = soft_restart_cb
        self.kickstart_cb = kickstart_cb
        self.notify_cb = notify_cb
        self.is_updater_running = is_updater_running
        self.heartbeat_cb = heartbeat_cb
        self.state = HealthState()
        self._task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def note_ok(self) -> None:
        self.state.last_ok_at = time.time()
        self.state.consecutive_poll_failures = 0
        self.state.updater_alive = True
        # Do not clear soft_restarts here — owner messages call note_ok on every
        # update and would prevent kickstart escalation after soft restarts.

    def note_poll_error(self, err: BaseException | None = None) -> None:
        self.state.last_poll_error_at = time.time()
        self.state.consecutive_poll_failures += 1
        if err is not None:
            logger.warning(
                "Health poll error #%s: %s",
                self.state.consecutive_poll_failures,
                err,
            )

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="health-probe")
        if self.heartbeat_cb is not None:
            self._heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(), name="health-heartbeat",
            )

    async def stop(self) -> None:
        self._stop.set()
        task = self._task
        self._task = None
        hb_task = self._heartbeat_task
        self._heartbeat_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if hb_task is not None:
            hb_task.cancel()
            try:
                await hb_task
            except asyncio.CancelledError:
                pass

    async def _heartbeat_loop(self) -> None:
        """Periodically ping Telegram; failure counts toward poll errors.

        Success must *not* call ``note_ok()`` — getMe can succeed while long
        polling is wedged, and clearing poll failures would hide that.
        """
        while not self._stop.is_set():
            await self._heartbeat_tick()
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.heartbeat_interval_sec,
                )
                return
            except asyncio.TimeoutError:
                pass

    async def _heartbeat_tick(self) -> None:
        try:
            await self.heartbeat_cb()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.note_poll_error(exc)
            logger.warning("Telegram heartbeat failed: %s", exc)

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.check_interval_sec,
                )
                return
            except asyncio.TimeoutError:
                pass
            try:
                await self._tick()
            except Exception:  # noqa: BLE001
                logger.exception("Health probe tick failed")

    async def _tick(self) -> None:
        now = time.time()
        # Cooldown after a recovery attempt so we don't thrash.
        if now - self.state.last_recovery_at < 90.0:
            return

        updater_dead = False
        if self.is_updater_running is not None:
            try:
                updater_dead = not bool(self.is_updater_running())
            except Exception:  # noqa: BLE001
                updater_dead = True
            self.state.updater_alive = not updater_dead

        failures = self.state.consecutive_poll_failures
        quiet = now - self.state.last_ok_at
        wedged = updater_dead or (
            failures >= self.poll_fail_threshold
            and quiet >= self.quiet_sec
        )
        if not wedged:
            # Sustained health after the last recovery — reset escalation counter.
            # Require updater alive, no poll failures, and past recovery cooldown
            # so a soft-restart notify + owner reply cannot wipe the counter.
            healthy_for = now - self.state.last_recovery_at
            if (
                self.state.soft_restarts
                and not updater_dead
                and failures == 0
                and self.state.last_recovery_at > 0
                and healthy_for >= max(300.0, self.quiet_sec)
            ):
                logger.info(
                    "Health probe: sustained healthy for %.0fs — resetting soft_restarts",
                    healthy_for,
                )
                if self.state.episode_open:
                    await self._notify(
                        "✅ Health probe: Telegram poll recovered — "
                        f"episode closed after {self.state.soft_restarts} "
                        "soft restart(s)."
                    )
                self.state.soft_restarts = 0
                self.state.episode_open = False
            return

        reason = (
            f"updater_dead={updater_dead} poll_failures={failures} "
            f"quiet={quiet:.0f}s"
        )
        logger.error("Health probe: Telegram bridge appears wedged (%s)", reason)

        # Prefer soft in-process restart first; escalate to launchd kickstart.
        if (
            self.kickstart_after_soft > 0
            and self.state.soft_restarts >= self.kickstart_after_soft
            and self.kickstart_cb is not None
        ):
            msg = (
                "🩺 Health probe: soft restart failed repeatedly — "
                "launchd kickstart to recover."
            )
            await self._notify(msg)
            self.state.last_recovery_at = now
            await self.kickstart_cb()
            return

        if self.soft_restart_cb is not None:
            self.state.soft_restarts += 1
            self.state.last_recovery_at = now
            if not self.state.episode_open:
                # First wedge of this outage — notify once; later restarts in
                # the same episode stay silent until recovery or kickstart.
                self.state.episode_open = True
                msg = (
                    "🩺 Health probe: Telegram poll unhealthy — "
                    f"soft restart ({self.state.soft_restarts}).\n"
                    f"<code>{reason}</code>\n"
                    "Further restarts in this episode stay silent; "
                    "you'll hear from me on recovery or kickstart."
                )
                await self._notify(msg)
            else:
                logger.warning(
                    "Health probe: still wedged — silent soft restart (%s)",
                    self.state.soft_restarts,
                )
            result = self.soft_restart_cb()
            if inspect.isawaitable(result):
                await result
            return

        logger.error("Health probe: no recovery callback configured")

    async def _notify(self, text: str) -> None:
        if self.notify_cb is None:
            return
        try:
            await self.notify_cb(text)
        except Exception:  # noqa: BLE001
            logger.warning("Health notify failed", exc_info=True)


def should_count_as_poll_error(err: BaseException | None) -> bool:
    """True for transient Telegram poll failures worth tracking."""
    if err is None:
        return False
    name = type(err).__name__.lower()
    text = str(err).lower()
    needles = (
        "networkerror",
        "timedout",
        "timeout",
        "pooltimeout",
        "connecterror",
        "remoteprotocolerror",
        "server disconnected",
        "connection reset",
    )
    return any(n in name or n in text for n in needles)
