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
  echo "== building hourly_bgm"
  $CXX $CXXFLAGS hourly/hourly_bgm.cpp StreamServer.cpp -o hourly_bgm $LDLIBS
  ls -la hourly_bgm
}

if [[ "${1:-}" == "player" ]]; then
  build_player
  exit 0
fi

echo "== radio C++ suite (engine parity)"
$CXX $CXXFLAGS -DKKFM_TEST_MODE tests_cpp.cpp StreamServer.cpp -o "$OUT/tests_cpp" -pthread
"$OUT/tests_cpp" -v

echo "== hourly C++ suite"
$CXX $CXXFLAGS -DKKFM_TEST_MODE hourly/tests_hourly.cpp StreamServer.cpp -o "$OUT/hourly_tests" -pthread
"$OUT/hourly_tests" -v

echo "== python suite"
# The watchdog package imports as `kkfm.watchdog`; give it a parent named kkfm.
PYROOT="$(mktemp -d)"
ln -s "$ROOT" "$PYROOT/kkfm"
PYTHONPATH="$PYROOT" python3 -m unittest hourly.tests.test_hourly -v

echo "== watchdog suite (isolated out dir so fixtures never reach the dashboard)"
WDOUT="$(mktemp -d)"
PYTHONPATH="$PYROOT" WATCHDOG_OUT_DIR="$WDOUT" python3 -m watchdog.run selftest -v \
  | grep -E "hourly_bgm|run,|✗" || true
PYTHONPATH="$PYROOT" WATCHDOG_OUT_DIR="$WDOUT" python3 -m unittest watchdog.tests.test_hourly_bgm_detector
rm -f "$PYROOT/kkfm"; rmdir "$PYROOT"

build_player
echo "== done; outputs in $OUT"
