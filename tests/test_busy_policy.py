"""Busy-policy prompt queue helpers."""

from __future__ import annotations

from cursor_bridge.sessions import MAX_PROMPT_QUEUE, QueuedPrompt, SessionManager


def test_prompt_queue_fifo_and_clear(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_1", "t")
    monkeypatch.setenv("CURSOR_API_KEY", "k")
    from cursor_bridge.config import load_config

    cfg = load_config(tmp_path)
    # Minimal manager without loading bridges: construct then clear file IO noise
    mgr = SessionManager.__new__(SessionManager)
    mgr._prompt_queues = {}

    a = QueuedPrompt(prompt="a", chat_id=1)
    b = QueuedPrompt(prompt="b", chat_id=1)
    assert mgr.enqueue_prompt("s1", a) == 1
    assert mgr.enqueue_prompt("s1", b) == 2
    assert mgr.queued_count("s1") == 2
    assert mgr.pop_queued_prompt("s1") is a
    assert mgr.pop_queued_prompt("s1") is b
    assert mgr.pop_queued_prompt("s1") is None
    assert mgr.queued_count("s1") == 0

    mgr.enqueue_prompt("s1", a)
    mgr.enqueue_prompt("s1", b)
    assert mgr.clear_prompt_queue("s1") == 2
    assert mgr.queued_count("s1") == 0


def test_prompt_queue_full(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_1", "t")
    monkeypatch.setenv("CURSOR_API_KEY", "k")
    mgr = SessionManager.__new__(SessionManager)
    mgr._prompt_queues = {}
    for i in range(MAX_PROMPT_QUEUE):
        mgr.enqueue_prompt("s1", QueuedPrompt(prompt=str(i), chat_id=1))
    try:
        mgr.enqueue_prompt("s1", QueuedPrompt(prompt="x", chat_id=1))
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_busy_policy_config_default(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_1", "t")
    monkeypatch.setenv("CURSOR_API_KEY", "k")
    from cursor_bridge.config import load_config

    cfg = load_config(tmp_path)
    assert cfg.busy_policy == "queue"

    (tmp_path / "config.toml").write_text('busy_policy = "interrupt"\n', encoding="utf-8")
    cfg2 = load_config(tmp_path)
    assert cfg2.busy_policy == "interrupt"


def test_remove_queued_by_token() -> None:
    mgr = SessionManager.__new__(SessionManager)
    mgr._prompt_queues = {}
    a = QueuedPrompt(prompt="a", chat_id=1, token="aaa")
    b = QueuedPrompt(prompt="b", chat_id=1, token="bbb")
    mgr.enqueue_prompt("s1", a)
    mgr.enqueue_prompt("s1", b)
    assert mgr.remove_queued_by_token("s1", "missing") is None
    assert mgr.remove_queued_by_token("s1", "bbb") is b
    assert mgr.queued_count("s1") == 1
    assert mgr.pop_queued_prompt("s1") is a
