from __future__ import annotations

from pathlib import Path

import pytest

from cursor_bridge.config import load_config


def test_load_config_defaults_and_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_1", "telegram-token")
    monkeypatch.setenv("CURSOR_API_KEY", "cursor-key")
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_ID", "123456789")

    cfg = load_config(tmp_path)

    assert cfg.telegram_token == "telegram-token"
    assert cfg.cursor_api_key == "cursor-key"
    assert cfg.allowed_user_id == 123456789
    assert cfg.model == "composer-2.5"
    assert cfg.setting_sources == ["user", "project"]
    assert cfg.console_host == "127.0.0.1"
    assert cfg.console_port == 9477
    assert cfg.sessions_file == tmp_path / "state" / "bots" / "default" / "sessions.json"


def test_load_config_toml_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_1", "telegram-token")
    monkeypatch.setenv("CURSOR_API_KEY", "cursor-key")
    monkeypatch.delenv("ALLOWED_TELEGRAM_USER_ID", raising=False)
    (tmp_path / "config.toml").write_text(
        "\n".join(
            [
                'projects_root = "./projects"',
                'model = "composer-2.5"',
                'models = ["composer-2.5", "gpt-5.6-luna"]',
                "browser_page_size = 12",
                "event_log_max = 25",
                "console_enabled = false",
                'console_host = "127.0.0.1"',
                "console_port = 9999",
                "",
                "[[bookmarks]]",
                'name = "Demo"',
                'path = "./demo"',
            ]
        ),
        encoding="utf-8",
    )

    cfg = load_config(tmp_path)

    assert cfg.allowed_user_id is None
    assert cfg.projects_root == (tmp_path / "projects").resolve()
    assert cfg.models == ["composer-2.5", "gpt-5.6-luna"]
    assert cfg.browser_page_size == 12
    assert cfg.event_log_max == 25
    assert cfg.console_enabled is False
    assert cfg.console_port == 9999
    assert cfg.bookmarks[0].name == "Demo"
    assert cfg.bookmarks[0].path == str((tmp_path / "demo").resolve())


def test_invalid_allowed_user_id_exits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_1", "telegram-token")
    monkeypatch.setenv("CURSOR_API_KEY", "cursor-key")
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_ID", "not-a-number")

    with pytest.raises(SystemExit):
        load_config(tmp_path)


def test_load_config_multi_bots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_1", "default-token")
    monkeypatch.setenv("CURSOR_API_KEY", "cursor-key")
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_ID", "100")
    (tmp_path / "config.toml").write_text(
        "\n".join(
            [
                'model = "composer-2.5"',
                "",
                "[[bots]]",
                'name = "BotA"',
                'token = "token-a"',
                "allowed_user_id = 111",
                "",
                "[[bots]]",
                'name = "BotB"',
                'token = "token-b"',
                'model = "grok-4.5"',
            ]
        ),
        encoding="utf-8",
    )

    cfg = load_config(tmp_path)

    assert len(cfg.bots) == 2
    assert cfg.bots[0].name == "BotA"
    assert cfg.bots[0].token == "token-a"
    assert cfg.bots[0].allowed_user_id == 111
    assert cfg.bots[0].model == "composer-2.5"

    assert cfg.bots[1].name == "BotB"
    assert cfg.bots[1].token == "token-b"
    assert cfg.bots[1].allowed_user_id == 100
    assert cfg.bots[1].model == "grok-4.5"


def test_load_config_bot_token_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_1", "default-token")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_2", "second-token")
    monkeypatch.setenv("CURSOR_API_KEY", "cursor-key")
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_ID", "100")
    (tmp_path / "config.toml").write_text(
        "\n".join(
            [
                "[[bots]]",
                'name = "default"',
                'token_env = "TELEGRAM_BOT_TOKEN_1"',
                "",
                "[[bots]]",
                'name = "secondary"',
                'token_env = "TELEGRAM_BOT_TOKEN_2"',
            ]
        ),
        encoding="utf-8",
    )

    cfg = load_config(tmp_path)
    assert [b.name for b in cfg.bots] == ["default", "secondary"]
    assert cfg.bots[0].token == "default-token"
    assert cfg.bots[1].token == "second-token"
    assert cfg.bots[0].allowed_user_id == 100
    assert cfg.bots[1].allowed_user_id == 100
    assert cfg.bots[0].permission == "full"
    assert cfg.bots[1].allowed_chat_ids == []


def test_load_config_bot_permission_and_chats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_1", "default-token")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_2", "second-token")
    monkeypatch.setenv("CURSOR_API_KEY", "cursor-key")
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_ID", "100")
    (tmp_path / "config.toml").write_text(
        "\n".join(
            [
                "[[bots]]",
                'name = "default"',
                'token_env = "TELEGRAM_BOT_TOKEN_1"',
                'permission = "full"',
                "",
                "[[bots]]",
                'name = "group-reader"',
                'token_env = "TELEGRAM_BOT_TOKEN_2"',
                'permission = "readonly"',
                "allowed_user_id = 100",
                "allowed_chat_ids = [-1001234567890, -1009876543210]",
            ]
        ),
        encoding="utf-8",
    )

    cfg = load_config(tmp_path)
    assert cfg.bots[0].permission == "full"
    assert cfg.bots[1].permission == "readonly"
    assert cfg.bots[1].allowed_chat_ids == [-1001234567890, -1009876543210]


def test_load_config_invalid_permission_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_1", "t")
    monkeypatch.setenv("CURSOR_API_KEY", "k")
    (tmp_path / "config.toml").write_text(
        "\n".join(
            [
                "[[bots]]",
                'name = "default"',
                'token = "t"',
                'permission = "nope"',
            ]
        ),
        encoding="utf-8",
    )
    assert load_config(tmp_path).bots[0].permission == "full"


def test_setting_sources_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_1", "telegram-token")
    monkeypatch.setenv("CURSOR_API_KEY", "cursor-key")
    monkeypatch.delenv("ALLOWED_TELEGRAM_USER_ID", raising=False)

    (tmp_path / "config.toml").write_text(
        'setting_sources = ["user"]\n',
        encoding="utf-8",
    )
    assert load_config(tmp_path).setting_sources == ["user"]

    (tmp_path / "config.toml").write_text(
        'setting_sources = "all"\n',
        encoding="utf-8",
    )
    assert load_config(tmp_path).setting_sources == ["all"]

    (tmp_path / "config.toml").write_text(
        "setting_sources = []\n",
        encoding="utf-8",
    )
    assert load_config(tmp_path).setting_sources == []
