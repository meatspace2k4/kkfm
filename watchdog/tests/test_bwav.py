import unittest

from watchdog.bwav import BwavError, parse_bwav_header


class TestBwav(unittest.TestCase):
    def test_bad_magic(self):
        buf = b"NOPE" + b"\x00" * 200
        with self.assertRaises(BwavError):
            parse_bwav_header(buf)

    def test_too_small(self):
        with self.assertRaises(BwavError):
            parse_bwav_header(b"BWAV" + b"\x00" * 10)

    def test_good_minimal(self):
        # Build a minimal header that passes our checks.
        buf = bytearray(b"BWAV" + b"\x00" * 200)
        # bom at 4
        buf[4] = 0xFF
        buf[5] = 0xFE
        # channels at 14
        buf[14] = 0x01
        buf[15] = 0x00
        ci = 16
        # rate at ci+4 = 48000
        rate = 48000
        buf[ci + 4 : ci + 8] = rate.to_bytes(4, "little", signed=False)
        # total at ci+8
        total = 96000
        buf[ci + 8 : ci + 12] = total.to_bytes(4, "little", signed=False)
        # start at ci+52 (must be < len(buf))
        start = 128
        buf[ci + 52 : ci + 56] = start.to_bytes(4, "little", signed=False)
        h = parse_bwav_header(bytes(buf))
        self.assertEqual(h.rate, rate)
        self.assertEqual(h.total_samples, total)
        self.assertEqual(h.start_offset, start)


