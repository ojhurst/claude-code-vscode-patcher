#!/bin/bash
# Install (or remove) a launchd agent that re-applies the patches whenever
# the Claude Code extension updates. macOS only.
#
#   bash install-watcher.sh              install the watcher
#   bash install-watcher.sh --uninstall  remove it
#
# On Windows / Linux there is no bundled watcher — re-run
# `python3 patch-extension.py` yourself after an extension update.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.claude-code-vscode-patcher.watcher"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
WATCHER="$REPO_DIR/claude-code-vscode-repatcher.sh"

if [ "${1:-}" = "--uninstall" ]; then
    launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    echo "Watcher removed."
    exit 0
fi

if [ "$(uname)" != "Darwin" ]; then
    echo "The launchd watcher is macOS only."
    echo "On this platform, re-run 'python3 patch-extension.py' after a Claude"
    echo "Code extension update."
    exit 1
fi

chmod +x "$WATCHER"
mkdir -p "$HOME/Library/LaunchAgents"

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$WATCHER</string>
    </array>
    <key>WatchPaths</key>
    <array>
        <string>$HOME/.vscode-server/extensions</string>
        <string>$HOME/.vscode/extensions</string>
    </array>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
PLIST

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"

echo "Watcher installed: $PLIST"
echo "It re-applies the patches whenever the Claude Code extension updates."
echo "Logs: $REPO_DIR/logs/watcher.log"
echo "Remove it any time with: bash install-watcher.sh --uninstall"
