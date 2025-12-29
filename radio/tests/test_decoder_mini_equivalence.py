# -*- coding: utf-8 -*-
from __future__ import annotations

import numpy as np

from radio.decoder import ADPCMState as RefState, AudioContext, BWAVChannelInfo, BWAVHeader
from radio.decoder_mini import ADPCMState as MiniState

def _build_block(pred_idx: int, scale_n: int, nibbles14):
    header = bytes([(pred_idx << 4) | (scale_n & 0x0F)])
    data = bytearray()
    for i in range(0, 14, 2):
        data.append(((nibbles14[i] & 0x0F) << 4) | (nibbles14[i + 1] & 0x0F))
    return header + bytes(data)

def _mk_ctx(raw: bytes, coeffs, sr: int, num_samples: int, loop_start=0, loop_end=0xFFFFFFFF):
    header = BWAVHeader(b"BWAV", 0xFEFF, 0, 0, 0, 1)
    info = BWAVChannelInfo(0, sr, num_samples, 0, 0, loop_end, loop_start, 0, 0, 0)
    return AudioContext("<mem>", header, info, raw, coeffs)

def _coeffs_table():
    # simple table: idx0=(0,0), idx1=(512,-256), fill rest zeros
    out = [(0, 0)] * 16
    out[1] = (512, -256)
    return out

def test_mini_matches_reference_simple():
    n1 = list(range(14))
    n2 = [(i * 3) & 0x0F for i in range(14)]
    raw = _build_block(0, 0, n1) + _build_block(1, 2, n2)
    coeffs = _coeffs_table()
    ctx = _mk_ctx(raw, coeffs, 32000, 28)

    ref = RefState(ctx)
    mini = MiniState(ctx)

    # Test multiple read sizes to ensure consistency
    a_full = ref.read(28)
    b_full = mini.read(28)
    assert a_full.dtype == np.int16 and b_full.dtype == np.int16
    assert np.array_equal(a_full, b_full), f"Full read mismatch: {a_full[:5]} vs {b_full[:5]}"

    # Test partial reads with state persistence: re-instantiate to reset state
    ref = RefState(ctx)
    mini = MiniState(ctx)
    a1 = ref.read(10)
    b1 = mini.read(10)
    assert np.array_equal(a1, b1), "Partial read 1 mismatch"

    a2 = ref.read(10)  # Should continue from where it left off
    b2 = mini.read(10)
    assert np.array_equal(a2, b2), "Partial read 2 mismatch"

    a3 = ref.read(8)
    b3 = mini.read(8)
    assert np.array_equal(a3, b3), "Partial read 3 mismatch"

    # Final outputs matched across read sizes; internal state is an implementation detail

    # Test edge case: reading exactly at buffer boundaries from fresh state
    ref = RefState(ctx)
    mini = MiniState(ctx)
    for i in range(28):
        a_single = ref.read(1)
        b_single = mini.read(1)
        assert np.array_equal(a_single, b_single), f"Single sample mismatch at position {i}"

def test_mini_matches_reference_with_loop_skip():
    n1 = [(i + 7) & 0x0F for i in range(14)]
    n2 = [(15 - i) & 0x0F for i in range(14)]
    raw = _build_block(1, 1, n1) + _build_block(1, 1, n2)
    coeffs = _coeffs_table()
    loop_start = 9
    loop_end = 20
    ctx = _mk_ctx(raw, coeffs, 48000, 28, loop_start, loop_end)

    ref = RefState(ctx)
    mini = MiniState(ctx)

    # read across wrap
    frames = 25
    a = ref.read(frames)
    b = mini.read(frames)
    assert np.array_equal(a, b)

def test_mini_matches_reference_with_fade():
    n1 = list(range(14))
    n2 = [(i + 1) % 16 for i in range(14)]
    raw = _build_block(0, 0, n1) + _build_block(0, 0, n2)
    coeffs = _coeffs_table()
    loop_start = 5
    loop_end = 15
    ctx = _mk_ctx(raw, coeffs, 48000, 28, loop_start, loop_end)

    ref = RefState(ctx)
    mini = MiniState(ctx)

    # read through fade window
    frames = 25
    a = ref.read(frames)
    b = mini.read(frames)
    assert np.array_equal(a, b)
