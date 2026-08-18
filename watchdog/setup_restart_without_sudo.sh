#!/usr/bin/env bash
set -euo pipefail

# Allow a specific user to restart the watchdog services without sudo.
#
# This installs a Polkit rule for systemd's "manage-units" action limited to:
#   - aircheck-watchdog.service
#   - aircheck-tx-watchdog.service
#
# Usage:
#   sudo ./watchdog/setup_restart_without_sudo.sh <username>
#   sudo ./watchdog/setup_restart_without_sudo.sh <username> 1   # also allow restarting aircheck.service
#
# After installation, the user can run:
#   systemctl restart aircheck-watchdog.service
#   systemctl restart aircheck-tx-watchdog.service
# If you pass a second arg of "1", it also allows: aircheck.service

if [[ ${EUID:-0} -ne 0 ]]; then
  echo "ERROR: run as root (use sudo)" >&2
  exit 1
fi

TARGET_USER="${1:-}"
ALLOW_MAIN="${2:-0}"
if [[ -z "$TARGET_USER" ]]; then
  echo "Usage: $0 <username>" >&2
  exit 2
fi

if ! id "$TARGET_USER" >/dev/null 2>&1; then
  echo "ERROR: user '$TARGET_USER' does not exist" >&2
  exit 3
fi

RULE_PATH="/etc/polkit-1/rules.d/49-kkfm-watchdog-restart.rules"

EXTRA_UNIT_JS=""
if [[ "$ALLOW_MAIN" == "1" ]]; then
  EXTRA_UNIT_JS=' || unit === "aircheck.service"'
fi

cat >"$RULE_PATH" <<EOF
// Allow $TARGET_USER to manage only the KKFM watchdog units.
polkit.addRule(function(action, subject) {
  if (action.id === "org.freedesktop.systemd1.manage-units" && subject.user === "$TARGET_USER") {
    var unit = action.lookup("unit");
    if (unit === "aircheck-watchdog.service" || unit === "aircheck-tx-watchdog.service"$EXTRA_UNIT_JS) {
      return polkit.Result.YES;
    }
  }
});
EOF

chmod 0644 "$RULE_PATH"

echo "Installed polkit rule: $RULE_PATH"
echo "You may need to restart polkit: systemctl restart polkit (usually not required)"

echo "Test (as $TARGET_USER):"
echo "  systemctl restart aircheck-watchdog.service"
echo "  systemctl restart aircheck-tx-watchdog.service"
if [[ "$ALLOW_MAIN" == "1" ]]; then
  echo "  systemctl restart aircheck.service"
fi
