"""Transcript path slug and context restore messaging."""

from __future__ import annotations

from pathlib import Path

from cursor_bridge.context import find_session_transcript, project_slug


def test_project_slug_replaces_spaces() -> None:
    slug = project_slug("~/Projects/GitHub Copilot")
    assert " " not in slug
    assert slug.endswith("GitHub-Copilot")
    assert "GitHub Copilot" not in slug


def test_find_session_transcript_with_space_in_cwd(tmp_path: Path, monkeypatch) -> None:
    # Simulate Cursor's on-disk layout (spaces → hyphens in project slug).
    cwd = tmp_path / "GitHub Copilot"
    cwd.mkdir()
    agent_id = "agent-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    slug = str(cwd.resolve()).lstrip("/").replace("/", "-").replace(" ", "-")
    transcript_dir = (
        tmp_path / "fake-home" / ".cursor" / "projects" / slug / "agent-transcripts" / agent_id
    )
    transcript_dir.mkdir(parents=True)
    transcript = transcript_dir / f"{agent_id}.jsonl"
    transcript.write_text('{"role":"user"}\n', encoding="utf-8")

    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    # Path.home() reads HOME on Unix
    found = find_session_transcript(agent_id, str(cwd))
    assert found is not None
    assert found == transcript
