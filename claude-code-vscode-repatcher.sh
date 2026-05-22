#!/bin/bash
# Re-apply the patches whenever the Claude Code extension updates.
#
# Triggered by the launchd agent that install-watcher.sh registers. Every
# Claude Code release ships a freshly built bundle that wipes the patches;
# this runs patch-extension.py to put them back. patch-extension.py is
# idempotent, so running it on every filesystem event is safe.
#
# Output goes to logs/watcher.log next to this script.

set -uo pipefail
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PATCHER="$REPO_DIR/patch-extension.py"
LOG_DIR="$REPO_DIR/logs"
LOG="$LOG_DIR/watcher.log"

mkdir -p "$LOG_DIR"
ts() { date +"%Y-%m-%d %H:%M:%S"; }

{
    echo "[$(ts)] watcher fired"
    if [ -f "$PATCHER" ]; then
        python3 "$PATCHER" 2>&1 | sed 's/^/  /'
    else
        echo "  ERROR: patcher not found at $PATCHER"
    fi
} >> "$LOG" 2>&1
