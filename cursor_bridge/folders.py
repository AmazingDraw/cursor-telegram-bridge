from __future__ import annotations

from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .config import Config


class TokenStore:
    """Maps short tokens to filesystem paths.

    Telegram callback_data is capped at 64 bytes, so we cannot embed absolute
    paths directly. Each path gets a stable short token (``p1``, ``p2`` ...).
    """

    def __init__(self) -> None:
        self._by_token: dict[str, str] = {}
        self._by_path: dict[str, str] = {}
        self._n = 0

    def token(self, path: str) -> str:
        path = str(path)
        existing = self._by_path.get(path)
        if existing is not None:
            return existing
        self._n += 1
        tok = f"p{self._n}"
        self._by_token[tok] = path
        self._by_path[path] = tok
        return tok

    def path(self, tok: str) -> str | None:
        return self._by_token.get(tok)


def is_git_repo(p: Path) -> bool:
    return (p / ".git").exists()


def discover_projects(cfg: Config) -> list[tuple[str, Path]]:
    """Bookmarks first, then immediate subfolders of ``projects_root``."""
    items: list[tuple[str, Path]] = []
    seen: set[str] = set()

    for bm in cfg.bookmarks:
        p = Path(bm.path)
        if p.is_dir() and str(p) not in seen:
            items.append((f"\U0001F4CC {bm.name}", p))
            seen.add(str(p))

    root = cfg.projects_root
    if root.is_dir():
        try:
            children = sorted(root.iterdir(), key=lambda c: c.name.lower())
        except PermissionError:
            children = []
        for child in children:
            if not child.is_dir() or child.name.startswith("."):
                continue
            if str(child) in seen:
                continue
            tag = " \u00b7git" if is_git_repo(child) else ""
            items.append((f"\U0001F4C1 {child.name}{tag}", child))
            seen.add(str(child))

    return items


def projects_keyboard(cfg: Config, tokens: TokenStore) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for label, path in discover_projects(cfg):
        rows.append([InlineKeyboardButton(label, callback_data=f"pick:{tokens.token(str(path))}")])
    rows.append([
        InlineKeyboardButton(
            f"\u2795 在 {cfg.projects_root.name or '/'} 下新建文件夹",
            callback_data=f"mkdir:{tokens.token(str(cfg.projects_root))}",
        )
    ])
    rows.append([
        InlineKeyboardButton(
            f"\U0001F5C2 浏览 {cfg.projects_root.name or '/'}",
            callback_data=f"nav:{tokens.token(str(cfg.projects_root))}",
        )
    ])
    rows.append([InlineKeyboardButton("\U0001F5C2 浏览根目录 /", callback_data=f"nav:{tokens.token('/')}")])
    return InlineKeyboardMarkup(rows)


def browser_keyboard(path: str, tokens: TokenStore, page_size: int) -> InlineKeyboardMarkup:
    p = Path(path)
    rows: list[list[InlineKeyboardButton]] = []

    rows.append([
        InlineKeyboardButton(
            f"\u2705 选择此文件夹 ({p.name or '/'})",
            callback_data=f"use:{tokens.token(str(p))}",
        )
    ])
    rows.append([
        InlineKeyboardButton(
            "\u2795 在此处新建文件夹",
            callback_data=f"mkdir:{tokens.token(str(p))}",
        )
    ])

    parent = p.parent
    if str(parent) != str(p):
        rows.append([InlineKeyboardButton("\u2B06\uFE0F 返回上级目录 ..", callback_data=f"nav:{tokens.token(str(parent))}")])

    try:
        dirs = sorted(
            (c for c in p.iterdir() if c.is_dir() and not c.name.startswith(".")),
            key=lambda c: c.name.lower(),
        )
    except (PermissionError, OSError):
        dirs = []

    for child in dirs[:page_size]:
        icon = "\U0001F4E6" if is_git_repo(child) else "\U0001F4C1"
        rows.append([InlineKeyboardButton(f"{icon} {child.name}", callback_data=f"nav:{tokens.token(str(child))}")])

    return InlineKeyboardMarkup(rows)
