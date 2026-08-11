# cursor-telegram-bridge

Control headless [Cursor](https://cursor.com) agent sessions on your Mac from a
Telegram bot. Start a session in any folder, send prompts from your phone, watch
the agent work in a single live-updating message (tool activity, code snippets,
and formatted final reply) — all without touching the Mac.

**中文说明：** [README.zh-CN.md](README.zh-CN.md)

There is no Cursor GUI involved: each session is a Cursor SDK *local agent*
running against a folder on your machine.

See [CHANGELOG.md](CHANGELOG.md) for bug fixes and release notes.

## Quick start

```bash
brew install python@3.12  # skip if you already have Python 3.11+
git clone https://github.com/AmazingDraw/cursor-telegram-bridge.git
cd cursor-telegram-bridge
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -m cursor_bridge
```

Leave `ALLOWED_TELEGRAM_USER_ID` blank for the first run. Message your new bot;
it replies with your numeric Telegram id. Add it to `.env`, restart, and use
`/new` to choose a project folder.

Double-click `start.command` on the Mac for a foreground launcher that survives
soft `/restart` without closing the window.

## How it works

```
Telegram (phone)  <->  cursor-telegram-bridge (Mac)  <->  Cursor SDK bridge  ->  local agents per folder
```

- One long-running Python process listens for your Telegram messages.
- A **session registry** tracks every live agent (folder, id, status) and is
  mirrored to ``state/bots/<bot>/sessions.json``, so sessions survive a restart (they are
  re-attached with `Agent.resume`).
- **One Cursor bridge is launched per folder.** At spawn time the bridge
  subprocess is started with that folder as its cwd (injected into the
  subprocess call — the bot process itself does not `chdir`). Each distinct
  folder gets its own bridge; the bridge is closed when the last session in
  that folder ends.
- The bot is locked to **your** Telegram user id — it ignores everyone else.
- Use `/use <id>` or the session list buttons to pick the **active** session
  before sending prompts. The bot does not auto-switch to another session.

## Commands

| Command | What it does |
| --- | --- |
| `/new [path]` | Start a session. With no path, shows a tappable folder picker. |
| `/browse [path]` | Navigate folders with inline buttons, then "Use this folder". |
| `/cd <path>` | Start a session in an exact absolute path. |
| `/sessions` | List sessions with status badges; buttons to switch/cancel/end. |
| `/use <id>` | Set the active session for this chat. |
| `/status` | Active session details + run state + context usage. |
| `/rename <name>` | Rename the active session (`/rename reset` restores default). |
| `/compact` | Compact the active session's context (agent `/compact`). |
| `/context [list\|refresh\|<agent-id>]` | Restore prior chat context after agent reset. |
| `/model` | Pick the model for the active session. |
| `/effort` | Set reasoning/effort level (supported models only). |
| `/mode [agent\|plan]` | Show or set agent/plan mode. |
| `/busy [interrupt\|queue]` | When busy: **queue** (default) shows **排队 / 发送 / 取消** for the new message. **取消** drops only the new command. **interrupt** always cancels the run and starts the new message. |
| `/cancel` | Stop the prompt currently running in the active session. |
| `/end <id>` | Close a session (and its bridge, if it was the last in that folder). |
| `/files` | Browse files in the active session folder; tap to send to Telegram. |
| `/files find <name>` | Search files by name under the active session folder. |
| `/usage` | Cursor subscription usage, bonus credits, and active session context. |
| `/restart` | Soft restart: reloads `.env`/`config.toml` and re-attaches sessions (does **not** reload Python code). |
| `/reload` | Full launchd restart from Telegram — picks up code changes. |
| _(plain text)_ | Sent as a prompt to the active session; reply streams in one live message. |
| _(photo/file)_ | Saved to the session folder and sent to the agent (caption = optional prompt). |

Send `/start` or `/help` for the in-bot summary. The **button menu** under `/start`
includes **Files**, **Status**, **Restart** (soft), and **Reload** (full launchd restart).

Status badges: green = running, yellow = idle, red = error.

### Live run display

When you send a prompt (or `/compact`), the bot uses **one Telegram message**
that updates in place for the whole run.

**Header** (blockquote, always visible):

```
[s1] MyProject · composer-2.5
```

**While the agent works**, the message shows:

| Layer | What you see |
| --- | --- |
| Body preview | Latest assistant text (truncated if very long) |
| Activity line | Status emoji + action: 🟡 while running, ✅ when done, ❌ on failure |
| Tool snippet | Red/green monospace preview for edits, grep, shell output |
| Elapsed timer | `⏳ 12s` / `⌛ 12s` in the header bar |

Activity and tool events **force an immediate refresh** (they bypass the normal
edit throttle).

**When the run finishes**, the same message is replaced with the final reply:

- Markdown from the agent is rendered as Telegram HTML
- Status icon: ✅ finished, ✋ cancelled, or 🔴 error

Long replies are split across multiple messages (Telegram's 4096-character limit).

Implementation: `cursor_bridge/formatting.py` (`build_live_html`, `build_final_html`, `markdown_to_telegram_html`).

### Outbound files (agent → Telegram)

| Source | When | How |
| --- | --- | --- |
| **`GenerateImage`** | Live, as soon as the tool completes | Sent automatically as a photo/animation |
| **Everything else** | On demand | `/files` or `/files find <name>` |

Blocked paths (`.git`, `node_modules`, `.env`, etc.) are never listed or sent.

### Inbound files (Telegram → agent)

Send a **photo**, **document**, **animation**, **video**, or **audio**.
Optional caption becomes part of your prompt.

- Saved under `.cursor_bridge/inbound/` in the active session folder
- **Images** are passed to the agent visually (`SDKImage`)
- **Other files** are referenced by path in the prompt
- Max **20 MB** (Telegram Bot API download limit)
- Rapid multi-file sends are batched into one prompt (~1.2s window)

### `/usage`

Reads your Cursor login from the local Cursor app state DB and calls the Cursor
usage API. Shows plan name, billing period, usage percentages, included spend,
and **bonus credits**. Also includes active session context when a session is selected.

### Web console

While the bot is running, open **http://127.0.0.1:9477** on the Mac that runs
the bridge. It shows all sessions (including multi-bot setups), per-session event
history, and a live tail of the bot log.

- Optional: set `CONSOLE_TOKEN` in `.env` and open `http://127.0.0.1:9477?token=…`
- Change `console_port` in `config.toml` if the port is taken
- Disable with `console_enabled = false`
- Terminal dashboard: double-click `console.command`

Session events are stored as JSONL under `state/bots/<bot>/events/{sid}.jsonl`
(trimmed by `event_log_max` in `config.toml`). Each bot's registry lives under
`state/bots/<name>/`; the web console labels non-default sessions as `BotName:s1`.
Process logs and pid stay in `state/`.

## Prerequisites

- macOS with the [Cursor app](https://cursor.com) installed and a Cursor subscription.
- **Python 3.11+** (`brew install python@3.12`).
- A Telegram account and the Telegram app on your phone.

## Setup

### 1. Create the Telegram bot

1. In Telegram, message [@BotFather](https://t.me/BotFather).
2. Send `/newbot`, pick a name and username.
3. Copy the **bot token**.

### 2. Get a Cursor API key

1. Open [cursor.com/dashboard](https://cursor.com/dashboard) → **Integrations**.
2. Create a **User API Key** and copy it.

### 3. Install

```bash
git clone https://github.com/AmazingDraw/cursor-telegram-bridge.git
cd cursor-telegram-bridge

brew install python@3.12
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
```

Edit `.env`:

```
TELEGRAM_BOT_TOKEN_1=
TELEGRAM_BOT_TOKEN_2=
CURSOR_API_KEY=
ALLOWED_TELEGRAM_USER_ID=     # leave blank for first run
```

Optionally edit `config.toml` for `projects_root`, `model`, `[[bookmarks]]`, or
`[[bots]]` for multiple Telegram bots in one process.

### 4. First run — lock it to you

```bash
python -m cursor_bridge
```

Message your bot. Paste your numeric id into `.env`, restart, then `/new`.

### 5. Run as a background service (optional)

Install with path substitution (do **not** copy the template as-is — it still
contains `__PROJECT_DIR__` placeholders):

```bash
sed "s|__PROJECT_DIR__|$PWD|g" launchd/com.cursor-telegram-bridge.bot.plist \
  > ~/Library/LaunchAgents/com.cursor-telegram-bridge.bot.plist
launchctl load ~/Library/LaunchAgents/com.cursor-telegram-bridge.bot.plist
```

Logs: `state/cursor_bridge.out.log` and `state/cursor_bridge.err.log`.

Do **not** set `HTTP(S)_PROXY` in the plist or `.env` when using Stash/Clash
**TUN + fake-ip** — that breaks Telegram long-polling. Only set `HTTPS_PROXY`
for pure HTTP-proxy mode (no TUN); see `.env.example`.

```bash
# Check it's running (middle column 0 = healthy):
launchctl list | grep cursor-telegram-bridge

# Full restart (picks up code changes):
launchctl kickstart -k "gui/$(id -u)/com.cursor-telegram-bridge.bot"

# Or unload + load:
launchctl unload ~/Library/LaunchAgents/com.cursor-telegram-bridge.bot.plist
launchctl load   ~/Library/LaunchAgents/com.cursor-telegram-bridge.bot.plist
```

## Remote reload (from your phone)

1. Open your bot in Telegram.
2. Send **`/reload`** or tap **🔄 Reload** on the `/start` menu.
3. Wait — the bot messages you when it is back online.

| Action | Telegram | What it does |
| --- | --- | --- |
| Soft restart | `/restart` or **♻️ Restart** | Reloads `.env` / `config.toml`, re-attaches sessions. Does **not** reload Python code. |
| Full restart | `/reload` or **🔄 Reload** | Full launchd restart — picks up **code changes**. |

Wait for running sessions to finish (or `/cancel`) before `/reload`.

From another machine with SSH:

```bash
ssh you@your-mac 'launchctl kickstart -k "gui/$(id -u)/com.cursor-telegram-bridge.bot"'
```

## Configuration reference

### `.env` (secrets)

| Variable | Required | Description |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN_1` | yes | Primary bot token from [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_BOT_TOKEN_2` | no | Second bot token when `[[bots]]` uses `token_env` |
| `CURSOR_API_KEY` | yes | User API key from Cursor dashboard → Integrations |
| `ALLOWED_TELEGRAM_USER_ID` | yes | Your numeric Telegram user id |
| `CONSOLE_TOKEN` | no | If set, web console requires `?token=…` |

### `config.toml` (non-secret)

| Key | Default | Description |
| --- | --- | --- |
| `projects_root` | `~/Coding` | Root scanned for `/new` folder picker |
| `model` | `composer-2.5` | Default model for new sessions |
| `models` | — | Optional allowlist for `/model` picker |
| `effort` | — | Default effort for new sessions (model-dependent) |
| `busy_policy` | `queue` | Busy-session follow-ups: `queue` (default) or `interrupt` |
| `setting_sources` | `["user","project"]` | On-disk `.cursor/` + `~/.cursor/` (MCP/hooks/project rules). **Not** Customize User Rules |
| `rules_file` | — | Markdown file injected into every prompt (use this for personas / User Rule text) |
| `rules` | — | Inline rules string (merged with `rules_file` if both set) |
| `browser_page_size` | `20` | Entries per screen in `/browse` and `/files` |
| `event_log_max` | `500` | Max JSONL events kept per session |
| `console_enabled` | `true` | Local web dashboard on/off |
| `console_host` | `127.0.0.1` | Bind address for web console |
| `console_port` | `9477` | Port for web console |
| `[[bookmarks]]` | — | Optional pinned folders at top of `/new` |
| `[[bots]]` | — | Optional multi-bot config. Prefer `token_env = "TELEGRAM_BOT_TOKEN_2"` (secrets in `.env`); inline `token` also works |
| `permission` (per-bot) | `full` | Tool gate: `full` or `readonly` |
| `allowed_chat_ids` (per-bot) | `[]` | Whitelisted group/supergroup chat ids that may talk to this bot |

Example multi-bot with a group-reader:

```toml
[[bots]]
name = "default"
token_env = "TELEGRAM_BOT_TOKEN_1"
permission = "full"

[[bots]]
name = "group-reader"
token_env = "TELEGRAM_BOT_TOKEN_2"
permission = "readonly"
allowed_chat_ids = [-1001234567890]
```

- **`permission = "readonly"`** hard-denies Shell / write-edit-delete / GenerateImage / MCP; allows Read / Grep / Glob (etc.); every path arg must stay inside the session `cwd` (symlink escapes blocked).
- **`allowed_chat_ids`**: anyone in those groups may talk; private chats still require `allowed_user_id`; sessions are keyed by `chat.id` (one shared session per group).
- `/reload` and `/restart` (including menu buttons) remain owner-only.

## Security model

- Default: one operator per bot. Every Telegram update is checked against
  `ALLOWED_TELEGRAM_USER_ID` (or per-bot `allowed_user_id`); other users are ignored.
- Optional: open specific groups via `allowed_chat_ids`, and/or tighten tools with
  `permission = "readonly"`.
- With `full`, agents run with full tool access in the folder you select — treat
  the bot like a remote shell into that folder.
- Secrets live in `.env` (gitignored). The bot does not send `.env`, `.git`, or
  dependency folders through `/files`.
- Agent shell commands that try to stop or restart this service are blocked;
  use `/reload` or `/restart` from Telegram instead.
- The web console binds to `127.0.0.1` by default. Set `CONSOLE_TOKEN` if you
  proxy or tunnel it.

## Troubleshooting

1. **Let the run finish** or **`/cancel`** — don't send another prompt while 🟡 running.
2. **`/reload`** — full launchd restart after code changes or if the service hangs.
3. **`/model` → `composer-2.5`** — if a model keeps failing with no output.
4. **`/mode agent`** — if plan mode glitches on a model.
5. **`/restart`** — soft restart: reloads `.env` / `config.toml` from disk, does **not** reload Python code.
6. **`/compact`** — if context is huge or the session acts oddly.
7. **`/end <id>` + `/new`** — if a session shows red after restart (folder moved, can't resume).
8. **`/use <id>`** — if messages go nowhere, pick the active session explicitly.
9. **`/end` idle sessions** — each open folder keeps a bridge subprocess (~50MB+).
10. **No Telegram replies** — check `state/cursor_bridge.err.log` for proxy/connect errors; unset `HTTPS_PROXY` under TUN; confirm launchd plist paths are substituted (not `__PROJECT_DIR__`).

The bridge auto-recovers from **bridge down** and **stuck agent** errors in many
cases — check `state/cursor_bridge.err.log`. If stuck, `/reload` usually clears it.

## Notes and limitations

- **Memory:** each folder spawns its own Cursor bridge (Node, ~50MB+). Close
  sessions you are done with on a small Mac.
- Long replies are split across Telegram messages (4096-char limit).
- If a session shows red after restart, `/end` it and `/new` a fresh one.
- TLS-inspecting proxy/VPN: `export NODE_EXTRA_CA_CERTS=/path/to/root-ca.pem` before launch.

## Project layout

```
cursor_bridge/
  __main__.py     # entry: python -m cursor_bridge
  bot.py          # Telegram handlers, LiveMessage, prompt execution
  sessions.py     # SessionManager: per-folder bridges, registry, run_prompt streaming
  formatting.py   # live + final HTML, markdown→Telegram, tool activity/snippets
  attachments.py  # outbound file detection, /files browser, live GenerateImage delivery
  inbound.py      # download Telegram media → .cursor_bridge/inbound/
  folders.py      # bookmark discovery + inline-keyboard folder browser
  config.py       # load .env + config.toml
  context.py      # session context usage + /context restore
  usage.py        # /usage: Cursor subscription + bonus credits API
  events.py       # per-session JSONL event log (state/events/)
  webconsole.py   # local HTTP dashboard (session list + events + log tail)
  console.py      # optional Mac terminal live status view
config.toml       # non-secret settings
.env              # secrets (gitignored)
launchd/          # com.cursor-telegram-bridge.bot.plist
state/            # process logs/pid; per-bot sessions under bots/<name>/
start.command     # foreground launcher (survives soft /restart)
console.command   # terminal live dashboard
```

### Module map

```
Telegram  ↔  bot.py  ↔  sessions.py  ↔  Cursor SDK (AsyncClient, local bridge per folder)
                │            │
                │            ├── formatting.py   (live/final message text)
                │            ├── attachments.py  (files out + /files)
                │            └── events.py       (JSONL audit log)
                ├── inbound.py      (files in from Telegram)
                ├── folders.py      (/new, /browse pickers)
                ├── usage.py        (/usage API)
                └── webconsole.py   (browser dashboard)
```
