"""Weather sources for the hourly profile: is precipitation falling right now, and until when.

A WeatherSource reports the Weather app's precipitation chart (the panel with the Now / 10m /
20m ... bars: one bar per minute, its height the precipitation intensity, no bar when nothing is
falling) plus the current condition name. The hourly player's weather is a three-state engine
evaluated at the current time:

  the bar at "Now" is above zero, snow condition  -> SNOWY
  the bar at "Now" is above zero, otherwise       -> RAINY
  no bar at "Now"                                 -> the condition's state: RAINING -> RAINY,
                                                     SNOWING -> SNOWY, anything else -> SUNNY

The chart also says when the precipitation stops: the first later minute with no bar. The
monitor sends the player RAINY when the bars start and, when that end minute is reached, the
condition's state again (Cloudy before the rain -> SUNNY after it), with no new fetch needed.
The dashboard only pushes a state the player is not already in.

  WeatherAppSource  the macOS Weather app's own data: its cached WeatherKit responses,
                    mirrored by hourly/mac/WeatherWatch.swift (the only process with Full
                    Disk Access) whenever the app writes them. No WeatherKit license needed.
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

# With nothing falling, the next hour's forecast is sent this long before the hour.
CHANGEOVER_LEAD_S = 60.0

# Conditions that report precipitation falling here. Scattered and isolated thunderstorms are
# storms in the area, not precipitation at the location, so they are in neither set.
RAINING = frozenset({
    "Drizzle", "FreezingDrizzle", "Rain", "HeavyRain", "FreezingRain", "SunShowers", "Hail",
    "Sleet", "WintryMix", "Thunderstorms", "StrongStorms", "TropicalStorm", "Hurricane",
})
SNOWING = frozenset({"Flurries", "SunFlurries", "Snow", "HeavySnow", "Blizzard", "BlowingSnow"})


def hourly_weather(condition: str, precipitating: bool = False) -> str:
    """The engine's state: RAINY while precipitation falls (or a rain-type condition is reported),
    SNOWY when it is snow, SUNNY while nothing is falling."""
    if condition in SNOWING:
        return SNOWY
    if precipitating or condition in RAINING:
        return RAINY
    return SUNNY


# The app's precipitation chart as bars: (minute start, intensity mm/h); a bar holds until the
# next bar's start, the last until `end`.
Chart = tuple[tuple[float, float], ...]
Minutes = Chart


def precipitation_at(minutes: Chart, end: float, now: float) -> tuple[bool, Optional[float]]:
    """(falling now, when it stops). Falling when the bar covering `now` is above zero; it stops
    at the first later bar at zero (or when the chart ends). Outside the chart's span nothing is
    known, so nothing is falling."""
    if not minutes or now < minutes[0][0] or now >= end:
        return False, None
    idx = 0
    for i, (start, _) in enumerate(minutes):
        if start <= now:
            idx = i
        else:
            break
    if minutes[idx][1] <= 0.0:
        return False, None
    for start, intensity in minutes[idx + 1:]:
        if intensity <= 0.0:
            return True, start
    return True, end


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
    condition: str              # Apple conditionCode name, e.g. "MostlyCloudy"
    as_of: float                # epoch seconds the data describes
    source: str                 # WeatherSource.name
    minutes: Chart = ()         # the precipitation chart's bars (minute start, mm/h)
    minutes_end: float = 0.0    # when the chart ends
    hours: Hours = ()           # the hourly forecast (hour start, condition)

    def precipitation_at(self, now: float) -> tuple[bool, Optional[float]]:
        return precipitation_at(self.minutes, self.minutes_end, now)

    def hourly_at(self, now: float) -> str:
        falling, _ = self.precipitation_at(now)
        if falling:
            return hourly_weather(self.condition, True)
        cond = forecast_condition_at(self.hours, now + CHANGEOVER_LEAD_S)
        if cond is None:
            cond = forecast_condition_at(self.hours, now)
        return hourly_weather(cond if cond is not None else self.condition)

    @property
    def hourly(self) -> str:
        """The state at the time the data describes."""
        return self.hourly_at(self.as_of)


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


# WK2.Weather root fields: currentWeather 1, the precipitation chart 4 (WeatherKit calls it
# forecastNextHour; the app draws it as the Now / 10m / 20m bars). Chart table: start (3),
# end (4), minutes (5). A minute entry: startTime (0), precipitationChance (2),
# precipitationIntensity (3, mm/h, absent when 0). Checked against the app's cache on
# 2026-10-11 12:27: intensity 0.579 from 12:32 while the app showed "Light Rain" bars, no
# intensity field in the dry minutes before and after.
WK2_ROOT_CURRENT = 1
WK2_ROOT_NEXT_HOUR = 4
WK2_NEXT_HOUR_END = 4
WK2_NEXT_HOUR_MINUTES = 5
WK2_MINUTE_START = 0
WK2_MINUTE_INTENSITY = 3


def _wk2_fields(b: bytes, t: int) -> dict[int, int]:
    i32 = lambda o: struct.unpack_from("<i", b, o)[0]
    u16 = lambda o: struct.unpack_from("<H", b, o)[0]
    vt = t - i32(t)
    n = (u16(vt) - 4) // 2
    return {i: t + u16(vt + 4 + 2 * i) for i in range(n) if u16(vt + 4 + 2 * i)}


def wk2_next_hour(b: bytes) -> tuple[Chart, float]:
    """(chart bars, chart end) from the precipitation chart table; ((), 0.0) when absent."""
    u32 = lambda o: struct.unpack_from("<I", b, o)[0]
    f32 = lambda o: struct.unpack_from("<f", b, o)[0]
    try:
        root = _wk2_fields(b, u32(0))
        if WK2_ROOT_NEXT_HOUR not in root:
            return (), 0.0
        nh = _wk2_fields(b, root[WK2_ROOT_NEXT_HOUR] + u32(root[WK2_ROOT_NEXT_HOUR]))
        end = float(u32(nh[WK2_NEXT_HOUR_END])) if WK2_NEXT_HOUR_END in nh else 0.0
        if WK2_NEXT_HOUR_MINUTES not in nh:
            return (), end
        v = nh[WK2_NEXT_HOUR_MINUTES] + u32(nh[WK2_NEXT_HOUR_MINUTES])
        out = []
        for i in range(u32(v)):
            e = v + 4 + 4 * i
            m = _wk2_fields(b, e + u32(e))
            start = float(u32(m[WK2_MINUTE_START])) if WK2_MINUTE_START in m else 0.0
            inten = float(f32(m[WK2_MINUTE_INTENSITY])) if WK2_MINUTE_INTENSITY in m else 0.0
            out.append((start, inten))
    except (struct.error, IndexError, KeyError):
        return (), 0.0
    return tuple(out), end


# Hourly forecast: WK2.Weather root field 3, its vector field 1; an hour entry: forecastStart (0),
# conditionCode (5). Checked against the app's Conditions row on 2026-10-11 12:48.
WK2_ROOT_HOURLY = 3
WK2_HOURLY_HOURS = 1
WK2_HOUR_START = 0
WK2_HOUR_CONDITION = 5

Hours = tuple[tuple[float, str], ...]   # (hour start, condition name)


def wk2_hourly(b: bytes) -> Hours:
    u32 = lambda o: struct.unpack_from("<I", b, o)[0]
    try:
        root = _wk2_fields(b, u32(0))
        if WK2_ROOT_HOURLY not in root:
            return ()
        hf = _wk2_fields(b, root[WK2_ROOT_HOURLY] + u32(root[WK2_ROOT_HOURLY]))
        if WK2_HOURLY_HOURS not in hf:
            return ()
        v = hf[WK2_HOURLY_HOURS] + u32(hf[WK2_HOURLY_HOURS])
        out = []
        for i in range(u32(v)):
            e = v + 4 + 4 * i
            h = _wk2_fields(b, e + u32(e))
            start = float(u32(h[WK2_HOUR_START])) if WK2_HOUR_START in h else 0.0
            code = b[h[WK2_HOUR_CONDITION]] if WK2_HOUR_CONDITION in h else 0
            out.append((start, WK2_CONDITIONS[code] if code < len(WK2_CONDITIONS) else f"Code{code}"))
    except (struct.error, IndexError, KeyError):
        return ()
    return tuple(out)


def forecast_condition_at(hours: Hours, t: float) -> Optional[str]:
    for start, cond in hours:
        if start <= t < start + 3600:
            return cond
    return None


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
        minutes, end = wk2_next_hour(body)
        return WeatherReading(condition=cc[0], as_of=cc[1], source=self.name, minutes=minutes, minutes_end=end,
                              hours=wk2_hourly(body))


SOURCES: dict[str, type[WeatherSource]] = {
    WeatherAppSource.name: WeatherAppSource,
}


def make_source(name: Optional[str]) -> Optional[WeatherSource]:
    """A WeatherSource by name ('none' or empty: no automatic weather)."""
    if not name or name == "none":
        return None
    return SOURCES[name]()
