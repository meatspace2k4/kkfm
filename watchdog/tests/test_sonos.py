"""Sonos helpers: topology parsing and SOAP request shape (no network)."""
from __future__ import annotations

import html
import unittest
from unittest import mock

from watchdog import sonos

ZGS = (
    '<ZoneGroups><ZoneGroup Coordinator="RINCON_AAA" ID="RINCON_AAA:1">'
    '<ZoneGroupMember UUID="RINCON_AAA" Location="http://192.168.10.156:1400/xml/device_description.xml" ZoneName="Balcony" Icon="x"/>'
    '<ZoneGroupMember UUID="RINCON_BBB" Location="http://192.168.10.225:1400/xml/device_description.xml" ZoneName="Balcony" Icon="x"/>'
    "</ZoneGroup></ZoneGroups>"
)


class TestTopology(unittest.TestCase):
    def test_parse_plain_and_escaped(self) -> None:
        for doc in (ZGS, html.escape(ZGS)):
            ps = sonos.parse_zone_group_state(doc)
            self.assertEqual(len(ps), 2)
            self.assertEqual([p.ip for p in ps], ["192.168.10.156", "192.168.10.225"])
            self.assertTrue(ps[0].coordinator)
            self.assertFalse(ps[1].coordinator)
            self.assertEqual(ps[0].room, "Balcony")

    def test_find_coordinator_prefers_discovered(self) -> None:
        with mock.patch.object(sonos, "discover", return_value=["192.168.10.225", "192.168.10.156"]), \
             mock.patch.object(sonos, "topology", return_value=sonos.parse_zone_group_state(ZGS)):
            c = sonos.find_coordinator()
        self.assertEqual(c.ip, "192.168.10.156")
        self.assertTrue(c.coordinator)

    def test_find_coordinator_no_players(self) -> None:
        with mock.patch.object(sonos, "discover", return_value=[]):
            with self.assertRaises(sonos.SonosError):
                sonos.find_coordinator()


class TestSoapShape(unittest.TestCase):
    def test_set_uri_and_play_requests(self) -> None:
        calls = []

        def fake_soap(ip, service, path, action, body, timeout_s=8.0):
            calls.append((ip, service, path, action, body))
            return "<ok/>"

        with mock.patch.object(sonos, "soap", side_effect=fake_soap):
            sonos.set_uri_and_play("192.168.10.156", "http://192.168.10.189:8791/stream", title="KKFM")
        self.assertEqual([c[3] for c in calls], ["SetAVTransportURI", "Play"])
        self.assertTrue(all(c[1] == "AVTransport" and c[0] == "192.168.10.156" for c in calls))
        self.assertIn("x-rincon-mp3radio://192.168.10.189:8791/stream", calls[0][4])
        self.assertNotIn("http://192.168.10.189", calls[0][4].split("CurrentURIMetaData")[0])
        self.assertIn("audioBroadcast", html.unescape(calls[0][4]))

    def test_radio_uri(self) -> None:
        self.assertEqual(sonos.radio_uri("http://h:1/stream"), "x-rincon-mp3radio://h:1/stream")
        self.assertEqual(sonos.radio_uri("x-rincon-mp3radio://h:1/s"), "x-rincon-mp3radio://h:1/s")

    def test_transport_state_parse(self) -> None:
        with mock.patch.object(sonos, "soap", return_value="<r><CurrentTransportState>PLAYING</CurrentTransportState></r>"):
            self.assertEqual(sonos.transport_state("1.2.3.4"), "PLAYING")

    def test_upnp_error_is_sonos_error(self) -> None:
        import io
        import urllib.error

        err = urllib.error.HTTPError("http://x", 500, "err", {}, io.BytesIO(b"<errorCode>714</errorCode>"))
        with mock.patch.object(sonos.urllib.request, "urlopen", side_effect=err):
            with self.assertRaises(sonos.SonosError) as cm:
                sonos.soap("1.2.3.4", "AVTransport", "/x", "Play", "")
        self.assertIn("714", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
