from __future__ import annotations

import asyncio
import logging
import mimetypes
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from cursor_sdk import SDKImage, UserMessage
from telegram import Message, Update
from telegram.ext import ContextTypes

INBOUND_SUBDIR = ".cursor_bridge/inbound"
MAX_INBOUND_BYTES = 20 * 1024 * 1024  # Telegram Bot API download limit
# Rapid-fire attachments (e.g. multi-select photos) are batched into one prompt.
INBOUND_BATCH_DELAY_SEC = 1.2

_SAFE_NAME = re.compile(r"[^\w.\-]+")
logger = logging.getLogger("cursor_bridge.inbound")


def inbound_dir(cwd: str) -> Path:
    p = Path(cwd) / INBOUND_SUBDIR
    p.mkdir(parents=True, exist_ok=True)
    return p


def _safe_filename(name: str, fallback: str) -> str:
    base = Path(name).name or fallback
    cleaned = _SAFE_NAME.sub("_", base).strip("._")
    return cleaned or fallback


def _is_image_path(path: Path) -> bool:
    mime, _ = mimetypes.guess_type(str(path))
    if mime and mime.startswith("image/"):
        return True
    return path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def _relative_inbound_path(path: Path, cwd: str) -> str:
    try:
        return str(path.resolve().relative_to(Path(cwd).resolve()))
    except ValueError:
        return path.name


def _combine_captions(captions: list[str]) -> str:
    unique: list[str] = []
    for caption in captions:
        text = caption.strip()
        if text and text not in unique:
            unique.append(text)
    if not unique:
        return ""
    if len(unique) == 1:
        return unique[0]
    return "\n\n".join(unique)


@dataclass
class PendingInbound:
    path: Path
    kind: str
    filename: str
    caption: str | None = None


def build_user_message(path: Path, cwd: str, *, caption: str | None, filename: str) -> UserMessage:
    """Turn a saved inbound file into a Cursor SDK user message."""
    return build_combined_user_message(
        [PendingInbound(path=path, kind=describe_inbound(path), filename=filename, caption=caption)],
        cwd,
    )


def build_combined_user_message(items: list[PendingInbound], cwd: str) -> UserMessage:
    """Merge several inbound attachments into one SDK user message."""
    if not items:
        raise ValueError("No inbound attachments to combine.")

    images: list[SDKImage] = []
    rel_paths: list[str] = []
    captions: list[str] = []
    file_lines: list[str] = []

    for item in items:
        rel = _relative_inbound_path(item.path, cwd)
        rel_paths.append(rel)
        if item.caption:
            captions.append(item.caption)
        if _is_image_path(item.path):
            images.append(SDKImage.from_file(item.path))
        else:
            file_lines.append(f"- `{rel}` ({item.filename})")

    caption = _combine_captions(captions)

    if images and not file_lines:
        count = len(images)
        default = (
            "Please review the image I sent from Telegram."
            if count == 1
            else f"Please review the {count} images I sent from Telegram."
        )
        text = caption or default
        if count == 1:
            text += f"\n\n(Saved as `{rel_paths[0]}`)"
        else:
            listed = ", ".join(f"`{rel}`" for rel in rel_paths)
            text += f"\n\n(Saved as {listed})"
        return UserMessage(text=text, images=images)

    parts: list[str] = []
    if images:
        count = len(images)
        default = (
            "Please review the image I sent from Telegram."
            if count == 1
            else f"Please review the {count} images I sent from Telegram."
        )
        parts.append(caption or default)
        listed = ", ".join(f"`{rel}`" for rel in rel_paths[: len(images)])
        parts.append(f"Images: {listed}")
    else:
        parts.append(caption or "Please review the files I sent from Telegram.")

    if file_lines:
        parts.append("Other files:\n" + "\n".join(file_lines))

    text = "\n\n".join(parts)
    if images:
        return UserMessage(text=text, images=images)
    return UserMessage(text=text)


def describe_inbound(path: Path) -> str:
    return "image" if _is_image_path(path) else "file"


async def download_telegram_file(
    context: ContextTypes.DEFAULT_TYPE,
    file_id: str,
    dest: Path,
) -> None:
    tg_file = await context.bot.get_file(file_id)
    if tg_file.file_size and tg_file.file_size > MAX_INBOUND_BYTES:
        raise ValueError(
            f"File too large ({tg_file.file_size // (1024 * 1024)}MB). Telegram limit is 20MB."
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    await tg_file.download_to_drive(custom_path=str(dest))


def _pick_photo_file_id(message: Message) -> str | None:
    if not message.photo:
        return None
    return message.photo[-1].file_id


def inbound_file_spec(message: Message) -> tuple[str, str, str] | None:
    """Return (file_id, filename, kind) for an inbound attachment message."""
    if message.photo:
        photo = message.photo[-1]
        return photo.file_id, f"photo_{photo.file_unique_id}.jpg", "photo"
    if message.document:
        doc = message.document
        name = _safe_filename(doc.file_name or "", f"document_{doc.file_unique_id}")
        return doc.file_id, name, "document"
    if message.animation:
        anim = message.animation
        name = _safe_filename(anim.file_name or "", f"animation_{anim.file_unique_id}.gif")
        return anim.file_id, name, "animation"
    if message.video:
        vid = message.video
        name = _safe_filename(vid.file_name or "", f"video_{vid.file_unique_id}.mp4")
        return vid.file_id, name, "video"
    if message.audio:
        aud = message.audio
        name = _safe_filename(aud.file_name or "", f"audio_{aud.file_unique_id}.mp3")
        return aud.file_id, name, "audio"
    return None


async def save_inbound_from_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    cwd: str,
) -> tuple[Path, UserMessage, str]:
    """Download a Telegram attachment into the session folder and build the agent prompt."""
    pending = await save_inbound_attachment(update, context, cwd)
    prompt = build_user_message(
        pending.path,
        cwd,
        caption=pending.caption,
        filename=pending.filename,
    )
    return pending.path, prompt, pending.kind


async def save_inbound_attachment(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    cwd: str,
) -> PendingInbound:
    """Download one Telegram attachment; batching combines these later."""
    message = update.effective_message
    if message is None:
        raise ValueError("No message.")
    spec = inbound_file_spec(message)
    if spec is None:
        raise ValueError("No supported attachment on this message.")

    file_id, filename, kind = spec
    stamp = int(time.time())
    dest = inbound_dir(cwd) / f"{stamp}_{filename}"
    await download_telegram_file(context, file_id, dest)
    return PendingInbound(
        path=dest,
        kind=kind,
        filename=filename,
        caption=message.caption,
    )


FlushCb = Callable[[list[PendingInbound]], Awaitable[None]]


class InboundBatcher:
    """Debounce rapid inbound media into a single agent prompt per chat/session."""

    def __init__(self, delay_sec: float = INBOUND_BATCH_DELAY_SEC) -> None:
        self._delay = delay_sec
        self._pending: dict[tuple[int, str], list[PendingInbound]] = {}
        self._tasks: dict[tuple[int, str], asyncio.Task[None]] = {}

    def pending_count(self, chat_id: int, sid: str) -> int:
        return len(self._pending.get((chat_id, sid), ()))

    async def add(
        self,
        chat_id: int,
        sid: str,
        item: PendingInbound,
        flush_cb: FlushCb,
    ) -> int:
        key = (chat_id, sid)
        batch = self._pending.setdefault(key, [])
        batch.append(item)
        count = len(batch)

        existing = self._tasks.pop(key, None)
        if existing is not None:
            existing.cancel()
            try:
                await existing
            except asyncio.CancelledError:
                pass

        self._tasks[key] = asyncio.create_task(
            self._flush_after(key, flush_cb),
            name=f"inbound-batch-{sid}",
        )
        return count

    async def _flush_after(self, key: tuple[int, str], flush_cb: FlushCb) -> None:
        try:
            await asyncio.sleep(self._delay)
            items = self._pending.pop(key, [])
            self._tasks.pop(key, None)
            if items:
                await flush_cb(items)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Inbound batch flush failed for %s", key[1])
            self._pending.pop(key, None)
            self._tasks.pop(key, None)
