#!/usr/bin/env bash
# Restart the local hourly player on the current build, with the watchdog log monitor attached.
# Usage (repo root): bash hourly/run_local.sh [port]    env: ACNH_AUDIO_DEVICE (default BlackHole)
set -euo pipefail
cd "$(dirname "$0")/.."
PORT="${1:-8791}"
pkill -f "hourly_bgm $PORT" || true
pkill -f "watchdog.run monitor-logs" || true
bash hourly/build.sh player >/dev/null
mkdir -p hourly/out
ACNH_AUDIO_DEVICE="${ACNH_AUDIO_DEVICE:-BlackHole}" nohup sh -c "./hourly_bgm $PORT 2>&1 | tee -a hourly/out/service.log | python3 -m watchdog.run monitor-logs --stdin" > hourly/out/service_monitor.out 2>&1 &
sleep 3
pgrep -fl "hourly_bgm $PORT|monitor-logs" | grep -v "sh -c" || true
tail -3 hourly/out/service.log | sed 's/\x1b\[[0-9;]*m//g'
