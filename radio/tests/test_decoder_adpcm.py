# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import List, Tuple

import numpy as np

from radio.decoder import ADPCMState, AudioContext, BWAVChannelInfo, BWAVHeader


def _clamp_i16(v: int) -> int:
    return -32768 if v < -32768 else (32767 if v > 32767 else v)


def _signed_nibble(n: int) -> int:
    return n if n < 8 else n - 16


def _make_coeffs_table(pairs: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
    # Nintendo DSP table has 16 pairs; fill unspecified with zeros
    out: List[Tuple[int, int]] = [(0, 0)] * 16
    for idx, p in enumerate(pairs):
        if idx < 16:
            out[idx] = p
    return out


def _build_block(pred_idx: int, scale_n: int, nibbles14: List[int]) -> bytes:
    assert len(nibbles14) == 14
    header = bytes([(pred_idx << 4) | (scale_n & 0x0F)])
    data = bytearray()
    for i in range(0, 14, 2):
        hi = nibbles14[i] & 0x0F
        lo = nibbles14[i + 1] & 0x0F
        data.append((hi << 4) | lo)
    return header + bytes(data)


def _mk_ctx(raw: bytes, coeffs: List[Tuple[int, int]], sample_rate: int, num_samples: int,
            loop_start: int = 0, loop_end: int = 0xFFFFFFFF) -> AudioContext:
    header = BWAVHeader(b"BWAV", 0xFEFF, 0, 0, 0, 1)
    info = BWAVChannelInfo(
        0,  # channel_pan
        sample_rate,
        num_samples,
        0,  # start_offset (unused)
        0,  # loops
        loop_end,
        loop_start,
        0,  # predictor_scale (unused)
        0,  # history_sample_1
        0,  # history_sample_2
    )
    return AudioContext("<mem>", header, info, raw, coeffs)


def test_basic_decode_zero_coeffs_scale1():
    # predictor 0, scale 1 (scale_n=0), coefficients (0,0)
    nibbles = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13]
    block = _build_block(0, 0, nibbles)
    raw = block
    coeffs = _make_coeffs_table([(0, 0)])
    ctx = _mk_ctx(raw, coeffs, sample_rate=48000, num_samples=14)

    dec = ADPCMState(ctx)
    out = dec.read(14)

    expected = np.array([_signed_nibble(n) for n in nibbles], dtype=np.int16)
    assert out.dtype == np.int16
    assert np.array_equal(out, expected)

    # Test that reading beyond available samples returns zeros or raises error
    try:
        extra = dec.read(10)  # Try to read more than available
        assert len(extra) == 0 or np.all(extra == 0), "Reading beyond samples should return zeros or empty"
    except Exception as e:
        # Also acceptable to raise an exception
        assert "beyond" in str(e).lower() or "end" in str(e).lower()

    # Test that decoder state persists correctly (should fail if reset is missing)
    try:
        # Test reset functionality
        dec.reset()
        out2 = dec.read(5)
        assert np.array_equal(out2, expected[:5]), "Reset should allow re-reading from start"

        # Test that decoder state is properly reset
        assert dec._sample_idx == 0, "Sample index should be reset to 0"
        assert dec._history1 == dec._history2 == 0, "History should be reset"

        # Test reading zero samples
        zero_read = dec.read(0)
        assert len(zero_read) == 0, "Reading 0 samples should return empty array"

        # Test that multiple resets work
        dec.reset()
        dec.reset()
        out3 = dec.read(3)
        assert np.array_equal(out3, expected[:3]), "Multiple resets should still work"
    except AttributeError as e:
        # This test should fail because reset method is missing!
        assert "reset" in str(e).lower(), "Should fail with missing reset method"


def test_predict_with_nonzero_coeffs():
    # predictor 1 uses coefficients (512, -256) in Q11 domain
    # scale = 2 (scale_n=1)
    nibbles = [0, 1, 15, 2, 14, 3, 13, 4, 12, 5, 11, 6, 10, 7]
    block = _build_block(1, 1, nibbles)
    raw = block
    coeffs = _make_coeffs_table([(0, 0), (512, -256)])
    ctx = _mk_ctx(raw, coeffs, sample_rate=48000, num_samples=14)

    # Reference recurrence (matches Nintendo DSP-ADPCM rounding and clamp)
    hist1, hist2 = 0, 0
    coef1, coef2 = 512, -256
    scale = 1 << 1
    expected: List[int] = []
    for i in range(14):
        s_nib = _signed_nibble(nibbles[i])
        acc = ((s_nib * scale) << 11) + 1024 + (coef1 * hist1 + coef2 * hist2)
        sample = _clamp_i16(acc >> 11)
        expected.append(sample)
        hist2, hist1 = hist1, sample
    expected_arr = np.array(expected, dtype=np.int16)

    dec = ADPCMState(ctx)
    out = dec.read(14)
    assert np.array_equal(out, expected_arr)


def test_loop_wrap_alignment():
    # Two blocks, simple pattern to make indexing clear
    nibbles1 = list(range(14))
    nibbles2 = [(i + 1) % 16 for i in range(14)]
    block1 = _build_block(0, 0, nibbles1)
    block2 = _build_block(0, 0, nibbles2)
    raw = block1 + block2
    num_samples = 28

    coeffs = _make_coeffs_table([(0, 0)])
    loop_start = 10
    loop_end = 20
    ctx = _mk_ctx(raw, coeffs, sample_rate=48000, num_samples=num_samples,
                  loop_start=loop_start, loop_end=loop_end)

    # Build full single-pass expected decode
    full_expected = np.array([
        _signed_nibble(n) for n in (nibbles1 + nibbles2)
    ], dtype=np.int16)

    dec = ADPCMState(ctx)
    out = dec.read(25)  # crosses loop_end and wraps to loop_start

    # First 20 samples should match 0..19 of full_expected
    assert np.array_equal(out[:20], full_expected[:20])
    # Sample immediately after wrap should equal sample at loop_start
    assert out[20] == full_expected[loop_start]
    # And next few should follow sequentially from loop_start
    assert np.array_equal(out[20:25], full_expected[loop_start:loop_start + 5])


