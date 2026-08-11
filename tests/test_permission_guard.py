"""Tests for permission_guard (readonly allowlist + cwd confinement)."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from cursor_bridge.permission_guard import (
    evaluate_tool_call,
    is_path_within_cwd,
    tool_paths_from_args,
)


def test_readonly_allows_read_inside_cwd(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("hi", encoding="utf-8")
    assert (
        evaluate_tool_call(
            "Read",
            {"path": str(target)},
            permission="readonly",
            cwd=str(tmp_path),
        )
        is None
    )


def test_readonly_blocks_shell(tmp_path: Path) -> None:
    msg = evaluate_tool_call(
        "Shell",
        {"command": "ls"},
        permission="readonly",
        cwd=str(tmp_path),
    )
    assert msg is not None
    assert "readonly" in msg.lower()


def test_readonly_blocks_write(tmp_path: Path) -> None:
    msg = evaluate_tool_call(
        "Write",
        {"path": str(tmp_path / "x.txt"), "contents": "nope"},
        permission="readonly",
        cwd=str(tmp_path),
    )
    assert msg is not None


def test_readonly_blocks_generate_image(tmp_path: Path) -> None:
    msg = evaluate_tool_call(
        "GenerateImage",
        {"description": "cat"},
        permission="readonly",
        cwd=str(tmp_path),
    )
    assert msg is not None


def test_readonly_blocks_path_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.txt"
    msg = evaluate_tool_call(
        "Read",
        {"path": str(outside)},
        permission="readonly",
        cwd=str(tmp_path),
    )
    assert msg is not None
    assert "escapes" in msg.lower()


def test_readonly_blocks_dotdot_escape(tmp_path: Path) -> None:
    msg = evaluate_tool_call(
        "Read",
        {"path": "../secret.txt"},
        permission="readonly",
        cwd=str(tmp_path),
    )
    assert msg is not None


def test_readonly_blocks_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"outside_{os.getpid()}.txt"
    outside.write_text("secret", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(outside)
        msg = evaluate_tool_call(
            "Read",
            {"path": str(link)},
            permission="readonly",
            cwd=str(tmp_path),
        )
        assert msg is not None
    finally:
        outside.unlink(missing_ok=True)
        link.unlink(missing_ok=True)


def test_full_allows_shell_but_blocks_self_management(tmp_path: Path) -> None:
    assert (
        evaluate_tool_call(
            "Shell",
            {"command": "pytest"},
            permission="full",
            cwd=str(tmp_path),
        )
        is None
    )
    msg = evaluate_tool_call(
        "Shell",
        {"command": "launchctl kickstart -k gui/501/com.cursor-telegram-bridge.bot"},
        permission="full",
        cwd=str(tmp_path),
    )
    assert msg is not None
    assert "Blocked" in msg


def test_is_path_within_cwd_relative(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    assert is_path_within_cwd("sub/a.txt", str(tmp_path))
    assert not is_path_within_cwd("../x", str(tmp_path))


def test_tool_paths_from_args() -> None:
    assert tool_paths_from_args("Read", {"path": "/tmp/a"}) == ["/tmp/a"]
    assert "b" in tool_paths_from_args("x", {"paths": ["a", "b"]})


def test_full_blocks_agent_transcript_read(tmp_path: Path) -> None:
    transcript = (
        Path.home()
        / ".cursor"
        / "projects"
        / "demo"
        / "agent-transcripts"
        / "agent-abc"
        / "agent-abc.jsonl"
    )
    msg = evaluate_tool_call(
        "Read",
        {"path": str(transcript)},
        permission="full",
        cwd=str(tmp_path),
        session_id="s2",
    )
    assert msg is not None
    assert "transcripts are isolated" in msg


def test_full_blocks_agent_transcript_grep(tmp_path: Path) -> None:
    msg = evaluate_tool_call(
        "Grep",
        {
            "path": str(Path.home() / ".cursor" / "projects" / "x" / "agent-transcripts"),
            "pattern": "hello",
        },
        permission="full",
        cwd=str(tmp_path),
        session_id="s1",
    )
    assert msg is not None
    assert "transcripts are isolated" in msg


def test_full_blocks_shell_agent_transcripts(tmp_path: Path) -> None:
    msg = evaluate_tool_call(
        "Shell",
        {"command": "rg 连抽 ~/.cursor/projects/*/agent-transcripts"},
        permission="full",
        cwd=str(tmp_path),
        session_id="s2",
    )
    assert msg is not None
    assert "transcripts are isolated" in msg


def test_readonly_also_blocks_agent_transcripts(tmp_path: Path) -> None:
    msg = evaluate_tool_call(
        "Read",
        {"path": "/tmp/agent-transcripts/foo.jsonl"},
        permission="readonly",
        cwd=str(tmp_path),
        session_id="s1",
    )
    assert msg is not None
    assert "transcripts are isolated" in msg


def test_blocks_other_session_prior_context(tmp_path: Path) -> None:
    foreign = tmp_path / ".cursor_bridge" / "prior-context-s1.md"
    foreign.parent.mkdir(parents=True)
    foreign.write_text("# prior", encoding="utf-8")
    msg = evaluate_tool_call(
        "Read",
        {"path": str(foreign)},
        permission="full",
        cwd=str(tmp_path),
        session_id="s2",
    )
    assert msg is not None
    assert "another session" in msg


def test_allows_own_session_prior_context(tmp_path: Path) -> None:
    own = tmp_path / ".cursor_bridge" / "prior-context-s2.md"
    own.parent.mkdir(parents=True)
    own.write_text("# prior", encoding="utf-8")
    assert (
        evaluate_tool_call(
            "Read",
            {"path": str(own)},
            permission="full",
            cwd=str(tmp_path),
            session_id="s2",
        )
        is None
    )


def test_shell_blocks_foreign_prior_context(tmp_path: Path) -> None:
    msg = evaluate_tool_call(
        "Shell",
        {"command": "cat .cursor_bridge/prior-context-s1.md"},
        permission="full",
        cwd=str(tmp_path),
        session_id="s2",
    )
    assert msg is not None
    assert "another session" in msg


def test_full_allows_normal_project_read(tmp_path: Path) -> None:
    target = tmp_path / "README.md"
    target.write_text("ok", encoding="utf-8")
    assert (
        evaluate_tool_call(
            "Read",
            {"path": str(target)},
            permission="full",
            cwd=str(tmp_path),
            session_id="s2",
        )
        is None
    )
