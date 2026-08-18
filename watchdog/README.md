## KKFM watchdog (in-repo)

This watchdog is implemented **inside this repo** and can:
- **Monitor service logs** for playback anomalies (jingle/commercial missing start/end, failed to load, DAC overflow/throttle).
- **Scan BWAV assets** for filesystem/header integrity issues (0-byte files, bad BWAV header, `assets.h` size=0, blob range errors).

It relies on logs emitted by `aircheck_pi`, especially:
- `[RADIO] Jingle picked/start/end ...`
- `[RADIO] Commercial picked/start/end ...`
- `ERROR: [RADIO] ... failed to load ...`
- `[DAC] Ring buffer ...`

### Run (no external service)

Install deps (needed for `monitor-tx`):

```bash
python3 -m pip install -r kkfm/watchdog/requirements.txt
```

Optional editable install (so `import watchdog` works regardless of cwd):

```bash
python3 -m pip install -e kkfm/watchdog
```

Run log monitor (live):

```bash
python3 kkfm/watchdog_run.py monitor-logs --since now
```

### Auto-restart (self-healing)

The watchdog can optionally restart the main player service automatically when it detects:
- **Dead air** (`RADIO_DEAD_AIR`, critical): no log activity for `--dead-air-timeout` seconds.
- **Persistent underrun** (`DAC_RING_UNDERRUN`, warn): $N$ underruns within a time window.

Enable via CLI:

```bash
python3 kkfm/watchdog_run.py monitor-logs --since now --auto-restart
```

Or via env vars (recommended for systemd services):

- `WATCHDOG_AUTO_RESTART=1`
- `WATCHDOG_AUTO_RESTART_COOLDOWN_S` (default `600`)
- `WATCHDOG_AUTO_RESTART_MAX_PER_HOUR` (default `3`)
- `WATCHDOG_AUTO_RESTART_DEAD_AIR` (default `1`)
- `WATCHDOG_AUTO_RESTART_UNDERRUN` (default `1`)
- `WATCHDOG_AUTO_RESTART_UNDERRUN_COUNT` (default `10`)
- `WATCHDOG_AUTO_RESTART_UNDERRUN_WINDOW_S` (default `120`)

Important:
- To restart `aircheck.service`, the watchdog process must have permission to manage that unit.
  The simplest safe option is running the watchdog systemd service as `root`.
- If you run the watchdog as an unprivileged user, you can install a Polkit rule that allows restart
  for only the required units.

Run transmitter heartbeat monitor (UNO serial; live):

```bash
python3 kkfm/watchdog_run.py monitor-tx
```

Run asset scan (one-shot):

```bash
python3 kkfm/watchdog_run.py scan-assets
```

Run regression suite:

```bash
python3 kkfm/watchdog_run.py selftest
```

### Restart systemd services without sudo

If you run the watchdog monitors as system services (e.g. `aircheck-watchdog.service` and
`aircheck-tx-watchdog.service`) and want to restart them without `sudo`, you can install a
Polkit rule that allows *only those two units* to be restarted by a chosen user.

Install (run once on the Pi):

```bash
sudo ./kkfm/watchdog/setup_restart_without_sudo.sh $USER
```

After that, as that user:

```bash
systemctl restart aircheck-watchdog.service
systemctl restart aircheck-tx-watchdog.service
```

### Outputs

By default, outputs go under:
- `kkfm/watchdog/out/`
  - `anomalies.jsonl`
  - `flags/*.flag`

You can change the output directory via `--out-dir`.

Defaults can also be overridden via env vars:
- `WATCHDOG_OUT_DIR`
- `WATCHDOG_STREAM_DIR`
- `WATCHDOG_ASSETS_H`
- `WATCHDOG_ASSETS_BLOB`
