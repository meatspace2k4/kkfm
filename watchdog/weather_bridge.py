"""The weather bridge: the hourly player's weather kept to the weather source's, with no dashboard open.

The dashboard's hourly profile does the same while it is on screen (dashboard.py _poll_weather);
this is that loop alone, for a LaunchAgent on the Mac that has the Weather app:

  the source (weather.py: the macOS Weather app's data, mirrored by hourly/mac/WeatherWatch.swift)
    + NOAA MRMS radar precipitation type (mrms.py, run on the player's machine over ssh): radar
      snow overrides the app's state; with no app reading from the last two hours, the radar alone
    -> WeatherFollower (which state to send, and when: only one the player is not already in)
    -> GET /control/weather/<sunny|rainy|snowy> on the player

The player binds loopback only, so with --ssh it is reached through an SSH local forward, made
again whenever it has gone. The state is evaluated every tick at the current time (the
precipitation chart's bars minute by minute, the hour's forecast at the top of the hour), so it
changes with no new data from the app.

  python3 -m watchdog.weather_bridge --ssh jpark@aircheck-pi.local
  python3 weather_bridge.py --ssh ...      (installed beside weather.py, outside the package)
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from typing import Callable, Optional

try:
    from .weather import RAINY, SNOWY, SUNNY, WeatherFollower, WeatherSource, make_source
except ImportError:   # run as a script beside weather.py
    from weather import RAINY, SNOWY, SUNNY, WeatherFollower, WeatherSource, make_source  # type: ignore[no-redef]

# The app's reading counts for two hours (its hourly forecast carries the next hour's changeover
# with no new data); older (the app's cache stuck, as on 2026-11-20 when it sat at 00:09 for ten
# hours) it is not available and the radar decides.
APP_STALE_S = 7200.0
# A radar reading counts for 15 minutes (MRMS publishes every 2).
RADAR_STALE_S = 900.0
MRMS_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mrms.py")


class Radar:
    """NOAA MRMS precipitation type at the location (mrms.py), fetched on the player's machine
    over ssh (the script piped to its python3) or here without one, every `every` seconds.
    latest(now) -> "snow" | "rain" | "none", or None with no reading from the last 15 minutes."""

    def __init__(self, latlon: str, ssh_target: Optional[str] = None, radius: int = 0,
                 every: float = 120.0, say: Optional[Callable[[str], None]] = None,
                 run: Optional[Callable[[list[str]], str]] = None) -> None:
        self.lat, self.lon = latlon.split("/")
        self.ssh_target, self.radius, self.every = ssh_target, radius, every
        self.say = say or log
        self.run = run or self._run
        self.next = 0.0
        self.reading: Optional[dict] = None
        self.error: Optional[str] = None

    def _run(self, args: list[str]) -> str:
        if self.ssh_target:
            cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", self.ssh_target,
                   "nice", "-n", "19", "python3", "-", *args]
        else:
            cmd = [sys.executable, MRMS_SCRIPT, *args]
        with open(MRMS_SCRIPT, "rb") as script:
            return subprocess.run(cmd, stdin=script, capture_output=True, timeout=90).stdout.decode()

    def latest(self, now: float) -> Optional[str]:
        if now >= self.next:
            self.next = now + self.every
            try:
                r = json.loads(self.run([self.lat, self.lon, str(self.radius)]).strip().splitlines()[-1])
            except Exception as e:   # ssh down, a timeout, no output
                r = {"error": f"{type(e).__name__}: {e}"}
            if "error" in r:
                if r["error"] != self.error:
                    self.say(f"radar: {r['error']}")
                    self.error = r["error"]
            else:
                if self.reading is None or r["precip"] != self.reading["precip"]:
                    self.say(f"radar: {r['precip']} as of {time.strftime('%Y-%m-%d %H:%M', time.localtime(r['time']))}"
                             f" (codes {r['kinds']})")
                self.reading, self.error = r, None
        if self.reading is None or now - self.reading["time"] > RADAR_STALE_S:
            return None
        return self.reading["precip"]


def log(text: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}", flush=True)


class Tunnel:
    """An SSH local forward to the player's loopback port, made again when it has gone."""

    def __init__(self, ssh_target: str, remote_port: int) -> None:
        self.ssh_target, self.remote_port = ssh_target, remote_port
        self.proc: Optional[subprocess.Popen] = None
        self.local_port = 0

    def base(self) -> str:
        if self.proc is None or self.proc.poll() is not None:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                self.local_port = sock.getsockname()[1]
            self.proc = subprocess.Popen(
                ["ssh", "-N", "-o", "ExitOnForwardFailure=yes", "-o", "BatchMode=yes",
                 "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2",
                 "-L", f"127.0.0.1:{self.local_port}:localhost:{self.remote_port}", self.ssh_target],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.time() + 8.0
            while time.time() < deadline and self.proc.poll() is None:
                try:
                    socket.create_connection(("127.0.0.1", self.local_port), timeout=0.5).close()
                    break
                except OSError:
                    time.sleep(0.2)
        return f"http://127.0.0.1:{self.local_port}"

    def close(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
        self.proc = None


def http_push(base: Callable[[], str]) -> Callable[[str], tuple[bool, str]]:
    """push(weather) -> (acknowledged, what the player or the failure said)."""
    def push(weather: str) -> tuple[bool, str]:
        try:
            with urllib.request.urlopen(f"{base()}/control/weather/{weather}", timeout=4) as resp:
                body = resp.read().decode("utf-8", errors="replace").strip()
            return body.startswith("ok"), body
        except Exception as e:   # the tunnel down, the player restarting: tried again
            return False, f"failed ({e})"
    return push


class Bridge:
    """One tick: read the source when its data moved, evaluate the state now, push it when the
    player has not acknowledged it. What it reads and sends is logged as it changes."""

    def __init__(self, source: WeatherSource, push: Callable[[str], tuple[bool, str]],
                 say: Callable[[str], None] = log, radar: Optional[Radar] = None) -> None:
        self.source, self.push, self.say, self.radar = source, push, say, radar
        self.follower = WeatherFollower()
        self.reading = None
        self.state: Optional[str] = None
        self.failed: Optional[str] = None

    def tick(self, now: float) -> None:
        if self.source.changed():
            r = self.source.read()
            if r is None:
                self.say(f"{self.source.name}: no data")
            elif self.reading is None or (r.condition, r.as_of) != (self.reading.condition, self.reading.as_of):
                self.say(f"{r.source}: {r.condition} as of {time.strftime('%Y-%m-%d %H:%M', time.localtime(r.as_of))}")
            self.reading = r if r is not None else self.reading
        r = self.reading
        app = r.hourly_at(now) if r is not None and now - r.as_of <= APP_STALE_S else None
        # Already Rainy or Snowy: a dry minute does not end it while the chart still shows
        # precipitation ahead (2026-11-20 15:31: Rainy went Sunny with rain charted from 16:00).
        if app == SUNNY and self.state in (RAINY, SNOWY) and r.precipitation_ahead(now):
            app = self.state
        radar = self.radar.latest(now) if self.radar else None
        # Radar snow supersedes the app (it rains at the airport while it snows up the hill, and
        # the app follows the valley); otherwise the app; with no current app reading, the radar.
        if radar == "snow":
            state, why = SNOWY, "radar snow"
        elif app is not None:
            state, why = app, "app"
        elif radar is not None:
            state, why = (RAINY if radar == "rain" else SUNNY), f"radar {radar}, no current app reading"
        else:
            state, why = None, "no current app or radar reading"
        if state is None and r is None:
            return
        if state != self.state:
            self.say(f"state now: {state if state is not None else 'unknown'} ({why})")
            self.state = state
        # (a state already pending is left to its retry time: on_reading starts the wait over)
        if state != self.follower.pending:
            self.follower.on_reading(state)
        w = self.follower.due(now)
        if w is None:
            return
        self.follower.started()
        ok, said = self.push(w)
        self.follower.finished(w, ok, now)
        if ok:
            self.say(f"player: {w} ({said})")
            self.failed = None
        elif said != self.failed:   # (one line for a failure that repeats)
            self.say(f"player: {w} not sent, {said}; retrying")
            self.failed = said


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--ssh", help="user@host of the player's machine (reached through an SSH local forward)")
    ap.add_argument("--port", type=int, default=8791, help="the hourly player's HTTP port")
    ap.add_argument("--weather-source", default="weather-app")
    ap.add_argument("--interval", type=float, default=2.0, help="seconds between ticks")
    ap.add_argument("--no-radar", action="store_true", help="the app alone, no MRMS radar")
    # (0: the location's own ~1 km cell. A 5x5 box took snow 4 km up the hill as snow here while it
    # rained at 37 F, 2026-11-20 14:56)
    ap.add_argument("--radar-radius", type=int, default=0,
                    help="MRMS cells around the location that count (0: its own cell only)")
    args = ap.parse_args(argv)
    source = make_source(args.weather_source)
    if source is None:
        print("no weather source", file=sys.stderr)
        return 2
    tunnel = Tunnel(args.ssh, args.port) if args.ssh else None
    latlon = getattr(source, "latlon", None)
    radar = None if args.no_radar or not latlon else Radar(latlon, args.ssh, args.radar_radius)
    bridge = Bridge(source, http_push(tunnel.base if tunnel else (lambda: f"http://127.0.0.1:{args.port}")),
                    radar=radar)
    log(f"weather bridge: {source.name} -> the player at {args.ssh or 'this machine'} port {args.port}")
    try:
        while True:
            bridge.tick(time.time())
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0
    finally:
        if tunnel:
            tunnel.close()


if __name__ == "__main__":
    sys.exit(main())
