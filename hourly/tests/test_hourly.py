"""Hourly BGM fork: Python-side checks.

Run from the repo root (a directory containing the kkfm package, see hourly/build.sh):
    python3 -m unittest hourly.tests.test_hourly -v

Covers:
  - the 72 BGM files are 2-channel BWAV with per-channel offsets and shared loop points
  - the fork's log lines parse with the existing watchdog log parser / dashboard regexes
  - the engine-decoded loop-once+fade WAV (written by hourly/tests_hourly.cpp) has the
    expected length and fade envelope, and a spectrogram PNG is produced for review
"""
from __future__ import annotations

import os
import re
import struct
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOURLY = HERE.parent
REPO = HOURLY.parent
SOUND_DIR = Path(os.environ.get("HOURLY_SOUND_DIR", str(HOURLY / "Stream")))
OUT_DIR = Path(os.environ.get("HOURLY_OUT_DIR", str(HOURLY / "out")))

sys.path.insert(0, str(REPO))
from watchdog.log_parser import parse_event  # noqa: E402
from watchdog.events import EventType  # noqa: E402
from watchdog.ansi import strip_ansi  # noqa: E402

WEATHERS = ("Sunny", "Rainy", "Snowy")
CH_INFO = 0x4C
RATE = 48000


def _engine_const(name: str) -> float:
    """Read a `const float NAME = X.Yf;` from the engine so the tests follow it."""
    src = (REPO / "acnh_aircheck_pi.cpp").read_text(errors="ignore")
    m = re.search(rf"const\s+float\s+{name}\s*=\s*([0-9.]+)f?\s*;", src)
    if not m:
        raise RuntimeError(f"{name} not found in acnh_aircheck_pi.cpp")
    return float(m.group(1))


FADE_PLAY_S = _engine_const("FADE_PLAY_DURATION")
FADE_TIME_S = _engine_const("FADE_TIME_DURATION")


def u16(b: bytes, o: int) -> int:
    return struct.unpack_from("<H", b, o)[0]


def u32(b: bytes, o: int) -> int:
    return struct.unpack_from("<I", b, o)[0]


def parse_stereo_header(buf: bytes) -> dict:
    """Mirror of BWAVParser::parse_bytes for the header only (1 or 2 channels)."""
    if len(buf) < 96 or buf[:4] != b"BWAV":
        raise ValueError("bad magic")
    if u16(buf, 4) != 0xFEFF:
        raise ValueError("bad bom")
    chans = u16(buf, 14)
    if chans not in (1, 2):
        raise ValueError(f"unsupported channels {chans}")
    out = {"channels": chans, "ch": []}
    for c in range(chans):
        ci = 16 + c * CH_INFO
        out["ch"].append(
            {
                "rate": u32(buf, ci + 4),
                "total": u32(buf, ci + 8),
                "start": u32(buf, ci + 52),
                "loops": u32(buf, ci + 56),
                "loop_end": u32(buf, ci + 60),
                "loop_start": u32(buf, ci + 64),
                "coeffs": buf[ci + 16 : ci + 48],
            }
        )
    return out


class TestBgmFiles(unittest.TestCase):
    def test_all_72_files_are_stereo_with_shared_loop(self) -> None:
        self.assertTrue(SOUND_DIR.is_dir(), f"missing {SOUND_DIR}")
        n = 0
        for hour in range(24):
            for w in WEATHERS:
                p = SOUND_DIR / f"BGM_24Hour_{hour:02d}_{w}.bwav"
                self.assertTrue(p.is_file(), p)
                size = p.stat().st_size
                h = parse_stereo_header(p.read_bytes()[:512])
                self.assertEqual(h["channels"], 2, p)
                c0, c1 = h["ch"]
                self.assertEqual(c0["rate"], RATE)
                self.assertEqual(c1["rate"], RATE)
                self.assertEqual(c0["total"], c1["total"])
                self.assertEqual(c0["loop_end"], c1["loop_end"])
                self.assertEqual(c0["loop_start"], c1["loop_start"])
                self.assertEqual(c0["loop_end"], c0["total"])
                self.assertLess(c0["loop_start"], c0["loop_end"])
                self.assertEqual(c0["start"], 192)
                self.assertGreater(c1["start"], c0["start"])
                self.assertLess(c1["start"], size)
                self.assertNotEqual(c0["coeffs"], c1["coeffs"])
                n += 1
        self.assertEqual(n, 72)


class TestPacker(unittest.TestCase):
    """hourly/packer_hourly.py packs the set the way the radio packer does."""

    def test_pack_table_matches_files(self) -> None:
        sys.path.insert(0, str(HOURLY))
        import packer_hourly

        with tempfile.TemporaryDirectory() as td:
            os.makedirs(os.path.join(td, "hourly"))
            entries = packer_hourly.pack(str(SOUND_DIR), td)
            self.assertEqual(len(entries), 72)
            blob = Path(td) / packer_hourly.BLOB
            hdr = (Path(td) / packer_hourly.HDR).read_text()
            asm = (Path(td) / packer_hourly.ASM).read_text()
            self.assertIn("hourly_assets_count = 72", hdr)
            self.assertIn("hourly_assets_blob", asm)
            self.assertIn(f'.incbin "{packer_hourly.BLOB}"', asm)
            expect_off = 0
            with open(blob, "rb") as b:
                for off, size, hour, w, name in entries:
                    self.assertEqual(off, expect_off)            # contiguous
                    self.assertEqual(size, (SOUND_DIR / name).stat().st_size)
                    self.assertEqual(name, f"BGM_24Hour_{hour:02d}_{WEATHERS[w]}.bwav")
                    b.seek(off)
                    img = b.read(size)
                    h = parse_stereo_header(img[:512])           # header intact in the blob
                    self.assertEqual(h["channels"], 2)
                    self.assertEqual(img[-4:], b"\x00\x00\x00\x00")  # CRC zeroed like the radio packer
                    self.assertEqual(img[:-4], (SOUND_DIR / name).read_bytes()[:-4])
                    expect_off += size
            self.assertEqual(blob.stat().st_size, expect_off)


class TestLogLines(unittest.TestCase):
    """Lines the fork emits must be understood by the existing watchdog."""

    # Same regexes as watchdog/run.py maybe_note_bwav
    bwav_file_re = re.compile(r"\[(?:FILE|BWAV)\]\s+(?P<name>[^()|]+?)\s*(?:\(|\|)", re.IGNORECASE)

    def test_now_playing_parses(self) -> None:
        ev = parse_event("\x1b[1mNow Playing: \x1b[0m1 PM (Sunny)", default_ts=0.0)
        self.assertIsNotNone(ev)
        self.assertEqual(ev.etype, EventType.NOW_PLAYING)
        self.assertEqual(ev.name, "1 PM (Sunny)")

    def test_file_line_noted_by_dashboard_regex(self) -> None:
        line = strip_ansi("\x1b[90m[FILE] \x1b[0m\x1b[36mBGM_24Hour_13_Sunny.bwav\x1b[0m (id=13, size=3879573)")
        m = self.bwav_file_re.search(line)
        self.assertIsNotNone(m)
        self.assertEqual(m.group("name").strip(), "BGM_24Hour_13_Sunny.bwav")

    def test_fork_lines_are_bgm_events_not_radio_events(self) -> None:
        expected = {
            "[HOURLY] Hour 13 -> picked BGM_24Hour_13_Sunny.bwav (weather=Sunny)": EventType.BGM_PICKED,
            "[HOURLY] Changeover 13 -> 14: BGM_24Hour_13_Sunny.bwav fades at its next loop end (hour changed)": EventType.BGM_CHANGEOVER,
            "[HOURLY] Track end: BGM_24Hour_13_Sunny.bwav (loops=37)": EventType.BGM_END,
            "[HOURLY] Weather fetched: Rainy (wmo=61 rain=0.40mm showers=0.00mm snow=0.00cm at 2026-09-20T12:45)": EventType.WEATHER_FETCHED,
        }
        radio_types = {t for t in EventType if t.value.startswith(("jingle_", "commercial_"))}
        for line, et in expected.items():
            ev = parse_event(line, default_ts=0.0)
            self.assertIsNotNone(ev, line)
            self.assertEqual(ev.etype, et, line)
            self.assertNotIn(ev.etype, radio_types)


class TestDecodedWav(unittest.TestCase):
    """hourly/out/loop_once_fade.wav is written by hourly/tests_hourly.cpp from the engine."""

    wav = OUT_DIR / "loop_once_fade.wav"

    def _read(self):
        import numpy as np

        with wave.open(str(self.wav), "rb") as w:
            self.assertEqual(w.getnchannels(), 2)
            self.assertEqual(w.getframerate(), RATE)
            self.assertEqual(w.getsampwidth(), 2)
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").reshape(-1, 2)
        return np, pcm

    def test_length_matches_loop_end_plus_fade(self) -> None:
        self.assertTrue(self.wav.is_file(), f"run the C++ suite first: {self.wav}")
        np, pcm = self._read()
        h = parse_stereo_header((SOUND_DIR / "BGM_24Hour_00_Sunny.bwav").read_bytes()[:512])
        loop_end = h["ch"][0]["loop_end"]
        expect = loop_end + int(FADE_PLAY_S * RATE) + int(FADE_TIME_S * RATE)
        self.assertGreaterEqual(pcm.shape[0], expect)
        self.assertLessEqual(pcm.shape[0], expect + 14)
        # Non-silent start, non-silent right before the loop end, silent tail.
        self.assertGreater(np.abs(pcm[:RATE]).mean(), 20)
        self.assertGreater(np.abs(pcm[loop_end - RATE : loop_end]).mean(), 20)
        self.assertLess(np.abs(pcm[expect - 480 : expect]).mean(), 8)

    def test_fade_envelope_and_spectrogram(self) -> None:
        self.assertTrue(self.wav.is_file(), f"run the C++ suite first: {self.wav}")
        np, pcm = self._read()
        h = parse_stereo_header((SOUND_DIR / "BGM_24Hour_00_Sunny.bwav").read_bytes()[:512])
        loop_end = h["ch"][0]["loop_end"]
        loop_start = h["ch"][0]["loop_start"]
        play = int(FADE_PLAY_S * RATE)
        fade = int(FADE_TIME_S * RATE)
        fade_from = loop_end + play
        ref_from = loop_start + play
        x = pcm.astype(np.float64)
        for k in range(14):
            a, b = k * 24000, (k + 1) * 24000
            faded = np.sqrt((x[fade_from + a : fade_from + b] ** 2).mean())
            ref = np.sqrt((x[ref_from + a : ref_from + b] ** 2).mean())
            expect_gain = 1.0 - (k + 0.5) * 24000 / fade
            self.assertGreater(ref, 50)
            self.assertAlmostEqual(faded / ref, expect_gain, delta=0.06, msg=f"chunk {k}")
        png = OUT_DIR / "loop_once_fade.png"
        subprocess.run(
            [
                sys.executable,
                str(HOURLY / "tools" / "spectrogram.py"),
                str(self.wav),
                str(png),
                "--title",
                "BGM_24Hour_00_Sunny via engine: frame 0 -> loop end -> 10s play -> 7s fade",
                "--marks",
                f"{loop_start / RATE:.3f},{loop_end / RATE:.3f},{fade_from / RATE:.3f},{(fade_from + fade) / RATE:.3f}",
            ],
            check=True,
        )
        self.assertTrue(png.is_file())
        self.assertGreater(png.stat().st_size, 10000)


if __name__ == "__main__":
    unittest.main()
