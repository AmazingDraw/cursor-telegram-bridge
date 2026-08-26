"""Known model quirks surfaced in the Telegram UI."""

from __future__ import annotations

import html

# Models that often return instant empty errors from the Cursor SDK.
PROBLEMATIC_MODELS: frozenset[str] = frozenset({"claude-fable-5"})

RECOMMENDED_MODEL = "gpt-5.6-luna"

# Cursor SDK model parameter id (Claude models).
EFFORT_PARAM = "effort"


def model_picker_label(model: str, *, current: bool = False) -> str:
    """Short label for inline keyboard buttons (64-char Telegram limit)."""
    prefix = "\u2713 " if current else ""
    warn = "\u26a0 " if model in PROBLEMATIC_MODELS and not current else ""
    label = f"{prefix}{warn}{model}"
    return label[:64] if len(label) <= 64 else label[:61] + "\u2026"


def model_set_notice(model: str) -> str:
    """Confirmation text after /model or the model picker."""
    safe = html.escape(model)
    base = f"Model set to <code>{safe}</code>."
    if model in PROBLEMATIC_MODELS:
        return (
            f"{base}\n\n"
            f"\u26a0\ufe0f <b>{safe}</b> often fails with no output.\n"
            f"<code>{RECOMMENDED_MODEL}</code> is more reliable."
        )
    return base


def instant_empty_user_message(model: str, mode: str) -> str:
    """Shown after same-agent + recreate retries still return a fast empty error."""
    lines = [
        f"**{model or 'unknown'}** failed with no output.",
        "Session was reset — send your prompt again.",
    ]
    if model in PROBLEMATIC_MODELS:
        lines.append(f"This model is unreliable — use `/model {RECOMMENDED_MODEL}`.")
    else:
        lines.append(
            f"If this keeps happening, try `/model {RECOMMENDED_MODEL}`"
            + (f" or `/mode agent` (was {mode})." if mode == "plan" else "."),
        )
    return "\n".join(lines)


def format_model_display(model: str, model_params: dict[str, str]) -> str:
    """Session status line: model id plus active SDK params."""
    effort = (
        model_params.get("effort")
        or model_params.get("reasoning")
        or model_params.get("thinking")
    )
    if effort:
        return f"{model} · effort={effort}"
    return model
