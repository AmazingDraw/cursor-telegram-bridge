"""Telegram message formatting for agent runs.

Used by sessions.py (live stream) and bot.py (final reply).

Live message layout (HTML, edited in place):
  1. Blockquote header: [sid] folder-name · model
  2. Assistant text preview (truncated)
  3. Activity line: tool-specific with status emoji (🟡 running, ✅ done, ❌ failed)
  4. Optional <pre> snippet: red/green diff lines (🔴/🟢), grep hits, shell output
  5. Elapsed timer (⏳/⌛ Ns) in the header bar, flipping every 5s

Final message: same header + status icon + markdown_to_telegram_html(body).
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any

TELEGRAM_MESSAGE_LIMIT = 4096
_LIVE_BODY_LIMIT = 800
_LIVE_MESSAGE_BUDGET = 3400
_SNIPPET_MAX_LINES = 6
_SNIPPET_MAX_LINE_CHARS = 120
_INLINE_CODE_MAX_CHARS = 160
LIVE_TIMER_INTERVAL_SEC = 3
_TIMER_ICONS = ("⏳", "⌛")

_PLACEHOLDER_CODEBLOCK = "\x00CB"
_PLACEHOLDER_INLINE = "\x00IC"
_PLACEHOLDER_LINK = "\x00LN"
_PLACEHOLDER_TABLE = "\x00TB"


def _normalize_tool(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def parse_tool_args(args: Any) -> dict[str, Any]:
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


def is_create_plan_tool(name: str) -> bool:
    return _normalize_tool(name) == "createplan"


def extract_plan_text(args: Any, result: object | None = None) -> str:
    """Plan mode stores the document in createPlan tool args."""
    plan = _arg_text(parse_tool_args(args), "plan")
    if plan:
        return plan
    if result is not None:
        from_result = _extract_result_text(result).strip()
        if len(from_result) > 80:
            return from_result
    return ""


def plan_from_tool_hits(tool_hits: list[tuple[str, object, object]]) -> str:
    """Return the longest plan seen in a run (createPlan streams partial updates)."""
    best = ""
    for name, args, result in tool_hits:
        if is_create_plan_tool(name):
            plan = extract_plan_text(args, result)
            if len(plan) > len(best):
                best = plan
    return best


def has_create_plan_tool(tool_hits: list[tuple[str, object, object]]) -> bool:
    return any(is_create_plan_tool(name) for name, _a, _r in tool_hits)


def _looks_like_plan_teaser(text: str) -> bool:
    lower = text.lower()
    if len(text) > 420:
        return False
    teasers = (
        "drafting",
        "creating a plan",
        "remediation plan",
        "audit and remediation",
        "findings are validated",
        "will draft",
        "draft a plan",
        "我将为您起草",
        "我来起草",
        "正在制定",
        "稍后给出完整",
        "先整理方案",
        "起草方案",
    )
    return any(t in lower for t in teasers) or (
        "plan" in lower and len(text) < 360
    )


def resolve_final_body(
    *,
    sdk_final: str | None,
    text_parts: list[str],
    tool_hits: list[tuple[str, object, object]],
) -> str:
    """Pick the user-visible body for Telegram — plans must not stay in IDE-only tools.

    Prefer a single createPlan document over assistant+plan doubles. Short
    non-plan conclusions may still preface the tool plan.
    """
    assistant = (sdk_final or "").strip() or "".join(text_parts).strip()
    plan = plan_from_tool_hits(tool_hits).strip()
    if not plan:
        return assistant
    if not assistant:
        return plan
    if plan in assistant:
        # Assistant already embedded the tool plan (possibly with a short lead-in).
        return assistant
    if _looks_like_plan_teaser(assistant) or len(plan) > len(assistant) + 80:
        return plan
    # Model rewrote a full plan in chat while createPlan also has one — keep tool.
    if looks_like_plan_document(assistant):
        return plan
    return f"{assistant}\n\n---\n\n{plan}"


def looks_like_plan_document(text: str) -> bool:
    text = text.strip()
    if len(text) < 80:
        return False
    # Heading-structured markdown is almost always a plan/doc rewrite.
    if re.search(r"^#{1,3}\s", text, re.MULTILINE):
        return True
    if len(text) < 180:
        return False
    markers = (
        "Phase A",
        "Tier 1",
        "## Summary",
        "Recommended order",
        "### ",
        "## 结论",
        "## 方案",
        "## 总览",
        "## 推荐",
    )
    return any(m in text for m in markers)


def apply_plan_to_text_parts(name: str, args: Any, text_parts: list[str]) -> bool:
    """Stream createPlan output into the live assistant preview."""
    if not is_create_plan_tool(name):
        return False
    plan = extract_plan_text(args)
    if not plan:
        return False
    current = "".join(text_parts)
    if len(plan) >= len(current):
        text_parts.clear()
        text_parts.append(plan)
    return True


def _short_path(path: str, *, max_len: int = 48) -> str:
    text = (path or "").strip()
    if not text:
        return "?"
    try:
        text = str(Path(text).name) if len(text) > max_len else text
    except (ValueError, OSError):
        pass
    if len(text) > max_len:
        text = "…" + text[-(max_len - 1):]
    return text


def _arg_text(data: dict[str, Any], *keys: str) -> str:
    for key in keys:
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _truncate_cmd(cmd: str, *, max_len: int = 60) -> str:
    cmd = re.sub(r"\s+", " ", cmd.strip())
    return cmd[: max_len - 1] + "…" if len(cmd) > max_len else cmd


def _tool_path(data: dict[str, Any]) -> str:
    return _short_path(_arg_text(data, "path", "file", "target_file", "file_path"))


_SDK_LIFECYCLE_SUPPRESS = frozenset({
    "creating",
    "running",
    "finished",
    "error",
    "cancelled",
    "expired",
    "unspecified",
})
_SDK_LIFECYCLE_ENUMS = frozenset({
    "ERROR",
    "RUNNING",
    "CREATING",
    "FINISHED",
    "CANCELLED",
    "EXPIRED",
    "RUN_LIFECYCLE_STATUS_ERROR",
    "RUN_LIFECYCLE_STATUS_RUNNING",
    "RUN_LIFECYCLE_STATUS_CREATING",
    "RUN_LIFECYCLE_STATUS_FINISHED",
    "RUN_LIFECYCLE_STATUS_CANCELLED",
    "RUN_LIFECYCLE_STATUS_EXPIRED",
})


def _normalize_sdk_status(status: str) -> str:
    return status.removeprefix("RUN_LIFECYCLE_STATUS_").lower()


def format_sdk_status_activity(status: str, message: str = "") -> str | None:
    """Map Cursor SDK lifecycle status events to a live activity line.

    Returns ``None`` for noisy/transient statuses (e.g. brief ``ERROR`` during
    an otherwise healthy run). Real failures are surfaced when the run ends.
    """
    key = _normalize_sdk_status((status or "").strip())
    detail = (message or "").strip()
    if detail and detail.upper() not in _SDK_LIFECYCLE_ENUMS and detail != status:
        return f"ℹ️ {detail[:100]}"
    if key in _SDK_LIFECYCLE_SUPPRESS:
        return None
    if detail:
        return f"ℹ️ {detail[:100]}"
    return None


def _format_activity_line(activity: str) -> str:
    """Prefix plain-English status lines; leave emoji-led tool/SDK lines alone."""
    if not activity:
        return ""
    if not activity[0].isascii() or not activity[0].isalnum():
        return activity
    return f"⚙ {activity}"


def format_tool_activity(name: str, args: Any, *, done: bool = False) -> str:
    """One-line description of what a tool is doing (🟡) or just did (✅)."""
    tool = _normalize_tool(name)
    data = parse_tool_args(args)
    mark = "✅" if done else "🟡"

    if tool in ("read", "readfile"):
        path = _tool_path(data)
        verb = "read" if done else "reading"
        return f"{mark} {verb} {path}" if path else f"{mark} {verb} file…"

    if tool in ("write", "writefile"):
        path = _tool_path(data)
        verb = "wrote" if done else "writing"
        return f"{mark} {verb} {path}" if path else f"{mark} {verb} file…"

    if tool in ("strreplace", "edit", "searchreplace"):
        path = _tool_path(data)
        verb = "edited" if done else "editing"
        return f"{mark} {verb} {path}" if path else f"{mark} {verb} file…"

    if tool in ("shell", "runterminalcmd", "terminal"):
        cmd = _arg_text(data, "command", "cmd")
        if cmd:
            return f"{mark} shell: {_truncate_cmd(cmd)}"
        return f"{mark} shell…"

    if tool in ("grep", "rg"):
        pattern = _arg_text(data, "pattern", "query", "regex")
        verb = "grep done" if done else "grep"
        return f"{mark} {verb}: {pattern}" if pattern else f"{mark} searching…"

    if tool in ("semanticesearch", "codebasesearch"):
        query = _arg_text(data, "query", "search", "pattern")
        verb = "search done" if done else "search"
        return f"{mark} {verb}: {query[:50]}" if query else f"{mark} semantic search…"

    if tool in ("glob", "globfilesearch", "listdir", "list"):
        pattern = _arg_text(data, "pattern", "glob_pattern", "path", "target_directory")
        verb = "listed" if done else "listing"
        return f"{mark} {verb} {_short_path(pattern)}" if pattern else f"{mark} listing files…"

    if tool in ("generateimage",):
        filename = _arg_text(data, "filename", "file", "path")
        verb = "generated" if done else "generating"
        return f"{mark} {verb} {_short_path(filename)}" if filename else f"{mark} generating image…"

    if tool in ("delete", "deletefile"):
        path = _tool_path(data)
        verb = "deleted" if done else "deleting"
        return f"{mark} {verb} {path}" if path else f"{mark} deleting file…"

    if tool in ("createplan",):
        return f"{mark} plan ready" if done else f"{mark} writing plan…"

    display = (name or "tool").replace("_", " ")
    return f"{mark} {display}{'' if done else '…'}"


def _result_dict(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        data = result
    elif isinstance(result, str):
        try:
            parsed = json.loads(result)
            data = parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            data = {}
    else:
        data = {}
    unwrapped = _unwrap_tool_result(data)
    return unwrapped if isinstance(unwrapped, dict) else data


def _unwrap_tool_result(result: Any) -> Any:
    """Peel common SDK envelopes like ``{status, value}`` or ``{result: ...}``."""
    seen: set[int] = set()
    current = result
    while isinstance(current, dict) and id(current) not in seen:
        seen.add(id(current))
        status = current.get("status")
        if status in ("success", "ok", "completed") and isinstance(current.get("value"), dict):
            current = current["value"]
            continue
        for key in ("value", "result", "data", "output"):
            nested = current.get(key)
            if isinstance(nested, dict) and any(
                isinstance(nested.get(k), str) and nested.get(k, "").strip()
                for k in ("content", "output", "stdout", "text", "message")
            ):
                current = nested
                break
        else:
            break
    return current


def _extract_result_text(result: Any) -> str:
    if result is None:
        return ""
    if isinstance(result, str):
        text = result.strip()
        if text.startswith("{") or text.startswith("["):
            try:
                parsed = json.loads(text)
                return _extract_result_text(parsed)
            except json.JSONDecodeError:
                pass
        return result

    current = _unwrap_tool_result(result)
    if isinstance(current, str):
        return current
    if isinstance(current, dict):
        for key in ("content", "output", "stdout", "text", "result", "message", "data"):
            val = current.get(key)
            if isinstance(val, list):
                parts: list[str] = []
                for block in val:
                    if isinstance(block, dict) and block.get("type") == "text":
                        t = str(block.get("text") or "").strip()
                        if t:
                            parts.append(t)
                    elif isinstance(block, str) and block.strip():
                        parts.append(block.strip())
                if parts:
                    return "\n".join(parts)
            if isinstance(val, str) and val.strip():
                return val
            if isinstance(val, dict):
                nested = _extract_result_text(val)
                if nested.strip():
                    return nested
        path = current.get("path")
        if isinstance(path, str) and path.strip():
            try:
                return f"(read {Path(path).name})"
            except (ValueError, OSError):
                return "(read file)"
    if isinstance(current, dict):
        return ""
    return str(current) if isinstance(current, (int, float, bool)) else ""


def _truncate_line_chars(line: str, max_chars: int = _SNIPPET_MAX_LINE_CHARS) -> str:
    if len(line) <= max_chars:
        return line
    return line[: max_chars - 1] + "…"


def _limit_lines(
    lines: list[str],
    max_lines: int,
    *,
    max_line_chars: int = _SNIPPET_MAX_LINE_CHARS,
) -> list[str]:
    trimmed = [_truncate_line_chars(line, max_line_chars) for line in lines]
    if len(trimmed) <= max_lines:
        return trimmed
    extra = len(trimmed) - max_lines
    return [*trimmed[:max_lines], f"+{extra} lines"]


def _truncate_to_lines(
    text: str,
    max_lines: int = _SNIPPET_MAX_LINES,
    *,
    max_line_chars: int = _SNIPPET_MAX_LINE_CHARS,
) -> str:
    """Keep the first ``max_lines`` of text; append ``+N lines`` when truncated."""
    if not text:
        return ""
    return "\n".join(_limit_lines(text.splitlines(), max_lines, max_line_chars=max_line_chars))


def _pre_code_html(content: str, *, max_lines: int | None = _SNIPPET_MAX_LINES) -> str:
    """Telegram monospace block with optional line cap."""
    text = content.strip()
    if max_lines is not None:
        text = _truncate_to_lines(text, max_lines)
    return f"<pre><code>{html.escape(text)}</code></pre>"


def _looks_like_raw_code(text: str) -> bool:
    """True when plain text is probably code (no markdown fences)."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 4:
        return False
    codeish = 0
    for line in lines:
        stripped = line.lstrip()
        if line[:1] in (" ", "\t") or stripped.startswith((
            "def ", "class ", "import ", "from ", "const ", "let ", "var ",
            "function ", "export ", "return ", "if ", "for ", "while ", "elif ",
            "else:", "try:", "except ", "async ", "await ", "public ", "private ",
            "#!", "#", "//", "/*", "*", "{", "}", ")", ");",
        )):
            codeish += 1
        elif re.match(r"^\s*[\w.]+\s*[=\(:]", line):
            codeish += 1
    return codeish / len(lines) >= 0.45


def _is_unified_diff(text: str) -> bool:
    lines = text.splitlines()
    plus = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
    minus = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
    return plus + minus >= 2


def colorize_diff_lines(text: str, *, max_lines: int = _SNIPPET_MAX_LINES) -> str:
    """Prefix unified-diff lines with red/green emoji markers."""
    out: list[str] = []
    for line in text.splitlines():
        if line.startswith("+++") or line.startswith("---") or line.startswith("@@"):
            out.append(f"🔷 {line}")
        elif line.startswith("+"):
            out.append(f"🟢 {line}")
        elif line.startswith("-"):
            out.append(f"🔴 {line}")
        else:
            out.append(f"   {line}")
    return "\n".join(_limit_lines(out, max_lines))


def colorize_grep_lines(text: str, *, max_lines: int = _SNIPPET_MAX_LINES) -> str:
    out = [f"🟢 {line}" for line in text.splitlines()]
    return "\n".join(_limit_lines(out, max_lines))


def _edit_snippet_from_args(args: Any, *, max_lines: int = _SNIPPET_MAX_LINES) -> str:
    """Red/green mini-diff from StrReplace/Edit tool args."""
    data = parse_tool_args(args)
    old = _arg_text(data, "old_string", "old_str", "oldText", "old_text")
    new = _arg_text(data, "new_string", "new_str", "newText", "new_text")
    if not old and not new:
        return ""

    out: list[str] = []
    for line in old.splitlines() or [old]:
        out.append(f"🔴 -{line}")
    for line in new.splitlines() or [new]:
        out.append(f"🟢 +{line}")
    return "\n".join(_limit_lines(out, max_lines))


def _write_snippet_from_args(args: Any, *, max_lines: int = _SNIPPET_MAX_LINES) -> str:
    """Green lines for new file content from Write tool args."""
    data = parse_tool_args(args)
    content = _arg_text(data, "content", "contents", "text", "body", "code")
    if not content:
        return ""
    out = [f"🟢 +{line}" for line in content.splitlines()]
    return "\n".join(_limit_lines(out, max_lines))


def _shell_snippet(result: Any, *, max_lines: int = _SNIPPET_MAX_LINES) -> str:
    """Color shell stdout green and stderr red."""
    data = _result_dict(result)
    stdout = (data.get("stdout") or data.get("output") or "").strip()
    stderr = (data.get("stderr") or "").strip()
    exit_code = data.get("exit_code", data.get("exitCode", data.get("code")))

    if not stdout and not stderr and not data:
        text = _extract_result_text(result).strip()
        if not text:
            return ""
        if _is_unified_diff(text):
            return colorize_diff_lines(text, max_lines=max_lines)
        lines = [f"🟢 {line}" for line in text.splitlines()]
        return "\n".join(_limit_lines(lines, max_lines))

    out: list[str] = []
    if exit_code not in (None, 0, "0"):
        out.append(f"🔴 exit {exit_code}")
    for line in stderr.splitlines():
        out.append(f"🔴 {line}")
    for line in stdout.splitlines():
        out.append(f"🟢 {line}")
    if not out:
        out.append("🟢 (no output)")
    return "\n".join(_limit_lines(out, max_lines))


def format_tool_error_snippet(result: Any, *, max_lines: int = _SNIPPET_MAX_LINES) -> str:
    text = _extract_result_text(result).strip()
    if not text:
        return ""
    lines = [f"🔴 {line}" for line in text.splitlines()]
    return "\n".join(_limit_lines(lines, max_lines))


def format_tool_result_snippet(
    name: str,
    result: Any,
    args: Any = None,
    *,
    max_lines: int = _SNIPPET_MAX_LINES,
) -> str:
    """Colored snippet for live display after a tool completes."""
    tool = _normalize_tool(name)

    if tool in ("strreplace", "edit", "searchreplace"):
        snippet = _edit_snippet_from_args(args, max_lines=max_lines)
        if snippet:
            return snippet

    if tool in ("write", "writefile"):
        snippet = _write_snippet_from_args(args, max_lines=max_lines)
        if snippet:
            return snippet

    if tool in ("createplan",):
        plan = extract_plan_text(args)
        if plan:
            return "\n".join(_limit_lines(plan.splitlines(), max_lines))

    text = _extract_result_text(result).strip()
    if text and _is_unified_diff(text):
        return colorize_diff_lines(text, max_lines=max_lines)

    if tool in ("grep", "rg") and text:
        return colorize_grep_lines(text, max_lines=max_lines)

    if tool in ("shell", "runterminalcmd", "terminal"):
        return _shell_snippet(result, max_lines=max_lines)

    if tool not in (
        "read", "readfile", "semanticesearch", "codebasesearch",
    ):
        return ""

    if not text:
        return ""

    lines = [f"   {line}" for line in text.splitlines()]
    return "\n".join(_limit_lines(lines, max_lines))


def _timer_emoji(elapsed: int) -> str:
    """Flip between hourglass icons every ``LIVE_TIMER_INTERVAL_SEC``."""
    slot = elapsed // LIVE_TIMER_INTERVAL_SEC
    return _TIMER_ICONS[slot % len(_TIMER_ICONS)]


def _session_header_html(
    short_id: str,
    name: str,
    model: str = "",
    *,
    elapsed: int | None = None,
) -> str:
    header = f"<blockquote><b>{html.escape(name)}</b>"
    if model:
        header += f" · <code>{html.escape(model)}</code>"
    if elapsed is not None:
        header += f" · {_timer_emoji(elapsed)} {elapsed}s"
    return header + "</blockquote>"


def _finalize_live_markdown(body: str) -> str:
    """Truncate streaming preview text and close any open markdown fences.

    Deliberately does *not* auto-wrap plain text as a code block — that caused
    the live message to suddenly jump to a huge monospace window once a few
    code-like lines had streamed in.
    """
    body = body.strip()
    if not body:
        return body
    if body.count("```") % 2 == 1:
        body = body.rstrip() + "\n```"
    if len(body) > _LIVE_BODY_LIMIT:
        body = "…\n" + body[-(_LIVE_BODY_LIMIT - 2):]
        if body.count("```") % 2 == 1:
            body = body.rstrip() + "\n```"
    return body


def _cap_live_html(text: str, budget: int = _LIVE_MESSAGE_BUDGET) -> str:
    """Hard cap on live message size — trim oversized <pre> blocks first."""
    if len(text) <= budget:
        return text

    def _shrink_pres(match: re.Match[str]) -> str:
        block = match.group(0)
        if len(block) <= 600:
            return block
        inner = re.search(r"<pre><code>(.*)</code></pre>", block, flags=re.DOTALL)
        if inner is None:
            return block[:600] + "…</code></pre>"
        code = html.unescape(inner.group(1))
        shrunk = _pre_code_html(code, max_lines=4)
        return shrunk

    capped = re.sub(r"<pre><code>.*?</code></pre>", _shrink_pres, text, flags=re.DOTALL)
    if len(capped) <= budget:
        return capped
    # Drop snippet blocks entirely before cutting prose mid-tag.
    capped = re.sub(
        r"\n<pre><code>.*?</code></pre>\s*$",
        "\n<i>…preview trimmed…</i>",
        capped,
        count=1,
        flags=re.DOTALL,
    )
    if len(capped) <= budget:
        return capped
    if capped.startswith("<blockquote>"):
        end = capped.find("</blockquote>")
        if end != -1:
            header = capped[: end + len("</blockquote>")]
            body = capped[end + len("</blockquote>") :].lstrip("\n")
            room = budget - len(header) - len("\n<i>…trimmed…</i>")
            if room > 80:
                return header + "\n" + body[: room - 1] + "…"
    return capped[: budget - len("\n<i>…trimmed…</i>")] + "\n<i>…trimmed…</i>"


def html_to_plain_preview(text: str) -> str:
    """Readable plain text when Telegram rejects HTML."""
    plain = re.sub(
        r"<pre><code>(.*?)</code></pre>",
        lambda m: html.unescape(m.group(1)),
        text,
        flags=re.DOTALL,
    )
    plain = re.sub(r"<blockquote>(.*?)</blockquote>", r"\1", plain, flags=re.DOTALL)
    plain = re.sub(r"<br\s*/?>", "\n", plain, flags=re.IGNORECASE)
    plain = re.sub(r"<[^>]+>", "", plain)
    return html.unescape(plain).strip()


def build_live_html(
    short_id: str,
    name: str,
    model: str,
    text_parts: list[str],
    activity: str,
    snippet: str = "",
    *,
    elapsed: int | None = None,
) -> str:
    """Single live-updating message body (HTML)."""
    parts: list[str] = []

    body = "".join(text_parts).strip()
    if body:
        parts.append(
            markdown_to_telegram_html(
                _finalize_live_markdown(body),
                max_code_lines=_SNIPPET_MAX_LINES,
            )
        )

    if activity:
        mapped = format_sdk_status_activity(activity)
        if mapped is not None:
            activity = mapped
        elif _normalize_sdk_status(activity) in _SDK_LIFECYCLE_SUPPRESS:
            activity = ""
    if activity:
        parts.append(f"<i>{html.escape(_format_activity_line(activity))}</i>")

    if snippet:
        parts.append(_pre_code_html(snippet, max_lines=_SNIPPET_MAX_LINES))

    if not body and not activity and not snippet:
        parts.append("<i>working…</i>")

    return _cap_live_html("\n\n".join(parts))


def _list_indent(depth: int) -> str:
    return "   " * max(depth, 0)


def _indent_depth(leading: str) -> int:
    return len(leading.replace("\t", "    ")) // 2


def _is_table_separator(line: str) -> bool:
    if "|" not in line:
        return False
    cells = _split_table_cells(line)
    if len(cells) < 2:
        return False
    return all(re.fullmatch(r":?-+:?", cell) is not None or not cell for cell in cells)


def _split_table_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _looks_like_table_row(line: str) -> bool:
    stripped = line.strip()
    if not stripped.startswith("|"):
        return False
    cells = _split_table_cells(line)
    return len(cells) >= 2 and any(cell for cell in cells)


def _format_table_card(header: list[str], rows: list[list[str]]) -> str:
    """Structured card-style HTML for Markdown tables in Telegram (matching copilot telegram-bridge)."""
    if not header or not rows:
        return ""

    card_blocks: list[str] = []

    def _fmt_cell(cell: str) -> str:
        cell = cell.strip()
        if not cell:
            return "—"
        cell_html = html.escape(cell)
        cell_html = re.sub(
            r"\[([^\]\n]+)\]\((https?://[^\s\)]+)\)",
            r'<a href="\2">\1</a>',
            cell_html,
        )
        cell_html = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", cell_html)
        cell_html = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"<i>\1</i>", cell_html)
        cell_html = re.sub(r"`([^`]+)`", r"<code>\1</code>", cell_html)
        return cell_html

    clean_headers = [_fmt_cell(h) for h in header]

    for row in rows:
        if not row or not any(c.strip() for c in row):
            continue
        clean_row = [_fmt_cell(c) for c in row]

        first_h = clean_headers[0] if len(clean_headers) > 0 else "项目"
        first_v = clean_row[0] if len(clean_row) > 0 else "—"

        if len(clean_headers) == 1:
            card_blocks.append(f"• <b>{first_h}</b>: {first_v}")
        elif len(clean_headers) == 2:
            second_h = clean_headers[1] if len(clean_headers) > 1 else "内容"
            second_v = clean_row[1] if len(clean_row) > 1 else "—"
            card_blocks.append(
                f"📌 <b>{first_v}</b> ({first_h})\n"
                f"   └ 🔹 <b>{second_h}</b>: {second_v}"
            )
        else:
            sub_items: list[str] = []
            for i in range(1, len(clean_headers)):
                h = clean_headers[i] if i < len(clean_headers) else f"列{i+1}"
                v = clean_row[i] if i < len(clean_row) else "—"
                is_last = (i == len(clean_headers) - 1)
                prefix = "└ 🔸" if is_last else "├ 🔹"
                sub_items.append(f"   {prefix} <b>{h}</b>: {v}")
            sub_str = "\n".join(sub_items)
            card_blocks.append(f"📌 <b>{first_h}</b>: {first_v}\n{sub_str}")

    return "\n\n".join(card_blocks)


def _parse_table_at(lines: list[str], start: int) -> tuple[tuple[list[str], list[list[str]]], int] | tuple[None, int]:
    if start >= len(lines):
        return None, start

    line = lines[start]
    if _looks_like_table_row(line):
        first = _split_table_cells(line)
        idx = start + 1
    elif "|" in line and start + 1 < len(lines) and _is_table_separator(lines[start + 1]):
        first = _split_table_cells(line)
        idx = start + 1
    else:
        return None, start

    if idx < len(lines) and _is_table_separator(lines[idx]):
        header = first
        idx += 1
        rows: list[list[str]] = []
        while idx < len(lines) and _looks_like_table_row(lines[idx]) and not _is_table_separator(lines[idx]):
            rows.append(_split_table_cells(lines[idx]))
            idx += 1
        if rows:
            return (header, rows), idx
        return None, start

    block = [first]
    while idx < len(lines) and _looks_like_table_row(lines[idx]) and not _is_table_separator(lines[idx]):
        block.append(_split_table_cells(lines[idx]))
        idx += 1
    if len(block) >= 2:
        return (block[0], block[1:]), idx
    return None, start


def _extract_markdown_tables(text: str) -> tuple[str, list[str]]:
    """Replace markdown pipe tables with placeholders for card rendering."""
    lines = text.splitlines()
    out: list[str] = []
    tables: list[str] = []
    idx = 0
    while idx < len(lines):
        parsed, next_idx = _parse_table_at(lines, idx)
        if parsed is not None:
            header, rows = parsed
            tables.append(_format_table_card(header, rows))
            out.append(f"{_PLACEHOLDER_TABLE}{len(tables) - 1}\x00")
            idx = next_idx
            continue
        out.append(lines[idx])
        idx += 1
    return "\n".join(out), tables


def _normalize_document_markdown(text: str) -> str:
    """Pre-process plan-style markdown before HTML conversion."""
    text = text.replace("\r\n", "\n").strip()
    text = re.sub(r"^(\*{3,}|-{3,}|_{3,})\s*$", "━━━━━━━━━━", text, flags=re.MULTILINE)

    lines: list[str] = []
    for line in text.splitlines():
        checkbox = re.match(r"^(\s*)[-*]\s+\[([ xX])\]\s+(.+)$", line)
        if checkbox:
            mark = "☑" if checkbox.group(2).lower() == "x" else "☐"
            lines.append(
                f"{_list_indent(_indent_depth(checkbox.group(1)))}• {mark} {checkbox.group(3)}"
            )
            continue
        bullet = re.match(r"^(\s+)[-*]\s+(.+)$", line)
        if bullet:
            lines.append(
                f"{_list_indent(_indent_depth(bullet.group(1)))}• {bullet.group(2)}"
            )
            continue
        numbered = re.match(r"^(\s*)(\d+)\.\s+(.+)$", line)
        if numbered:
            lines.append(
                f"{_list_indent(_indent_depth(numbered.group(1)))}"
                f"{numbered.group(2)}. {numbered.group(3)}"
            )
            continue
        if re.match(r"^[-*]\s+", line):
            lines.append("• " + line.split(None, 1)[1])
            continue
        lines.append(line)

    # Breath between consecutive list items (Telegram collapses tight stacks).
    spaced: list[str] = []
    for line in lines:
        if (
            spaced
            and spaced[-1] != ""
            and _is_document_list_line(spaced[-1])
            and _is_document_list_line(line)
        ):
            spaced.append("")
        spaced.append(line)

    text = "\n".join(spaced)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def _is_document_list_line(line: str) -> bool:
    return bool(re.match(r"^\s*(?:•|☐|☑|\d+\.)\s+\S", line))


def _bracket_major_heading(title: str) -> str:
    """Wrap # / ## titles in 【】 for Telegram plan docs (idempotent)."""
    text = title.strip()
    if text.startswith("【") and text.endswith("】") and len(text) >= 2:
        return text
    return f"【{text}】"


def markdown_to_telegram_html(text: str, *, max_code_lines: int | None = None) -> str:
    """Convert agent markdown to Telegram-safe HTML.

    When ``max_code_lines`` is set (live preview), fenced blocks are capped and
    always rendered as ``<pre><code>`` with a ``+N lines`` footer.
    """
    document = max_code_lines is None
    text, tables = _extract_markdown_tables(text)
    if document:
        text = _normalize_document_markdown(text)

    code_blocks: list[str] = []
    inline_codes: list[str] = []
    links: list[tuple[str, str]] = []

    def _save_code_block(match: re.Match[str]) -> str:
        # Both fence patterns have a single capture group; use groups() so an
        # empty block (e.g. ```python\n```) never falls through to group(2).
        code_blocks.append(match.groups()[0])
        return f"{_PLACEHOLDER_CODEBLOCK}{len(code_blocks) - 1}\x00"

    def _save_inline_code(match: re.Match[str]) -> str:
        inline_codes.append(match.group(1))
        return f"{_PLACEHOLDER_INLINE}{len(inline_codes) - 1}\x00"

    def _save_link(match: re.Match[str]) -> str:
        links.append((match.group(1), match.group(2)))
        return f"{_PLACEHOLDER_LINK}{len(links) - 1}\x00"

    text = re.sub(r"```\w*\n(.*?)```", _save_code_block, text, flags=re.DOTALL)
    text = re.sub(r"```(.*?)```", _save_code_block, text, flags=re.DOTALL)
    text = re.sub(r"`([^`]+)`", _save_inline_code, text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", _save_link, text)

    text = html.escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"<i>\1</i>", text)

    if document:
        # Major headings (# / ##): blank lines + bold + 【】 so TG plans skim well.
        def _major_heading(match: re.Match[str]) -> str:
            return f"\n\n<b>{_bracket_major_heading(match.group(1))}</b>\n\n"

        text = re.sub(r"^# (.+)$", _major_heading, text, flags=re.MULTILINE)
        text = re.sub(r"^## (.+)$", _major_heading, text, flags=re.MULTILINE)
        text = re.sub(r"^### (.+)$", r"\n\n<b><i>\1</i></b>\n\n", text, flags=re.MULTILINE)
        text = re.sub(r"^#### (.+)$", r"\n\n<b><i>\1</i></b>\n\n", text, flags=re.MULTILINE)
        text = re.sub(r"^#{5,6} (.+)$", r"\n\n\1\n\n", text, flags=re.MULTILINE)
        text = re.sub(r"^(\d+)\.\s+", r"\n\1. ", text, flags=re.MULTILINE)
    else:
        text = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)
        text = re.sub(r"^[-*]\s+", "• ", text, flags=re.MULTILINE)

    def _blockquote(match: re.Match[str]) -> str:
        content = re.sub(r"^&gt;\s?", "", match.group(0), flags=re.MULTILINE)
        return f"<blockquote>{content.strip()}</blockquote>"

    text = re.sub(
        r"^&gt;\s?.+(?:\n&gt;\s?.+)*", _blockquote, text, flags=re.MULTILINE,
    )

    for i, code in enumerate(code_blocks):
        block_html = _pre_code_html(code, max_lines=max_code_lines)
        text = text.replace(f"{_PLACEHOLDER_CODEBLOCK}{i}\x00", block_html)
    for i, code in enumerate(inline_codes):
        if max_code_lines is not None and len(code) > _INLINE_CODE_MAX_CHARS:
            code = code[: _INLINE_CODE_MAX_CHARS - 1] + "…"
        escaped = html.escape(code)
        text = text.replace(f"{_PLACEHOLDER_INLINE}{i}\x00", f"<code>{escaped}</code>")
    for i, (label, url) in enumerate(links):
        escaped_label = html.escape(label)
        escaped_url = html.escape(url)
        text = text.replace(
            f"{_PLACEHOLDER_LINK}{i}\x00",
            f'<a href="{escaped_url}">{escaped_label}</a>',
        )
    for i, table in enumerate(tables):
        text = text.replace(f"{_PLACEHOLDER_TABLE}{i}\x00", table)

    if document:
        text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def build_final_html(
    short_id: str,
    name: str,
    model: str,
    status_icon: str,
    body_md: str,
    *,
    mode: str = "agent",
) -> str:
    """Formatted final reply after a run completes."""
    body_html = markdown_to_telegram_html(body_md) if body_md.strip() else "<i>(no output)</i>"
    footer_parts: list[str] = []
    if status_icon and status_icon != "✅":
        footer_parts.append(status_icon)

    footer = f"\n\n{' · '.join(footer_parts)}" if footer_parts else ""
    return f"{body_html}{footer}"


def chunk_telegram_html(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    """Split HTML message into Telegram-sized chunks."""
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        window = remaining[:limit]
        split_at = -1
        for marker in ("</pre>", "</blockquote>", "\n\n"):
            pos = window.rfind(marker)
            if pos > split_at:
                split_at = pos + len(marker) if marker.startswith("</") else pos
        if split_at <= 0:
            split_at = window.rfind("\n")
        if split_at <= 0:
            split_at = limit
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    return chunks
