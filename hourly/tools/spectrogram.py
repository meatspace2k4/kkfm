#!/usr/bin/env python3
"""Render a waveform + spectrogram PNG for a 16-bit PCM WAV (mono or stereo).

Usage:
    python3 hourly/tools/spectrogram.py in.wav out.png [--title T] [--marks t1,t2,...]

Marks are times in seconds drawn as vertical lines (e.g. loop end, fade start, fade end).
"""
from __future__ import annotations

import argparse
import struct
import sys
import wave

import numpy as np


def read_wav(path: str) -> tuple[int, np.ndarray]:
    with wave.open(path, "rb") as w:
        ch = w.getnchannels()
        rate = w.getframerate()
        sw = w.getsampwidth()
        n = w.getnframes()
        raw = w.readframes(n)
    if sw != 2:
        raise SystemExit(f"{path}: expected 16-bit PCM, got {sw * 8}-bit")
    pcm = np.frombuffer(raw, dtype="<i2").reshape(-1, ch)
    return rate, pcm


def render(in_path: str, out_path: str, title: str | None, marks: list[float]) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from scipy.signal import spectrogram

    rate, pcm = read_wav(in_path)
    ch = pcm.shape[1]
    dur = pcm.shape[0] / rate
    t = np.arange(pcm.shape[0]) / rate

    rows = 1 + ch
    fig, axes = plt.subplots(rows, 1, figsize=(16, 3.2 * rows), sharex=True, constrained_layout=True)
    if rows == 1:
        axes = [axes]

    ax = axes[0]
    for c in range(ch):
        ax.plot(t, pcm[:, c] / 32768.0, lw=0.3, alpha=0.8, label=f"ch{c}")
    ax.set_ylim(-1, 1)
    ax.set_ylabel("amplitude")
    ax.legend(loc="upper right")
    ax.set_title(title or in_path)

    for c in range(ch):
        f, tt, sxx = spectrogram(pcm[:, c].astype(np.float32), fs=rate, nperseg=2048, noverlap=1024)
        db = 10 * np.log10(sxx + 1e-9)
        axes[1 + c].pcolormesh(tt, f / 1000.0, db, shading="auto", cmap="magma", vmin=db.max() - 90, vmax=db.max())
        axes[1 + c].set_ylabel(f"ch{c} kHz")
        axes[1 + c].set_ylim(0, 20)

    for a in axes:
        for m in marks:
            a.axvline(m, color="cyan", lw=0.8, ls="--")
    axes[-1].set_xlabel(f"seconds (total {dur:.2f}s @ {rate} Hz, {ch} ch)")
    fig.savefig(out_path, dpi=110)
    print(f"wrote {out_path} ({dur:.2f}s, {ch} ch, {rate} Hz)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("output")
    ap.add_argument("--title", default=None)
    ap.add_argument("--marks", default="")
    a = ap.parse_args()
    marks = [float(x) for x in a.marks.split(",") if x.strip()]
    render(a.input, a.output, a.title, marks)
    return 0


if __name__ == "__main__":
    sys.exit(main())
