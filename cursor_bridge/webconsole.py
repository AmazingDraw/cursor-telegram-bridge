"""Local HTTP dashboard — session list, event history, live log tail."""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

from .context import format_context_line, get_context_info
from .events import SessionEventLog
from .state_layout import bot_state_dir, migrate_legacy_default_state

if TYPE_CHECKING:
    from .config import Config

_CONSOLE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>cursor-telegram-bridge Console</title>
<style>
  :root { --bg:#0d1117; --panel:#161b22; --border:#30363d; --text:#e6edf3;
          --muted:#8b949e; --green:#3fb950; --yellow:#d29922; --red:#f85149; }
  * { box-sizing: border-box; }
  body { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; margin:0;
         background:var(--bg); color:var(--text); font-size:13px; line-height:1.45; }
  header { padding:14px 18px; border-bottom:1px solid var(--border);
           display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px; }
  h1 { margin:0; font-size:15px; font-weight:600; }
  .pill { padding:3px 8px; border-radius:999px; font-size:11px; border:1px solid var(--border); }
  .live { color:var(--green); border-color:var(--green); }
  .idle { color:var(--yellow); }
  main { display:grid; grid-template-columns: 320px 1fr; gap:0; min-height:calc(100vh - 52px); }
  @media (max-width:900px) { main { grid-template-columns:1fr; } }
  aside { border-right:1px solid var(--border); padding:12px; overflow:auto; }
  section { padding:12px; overflow:auto; }
  h2 { font-size:12px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted);
       margin:0 0 10px; }
  .sess { border:1px solid var(--border); border-radius:8px; padding:10px; margin-bottom:8px;
          cursor:pointer; background:var(--panel); }
  .sess:hover, .sess.active { border-color:#58a6ff; }
  .sess .id { font-weight:700; }
  .sess .meta { color:var(--muted); font-size:11px; margin-top:4px; }
  .badge.running { color:var(--green); }
  .badge.error { color:var(--red); }
  .badge.idle { color:var(--yellow); }
  .star { color:#f0c14b; }
  pre.log { background:var(--panel); border:1px solid var(--border); border-radius:8px;
            padding:10px; margin:0; white-space:pre-wrap; word-break:break-word;
            max-height:280px; overflow:auto; font-size:11px; }
  table.events { width:100%; border-collapse:collapse; font-size:11px; }
  table.events td { padding:4px 6px; border-bottom:1px solid var(--border); vertical-align:top; }
  table.events td.ts { color:var(--muted); white-space:nowrap; width:70px; }
  table.events td.ev { color:#79c0ff; width:90px; }
  .footer { color:var(--muted); font-size:11px; margin-top:10px; }
</style>
</head>
<body>
<header>
  <h1>cursor-telegram-bridge Console</h1>
  <div>
    <span id="headline" class="pill idle">loading…</span>
    <span id="clock" class="pill"></span>
  </div>
</header>
<main>
  <aside>
    <h2>Sessions</h2>
    <div id="sessions"></div>
  </aside>
  <section>
    <h2 id="events-title">Recent events</h2>
    <table class="events"><tbody id="events"></tbody></table>
    <h2 style="margin-top:18px">Live log</h2>
    <pre class="log" id="log"></pre>
    <p class="footer">Refreshes every 2s · read-only · local only</p>
  </section>
</main>
<script>
const token = new URLSearchParams(location.search).get('token') || '';
let selectedSid = new URLSearchParams(location.search).get('s') || '';

function api(path) {
  const sep = path.includes('?') ? '&' : '?';
  const t = token ? sep + 'token=' + encodeURIComponent(token) : '';
  return fetch(path + t).then(r => { if (!r.ok) throw new Error(r.status); return r.json(); });
}

function fmtTs(ts) {
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'});
}

function badge(st) {
  return '<span class="badge ' + st + '">' + st + '</span>';
}

function renderSessions(data) {
  const el = document.getElementById('sessions');
  if (!data.sessions.length) { el.innerHTML = '<p class="meta">No sessions</p>'; return; }
  el.innerHTML = data.sessions.map(s => {
    const active = s.active ? ' <span class="star">★</span>' : '';
    const cls = s.short_id === selectedSid ? 'sess active' : 'sess';
    return '<div class="' + cls + '" data-sid="' + s.short_id + '">' +
      '<div class="id">[' + s.short_id + ']' + active + ' ' + badge(s.status) + '</div>' +
      '<div class="meta">' + s.name + '</div>' +
      '<div class="meta">' + s.model + ' · ' + s.mode + '</div>' +
      '<div class="meta">' + (s.context || '') + '</div>' +
      '</div>';
  }).join('');
  el.querySelectorAll('.sess').forEach(node => {
    node.onclick = () => {
      selectedSid = node.dataset.sid;
      const u = new URL(location);
      u.searchParams.set('s', selectedSid);
      if (token) u.searchParams.set('token', token);
      history.replaceState(null, '', u);
      refresh();
    };
  });
}

function renderEvents(rows) {
  const el = document.getElementById('events');
  document.getElementById('events-title').textContent =
    selectedSid ? 'Events · ' + selectedSid : 'Recent events (all sessions)';
  if (!rows.length) { el.innerHTML = '<tr><td colspan="3">(no events yet)</td></tr>'; return; }
  el.innerHTML = rows.map(r => {
    const detail = Object.entries(r).filter(([k]) => !['ts','sid','event'].includes(k))
      .map(([k,v]) => k + '=' + JSON.stringify(v)).join(' ');
    const sid = r.sid && !selectedSid ? ' <span style="color:#8b949e">[' + r.sid + ']</span>' : '';
    return '<tr><td class="ts">' + fmtTs(r.ts) + '</td>' +
      '<td class="ev">' + r.event + sid + '</td>' +
      '<td>' + (detail || '') + '</td></tr>';
  }).join('');
}

function renderLog(lines) {
  document.getElementById('log').textContent = lines.join('\\n') || '(empty)';
}

async function refresh() {
  try {
    const [overview, events, log] = await Promise.all([
      api('/api/overview'),
      api(selectedSid ? '/api/events?s=' + encodeURIComponent(selectedSid) : '/api/events'),
      api('/api/log?lines=40'),
    ]);
    const pill = document.getElementById('headline');
    pill.textContent = overview.headline;
    pill.className = 'pill ' + (overview.any_running ? 'live' : 'idle');
    document.getElementById('clock').textContent = new Date().toLocaleTimeString();
    renderSessions(overview);
    renderEvents(events);
    renderLog(log.lines);
  } catch (e) {
    document.getElementById('headline').textContent = 'error: ' + e.message;
  }
}

refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>
"""


def _tail(path: Path, n: int) -> list[str]:
    if not path.is_file():
        return ["(no log yet)"]
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return [f"(could not read log: {exc})"]
    return lines[-n:] if lines else ["(log empty)"]


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _err_log_path(state_dir: Path) -> Path:
    return state_dir / "cursor_bridge.err.log"


def _session_registry_paths(cfg: Config) -> list[tuple[str, Path]]:
    """(bot label, sessions.json path) for each ``state/bots/<name>/`` registry."""
    migrate_legacy_default_state(cfg)
    paths: list[tuple[str, Path]] = []
    seen: set[str] = set()

    # Prefer configured bot names so empty bots still appear in the console.
    for b in cfg.bots or []:
        name = b.name or "default"
        if name in seen:
            continue
        seen.add(name)
        paths.append((name, bot_state_dir(cfg, name) / "sessions.json"))

    bots_dir = cfg.state_dir / "bots"
    if bots_dir.is_dir():
        for child in sorted(bots_dir.iterdir()):
            if not child.is_dir() or child.name in seen:
                continue
            reg = child / "sessions.json"
            if reg.is_file():
                seen.add(child.name)
                paths.append((child.name, reg))

    if not paths:
        paths.append(("default", bot_state_dir(cfg, "default") / "sessions.json"))
    return paths


def _state_dir_for_bot(cfg: Config, bot_name: str) -> Path:
    return bot_state_dir(cfg, bot_name)

def _display_session_id(bot_name: str, short_id: str) -> str:
    if bot_name == "default":
        return short_id
    return f"{bot_name}:{short_id}"


def _parse_display_session_id(composite: str) -> tuple[str, str]:
    if ":" in composite:
        bot_name, sid = composite.split(":", 1)
        return bot_name, sid
    return "default", composite


def _load_all_sessions(cfg: Config) -> tuple[set[str], list[dict], bool]:
    active_ids: set[str] = set()
    rows: list[dict] = []
    any_running = False
    for bot_name, path in _session_registry_paths(cfg):
        active_map, sessions = _load_sessions_json(path)
        for sid in active_map.values():
            active_ids.add(_display_session_id(bot_name, sid))
        for s in sessions:
            sid = str(s.get("short_id", "?"))
            display_id = _display_session_id(bot_name, sid)
            any_running = any_running or s.get("status") == "running"
            rows.append({
                **s,
                "short_id": display_id,
                "bot": bot_name,
            })
    return active_ids, rows, any_running


def _event_log_for_bot(cfg: Config, bot_name: str) -> SessionEventLog:
    return SessionEventLog(
        _state_dir_for_bot(cfg, bot_name),
        max_events=cfg.event_log_max,
    )


def _read_all_recent_events(cfg: Config, limit: int) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for bot_name, _path in _session_registry_paths(cfg):
        log = _event_log_for_bot(cfg, bot_name)
        for row in log.read_recent(limit=limit):
            item = dict(row)
            sid = str(item.get("sid", ""))
            if bot_name != "default" and sid:
                item["sid"] = _display_session_id(bot_name, sid)
            merged.append(item)
    merged.sort(key=lambda r: float(r.get("ts", 0)), reverse=True)
    return merged[:limit]


def _bot_pid(state_dir: Path) -> int | None:
    path = state_dir / "cursor_bridge.pid"
    if not path.is_file():
        return None
    try:
        pid = int(path.read_text().strip())
    except ValueError:
        return None
    return pid if _pid_alive(pid) else None


def _load_sessions_json(path: Path) -> tuple[dict[str, str], list[dict]]:
    if not path.is_file():
        return {}, []
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}, []
    active = {str(k): v for k, v in data.get("active", {}).items()}
    return active, list(data.get("sessions", []))


class _ConsoleServer(ThreadingHTTPServer):
    def __init__(
        self,
        addr: tuple[str, int],
        cfg: Config,
    ) -> None:
        self.cfg = cfg
        self.token = cfg.console_token
        super().__init__(addr, _ConsoleHandler)


class _ConsoleHandler(BaseHTTPRequestHandler):
    server: _ConsoleServer  # type: ignore[assignment]

    def log_message(self, format: str, *args: object) -> None:
        pass

    def _unauthorized(self) -> None:
        self.send_response(401)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Unauthorized")

    def _auth_ok(self) -> bool:
        token = self.server.token
        if not token:
            return True
        qs = parse_qs(urlparse(self.path).query)
        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer ") and header[7:].strip() == token:
            return True
        if qs.get("token", [""])[0] == token:
            return True
        return False

    def _json(self, status: int, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _text(self, status: int, body: str, content_type: str = "text/html; charset=utf-8") -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if not self._auth_ok():
            self._unauthorized()
            return

        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = parse_qs(parsed.query)
        cfg = self.server.cfg

        if path == "/":
            self._text(200, _CONSOLE_HTML)
            return

        if path == "/api/overview":
            active_ids, sessions, any_running = _load_all_sessions(cfg)
            pid = _bot_pid(cfg.state_dir)
            if pid is None:
                headline = "Bot not running"
            elif any_running:
                headline = "SESSION LIVE"
            else:
                headline = "Online — waiting"
            rows = []
            for s in sessions:
                sid = str(s.get("short_id", "?"))
                ctx = get_context_info(s.get("agent_id"), s.get("cwd", ""))
                cwd = s.get("cwd", "")
                name = s.get("custom_name") or Path(cwd).name or sid
                rows.append({
                    "short_id": sid,
                    "name": name,
                    "cwd": cwd,
                    "status": s.get("status", "idle"),
                    "model": s.get("model", ""),
                    "mode": s.get("mode", ""),
                    "active": sid in active_ids,
                    "bot": s.get("bot", "default"),
                    "context": format_context_line(ctx) if ctx else "",
                    "last_prompt": (s.get("last_prompt") or "")[:120],
                })
            self._json(200, {
                "headline": headline,
                "pid": pid,
                "any_running": any_running,
                "sessions": rows,
            })
            return

        if path == "/api/events":
            sid = qs.get("s", [""])[0]
            limit = min(int(qs.get("limit", ["80"])[0] or 80), 500)
            if sid:
                bot_name, raw_sid = _parse_display_session_id(sid)
                log = _event_log_for_bot(cfg, bot_name)
                rows = log.read(raw_sid, limit=limit)
                if bot_name != "default":
                    rows = [
                        {**row, "sid": _display_session_id(bot_name, str(row.get("sid", "")))}
                        for row in rows
                    ]
            else:
                rows = _read_all_recent_events(cfg, limit=limit)
            self._json(200, rows)
            return

        if path == "/api/log":
            lines_n = min(int(qs.get("lines", ["40"])[0] or 40), 200)
            log_path = _err_log_path(cfg.state_dir)
            self._json(200, {"lines": _tail(log_path, lines_n)})
            return

        self._text(404, "Not found", "text/plain; charset=utf-8")


class WebConsole:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._http: _ConsoleServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.cfg.console_enabled:
            return
        host = self.cfg.console_host
        port = self.cfg.console_port
        try:
            self._http = _ConsoleServer((host, port), self.cfg)
        except OSError as exc:
            import logging
            logging.getLogger("cursor_bridge").warning(
                "Web console could not bind %s:%s — %s", host, port, exc,
            )
            return
        self._thread = threading.Thread(
            target=self._http.serve_forever,
            name="cursor-bridge-webconsole",
            daemon=True,
        )
        self._thread.start()
        import logging
        auth = " (token required)" if self.cfg.console_token else ""
        logging.getLogger("cursor_bridge").info(
            "Web console at http://%s:%s%s", host, port, auth,
        )

    def stop(self) -> None:
        if self._http is not None:
            self._http.shutdown()
            self._http.server_close()
            self._http = None
