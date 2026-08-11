"""Background prompt scheduling keeps Telegram handlers responsive."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import cursor_bridge.bot as bot_mod
from cursor_bridge.sessions import STATUS_IDLE, STATUS_RUNNING, Session


class _FakeMgr:
    def __init__(self) -> None:
        self._run_tasks: dict[str, asyncio.Task] = {}
        self._persisted = 0

    def is_busy(self, s: Session) -> bool:
        if s.status == STATUS_RUNNING:
            return True
        task = self._run_tasks.get(s.short_id)
        if task is not None and not task.done():
            return True
        return False

    def pop_queued_prompt(self, sid: str):
        return None

    def queued_count(self, sid: str) -> int:
        return 0

    def _persist(self) -> None:
        self._persisted += 1


def test_schedule_session_work_runs_off_handler_and_marks_busy() -> None:
    async def _run() -> None:
        mgr = _FakeMgr()
        session = Session(short_id="s1", cwd="/tmp", status=STATUS_IDLE)
        started = asyncio.Event()
        release = asyncio.Event()

        async def work() -> None:
            started.set()
            await release.wait()

        app = MagicMock()
        app.bot_data = {}
        context = SimpleNamespace(application=app)

        # Patch _mgr used by scheduler.
        original = bot_mod._mgr
        bot_mod._mgr = lambda _ctx: mgr  # type: ignore[assignment]
        try:
            assert bot_mod._schedule_session_work(
                context, session, work(), name="prompt",
            )
            assert mgr.is_busy(session)
            dup = work()
            assert not bot_mod._schedule_session_work(
                context, session, dup, name="prompt",
            )
            dup.close()
            task = mgr._run_tasks["s1"]
            await asyncio.wait_for(started.wait(), timeout=1.0)
            release.set()
            await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
            assert "s1" not in mgr._run_tasks
        finally:
            bot_mod._mgr = original  # type: ignore[assignment]

    asyncio.run(_run())
