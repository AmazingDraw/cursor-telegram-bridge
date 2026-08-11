from __future__ import annotations

from cursor_bridge.config import Bookmark, Config
from cursor_bridge.folders import TokenStore, projects_keyboard


def test_token_store_round_trip_and_stability() -> None:
    tokens = TokenStore()
    path = "/very/long/path/that/would/not/fit/in/telegram/callback/data"

    first = tokens.token(path)
    second = tokens.token(path)

    assert first == second
    assert tokens.path(first) == path


def test_project_picker_callback_data_stays_short(tmp_path) -> None:
    root = tmp_path / ("a" * 80)
    root.mkdir()
    cfg = Config(
        telegram_token="telegram-token",
        cursor_api_key="cursor-key",
        allowed_user_id=1,
        projects_root=root,
        model="composer-2.5",
        browser_page_size=20,
        bookmarks=[Bookmark("Long", str(root))],
        state_dir=tmp_path / "state",
        event_log_max=500,
        console_enabled=True,
        console_host="127.0.0.1",
        console_port=9477,
        console_token="",
    )

    keyboard = projects_keyboard(cfg, TokenStore())
    callback_values = [
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data
    ]

    assert callback_values
    assert all(len(value.encode("utf-8")) <= 64 for value in callback_values)
