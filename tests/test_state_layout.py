from __future__ import annotations

from pathlib import Path

from cursor_bridge.config import BotConfig, Config, load_config
from cursor_bridge.state_layout import bot_state_dir, migrate_legacy_default_state
from cursor_bridge.sessions import SessionManager


def _cfg(tmp_path: Path) -> Config:
    return Config(
        telegram_token="t",
        cursor_api_key="k",
        allowed_user_id=1,
        projects_root=tmp_path,
        model="composer-2.5",
        browser_page_size=20,
        bookmarks=[],
        state_dir=tmp_path / "state",
        event_log_max=50,
        console_enabled=False,
        console_host="127.0.0.1",
        console_port=9477,
        console_token="",
        bots=[
            BotConfig(name="default", token="t1", allowed_user_id=1, model="composer-2.5"),
            BotConfig(name="secondary", token="t2", allowed_user_id=1, model="composer-2.5"),
        ],
    )


def test_bot_state_dirs_are_peer_level(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    assert bot_state_dir(cfg, "default") == tmp_path / "state" / "bots" / "default"
    assert bot_state_dir(cfg, "secondary") == tmp_path / "state" / "bots" / "secondary"


def test_migrate_legacy_default_state(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    root = tmp_path / "state"
    root.mkdir()
    legacy = root / "sessions.json"
    legacy.write_text('{"counter": 1, "active": {}, "sessions": []}\n', encoding="utf-8")
    events = root / "events"
    events.mkdir()
    (events / "s1.jsonl").write_text('{"event":"x"}\n', encoding="utf-8")

    assert migrate_legacy_default_state(cfg) is True
    dest = root / "bots" / "default"
    assert (dest / "sessions.json").is_file()
    assert (dest / "events" / "s1.jsonl").is_file()
    assert not legacy.exists()
    assert not events.exists()


def test_session_manager_uses_bot_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_1", "t")
    monkeypatch.setenv("CURSOR_API_KEY", "k")
    cfg = _cfg(tmp_path)
    mgr = SessionManager(cfg, cfg.bots[0])
    assert mgr.state_dir == tmp_path / "state" / "bots" / "default"
    assert mgr.sessions_file == mgr.state_dir / "sessions.json"

    mgr2 = SessionManager(cfg, cfg.bots[1])
    assert mgr2.state_dir == tmp_path / "state" / "bots" / "secondary"
