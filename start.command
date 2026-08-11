#!/bin/bash
# cursor-telegram-bridge launcher.
# Double-click in Finder, or run ./start.command from a terminal.
# It always runs from this script's own folder, so CWD doesn't matter.

set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

if [ ! -x "$DIR/.venv/bin/python" ]; then
    echo "No virtualenv found at .venv. Create it first:"
    echo "    uv venv --python 3.12 .venv && uv pip install -r requirements.txt"
    exit 1
fi

if [ ! -f "$DIR/.env" ]; then
    echo "No .env found. Copy .env.example to .env and fill it in."
    exit 1
fi

echo ""
echo "  cursor-telegram-bridge launcher (foreground)"
echo "  ─────────────────────────────────────────"
echo "  Logs show commands, sessions, prompts, and"
echo "  tool activity — not raw Telegram HTTP calls."
echo "  Press Ctrl-C to stop. Use /restart in Telegram"
echo "  to reload without closing this window."
echo ""
echo "  Running headless via launchd? Open console.command"
echo "  for a live \"SESSION LIVE\" dashboard on your Mac."
echo "  ─────────────────────────────────────────"
echo ""

# Keep this window alive across /restart — Python sets state/restart_requested
# when a restart is needed; if the process exits before coming back, we relaunch.
while true; do
    "$DIR/.venv/bin/python" -m cursor_bridge
    EXIT=$?
    if [ ! -f "$DIR/state/restart_requested" ]; then
        exit "$EXIT"
    fi
    echo ""
    echo "  ↻ Restarting..."
    echo ""
    sleep 2
done
