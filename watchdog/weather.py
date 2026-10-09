"""Weather sources for the hourly profile: what the sky is doing, as an Apple condition.

A WeatherSource reports the current condition as an Apple WeatherKit conditionCode name
("Rain", "MostlyCloudy", ...). The dashboard picks the hourly player's track set with
`next_hourly()`: from Sunny, Rain or Thunderstorms and Snow switch to Rainy/Snowy; Rainy and Snowy hold through
everything until Clear switches back to Sunny. A source only says what the condition is.

  WeatherAppSource  the macOS Weather app's own data: its cached WeatherKit responses,
                    mirrored by hourly/mac/WeatherWatch.swift (the only process with Full
                    Disk Access) whenever the app writes them. No WeatherKit license needed.

A WeatherKit REST source drops in by subclassing WeatherSource: currentWeather.conditionCode
in the REST JSON already uses these names, so `read()` returns it as-is and register it in
SOURCES.
"""
from __future__ import annotations

import glob
import os
import shutil
import sqlite3
import struct
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

# The only conditions that pick a track set. Every other condition (MostlyCloudy, Drizzle,
# Flurries, ...) leaves the player's weather as it is.
HOURLY_BY_CONDITION = {"Clear": "sunny", "Rain": "rainy", "Thunderstorms": "rainy", "Snow": "snowy"}


def hourly_weather(condition: str) -> Optional[str]:
    """'sunny' | 'rainy' | 'snowy' for Clear / Rain or Thunderstorms / Snow; None for anything else."""
    return HOURLY_BY_CONDITION.get(condition)


def next_hourly(current: Optional[str], condition: str) -> Optional[str]:
    """The track set to switch to, or None to keep `current`. Rainy and Snowy hold until the
    sky is Clear (then Sunny); only from Sunny (or unknown) do Rain/Snow switch."""
    target = hourly_weather(condition)
    if target is None or target == current:
        return None
    if current in ("rainy", "snowy") and target != "sunny":
        return None
    return target


@dataclass(frozen=True)
class WeatherReading:
    condition: str      # Apple conditionCode name, e.g. "MostlyCloudy"
    as_of: float        # epoch seconds the data describes
    source: str         # WeatherSource.name

    @property
    def hourly(self) -> Optional[str]:
        return hourly_weather(self.condition)

    def next_hourly(self, current: Optional[str]) -> Optional[str]:
        return next_hourly(current, self.condition)


class WeatherSource(ABC):
    name: str = "?"

    @abstractmethod
    def changed(self) -> bool:
        """Cheap check, called every dashboard tick: True when read() may return something new
        (always True on the first call)."""

    @abstractmethod
    def read(self) -> Optional[WeatherReading]:
        """The current condition, or None when the source has nothing usable."""


# WK2 (WeatherKit's FlatBuffer) WeatherCondition enum. The enum is Apple's WeatherCondition
# cases in alphabetical order with Clear moved to 0. Checked against the Weather app:
# 0 Clear, 5 Cloudy, 19 MostlyClear, 20 MostlyCloudy, 21 PartlyCloudy; and against the
# generated WK2 bindings: 6-8, 13-15, 18, 22, 26, 30, 32.
WK2_CONDITIONS = [
    "Clear", "Blizzard", "BlowingDust", "BlowingSnow", "Breezy", "Cloudy", "Drizzle", "Flurries",
    "Foggy", "FreezingDrizzle", "FreezingRain", "Frigid", "Hail", "Haze", "HeavyRain", "HeavySnow",
    "Hot", "Hurricane", "IsolatedThunderstorms", "MostlyClear", "MostlyCloudy", "PartlyCloudy",
    "Rain", "ScatteredThunderstorms", "Sleet", "Smoky", "Snow", "StrongStorms", "SunFlurries",
    "SunShowers", "Thunderstorms", "TropicalStorm", "Windy", "WintryMix",
]


def wk2_current_condition(b: bytes) -> Optional[tuple[str, float]]:
    """(condition name, asOf) from a WK2.Weather FlatBuffer (Weather.currentWeather is root
    field 1; CurrentWeatherData.asOf is field 1, conditionCode field 6, default Clear)."""
    u32 = lambda o: struct.unpack_from("<I", b, o)[0]
    i32 = lambda o: struct.unpack_from("<i", b, o)[0]
    u16 = lambda o: struct.unpack_from("<H", b, o)[0]

    def fields(t: int) -> dict[int, int]:
        vt = t - i32(t)
        n = (u16(vt) - 4) // 2
        return {i: t + u16(vt + 4 + 2 * i) for i in range(n) if u16(vt + 4 + 2 * i)}

    try:
        root = fields(u32(0))
        if 1 not in root:
            return None
        cur = fields(root[1] + u32(root[1]))
        code = b[cur[6]] if 6 in cur else 0
        as_of = float(u32(cur[1])) if 1 in cur else 0.0
    except (struct.error, IndexError):
        return None
    name = WK2_CONDITIONS[code] if code < len(WK2_CONDITIONS) else f"Code{code}"
    return name, as_of


class WeatherAppSource(WeatherSource):
    """The macOS Weather app's current condition for one of its locations, read from the
    app's URL cache (its own WeatherKit responses) in the WeatherWatch mirror."""
    name = "weather-app"

    def __init__(self, mirror: Optional[str] = None, latlon: Optional[str] = None) -> None:
        self.mirror = os.path.expanduser(mirror or os.environ.get(
            "KKFM_WEATHER_MIRROR", "~/Library/Caches/kkfm-weather-mirror"))
        # The app requests weather for each location as .../weather/<lang>/<lat>/<lon>?...
        self.latlon = latlon or os.environ.get("KKFM_WEATHER_LATLON", "40.782/-111.872")
        self.cache = os.path.join(self.mirror, "com.apple.weather_Data/Library/Caches/com.apple.weather")
        self._stamp: Optional[tuple] = None

    def changed(self) -> bool:
        stamp = tuple(
            (os.path.basename(p), os.stat(p).st_mtime_ns)
            for p in sorted(glob.glob(os.path.join(self.cache, "Cache.db*")))
            if os.path.exists(p)
        )
        if stamp == self._stamp:
            return False
        self._stamp = stamp
        return True

    def _latest_body(self) -> Optional[bytes]:
        with tempfile.TemporaryDirectory() as tmp:
            # Copy the db with its WAL so the newest rows are visible without touching the mirror.
            for f in glob.glob(os.path.join(self.cache, "Cache.db*")):
                shutil.copy(f, tmp)
            if not os.path.exists(os.path.join(tmp, "Cache.db")):
                return None
            db = sqlite3.connect(os.path.join(tmp, "Cache.db"))
            try:
                row = db.execute(
                    "select d.isDataOnFS, d.receiver_data from cfurl_cache_response r "
                    "join cfurl_cache_receiver_data d using(entry_ID) "
                    "where r.request_key like ? and r.request_key like '%currentWeather%' "
                    "order by r.time_stamp desc limit 1",
                    (f"%/weather/%/{self.latlon}?%",),
                ).fetchone()
            except sqlite3.Error:
                row = None
            finally:
                db.close()
        if not row:
            return None
        on_fs, data = row
        if not on_fs:
            return bytes(data)
        name = (data if isinstance(data, str) else bytes(data).decode()).strip("\0")
        try:
            with open(os.path.join(self.cache, "fsCachedData", name), "rb") as f:
                return f.read()
        except OSError:
            return None

    def read(self) -> Optional[WeatherReading]:
        body = self._latest_body()
        cc = wk2_current_condition(body) if body else None
        if not cc:
            return None
        return WeatherReading(condition=cc[0], as_of=cc[1], source=self.name)


SOURCES: dict[str, type[WeatherSource]] = {
    WeatherAppSource.name: WeatherAppSource,
}


def make_source(name: Optional[str]) -> Optional[WeatherSource]:
    """A WeatherSource by name ('none' or empty: no automatic weather)."""
    if not name or name == "none":
        return None
    return SOURCES[name]()
