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
SoftRestartCb = Callable[[], bool | Awaitable[bool]]
NotifyCb = Callable[[str], Awaitable[None]]


@dataclass
class HealthState:
    """Mutable counters shared by the error handler and probe task."""

    last_ok_at: float = field(default_factory=time.time)
    last_poll_error_at: float = 0.0
    consecutive_poll_failures: int = 0
    heartbeat_failures: int = 0
    last_heartbeat_error_at: float = 0.0
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
        """Record positive getUpdates evidence from an authorized update."""
        self.state.last_ok_at = time.time()
        self.state.consecutive_poll_failures = 0
        self.state.last_poll_error_at = 0.0
        self.state.updater_alive = True
        self.state.soft_restarts = 0
        self.state.episode_open = False

    def note_poll_error(self, err: BaseException | None = None) -> None:
        """Record one getUpdates failure.

        Errors separated by a healthy-sized gap are different bursts. This
        prevents eight sporadic network failures over an idle afternoon from
        being misclassified as eight consecutive polling failures.
        """
        now = time.time()
        previous = self.state.last_poll_error_at
        if previous and now - previous >= self.quiet_sec:
            self.state.consecutive_poll_failures = 0
        self.state.last_poll_error_at = now
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
            self.state.heartbeat_failures = 0
            self.state.last_heartbeat_error_at = 0.0
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            # getMe uses the normal Bot API pool, not the separate getUpdates
            # pool. It is useful reachability telemetry, but not poll evidence.
            self.state.heartbeat_failures += 1
            self.state.last_heartbeat_error_at = time.time()
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

        updater_dead = False
        if self.is_updater_running is not None:
            try:
                updater_dead = not bool(self.is_updater_running())
            except Exception:  # noqa: BLE001
                updater_dead = True
            self.state.updater_alive = not updater_dead

        failures = self.state.consecutive_poll_failures
        error_age = (
            now - self.state.last_poll_error_at
            if self.state.last_poll_error_at
            else float("inf")
        )

        # No new polling error for a full quiet window: the burst recovered.
        if not updater_dead and failures and error_age >= self.quiet_sec:
            logger.info(
                "Health probe: poll error burst recovered after %.0fs "
                "(failures=%s)",
                error_age,
                failures,
            )
            if self.state.episode_open:
                await self._notify(
                    "✅ Health probe: Telegram poll recovered — "
                    f"episode closed after {self.state.soft_restarts} "
                    "soft restart(s)."
                )
            self.state.consecutive_poll_failures = 0
            self.state.last_poll_error_at = 0.0
            self.state.soft_restarts = 0
            self.state.episode_open = False
            return

        # A successful restart with no new errors closes after one quiet
        # probation window. Until then, retain the escalation count.
        if (
            not updater_dead
            and failures == 0
            and self.state.episode_open
            and self.state.last_recovery_at > 0
            and now - self.state.last_recovery_at >= self.quiet_sec
        ):
            logger.info(
                "Health probe: poller healthy for %.0fs after restart",
                now - self.state.last_recovery_at,
            )
            await self._notify(
                "✅ Health probe: Telegram poll recovered — "
                f"episode closed after {self.state.soft_restarts} "
                "soft restart(s)."
            )
            self.state.soft_restarts = 0
            self.state.episode_open = False
            return

        # Cooldown after a recovery attempt so we don't thrash.
        if now - self.state.last_recovery_at < 90.0:
            return

        wedged = updater_dead or failures >= self.poll_fail_threshold
        if not wedged:
            return

        reason = (
            f"updater_dead={updater_dead} poll_failures={failures} "
            f"last_poll_error={error_age:.0f}s ago "
            f"heartbeat_failures={self.state.heartbeat_failures}"
        )
        logger.error("Health probe: Telegram bridge appears wedged (%s)", reason)

        # Prefer soft in-process restart first; escalate to launchd kickstart.
        if (
            self.kickstart_after_soft > 0
            and self.state.soft_restarts >= self.kickstart_after_soft
            and self.kickstart_cb is not None
        ):
            logger.error(
                "Health probe: escalating to launchd kickstart after %s "
                "soft restart(s) (%s)",
                self.state.soft_restarts,
                reason,
            )
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
                result = await result
            restart_ok = result is not False
            if restart_ok:
                # A successful in-process restart is a new polling baseline.
                # Require fresh post-restart errors before another recovery.
                self.state.consecutive_poll_failures = 0
                self.state.last_poll_error_at = 0.0
                self.state.updater_alive = True
                logger.warning(
                    "Health probe: poller restart succeeded; awaiting fresh "
                    "poll evidence"
                )
            else:
                logger.error("Health probe: poller restart failed (%s)", reason)
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
