"""Weather sources for the hourly profile: what the sky is doing, as an Apple condition.

A WeatherSource reports the current condition as an Apple WeatherKit conditionCode name
("Rain", "MostlyCloudy", ...). The hourly player's weather is a three-state engine driven by
that condition alone:

  rain-type precipitation (RAINING)  -> RAINY
  snow-type precipitation (SNOWING)  -> SNOWY
  any other condition                -> SUNNY   (the app reports nothing falling; this includes
                                                 IsolatedThunderstorms and ScatteredThunderstorms)

Every reading moves the engine to the state its condition belongs to; no state is held over
from the previous reading. The dashboard only pushes a state the player is not already in.

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

SUNNY = "sunny"
RAINY = "rainy"
SNOWY = "snowy"

# Conditions that report precipitation falling here. Scattered and isolated thunderstorms are
# storms in the area, not precipitation at the location, so they are in neither set.
RAINING = frozenset({
    "Drizzle", "FreezingDrizzle", "Rain", "HeavyRain", "FreezingRain", "SunShowers", "Hail",
    "Sleet", "WintryMix", "Thunderstorms", "StrongStorms", "TropicalStorm", "Hurricane",
})
SNOWING = frozenset({"Flurries", "SunFlurries", "Snow", "HeavySnow", "Blizzard", "BlowingSnow"})


def hourly_weather(condition: str) -> str:
    """The state a condition puts the engine in: RAINY while rain-type precipitation is reported,
    SNOWY while snow is, SUNNY while nothing is falling."""
    if condition in RAINING:
        return RAINY
    if condition in SNOWING:
        return SNOWY
    return SUNNY


class WeatherFollower:
    """Which weather to push to the player, and when. A reading whose weather equals the weather
    the player last acknowledged is skipped, so the track already playing is not faded and
    replayed. Only an acknowledged push counts as applied: a failed push stays pending and is
    retried after `retry_s`."""

    def __init__(self, retry_s: float = 5.0) -> None:
        self.retry_s = retry_s
        self.acked: Optional[str] = None     # last weather the player answered "ok" to
        self.pending: Optional[str] = None   # weather still to be applied
        self.inflight = False
        self.next_try = 0.0

    def on_reading(self, weather: Optional[str]) -> None:
        if weather is None:
            return
        self.pending = weather if weather != self.acked else None
        self.next_try = 0.0

    def on_manual(self) -> None:
        """A hand-set weather replaces whatever reading was still pending."""
        self.pending = None

    def due(self, now: float) -> Optional[str]:
        if self.pending is None or self.inflight or now < self.next_try:
            return None
        return self.pending

    def started(self) -> None:
        self.inflight = True

    def finished(self, weather: str, ok: bool, now: float) -> None:
        if ok:
            self.acked = weather
            if self.pending == weather:
                self.pending = None
        self.next_try = now + self.retry_s
        self.inflight = False


@dataclass(frozen=True)
class WeatherReading:
    condition: str      # Apple conditionCode name, e.g. "MostlyCloudy"
    as_of: float        # epoch seconds the data describes
    source: str         # WeatherSource.name

    @property
    def hourly(self) -> str:
        return hourly_weather(self.condition)


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
