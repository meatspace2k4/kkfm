#!/usr/bin/env python3
"""Minimal anonymous Sonos Music API (SMAPI) service exposing the aircheck stream.

Browsing returns two "stream" items: KKFM (the :9000 aircheck player) and
Hourly BGM (hourly_bgm's existing /radio.mp3 on :8791). Both are served to players
from this process's LAN audio port with ICY StreamTitle set from the watchdogs'
now-playing state; players expose it to the app as streamInfo, which updates live. Sonos radio mode will not play WAV, so KKFM is re-encoded to MP3
by ffmpeg on demand, only while a player is connected; Hourly is passed through.
getMediaURI hands back LAN URLs, so audio never leaves the network.
"""

import json
import os
import re
import secrets
import socket
import subprocess
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from xml.sax.saxutils import escape

BIND = os.environ.get("SMAPI_BIND", "127.0.0.1")
PORT = int(os.environ.get("SMAPI_PORT", "8776"))
RADIO_PORT = int(os.environ.get("SMAPI_RADIO_PORT", "8791"))
KKFM_PORT = int(os.environ.get("ACNH_STREAM_PORT", "9000"))
AUDIO_BIND = os.environ.get("SMAPI_AUDIO_BIND", "0.0.0.0")
AUDIO_PORT = int(os.environ.get("SMAPI_AUDIO_PORT", "8777"))
MP3_BITRATE = os.environ.get("SMAPI_MP3_BITRATE", "192k")
ART_URL = os.environ.get("SMAPI_ART_URL", "")
PUBLIC_BASE = os.environ.get("SMAPI_PUBLIC_BASE", "https://sonos.funstuff.app")
# The Sonos app never opens regUrl for this flow, so codes are approved on issue by default.
AUTO_APPROVE = os.environ.get("SMAPI_AUTO_APPROVE", "1") == "1"

NS = "http://www.sonos.com/Services/1.1"
# id -> (title, url builder taking the LAN IP)
STREAMS = {
    "kkfm": ("KKFM", lambda ip: f"http://{ip}:{AUDIO_PORT}/kkfm.mp3"),
    "hourly": ("Hourly BGM", lambda ip: f"http://{ip}:{AUDIO_PORT}/hourly.mp3"),
}
# Watchdog snapshots (same data the TUI dashboard shows).
STATUS_FILES = {
    "kkfm": os.environ.get("SMAPI_KKFM_STATUS", "/run/kkfm-watchdog/log_status.json"),
    "hourly": os.environ.get("SMAPI_HOURLY_STATUS", "/run/user/1000/kkfm-hourly/log_status.json"),
}
ICY_METAINT = 16000
_last_title = {}
ALIASES = {"aircheck": "hourly"}
# Changes on every restart so Sonos re-fetches the catalog after a deploy.
CATALOG_VERSION = str(int(time.time()))

# AppLink: linkCode -> {"at": issued, "approved": bool}. There are no user accounts;
# linking only exists so the Sonos app's add flow has an auth step it can complete.
LINK_TTL = 600
_links = {}
_links_lock = threading.Lock()

_lan_cache = {"ip": None, "at": 0.0}
_lan_lock = threading.Lock()


def _local_ipv4s():
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show", "scope", "global"],
                             capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return re.findall(r"inet (\d+\.\d+\.\d+\.\d+)/", out)


def _find_sonos_player(local_ip, timeout=1.5):
    """SSDP search for a Sonos player out of the interface owning local_ip."""
    msg = (b'M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: "ssdp:discover"\r\n'
           b'MX: 1\r\nST: urn:schemas-upnp-org:device:ZonePlayer:1\r\n\r\n')
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local_ip))
        s.bind((local_ip, 0))
        s.sendto(msg, ("239.255.255.250", 1900))
        _, addr = s.recvfrom(4096)
        return addr[0]
    except OSError:
        return None
    finally:
        s.close()


def _source_ip_toward(target):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target, 1400))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def lan_ip():
    """Pi address on the interface that reaches the Sonos players (cached 10 min)."""
    with _lan_lock:
        if _lan_cache["ip"] and time.time() - _lan_cache["at"] < 600:
            return _lan_cache["ip"]
        ip = next((a for a in _local_ipv4s() if _find_sonos_player(a)), None)
        ip = ip or _source_ip_toward("8.8.8.8")
        if ip:
            _lan_cache.update(ip=ip, at=time.time())
        return ip or _lan_cache["ip"]


def stream_url(sid):
    return STREAMS[sid][1](lan_ip())


def now_playing(sid):
    """Title for the watchdog's latest event: the song while it is on air, "Commercial break"
    during ads, the station name for jingles and the gap after a break. Events that don't
    change what is on air (DAC/unit) keep the previous title."""
    try:
        with open(STATUS_FILES[sid]) as f:
            st = json.load(f)
    except (OSError, ValueError):
        return _last_title.get(sid, "")
    ev = st.get("last_event") or {}
    etype = ev.get("etype", "")
    if etype == "now_playing" and ev.get("name"):
        _last_title[sid] = ev["name"]
    elif etype in ("commercial_picked", "commercial_start"):
        _last_title[sid] = "Commercial break"
    elif etype.startswith(("jingle_", "commercial_")):
        _last_title[sid] = STREAMS[sid][0]
    elif sid not in _last_title and (st.get("last_bwav") or {}).get("name"):
        _last_title[sid] = re.sub(r"\.bwav$", "", st["last_bwav"]["name"]).replace("_", " ")
    return _last_title.get(sid, "")


def media_metadata(sid):
    # No currentShow: the player stores it once per play and the app shows it in place of the
    # live ICY title (streamInfo), so any song name here goes stale at the next song.
    title = STREAMS[sid][0]
    art = f"<logo>{escape(ART_URL)}</logo>" if ART_URL else ""
    return (f"<id>{sid}</id><itemType>stream</itemType>"
            f"<title>{escape(title)}</title><mimeType>audio/mpeg</mimeType>"
            f"<streamMetadata>{art}</streamMetadata>")


def envelope(body):
    return ('<?xml version="1.0" encoding="utf-8"?>'
            '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
            f'<soap:Body>{body}</soap:Body></soap:Envelope>')


def response(method, inner):
    return envelope(f'<{method}Response xmlns="{NS}">{inner}</{method}Response>')


def fault(code, text, sonos_error=None):
    detail = (f'<detail><SonosError xmlns="{NS}">{sonos_error}</SonosError></detail>'
              if sonos_error is not None else "")
    return envelope(f"<soap:Fault><faultcode>soap:{code}</faultcode>"
                    f"<faultstring>{escape(text)}</faultstring>{detail}</soap:Fault>")


def new_link():
    code = secrets.token_hex(4).upper()
    with _links_lock:
        now = time.time()
        for k in [k for k, v in _links.items() if now - v["at"] > LINK_TTL]:
            del _links[k]
        _links[code] = {"at": now, "approved": AUTO_APPROVE}
    return code


def approve_link(code):
    with _links_lock:
        link = _links.get(code)
        if not link or time.time() - link["at"] > LINK_TTL:
            return False
        link["approved"] = True
        return True


def link_state(code):
    with _links_lock:
        link = _links.get(code)
        if not link or time.time() - link["at"] > LINK_TTL:
            return "expired"
        return "approved" if link["approved"] else "pending"


USER_INFO = "<userInfo><nickname>KKFM</nickname><userIdHashCode>kkfm-aircheck</userIdHashCode></userInfo>"


def handle(method, args):
    """Returns (status, xml)."""
    item_id = re.sub(r"\.(wav|mp3)$", "", args.get("id", ""))
    item_id = ALIASES.get(item_id, item_id)
    # Favorites created with item id "F00092020<id>" come back as "0<id>".
    if item_id not in STREAMS and item_id[:1] == "0" and item_id[1:] in STREAMS:
        item_id = item_id[1:]
    if method == "getMetadata":
        index = max(int(args.get("index", "0") or 0), 0)
        count = max(int(args.get("count", "100") or 100), 0)
        ids = list(STREAMS) if item_id == "root" else []
        page = ids[index:index + count]
        items = "".join(f"<mediaMetadata>{media_metadata(i)}</mediaMetadata>" for i in page)
        return 200, response(method, f"<getMetadataResult><index>{index}</index>"
                                     f"<count>{len(page)}</count><total>{len(ids)}</total>"
                                     f"{items}</getMetadataResult>")
    if method in ("getMediaMetadata", "getExtendedMetadata"):
        if item_id not in STREAMS:
            return 500, fault("Client", "ItemNotFound")
        if method == "getMediaMetadata":
            return 200, response(method, f"<getMediaMetadataResult>{media_metadata(item_id)}</getMediaMetadataResult>")
        return 200, response(method, f"<getExtendedMetadataResult><mediaMetadata>{media_metadata(item_id)}"
                                     f"</mediaMetadata></getExtendedMetadataResult>")
    if method == "getMediaURI":
        if item_id not in STREAMS:
            return 500, fault("Client", "ItemNotFound")
        return 200, response(method, f"<getMediaURIResult>{escape(stream_url(item_id))}</getMediaURIResult>")
    if method == "getLastUpdate":
        return 200, response(method, f"<getLastUpdateResult><catalog>{CATALOG_VERSION}</catalog>"
                                     "<favorites>1</favorites><pollInterval>3600</pollInterval>"
                                     "</getLastUpdateResult>")
    if method == "getAppLink":
        code = new_link()
        url = f"{PUBLIC_BASE}/link?code={code}"
        return 200, response(method, "<getAppLinkResult><authorizeAccount>"
                                     f"<deviceLink><regUrl>{escape(url)}</regUrl><linkCode>{code}</linkCode>"
                                     "<showLinkCode>false</showLinkCode></deviceLink>"
                                     "</authorizeAccount></getAppLinkResult>")
    if method == "getDeviceLinkCode":
        code = new_link()
        url = f"{PUBLIC_BASE}/link?code={code}"
        return 200, response(method, f"<getDeviceLinkCodeResult><regUrl>{escape(url)}</regUrl>"
                                     f"<linkCode>{code}</linkCode><showLinkCode>true</showLinkCode>"
                                     "</getDeviceLinkCodeResult>")
    if method == "getDeviceAuthToken":
        state = link_state(args.get("linkCode", ""))
        if state == "pending":
            return 500, fault("Client.NOT_LINKED_RETRY", "NOT_LINKED_RETRY", 5)
        if state == "expired":
            return 500, fault("Client.NOT_LINKED_FAILURE", "NOT_LINKED_FAILURE", 6)
        return 200, response(method, f"<getDeviceAuthTokenResult><authToken>{secrets.token_hex(16)}</authToken>"
                                     f"<privateKey>{secrets.token_hex(16)}</privateKey>{USER_INFO}"
                                     "</getDeviceAuthTokenResult>")
    if method == "refreshAuthToken":
        return 200, response(method, f"<refreshAuthTokenResult><authToken>{secrets.token_hex(16)}</authToken>"
                                     f"<privateKey>{secrets.token_hex(16)}</privateKey>"
                                     "</refreshAuthTokenResult>")
    if method == "getUserInfo":
        return 200, response(method, f"<getUserInfoResult><userIdHashCode>kkfm-aircheck</userIdHashCode>"
                                     "<nickname>KKFM</nickname></getUserInfoResult>")
    if method in ("reportPlaySeconds", "reportPlayStatus", "setPlayedSeconds", "reportAccountAction"):
        return 200, response(method, "")
    return 500, fault("Client", f"Unsupported method {method}")


def parse_request(raw):
    root = ET.fromstring(raw)
    body = next(el for el in root if el.tag.endswith("}Body"))
    call = next(iter(body))
    method = call.tag.split("}")[-1]
    args = {child.tag.split("}")[-1]: (child.text or "").strip() for child in call}
    return method, args


class Handler(BaseHTTPRequestHandler):
    server_version = "kkfm-smapi/1"

    def _send(self, status, payload, ctype="text/xml; charset=utf-8"):
        data = payload.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _link_page(self, code, message):
        button = (f'<form method="post" action="/link"><input type="hidden" name="code" value="{escape(code)}">'
                  '<button type="submit">Link Aircheck FM</button></form>') if message is None else f"<p>{message}</p>"
        self._send(200, "<!doctype html><meta name=viewport content='width=device-width'>"
                        "<title>Aircheck FM</title><body style='font:18px system-ui;margin:2em'>"
                        f"<h1>Aircheck FM</h1>{button}</body>", "text/html; charset=utf-8")

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/", "/healthz"):
            self._send(200, "ok " + " ".join(stream_url(i) for i in STREAMS) + "\n", "text/plain; charset=utf-8")
        elif url.path == "/link":
            code = parse_qs(url.query).get("code", [""])[0]
            state = link_state(code)
            self._link_page(code, None if state == "pending" else
                            "Already linked. Return to the Sonos app." if state == "approved" else
                            "This link expired. Start again in the Sonos app.")
        else:
            self._send(404, "not found\n", "text/plain; charset=utf-8")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(min(length, 1 << 20))
        if urlparse(self.path).path == "/link":
            code = parse_qs(raw.decode(errors="replace")).get("code", [""])[0]
            ok = approve_link(code)
            print(f"[smapi] link {code} -> {'approved' if ok else 'expired'}", flush=True)
            self._link_page(code, "Linked. Return to the Sonos app." if ok else
                            "This link expired. Start again in the Sonos app.")
            return
        action = re.sub(r'.*#|"', "", self.headers.get("SOAPAction", ""))
        try:
            method, args = parse_request(raw)
        except (ET.ParseError, StopIteration):
            self._send(500, fault("Client", "Malformed SOAP request"))
            return
        status, payload = handle(method or action, args)
        print(f"[smapi] {method} {args} -> {status} UA={self.headers.get('User-Agent', '')}", flush=True)
        self._send(status, payload)

    def log_message(self, fmt, *a):
        print(f"[smapi] {self.address_string()} {fmt % a}", flush=True)


class AudioHandler(BaseHTTPRequestHandler):
    """GET /kkfm.mp3 (ffmpeg per listener) and /hourly.mp3 (pass-through of :8791),
    with ICY StreamTitle from the watchdog snapshot when the player asks for it."""
    protocol_version = "HTTP/1.0"

    def do_GET(self):
        path = self.path.split("?")[0]
        sid = {"/kkfm.mp3": "kkfm", "/hourly.mp3": "hourly"}.get(path)
        if not sid:
            self.send_error(404)
            return
        icy = self.headers.get("Icy-MetaData", "") == "1"
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("icy-name", STREAMS[sid][0])
        if icy:
            self.send_header("icy-metaint", str(ICY_METAINT))
        self.send_header("Cache-Control", "no-cache, no-store")
        self.end_headers()
        proc = upstream = None
        if sid == "kkfm":
            # -seekable 0: the :9000 server answers Range requests with only the WAV header.
            proc = subprocess.Popen(
                ["nice", "-n", "10", "ffmpeg", "-nostdin", "-loglevel", "error", "-seekable", "0",
                 "-i", f"http://127.0.0.1:{KKFM_PORT}/stream.wav",
                 "-c:a", "libmp3lame", "-b:a", MP3_BITRATE, "-f", "mp3", "-"],
                stdout=subprocess.PIPE)
            src = proc.stdout
        else:
            upstream = urllib.request.urlopen(f"http://127.0.0.1:{RADIO_PORT}/radio.mp3", timeout=15)
            src = upstream
        print(f"[audio] {self.address_string()} {sid} connected icy={icy}", flush=True)
        sent_title = None
        until_meta = ICY_METAINT
        try:
            while True:
                chunk = src.read(4096)
                if not chunk:
                    break
                if not icy:
                    self.wfile.write(chunk)
                    continue
                while chunk:
                    part, chunk = chunk[:until_meta], chunk[until_meta:]
                    self.wfile.write(part)
                    until_meta -= len(part)
                    if until_meta == 0:
                        title = now_playing(sid) or STREAMS[sid][0]
                        if title != sent_title:
                            meta = f"StreamTitle='{title.replace(chr(39), '')}';".encode()
                            meta += b"\0" * (-len(meta) % 16)
                            self.wfile.write(bytes([len(meta) // 16]) + meta)
                            sent_title = title
                        else:
                            self.wfile.write(b"\0")
                        until_meta = ICY_METAINT
        except OSError:
            pass
        finally:
            if proc:
                proc.kill()
                proc.wait()
            if upstream:
                upstream.close()
            print(f"[audio] {self.address_string()} {sid} disconnected", flush=True)

    def log_message(self, fmt, *a):
        pass


if __name__ == "__main__":
    audio = ThreadingHTTPServer((AUDIO_BIND, AUDIO_PORT), AudioHandler)
    audio.daemon_threads = True
    threading.Thread(target=audio.serve_forever, daemon=True).start()
    print(f"[smapi] listening on {BIND}:{PORT}, audio on {AUDIO_BIND}:{AUDIO_PORT}", flush=True)
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()
