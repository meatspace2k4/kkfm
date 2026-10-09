import os
import sqlite3
import struct
import tempfile
import unittest

from watchdog.weather import (
    WK2_CONDITIONS,
    WeatherAppSource,
    hourly_weather,
    make_source,
    next_hourly,
    wk2_current_condition,
)


def wk2_weather(code=None, as_of=1791447660):
    """A minimal WK2.Weather FlatBuffer: root field 1 -> CurrentWeatherData with asOf (field 1)
    and, unless None, conditionCode (field 6)."""
    # CurrentWeatherData table at 32: vtable at 16 (8 slots: fields 0..6 used up to 6).
    cur_fields = {1: struct.pack("<I", as_of)}
    if code is not None:
        cur_fields[6] = struct.pack("<B", code) + b"\0\0\0"
    buf = bytearray(4)                                # root offset, patched below
    # root vtable + table
    root_vt = len(buf)
    buf += struct.pack("<HHHH", 8, 8, 0, 4)           # vt size 8, table size 8, field0 absent, field1 @4
    root_tab = len(buf)
    buf += struct.pack("<i", root_tab - root_vt)
    ref_pos = len(buf)
    buf += b"\0\0\0\0"                                # uoffset to CurrentWeatherData
    cur_vt = len(buf)
    slots = [0] * 7
    layout, off = [], 4
    for idx in sorted(cur_fields):
        slots[idx] = off
        layout.append(cur_fields[idx])
        off += len(cur_fields[idx])
    buf += struct.pack("<HH", 4 + 2 * len(slots), off) + struct.pack(f"<{len(slots)}H", *slots)
    while len(buf) % 4:
        buf += b"\0"
    cur_tab = len(buf)
    buf += struct.pack("<i", cur_tab - cur_vt) + b"".join(layout)
    struct.pack_into("<I", buf, 0, root_tab)
    struct.pack_into("<I", buf, ref_pos, cur_tab - ref_pos)
    return bytes(buf)


class TestWeather(unittest.TestCase):
    def test_only_clear_rain_snow_pick_a_track_set(self):
        self.assertEqual(hourly_weather("Clear"), "sunny")
        self.assertEqual(hourly_weather("Rain"), "rainy")
        self.assertEqual(hourly_weather("Thunderstorms"), "rainy")
        self.assertEqual(hourly_weather("IsolatedThunderstorms"), "rainy")
        self.assertEqual(hourly_weather("Snow"), "snowy")
        for other in ("MostlyCloudy", "MostlyClear", "Drizzle", "HeavyRain", "Flurries", "Windy"):
            self.assertIsNone(hourly_weather(other), other)

    def test_rainy_and_snowy_hold_until_clear(self):
        # From Sunny (or unknown at startup) Rain/Snow switch; Clear while sunny is no change.
        self.assertEqual(next_hourly("sunny", "Rain"), "rainy")
        self.assertEqual(next_hourly("sunny", "Snow"), "snowy")
        self.assertEqual(next_hourly(None, "Rain"), "rainy")
        self.assertEqual(next_hourly("sunny", "Thunderstorms"), "rainy")
        self.assertEqual(next_hourly(None, "Thunderstorms"), "rainy")
        self.assertEqual(next_hourly("sunny", "IsolatedThunderstorms"), "rainy")
        self.assertEqual(next_hourly(None, "IsolatedThunderstorms"), "rainy")
        self.assertEqual(next_hourly(None, "Clear"), "sunny")
        self.assertIsNone(next_hourly("sunny", "Clear"))
        self.assertIsNone(next_hourly("sunny", "MostlyCloudy"))
        for held in ("rainy", "snowy"):
            for cond in ("Cloudy", "MostlyCloudy", "MostlyClear", "Drizzle", "Flurries", "Rain", "Thunderstorms", "IsolatedThunderstorms", "Snow"):
                self.assertIsNone(next_hourly(held, cond), (held, cond))
            self.assertEqual(next_hourly(held, "Clear"), "sunny")

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
