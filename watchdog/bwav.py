from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BwavHeader:
    rate: int
    total_samples: int
    start_offset: int
    channels: int
    bom: int

    @property
    def duration_sec(self) -> float:
        if self.rate <= 0:
            return 0.0
        return float(self.total_samples) / float(self.rate)


class BwavError(ValueError):
    pass


def _u16_le(b: bytes, off: int) -> int:
    return b[off] | (b[off + 1] << 8)


def _u32_le(b: bytes, off: int) -> int:
    return b[off] | (b[off + 1] << 8) | (b[off + 2] << 16) | (b[off + 3] << 24)


def parse_bwav_header(buf: bytes) -> BwavHeader:
    """Parse and validate the BWAV container header used by this project.

    Mirrors the checks in `BWAVParser::parse()`:
    - magic 'BWAV'
    - bom 0xFEFF
    - channels == 1
    - len >= 96
    - start offset within file length
    """
    if len(buf) < 96:
        raise BwavError(f"too small for BWAV header: {len(buf)} bytes")
    if buf[0:4] != b"BWAV":
        raise BwavError("bad magic (expected BWAV)")
    bom = _u16_le(buf, 4)
    chans = _u16_le(buf, 14)
    if bom != 0xFEFF:
        raise BwavError(f"bad bom: 0x{bom:04x} (expected 0xfeff)")
    if chans != 1:
        raise BwavError(f"unexpected channels: {chans} (expected 1)")

    ci = 16
    rate = _u32_le(buf, ci + 4)
    total = _u32_le(buf, ci + 8)
    start = _u32_le(buf, ci + 52)
    if start >= len(buf):
        raise BwavError(f"start offset out of range: {start} >= {len(buf)}")

    return BwavHeader(
        rate=int(rate),
        total_samples=int(total),
        start_offset=int(start),
        channels=int(chans),
        bom=int(bom),
    )


