#!/usr/bin/env python3
"""aircheck-status: expose the radio's logs and status over HTTP.

Runs next to the player. It only reads what already exists, and offers no
control surface: the radio binary, the watchdogs, and the SSH dashboard are
untouched.

  GET /logs         chunked text/plain: `journalctl -u aircheck.service`
                    (last 200 lines, then follow). The client parses these
                    lines itself, exactly as the watchdog's log_parser does.
  GET /status.json  one JSON snapshot: systemd unit states + tx_status.json
                    + anomalies.jsonl tail from /run/kkfm-watchdog. Same
                    shape as the SSH dashboard's remote snapshot.
  GET /dash/        the dashboard page itself, so it can ride the same host
                    as the audio stream. Any /dash-prefixed path is served
                    as if the prefix were stripped, so the page's relative
                    fetches (scene.cfg, dashboard.js, logs, status.json)
                    resolve under /dash/ and the host's / stays the player.

devserver.py relays these two endpoints and serves the same page locally.
Native web traffic only: plain chunked HTTP streaming, no websockets.

  python3 logserver.py --port 8775
"""
import argparse
import json
import os
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

UNIT = "aircheck.service"

UNITS = [
    "aircheck.service",
    "aircheck-watchdog.service",
    "aircheck-tx-watchdog.service",
]
HERE = Path(__file__).resolve().parent
STATIC = {
    "/": ("dashboard/index.html", "text/html; charset=utf-8"),
    "/index.html": ("dashboard/index.html", "text/html; charset=utf-8"),
    "/scene.cfg": ("dashboard/scene.cfg", "text/plain; charset=utf-8"),
    "/dashboard.js": ("dashboard/dashboard.js",
                      "application/javascript; charset=utf-8"),
    "/terminal.js": ("dashboard/terminal.js",
                   "application/javascript; charset=utf-8"),
}


def _run(cmd, timeout_s=2.0):
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True,
                           timeout=timeout_s)
        return (p.stdout or "").strip()
    except Exception:
        return ""


def _systemctl_state(unit):
    out = _run(["systemctl", "show", unit, "-p", "ActiveState",
                "-p", "SubState", "-p", "ActiveEnterTimestamp", "--no-pager"])
    st = {"active": "?", "sub": "?", "since": ""}
    for line in out.splitlines():
        if line.startswith("ActiveState="):
            st["active"] = line.split("=", 1)[1].strip()
        elif line.startswith("SubState="):
            st["sub"] = line.split("=", 1)[1].strip()
        elif line.startswith("ActiveEnterTimestamp="):
            st["since"] = line.split("=", 1)[1].strip()
    return st


def _status_dir():
    uid = os.getuid()
    for d in (Path("/run/kkfm-watchdog"),
              Path("/run/user/%d/kkfm-watchdog" % uid),
              Path("/dev/shm/kkfm-watchdog-%d" % uid)):
        if d.exists():
            return d
    return Path("/run/kkfm-watchdog")


def _read_json(p):
    try:
        txt = p.read_text(encoding="utf-8", errors="ignore").strip()
        return json.loads(txt) if txt else None
    except Exception:
        return None


def _tail_jsonl(p, n=50):
    try:
        lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return []
    out = []
    for raw in lines[-n:]:
        try:
            out.append(json.loads(raw))
        except Exception:
            pass
    return out


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _hdr(self, code, ctype, extra=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/dash":
            # relative fetches must resolve under /dash/, not /
            self.send_response(301)
            self.send_header("Location", "/dash/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path.startswith("/dash/"):
            path = path[len("/dash"):]
        if path == "/logs":
            return self._logs()
        if path == "/status.json":
            return self._status()
        entry = STATIC.get(path)
        if entry is not None:
            rel, ctype = entry
            try:
                body = (HERE / rel).read_bytes()
            except OSError:
                self._hdr(404, "text/plain")
                self.wfile.write(b"missing: %s" % rel.encode())
                return
            self._hdr(200, ctype, [("Content-Length", str(len(body)))])
            self.wfile.write(body)
            return
        self._hdr(404, "text/plain")
        self.wfile.write(b"Not Found")

    def _status(self):
        d = _status_dir()
        snap = {
            "ts": time.time(),
            "services": {u: _systemctl_state(u) for u in UNITS},
            "tx": _read_json(d / "tx_status.json"),
            "log": _read_json(d / "log_status.json"),
            "anomalies": _tail_jsonl(d / "anomalies.jsonl", 50),
        }
        body = json.dumps(snap, ensure_ascii=False).encode()
        self._hdr(200, "application/json", [("Content-Length", str(len(body)))])
        self.wfile.write(body)

    def _logs(self):
        """Last 200 journal lines, then follow. Chunked until disconnect."""
        proc = subprocess.Popen(
            ["journalctl", "-u", UNIT, "-n", "200", "-f",
             "-o", "short-iso", "--no-pager"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self._hdr(200, "text/plain; charset=utf-8",
                  [("Transfer-Encoding", "chunked")])
        try:
            for raw in proc.stdout:
                chunk = b"%x\r\n%s\r\n" % (len(raw), raw)
                self.wfile.write(chunk)
                self.wfile.flush()
        except OSError:
            pass  # client went away
        finally:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        try:
            self.wfile.write(b"0\r\n\r\n")
        except OSError:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8775)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print("aircheck-status on %s:%d" % (a.host, a.port), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
