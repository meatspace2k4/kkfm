# KKFM hourly BGM fork

Plays the ACNH 24-hour background music on the kkfm aircheck engine instead of the
radio schedule. Same codebase, new `main` (`hourly/hourly_bgm.cpp`). The engine
(`acnh_aircheck_pi.cpp`) is imported as a facade by `hourly/hourly_core.h`; the fork
adds no decoder, no playback path and no external libraries.

## What it does

- **Hourly pick.** Local wall-clock hour `0..23` (0 = 12 AM, 23 = 11 PM) selects
  `BGM_24Hour_HH_<Weather>.bwav`. Deterministic; no rotation, jingles or commercials,
  and no fallback to radio tracks (a missing file is an error log and a retry).
- **Weather variant.** `<Weather>` is Sunny / Rainy / Snowy from the in-memory "now"
  observation for Salt Lake City proper (Open-Meteo, lat 40.7608 lon -111.8910,
  fetched with `curl`). Refreshed at :14, :29, :44, :59 (every 15 min, one minute
  early). A refresh only updates memory; it never changes the playing track. The
  pick reads memory and never fetches.
- **Playback.** The track starts at frame 0 and the engine's `Decoder` loops it.
  When the hour changes the player calls `arm_fade_at_next_loop(FADE_PLAY_DURATION,
  FADE_TIME_DURATION)`: the track is not interrupted, it reaches its loop point, the
  radio's own end-of-song fade runs (10 s play + 7 s fade), then 1 s of silence,
  then the next hour's file starts at frame 0.
- **Output.** Existing `DacOutput` (miniaudio), `PCMBuffer` and `StreamServer`
  (`/stream`), so the watchdog dashboard play toggle and `--ssh` mode work unchanged.
  Log lines parse with the existing watchdog (`Now Playing: …`, `[FILE] …`), and a
  `Now Playing:` heartbeat every 240 s keeps the 300 s dead-air detector quiet.

## Engine changes (see `hourly/patches/kkfm-hourly.patch`)

The BGM files are 2-channel BWAV (two ADPCM channel streams per file). The engine now
handles that natively:

- `AudioCtx` carries per-channel data/coeffs/history (`ch[0..1]`); the channel-0
  fields are unchanged.
- `BWAVParser::parse_bytes` accepts 1 or 2 channels (channel-info blocks at
  `16 + ch*0x4C`); `parse(const AssetMeta&)` calls it.
- `Decoder::decode_block(out_l, out_r)` runs the per-sample ADPCM step for each
  channel; the loop / history reset / fade / exhaustion logic is untouched and shared.
  `arm_fade_at_next_loop()` defers the radio fade to the next loop hit; `loops_hit()`.
- `Track::pull_stereo`, `Mixer::mix_block` mix L and R. Mono radio assets produce
  L == R exactly as before (`tests_cpp.cpp` still passes).
- `StreamServer.cpp`: portable `getcwd` fallback so it builds on macOS.

## Watchdog

The watchdog understands the fork's log lines (`watchdog/log_parser.py`,
`watchdog/detector.py`) and raises anomalies on its own:

| Code | Trigger |
|---|---|
| `BGM_FAILED_TO_LOAD` | `[HOURLY] BGM failed to load: …` |
| `BGM_HOUR_MISMATCH` | picked hour ≠ wall-clock hour of the pick |
| `BGM_WEATHER_MISMATCH` | picked variant ≠ last fetched weather |
| `BGM_PICK_WHILE_PLAYING` | a new pick before the previous track ended (track cut) |
| `BGM_END_WITHOUT_CHANGEOVER` | track ended with no hour change armed |
| `BGM_NAME_MISMATCH` | changeover/end names a different file than picked |
| `BGM_CHANGEOVER_NO_END` | fade armed but no track end within 300 s |
| `BGM_END_NO_PICK` | track ended but no next pick within 30 s |
| `WEATHER_FETCH_FAILED` | `[HOURLY] Weather fetch failed` |
| `WEATHER_STALE` | no successful fetch for 60 min |
| `RADIO_OFFLINE` | `monitor-logs --stdin`: the player's log stream closed (process exited) |

`monitor-logs --stdin` now ticks once a second (timeout checks, status file), so
the dashboard's RADIO LOGIC MONITOR row stays fresh between the hourly lines and
the heartbeat. In local mode (no systemd) that row reports the monitor as up when
its status file is fresh and its recorded PID is alive. The selftest suite writes
its fixture anomalies under its own temp dir, never into the live runtime file.

Run the service with the monitor attached (production shape, stdout piped):

```bash
ACNH_AUDIO_DEVICE=BlackHole ./hourly_bgm 8791 2>&1 | python3 -m watchdog.run monitor-logs --stdin
```

Dashboard (local mode must be allowed explicitly):

```bash
WATCHDOG_ALLOW_LOCAL_DASHBOARD=1 python3 -m watchdog.run dashboard --stream-port 8791
```

## Build and test

```bash
bash hourly/build.sh          # radio C++ suite, hourly C++ suite, Python suite, player binary
bash hourly/build.sh player   # player binary only
```

Outputs land in `hourly/out/`: `loop_once_fade.wav` (hour 00 Sunny decoded by the
engine: frame 0, loop once, 10 s play, 7 s fade) and `loop_once_fade.png`
(waveform + spectrogram, cyan marks at loop start, loop end, fade start, fade end).

Audio files are read from `hourly/Stream/` (gitignored symlink locally; on the Pi
the directory holds the `.bwav` files). Override with `HOURLY_SOUND_DIR`.

## Run (localhost)

```bash
ACNH_AUDIO_DEVICE=BlackHole ACNH_LOCALHOST_ONLY=1 ./hourly_bgm 8791
```

`ACNH_LOCALHOST_ONLY` binds `::1` (engine behaviour); reach the stream at
`http://[::1]:8791/stream` or `http://localhost:8791/stream`. Port 8765 is taken by
another local service on this Mac, so use another port.

Env (same as the radio): `ACNH_STREAM_PORT`, `ACNH_LOCALHOST_ONLY`,
`ACNH_AUDIO_DEVICE`, `ACNH_AUDIO_DEVICE_INDEX`, `ACNH_DISABLE_DAC`.
Fork env: `HOURLY_SOUND_DIR`, `HOURLY_HEARTBEAT_S`, `HOURLY_WEATHER_URL`,
`HOURLY_WEATHER_OVERRIDE=Sunny|Rainy|Snowy`.
Test hooks (logged in red when set): `HOURLY_TEST_LOOPS_BEFORE_SWITCH=N` (arm the fade
after N loop hits instead of waiting for the hour), `HOURLY_TEST_MAX_TRACKS=N` (exit
after N tracks), `HOURLY_WAV_DUMP=path` (tee the mixer output to a WAV).

Deployment to the Pi is manual and not part of this fork's tooling.
