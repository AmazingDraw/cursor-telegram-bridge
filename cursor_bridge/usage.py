from __future__ import annotations

import asyncio
import html
import platform
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

API_BASE = "https://api2.cursor.sh"
USAGE_PATH = "/aiserver.v1.DashboardService/GetCurrentPeriodUsage"
PLAN_PATH = "/aiserver.v1.DashboardService/GetPlanInfo"

AUTH_KEYS = (
    "cursorAuth/accessToken",
    "cursorAuth/cachedEmail",
    "cursorAuth/stripeMembershipType",
)


@dataclass
class UsageSummary:
    plan_name: str
    email: str
    billing_start: str
    billing_end: str
    auto_percent: float | None
    api_percent: float | None
    total_percent: float | None
    included_spend_cents: int
    limit_cents: int
    bonus_spend_cents: int
    display_message: str
    auto_models: list[str]


class UsageError(Exception):
    pass


def _cursor_state_db() -> Path:
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library/Application Support/Cursor/User/globalStorage/state.vscdb"
    if system == "Windows":
        appdata = Path.home() / "AppData/Roaming"
        return appdata / "Cursor/User/globalStorage/state.vscdb"
    return Path.home() / ".config/Cursor/User/globalStorage/state.vscdb"


def _read_auth() -> dict[str, str]:
    db = _cursor_state_db()
    if not db.is_file():
        raise UsageError(
            "Cursor is not signed in on this Mac. Open the Cursor app and log in, then try /usage again."
        )
    uri = f"file:{db}?mode=ro&immutable=1"
    try:
        con = sqlite3.connect(uri, uri=True)
        rows = {
            key: value
            for key, value in con.execute(
                "SELECT key, value FROM ItemTable WHERE key IN ({})".format(
                    ",".join("?" * len(AUTH_KEYS))
                ),
                AUTH_KEYS,
            )
        }
        con.close()
    except sqlite3.Error as exc:
        raise UsageError(f"Could not read Cursor login state: {exc}") from exc

    token = rows.get("cursorAuth/accessToken", "").strip()
    if not token:
        raise UsageError(
            "No Cursor access token found. Open the Cursor app on this Mac to refresh your login."
        )
    return rows


def _ms_date(raw: str | int | None) -> str:
    if raw is None:
        return "?"
    try:
        ms = int(raw)
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%d %b %Y")
    except (TypeError, ValueError):
        return "?"


def _pct(raw: object) -> float | None:
    try:
        val = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if val != val:  # NaN
        return None
    return max(0.0, min(100.0, val))


def _dollars(cents: int) -> str:
    return f"${cents / 100:.2f}"


def _bar(percent: float, width: int = 10) -> str:
    filled = round(width * percent / 100)
    return "\u2588" * filled + "\u2591" * (width - filled)


async def fetch_usage() -> UsageSummary:
    auth = _read_auth()
    token = auth["cursorAuth/accessToken"]
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Connect-Protocol-Version": "1",
    }
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                usage_resp = await client.post(f"{API_BASE}{USAGE_PATH}", headers=headers, json={})
                plan_resp = await client.post(f"{API_BASE}{PLAN_PATH}", headers=headers, json={})
            break
        except httpx.HTTPError as exc:
            if attempt == 0:
                await asyncio.sleep(0.5)
                continue
            raise UsageError(f"Could not reach Cursor usage API: {exc}") from exc

    if usage_resp.status_code == 401:
        raise UsageError(
            "Cursor session expired. Open the Cursor app on this Mac briefly, then try /usage again."
        )
    if usage_resp.status_code != 200:
        raise UsageError(f"Cursor usage API returned {usage_resp.status_code}.")

    usage = usage_resp.json()
    plan_name = auth.get("cursorAuth/stripeMembershipType") or "unknown"
    if plan_resp.status_code == 200:
        plan_info = plan_resp.json().get("planInfo") or {}
        plan_name = plan_info.get("planName") or plan_name

    pu = usage.get("planUsage") or {}
    return UsageSummary(
        plan_name=str(plan_name),
        email=auth.get("cursorAuth/cachedEmail", ""),
        billing_start=_ms_date(usage.get("billingCycleStart")),
        billing_end=_ms_date(usage.get("billingCycleEnd")),
        auto_percent=_pct(pu.get("autoPercentUsed")),
        api_percent=_pct(pu.get("apiPercentUsed")),
        total_percent=_pct(pu.get("totalPercentUsed")),
        included_spend_cents=int(pu.get("includedSpend") or 0),
        limit_cents=int(pu.get("limit") or 0),
        bonus_spend_cents=int(pu.get("bonusSpend") or 0),
        display_message=str(usage.get("displayMessage") or "").strip(),
        auto_models=list(usage.get("autoBucketModels") or []),
    )


def _esc(text: object) -> str:
    return html.escape(str(text))


def format_usage_html(summary: UsageSummary) -> str:
    lines = [
        f"<b>Cursor usage</b> \u2014 {_esc(summary.plan_name)}",
    ]
    if summary.email:
        lines.append(f"Account: <code>{_esc(summary.email)}</code>")
    lines.append(f"Cycle: {summary.billing_start} \u2192 {summary.billing_end}")
    lines.append("")

    if summary.auto_percent is not None:
        lines.append(
            f"<b>Auto + Composer</b>  <code>{_bar(summary.auto_percent)}</code>  {summary.auto_percent:.1f}%"
        )
    if summary.api_percent is not None:
        lines.append(
            f"<b>API</b>             <code>{_bar(summary.api_percent)}</code>  {summary.api_percent:.1f}%"
        )
    if summary.total_percent is not None:
        lines.append(
            f"<b>Total included</b>  <code>{_bar(summary.total_percent)}</code>  {summary.total_percent:.1f}%"
        )

    lines.append("")
    if summary.limit_cents:
        lines.append(
            f"Included spend: <b>{_dollars(summary.included_spend_cents)}</b> / {_dollars(summary.limit_cents)}"
        )
    if summary.bonus_spend_cents:
        lines.append(f"Bonus credits: <b>{_dollars(summary.bonus_spend_cents)}</b>")

    return "\n".join(lines)
