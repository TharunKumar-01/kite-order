#!/bin/bash
# Installs the two launchd agents for the current user. Safe to re-run.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
DEST="$HOME/Library/LaunchAgents"
DOMAIN="gui/$(id -u)"
[ -x "$PY" ] || { echo "No venv at $PY. Create it first (see README)."; exit 1; }
[ -f "$ROOT/.env" ] || { echo "Missing $ROOT/.env (copy .env.example)."; exit 1; }
mkdir -p "$DEST" "$ROOT/logs"
for name in tick bot; do
  label="com.user.trading-assistant.$name"
  sed -e "s#__ROOT__#$ROOT#g" -e "s#__PYTHON__#$PY#g" \
      "$ROOT/launchd/$label.plist.template" > "$DEST/$label.plist"
  launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
  launchctl bootstrap "$DOMAIN" "$DEST/$label.plist"
  echo "installed $label"
done
echo "Check with: launchctl print $DOMAIN/com.user.trading-assistant.tick | head -20"
