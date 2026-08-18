#!/usr/bin/env python3
import argparse
import json
import math
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

try:
    import serial  # pyserial
except ImportError as exc:
    raise SystemExit("pyserial is required. Install with: pip install pyserial") from exc

try:
    import numpy as np
except ImportError as exc:
    raise SystemExit("numpy is required. Install with: pip install numpy") from exc

try:
    import sounddevice as sd
except ImportError:
    sd = None


def parse_line(line):
    # Expected format: "<ms> <freq_mhz> <noise> <in_level> <over>"
    parts = line.strip().split()
    if len(parts) != 5:
        return None
    try:
        ms = int(parts[0])
        freq_mhz = float(parts[1])
        noise = int(parts[2])
        in_level = int(parts[3])
        over = int(parts[4])
    except ValueError:
        return None
    return {
        "ms": ms,
        "freq_mhz": freq_mhz,
        "noise": noise,
        "in_level": in_level,
        "over": bool(over),
    }


def clear_screen():
    print("\x1b[2J\x1b[H", end="")


def format_age(age):
    if age is None:
        return "never"
    return f"{age:0.2f}s ago"


def bool_label(value, true_label="OK", false_label="NO"):
    return true_label if value else false_label


def compute_dbfs(samples):
    if samples.size == 0:
        return None
    rms = float(np.sqrt(np.mean(np.square(samples))))
    if rms <= 0.0:
        return -120.0
    return 20.0 * math.log10(rms)


class SharedState:
    def __init__(self, args):
        self.lock = threading.Lock()
        self.start_time = time.monotonic()
        self.port = args.port
        self.baud = args.baud
        self.stale_seconds = args.stale_seconds
        self.min_in_level = args.min_inlevel
        self.min_audio_dbfs = args.min_audio_dbfs
        self.audio_stale_seconds = args.audio_stale_seconds
        self.audio_dead_seconds = args.audio_dead_seconds
        self.reset_cooldown = args.reset_cooldown
        self.audio_enabled = not args.no_audio
        self.frames = 0
        self.last_seen = None
        self.last_values = None
        self.last_line = None
        self.last_boot = None
        self.serial_error = None
        self.reset_count = 0
        self.last_reset = None
        self.audio_dbfs = None
        self.audio_last_seen = None
        self.audio_dead_since = None
        self.audio_error = None

    def snapshot(self):
        with self.lock:
            now = time.monotonic()
            serial_age = None if self.last_seen is None else now - self.last_seen
            audio_age = None if self.audio_last_seen is None else now - self.audio_last_seen
            return {
                "uptime_s": now - self.start_time,
                "port": self.port,
                "baud": self.baud,
                "stale_seconds": self.stale_seconds,
                "audio_stale_seconds": self.audio_stale_seconds,
                "audio_dead_seconds": self.audio_dead_seconds,
                "reset_cooldown": self.reset_cooldown,
                "audio_enabled": self.audio_enabled,
                "frames": self.frames,
                "last_seen_age": serial_age,
                "last_values": self.last_values,
                "last_line": self.last_line,
                "last_boot": self.last_boot,
                "serial_error": self.serial_error,
                "reset_count": self.reset_count,
                "last_reset_age": None if self.last_reset is None else now - self.last_reset,
                "audio_dbfs": self.audio_dbfs,
                "audio_last_seen_age": audio_age,
                "audio_error": self.audio_error,
                "min_in_level": self.min_in_level,
                "min_audio_dbfs": self.min_audio_dbfs,
                "audio_dead_since_age": None if self.audio_dead_since is None else now - self.audio_dead_since,
            }


class SerialWorker(threading.Thread):
    def __init__(self, args, state):
        super().__init__(daemon=True)
        self.args = args
        self.state = state
        self.ser = None
        self.stop_event = threading.Event()

    def reset_arduino(self):
        # Intentionally disabled: toggling DTR resets many Arduino boards.
        # We do not want monitoring code to ever reboot the TX sketch.
        return

    def open_serial(self):
        # Open serial in a way that avoids DTR transitions (which reset many Arduino boards).
        # `serial.Serial(port=...)` may toggle DTR as part of open on some platforms/drivers.
        self.ser = serial.Serial()
        self.ser.port = self.args.port
        self.ser.baudrate = self.args.baud
        self.ser.timeout = 0.2
        try:
            self.ser.dtr = False
        except Exception:
            pass
        try:
            self.ser.rts = False
        except Exception:
            pass
        self.ser.open()
        try:
            self.ser.reset_input_buffer()
        except serial.SerialException:
            pass

    def run(self):
        while not self.stop_event.is_set():
            if self.ser is None or not self.ser.is_open:
                try:
                    self.open_serial()
                    with self.state.lock:
                        self.state.serial_error = None
                except serial.SerialException as exc:
                    with self.state.lock:
                        self.state.serial_error = str(exc)
                    time.sleep(1.0)
                    continue

            try:
                raw = self.ser.readline()
            except serial.SerialException:
                try:
                    self.ser.close()
                except serial.SerialException:
                    pass
                self.ser = None
                continue

            if not raw:
                time.sleep(0.01)
                continue

            line = raw.decode("ascii", errors="ignore").strip()
            parsed = parse_line(line)
            with self.state.lock:
                self.state.last_line = line
                self.state.frames += 1
                self.state.last_seen = time.monotonic()
                if parsed:
                    self.state.last_values = parsed
                if line.startswith("TX_FREQ="):
                    self.state.last_boot = line

    def stop(self):
        self.stop_event.set()
        if self.ser:
            try:
                self.ser.close()
            except serial.SerialException:
                pass


class AudioWorker(threading.Thread):
    def __init__(self, args, state):
        super().__init__(daemon=True)
        self.args = args
        self.state = state
        self.queue = queue.Queue(maxsize=4)
        self.stop_event = threading.Event()
        self.stream = None

    def audio_callback(self, indata, frames, time_info, status):
        if status:
            with self.state.lock:
                self.state.audio_error = str(status)
        if self.stop_event.is_set():
            return
        try:
            self.queue.put_nowait(indata.copy())
        except queue.Full:
            pass

    def run(self):
        if sd is None:
            with self.state.lock:
                self.state.audio_error = "sounddevice not installed"
            return

        try:
            self.stream = sd.InputStream(
                device=self.args.audio_device,
                channels=self.args.audio_channels,
                samplerate=self.args.audio_samplerate,
                blocksize=self.args.audio_blocksize,
                callback=self.audio_callback,
            )
            self.stream.start()
        except Exception as exc:
            with self.state.lock:
                self.state.audio_error = str(exc)
            return

        while not self.stop_event.is_set():
            try:
                block = self.queue.get(timeout=0.5)
            except queue.Empty:
                continue

            samples = np.asarray(block, dtype=np.float32)
            dbfs = compute_dbfs(samples)
            with self.state.lock:
                self.state.audio_dbfs = dbfs
                self.state.audio_last_seen = time.monotonic()
                if dbfs is not None and dbfs < self.state.min_audio_dbfs:
                    if self.state.audio_dead_since is None:
                        self.state.audio_dead_since = time.monotonic()
                else:
                    self.state.audio_dead_since = None

    def stop(self):
        self.stop_event.set()
        if self.stream:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass


class StatusHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(render_html().encode("utf-8"))
            return
        if self.path.startswith("/status"):
            payload = json.dumps(self.server.state.snapshot())
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload.encode("utf-8"))
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, fmt, *args):
        return


def render_html():
    return """<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Aircheck TX Monitor</title>
  <style>
    body { font-family: sans-serif; background: #111; color: #eee; margin: 0; padding: 20px; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px; }
    .card { background: #1c1c1c; padding: 12px; border-radius: 8px; }
    .label { color: #999; font-size: 12px; text-transform: uppercase; }
    .value { font-size: 20px; margin-top: 4px; }
    .ok { color: #3bd671; }
    .bad { color: #ff6a6a; }
  </style>
</head>
<body>
  <h1>Aircheck TX Monitor</h1>
  <div class="grid">
    <div class="card"><div class="label">Status</div><div id="status" class="value">...</div></div>
    <div class="card"><div class="label">Freq</div><div id="freq" class="value">...</div></div>
    <div class="card"><div class="label">Noise</div><div id="noise" class="value">...</div></div>
    <div class="card"><div class="label">In Level</div><div id="inlevel" class="value">...</div></div>
    <div class="card"><div class="label">Overmod</div><div id="overmod" class="value">...</div></div>
    <div class="card"><div class="label">Audio dBFS</div><div id="audio" class="value">...</div></div>
    <div class="card"><div class="label">Last Update</div><div id="last" class="value">...</div></div>
    <div class="card"><div class="label">Resets</div><div id="resets" class="value">...</div></div>
  </div>
  <script>
    async function refresh() {
      const res = await fetch('/status');
      const data = await res.json();
      const stale = data.last_seen_age === null || data.last_seen_age > data.stale_seconds;
      const audioStale = data.audio_last_seen_age === null || data.audio_last_seen_age > data.audio_stale_seconds;
      const audioDead = data.audio_dead_since_age !== null && data.audio_dead_since_age > data.audio_dead_seconds;
      const audioIssue = data.audio_enabled ? (audioStale || audioDead) : false;
      const status = (!stale && !audioIssue) ? 'BROADCASTING' : 'ISSUE';
      document.getElementById('status').textContent = status;
      document.getElementById('status').className = 'value ' + (status === 'BROADCASTING' ? 'ok' : 'bad');
      const v = data.last_values || {};
      document.getElementById('freq').textContent = v.freq_mhz ? v.freq_mhz.toFixed(2) + ' MHz' : '--';
      document.getElementById('noise').textContent = v.noise ?? '--';
      document.getElementById('inlevel').textContent = v.in_level ?? '--';
      document.getElementById('overmod').textContent = v.over ? 'YES' : 'no';
      document.getElementById('audio').textContent = data.audio_enabled
        ? (data.audio_dbfs !== null ? data.audio_dbfs.toFixed(1) + ' dBFS' : '--')
        : 'disabled';
      document.getElementById('last').textContent = data.last_seen_age === null ? 'never' : data.last_seen_age.toFixed(2) + 's ago';
      document.getElementById('resets').textContent = data.reset_count;
    }
    setInterval(refresh, 500);
    refresh();
  </script>
</body>
</html>"""


def start_http_server(state, host, port):
    server = HTTPServer((host, port), StatusHandler)
    server.state = state
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def render_tui(snapshot):
    clear_screen()
    now = time.monotonic()
    stale = snapshot["last_seen_age"] is None or snapshot["last_seen_age"] > snapshot["stale_seconds"]
    audio_stale = (
        snapshot["audio_last_seen_age"] is None
        or snapshot["audio_last_seen_age"] > snapshot["audio_stale_seconds"]
    )
    audio_dead = (
        snapshot["audio_dead_since_age"] is not None
        and snapshot["audio_dead_since_age"] > snapshot["audio_dead_seconds"]
    )
    audio_issue = (audio_stale or audio_dead) if snapshot["audio_enabled"] else False

    status = "BROADCASTING" if not stale and not audio_issue else "ISSUE"
    print("Aircheck TX Monitor")
    print("=" * 60)
    print(f"Status: {status}")
    print(f"Serial: {snapshot['port']} @ {snapshot['baud']}")
    print(f"Last update: {format_age(snapshot['last_seen_age'])}")
    if snapshot["serial_error"]:
        print(f"Serial error: {snapshot['serial_error']}")
    if snapshot["last_values"]:
        values = snapshot["last_values"]
        print(
            "Freq: {freq:0.2f} MHz  Noise: {noise}  InLevel: {level}  Overmod: {over}".format(
                freq=values["freq_mhz"],
                noise=values["noise"],
                level=values["in_level"],
                over="YES" if values["over"] else "no",
            )
        )
        audio_ok = values["in_level"] >= snapshot["min_in_level"]
        print(f"Audio level (Si4713): {bool_label(audio_ok)}")
    else:
        print("Freq: --  Noise: --  InLevel: --  Overmod: --")
        print("Audio level (Si4713): --")

    if not snapshot["audio_enabled"]:
        print("Audio dBFS (Pi input): disabled")
    elif snapshot["audio_dbfs"] is None:
        print("Audio dBFS (Pi input): --")
    else:
        audio_ok = snapshot["audio_dbfs"] >= snapshot["min_audio_dbfs"]
        print(f"Audio dBFS (Pi input): {snapshot['audio_dbfs']:.1f} dBFS  {bool_label(audio_ok)}")

    if snapshot["audio_error"]:
        print(f"Audio error: {snapshot['audio_error']}")

    print("-" * 60)
    print(f"Frames: {snapshot['frames']}  Resets: {snapshot['reset_count']}")
    print(f"Last reset: {format_age(snapshot['last_reset_age'])}")
    if snapshot["last_boot"]:
        print(f"Boot line: {snapshot['last_boot']}")
    if snapshot["last_line"]:
        print(f"Raw: {snapshot['last_line']}")
    print("=" * 60)
    print("Ctrl-C to quit")


def main():
    parser = argparse.ArgumentParser(description="Custom monitor for Si4713 Arduino TX")
    parser.add_argument("--port", default="/dev/ttyACM0", help="Serial device path")
    parser.add_argument("--baud", type=int, default=115200, help="Serial baud rate")
    parser.add_argument("--stale-seconds", type=float, default=2.0, help="No-data threshold")
    parser.add_argument("--reset-cooldown", type=float, default=5.0, help="Minimum seconds between resets")
    parser.add_argument("--min-inlevel", type=int, default=1, help="Minimum Si4713 in_level")
    parser.add_argument("--reset", dest="reset_on_start", action="store_true", help="(disabled) historically toggled DTR on connect")
    parser.add_argument("--no-reset", dest="reset_on_start", action="store_false", help="Do not toggle DTR")
    parser.set_defaults(reset_on_start=False)

    parser.add_argument("--audio-device", type=int, default=None, help="sounddevice input device index")
    parser.add_argument("--audio-samplerate", type=int, default=48000, help="Audio sample rate")
    parser.add_argument("--audio-blocksize", type=int, default=2048, help="Audio block size")
    parser.add_argument("--audio-channels", type=int, default=1, help="Audio channels")
    parser.add_argument("--min-audio-dbfs", type=float, default=-45.0, help="Minimum audio dBFS")
    parser.add_argument("--audio-stale-seconds", type=float, default=2.0, help="Audio no-data threshold")
    parser.add_argument("--audio-dead-seconds", type=float, default=5.0, help="Audio low-level threshold")
    parser.add_argument("--no-audio", action="store_true", help="Disable Pi audio monitoring")

    parser.add_argument("--http-host", default="0.0.0.0", help="HTTP host")
    parser.add_argument("--http-port", type=int, default=8080, help="HTTP port")
    parser.add_argument("--no-http", action="store_true", help="Disable HTTP dashboard")
    args = parser.parse_args()

    state = SharedState(args)

    serial_worker = SerialWorker(args, state)
    serial_worker.start()

    audio_worker = None
    if not args.no_audio:
        audio_worker = AudioWorker(args, state)
        audio_worker.start()

    server = None
    if not args.no_http:
        server = start_http_server(state, args.http_host, args.http_port)

    last_render = 0.0

    try:
        while True:
            snapshot = state.snapshot()
            now = time.monotonic()
            if now - last_render > 0.25:
                render_tui(snapshot)
                last_render = now

            stale = snapshot["last_seen_age"] is None or snapshot["last_seen_age"] > snapshot["stale_seconds"]
            audio_stale = (
                snapshot["audio_last_seen_age"] is None
                or snapshot["audio_last_seen_age"] > snapshot["audio_stale_seconds"]
            )
            audio_dead = (
                snapshot["audio_dead_since_age"] is not None
                and snapshot["audio_dead_since_age"] > snapshot["audio_dead_seconds"]
            )
            audio_issue = (audio_stale or audio_dead) if snapshot["audio_enabled"] else False

            # Intentionally do not reset/restart the Arduino on stale telemetry.
            # Closing/reopening the port (or toggling DTR) can reboot the UNO and restart the sketch.
            # We only report the ISSUE state.

            if audio_issue:
                time.sleep(0.25)
            else:
                time.sleep(0.05)

    except KeyboardInterrupt:
        pass
    finally:
        serial_worker.stop()
        if audio_worker:
            audio_worker.stop()
        if server:
            server.shutdown()


if __name__ == "__main__":
    main()
