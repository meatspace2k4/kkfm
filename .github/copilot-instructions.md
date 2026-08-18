# KKFM Aircheck - AI Agent Instructions

## Big picture
- C++ player in aircheck_pi.cpp handles decoding/mixing (Decoder/Mixer/Track), scheduling (Radio/CalendarManager), and overlays via plan_overlay().
- Python watchdog in watchdog/ monitors player logs and assets; log lines are the interface between C++ and watchdog.
- Asset pipeline: CSV schedule data in radio/data/*.csv + BWAV files are packed by radio/packer.py into assets.h/assets_blob.bin used by the C++ player.

## Critical coupling rules
- C++ log patterns for jingles/commercials must match watchdog/log_parser.py (exact strings like "[RADIO] Jingle picked/start/end ..."). If you change logs, update parser/tests.
- BWAV parsing must stay in sync between BWAVParser::parse in C++ and watchdog/bwav.py: header >=96 bytes, magic "BWAV" at offset 0, BOM 0xFEFF at offset 4, channels at offset 14 (mono), sample rate at offset 20, total samples at offset 24.
- New events/jingles require updates in both C++ EventId and radio/packer.py EVENT_IDS, plus CSV rows in radio/data/CalendarEventParam.csv and radio/data/RadioJingle.csv, then rerun python3 radio/packer.py.

## Workflows (non-obvious)
- C++ tests (mocked hardware): g++ -DKKFM_TEST_MODE tests_cpp.cpp -o tests_cpp && ./tests_cpp
- Build player: g++ -O2 -pthread aircheck_pi.cpp -o aircheck_pi -lasound
- Watchdog tests: python3 -m pytest watchdog/tests/
- Radio/packer tests: python3 -m pytest radio/tests/
- Watchdog self-test: python3 watchdog_run.py selftest
- Run watchdog monitors: python3 watchdog_run.py monitor-logs --since now (optional --auto-restart) or python3 watchdog_run.py monitor-tx
- Asset scan: python3 watchdog_run.py scan-assets

## Conventions and constraints
- Real-time audio path (AudioPump/DAC callback) must avoid allocations; Radio::ctx_cache holds parsed AudioCtx, so invalidate if parse logic changes.
- Asset files (.bwav, radio/Sound/) are gitignored; regenerate assets_blob.bin via python3 radio/packer.py after any asset/CSV change.
- Low-RAM target (Pi). Avoid heavy runtime dependencies; see .vscode/settings.json exclusions.

## Ops & integration points
- Systemd: aircheck.service is the player; watchdog can auto-restart it (see watchdog_run.py and watchdog/README.md). Polkit rule helper: watchdog/setup_restart_without_sudo.sh.
- Env vars used by player: AIRCHECK_DEBUG, AIRCHECK_STREAM_PORT, AIRCHECK_AUDIO_DEVICE, AIRCHECK_DISABLE_DAC.
- FM transmitter integration: Arduino sketch in radio/si4713_sketch/ and watchdog tx monitor.
