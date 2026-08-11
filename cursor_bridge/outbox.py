"""Persistent per-bot Telegram outbox — final replies are never silently dropped.

Messages that exhaust the in-memory retry budget (network down, poll wedge, …)
are persisted under ``state/bots/<name>/outbox.jsonl`` and redelivered on a
background loop. Multi-bot safe: each bot app owns its own file and sender, so
items are naturally isolated per bot.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Awaitable, Callable

logger = logging.getLogger("cursor_bridge.outbox")

# Deliver (chat_id, text, parse_mode) -> True when the message is settled
# (sent, or permanently undeliverable), False when it must be retried later.
SendCb = Callable[[int, str, str | None], Awaitable[bool]]


@dataclass
class OutboxItem:
    id: str
    chat_id: int
    text: str
    parse_mode: str | None
    created_at: float
    attempts: int = 0
    last_error: str = ""

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> "OutboxItem":
        return cls(
            id=str(data["id"]),
            chat_id=int(data["chat_id"]),
            text=str(data["text"]),
            parse_mode=data.get("parse_mode") or None,
            created_at=float(data.get("created_at", 0.0)),
            attempts=int(data.get("attempts", 0)),
            last_error=str(data.get("last_error", "")),
        )


class TelegramOutbox:
    """Persistent, per-bot retry queue for Telegram messages."""

    def __init__(
        self,
        path: Path,
        *,
        retry_interval_sec: float = 30.0,
        max_age_sec: float = 86400.0,
    ) -> None:
        self._path = path
        self._retry_interval_sec = max(10.0, float(retry_interval_sec))
        self._max_age_sec = max(300.0, float(max_age_sec))
        self._items: list[OutboxItem] = []
        self._send_cb: SendCb | None = None
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()
        self._load()

    # -- lifecycle -----------------------------------------------------------

    def start(self, send_cb: SendCb) -> None:
        if self._task is not None and not self._task.done():
            return
        self._send_cb = send_cb
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="telegram-outbox")

    async def stop(self) -> None:
        self._stop.set()
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._persist()

    # -- api -----------------------------------------------------------------

    def enqueue(
        self,
        *,
        chat_id: int,
        text: str,
        parse_mode: str | None = None,
    ) -> int:
        item = OutboxItem(
            id=uuid.uuid4().hex,
            chat_id=chat_id,
            text=text,
            parse_mode=parse_mode,
            created_at=time.time(),
        )
        self._items.append(item)
        self._persist()
        return len(self._items)

    def pending_count(self) -> int:
        return len(self._items)

    # -- internals -----------------------------------------------------------

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = self._path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            logger.warning("outbox load failed: %s", exc)
            return
        for line in raw:
            line = line.strip()
            if not line:
                continue
            try:
                self._items.append(OutboxItem.from_json(json.loads(line)))
            except Exception:  # noqa: BLE001 - one bad line must not kill the queue
                logger.warning("outbox skipped malformed line: %.120s", line)

    def _persist(self) -> None:
        try:
            lines = "\n".join(json.dumps(i.to_json()) for i in self._items)
            self._path.write_text(lines + ("\n" if lines else ""), encoding="utf-8")
        except OSError as exc:
            logger.warning("outbox persist failed: %s", exc)

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._deliver_once()
            except Exception:  # noqa: BLE001
                logger.exception("outbox delivery tick failed")
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._retry_interval_sec,
                )
                return
            except asyncio.TimeoutError:
                pass

    async def _deliver_once(self) -> int:
        """Try every pending item once. Returns the number delivered/dropped."""
        if self._send_cb is None or not self._items:
            return 0
        delivered = 0
        async with self._lock:
            to_process = list(self._items)
            handled_ids: set[str] = set()
            for item in to_process:
                if time.time() - item.created_at > self._max_age_sec:
                    logger.error(
                        "outbox item expired, dropping (chat=%s id=%s)",
                        item.chat_id,
                        item.id,
                    )
                    handled_ids.add(item.id)
                    delivered += 1
                    continue
                try:
                    ok = await self._send_cb(item.chat_id, item.text, item.parse_mode)
                except Exception as exc:  # noqa: BLE001
                    ok = False
                    item.last_error = str(exc)[:200]
                item.attempts += 1
                if ok:
                    handled_ids.add(item.id)
                    delivered += 1
                    logger.info(
                        "outbox delivered (chat=%s id=%s attempts=%s)",
                        item.chat_id,
                        item.id,
                        item.attempts,
                    )
                else:
                    if not item.last_error:
                        item.last_error = "delivery returned False"
            # Retain unhandled items + any new items enqueued during delivery
            self._items = [i for i in self._items if i.id not in handled_ids]
            self._persist()
        return delivered
