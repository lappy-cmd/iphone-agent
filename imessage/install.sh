#!/bin/sh
# Run once on the Mac:  sh install.sh
# Starts the bridge now and at every login, and stops the Mac from sleeping.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
[ -f "$DIR/.env" ] || { echo "Create $DIR/.env first (copy .env.example)"; exit 1; }
DEST="$HOME/Library/LaunchAgents/com.iphoneagent.bridge.plist"
mkdir -p "$HOME/Library/LaunchAgents"
sed "s#BRIDGE_DIR#$DIR#g" "$DIR/com.iphoneagent.bridge.plist" > "$DEST"
launchctl unload "$DEST" 2>/dev/null || true
launchctl load "$DEST"
echo "Keeping the Mac awake (asks for your Mac password):"
sudo pmset -a sleep 0 disksleep 0
sudo pmset -a autorestart 1 2>/dev/null || true
echo "Done. Log: $DIR/bridge.log"
