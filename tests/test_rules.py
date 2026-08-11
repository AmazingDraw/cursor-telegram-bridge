from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from cursor_sdk import UserMessage

from cursor_bridge.config import load_config
from cursor_bridge.rules import RULES_MARKER, strip_rules_prefix, wrap_with_rules


def test_wrap_and_strip_rules() -> None:
    wrapped = wrap_with_rules("你好", "# Persona\n你是小白")
    assert isinstance(wrapped, str)
    assert wrapped.startswith(RULES_MARKER)
    assert "你是小白" in wrapped
    assert wrap_with_rules(wrapped, "# Persona\n你是小白") == wrapped
    assert strip_rules_prefix(wrapped) == "你好"


def test_wrap_rules_preserves_images() -> None:
    img = MagicMock(name="SDKImage")
    msg = UserMessage(text="看图", images=[img])
    wrapped = wrap_with_rules(msg, "rule-a")
    assert isinstance(wrapped, UserMessage)
    assert list(wrapped.images or []) == [img]
    assert wrapped.text.startswith(RULES_MARKER)


def test_rules_file_loaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_1", "telegram-token")
    monkeypatch.setenv("CURSOR_API_KEY", "cursor-key")
    monkeypatch.delenv("ALLOWED_TELEGRAM_USER_ID", raising=False)
    (tmp_path / "rules.md").write_text("你是小白\n", encoding="utf-8")
    (tmp_path / "config.toml").write_text(
        'rules_file = "rules.md"\n',
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    assert cfg.rules_text == "你是小白"
    assert cfg.rules_file == (tmp_path / "rules.md").resolve()
