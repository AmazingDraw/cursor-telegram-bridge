from __future__ import annotations

from cursor_bridge.attachments import list_session_files, resolve_attachment, search_session_files


def test_sensitive_paths_are_not_resolved_or_listed(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env_file = workspace / ".env"
    env_file.write_text("SECRET=1", encoding="utf-8")
    git_file = workspace / ".git" / "config"
    git_file.parent.mkdir()
    git_file.write_text("[core]", encoding="utf-8")
    node_file = workspace / "node_modules" / "pkg" / "index.js"
    node_file.parent.mkdir(parents=True)
    node_file.write_text("module.exports = {}", encoding="utf-8")

    assert resolve_attachment(".env", str(workspace)) is None
    assert resolve_attachment(".git/config", str(workspace)) is None
    assert resolve_attachment("node_modules/pkg/index.js", str(workspace)) is None
    assert env_file not in list_session_files(str(workspace))
    assert git_file not in search_session_files(str(workspace), "config")
    assert node_file not in search_session_files(str(workspace), "index")


def test_regular_small_file_is_resolved_and_listed(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    report = workspace / "report.txt"
    report.write_text("hello", encoding="utf-8")

    assert resolve_attachment("report.txt", str(workspace)) == report.resolve()
    assert report in list_session_files(str(workspace))
