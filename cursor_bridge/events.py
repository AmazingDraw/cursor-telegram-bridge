"""Append-only per-session event log (JSONL) for history and the web console."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class SessionEventLog:
    """One JSONL file per session under ``state/bots/<bot>/events/{sid}.jsonl``."""

    def __init__(self, state_dir: Path, *, max_events: int = 500) -> None:
        self.dir = state_dir / "events"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.max_events = max_events

    def _path(self, sid: str) -> Path:
        return self.dir / f"{sid}.jsonl"

    def append(self, sid: str, event: str, **data: Any) -> None:
        record = {"ts": time.time(), "sid": sid, "event": event, **data}
        path = self._path(sid)
        try:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            return
        self._trim(path)

    def append_audit(self, event: str, **data: Any) -> None:
        """Cross-session audit trail at ``events/audit.jsonl`` under the bot state dir."""
        record = {"ts": time.time(), "event": event, **data}
        path = self.dir / "audit.jsonl"
        try:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            return
        self._trim(path)

    def _trim(self, path: Path) -> None:
        if self.max_events <= 0:
            return
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        if len(lines) <= self.max_events:
            return
        try:
            path.write_text("\n".join(lines[-self.max_events :]) + "\n", encoding="utf-8")
        except OSError:
            pass

    def read(self, sid: str, *, limit: int = 100) -> list[dict[str, Any]]:
        path = self._path(sid)
        if not path.is_file():
            return []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out: list[dict[str, Any]] = []
        for line in lines[-limit:]:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def read_recent(self, *, limit: int = 80) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        try:
            paths = sorted(self.dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return []
        for path in paths:
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines[-limit:]:
                try:
                    merged.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        merged.sort(key=lambda r: float(r.get("ts", 0)), reverse=True)
        return merged[:limit]

    def list_session_ids(self) -> list[str]:
        return sorted(p.stem for p in self.dir.glob("*.jsonl"))
