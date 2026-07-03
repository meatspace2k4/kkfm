#!/usr/bin/env python3
"""Now-playing sidecar: journalctl -f -> browser/nowplaying.json.

Follows the acnh-aircheck service log and mirrors the current broadcast
state into a tiny JSON file that the C++ StreamServer serves at
/nowplaying.json for the jukebox Activity page (browser/jukebox.html).

Log patterns are lifted from watchdog/log_parser.py (the TUI's parser):
  - "Now Playing: <title>"            -> music phase, English song title
  - "[RADIO] Jingle start: <bwav>"    -> jingle phase
  - "[RADIO] Commercial start: <bwav>"-> commercial phase
  - "[RADIO] Intro/Outro start: ..."  -> mc phase (K.K. talking)
  - "... end: ..."                    -> phase returns to music, title kept

Output shape:
  {"title": "K.K. Cruisin’", "phase": "music", "ts": 1730000000}

Run on the Pi (same user/dir as the audio service, so relative
browser/nowplaying.json lands next to the served files):

  cd /mnt/kkfm-usb/kkfm && python3 nowplaying_sidecar.py

Or install the systemd unit (see --print-unit).
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

NOW_PLAYING_RE = re.compile(r"Now Playing:\s+(?P<name>.+)$")
JINGLE_START_RE = re.compile(r"\[RADIO\]\s+Jingle start:\s+(?P<name>.+)$")
JINGLE_END_RE = re.compile(r"\[RADIO\]\s+Jingle end:\s+(?P<name>.+)$")
COMM_START_RE = re.compile(r"\[RADIO\]\s+Commercial start:\s+(?P<name>.+)$")
COMM_END_RE = re.compile(r"\[RADIO\]\s+Commercial end:\s+(?P<name>.+)$")
MC_START_RE = re.compile(r"\[RADIO\]\s+(?:Intro|Outro) start:\s+(?P<name>.+)$")
MC_END_RE = re.compile(r"\[RADIO\]\s+(?:Intro|Outro) end:\s+(?P<name>.+)$")

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

UNIT_TEMPLATE = """\
[Unit]
Description=KKFM now-playing JSON sidecar
After=acnh-aircheck.service
BindsTo=acnh-aircheck.service

[Service]
Type=simple
User={user}
WorkingDirectory={workdir}
ExecStart=/usr/bin/python3 {workdir}/nowplaying_sidecar.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""


def atomic_write(path: str, payload: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, path)


def follow(unit: str):
    """Yield decoded log lines from journalctl -f, restarting it if it dies."""
    while True:
        proc = subprocess.Popen(
            ["journalctl", "-u", unit, "-f", "-n", "50", "--output", "cat"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                yield ANSI_RE.sub("", line.rstrip("\n"))
        finally:
            proc.kill()
            proc.wait()
        time.sleep(2)  # journalctl exited (rotation, restart) — reattach


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--unit", default="acnh-aircheck.service")
    ap.add_argument("--out", default="browser/nowplaying.json")
    ap.add_argument("--print-unit", action="store_true",
                    help="print a systemd unit file for this sidecar and exit")
    args = ap.parse_args()

    if args.print_unit:
        print(UNIT_TEMPLATE.format(user=os.environ.get("USER", "jpark"), workdir=os.getcwd()))
        return

    state = {"title": None, "phase": "music", "ts": int(time.time())}
    # Seed the file immediately so the page never 404s while waiting for
    # the first event after a boot.
    atomic_write(args.out, state)

    for line in follow(args.unit):
        changed = False
        m = NOW_PLAYING_RE.search(line)
        if m:
            state["title"] = m.group("name").strip()
            state["phase"] = "music"
            changed = True
        elif JINGLE_START_RE.search(line):
            state["phase"] = "jingle"
            changed = True
        elif COMM_START_RE.search(line):
            state["phase"] = "commercial"
            changed = True
        elif MC_START_RE.search(line):
            state["phase"] = "mc"
            changed = True
        elif (JINGLE_END_RE.search(line) or COMM_END_RE.search(line)
              or MC_END_RE.search(line)):
            state["phase"] = "music"
            changed = True

        if changed:
            state["ts"] = int(time.time())
            try:
                atomic_write(args.out, state)
            except OSError as e:
                print(f"write failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
