from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .anomalies import Anomaly, AnomalyCode, Severity
from .bwav import BwavError, parse_bwav_header


ASSET_META_RE = re.compile(
    r'\{\s*(?P<offset>\d+)\s*,\s*(?P<size>\d+)\s*,\s*(?P<bar>[0-9.]+)f\s*,\s*(?P<id>-?\d+)\s*,\s*"(?P<name>[^"]+)"'
)


@dataclass(frozen=True)
class AssetMeta:
    offset: int
    size: int
    name: str


def iter_assets_from_header(assets_h_path: str) -> Iterable[AssetMeta]:
    txt = Path(assets_h_path).read_text(errors="ignore")
    for m in ASSET_META_RE.finditer(txt):
        yield AssetMeta(
            offset=int(m.group("offset")),
            size=int(m.group("size")),
            name=m.group("name"),
        )


def check_assets_h_sizes(assets_h_path: str, ts: float) -> list[Anomaly]:
    out: list[Anomaly] = []
    for meta in iter_assets_from_header(assets_h_path):
        if meta.size == 0:
            out.append(
                Anomaly(
                    code=AnomalyCode.ASSETS_H_HAS_ZERO_SIZE,
                    severity=Severity.ERROR,
                    ts=ts,
                    name=meta.name,
                    message=f"assets.h has size=0 for {meta.name}",
                )
            )
    return out


def check_assets_blob_ranges(assets_h_path: str, assets_blob_path: str, ts: float) -> list[Anomaly]:
    out: list[Anomaly] = []
    blob_size = os.path.getsize(assets_blob_path)
    for meta in iter_assets_from_header(assets_h_path):
        if meta.size <= 0:
            continue
        end = meta.offset + meta.size
        if meta.offset < 0 or end > blob_size:
            out.append(
                Anomaly(
                    code=AnomalyCode.ASSETS_BLOB_RANGE_ERROR,
                    severity=Severity.ERROR,
                    ts=ts,
                    name=meta.name,
                    message=f"assets blob range error: {meta.name} offset={meta.offset} size={meta.size} blob_size={blob_size}",
                )
            )
    return out


def scan_stream_bwav_dir(stream_dir: str, ts: float) -> list[Anomaly]:
    """Scan filesystem files for truncation/header issues."""
    out: list[Anomaly] = []
    p = Path(stream_dir)
    if not p.exists():
        return out
    for fp in sorted(p.glob("*.bwav")):
        try:
            st = fp.stat()
        except FileNotFoundError:
            continue
        if st.st_size == 0:
            out.append(
                Anomaly(
                    code=AnomalyCode.FILE_ZERO_BYTES,
                    severity=Severity.ERROR,
                    ts=ts,
                    name=fp.name,
                    message=f"0-byte file: {fp}",
                )
            )
            continue
        if st.st_size < 96:
            out.append(
                Anomaly(
                    code=AnomalyCode.FILE_TOO_SMALL,
                    severity=Severity.ERROR,
                    ts=ts,
                    name=fp.name,
                    message=f"too-small file (<96 bytes): {fp} size={st.st_size}",
                )
            )
            continue
        try:
            # Only read the first 256 bytes for header validation.
            with fp.open("rb") as f:
                head = f.read(256)
            parse_bwav_header(head if len(head) >= 96 else head)
        except BwavError as e:
            out.append(
                Anomaly(
                    code=AnomalyCode.FILE_BAD_HEADER,
                    severity=Severity.ERROR,
                    ts=ts,
                    name=fp.name,
                    message=f"bad File header: {fp} :: {e}",
                )
            )
    return out


