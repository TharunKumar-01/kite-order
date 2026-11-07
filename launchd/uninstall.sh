#!/bin/bash
set -uo pipefail
DOMAIN="gui/$(id -u)"
for name in tick bot; do
  label="com.user.trading-assistant.$name"
  launchctl bootout "$DOMAIN/$label" 2>/dev/null && echo "stopped $label"
  rm -f "$HOME/Library/LaunchAgents/$label.plist"
done
echo "Uninstalled. Data in data/ and logs/ is kept."
