"""NOAA MRMS surface precipitation type (PrecipFlag) at a point: what the radar network says is
reaching the ground, on a 0.01 degree (~1 km) grid, every 2 minutes. Public data,
https://mrms.ncep.noaa.gov/2D/PrecipFlag/.

Standard library only (it runs on the Pi, piped over ssh by weather_bridge.py):

  python3 mrms.py LAT LON [RADIUS]    -> one JSON line: {"time", "flag", "kinds", "precip"}

`flag` is the cell at the point; `kinds` every code within RADIUS cells (default 1, a 3x3 box);
`precip` is "snow" when any of them is snow, else "rain" when any is rain, else "none".
The GRIB2 reader covers this product only: a lat/lon grid (template 3.0) with PNG packing (5.41).
"""
from __future__ import annotations

import calendar
import gzip
import json
import re
import struct
import sys
import time
import urllib.error
import urllib.request
import zlib
from typing import Optional

BASE = "https://mrms.ncep.noaa.gov/2D/PrecipFlag/"
# PrecipFlag codes (MRMS v12): -3 no coverage, 0 none, 1 warm stratiform rain, 3 snow,
# 6 convective rain, 7 rain with hail, 10 cool stratiform rain, 91/96 tropical rain mixes.
SNOW = {3}
RAIN = {1, 6, 7, 10, 91, 96}


def latest_names(index: str, n: int = 3) -> list[str]:
    """The newest `n` files listed, newest first (the newest can be listed before it is served)."""
    names = sorted(set(re.findall(r"MRMS_PrecipFlag_00\.00_\d{8}-\d{6}\.grib2\.gz", index)))
    return names[::-1][:n]


def name_time(name: str) -> float:
    m = re.search(r"(\d{8})-(\d{6})", name)
    return float(calendar.timegm(time.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S"))) if m else 0.0


def _sections(b: bytes) -> dict:
    out, o = {}, 16
    while o < len(b) - 4 and b[o:o + 4] != b"7777":
        ln = struct.unpack(">I", b[o:o + 4])[0]
        out[b[o + 4]] = b[o:o + ln]
        o += ln
    return out


def _signed16(v: int) -> int:
    return -(v & 0x7FFF) if v & 0x8000 else v


def _png_rows(png: bytes, last_row: int, width: int) -> list[bytes]:
    """Rows 0..last_row of an 8/16-bit grayscale PNG, only their first `width` pixels (the
    filters reach left and up, never right, so a prefix decodes on its own)."""
    _, _, depth = struct.unpack(">IIB", png[16:25])
    o, idat = 8, b""
    while o < len(png):
        ln = struct.unpack(">I", png[o:o + 4])[0]
        if png[o + 4:o + 8] == b"IDAT":
            idat += png[o + 8:o + 8 + ln]
        o += 12 + ln
    w = struct.unpack(">I", png[16:20])[0]
    bpp = depth // 8
    stride, keep = w * bpp, width * bpp
    raw = zlib.decompressobj().decompress(idat, (last_row + 1) * (stride + 1))
    rows, prev = [], bytearray(keep)
    for y in range(last_row + 1):
        f = raw[y * (stride + 1)]
        line = bytearray(raw[y * (stride + 1) + 1:y * (stride + 1) + 1 + keep])
        if f == 1:
            for i in range(bpp, keep):
                line[i] = (line[i] + line[i - bpp]) & 255
        elif f == 2:
            for i in range(keep):
                line[i] = (line[i] + prev[i]) & 255
        elif f == 3:
            for i in range(keep):
                a = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 255
        elif f == 4:
            for i in range(keep):
                a = line[i - bpp] if i >= bpp else 0
                b_ = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b_ - c
                pa, pb, pc = abs(p - a), abs(p - b_), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b_ if pb <= pc else c)) & 255
        rows.append(bytes(line))
        prev = line
    return rows


def flags_at(grib: bytes, lat: float, lon: float, radius: int = 1) -> tuple[int, list[int]]:
    """(the code at the point, every code within `radius` cells)."""
    s = _sections(grib)
    g, r = s[3], s[5]
    la1 = struct.unpack(">i", g[46:50])[0] / 1e6
    lo1 = struct.unpack(">i", g[50:54])[0] / 1e6
    di = struct.unpack(">I", g[63:67])[0] / 1e6
    dj = struct.unpack(">I", g[67:71])[0] / 1e6
    ref = struct.unpack(">f", r[11:15])[0]
    e = _signed16(struct.unpack(">H", r[15:17])[0])
    d = _signed16(struct.unpack(">H", r[17:19])[0])
    depth = s[7][5:][24]
    bpp = depth // 8
    j = round((la1 - lat) / dj)
    i = round((lon % 360 - lo1) / di)
    rows = _png_rows(s[7][5:], j + radius, i + radius + 1)
    value = lambda jj, ii: round((ref + int.from_bytes(rows[jj][ii * bpp:(ii + 1) * bpp], "big") * 2 ** e) / 10 ** d)
    around = sorted({value(jj, ii) for jj in range(j - radius, j + radius + 1)
                     for ii in range(i - radius, i + radius + 1)})
    return value(j, i), around


def precip(kinds: list[int]) -> str:
    if any(k in SNOW for k in kinds):
        return "snow"
    if any(k in RAIN for k in kinds):
        return "rain"
    return "none"


def read(lat: float, lon: float, radius: int = 1, timeout: float = 20.0) -> dict:
    names = latest_names(urllib.request.urlopen(BASE, timeout=timeout).read().decode())
    if not names:
        raise RuntimeError("no PrecipFlag file listed")
    for name in names:
        try:
            grib = gzip.decompress(urllib.request.urlopen(BASE + name, timeout=timeout).read())
            break
        except urllib.error.HTTPError as ex:
            if ex.code != 404 or name == names[-1]:
                raise
    flag, kinds = flags_at(grib, lat, lon, radius)
    return {"time": name_time(name), "flag": flag, "kinds": kinds, "precip": precip(kinds)}


if __name__ == "__main__":
    a = sys.argv[1:]
    try:
        print(json.dumps(read(float(a[0]), float(a[1]), int(a[2]) if len(a) > 2 else 1)))
    except Exception as ex:   # one line the bridge can log
        print(json.dumps({"error": f"{type(ex).__name__}: {ex}"}))
        sys.exit(1)
