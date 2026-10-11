import os
import sqlite3
import struct
import tempfile
import time
import unittest

from watchdog.weather import (
    RAINING,
    RAINY,
    SNOWING,
    SNOWY,
    SUNNY,
    WK2_CONDITIONS,
    WeatherAppSource,
    WeatherFollower,
    WeatherReading,
    hourly_weather,
    make_source,
    wk2_current_condition,
    wk2_hourly,
    wk2_next_hour,
    forecast_condition_at,
    precipitation_at,
)


def _table(buf, fields_bytes):
    """Append a vtable + table whose fields (dict idx -> packed bytes, 4-byte multiples) are
    given; returns the table offset. uoffset fields are patched afterwards with _point()."""
    n = (max(fields_bytes) + 1) if fields_bytes else 0
    slots = [0] * n
    layout, off = [], 4
    for idx in sorted(fields_bytes):
        slots[idx] = off
        layout.append(fields_bytes[idx])
        off += len(fields_bytes[idx])
    vt = len(buf)
    buf += struct.pack("<HH", 4 + 2 * n, off) + struct.pack(f"<{n}H", *slots)
    while len(buf) % 4:
        buf += b"\0"
    tab = len(buf)
    buf += struct.pack("<i", tab - vt) + b"".join(layout)
    while len(buf) % 4:
        buf += b"\0"
    return tab


def _field_pos(buf, tab, idx):
    vt = tab - struct.unpack_from("<i", buf, tab)[0]
    return tab + struct.unpack_from("<H", buf, vt + 4 + 2 * idx)[0]


def _point(buf, pos, target):
    """Store a FlatBuffers uoffset (unsigned, forward) at pos -> target."""
    assert target > pos, "referenced objects must come after the reference"
    struct.pack_into("<I", buf, pos, target - pos)


def wk2_weather(code=None, as_of=1791447660, minutes=None, end=None, hours=None):
    """A WK2.Weather FlatBuffer: root field 1 -> CurrentWeatherData (asOf field 1, conditionCode
    field 6 unless None) and, when `minutes` is given, root field 4 -> NextHourForecast (end field
    4, minutes field 5: entries with startTime field 0 and precipitationIntensity field 3 when
    non-zero). Objects are laid out after whatever references them, as uoffsets require."""
    buf = bytearray(4)
    root_fields = {1: b"\0\0\0\0"}
    if minutes is not None:
        root_fields[4] = b"\0\0\0\0"
    if hours is not None:
        root_fields[3] = b"\0\0\0\0"
    root_tab = _table(buf, root_fields)
    struct.pack_into("<I", buf, 0, root_tab)

    cur_fields = {1: struct.pack("<I", as_of)}
    if code is not None:
        cur_fields[6] = struct.pack("<B", code) + b"\0\0\0"
    cur_tab = _table(buf, cur_fields)
    _point(buf, _field_pos(buf, root_tab, 1), cur_tab)

    if minutes is not None:
        nh_tab = _table(buf, {4: struct.pack("<I", int(end if end is not None else minutes[-1][0] + 60)),
                              5: b"\0\0\0\0"})
        _point(buf, _field_pos(buf, root_tab, 4), nh_tab)
        vec = len(buf)
        buf += struct.pack("<I", len(minutes)) + b"\0\0\0\0" * len(minutes)
        _point(buf, _field_pos(buf, nh_tab, 5), vec)
        for i, (start, inten) in enumerate(minutes):
            ef = {0: struct.pack("<I", int(start))}
            if inten:
                ef[3] = struct.pack("<f", inten)
            _point(buf, vec + 4 + 4 * i, _table(buf, ef))
    if hours is not None:
        hf_tab = _table(buf, {1: b"\0\0\0\0"})
        _point(buf, _field_pos(buf, root_tab, 3), hf_tab)
        vec = len(buf)
        buf += struct.pack("<I", len(hours)) + b"\0\0\0\0" * len(hours)
        _point(buf, _field_pos(buf, hf_tab, 1), vec)
        for i, (start, code) in enumerate(hours):
            _point(buf, vec + 4 + 4 * i, _table(buf, {0: struct.pack("<I", int(start)), 5: struct.pack("<B", code) + b"\0\0\0"}))
    return bytes(buf)


class TestWeather(unittest.TestCase):
    def test_state_engine(self):
        # Nothing falling: the condition decides.
        self.assertEqual(hourly_weather("Rain"), RAINY)
        self.assertEqual(hourly_weather("HeavyRain"), RAINY)
        self.assertEqual(hourly_weather("Drizzle"), RAINY)
        self.assertEqual(hourly_weather("Thunderstorms"), RAINY)
        self.assertEqual(hourly_weather("Snow"), SNOWY)
        self.assertEqual(hourly_weather("BlowingSnow"), SNOWY)
        self.assertEqual(hourly_weather("IsolatedThunderstorms"), SUNNY)
        self.assertEqual(hourly_weather("ScatteredThunderstorms"), SUNNY)
        self.assertEqual(hourly_weather("Cloudy"), SUNNY)
        self.assertEqual(hourly_weather("Clear"), SUNNY)
        # Precipitation falling now: rainy whatever the condition says, snowy for a snow condition.
        self.assertEqual(hourly_weather("Cloudy", precipitating=True), RAINY)
        self.assertEqual(hourly_weather("IsolatedThunderstorms", precipitating=True), RAINY)
        self.assertEqual(hourly_weather("Snow", precipitating=True), SNOWY)

    def test_precipitation_at_follows_the_minute_data(self):
        t0 = 1791743220.0   # 12:27
        m = ((t0, 0.0), (t0 + 60, 0.0), (t0 + 120, 0.5), (t0 + 180, 0.6), (t0 + 240, 0.0), (t0 + 300, 0.0))
        end = t0 + 360
        self.assertEqual(precipitation_at(m, end, t0 + 30), (False, None))      # before it starts
        self.assertEqual(precipitation_at(m, end, t0 + 120), (True, t0 + 240))  # starts at 12:29
        self.assertEqual(precipitation_at(m, end, t0 + 239), (True, t0 + 240))  # ...until 12:31
        self.assertEqual(precipitation_at(m, end, t0 + 240), (False, None))     # stopped
        self.assertEqual(precipitation_at(m, end, t0 - 1), (False, None))       # outside the data
        self.assertEqual(precipitation_at(m, end, end), (False, None))
        self.assertEqual(precipitation_at((), 0.0, t0), (False, None))
        # Falling through the end of the data: it stops when the data does.
        self.assertEqual(precipitation_at(((t0, 0.3),), t0 + 60, t0 + 10), (True, t0 + 60))

    def test_reading_returns_to_the_condition_when_the_rain_ends(self):
        # Cloudy before, light rain 12:29-12:31 per the chart, Cloudy after: sunny, rainy, sunny.
        t0 = 1791743220.0
        r = WeatherReading("Cloudy", t0, "t",
                           minutes=((t0, 0.0), (t0 + 60, 0.0), (t0 + 120, 0.5), (t0 + 180, 0.6), (t0 + 240, 0.0)),
                           minutes_end=t0 + 300)
        self.assertEqual([r.hourly_at(t) for t in (t0, t0 + 120, t0 + 200, t0 + 240, t0 + 299)],
                         [SUNNY, RAINY, RAINY, SUNNY, SUNNY])
        self.assertEqual(r.precipitation_at(t0 + 150), (True, t0 + 240))
        # A rain-type condition stays rainy even with no minute data.
        self.assertEqual(WeatherReading("Rain", t0, "t").hourly_at(t0 + 999), RAINY)

    def test_follower_tracks_the_rain_start_and_end(self):
        t0 = 1791743220.0
        r = WeatherReading("Cloudy", t0, "t", minutes=((t0, 0.0), (t0 + 60, 0.4), (t0 + 120, 0.0)), minutes_end=t0 + 180)
        f = WeatherFollower()
        f.on_reading(r.hourly_at(t0)); self.assertEqual(f.due(t0), SUNNY); f.started(); f.finished(SUNNY, True, t0)
        f.on_reading(r.hourly_at(t0 + 30)); self.assertIsNone(f.due(t0 + 30))         # same state: skipped
        f.on_reading(r.hourly_at(t0 + 60)); self.assertEqual(f.due(t0 + 60), RAINY)    # rain starts
        f.started(); f.finished(RAINY, True, t0 + 60)
        f.on_reading(r.hourly_at(t0 + 90)); self.assertIsNone(f.due(t0 + 90))
        f.on_reading(r.hourly_at(t0 + 120)); self.assertEqual(f.due(t0 + 120), SUNNY)  # rain ends: back to Cloudy's state

    def test_no_hold_from_previous_state(self):
        # The 16:00 case: rain, then IsolatedThunderstorms with nothing falling, picks sunny.
        readings = ["Rain", "IsolatedThunderstorms", "Cloudy", "Thunderstorms", "Snow", "Clear"]
        self.assertEqual([WeatherReading(c, 0, "t").hourly for c in readings],
                         [RAINY, SUNNY, SUNNY, RAINY, SNOWY, SUNNY])

    def test_changeover_sends_the_next_hours_forecast(self):
        h12 = 1791741600.0
        r = WeatherReading("Cloudy", h12 + 48 * 60, "t",
                           minutes=((h12 + 48 * 60, 0.0), (h12 + 98 * 60, 0.5), (h12 + 131 * 60, 0.0)),
                           minutes_end=h12 + 132 * 60,
                           hours=((h12, "Cloudy"), (h12 + 3600, "Drizzle"), (h12 + 7200, "Windy")))
        self.assertEqual(r.hourly_at(h12 + 48 * 60), SUNNY)
        self.assertEqual(r.hourly_at(h12 + 58 * 60), SUNNY)
        self.assertEqual(r.hourly_at(h12 + 59 * 60), RAINY)
        self.assertEqual(r.hourly_at(h12 + 3600), RAINY)
        self.assertEqual(r.hourly_at(h12 + 98 * 60), RAINY)
        self.assertEqual(r.hourly_at(h12 + 7200 - 60), RAINY)    # still raining at 1:59
        self.assertEqual(r.hourly_at(h12 + 131 * 60 + 60), SUNNY)  # rain over: 2PM Windy

    def test_follow_decides_the_hour_once_and_sends_the_forecast_to_the_next_hour_slot(self):
        # 12:48, Cloudy, 1 PM Drizzle. Local hour boundaries: build the epoch from local time.
        h12 = time.mktime((2026, 10, 11, 12, 0, 0, 0, 0, -1))
        r = WeatherReading("Cloudy", h12 + 48 * 60, "t",
                           hours=((h12, "Cloudy"), (h12 + 3600, "Drizzle"), (h12 + 7200, "Windy")))
        f = WeatherFollower()
        f.follow(r, h12 + 48 * 60)
        self.assertEqual(f.due(h12 + 48 * 60), SUNNY); self.assertFalse(f.for_next_hour)
        f.started(); f.finished(SUNNY, True, h12 + 48 * 60)
        f.follow(r, h12 + 58 * 60); self.assertIsNone(f.due(h12 + 58 * 60))
        # 12:59: 1 PM's forecast, for the next hour (the 12:00 track is not changed over).
        f.follow(r, h12 + 59 * 60)
        self.assertEqual(f.due(h12 + 59 * 60), RAINY); self.assertTrue(f.for_next_hour)
        f.started(); f.finished(RAINY, True, h12 + 59 * 60)
        # 1:00: the app refreshes and says Cloudy now, 1 PM row Cloudy: the hour stays rainy.
        r2 = WeatherReading("Cloudy", h12 + 3600, "t",
                            hours=((h12 + 3600, "Cloudy"), (h12 + 7200, "Windy")))
        for m in (0, 1, 10, 30, 58):
            f.follow(r2, h12 + 3600 + m * 60)
            self.assertIsNone(f.due(h12 + 3600 + m * 60), m)
            self.assertFalse(f.for_next_hour)
        # Rain falls at 1:20 and stops at 1:30: rainy either way, nothing sent.
        r3 = WeatherReading("Cloudy", h12 + 80 * 60, "t",
                            minutes=((h12 + 80 * 60, 0.4), (h12 + 90 * 60, 0.0)), minutes_end=h12 + 140 * 60,
                            hours=((h12 + 3600, "Cloudy"), (h12 + 7200, "Windy")))
        f.follow(r3, h12 + 85 * 60); self.assertIsNone(f.due(h12 + 85 * 60))
        f.follow(r3, h12 + 95 * 60); self.assertIsNone(f.due(h12 + 95 * 60))
        # 1:59: 2 PM Windy, for the next hour.
        f.follow(r3, h12 + 119 * 60)
        self.assertEqual(f.due(h12 + 119 * 60), SUNNY); self.assertTrue(f.for_next_hour)

    def test_follow_precipitation_within_the_hour_and_back(self):
        h = time.mktime((2026, 10, 11, 12, 0, 0, 0, 0, -1))
        r = WeatherReading("Cloudy", h + 20 * 60, "t",
                           minutes=((h + 20 * 60, 0.6), (h + 30 * 60, 0.0)), minutes_end=h + 80 * 60,
                           hours=((h, "Cloudy"), (h + 3600, "Clear")))
        f = WeatherFollower()
        f.follow(r, h + 10 * 60)
        self.assertEqual(f.due(h + 10 * 60), SUNNY); f.started(); f.finished(SUNNY, True, h + 10 * 60)
        f.follow(r, h + 20 * 60)
        self.assertEqual(f.due(h + 20 * 60), RAINY); self.assertFalse(f.for_next_hour)   # now, this hour
        f.started(); f.finished(RAINY, True, h + 20 * 60)
        f.follow(r, h + 25 * 60); self.assertIsNone(f.due(h + 25 * 60))
        f.follow(r, h + 30 * 60)
        self.assertEqual(f.due(h + 30 * 60), SUNNY); self.assertFalse(f.for_next_hour)   # back to the hour's weather
        f.started(); f.finished(SUNNY, True, h + 30 * 60)
        # A forecast revised mid-hour (12 PM row now Drizzle) changes nothing: the hour is decided.
        r2 = WeatherReading("Drizzle", h + 40 * 60, "t", hours=((h, "Drizzle"), (h + 3600, "Clear")))
        f.follow(r2, h + 40 * 60); self.assertIsNone(f.due(h + 40 * 60))

    def test_follow_started_mid_hour_uses_this_hours_forecast(self):
        h = time.mktime((2026, 10, 11, 12, 0, 0, 0, 0, -1))
        r = WeatherReading("Clear", h + 30 * 60, "t", hours=((h, "Rain"), (h + 3600, "Clear")))
        f = WeatherFollower()
        f.follow(r, h + 30 * 60)
        self.assertEqual(f.due(h + 30 * 60), RAINY); self.assertFalse(f.for_next_hour)

    def test_precipitation_now_overrides_the_hours_forecast(self):
        h = 1791741600.0
        r = WeatherReading("Cloudy", h, "t", minutes=((h, 0.6), (h + 600, 0.0)), minutes_end=h + 3600,
                           hours=((h, "Cloudy"), (h + 3600, "Clear")))
        self.assertEqual(r.hourly_at(h + 300), RAINY)
        self.assertEqual(r.hourly_at(h + 600), SUNNY)

    def test_decode_hourly_forecast(self):
        h = 1791741600
        b = wk2_weather(5, as_of=h, hours=[(h, 5), (h + 3600, 6), (h + 7200, 32)])
        self.assertEqual(wk2_hourly(b), ((float(h), "Cloudy"), (float(h + 3600), "Drizzle"), (float(h + 7200), "Windy")))
        self.assertEqual(wk2_hourly(wk2_weather(5)), ())
        self.assertEqual(forecast_condition_at(wk2_hourly(b), h + 3700), "Drizzle")
        self.assertIsNone(forecast_condition_at((), h))

    def test_decode_next_hour_minutes(self):
        t0 = 1791743220
        b = wk2_weather(5, as_of=t0, minutes=[(t0, 0.0), (t0 + 60, 0.579), (t0 + 120, 0.0)], end=t0 + 180)
        self.assertEqual(wk2_current_condition(b), ("Cloudy", float(t0)))
        minutes, end = wk2_next_hour(b)
        self.assertEqual(end, float(t0 + 180))
        self.assertEqual([(int(s), round(i, 3)) for s, i in minutes], [(t0, 0.0), (t0 + 60, 0.579), (t0 + 120, 0.0)])
        self.assertEqual(wk2_next_hour(wk2_weather(5)), ((), 0.0))   # no next-hour table

    def test_rain_and_snow_sets_are_apple_conditions(self):
        self.assertTrue(RAINING <= set(WK2_CONDITIONS))
        self.assertTrue(SNOWING <= set(WK2_CONDITIONS))
        self.assertFalse(RAINING & SNOWING)

    def test_no_hold_from_previous_state(self):
        # The 16:00 case: rain, then IsolatedThunderstorms, picks sunny.
        readings = ["Rain", "IsolatedThunderstorms", "Cloudy", "Thunderstorms", "Snow", "Clear"]
        self.assertEqual([WeatherReading(c, 0, "t").hourly for c in readings],
                         [RAINY, SUNNY, SUNNY, RAINY, SNOWY, SUNNY])

    def test_follower_skips_identical_weather(self):
        # Playing clear: IsolatedThunderstorms then Cloudy keep it sunny, so nothing is pushed.
        f = WeatherFollower()
        f.on_reading(WeatherReading("Clear", 0, "t").hourly)
        self.assertEqual(f.due(0), SUNNY); f.started(); f.finished(SUNNY, True, 0)
        f.on_reading(WeatherReading("IsolatedThunderstorms", 0, "t").hourly)
        self.assertIsNone(f.due(100))
        f.on_reading(WeatherReading("Cloudy", 0, "t").hourly)
        self.assertIsNone(f.due(100))
        f.on_reading(WeatherReading("Rain", 0, "t").hourly)
        self.assertEqual(f.due(100), RAINY)

    def test_follower_retries_until_acknowledged(self):
        f = WeatherFollower(retry_s=5)
        f.on_reading("sunny")
        self.assertEqual(f.due(0), "sunny"); f.started()
        self.assertIsNone(f.due(0))                       # one push at a time
        f.finished("sunny", False, 10)                    # failed: not applied
        self.assertIsNone(f.due(12))
        self.assertEqual(f.due(15), "sunny")              # retried
        f.started(); f.finished("sunny", True, 15)
        self.assertIsNone(f.due(100))
        f.on_reading("sunny")                             # identical to the acknowledged weather
        self.assertIsNone(f.due(100))

    def test_follower_manual_key_replaces_pending(self):
        f = WeatherFollower()
        f.on_reading("rainy")
        f.on_manual()
        self.assertIsNone(f.due(0))
        f.started(); f.finished("snowy", True, 0)         # the key's push
        f.on_reading("rainy")
        self.assertEqual(f.due(10), "rainy")

    def test_wk2_enum_values_checked_against_the_app(self):
        self.assertEqual(WK2_CONDITIONS[0], "Clear")
        self.assertEqual(WK2_CONDITIONS[5], "Cloudy")
        self.assertEqual(WK2_CONDITIONS[19], "MostlyClear")
        self.assertEqual(WK2_CONDITIONS[20], "MostlyCloudy")
        self.assertEqual(WK2_CONDITIONS[21], "PartlyCloudy")
        self.assertEqual(WK2_CONDITIONS[22], "Rain")
        self.assertEqual(WK2_CONDITIONS[26], "Snow")

    def test_decode_current_condition(self):
        self.assertEqual(wk2_current_condition(wk2_weather(22)), ("Rain", 1791447660.0))
        self.assertEqual(wk2_current_condition(wk2_weather(20))[0], "MostlyCloudy")
        # conditionCode omitted -> FlatBuffers default 0 (Clear)
        self.assertEqual(wk2_current_condition(wk2_weather(None))[0], "Clear")
        self.assertEqual(wk2_current_condition(wk2_weather(99))[0], "Code99")
        self.assertIsNone(wk2_current_condition(b"\x01\x00"))

    def test_weather_app_source_reads_newest_response_for_location(self):
        with tempfile.TemporaryDirectory() as mirror:
            cache = os.path.join(mirror, "com.apple.weather_Data/Library/Caches/com.apple.weather")
            os.makedirs(os.path.join(cache, "fsCachedData"))
            db = sqlite3.connect(os.path.join(cache, "Cache.db"))
            db.execute("create table cfurl_cache_response (entry_ID integer, time_stamp text, request_key text)")
            db.execute("create table cfurl_cache_receiver_data (entry_ID integer, isDataOnFS integer, receiver_data blob)")
            url = "https://weatherkit.apple.com/api/v2/weather/en-US/{}?dataSets=currentWeather,forecastHourly"
            rows = [
                (1, "2026-10-08 08:00:00", url.format("40.782/-111.872"), 0, wk2_weather(20)),   # older
                (2, "2026-10-08 08:21:00", url.format("40.782/-111.872"), 1, "AA-BB"),           # newest, on disk
                (3, "2026-10-08 08:30:00", url.format("47.604/-122.331"), 0, wk2_weather(26)),   # other location
            ]
            for eid, ts, key, on_fs, data in rows:
                db.execute("insert into cfurl_cache_response values (?,?,?)", (eid, ts, key))
                db.execute("insert into cfurl_cache_receiver_data values (?,?,?)", (eid, on_fs, data))
            db.commit()
            db.close()
            with open(os.path.join(cache, "fsCachedData", "AA-BB"), "wb") as f:
                f.write(wk2_weather(22))
            src = WeatherAppSource(mirror=mirror, latlon="40.782/-111.872")
            self.assertTrue(src.changed())
            self.assertFalse(src.changed())
            r = src.read()
            self.assertEqual((r.condition, r.hourly, r.source), ("Rain", "rainy", "weather-app"))

    def test_make_source(self):
        self.assertIsNone(make_source("none"))
        self.assertIsNone(make_source(""))
        self.assertIsInstance(make_source("weather-app"), WeatherAppSource)


if __name__ == "__main__":
    unittest.main()
