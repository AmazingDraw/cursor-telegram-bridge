from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Awaitable, Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .context import project_slug

logger = logging.getLogger("cursor_bridge.attachments")

# Telegram Bot API limits (bytes).
MAX_PHOTO_BYTES = 10 * 1024 * 1024
MAX_DOCUMENT_BYTES = 50 * 1024 * 1024

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
DOCUMENT_EXTENSIONS = {
    ".md", ".txt", ".pdf", ".json", ".csv", ".html", ".htm", ".xml",
    ".yaml", ".yml", ".zip", ".tar", ".gz", ".py", ".js", ".ts", ".tsx",
    ".jsx", ".css", ".svg", ".mp4", ".mov", ".mp3", ".wav", ".doc", ".docx",
    ".xls", ".xlsx", ".ppt", ".pptx",
}

# Never push secrets or huge dependency trees.
BLOCKED_PARTS = {
    ".env", ".git", "node_modules", ".venv", "venv", "__pycache__",
    ".cursor", "state.vscdb",
}

LIST_MAX_DEPTH = 4

_EXT_GROUP = (
    r"png|jpe?g|webp|gif|svg|md|txt|pdf|json|csv|html?|ya?ml|"
    r"py|js|ts|tsx|jsx|css|zip|tar|gz|mp4|mov|mp3|wav|docx?|xlsx?|pptx?"
)
_PATH_IN_TEXT = re.compile(
    r"(?:"
    r"`([^`]+)`"
    r"|\*\*([^*]+)\*\*"
    r"|([~/][\w./\- ]+\.(?:" + _EXT_GROUP + r"))"
    r"|((?:[\w.-]+/)+[\w.-]+\.(?:" + _EXT_GROUP + r"))"
    r"|\b([\w.-]+\.(?:" + _EXT_GROUP + r"))\b"
    r")",
    re.IGNORECASE,
)


def _cursor_mirror_roots(cwd: str) -> list[Path]:
    slug = project_slug(cwd)
    base = Path.home() / ".cursor" / "projects" / slug
    return [base, base / "assets"]


def _allowed_roots(cwd: str) -> list[Path]:
    roots = [Path(cwd).resolve()]
    for p in _cursor_mirror_roots(cwd):
        if p.exists():
            roots.append(p.resolve())
    return roots


def _is_blocked(path: Path) -> bool:
    parts = set(path.parts)
    if parts & BLOCKED_PARTS:
        return True
    name = path.name.lower()
    if name.startswith(".env"):
        return True
    return False


def resolve_attachment(raw: str, cwd: str) -> Path | None:
    """Resolve and validate a candidate path under the session workspace."""
    if not raw or not str(raw).strip():
        return None
    cleaned = str(raw).strip().strip("\"'`")
    if not cleaned or "\n" in cleaned:
        return None
    p = Path(cleaned).expanduser()
    if not p.is_absolute():
        p = Path(cwd) / p
    try:
        resolved = p.resolve()
    except OSError:
        return None
    if not resolved.is_file() or _is_blocked(resolved):
        return None
    if not any(resolved == root or root in resolved.parents for root in _allowed_roots(cwd)):
        return None
    return resolved


def _paths_from_mapping(data: Any, cwd: str) -> list[Path]:
    found: list[Path] = []
    if isinstance(data, dict):
        for key in ("path", "file_path", "filepath", "filename", "file", "output", "target"):
            val = data.get(key)
            if isinstance(val, str):
                p = resolve_attachment(val, cwd)
                if p:
                    found.append(p)
        for val in data.values():
            found.extend(_paths_from_mapping(val, cwd))
    elif isinstance(data, list):
        for item in data:
            found.extend(_paths_from_mapping(item, cwd))
    elif isinstance(data, str):
        for match in _PATH_IN_TEXT.finditer(data):
            for group in match.groups():
                if group:
                    p = resolve_attachment(group, cwd)
                    if p:
                        found.append(p)
    return found


def _normalize_tool(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


# Only these tools auto-send to Telegram (live + end-of-run). Read/Write/Edit/etc. do not.
_AUTO_SEND_TOOLS = frozenset({"generateimage"})


def tool_auto_sends(name: str) -> bool:
    return _normalize_tool(name) in _AUTO_SEND_TOOLS


def paths_from_tool(name: str, args: Any, result: Any, cwd: str) -> list[Path]:
    """Extract sendable file paths from a completed tool call (broad; not for auto-delivery)."""
    found: list[Path] = []
    tool = (name or "").lower()

    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {"raw": args}

    if isinstance(args, dict):
        found.extend(_paths_from_mapping(args, cwd))
        if tool in ("generateimage", "generate_image"):
            filename = args.get("filename")
            if isinstance(filename, str):
                for root in _allowed_roots(cwd):
                    for candidate in (root / filename, root / "assets" / filename):
                        if candidate.is_file() and not _is_blocked(candidate):
                            found.append(candidate.resolve())

    if result is not None:
        if isinstance(result, str):
            try:
                parsed = json.loads(result)
                found.extend(_paths_from_mapping(parsed, cwd))
            except json.JSONDecodeError:
                found.extend(_paths_from_mapping(result, cwd))
        else:
            found.extend(_paths_from_mapping(result, cwd))

    return found


def _parse_tool_args(args: Any) -> dict[str, Any]:
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return {}


def _path_within_roots(path: Path, roots: list[Path]) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return any(resolved == root or root in resolved.parents for root in roots)


def _generate_image_candidates(args: Any, cwd: str) -> list[Path]:
    """Likely output paths from GenerateImage args (may not exist on disk yet)."""
    data = _parse_tool_args(args)
    names: list[str] = []
    for key in ("filename", "file", "path", "filepath", "file_path", "output"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            names.append(val.strip())

    candidates: list[Path] = []
    seen: set[str] = set()
    roots = _allowed_roots(cwd)
    for name in names:
        # Never join absolute / ..-escaping paths as-is — Path(root)/"/etc/x"
        # discards root on POSIX. Only keep basename + safe relative forms.
        raw = Path(name)
        basename = raw.name
        if not basename or basename in (".", ".."):
            continue
        rels: list[str] = [basename, f"assets/{basename}"]
        if not raw.is_absolute() and ".." not in raw.parts:
            rels.append(str(raw))
            if not str(raw).startswith("assets/"):
                rels.append(f"assets/{raw.as_posix()}")
        for root in roots:
            for rel in rels:
                try:
                    p = (root / rel).resolve()
                except OSError:
                    continue
                if not _path_within_roots(p, roots):
                    continue
                key = str(p)
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(p)
    return candidates


def resolve_generate_image_paths(args: Any, result: Any, cwd: str) -> list[Path]:
    """Resolved on-disk paths from a completed GenerateImage call."""
    found: list[Path] = []
    seen: set[str] = set()
    roots = _allowed_roots(cwd)

    def add(path: Path | None) -> None:
        if path is None:
            return
        try:
            resolved = path.resolve()
        except OSError:
            return
        key = str(resolved)
        if key in seen:
            return
        seen.add(key)
        if not _path_within_roots(resolved, roots):
            return
        if resolved.is_file() and not _is_blocked(resolved) and is_sendable(resolved):
            found.append(resolved)

    for candidate in _generate_image_candidates(args, cwd):
        add(candidate)

    payloads: list[Any] = []
    if isinstance(result, dict):
        payloads.append(result)
    elif isinstance(result, str):
        stripped = result.strip()
        if stripped.startswith("{"):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, dict):
                    payloads.append(parsed)
            except json.JSONDecodeError:
                pass
        for p in paths_from_text(result, cwd):
            add(p)

    for data in payloads:
        for key in ("path", "file_path", "filepath", "filename", "file", "output"):
            val = data.get(key)
            if isinstance(val, str):
                add(resolve_attachment(val, cwd))

    return found


def newest_session_image_since(cwd: str, since: float) -> Path | None:
    """Newest sendable image under the session folder with mtime >= since."""
    root = Path(cwd).resolve()
    best: Path | None = None
    best_m = since
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            _prune_dirnames(dirnames)
            for name in filenames:
                p = Path(dirpath) / name
                if not _path_allowed_for_listing(p, cwd):
                    continue
                if classify_attachment(p) not in ("photo", "animation"):
                    continue
                try:
                    mtime = p.stat().st_mtime
                except OSError:
                    continue
                if mtime >= since and mtime >= best_m:
                    best, best_m = p, mtime
    except (PermissionError, OSError):
        pass
    return best.resolve() if best else None


def paths_from_tool_auto_send(
    name: str,
    args: Any,
    result: Any,
    cwd: str,
    *,
    run_started_at: float | None = None,
) -> list[Path]:
    """Files to deliver automatically after a tool completes (whitelist only)."""
    if not tool_auto_sends(name):
        return []
    found = resolve_generate_image_paths(args, result, cwd)
    if not found and run_started_at is not None:
        img = newest_session_image_since(cwd, run_started_at - 2.0)
        if img:
            found = [img]
    return found


_LIVE_POLL_DELAYS_S = (0, 1, 2, 3, 5, 8, 13, 21, 34)


async def deliver_generate_image_live(
    name: str,
    args: Any,
    result: Any,
    cwd: str,
    on_attachment: Callable[[Path], Awaitable[bool]],
    sent_paths: set[str],
    *,
    run_started_at: float,
) -> list[Path]:
    """Poll until a GenerateImage output file exists, then send it live."""
    if not tool_auto_sends(name):
        return []

    delivered: list[Path] = []
    for delay in _LIVE_POLL_DELAYS_S:
        if delay:
            await asyncio.sleep(delay)
        paths = resolve_generate_image_paths(args, result, cwd)
        if not paths and delay >= 3:
            img = newest_session_image_since(cwd, run_started_at - 2.0)
            if img:
                paths = [img]
        for p in paths:
            key = str(p.resolve())
            if key in sent_paths:
                continue
            try:
                ok = await on_attachment(p)
            except Exception:
                logger.warning(
                    "Live attachment callback failed for %s: %s",
                    p, name, exc_info=True,
                )
                ok = False
            if ok:
                sent_paths.add(key)
                delivered.append(p)
        if delivered:
            break
    return delivered


def paths_from_text(text: str, cwd: str) -> list[Path]:
    found: list[Path] = []
    for match in _PATH_IN_TEXT.finditer(text or ""):
        for group in match.groups():
            if group:
                p = resolve_attachment(group, cwd)
                if p:
                    found.append(p)
    return found


def classify_attachment(path: Path) -> str:
    """Return ``photo``, ``animation``, or ``document``."""
    ext = path.suffix.lower()
    if ext == ".gif":
        return "animation"
    if ext in IMAGE_EXTENSIONS:
        return "photo"
    return "document"


def is_sendable(path: Path) -> bool:
    ext = path.suffix.lower()
    if ext in IMAGE_EXTENSIONS or ext in DOCUMENT_EXTENSIONS:
        return True
    # Allow other extensions if small enough to be a reasonable attachment.
    try:
        return path.stat().st_size <= MAX_DOCUMENT_BYTES
    except OSError:
        return False


def max_bytes_for(path: Path) -> int:
    return MAX_PHOTO_BYTES if classify_attachment(path) == "photo" else MAX_DOCUMENT_BYTES


def dedupe_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    out: list[Path] = []
    for p in paths:
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        if is_sendable(p):
            out.append(p)
    return out


def collect_run_attachments(
    *,
    cwd: str,
    tool_hits: list[tuple[str, Any, Any]],
    texts: list[str],
    run_started_at: float | None = None,
) -> list[Path]:
    """Outbound attachments at end of run — GenerateImage only; not reads or text mentions."""
    del texts  # unused; manual delivery via /files
    found: list[Path] = []
    for name, args, result in tool_hits:
        found.extend(
            paths_from_tool_auto_send(
                name, args, result, cwd, run_started_at=run_started_at,
            )
        )
    return dedupe_paths(found)


def _path_allowed_for_listing(path: Path, cwd: str) -> bool:
    root = Path(cwd).resolve()
    try:
        resolved = path.resolve()
    except OSError:
        return False
    if not resolved.is_file():
        return False
    if resolved != root and root not in resolved.parents:
        return False
    if _is_blocked(resolved) or not is_sendable(resolved):
        return False
    for parent in resolved.parents:
        if parent == root:
            break
        if _is_blocked(parent):
            return False
    return True


def _prune_dirnames(dirnames: list[str]) -> None:
    dirnames[:] = [
        name for name in dirnames
        if name not in BLOCKED_PARTS
        and (not name.startswith(".") or name == ".cursor_bridge")
    ]


def search_session_files(cwd: str, query: str, *, limit: int = 20) -> list[Path]:
    """All sendable files under cwd whose relative path contains ``query`` (case-insensitive)."""
    needle = (query or "").strip().lower()
    if not needle:
        return []
    root = Path(cwd).resolve()
    found: list[Path] = []
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            _prune_dirnames(dirnames)
            for name in filenames:
                p = Path(dirpath) / name
                if not _path_allowed_for_listing(p, cwd):
                    continue
                try:
                    rel = str(p.relative_to(root)).lower()
                except ValueError:
                    rel = p.name.lower()
                if needle in rel or needle in name.lower():
                    found.append(p)
    except (PermissionError, OSError):
        pass
    found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return found[:limit]


def list_session_files(cwd: str, *, limit: int = 20) -> list[Path]:
    root = Path(cwd).resolve()
    found: list[Path] = []
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            rel = Path(dirpath).relative_to(root)
            depth = 0 if rel == Path(".") else len(rel.parts)
            if depth > LIST_MAX_DEPTH:
                dirnames.clear()
                continue
            _prune_dirnames(dirnames)
            for name in filenames:
                p = Path(dirpath) / name
                if _path_allowed_for_listing(p, cwd):
                    found.append(p)
    except (PermissionError, OSError):
        pass
    found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return found[:limit]


def list_dir_files(dir_path: str, cwd: str, *, limit: int = 20) -> list[Path]:
    """Non-recursive: files directly in dir_path."""
    d = Path(dir_path)
    found: list[Path] = []
    try:
        for child in sorted(d.iterdir(), key=lambda c: c.name.lower()):
            if child.is_file() and _path_allowed_for_listing(child, cwd):
                found.append(child)
    except (PermissionError, OSError):
        pass
    found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return found[:limit]


def file_label(path: Path, cwd: str) -> str:
    try:
        name = str(path.relative_to(Path(cwd).resolve()))
    except ValueError:
        name = path.name
    kind = classify_attachment(path)
    prefix = "\U0001F5BC" if kind in ("photo", "animation") else "\U0001F4C4"
    label = f"{prefix} {name}"
    if len(label) > 40:
        label = label[:37] + "\u2026"
    return label


def files_keyboard(
    files: list[Path],
    cwd: str,
    tokens: object,
    page_size: int,
) -> InlineKeyboardMarkup:
    from .folders import TokenStore

    assert isinstance(tokens, TokenStore)
    rows: list[list[InlineKeyboardButton]] = []
    if not files:
        rows.append([InlineKeyboardButton("未找到文件", callback_data="noop")])
    else:
        for path in files[:page_size]:
            rows.append([
                InlineKeyboardButton(
                    file_label(path, cwd),
                    callback_data=f"fsend:{tokens.token(str(path))}",
                )
            ])
    rows.append([
        InlineKeyboardButton(
            "\U0001F4C1 浏览文件夹",
            callback_data=f"fdir:{tokens.token(cwd)}",
        )
    ])
    return InlineKeyboardMarkup(rows)


def files_dir_keyboard(
    dir_path: str,
    cwd: str,
    tokens: object,
    page_size: int,
) -> InlineKeyboardMarkup:
    from .folders import TokenStore

    assert isinstance(tokens, TokenStore)
    p = Path(dir_path)
    root = Path(cwd).resolve()
    rows: list[list[InlineKeyboardButton]] = []

    rows.append([
        InlineKeyboardButton(
            "\U0001F4C4 发送当前目录下的文件",
            callback_data=f"ffiles:{tokens.token(dir_path)}",
        )
    ])

    parent = p.parent
    if str(parent) != str(p):
        try:
            parent_resolved = parent.resolve()
            if parent_resolved == root or root in parent_resolved.parents:
                rows.append([
                    InlineKeyboardButton(
                        "\u2B06\uFE0F 返回上级目录 ..",
                        callback_data=f"fdir:{tokens.token(str(parent))}",
                    )
                ])
        except OSError:
            pass

    try:
        dirs = sorted(
            (
                c for c in p.iterdir()
                if c.is_dir()
                and c.name not in BLOCKED_PARTS
                and (not c.name.startswith(".") or c.name == ".cursor_bridge")
            ),
            key=lambda c: c.name.lower(),
        )
    except (PermissionError, OSError):
        dirs = []

    for child in dirs[:page_size]:
        rows.append([
            InlineKeyboardButton(
                f"\U0001F4C1 {child.name}",
                callback_data=f"fdir:{tokens.token(str(child))}",
            )
        ])

    return InlineKeyboardMarkup(rows)
