#!/bin/bash
# Installs the bot + 5-minute tick as macOS system services (LaunchDaemons).
# They start at boot and run as the project's owner, WITHOUT that user being logged in.
# Run from an admin account:   sudo /Users/Tharun-Personal/trading-assistant/launchd/install-daemon.sh
# Safe to re-run. Add --dry-run (no sudo needed) to only generate and check the files.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
OWNER="$(stat -f %Su "$ROOT")"
OWNER_HOME="$(dscl . -read "/Users/$OWNER" NFSHomeDirectory | awk '{print $2}')"
PY="$ROOT/.venv/bin/python"
DRY=0; [ "${1:-}" = "--dry-run" ] && DRY=1
DEST=/Library/LaunchDaemons
[ $DRY = 1 ] && DEST="$(mktemp -d)"

[ -x "$PY" ] || { echo "No venv at $PY (see README)."; exit 1; }
[ -f "$ROOT/.env" ] || { echo "Missing $ROOT/.env"; exit 1; }
case "$ROOT" in */Documents/*|*/Desktop/*|*/Downloads/*)
  echo "Move the project out of Documents/Desktop/Downloads first (macOS blocks services there)."; exit 1;; esac
if [ $DRY = 0 ] && [ "$(id -u)" != 0 ]; then echo "Run with sudo from an admin account."; exit 1; fi

echo "Project: $ROOT   (runs as: $OWNER)"
for name in tick bot; do
  label="com.user.trading-assistant.$name"
  sed -e "s#__ROOT__#$ROOT#g" -e "s#__PYTHON__#$PY#g" -e "s#__USER__#$OWNER#g" -e "s#__HOME__#$OWNER_HOME#g" \
      "$ROOT/launchd/$label.daemon.plist.template" > "$DEST/$label.plist"
  plutil -lint "$DEST/$label.plist" >/dev/null
done
if [ $DRY = 1 ]; then echo "Dry run OK: generated + validated in $DEST"; exit 0; fi

# 1) Old per-user copies (any account): stop them and disable their autostart, so only one bot polls Telegram.
for plist in /Users/*/Library/LaunchAgents/*.plist; do
  [ -f "$plist" ] || continue
  grep -q "assistant.cli" "$plist" 2>/dev/null || continue
  user="$(echo "$plist" | cut -d/ -f3)"; uid="$(id -u "$user" 2>/dev/null || echo)"
  [ -n "$uid" ] && launchctl bootout "gui/$uid" "$plist" 2>/dev/null || true
  mv "$plist" "$plist.disabled"
  echo "disabled old login item: $plist"
done
pkill -f "assistant.cli (bot|tick)" 2>/dev/null && echo "stopped running copies of the bot" || true
sleep 2

# 2) Install the services.
mkdir -p "$ROOT/logs"; chown "$OWNER" "$ROOT/logs"
for name in tick bot; do
  label="com.user.trading-assistant.$name"
  chown root:wheel "$DEST/$label.plist"; chmod 644 "$DEST/$label.plist"
  launchctl bootout "system/$label" 2>/dev/null || true
  launchctl bootstrap system "$DEST/$label.plist"
  launchctl enable "system/$label"
  echo "installed $label"
done

# 3) Never sleep while on the charger (display can still turn off). Battery settings unchanged.
pmset -c sleep 0
echo "set: no system sleep on power adapter (undo: sudo pmset -c sleep 10)"

sleep 5
for name in tick bot; do
  echo "$name: $(launchctl print "system/com.user.trading-assistant.$name" 2>/dev/null | awk '/state =/{print $3; exit}')"
done
echo "Done. Send /help to the bot from your phone to check. Logs: $ROOT/logs/"
