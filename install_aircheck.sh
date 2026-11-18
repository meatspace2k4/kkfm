#!/bin/bash
# install_aircheck.sh — build acnh_aircheck_pi and deploy to the USB mount.
#
# Default: DRY-RUN.  Prints every command it would run, no mutations.
# Pass --apply to actually execute.
#
# Usage:
#   ./install_aircheck.sh           # dry-run (default; safe)
#   ./install_aircheck.sh --apply   # actually build + deploy + restart

set -euo pipefail

SRC_DIR=/home/jpark/Aircheck/kkfm
DEPLOY_DIR=/mnt/kkfm-usb/kkfm
BIN_NAME=acnh_aircheck_pi
SERVICE=acnh-aircheck.service

APPLY=0
[[ "${1-}" == "--apply" || "${1-}" == "-x" ]] && APPLY=1

if [[ $APPLY -eq 1 ]]; then
    TAG="[apply]"
    run() { echo "+ $*"; "$@"; }
else
    TAG="[dry-run]"
    run() { echo "  would run: $*"; }
fi

echo "════════════════════════════════════════════════════════════════"
echo "  KKFM aircheck install  ${TAG}"
echo "  src    = ${SRC_DIR}"
echo "  deploy = ${DEPLOY_DIR}"
echo "  binary = ${BIN_NAME}"
echo "  service= ${SERVICE}"
echo "════════════════════════════════════════════════════════════════"
echo

# ── 1. Preconditions (always real — read-only checks) ──
echo "── preconditions ──"
[[ -d "$SRC_DIR" ]] || { echo "  FAIL: source dir missing: $SRC_DIR"; exit 1; }
[[ -f "$SRC_DIR/acnh_aircheck_pi.cpp" ]] || { echo "  FAIL: source cpp missing"; exit 1; }
[[ -f "$SRC_DIR/StreamServer.cpp"     ]] || { echo "  FAIL: StreamServer.cpp missing"; exit 1; }
[[ -f "$SRC_DIR/assets.S"             ]] || { echo "  FAIL: assets.S missing (run packer.py first?)"; exit 1; }
mountpoint -q "$DEPLOY_DIR" 2>/dev/null || \
    [[ -d "$DEPLOY_DIR" ]] || { echo "  FAIL: deploy dir not mounted: $DEPLOY_DIR"; exit 1; }
systemctl cat "$SERVICE" >/dev/null 2>&1 || { echo "  FAIL: service unit missing: $SERVICE"; exit 1; }
echo "  OK: src + deploy + service all present"
echo

# ── 2. State before ──
echo "── current state ──"
[[ -f "$SRC_DIR/$BIN_NAME" ]] && \
    echo "  src binary    : $(stat -c '%y  %s bytes' "$SRC_DIR/$BIN_NAME")" || \
    echo "  src binary    : (none yet)"
[[ -f "$DEPLOY_DIR/$BIN_NAME" ]] && \
    echo "  deploy binary : $(stat -c '%y  %s bytes' "$DEPLOY_DIR/$BIN_NAME")" || \
    echo "  deploy binary : (none yet)"
echo "  service state : $(systemctl is-active "$SERVICE" 2>/dev/null || echo unknown)"
echo

# ── 3. Build ──
echo "── build ──"
run cd "$SRC_DIR"
run g++ -o "$BIN_NAME" acnh_aircheck_pi.cpp StreamServer.cpp assets.S \
       -std=c++17 -pthread -I. -ldl -lm
echo

# ── 4. Deploy ──
echo "── deploy ──"
run sudo cp "$SRC_DIR/$BIN_NAME" "$DEPLOY_DIR/$BIN_NAME"
echo

# ── 4b. Units: the radio's and the hourly player's systemd units and drop-ins, from the repo ──
echo "── units ──"
UNIT_DIR=/etc/systemd/system
install_unit() {   # install_unit <unit file in the repo> [<drop-in dir in the repo>]
    local unit="$1" dropins="${2-}" name
    name=$(basename "$unit")
    run sudo install -m 644 "$unit" "$UNIT_DIR/$name"
    if [[ -n "$dropins" ]]; then
        run sudo install -d -m 755 "$UNIT_DIR/$name.d"
        for f in "$dropins"/*.conf; do run sudo install -m 644 "$f" "$UNIT_DIR/$name.d/"; done
    fi
}
install_unit "$SRC_DIR/deploy/acnh-aircheck.service" "$SRC_DIR/deploy/acnh-aircheck.service.d"
install_unit "$SRC_DIR/hourly/deploy/acnh-hourly.service" "$SRC_DIR/hourly/deploy/acnh-hourly.service.d"
install_unit "$SRC_DIR/hourly/deploy/acnh-hourly-watchdog.service" "$SRC_DIR/hourly/deploy/acnh-hourly-watchdog.service.d"
run sudo systemctl daemon-reload
echo

# ── 5. Restart service ──
echo "── restart ──"
run sudo systemctl restart "$SERVICE"
echo

# ── 6. Verify (only on --apply) ──
if [[ $APPLY -eq 1 ]]; then
    echo "── verify ──"
    sleep 2
    echo "  deploy binary : $(stat -c '%y  %s bytes' "$DEPLOY_DIR/$BIN_NAME")"
    echo "  service state : $(systemctl is-active "$SERVICE")"
    systemctl status --no-pager -l "$SERVICE" | head -8
else
    echo "── verify (skipped in dry-run) ──"
fi

echo
echo "${TAG} done."
[[ $APPLY -eq 0 ]] && echo "Re-run with --apply to actually execute."
