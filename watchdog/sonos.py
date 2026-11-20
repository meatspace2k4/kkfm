"""Sonos control for the dashboard's STREAM chip (stdlib only).

Discovers players with SSDP, resolves the zone-group coordinator with
ZoneGroupTopology, and drives AVTransport (SetAVTransportURI / Play / Stop /
GetTransportInfo) over plain UPnP SOAP on port 1400.
"""
from __future__ import annotations

import html
import re
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

SSDP_ADDR = ("239.255.255.250", 1900)
SSDP_ST = "urn:schemas-upnp-org:device:ZonePlayer:1"
PORT = 1400


class SonosError(Exception):
    pass


@dataclass(frozen=True)
class Player:
    ip: str
    uuid: str
    room: str
    coordinator: bool


def discover(timeout_s: float = 2.0, first_only: bool = False) -> list[str]:
    """IPs of Sonos players answering an SSDP M-SEARCH. With first_only, return as
    soon as one answers (the topology call names every player and the coordinator)."""
    msg = (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {SSDP_ADDR[0]}:{SSDP_ADDR[1]}\r\n"
        'MAN: "ssdp:discover"\r\n'
        f"MX: {max(1, int(timeout_s))}\r\n"
        f"ST: {SSDP_ST}\r\n\r\n"
    ).encode()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.settimeout(timeout_s)
    ips: list[str] = []
    try:
        s.sendto(msg, SSDP_ADDR)
        while True:
            try:
                data, addr = s.recvfrom(4096)
            except socket.timeout:
                break
            if b"Sonos" in data and addr[0] not in ips:
                ips.append(addr[0])
                if first_only:
                    break
    finally:
        s.close()
    return ips


def soap(ip: str, service: str, path: str, action: str, body: str, timeout_s: float = 8.0) -> str:
    env = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
        f'<u:{action} xmlns:u="urn:schemas-upnp-org:service:{service}:1">{body}</u:{action}>'
        "</s:Body></s:Envelope>"
    )
    req = urllib.request.Request(
        f"http://{ip}:{PORT}{path}",
        data=env.encode("utf-8"),
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": f'"urn:schemas-upnp-org:service:{service}:1#{action}"',
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", errors="replace")
        m = re.search(r"<errorCode>(\d+)</errorCode>", text)
        raise SonosError(f"{action} failed: UPnP error {m.group(1) if m else e.code}") from None
    except (urllib.error.URLError, OSError) as e:
        raise SonosError(f"{action} failed: {e}") from None


def parse_zone_group_state(xml_text: str) -> list[Player]:
    """Players from a (possibly HTML-escaped) ZoneGroupState document."""
    x = html.unescape(xml_text)
    out: list[Player] = []
    for g in re.finditer(r'<ZoneGroup\s+Coordinator="([^"]+)"[^>]*>(.*?)</ZoneGroup>', x, re.S):
        coord = g.group(1)
        for m in re.finditer(r"<ZoneGroupMember\s+([^>]*?)/?>", g.group(2)):
            attrs = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
            loc = re.search(r"http://([\d.]+):", attrs.get("Location", ""))
            if not loc:
                continue
            out.append(Player(ip=loc.group(1), uuid=attrs.get("UUID", ""), room=attrs.get("ZoneName", "?"), coordinator=(attrs.get("UUID") == coord)))
    return out


def topology(any_ip: str) -> list[Player]:
    t = soap(any_ip, "ZoneGroupTopology", "/ZoneGroupTopology/Control", "GetZoneGroupState", "")
    m = re.search(r"<ZoneGroupState>(.*?)</ZoneGroupState>", t, re.S)
    if not m:
        raise SonosError("GetZoneGroupState: no ZoneGroupState in reply")
    return parse_zone_group_state(m.group(1))


def find_coordinator(timeout_s: float = 2.0) -> Player:
    ips = discover(timeout_s, first_only=True)
    if not ips:
        raise SonosError("no Sonos players found on the network")
    players = topology(ips[0])
    coords = [p for p in players if p.coordinator]
    if not coords:
        raise SonosError("no zone-group coordinator in topology")
    # Prefer a coordinator among the discovered players; else the first.
    for p in coords:
        if p.ip in ips:
            return p
    return coords[0]


def local_ip_towards(ip: str) -> str:
    """The local interface address a Sonos at `ip` can reach us on."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((ip, PORT))
        return s.getsockname()[0]
    finally:
        s.close()


def didl_for_wav_stream(url: str, title: str) -> str:
    """The /stream endpoint is 16-bit PCM WAV. Sonos plays it as an http-get
    audio/wav track when the metadata says so; without the res/protocolInfo it
    answers 714 (illegal MIME), and the x-rincon-mp3radio scheme makes it sniff
    the PCM as MP3 (static)."""
    return (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        '<item id="-1" parentID="-1" restricted="true">'
        f"<dc:title>{html.escape(title)}</dc:title>"
        "<upnp:class>object.item.audioItem.musicTrack</upnp:class>"
        f'<res protocolInfo="http-get:*:audio/wav:*">{html.escape(url)}</res>'
        "</item></DIDL-Lite>"
    )


def set_uri_and_play(coordinator_ip: str, url: str, title: str = "KKFM") -> None:
    meta = didl_for_wav_stream(url, title)
    body = (
        "<InstanceID>0</InstanceID>"
        f"<CurrentURI>{html.escape(url)}</CurrentURI>"
        f"<CurrentURIMetaData>{html.escape(meta)}</CurrentURIMetaData>"
    )
    # Stop whatever is loaded first (a previous attempt of this same URI included),
    # so SetAVTransportURI starts the stream from a clean transport.
    try:
        stop(coordinator_ip)
    except SonosError:
        pass
    soap(coordinator_ip, "AVTransport", "/MediaRenderer/AVTransport/Control", "SetAVTransportURI", body)
    soap(coordinator_ip, "AVTransport", "/MediaRenderer/AVTransport/Control", "Play", "<InstanceID>0</InstanceID><Speed>1</Speed>")


def radio_uri(http_url: str) -> str:
    """Sonos addresses an internet-radio stream (MP3/AAC with ICY metadata) with
    the x-rincon-mp3radio scheme: no length, live StreamTitle."""
    if http_url.startswith("http://"):
        return "x-rincon-mp3radio://" + http_url[len("http://"):]
    return http_url


def set_radio_and_play(coordinator_ip: str, http_url: str, station: str = "KKFM") -> None:
    meta = (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
        'xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/">'
        '<item id="R:0/0/0" parentID="R:0/0" restricted="true">'
        f"<dc:title>{html.escape(station)}</dc:title>"
        "<upnp:class>object.item.audioItem.audioBroadcast</upnp:class>"
        '<desc id="cdudn" nameSpace="urn:schemas-rinconnetworks-com:metadata-1-0/">SA_RINCON65031_</desc>'
        "</item></DIDL-Lite>"
    )
    uri = radio_uri(http_url)
    body = (
        "<InstanceID>0</InstanceID>"
        f"<CurrentURI>{html.escape(uri)}</CurrentURI>"
        f"<CurrentURIMetaData>{html.escape(meta)}</CurrentURIMetaData>"
    )
    try:
        stop(coordinator_ip)
    except SonosError:
        pass
    soap(coordinator_ip, "AVTransport", "/MediaRenderer/AVTransport/Control", "SetAVTransportURI", body)
    soap(coordinator_ip, "AVTransport", "/MediaRenderer/AVTransport/Control", "Play", "<InstanceID>0</InstanceID><Speed>1</Speed>")


def serves_radio(http_url: str, timeout_s: float = 3.0) -> bool:
    """True if `http_url` (the player's /radio.mp3) answers as an MP3 stream."""
    req = urllib.request.Request(http_url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return resp.status == 200 and "audio/mpeg" in (resp.headers.get("Content-Type") or "")
    except Exception:
        return False


def stop(coordinator_ip: str) -> None:
    soap(coordinator_ip, "AVTransport", "/MediaRenderer/AVTransport/Control", "Stop", "<InstanceID>0</InstanceID>")


def transport_state(coordinator_ip: str) -> str:
    t = soap(coordinator_ip, "AVTransport", "/MediaRenderer/AVTransport/Control", "GetTransportInfo", "<InstanceID>0</InstanceID>")
    m = re.search(r"<CurrentTransportState>(.*?)</CurrentTransportState>", t)
    return m.group(1) if m else "?"


def current_uri(coordinator_ip: str) -> Optional[str]:
    t = soap(coordinator_ip, "AVTransport", "/MediaRenderer/AVTransport/Control", "GetMediaInfo", "<InstanceID>0</InstanceID>")
    m = re.search(r"<CurrentURI>(.*?)</CurrentURI>", t, re.S)
    return html.unescape(m.group(1)) if m else None
