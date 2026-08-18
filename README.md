# KKFM Aircheck

A self-hosted radio station. A C++ player mixes a scheduled program out of a
library of tracks, jingles, and commercials, pushes the mix to an FM
transmitter (Si4713) for over-the-air, and serves the same audio over HTTP so
browsers can listen live. A watchdog keeps the whole thing on the air and a
small web page lets you tune in and see what's playing.

## Layout

    aircheck_pi.cpp        the player: schedule, mixer, DAC/ALSA output, Si4713 feed
    StreamServer.*         HTTP audio server (chunked WAV/PCM), serves browser/
    AudioDefs.h PCMBuffer.h Utils.h   shared audio types and the ring buffer
    miniaudio.h            third-party decoder (public domain / MIT-0)
    browser/               the listener page and the status dashboard
    radio/                 Si4713 sketches (radio/si4713*), player test suite
    watchdog/              log/TX monitors, recovery, and the dashboard readouts
    aircheck-status/            the standalone dashboard (log endpoint + local server)
    monitor_tx.py          standalone TX telemetry page
    tests_cpp.cpp          C++ unit tests (KKFM_TEST_MODE)

## Build

The required tooling and assets are not included; source them, run packer.py, then compile on the target:

    python3 radio/packer.py      # writes assets.h + assets.S
    g++ -O2 -o aircheck_pi aircheck_pi.cpp StreamServer.cpp assets.S \
        -std=c++17 -pthread -I. -ldl -lm

Tests:

    g++ -DKKFM_TEST_MODE tests_cpp.cpp -o tests_cpp && ./tests_cpp
    python3 -m pytest radio/tests watchdog/tests

## Run

    ./aircheck_pi

Then open the stream host in a browser (default `:8765`). Useful env knobs:

| var | effect |
|---|---|
| `AIRCHECK_STREAM_PORT` | HTTP listen port (default 8765) |
| `AIRCHECK_LOCALHOST_ONLY` | bind loopback only |
| `AIRCHECK_AUDIO_DEVICE` / `AIRCHECK_AUDIO_DEVICE_INDEX` | pick the output device |
| `AIRCHECK_LIST_AUDIO_DEVICES` | print devices and exit |
| `AIRCHECK_DISABLE_DAC` | stream only, no local audio |
| `AIRCHECK_DEBUG` | verbose logging |

## Watchdog

The player runs unattended, so `watchdog/` supervises it: it tails the service
log and the Si4713 telemetry, catches dead air / DAC underruns / a wedged
sketch, and restarts what needs restarting. `dashboard` renders the live state
over SSH:

    python3 watchdog_run.py dashboard --ssh <user>@<host>

The browser dashboard (`aircheck-status/`) exposes the same state over plain HTTP
for anyone who'd rather watch it in a tab.
