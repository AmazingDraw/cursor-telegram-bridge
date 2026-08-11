#!/bin/bash
# cursor-telegram-bridge live console — double-click to open a Terminal dashboard.
# Shows session status ("SESSION LIVE" when an agent is running) and tails
# the bot log. Safe to close with Ctrl-C; the bot itself keeps running.

set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

if [ ! -x "$DIR/.venv/bin/python" ]; then
    echo "No virtualenv found at .venv."
    exit 1
fi

exec "$DIR/.venv/bin/python" -m cursor_bridge.console
