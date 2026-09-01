"""Hard tool permission gate: readonly allowlist + cwd path confinement."""

from __future__ import annotations

import os
import re
from pathlib import Path

from .formatting import _arg_text, _normalize_tool, parse_tool_args

# Normalized tool names allowed when permission == "readonly".
# Images arrive via Telegram inbound (bridge), not GenerateImage.
READONLY_ALLOWED_TOOLS = frozenset({
    "read",
    "readfile",
    "grep",
    "rg",
    "glob",
    "globfilesearch",
    "listdir",
    "list",
    "semanticesearch",
    "codebasesearch",
})

_PATH_KEYS = (
    "path",
    "file",
    "target_file",
    "file_path",
    "target_directory",
    "working_directory",
    "cwd",
)

_AGENT_TRANSCRIPTS_MARKER = "agent-transcripts"
_PRIOR_CONTEXT_RE = re.compile(r"prior-context-([A-Za-z0-9_-]+)", re.IGNORECASE)


def shell_command_from_args(name: str, args: object) -> str:
    if _normalize_tool(name) not in ("shell", "runterminalcmd", "terminal"):
        return ""
    return _arg_text(parse_tool_args(args), "command", "cmd")


def tool_paths_from_args(name: str, args: object) -> list[str]:
    """Collect path-like arguments from a tool call (best-effort)."""
    data = parse_tool_args(args)
    paths: list[str] = []
    for key in _PATH_KEYS:
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            paths.append(val.strip())
    for key in ("paths", "files"):
        val = data.get(key)
        if isinstance(val, list):
            for item in val:
                if isinstance(item, str) and item.strip():
                    paths.append(item.strip())
    return paths


def is_path_within_cwd(path_str: str, cwd: str) -> bool:
    """True when ``path_str`` resolves inside ``cwd`` (symlinks followed)."""
    raw = (path_str or "").strip()
    if not raw:
        return True
    try:
        base = Path(os.path.realpath(os.path.expanduser(cwd)))
        candidate = Path(os.path.expanduser(raw))
        if not candidate.is_absolute():
            candidate = base / candidate
        resolved = Path(os.path.realpath(str(candidate)))
        resolved.relative_to(base)
        return True
    except (ValueError, OSError):
        return False


def _normalize_ref(text: str) -> str:
    return (text or "").replace("\\", "/").lower()


def refers_to_agent_transcripts(text: str) -> bool:
    """True when a path/command targets Cursor agent-transcripts storage."""
    return _AGENT_TRANSCRIPTS_MARKER in _normalize_ref(text)


def prior_context_owner(text: str) -> str | None:
    """Return session id embedded in a prior-context filename, if any."""
    match = _PRIOR_CONTEXT_RE.search((text or "").replace("\\", "/"))
    return match.group(1) if match else None


def blocked_readonly_message(name: str) -> str:
    return (
        "Blocked: permission=readonly — this bot may only answer and read "
        "within the session folder.\n"
        f"Denied tool: {name}\n"
        "Shell / write / edit / delete / image generation / MCP are not allowed."
    )


def blocked_path_message(path: str, cwd: str) -> str:
    return (
        "Blocked: path escapes session cwd (permission=readonly).\n"
        f"Path: {path[:240]}\n"
        f"Cwd: {cwd}"
    )


def blocked_transcript_message() -> str:
    return (
        "Blocked: cross-session agent transcripts are isolated — "
        "use /context to restore on purpose."
    )


def blocked_prior_context_message(path: str, session_id: str) -> str:
    return (
        "Blocked: prior-context file belongs to another session "
        f"(active={session_id}).\n"
        f"Path: {path[:240]}\n"
        "Use /context in this session to restore history on purpose."
    )


def _isolation_deny(
    name: str,
    args: object,
    *,
    session_id: str | None,
    cmd: str | None,
) -> str | None:
    """Block cross-session transcript / foreign prior-context access for all perms."""
    haystacks: list[str] = []
    if cmd:
        haystacks.append(cmd)
    haystacks.extend(tool_paths_from_args(name, args))

    for item in haystacks:
        if refers_to_agent_transcripts(item):
            return blocked_transcript_message()
        if session_id:
            owner = prior_context_owner(item)
            if owner and owner != session_id:
                return blocked_prior_context_message(item, session_id)
    return None


def evaluate_tool_call(
    name: str,
    args: object,
    *,
    permission: str,
    cwd: str,
    session_id: str | None = None,
) -> str | None:
    """Return a block message, or ``None`` if the tool call is allowed.

    Always blocks cross-session transcript reads.
    When ``permission == \"readonly\"``, only the read/search allowlist is
    permitted, and every path arg must stay inside ``cwd``.
    """
    cmd = shell_command_from_args(name, args)
    deny = _isolation_deny(name, args, session_id=session_id, cmd=cmd)
    if deny:
        return deny

    perm = (permission or "full").strip().lower()
    if perm != "readonly":
        return None

    tool = _normalize_tool(name)
    if tool not in READONLY_ALLOWED_TOOLS:
        return blocked_readonly_message(name or tool or "tool")

    for path in tool_paths_from_args(name, args):
        if not is_path_within_cwd(path, cwd):
            return blocked_path_message(path, cwd)
    return None
