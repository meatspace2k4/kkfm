#!/usr/bin/env python3
"""Minimal anonymous Sonos Music API (SMAPI) service exposing the aircheck stream.

Browsing returns two "stream" items: KKFM (the :9000 aircheck player) and
Hourly BGM (hourly_bgm's existing /radio.mp3 on :8791). Both are served to players
from this process's LAN audio port with ICY StreamTitle set from the watchdogs'
now-playing state; players expose it to the app as streamInfo, which updates live. Sonos radio mode will not play WAV, so KKFM is re-encoded to MP3
by ffmpeg on demand, only while a player is connected; Hourly is passed through.
getMediaURI hands back LAN URLs, so audio never leaves the network.
"""

import collections
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
# The players bind loopback on IPv6 only ([::1]); "localhost" lets ffmpeg/urllib try both families.
UPSTREAM_HOST = os.environ.get("SMAPI_UPSTREAM_HOST", "localhost")
KKFM_PORT = int(os.environ.get("ACNH_STREAM_PORT", "9000"))
AUDIO_BIND = os.environ.get("SMAPI_AUDIO_BIND", "0.0.0.0")
AUDIO_PORT = int(os.environ.get("SMAPI_AUDIO_PORT", "8777"))
MP3_BITRATE = os.environ.get("SMAPI_MP3_BITRATE", "192k")
AAC_BITRATE = os.environ.get("SMAPI_AAC_BITRATE", "192k")
ART_URL = os.environ.get("SMAPI_ART_URL", "")
PUBLIC_BASE = os.environ.get("SMAPI_PUBLIC_BASE", "https://sonos.funstuff.app")
# The Sonos app never opens regUrl for this flow, so codes are approved on issue by default.
AUTO_APPROVE = os.environ.get("SMAPI_AUTO_APPROVE", "1") == "1"

NS = "http://www.sonos.com/Services/1.1"
# id -> (title, url builder taking the LAN IP)
STREAMS = {
    "kkfm": ("KKFM", lambda ip: f"http://{ip}:{AUDIO_PORT}/hls/kkfm.m3u8"),
    "hourly": ("Hourly BGM", lambda ip: f"http://{ip}:{AUDIO_PORT}/hls/hourly.m3u8"),
}
# Player journals, followed live for titles. The watchdog snapshots below lag the
# journal by up to ~5 s, so they are only a fallback until the journal yields a title.
JOURNAL_MATCH = {
    "kkfm": "_SYSTEMD_UNIT=acnh-aircheck.service",
    "hourly": "_SYSTEMD_UNIT=acnh-hourly.service",
}
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
NOW_PLAYING_RE = re.compile(r"Now Playing:\s*(.+)$")
BREAK_RE = re.compile(r"\[RADIO\]\s*(Commercial|Jingle) (start|end):")
BWAV_TRACK_RE = re.compile(r"BGM_Audio_\d+_([A-Za-z0-9]+)_[A-Za-z]+\.bwav")
_journal_title = {}
# Album art: per-song jackets for KKFM (mjk_<Name>.png), keyed case-insensitively by the track
# name in the BGM_Audio_<n>_<Name>_<variant>.bwav file the player logs just before "Now Playing";
# Hourly BGM uses the Nintendo Music hourly artwork (hourly_<h><am|pm>_<clear|rainy|snowy>.jpg)
# picked from the "4 PM (Sunny)" title.
HOURLY_TITLE_RE = re.compile(r"^(\d{1,2}) (AM|PM) \((\w+)\)$")
HOURLY_WEATHER = {"sunny": "clear", "clear": "clear", "rainy": "rainy", "snowy": "snowy"}
JACKETS_DIR = os.environ.get("SMAPI_JACKETS_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "jackets"))
JACKET_ALIASES = {"kekebily": "kekebilly", "rocknroll": "rockenroll", "town": "doubutunomachi",
                  "minnaatsumare": "maintheme"}
_jackets = {}
_art_files = set()
_journal_art = {}
_pending_track = {}
# Watchdog snapshots (same data the TUI dashboard shows).
STATUS_FILES = {
    "kkfm": os.environ.get("SMAPI_KKFM_STATUS", "/run/kkfm-watchdog/log_status.json"),
    "hourly": os.environ.get("SMAPI_HOURLY_STATUS", "/run/kkfm-hourly/log_status.json"),
}
ICY_METAINT = 8192
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


def load_jackets():
    try:
        names = os.listdir(JACKETS_DIR)
    except OSError:
        names = []
    for name in names:
        m = re.fullmatch(r"mjk_(.+)\.png", name)
        if m:
            _jackets[m.group(1).lower()] = name
        if re.fullmatch(r"(mjk_.+\.png|hourly_\d+(am|pm)_\w+\.jpg)", name):
            _art_files.add(name)


def jacket_for_track(track):
    key = track.lower()
    return _jackets.get(JACKET_ALIASES.get(key, key))


def hourly_art_for_title(title):
    m = HOURLY_TITLE_RE.match(title)
    if not m:
        return None
    hour, ampm, weather = m.groups()
    name = f"hourly_{int(hour)}{ampm.lower()}_{HOURLY_WEATHER.get(weather.lower(), 'clear')}.jpg"
    return name if name in _art_files else None


def art_url(sid):
    name = _journal_art.get(sid)
    return f"{PUBLIC_BASE}/art/{name}" if name else ""


def title_for_line(sid, line):
    """Title implied by one player log line, or None if the line doesn't change it."""
    line = ANSI_RE.sub("", line).strip()
    m = NOW_PLAYING_RE.search(line)
    if m:
        return m.group(1).strip()
    m = BREAK_RE.search(line)
    if m:
        kind, edge = m.groups()
        return "Commercial break" if (kind, edge) == ("Commercial", "start") else STREAMS[sid][0]
    return None


def follow_journal(sid):
    """Keep _journal_title[sid] (and the jacket in _journal_art[sid]) current from the
    player's journal; restarts journalctl if it exits."""
    while True:
        proc = subprocess.Popen(["journalctl", "-f", "-n", "200", "-o", "cat", JOURNAL_MATCH[sid]],
                                stdout=subprocess.PIPE, text=True, errors="replace")
        for line in proc.stdout:
            m = BWAV_TRACK_RE.search(ANSI_RE.sub("", line))
            if m:
                _pending_track[sid] = m.group(1)
            title = title_for_line(sid, line)
            if title:
                _journal_title[sid] = title
                is_song = bool(NOW_PLAYING_RE.search(ANSI_RE.sub("", line)))
                if not is_song:
                    _journal_art[sid] = None
                elif sid == "hourly":
                    _journal_art[sid] = hourly_art_for_title(title)
                else:
                    _journal_art[sid] = jacket_for_track(_pending_track.get(sid, ""))
        proc.wait()
        time.sleep(5)


def now_playing(sid):
    if _journal_title.get(sid):
        return _journal_title[sid]
    return snapshot_title(sid)


def snapshot_title(sid):
    """Fallback title from the watchdog's latest event: the song while it is on air,
    "Commercial break" during ads, the station name for jingles and the gap after a break.
    Events that don't change what is on air (DAC/unit) keep the previous title."""
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
            f"<title>{escape(title)}</title><mimeType>application/x-mpegURL</mimeType>"
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
        HLS[item_id].touch()  # warm the segmenter before the player asks for the playlist
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
        elif url.path.startswith("/art/"):
            name = url.path[len("/art/"):]
            if name not in _art_files:
                self._send(404, "not found\n", "text/plain; charset=utf-8")
                return
            with open(os.path.join(JACKETS_DIR, name), "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg" if name.endswith(".jpg") else "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(data)
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


def open_source(sid):
    """MP3 byte source for a stream: ffmpeg encode of the :9000 WAV for KKFM, the existing
    /radio.mp3 for Hourly. Returns (readable, close)."""
    if sid == "kkfm":
        # -seekable 0: the :9000 server answers Range requests with only the WAV header.
        proc = subprocess.Popen(
            ["nice", "-n", "10", "ffmpeg", "-nostdin", "-loglevel", "error", "-seekable", "0",
             "-i", f"http://{UPSTREAM_HOST}:{KKFM_PORT}/stream.wav",
             "-c:a", "libmp3lame", "-b:a", MP3_BITRATE, "-f", "mp3", "-"],
            stdout=subprocess.PIPE)

        def close():
            proc.kill()
            proc.wait()
        return proc.stdout, close
    upstream = urllib.request.urlopen(f"http://{UPSTREAM_HOST}:{RADIO_PORT}/radio.mp3", timeout=15)
    return upstream, upstream.close


def open_hls_source(sid):
    """AAC (ADTS) byte source for HLS; Sonos rejects MP3 packed-audio segments."""
    src = (f"http://{UPSTREAM_HOST}:{KKFM_PORT}/stream.wav" if sid == "kkfm"
           else f"http://{UPSTREAM_HOST}:{RADIO_PORT}/radio.mp3")
    proc = subprocess.Popen(
        ["nice", "-n", "10", "ffmpeg", "-nostdin", "-loglevel", "error", "-seekable", "0", "-i", src,
         "-c:a", "aac", "-b:a", AAC_BITRATE, "-f", "adts", "-"],
        stdout=subprocess.PIPE)

    def close():
        proc.kill()
        proc.wait()
    return proc.stdout, close


# --- HLS ---------------------------------------------------------------------------------
# Sonos only takes per-song artwork from HLS: each packed-audio AAC segment starts with an
# ID3v2.4 tag carrying the MPEG-TS timestamp (PRIV), the title (TIT2) and the artwork
# (WXXX "artworkURL_<w>x"). Segments are cut on ADTS frame boundaries; a producer runs only
# while a player keeps polling.
HLS_SEGMENT_S = float(os.environ.get("SMAPI_HLS_SEGMENT_S", "6"))   # long segments: the player keeps ~18 s buffered
HLS_WINDOW = 10
ART_SIZES = (640, 1280)
HLS_IDLE_S = 60
_AAC_RATES = [96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000, 7350]


def adts_frame(buf, i):
    """(length, samples, rate) of the ADTS AAC frame at buf[i], or None."""
    if len(buf) < i + 7 or buf[i] != 0xFF or (buf[i + 1] & 0xF6) != 0xF0:
        return None
    sri = (buf[i + 2] >> 2) & 0xF
    length = ((buf[i + 3] & 3) << 11) | (buf[i + 4] << 3) | (buf[i + 5] >> 5)
    if sri >= len(_AAC_RATES) or length < 7:
        return None
    return length, 1024 * ((buf[i + 6] & 3) + 1), _AAC_RATES[sri]


def _synchsafe(n):
    return bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F])


def id3_tag(pts90k, title, artist, art):
    # ID3v2.4 with UTF-8 text: Sonos drops the whole tag for v2.3/UTF-16.
    def frame(fid, body):
        return fid + _synchsafe(len(body)) + b"\0\0" + body
    body = frame(b"PRIV", b"com.apple.streaming.transportStreamTimestamp\0"
                 + (pts90k & 0x1FFFFFFFF).to_bytes(8, "big"))
    body += frame(b"TIT2", b"\x03" + title.encode())
    body += frame(b"TPE1", b"\x03" + artist.encode())
    if art:
        for size in ART_SIZES:
            body += frame(b"WXXX", b"\x03" + f"artworkURL_{size}x".encode() + b"\x00" + art.encode())
    return b"ID3\x04\x00\x00" + _synchsafe(len(body)) + body


class HlsStream:
    def __init__(self, sid):
        self.sid = sid
        self.segments = collections.deque(maxlen=HLS_WINDOW)  # (seq, duration, bytes)
        self.cond = threading.Condition()
        self.last_access = 0.0
        self.thread = None
        self.seq = 0

    def touch(self):
        with self.cond:
            self.last_access = time.time()
            if not (self.thread and self.thread.is_alive()):
                self.segments.clear()
                self.thread = threading.Thread(target=self._run, daemon=True)
                self.thread.start()

    def _run(self):
        src, close = open_hls_source(self.sid)
        print(f"[hls] {self.sid} producer started", flush=True)
        buf, frames, seg_samples, rate, pts = b"", [], 0, 48000, 0
        try:
            while time.time() - self.last_access < HLS_IDLE_S:
                chunk = src.read(8192)
                if not chunk:
                    break
                buf += chunk
                if buf.startswith(b"ID3") and len(buf) >= 10:  # leading tag from the encoder
                    size = 10 + ((buf[6] << 21) | (buf[7] << 14) | (buf[8] << 7) | buf[9])
                    if len(buf) < size:
                        continue
                    buf = buf[size:]
                i = 0
                while True:
                    f = adts_frame(buf, i)
                    if not f:
                        nxt = buf.find(b"\xff", i + 1)
                        if nxt < 0 or len(buf) - i < 4:
                            break
                        i = nxt
                        continue
                    length, samples, rate = f
                    if len(buf) < i + length:
                        break
                    frames.append(buf[i:i + length])
                    seg_samples += samples
                    i += length
                    if seg_samples >= HLS_SEGMENT_S * rate:
                        title = now_playing(self.sid) or STREAMS[self.sid][0]
                        data = id3_tag(pts, title, STREAMS[self.sid][0], art_url(self.sid)) + b"".join(frames)
                        with self.cond:
                            self.segments.append((self.seq, seg_samples / rate, data))
                            self.seq += 1
                            self.cond.notify_all()
                        pts += seg_samples * 90000 // rate
                        frames, seg_samples = [], 0
                buf = buf[i:]
        except OSError:
            pass
        finally:
            close()
            print(f"[hls] {self.sid} producer stopped", flush=True)

    def playlist(self):
        self.touch()
        with self.cond:
            self.cond.wait_for(lambda: len(self.segments) >= 1, timeout=10)
            segs = list(self.segments)
        if not segs:
            return None
        lines = ["#EXTM3U", "#EXT-X-VERSION:3",
                 f"#EXT-X-TARGETDURATION:{int(max(d for _, d, _ in segs)) + 1}",
                 f"#EXT-X-MEDIA-SEQUENCE:{segs[0][0]}"]
        for seq, dur, _ in segs:
            lines += [f"#EXTINF:{dur:.3f},", f"{self.sid}/{seq}.aac"]
        return "\n".join(lines) + "\n"

    def segment(self, seq):
        self.touch()
        with self.cond:
            for s, _, data in self.segments:
                if s == seq:
                    return data
        return None


HLS = {sid: HlsStream(sid) for sid in STREAMS}


class AudioHandler(BaseHTTPRequestHandler):
    """GET /kkfm.mp3 (ffmpeg per listener) and /hourly.mp3 (pass-through of :8791),
    with ICY StreamTitle from the watchdog snapshot when the player asks for it."""
    protocol_version = "HTTP/1.0"

    def _send_bytes(self, data, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache, no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        m = re.fullmatch(r"/hls/(\w+)\.m3u8", path)
        if m and m.group(1) in HLS:
            pl = HLS[m.group(1)].playlist()
            print(f"[hls] {self.address_string()} {path} -> {'503' if pl is None else pl.count('.aac')} segs", flush=True)
            if pl is None:
                self.send_error(503)
            else:
                self._send_bytes(pl.encode(), "application/vnd.apple.mpegurl")
            return
        m = re.fullmatch(r"/hls/(\w+)/(\d+)\.aac", path)
        if m and m.group(1) in HLS:
            data = HLS[m.group(1)].segment(int(m.group(2)))
            print(f"[hls] {self.address_string()} {path} -> {'404' if data is None else len(data)}", flush=True)
            if data is None:
                self.send_error(404)
            else:
                self._send_bytes(data, "audio/aac")
            return
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
        src, close = open_source(sid)
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
                        art = art_url(sid)
                        if (title, art) != sent_title:
                            meta = f"StreamTitle='{title.replace(chr(39), '')}';"
                            if art:
                                meta += f"StreamUrl='{art}';"
                            meta = meta.encode()
                            meta += b"\0" * (-len(meta) % 16)
                            self.wfile.write(bytes([len(meta) // 16]) + meta)
                            sent_title = (title, art)
                            print(f"[audio] {sid} title -> {title} art={art or '-'}", flush=True)
                        else:
                            self.wfile.write(b"\0")
                        until_meta = ICY_METAINT
        except OSError:
            pass
        finally:
            close()
            print(f"[audio] {self.address_string()} {sid} disconnected", flush=True)

    def log_message(self, fmt, *a):
        pass


if __name__ == "__main__":
    load_jackets()
    for sid in STREAMS:
        threading.Thread(target=follow_journal, args=(sid,), daemon=True).start()
    audio = ThreadingHTTPServer((AUDIO_BIND, AUDIO_PORT), AudioHandler)
    audio.daemon_threads = True
    threading.Thread(target=audio.serve_forever, daemon=True).start()
    print(f"[smapi] listening on {BIND}:{PORT}, audio on {AUDIO_BIND}:{AUDIO_PORT}", flush=True)
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()
