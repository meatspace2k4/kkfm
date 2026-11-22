"""HLS producer over a full night of audio, run as fast as the parser goes.

Run from the repo root: python3 -m unittest sonos.test_smapi_hls -v
"""

import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import smapi_server as s  # noqa: E402

HOURS = 14
PRIV_OWNER = b"com.apple.streaming.transportStreamTimestamp\0"
SRI = {48000: 3, 44100: 4}


def adts_frame(rate):
    length = 8
    return bytes([0xFF, 0xF1, (1 << 6) | (SRI[rate] << 2), (2 << 6) | ((length >> 11) & 3),
                  (length >> 3) & 0xFF, ((length & 7) << 5) | 0x1F, 0xFC, 0x00])


def priv_pts(data):
    i = data.index(PRIV_OWNER) + len(PRIV_OWNER)
    return int.from_bytes(data[i:i + 8], "big")


def run_night(rate):
    n_frames = HOURS * 3600 * rate // 1024
    src = io.BytesIO(adts_frame(rate) * n_frames)
    s.open_hls_source = lambda sid: (src, lambda: None)
    s.HLS_KEEP = 10 ** 7
    st = s.HlsStream("hourly")
    st.last_access = float("inf")
    st._run()
    return list(st.segments), n_frames


class HlsNight(unittest.TestCase):
    def check(self, rate):
        segs, n_frames = run_night(rate)
        seg_samples = round(segs[0][1] * rate)
        self.assertEqual([x[0] for x in segs], list(range(len(segs))), "sequence numbers not contiguous")
        self.assertLess(n_frames * 1024 - len(segs) * seg_samples, seg_samples, "audio missing from segments")
        self.assertTrue(all(round(x[1] * rate) == seg_samples for x in segs), "segment lengths vary")

        epoch_k = 0
        for k, (seq, dur, data, dseq) in enumerate(segs):
            pts = priv_pts(data)
            self.assertLess(pts, 1 << 31, f"seg {seq}: PTS {pts} past 2^31")
            if k and dseq != segs[k - 1][3]:
                self.assertEqual(pts, 0, f"seg {seq}: discontinuity does not restart PTS")
                epoch_k = k
            exact = (k - epoch_k) * seg_samples * 90000 / rate
            self.assertLess(abs(pts - exact), 1.0,
                            f"seg {seq} ({k * dur / 3600:.2f} h): PTS {pts} vs audio {exact:.1f} ticks")

    def test_48000(self):
        self.check(48000)

    def test_44100(self):
        self.check(44100)


def sonos_night(drift_hz, sonos_ppm, hours=HOURS):
    """A Sonos whose clock runs sonos_ppm off the Pi's plays the live HLS stream for `hours`.
    Segments hold a fixed number of samples; the producer finishes one every seg_audio seconds
    of source audio, shortened by the relay's stretch (drift_hz). The player starts 3 segments
    behind live and plays each segment once it has it. Returns (stalls, max segments behind live)."""
    rate = s.HLS_SOURCE_RATE
    seg_samples = -(-int(s.HLS_SEGMENT_S * rate) // 1024) * 1024
    seg_audio = seg_samples / rate                                  # seconds of audio per segment
    produced_every = seg_audio * (rate - drift_hz) / rate           # Pi seconds per segment
    play_time = seg_audio / (1 + sonos_ppm * 1e-6)                  # Pi seconds the Sonos takes per segment
    ready = lambda k: (k + 1) * produced_every
    j, t = 0, ready(2)                                              # starts with 3 segments in hand
    stalls, behind = 0, 0
    while t < hours * 3600:
        if ready(j) > t:
            stalls += 1
            t = ready(j)
        live = int(t / produced_every) - 1
        behind = max(behind, live - j)
        t += play_time
        j += 1
    return stalls, behind


class SonosClock(unittest.TestCase):
    """The Sonos plays on its own clock; over a night it must neither run dry nor fall out of
    the playlist, whether its clock is 100 ppm fast or slow."""

    def test_fast_sonos_never_runs_dry(self):
        stalls, _ = sonos_night(s.HLS_DRIFT_HZ, +100)
        self.assertEqual(stalls, 0)

    def test_slow_sonos_stays_in_playlist(self):
        stalls, behind = sonos_night(s.HLS_DRIFT_HZ, -100)
        self.assertEqual(stalls, 0)
        self.assertLess(behind, s.HLS_WINDOW)

    def test_unstretched_stream_runs_dry(self):
        stalls, _ = sonos_night(0, +100)
        self.assertGreater(stalls, 0)


if __name__ == "__main__":
    unittest.main()
