#!/bin/bash
# Removes the system services. Run: sudo .../launchd/uninstall-daemon.sh  (data/ and logs/ are kept)
set -uo pipefail
[ "$(id -u)" = 0 ] || { echo "Run with sudo from an admin account."; exit 1; }
for name in tick bot; do
  label="com.user.trading-assistant.$name"
  launchctl bootout "system/$label" 2>/dev/null && echo "stopped $label"
  rm -f "/Library/LaunchDaemons/$label.plist"
done
echo "Uninstalled. To let the Mac sleep on the charger again: sudo pmset -c sleep 10"
