from __future__ import annotations

import asyncio
import html
import platform
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

API_BASE = "https://api2.cursor.sh"
USAGE_PATH = "/aiserver.v1.DashboardService/GetCurrentPeriodUsage"
PLAN_PATH = "/aiserver.v1.DashboardService/GetPlanInfo"
AGG_PATH = "/aiserver.v1.DashboardService/GetAggregatedUsageEvents"
HARD_LIMIT_PATH = "/aiserver.v1.DashboardService/GetHardLimit"
SAND_PATH = "/aiserver.v1.DashboardService/GetSandUsageStatus"

LOCAL_TZ = ZoneInfo("Asia/Shanghai")

AUTH_KEYS = (
    "cursorAuth/accessToken",
    "cursorAuth/cachedEmail",
    "cursorAuth/stripeMembershipType",
)


@dataclass
class ModelSpend:
    name: str
    cents: float
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class UsageSummary:
    plan_name: str
    email: str
    billing_start: str
    billing_end: str
    billing_start_ms: int | None = None
    billing_end_ms: int | None = None
    auto_percent: float | None = None
    api_percent: float | None = None
    total_percent: float | None = None
    included_spend_cents: int = 0
    limit_cents: int = 0
    bonus_spend_cents: int = 0
    total_spend_cents: int = 0
    display_message: str = ""
    auto_message: str = ""
    api_message: str = ""
    auto_models: list[str] = field(default_factory=list)
    plan_price: str = ""
    on_demand_allowed: bool | None = None
    model_spends: list[ModelSpend] = field(default_factory=list)
    grok_bot_percent: float | None = None
    grok_bot_reset_ms: int | None = None
    grok_bot_label: str = ""


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


def _ms_int(raw: str | int | None) -> int | None:
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _ms_to_dt(raw: str | int | None) -> datetime | None:
    ms = _ms_int(raw)
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None


def _ms_date(raw: str | int | None) -> str:
    dt = _ms_to_dt(raw)
    if dt is None:
        return "?"
    return dt.astimezone(LOCAL_TZ).strftime("%d %b %Y")


def _fmt_local(dt: datetime) -> str:
    return dt.astimezone(LOCAL_TZ).strftime("%d %b %Y %H:%M UTC+8")


def _any_to_ms(raw: object) -> int | None:
    """Parse dashboard timestamps (unix ms, unix s, ISO-8601, or protobuf)."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        secs = raw.get("seconds")
        if secs is None:
            return None
        try:
            nanos = int(raw.get("nanos") or 0)
            return int(secs) * 1000 + nanos // 1_000_000
        except (TypeError, ValueError):
            return None
    if isinstance(raw, (int, float)):
        val = float(raw)
        if val > 1e12:
            return int(val)
        if val > 1e9:
            return int(val * 1000)
        return None
    text = str(raw).strip()
    if not text:
        return None
    if text.isdigit():
        return _ms_int(text)
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def countdown_until(until: datetime, *, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    delta = until - now
    secs = int(delta.total_seconds())
    if secs <= 0:
        return "due now"
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days:
        return f"{days}d {hours}h left"
    if hours:
        return f"{hours}h {mins}m left"
    return f"{max(mins, 1)}m left"


def _pct(raw: object) -> float | None:
    try:
        val = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if val != val:  # NaN
        return None
    return max(0.0, min(100.0, val))


def _dollars(cents: int | float) -> str:
    return f"${float(cents) / 100:.2f}"


def _bar(percent: float, width: int = 10) -> str:
    filled = round(width * percent / 100)
    return "\u2588" * filled + "\u2591" * (width - filled)


def _pretty_model(name: str) -> str:
    text = name.strip()
    if text.startswith("cursor-"):
        text = text[len("cursor-") :]
    if text == "default":
        return "Auto (default)"
    if text == "sand-default":
        return "Composer (sand)"
    return text


def is_grok_model(name: str) -> bool:
    return "grok" in name.lower()


def grok_in_auto_bucket(models: list[str]) -> bool:
    return any(is_grok_model(m) for m in models)


def group_model_spends(spends: list[ModelSpend]) -> tuple[float, list[ModelSpend], list[ModelSpend]]:
    grok = [row for row in spends if is_grok_model(row.name)]
    other = [row for row in spends if not is_grok_model(row.name)]
    grok_total = sum(row.cents for row in grok)
    grok.sort(key=lambda row: row.cents, reverse=True)
    other.sort(key=lambda row: row.cents, reverse=True)
    return grok_total, grok, other


def _parse_model_spends(payload: dict) -> list[ModelSpend]:
    rows: list[ModelSpend] = []
    for item in payload.get("aggregations") or []:
        name = str(item.get("modelIntent") or "").strip()
        if not name:
            continue
        try:
            cents = float(item.get("totalCents") or 0)
        except (TypeError, ValueError):
            cents = 0.0
        try:
            inn = int(item.get("inputTokens") or 0)
        except (TypeError, ValueError):
            inn = 0
        try:
            out = int(item.get("outputTokens") or 0)
        except (TypeError, ValueError):
            out = 0
        rows.append(ModelSpend(name=name, cents=cents, input_tokens=inn, output_tokens=out))
    rows.sort(key=lambda row: row.cents, reverse=True)
    return rows


async def _post_json(
    client: httpx.AsyncClient,
    path: str,
    headers: dict[str, str],
    body: dict,
) -> httpx.Response:
    return await client.post(f"{API_BASE}{path}", headers=headers, json=body)


async def fetch_usage() -> UsageSummary:
    auth = _read_auth()
    token = auth["cursorAuth/accessToken"]
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Connect-Protocol-Version": "1",
    }
    usage_resp: httpx.Response | None = None
    plan_resp: httpx.Response | None = None
    hard_resp: httpx.Response | None = None
    sand_resp: httpx.Response | None = None
    agg_resp: httpx.Response | None = None
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                usage_resp, plan_resp, hard_resp, sand_resp = await asyncio.gather(
                    _post_json(client, USAGE_PATH, headers, {}),
                    _post_json(client, PLAN_PATH, headers, {}),
                    _post_json(client, HARD_LIMIT_PATH, headers, {}),
                    _post_json(client, SAND_PATH, headers, {}),
                )
                usage_preview = usage_resp.json() if usage_resp.status_code == 200 else {}
                start_ms = usage_preview.get("billingCycleStart")
                end_ms = usage_preview.get("billingCycleEnd")
                agg_body: dict = {}
                if start_ms is not None and end_ms is not None:
                    agg_body = {"startDateMs": str(start_ms), "endDateMs": str(end_ms)}
                agg_resp = await _post_json(client, AGG_PATH, headers, agg_body)
            break
        except httpx.HTTPError as exc:
            if attempt == 0:
                await asyncio.sleep(0.5)
                continue
            raise UsageError(f"Could not reach Cursor usage API: {exc}") from exc

    assert usage_resp is not None
    if usage_resp.status_code == 401:
        raise UsageError(
            "Cursor session expired. Open the Cursor app on this Mac briefly, then try /usage again."
        )
    if usage_resp.status_code != 200:
        raise UsageError(f"Cursor usage API returned {usage_resp.status_code}.")

    usage = usage_resp.json()
    plan_name = auth.get("cursorAuth/stripeMembershipType") or "unknown"
    plan_price = ""
    if plan_resp is not None and plan_resp.status_code == 200:
        plan_info = plan_resp.json().get("planInfo") or {}
        plan_name = plan_info.get("planName") or plan_name
        plan_price = str(plan_info.get("price") or "").strip()

    on_demand_allowed: bool | None = None
    if hard_resp is not None and hard_resp.status_code == 200:
        hard = hard_resp.json()
        if "noUsageBasedAllowed" in hard:
            on_demand_allowed = not bool(hard.get("noUsageBasedAllowed"))

    model_spends: list[ModelSpend] = []
    if agg_resp is not None and agg_resp.status_code == 200:
        try:
            model_spends = _parse_model_spends(agg_resp.json())
        except (TypeError, ValueError):
            model_spends = []

    grok_bot_percent: float | None = None
    grok_bot_reset_ms: int | None = None
    grok_bot_label = ""
    if sand_resp is not None and sand_resp.status_code == 200:
        try:
            sand = sand_resp.json()
        except ValueError:
            sand = {}
        grok_bot_percent = _pct(sand.get("usagePercent"))
        grok_bot_reset_ms = _any_to_ms(sand.get("nextResetTimestampUtc"))
        grok_bot_label = str(sand.get("grokPlanLabel") or "Grok Bot").strip()

    pu = usage.get("planUsage") or {}
    return UsageSummary(
        plan_name=str(plan_name),
        email=auth.get("cursorAuth/cachedEmail", ""),
        billing_start=_ms_date(usage.get("billingCycleStart")),
        billing_end=_ms_date(usage.get("billingCycleEnd")),
        billing_start_ms=_ms_int(usage.get("billingCycleStart")),
        billing_end_ms=_ms_int(usage.get("billingCycleEnd")),
        auto_percent=_pct(pu.get("autoPercentUsed")),
        api_percent=_pct(pu.get("apiPercentUsed")),
        total_percent=_pct(pu.get("totalPercentUsed")),
        included_spend_cents=int(pu.get("includedSpend") or 0),
        limit_cents=int(pu.get("limit") or 0),
        bonus_spend_cents=int(pu.get("bonusSpend") or 0),
        total_spend_cents=int(pu.get("totalSpend") or 0),
        display_message=str(usage.get("displayMessage") or "").strip(),
        auto_message=str(usage.get("autoModelSelectedDisplayMessage") or "").strip(),
        api_message=str(usage.get("namedModelSelectedDisplayMessage") or "").strip(),
        auto_models=list(usage.get("autoBucketModels") or []),
        plan_price=plan_price,
        on_demand_allowed=on_demand_allowed,
        model_spends=model_spends,
        grok_bot_percent=grok_bot_percent,
        grok_bot_reset_ms=grok_bot_reset_ms,
        grok_bot_label=grok_bot_label,
    )


def _esc(text: object) -> str:
    return html.escape(str(text))


def format_usage_html(summary: UsageSummary, *, now: datetime | None = None) -> str:
    title = _esc(summary.plan_name)
    if summary.plan_price:
        title = f"{title} ({_esc(summary.plan_price)})"
    lines = [
        f"<b>Cursor usage</b> \u2014 {title}",
    ]
    if summary.email:
        lines.append(f"Account: <code>{_esc(summary.email)}</code>")

    start_dt = _ms_to_dt(summary.billing_start_ms)
    end_dt = _ms_to_dt(summary.billing_end_ms)
    if start_dt and end_dt:
        lines.append(f"Cycle: {_fmt_local(start_dt)} \u2192 {_fmt_local(end_dt)}")
        lines.append(f"Reset: <b>{_fmt_local(end_dt)}</b> \u00b7 {countdown_until(end_dt, now=now)}")
    else:
        lines.append(f"Cycle: {summary.billing_start} \u2192 {summary.billing_end}")

    lines.append("")

    if summary.auto_percent is not None:
        lines.append(
            f"<b>Auto + Composer</b>  <code>{_bar(summary.auto_percent)}</code>  "
            f"{summary.auto_percent:.1f}%"
        )
    if summary.api_percent is not None:
        lines.append(
            f"<b>API</b>             <code>{_bar(summary.api_percent)}</code>  "
            f"{summary.api_percent:.1f}%"
        )
    if summary.total_percent is not None:
        lines.append(
            f"<b>Total included</b>  <code>{_bar(summary.total_percent)}</code>  "
            f"{summary.total_percent:.1f}%"
        )

    lines.append("")
    if summary.total_spend_cents:
        lines.append(f"Billed this cycle: <b>{_dollars(summary.total_spend_cents)}</b>")
    if summary.limit_cents:
        lines.append(
            f"Included spend: <b>{_dollars(summary.included_spend_cents)}</b> / "
            f"{_dollars(summary.limit_cents)}"
        )
    if summary.bonus_spend_cents:
        lines.append(f"Bonus used: <b>{_dollars(summary.bonus_spend_cents)}</b>")
    if summary.on_demand_allowed is False:
        lines.append("On-demand spend: <b>off</b> (no extra billing past included + bonus)")

    if summary.grok_bot_percent is not None:
        label = _esc(summary.grok_bot_label or "Grok Bot")
        lines.append("")
        lines.append(
            f"<b>{label}</b>  <code>{_bar(summary.grok_bot_percent)}</code>  "
            f"{summary.grok_bot_percent:.1f}%"
        )
        reset_dt = _ms_to_dt(summary.grok_bot_reset_ms)
        if reset_dt is not None:
            lines.append(
                f"Weekly reset: <b>{_fmt_local(reset_dt)}</b> \u00b7 "
                f"{countdown_until(reset_dt, now=now)}"
            )

    grok_total, grok_rows, other_rows = group_model_spends(summary.model_spends)
    if grok_rows or other_rows:
        lines.append("")
        lines.append("<b>By model</b> <i>(provider list $, not Cursor billed $)</i>")
        if grok_rows:
            lines.append(f"Grok family  <b>{_dollars(grok_total)}</b>")
            for row in grok_rows[:4]:
                lines.append(f"  \u00b7 {_esc(_pretty_model(row.name))}  {_dollars(row.cents)}")
            extra_n = len(grok_rows) - 4
            if extra_n > 0:
                lines.append(f"  \u00b7 +{extra_n} more Grok variant(s)")
        for row in other_rows[:5]:
            lines.append(f"{_esc(_pretty_model(row.name))}  {_dollars(row.cents)}")
        extra_other = len(other_rows) - 5
        if extra_other > 0:
            lines.append(f"+{extra_other} more model(s)")

    return "\n".join(lines)
