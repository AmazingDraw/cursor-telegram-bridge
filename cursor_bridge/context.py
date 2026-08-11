from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONTEXT_WINDOW = 200_000
PRIOR_CONTEXT_MAX_CHARS = 80_000
_USER_QUERY_RE = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.DOTALL)


@dataclass(frozen=True)
class ContextInfo:
    tokens: int
    window: int
    percent: float
    messages: int
    source: str


def project_slug(cwd: str) -> str:
    """Match Cursor's ``~/.cursor/projects/<slug>`` naming.

    Cursor replaces path separators and spaces with ``-`` (e.g.
    ``/Users/.../GitHub Copilot`` → ``Users-...-GitHub-Copilot``).
    """
    return str(Path(cwd).resolve()).lstrip("/").replace("/", "-").replace(" ", "-")


def session_transcript_path(agent_id: str, cwd: str) -> Path:
    """Expected transcript path for an agent in this session's project folder only."""
    base = Path.home() / ".cursor" / "projects"
    return (
        base
        / project_slug(cwd)
        / "agent-transcripts"
        / agent_id
        / f"{agent_id}.jsonl"
    )


def find_session_transcript(agent_id: str, cwd: str) -> Path | None:
    """Locate a transcript scoped to this session's cwd — no cross-project fallback."""
    if not agent_id:
        return None
    path = session_transcript_path(agent_id, cwd)
    if path.is_file():
        return path
    # Older slug variants (pre space→hyphen) may still exist on disk.
    legacy = (
        Path.home()
        / ".cursor"
        / "projects"
        / str(Path(cwd).resolve()).lstrip("/").replace("/", "-")
        / "agent-transcripts"
        / agent_id
        / f"{agent_id}.jsonl"
    )
    return legacy if legacy.is_file() else None


def find_transcript(agent_id: str, cwd: str) -> Path | None:
    """Locate the agent transcript jsonl under ~/.cursor/projects."""
    direct = find_session_transcript(agent_id, cwd)
    if direct is not None:
        return direct
    if not agent_id:
        return None
    base = Path.home() / ".cursor" / "projects"
    if not base.is_dir():
        return None
    for proj in base.iterdir():
        if not proj.is_dir():
            continue
        path = proj / "agent-transcripts" / agent_id / f"{agent_id}.jsonl"
        if path.is_file():
            return path
    return None


def prior_context_relpath(session_id: str) -> str:
    """Session-specific prior-context file (safe when multiple sessions share a cwd)."""
    return f".cursor_bridge/prior-context-{session_id}.md"


def prior_context_path(cwd: str, session_id: str) -> Path:
    return Path(cwd) / prior_context_relpath(session_id)


@dataclass(frozen=True)
class PriorAgentInfo:
    agent_id: str
    tokens: int
    lines: int
    user_turns: int


@dataclass(frozen=True)
class CondensedTranscript:
    agent_id: str
    user_turns: int
    chars: int
    text: str


def _clean_transcript_text(raw: str) -> str:
    text = _USER_QUERY_RE.sub(lambda m: m.group(1).strip(), raw)
    return text.replace("[REDACTED]", "").strip()


def _content_text(content: object) -> str:
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        cleaned = _clean_transcript_text(str(block.get("text") or ""))
        if cleaned:
            parts.append(cleaned)
    return "\n\n".join(parts)


def condense_transcript(path: Path, *, max_chars: int = PRIOR_CONTEXT_MAX_CHARS) -> CondensedTranscript:
    """Turn raw agent jsonl into user/assistant turns (tool calls omitted)."""
    agent_id = path.parent.name
    turns: list[tuple[str, str]] = []
    pending_assistant: list[str] = []

    def flush_assistant() -> None:
        nonlocal pending_assistant
        if not pending_assistant:
            return
        body = "\n\n".join(pending_assistant)
        turns.append(("assistant", body))
        pending_assistant = []

    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            role = row.get("role")
            content = row.get("message", {}).get("content")
            text = _content_text(content)
            if not text:
                continue
            if role == "user":
                flush_assistant()
                turns.append(("user", text))
            elif role == "assistant":
                pending_assistant.append(text)
    flush_assistant()

    user_turns = sum(1 for role, _ in turns if role == "user")
    body_lines: list[str] = []
    for role, text in turns:
        label = "User" if role == "user" else "Assistant"
        body_lines.append(f"### {label}\n\n{text}")

    body = "\n\n---\n\n".join(body_lines)
    if len(body) > max_chars:
        body = (
            f"*(Earlier turns truncated — showing the most recent ~{max_chars // 4} tokens.)*\n\n"
            + body[-max_chars:]
        )

    return CondensedTranscript(
        agent_id=agent_id,
        user_turns=user_turns,
        chars=len(body),
        text=body,
    )


def build_prior_context_markdown(
    *,
    session_id: str,
    cwd: str,
    agent_id: str,
    condensed: CondensedTranscript,
) -> str:
    return (
        f"# Prior conversation — session {session_id}\n\n"
        f"- **Session:** `{session_id}`\n"
        f"- **Project:** `{cwd}`\n"
        f"- **Restored from agent:** `{agent_id}`\n"
        f"- **User turns:** {condensed.user_turns}\n\n"
        "This file is a condensed transcript from before an agent reset. "
        "Use it for continuity only — do **not** re-run old tool calls.\n\n"
        "---\n\n"
        f"{condensed.text}\n"
    )


def build_context_restore_prompt(session_id: str, cwd: str) -> str:
    rel = prior_context_relpath(session_id)
    return (
        f"Read `{rel}` in this workspace.\n\n"
        f"It contains the prior conversation for **session {session_id}** only "
        f"(project: `{cwd}`), from before an agent reset.\n\n"
        "Summarize what we were working on, what's done, and what's left. "
        "Do not re-run old tool calls — continue from here."
    )


def list_prior_agents(
    prior_agent_ids: list[str],
    cwd: str,
    *,
    exclude_agent_id: str | None = None,
) -> list[PriorAgentInfo]:
    """List prior agents for one session, scoped to its cwd."""
    out: list[PriorAgentInfo] = []
    seen: set[str] = set()
    for agent_id in reversed(prior_agent_ids):
        if agent_id in seen or agent_id == exclude_agent_id:
            continue
        seen.add(agent_id)
        path = find_session_transcript(agent_id, cwd)
        if path is None:
            continue
        tokens, lines = estimate_transcript_usage(path)
        condensed = condense_transcript(path)
        out.append(
            PriorAgentInfo(
                agent_id=agent_id,
                tokens=tokens,
                lines=lines,
                user_turns=condensed.user_turns,
            ),
        )
    return out


def resolve_prior_agent(
    prior_agent_ids: list[str],
    cwd: str,
    agent_id: str | None,
    *,
    exclude_agent_id: str | None = None,
) -> str | None:
    """Pick a prior agent id belonging to this session only."""
    if agent_id:
        if agent_id not in prior_agent_ids or agent_id == exclude_agent_id:
            return None
        return agent_id if find_session_transcript(agent_id, cwd) else None
    for aid in reversed(prior_agent_ids):
        if aid == exclude_agent_id:
            continue
        if find_session_transcript(aid, cwd):
            return aid
    return None


def estimate_transcript_usage(path: Path) -> tuple[int, int]:
    chars = 0
    lines = 0
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            lines += 1
            chars += len(line)
    return chars // 4, lines


def get_context_info(
    agent_id: str | None,
    cwd: str,
    *,
    window: int = DEFAULT_CONTEXT_WINDOW,
) -> ContextInfo | None:
    if not agent_id:
        return None
    path = find_transcript(agent_id, cwd)
    if path is None:
        return ContextInfo(
            tokens=0,
            window=window,
            percent=0.0,
            messages=0,
            source="none",
        )
    tokens, lines = estimate_transcript_usage(path)
    percent = min(100.0, (tokens / window) * 100) if window else 0.0
    return ContextInfo(
        tokens=tokens,
        window=window,
        percent=percent,
        messages=lines,
        source="transcript",
    )


def fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n / 1_000:.0f}k"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def format_context_line(info: ContextInfo | None) -> str:
    if info is None:
        return "Context: (unknown)"
    if info.source == "none":
        return f"Context: 0 / {fmt_tokens(info.window)} (0%)"
    return (
        f"Context: ~{fmt_tokens(info.tokens)} / {fmt_tokens(info.window)} "
        f"({info.percent:.0f}%) \u00b7 {info.messages} transcript lines"
    )


def _bar(percent: float, width: int = 10) -> str:
    filled = round(width * percent / 100)
    return "\u2588" * filled + "\u2591" * (width - filled)


def format_context_html(
    info: ContextInfo | None,
    *,
    session_id: str = "",
    esc: Callable[[object], str],
) -> str:
    """HTML block for Telegram — session context window usage."""
    lines: list[str] = []
    lines.append("<b>Session context</b>")
    if info is None:
        lines.append("No active session.")
        return "\n".join(lines)
    if info.source == "none":
        lines.append(f"<code>{_bar(0)}</code>  0% \u00b7 0 / {fmt_tokens(info.window)} tokens")
        lines.append("<i>No transcript found for this agent yet.</i>")
        return "\n".join(lines)
    lines.append(
        f"<code>{_bar(info.percent)}</code>  {info.percent:.0f}% \u00b7 "
        f"~{fmt_tokens(info.tokens)} / {fmt_tokens(info.window)} tokens"
    )
    lines.append(f"<i>{info.messages} transcript lines \u00b7 use /compact to free space</i>")
    return "\n".join(lines)
