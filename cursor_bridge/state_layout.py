"""Per-bot state directory layout under ``state/bots/<name>/``.

Process-level files (pid, logs, restart markers) stay in ``state/``.
Session registries and event logs live one level under each bot name so
multi-bot setups stay peer-aligned.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from .config import BotConfig, Config

logger = logging.getLogger(__name__)


def bot_name_for(bot_cfg: BotConfig | None) -> str:
    return bot_cfg.name if bot_cfg else "default"


def bot_state_dir(cfg: Config, bot_name: str | BotConfig | None = None) -> Path:
    """Return ``state/bots/<name>`` for a bot (always peer-level)."""
    if isinstance(bot_name, BotConfig):
        name = bot_name.name
    elif isinstance(bot_name, str) and bot_name.strip():
        name = bot_name.strip()
    else:
        name = "default"
    path = cfg.state_dir / "bots" / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def migrate_legacy_default_state(cfg: Config) -> bool:
    """Move legacy ``state/sessions.json`` + ``state/events`` into ``state/bots/default/``.

    Returns True if any files were moved.
    """
    legacy_sessions = cfg.state_dir / "sessions.json"
    legacy_events = cfg.state_dir / "events"
    dest = bot_state_dir(cfg, "default")
    dest_sessions = dest / "sessions.json"
    dest_events = dest / "events"
    moved = False

    if legacy_sessions.is_file() and not dest_sessions.exists():
        shutil.move(str(legacy_sessions), str(dest_sessions))
        logger.info("Migrated legacy sessions.json → %s", dest_sessions)
        moved = True
    elif legacy_sessions.is_file() and dest_sessions.exists():
        # Prefer the new location; keep a backup of the unused legacy file.
        backup = cfg.state_dir / "sessions.json.legacy"
        if not backup.exists():
            shutil.move(str(legacy_sessions), str(backup))
            logger.info("Archived duplicate legacy sessions.json → %s", backup)
            moved = True

    if legacy_events.is_dir():
        if not dest_events.exists():
            shutil.move(str(legacy_events), str(dest_events))
            logger.info("Migrated legacy events/ → %s", dest_events)
            moved = True
        elif not any(dest_events.iterdir()) and any(legacy_events.iterdir()):
            for child in legacy_events.iterdir():
                target = dest_events / child.name
                if not target.exists():
                    shutil.move(str(child), str(target))
                    moved = True
            try:
                legacy_events.rmdir()
            except OSError:
                pass
            if moved:
                logger.info("Merged legacy events/ into %s", dest_events)

    return moved
