#!/usr/bin/env python3
"""Localhost server for the aircheck status dashboard: static page plus
relayed data, one upstream.

  http://localhost:8901/              the dashboard (dashboard/index.html)
  http://localhost:8901/logs          relay of the station's /logs stream.
                                      ONE upstream connection no matter how
                                      many tabs hang off this process: the
                                      fan-out lives here, so the station's
                                      journalctl is never asked twice.
  http://localhost:8901/status.json   passthrough of the station's snapshot
                                      (cached 1s so N tabs cost one fetch)

The upstream is a running station's logserver, over plain HTTP:

  python3 devserver.py --pi http://<host>:8775
"""
import argparse
import http.server
import os
import sys
import threading
import time
import urllib.request

from pathlib import Path

HERE = Path(__file__).resolve().parent
HTTP_PORT = 8901
DEF_PI = "http://localhost:8775"

STATIC = {
    "/": ("dashboard/index.html", "text/html; charset=utf-8"),
    "/index.html": ("dashboard/index.html", "text/html; charset=utf-8"),
    "/scene.cfg": ("dashboard/scene.cfg", "text/plain; charset=utf-8"),
    "/dashboard.js": ("dashboard/dashboard.js",
                      "application/javascript; charset=utf-8"),
    "/terminal.js": ("dashboard/terminal.js",
                   "application/javascript; charset=utf-8"),
}

PI = DEF_PI

# Some proxies reject the default Python-urllib user agent, so upstream
# requests identify themselves. AIRCHECK_APP_TOKEN rides along as an
# x-app-token header when set, for endpoints that gate on one.
UP_HEADERS = {"User-Agent": "aircheck-status-devserver/1.0"}
if os.environ.get("AIRCHECK_APP_TOKEN"):
    UP_HEADERS["x-app-token"] = os.environ["AIRCHECK_APP_TOKEN"]

# fan-out state: every consumer owns a queue of lines, one producer appends
# to all.
_subs = {}
_subs_lock = threading.Lock()
_next_id = [0]
_backlog = []          # last 400 lines, so a late tab gets history at once
BACKLOG_MAX = 400


def _subscribe():
    with _subs_lock:
        _next_id[0] += 1
        sid = _next_id[0]
        q = {"lines": list(_backlog), "cv": threading.Condition()}
        _subs[sid] = q
        return sid, q


def _unsubscribe(sid):
    with _subs_lock:
        _subs.pop(sid, None)


def _publish(line):
    _backlog.append(line)
    if len(_backlog) > BACKLOG_MAX:
        del _backlog[:-BACKLOG_MAX]
    with _subs_lock:
        subs = list(_subs.values())
    for s in subs:
        with s["cv"]:
            s["lines"].append(line)
            if len(s["lines"]) > 4096:
                del s["lines"][:-4096]
            s["cv"].notify()


def _upstream_logs():
    """ONE connection to the station's /logs, relaying every line. Reconnects."""
    while True:
        try:
            req = urllib.request.Request(PI + "/logs", headers=UP_HEADERS)
            with urllib.request.urlopen(req, timeout=15) as resp:
                sys.stderr.write("logs upstream: connected to %s\n" % PI)
                buf = b""
                while True:
                    chunk = resp.read1(65536)
                    if not chunk:
                        raise OSError("stream ended")
                    buf += chunk
                    while b"\n" in buf:
                        raw, buf = buf.split(b"\n", 1)
                        _publish(raw.decode("utf-8", "replace"))
        except OSError as e:
            sys.stderr.write("logs upstream: %s; reconnecting\n" % e)
            time.sleep(3)


_status_cache = {"ts": 0.0, "body": None}
_status_lock = threading.Lock()


def _status_body():
    with _status_lock:
        if _status_cache["body"] is not None \
                and time.time() - _status_cache["ts"] < 1.0:
            return _status_cache["body"]
    try:
        req = urllib.request.Request(PI + "/status.json", headers=UP_HEADERS)
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read()
    except OSError:
        return None
    with _status_lock:
        _status_cache.update(ts=time.time(), body=body)
    return body


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _hdr(self, code, ctype, extra=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/logs":
            return self._logs()
        if path == "/stream":
            return self._stream()
        if path == "/status.json":
            body = _status_body()
            if body is None:
                self._hdr(502, "text/plain")
                self.wfile.write(b"upstream unreachable")
                return
            self._hdr(200, "application/json",
                      [("Content-Length", str(len(body)))])
            self.wfile.write(body)
            return
        entry = STATIC.get(path)
        if entry is None:
            self._hdr(404, "text/plain")
            self.wfile.write(b"Not Found")
            return
        rel, ctype = entry
        try:
            body = (HERE / rel).read_bytes()
        except OSError:
            self._hdr(404, "text/plain")
            self.wfile.write(b"missing: %s" % rel.encode())
            return
        self._hdr(200, ctype, [("Content-Length", str(len(body)))])
        self.wfile.write(body)

    def _stream(self):
        """Audio passthrough so the TUI's player chip works on localhost.
        Per-client upstream: PCM is cheap enough and fan-out buys nothing
        for audio."""
        try:
            req = urllib.request.Request(PI + "/stream", headers=UP_HEADERS)
            up = urllib.request.urlopen(req, timeout=15)
        except OSError:
            self._hdr(502, "text/plain")
            self.wfile.write(b"upstream unreachable")
            return
        self._hdr(200, "audio/wav", [("Transfer-Encoding", "chunked")])
        try:
            while True:
                chunk = up.read1(65536)
                if not chunk:
                    break
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.flush()
        except OSError:
            pass
        finally:
            try:
                up.close()
            except OSError:
                pass

    def _logs(self):
        sid, q = _subscribe()
        self._hdr(200, "text/plain; charset=utf-8",
                  [("Transfer-Encoding", "chunked")])
        try:
            while True:
                with q["cv"]:
                    if not q["lines"]:
                        q["cv"].wait(10.0)
                    lines, q["lines"][:] = list(q["lines"]), []
                payload = "".join(l + "\n" for l in lines).encode()
                if payload:
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(payload), payload))
                else:
                    self.wfile.write(b"1\r\n\n\r\n")   # keepalive newline
                self.wfile.flush()
        except OSError:
            pass
        finally:
            _unsubscribe(sid)


def main():
    global PI
    ap = argparse.ArgumentParser(
        description="aircheck status dev server (static + relay)")
    ap.add_argument("--pi", default=DEF_PI,
                    help="the station's data endpoint base URL (default %(default)s)")
    ap.add_argument("--http", type=int, default=HTTP_PORT)
    a = ap.parse_args()
    PI = a.pi.rstrip("/")
    threading.Thread(target=_upstream_logs, daemon=True).start()
    sys.stderr.write("http :%d   upstream %s\n" % (a.http, PI))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", a.http), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
