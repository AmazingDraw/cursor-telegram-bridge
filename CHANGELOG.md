# Changelog

All notable changes to cursor-telegram-bridge are documented here.

## Unreleased

### Added

- Hung-run watchdog: abort a session when the Cursor agent produces no
  tool/text progress for `run_stall_timeout_sec` (default 180s). On the
  **first** stall, auto-send「继续」once on the same agent (same as typing
  continue); a second stall still aborts with a clear Telegram message.
- Health probe: track Telegram poll failures / updater liveness; soft-restart
  first, then escalate to launchd `kickstart` after
  `health_kickstart_after_soft` (default 2). Notifies the allowlisted user.
- Per-bot Telegram HTTP isolation: dedicated `get_updates_request` pool so
  long-polling cannot starve `sendMessage` / edits (`PoolTimeout` class).
- Instant-empty SDK glitch now updates the live Telegram bubble while the
  agent is recreated and auto-retried.

### Fixed

- Plan delivery no longer nudges the model to re-paste `createPlan` into chat
  (bridge already injects the tool plan). Prefix now says: short conclusion OK,
  do not duplicate the full plan. `resolve_final_body` drops assistant text when
  it looks like a second plan document (avoids `正文 + --- + plan` doubles).
- Plan / long-document Telegram HTML breathes: headings get blank lines before
  and after; consecutive list items are spaced. Major headings (`#` / `##`)
  render as bold `【title】` (idempotent if already bracketed); `###`+ stay
  bold-italic without brackets.

### Changed

- Telegram API pool: `connection_pool_size=32`, `pool_timeout=30s`, keepalive
  expiry 5s (was 16 / 5s / 2s; pool_timeout was briefly 20s). Poll pool is
  separate (size 4, read 60s).
- Health probe default interval `health_check_interval_sec` is now 60s
  (was 45s) — slightly less chatty, still well under `health_quiet_sec`.

### Added (prior)

- Multi-bot session state is peer-aligned under `state/bots/<name>/`
  (legacy `state/sessions.json` + `state/events/` auto-migrate to
  `state/bots/default/`).
- Multi-bot `[[bots]]` can load tokens from `.env` via `token_env` (keeps
  secrets out of `config.toml`).
- Local agents load on-disk Cursor settings via `setting_sources` (default
  `["user", "project"]`) on create and resume. Customize → User Rules are
  cloud/IDE-only and are **not** included — use `rules_file` / `rules` to
  inject those into every Telegram prompt.
- Busy-session follow-ups: default `busy_policy = "queue"` stages new messages
  with **排队 / 发送 / 取消** (**取消** drops only the new command). Use
  `"interrupt"` or `/busy interrupt` to always cancel-and-run.

### Fixed

- Mid-stream Cursor bridge disconnects (`incomplete chunked read`, etc.) now
  retry up to 3 times like send-time recovery (was a single recovery only).
- Drop the live “取消任务” button under thinking (use `/cancel` or Sessions); stop
  leaving a stale `Cancelling…` bubble after a successful cancel.
- Context restore failed for project paths with spaces (e.g. `GitHub Copilot`)
  because the transcript slug did not match Cursor's on-disk naming; `/context`
  also no longer claims "no prior agents" when ids exist but transcripts are
  missing, and the stuck-agent reset note only suggests `/context` when restore
  is actually possible.
- Live message `force=True` updates flush immediately (activity / tool events
  no longer sit behind the edit throttle).
- Bridge launch no longer uses process-global `os.chdir`; cwd is injected into
  the subprocess spawn so concurrent awaits stay safe.
- Per-session agent lock serializes background resume vs prompt send / recreate,
  avoiding races right after startup.
- Custom event-loop startup calls PTB `post_init` / `post_shutdown` so the web
  console and session resume run correctly.
- Telegram HTTP client uses `trust_env=False` and optional explicit proxy only;
  forced `:7890` proxy under Stash/Clash TUN + fake-ip no longer breaks polling.
- Restored IPv4-first DNS for `api.telegram.org` where needed.
- `/cancel` cancels the SDK run before cancelling the asyncio task.
- launchd module is `cursor_bridge` (not legacy package names); logs under
  `state/cursor_bridge.*.log`.

### Changed

- Project naming and docs aligned to `cursor_bridge` / cursor-telegram-bridge
  (legacy upstream package paths removed).

## 0.1.0 - 2026-06-22

Initial public release (forked lineage).

### Added

- Telegram control for headless Cursor SDK local agents running on a Mac.
- Per-folder sessions with restart persistence, session switching, cancellation,
  model selection, agent/plan mode selection, context usage, and `git diff`
  review from chat.
- Live in-place run updates with assistant preview text, elapsed time, tool
  activity, command/edit snippets, and formatted final replies.
- Folder and file browsers for starting sessions and sending generated files
  back to Telegram.
- Inbound media support for photos, documents, animations, video, and audio,
  saved into the active project and passed to the agent.
- Automatic live delivery for generated images, plus on-demand delivery for
  other safe project files.
- Cursor usage summary, local web console, foreground launcher, and optional
  launchd service definition.
- Packaging metadata, CI smoke tests, and focused pytest coverage for release.

### Security

- Single-user Telegram allowlist enforced on every update.
- Secrets stay in `.env`, which is ignored by git.
- File delivery blocks `.env`, `.git`, virtual environments, dependency folders,
  Cursor app state, and other sensitive or noisy paths.
- The web console binds to `127.0.0.1` by default and can be protected with
  `CONSOLE_TOKEN`.
- Agent shell commands that try to stop or restart this service are blocked;
  use `/restart` or `/reload` from Telegram instead.

### Improved

- Plan-mode output is delivered back to Telegram as a readable final answer,
  instead of being easy to miss when the SDK emits plan data separately from the
  assistant text stream.
- Live status messages filter noisy SDK lifecycle events so transient internal
  statuses do not appear as user-facing errors.
- Session lists and status views show the user's prompt rather than internal
  Telegram delivery instructions.
- Fresh-start behavior is now generic for public installs; private local session
  recovery code was removed from the release copy.
