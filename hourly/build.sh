#!/usr/bin/env bash
# Build and test the hourly BGM fork. Run from the repo root:
#   bash hourly/build.sh            # tests (C++ radio suite, C++ hourly suite, Python suite) + player binary
#   bash hourly/build.sh player     # player binary only
# Env: HOURLY_SOUND_DIR (default hourly/Stream), HOURLY_OUT_DIR (default hourly/out)
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
OUT="${HOURLY_OUT_DIR:-hourly/out}"
mkdir -p "$OUT"

CXX="${CXX:-g++}"
CXXFLAGS="-std=c++17 -O2 -I."
LDLIBS="-pthread -ldl -lm"

build_player() {
  echo "== building hourly_bgm (files from HOURLY_SOUND_DIR)"
  $CXX $CXXFLAGS hourly/hourly_bgm.cpp StreamServer.cpp -o hourly_bgm $LDLIBS
  ls -la hourly_bgm
}

# Packed build: the 72 BGM files are .incbin'd into the binary (like the radio).
# Usage: bash hourly/build.sh pi [STREAM_DIR]   (run on the Pi; nice/ionice to spare playback)
build_packed() {
  local stream_dir="${1:-$ROOT/radio/Sound/Resource/Stream}"
  echo "== packing hourly assets from $stream_dir"
  python3 hourly/packer_hourly.py --stream-dir "$stream_dir" --root "$ROOT"
  echo "== building hourly_bgm (packed assets)"
  local NICE=""
  if command -v ionice >/dev/null 2>&1; then NICE="nice -n19 ionice -c3"; else NICE="nice -n19"; fi
  $NICE $CXX $CXXFLAGS -DHOURLY_USE_BLOB hourly/hourly_bgm.cpp StreamServer.cpp hourly_assets.S -o hourly_bgm $LDLIBS
  ls -la hourly_bgm
  echo "== packed-asset self-test"
  $NICE $CXX $CXXFLAGS -DKKFM_TEST_MODE -DHOURLY_USE_BLOB hourly/tests_hourly.cpp StreamServer.cpp hourly_assets.S -o "$OUT/hourly_tests_packed" -pthread
  "$OUT/hourly_tests_packed" -v | grep -E "packed|run,|✗"
}

case "${1:-}" in
  player) build_player; exit 0 ;;
  pi|packed) build_packed "${2:-}"; exit 0 ;;
esac

echo "== radio C++ suite (engine parity)"
$CXX $CXXFLAGS -DKKFM_TEST_MODE tests_cpp.cpp StreamServer.cpp -o "$OUT/tests_cpp" -pthread
"$OUT/tests_cpp" -v

echo "== hourly C++ suite"
$CXX $CXXFLAGS -DKKFM_TEST_MODE hourly/tests_hourly.cpp StreamServer.cpp -o "$OUT/hourly_tests" -pthread
"$OUT/hourly_tests" -v

echo "== python suite"
python3 -m unittest hourly.tests.test_hourly -v

echo "== watchdog suite (isolated out dir so fixtures never reach the dashboard)"
WDOUT="$(mktemp -d)"
WATCHDOG_OUT_DIR="$WDOUT" python3 watchdog_run.py selftest -v | grep -E "hourly_bgm|sonos|stdin|run,|✗" || true
WATCHDOG_OUT_DIR="$WDOUT" python3 -m unittest watchdog.tests.test_hourly_bgm_detector watchdog.tests.test_sonos

build_player
echo "== done; outputs in $OUT"
