"""The weather bridge: the hourly player's weather kept to the weather source's, with no dashboard open.

The dashboard's hourly profile does the same while it is on screen (dashboard.py _poll_weather);
this is that loop alone, for a LaunchAgent on the Mac that has the Weather app:

  the source (weather.py: the macOS Weather app's data, mirrored by hourly/mac/WeatherWatch.swift)
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
import socket
import subprocess
import sys
import time
import urllib.request
from typing import Callable, Optional

try:
    from .weather import WeatherFollower, WeatherSource, make_source
except ImportError:   # run as a script beside weather.py
    from weather import WeatherFollower, WeatherSource, make_source  # type: ignore[no-redef]


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
                 say: Callable[[str], None] = log) -> None:
        self.source, self.push, self.say = source, push, say
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
        if r is None:
            return
        state = r.hourly_at(now)
        if state != self.state:
            self.say(f"state now: {state if state is not None else 'unknown (the data does not cover this hour)'}")
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
    args = ap.parse_args(argv)
    source = make_source(args.weather_source)
    if source is None:
        print("no weather source", file=sys.stderr)
        return 2
    tunnel = Tunnel(args.ssh, args.port) if args.ssh else None
    bridge = Bridge(source, http_push(tunnel.base if tunnel else (lambda: f"http://127.0.0.1:{args.port}")))
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
