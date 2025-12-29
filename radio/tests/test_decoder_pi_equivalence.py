# -*- coding: utf-8 -*-
from __future__ import annotations

import numpy as np

from radio.decoder import ADPCMState as RefState, AudioContext, BWAVChannelInfo, BWAVHeader
from radio.decoder_pi import ADPCMState as PiState


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


def test_pi_matches_reference_simple():
    n1 = list(range(14))
    n2 = [(i * 3) & 0x0F for i in range(14)]
    raw = _build_block(0, 0, n1) + _build_block(1, 2, n2)
    coeffs = _coeffs_table()
    ctx = _mk_ctx(raw, coeffs, 32000, 28)

    ref = RefState(ctx)
    pi = PiState(ctx)

    a = ref.read(28)
    b = pi.read(28)
    assert a.dtype == np.int16 and b.dtype == np.int16
    assert np.array_equal(a, b)


def test_pi_matches_reference_with_loop_skip():
    n1 = [(i + 7) & 0x0F for i in range(14)]
    n2 = [(15 - i) & 0x0F for i in range(14)]
    raw = _build_block(1, 1, n1) + _build_block(1, 1, n2)
    coeffs = _coeffs_table()
    loop_start = 9
    loop_end = 20
    ctx = _mk_ctx(raw, coeffs, 48000, 28, loop_start, loop_end)

    ref = RefState(ctx)
    pi = PiState(ctx)

    # read across wrap
    frames = 25
    a = ref.read(frames)
    b = pi.read(frames)
    assert np.array_equal(a, b)


