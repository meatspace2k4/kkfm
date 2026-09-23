// Hourly BGM fork unit tests. Build from the repo root:
//   g++ -std=c++17 -O2 -I. -DKKFM_TEST_MODE hourly/tests_hourly.cpp StreamServer.cpp -o hourly_tests -pthread
// Real BGM files are read from HOURLY_SOUND_DIR (default hourly/Stream).
#include <iostream>
#include <vector>
#include <string>
#include <cmath>
#include <memory>
#include <stdexcept>
#include <fstream>

#ifndef KKFM_TEST_MODE
#define KKFM_TEST_MODE
#endif
#include "hourly_core.h"

const uint8_t assets_blob[16] = {0};

using namespace hourly;

static bool g_verbose = false;
struct TestResult { std::string name; bool passed; std::string error; };
static std::vector<TestResult> g_results;

#define ASSERT(cond) if (!(cond)) { throw std::runtime_error(std::string("Assertion failed: ") + #cond + " @" + std::to_string(__LINE__)); }
#define ASSERT_EQ(a, b) if (!((a) == (b))) { throw std::runtime_error(std::string("Assertion failed: ") + #a " == " #b " (" + std::to_string(a) + " != " + std::to_string(b) + ") @" + std::to_string(__LINE__)); }
#define ASSERT_STREQ(a, b) if (std::string(a) != std::string(b)) { throw std::runtime_error(std::string("Assertion failed: ") + #a " == " #b " (\"" + std::string(a) + "\" != \"" + std::string(b) + "\") @" + std::to_string(__LINE__)); }

static void run_test(const std::string& name, void (*func)()) {
    try {
        func();
        g_results.push_back({name, true, ""});
        if (g_verbose) std::cout << "✓ " << name << std::endl;
    } catch (const std::exception& e) {
        g_results.push_back({name, false, e.what()});
        std::cout << "✗ " << name << " (Error: " << e.what() << ")" << std::endl;
    }
}

static std::string sound_dir() {
    const char* d = std::getenv("HOURLY_SOUND_DIR");
    return (d && *d) ? d : "hourly/Stream";
}
static std::string out_dir() {
    const char* d = std::getenv("HOURLY_OUT_DIR");
    return (d && *d) ? d : "hourly/out";
}

static std::tm make_tm(int hour, int minute) {
    std::tm t{};
    t.tm_year = 126; t.tm_mon = 8; t.tm_mday = 20;
    t.tm_hour = hour; t.tm_min = minute;
    return t;
}

// ---------------------------------------------------------------------------
// Hour -> file
// ---------------------------------------------------------------------------
static void test_filename_all_hours_and_weathers() {
    const Weather ws[3] = {Weather::Sunny, Weather::Rainy, Weather::Snowy};
    const char* names[3] = {"Sunny", "Rainy", "Snowy"};
    for (int h = 0; h < 24; ++h) {
        for (int i = 0; i < 3; ++i) {
            char expect[64];
            std::snprintf(expect, sizeof(expect), "BGM_24Hour_%02d_%s.bwav", h, names[i]);
            ASSERT_STREQ(bgm_filename(h, ws[i]), expect);
        }
    }
    ASSERT_STREQ(bgm_filename(0, Weather::Sunny), "BGM_24Hour_00_Sunny.bwav");
    ASSERT_STREQ(bgm_filename(23, Weather::Snowy), "BGM_24Hour_23_Snowy.bwav");
}

static void test_hour_index_is_zero_based_local_hour() {
    ASSERT_EQ(hour_index(make_tm(0, 0)), 0);
    ASSERT_EQ(hour_index(make_tm(23, 59)), 23);
    ASSERT_EQ(hour_index(make_tm(12, 30)), 12);
}

static void test_hour_title() {
    ASSERT_STREQ(hour_title(0, Weather::Sunny), "12 AM (Sunny)");
    ASSERT_STREQ(hour_title(1, Weather::Rainy), "1 AM (Rainy)");
    ASSERT_STREQ(hour_title(12, Weather::Snowy), "12 PM (Snowy)");
    ASSERT_STREQ(hour_title(13, Weather::Sunny), "1 PM (Sunny)");
    ASSERT_STREQ(hour_title(23, Weather::Sunny), "11 PM (Sunny)");
}

// ---------------------------------------------------------------------------
// Weather
// ---------------------------------------------------------------------------
// WeatherKit forecastNextHour response: 60 minutes from `first` (epoch), chance per
// minute from `chance_at(i)`, and summary periods (offset minutes, condition).
static std::string iso_utc(std::time_t t) {
    std::tm g{};
    gmtime_r(&t, &g);
    char buf[32];
    std::snprintf(buf, sizeof(buf), "%04d-%02d-%02dT%02d:%02d:00Z", g.tm_year + 1900, g.tm_mon + 1, g.tm_mday, g.tm_hour, g.tm_min);
    return buf;
}
static std::string wk_json(std::time_t first, std::function<double(int)> chance_at,
                           std::vector<std::pair<int, std::string>> summary = {{0, "clear"}},
                           std::function<double(int)> intensity_at = [](int) { return 0.0; }) {
    std::string mins;
    for (int i = 0; i < 60; ++i) {
        if (i) mins += ",";
        char buf[160];
        std::snprintf(buf, sizeof(buf), "{\"startTime\":\"%s\",\"precipitationChance\":%.2f,\"precipitationIntensity\":%.2f}",
                      iso_utc(first + i * 60).c_str(), chance_at(i), intensity_at(i));
        mins += buf;
    }
    std::string sum;
    for (size_t k = 0; k < summary.size(); ++k) {
        if (k) sum += ",";
        sum += "{\"startTime\":\"" + iso_utc(first + summary[k].first * 60) + "\"";
        if (k + 1 < summary.size()) sum += ",\"endTime\":\"" + iso_utc(first + summary[k + 1].first * 60) + "\"";
        sum += ",\"condition\":\"" + summary[k].second + "\",\"precipitationChance\":0.0,\"precipitationIntensity\":0.0}";
    }
    return "{\"forecastNextHour\":{\"name\":\"NextHourForecast\",\"metadata\":{\"attributionURL\":\"x\",\"version\":1},"
           "\"summary\":[" + sum + "],\"forecastStart\":\"" + iso_utc(first) + "\",\"minutes\":[" + mins + "]}}";
}
static std::time_t local_epoch(int hour, int minute) { std::tm t = make_tm(hour, minute); t.tm_isdst = -1; return std::mktime(&t); }

static void test_next_block_start() {
    // One minute before each quarter targets the block that starts at that quarter.
    ASSERT(next_block_start(make_tm(12, 14)) == local_epoch(12, 15));
    ASSERT(next_block_start(make_tm(12, 29)) == local_epoch(12, 30));
    ASSERT(next_block_start(make_tm(12, 44)) == local_epoch(12, 45));
    ASSERT(next_block_start(make_tm(12, 59)) == local_epoch(13, 0));
    ASSERT(next_block_start(make_tm(12, 5))  == local_epoch(12, 15));   // startup mid-block
    ASSERT(next_block_start(make_tm(23, 59)) == local_epoch(23, 59) + 60);  // day wrap
}

static void test_weatherkit_next_block_not_current() {
    std::time_t t0 = local_epoch(12, 14);
    // Dry now, 15% chance only from 12:20: the next block [12:15, 12:30) is precipitation.
    std::string j = wk_json(t0, [](int i) { return i >= 6 ? 0.15 : 0.0; });
    WeatherObservation o;
    ASSERT(parse_weatherkit_block(j, local_epoch(12, 15), o));
    ASSERT(o.weather == Weather::Rainy);
    ASSERT(std::fabs(o.max_chance - 0.15) < 1e-9);
    // Any chance above zero counts, however small.
    j = wk_json(t0, [](int i) { return i == 10 ? 0.01 : 0.0; });
    ASSERT(parse_weatherkit_block(j, local_epoch(12, 15), o));
    ASSERT(o.weather == Weather::Rainy);
    // 0% for the whole block: Sunny, even if precipitation comes after it.
    j = wk_json(t0, [](int i) { return i >= 16 ? 0.9 : 0.0; });
    ASSERT(parse_weatherkit_block(j, local_epoch(12, 15), o));
    ASSERT(o.weather == Weather::Sunny);
    // Intensity without a chance value still counts.
    j = wk_json(t0, [](int) { return 0.0; }, {{0, "rain"}}, [](int i) { return i == 3 ? 0.4 : 0.0; });
    ASSERT(parse_weatherkit_block(j, local_epoch(12, 15), o));
    ASSERT(o.weather == Weather::Rainy);
}

static void test_weatherkit_snow_from_summary() {
    std::time_t t0 = local_epoch(12, 14);
    std::string j = wk_json(t0, [](int) { return 0.3; }, {{0, "clear"}, {5, "snow"}, {40, "clear"}});
    WeatherObservation o;
    ASSERT(parse_weatherkit_block(j, local_epoch(12, 15), o));
    ASSERT(o.weather == Weather::Snowy);
    ASSERT_STREQ(o.condition, "snow");
    j = wk_json(t0, [](int) { return 0.3; }, {{0, "precipitation"}});
    ASSERT(parse_weatherkit_block(j, local_epoch(12, 15), o));
    ASSERT(o.weather == Weather::Rainy);
}

static void test_weatherkit_garbage_keeps_previous() {
    WeatherObservation o;
    o.valid = true; o.weather = Weather::Rainy;
    ASSERT(!parse_weatherkit_block("", local_epoch(12, 15), o));
    ASSERT(!parse_weatherkit_block("<html>401</html>", local_epoch(12, 15), o));
    ASSERT(!parse_weatherkit_block("{\"reason\":\"NOT_ENABLED\"}", local_epoch(12, 15), o));
    // Forecast that does not cover the block.
    ASSERT(!parse_weatherkit_block(wk_json(local_epoch(14, 0), [](int) { return 0.5; }), local_epoch(12, 15), o));
    ASSERT(o.weather == Weather::Rainy);
}

static void test_weather_state_refresh_and_failure() {
    WeatherState ws(WeatherSource::WeatherKit);
    ASSERT(ws.current() == Weather::Sunny);                 // before any fetch
    ws.set_fetcher([](std::time_t now) { return wk_json(now, [](int i) { return i > 2 ? 0.2 : 0.0; }); });
    ASSERT(ws.refresh(make_tm(12, 14)));
    ASSERT(ws.current() == Weather::Rainy);
    ws.set_fetcher([](std::time_t) { return std::string(""); });
    ASSERT(!ws.refresh(make_tm(12, 29)));
    ASSERT(ws.current() == Weather::Rainy);                 // previous kept
    ws.set_override(Weather::Snowy);
    ASSERT(ws.current() == Weather::Snowy);
}

// Open-Meteo current: the last completed 15 minutes, rejected unless interval is 900 and fresh.
static std::string om_json(const char* time, int interval, double precip, double rain, double showers, double snow) {
    char buf[512];
    std::snprintf(buf, sizeof(buf),
        "{\"latitude\":40.746216,\"timezone\":\"America/Denver\",\"current_units\":{\"time\":\"iso8601\",\"interval\":\"seconds\"},"
        "\"current\":{\"time\":\"%s\",\"interval\":%d,\"precipitation\":%.2f,\"rain\":%.2f,\"showers\":%.2f,\"snowfall\":%.2f}}",
        time, interval, precip, rain, showers, snow);
    return buf;
}

static void test_open_meteo_last_15_minutes() {
    std::time_t now = local_epoch(12, 49);
    WeatherObservation o;
    ASSERT(parse_open_meteo_current(om_json("2026-09-20T12:45", 900, 0, 0, 0, 0), now, o));
    ASSERT(o.weather == Weather::Sunny);
    ASSERT_STREQ(o.time, "2026-09-20T12:45");
    ASSERT(parse_open_meteo_current(om_json("2026-09-20T12:45", 900, 0.1, 0.1, 0, 0), now, o));
    ASSERT(o.weather == Weather::Rainy);
    ASSERT(parse_open_meteo_current(om_json("2026-09-20T12:45", 900, 0.2, 0, 0.2, 0), now, o));
    ASSERT(o.weather == Weather::Rainy);                                        // showers
    ASSERT(parse_open_meteo_current(om_json("2026-09-20T12:45", 900, 0.5, 0, 0, 0.4), now, o));
    ASSERT(o.weather == Weather::Snowy);
}

static void test_open_meteo_rejects_hourly_and_stale() {
    std::time_t now = local_epoch(12, 49);
    WeatherObservation o;
    o.valid = true; o.weather = Weather::Rainy;
    ASSERT(!parse_open_meteo_current(om_json("2026-09-20T12:00", 3600, 0, 0, 0, 0), now, o));   // hourly
    ASSERT(!parse_open_meteo_current(om_json("2026-09-20T11:45", 900, 0, 0, 0, 0), now, o));    // an hour old
    ASSERT(!parse_open_meteo_current(om_json("2026-09-20T13:30", 900, 0, 0, 0, 0), now, o));    // future block
    ASSERT(!parse_open_meteo_current("{\"error\":true}", now, o));
    ASSERT(!parse_open_meteo_current("", now, o));
    ASSERT(o.weather == Weather::Rainy);
    ASSERT(open_meteo_url().find("current=precipitation,rain,showers,snowfall") != std::string::npos);
}

static void test_weather_state_open_meteo_and_manual() {
    WeatherState ws(WeatherSource::OpenMeteo);
    ws.set_fetcher([](std::time_t) { return om_json("2026-09-20T12:00", 900, 0.3, 0.3, 0, 0); });
    ASSERT(ws.refresh(make_tm(12, 14)));
    ASSERT(ws.current() == Weather::Rainy);
    ws.set_manual(Weather::Snowy);                          // dashboard '3'
    ASSERT(ws.current() == Weather::Snowy);
    ws.set_fetcher([](std::time_t) { return om_json("2026-09-20T12:15", 900, 0, 0, 0, 0); });
    ASSERT(ws.refresh(make_tm(12, 29)));                    // next scheduled refresh replaces it
    ASSERT(ws.current() == Weather::Sunny);
}

static void test_base64url_and_der_to_raw() {
    ASSERT_STREQ(base64url("f"), "Zg");
    ASSERT_STREQ(base64url("fo"), "Zm8");
    ASSERT_STREQ(base64url("foo"), "Zm9v");
    ASSERT_STREQ(base64url(std::string("\xfb\xff", 2)), "-_8");
    // r with a leading 0x00 pad (33 bytes), s short (31 bytes).
    std::string r(32, '\x81'), sv(31, '\x02');
    std::string der = std::string("\x30\x44\x02\x21\x00", 5) + r + std::string("\x02\x1f", 2) + sv;
    std::string raw;
    ASSERT(ecdsa_der_to_raw(der, raw));
    ASSERT_EQ((int)raw.size(), 64);
    ASSERT(raw.substr(0, 32) == r);
    ASSERT(raw.substr(32) == std::string(1, '\0') + sv);
}

// Real ES256 signature with a throwaway P-256 key; verified by openssl against the public key.
static void test_weatherkit_jwt_signature_verifies() {
    std::string dir = out_dir();
    std::string key = dir + "/wk_test_key.p8", pub = dir + "/wk_test_pub.pem";
    std::string gen = "openssl ecparam -name prime256v1 -genkey -noout 2>/dev/null | openssl pkcs8 -topk8 -nocrypt -out '" + key +
                      "' 2>/dev/null && openssl ec -in '" + key + "' -pubout -out '" + pub + "' 2>/dev/null";
    ASSERT(std::system(gen.c_str()) == 0);
    WeatherKitCreds c{key, "ABC123DEFG", "TEAM123456", "com.example.weatherkit"};
    std::string jwt = weatherkit_jwt(c, 1789000000);
    size_t d1 = jwt.find('.'), d2 = jwt.rfind('.');
    ASSERT(d1 != std::string::npos && d2 != d1);
    std::string input = jwt.substr(0, d2), sig = jwt.substr(d2 + 1);
    ASSERT_EQ((int)sig.size(), 86);                          // 64 raw bytes, base64url, no padding
    ASSERT_STREQ(jwt.substr(0, d1), base64url("{\"alg\":\"ES256\",\"kid\":\"ABC123DEFG\",\"id\":\"TEAM123456.com.example.weatherkit\"}"));
    // raw r||s back to DER, then openssl verifies.
    std::string raw;
    {
        std::string b64 = sig;
        for (auto& ch : b64) { if (ch == '-') ch = '+'; else if (ch == '_') ch = '/'; }
        while (b64.size() % 4) b64 += '=';
        std::string cmd = "printf %s '" + b64 + "' | openssl base64 -d -A";
        FILE* f = popen(cmd.c_str(), "r");
        char buf[128]; size_t n;
        while ((n = fread(buf, 1, sizeof(buf), f)) > 0) raw.append(buf, n);
        pclose(f);
    }
    ASSERT_EQ((int)raw.size(), 64);
    auto der_int = [](std::string v) {
        while (v.size() > 1 && v[0] == '\0' && !((uint8_t)v[1] & 0x80)) v.erase(0, 1);
        if ((uint8_t)v[0] & 0x80) v = std::string(1, '\0') + v;
        return std::string("\x02", 1) + (char)v.size() + v;
    };
    std::string body = der_int(raw.substr(0, 32)) + der_int(raw.substr(32));
    std::string der = std::string("\x30", 1) + (char)body.size() + body;
    std::ofstream(dir + "/wk_sig.der", std::ios::binary) << der;
    std::ofstream(dir + "/wk_input.txt", std::ios::binary) << input;
    std::string verify = "openssl dgst -sha256 -verify '" + pub + "' -signature '" + dir + "/wk_sig.der' '" + dir + "/wk_input.txt' 2>&1";
    FILE* f = popen(verify.c_str(), "r");
    std::string out; char buf[128]; size_t n;
    while ((n = fread(buf, 1, sizeof(buf), f)) > 0) out.append(buf, n);
    pclose(f);
    ASSERT(out.find("Verified OK") != std::string::npos);
}

static void test_refresh_slots_one_minute_early() {
    // Startup at 12:05 -> slot fixed; nothing due until :14.
    int last = weather_refresh_slot(make_tm(12, 5));
    ASSERT(!weather_refresh_due(make_tm(12, 13), last));
    ASSERT(weather_refresh_due(make_tm(12, 14), last));
    last = weather_refresh_slot(make_tm(12, 14));
    ASSERT(!weather_refresh_due(make_tm(12, 28), last));
    ASSERT(weather_refresh_due(make_tm(12, 29), last));
    last = weather_refresh_slot(make_tm(12, 29));
    ASSERT(!weather_refresh_due(make_tm(12, 43), last));
    ASSERT(weather_refresh_due(make_tm(12, 44), last));
    last = weather_refresh_slot(make_tm(12, 44));
    ASSERT(!weather_refresh_due(make_tm(12, 58), last));
    ASSERT(weather_refresh_due(make_tm(12, 59), last));
    last = weather_refresh_slot(make_tm(12, 59));
    // The top of the hour is NOT a refresh point.
    ASSERT(!weather_refresh_due(make_tm(13, 0), last));
    ASSERT(!weather_refresh_due(make_tm(13, 13), last));
    ASSERT(weather_refresh_due(make_tm(13, 14), last));
    // Day wrap.
    last = weather_refresh_slot(make_tm(23, 59));
    ASSERT(!weather_refresh_due(make_tm(23, 59), last));
}

static void test_weatherkit_url_targets_slc_proper() {
    std::string u = weatherkit_url();
    ASSERT(u.rfind("https://weatherkit.apple.com/api/v1/weather/en/40.7608/-111.8910?", 0) == 0);
    ASSERT(u.find("dataSets=forecastNextHour") != std::string::npos);
    ASSERT(u.find("timezone=America/Denver") != std::string::npos);
}

// ---------------------------------------------------------------------------
// Real files through the engine
// ---------------------------------------------------------------------------
static void test_all_72_files_parse_as_stereo() {
    const Weather ws[3] = {Weather::Sunny, Weather::Rainy, Weather::Snowy};
    int count = 0;
    for (int h = 0; h < 24; ++h) {
        for (Weather w : ws) {
            auto lb = load_bwav_file(sound_dir() + "/" + bgm_filename(h, w), hour_title(h, w), h);
            if (!lb) throw std::runtime_error("failed to load " + bgm_filename(h, w) + " from " + sound_dir());
            const AudioCtx& c = *lb->ctx;
            ASSERT_EQ(c.channels, 2);
            ASSERT_EQ((int)c.rate, 48000);
            ASSERT(c.loop_end == c.total_samples);
            ASSERT(c.loop_start < c.loop_end);
            ASSERT(c.loop_start != 0xFFFFFFFFu);
            ASSERT(c.loops == 1u);
            ASSERT_EQ((int)c.ch[0].start_offset, 192);
            ASSERT(c.ch[1].start_offset > c.ch[0].start_offset);
            ASSERT(c.ch[1].start_offset < lb->bytes.size());
            ASSERT(c.ch[0].coeffs.size() == 8 && c.ch[1].coeffs.size() == 8);
            ASSERT(c.ch[0].coeffs != c.ch[1].coeffs);   // really two streams
            ASSERT(c.raw_data == c.ch[0].raw_data);      // channel-0 view intact
            ASSERT_EQ(c.id, h);
            count++;
        }
    }
    ASSERT_EQ(count, 72);
}

static void test_mono_header_still_parses_and_rejects_bad() {
    // Synthesised mono header (radio layout): 1 channel, 48000 Hz, start 128.
    std::vector<uint8_t> d(256, 0);
    std::memcpy(d.data(), "BWAV", 4);
    d[4] = 0xFF; d[5] = 0xFE; d[14] = 1;
    uint32_t rate = 48000; std::memcpy(&d[16 + 4], &rate, 4);
    uint32_t total = 1000; std::memcpy(&d[16 + 8], &total, 4);
    uint32_t start = 128; std::memcpy(&d[16 + 52], &start, 4);
    auto ctx = BWAVParser::parse_bytes(d.data(), d.size(), "mono", nullptr, 1);
    ASSERT(ctx != nullptr);
    ASSERT_EQ(ctx->channels, 1);
    ASSERT_EQ((int)ctx->start_offset, 128);
    d[14] = 3;   // 3 channels: unsupported
    ASSERT(BWAVParser::parse_bytes(d.data(), d.size(), "bad", nullptr, 1) == nullptr);
    d[14] = 2;   // 2 channels but channel 1 start offset (0) is fine, timing differs (total 0 != 1000)
    ASSERT(BWAVParser::parse_bytes(d.data(), d.size(), "bad2", nullptr, 1) == nullptr);
}

struct DecodeRun {
    std::vector<int16_t> pcm;   // interleaved L/R
    size_t frames = 0;
    size_t loops = 0;
    bool done = false;
};

// Drive the real Mixer/Track exactly as the player does; arm the engine fade
// once loops_hit() reaches `arm_after_loops` (0 = arm before playback).
static DecodeRun decode_with_engine(const std::shared_ptr<AudioCtx>& ctx, size_t arm_after_loops, size_t max_frames) {
    Mixer mixer;
    auto tr = mixer.add(ctx, VOL_DEFAULT, 0.0f, false, 0.0f, 0.0f);
    auto dec = tr->get_decoder();
    DecodeRun r;
    std::vector<int16_t> block(MIX_BLOCK_FRAMES * 2, 0);
    bool armed = false;
    if (arm_after_loops == 0) { dec->arm_fade_at_next_loop(FADE_PLAY_DURATION, FADE_TIME_DURATION); armed = true; }
    while (!tr->done() && r.frames < max_frames) {
        if (!armed && dec->loops_hit() >= arm_after_loops) {
            dec->arm_fade_at_next_loop(FADE_PLAY_DURATION, FADE_TIME_DURATION);
            armed = true;
        }
        mixer.mix_block(block.data(), MIX_BLOCK_FRAMES);
        r.pcm.insert(r.pcm.end(), block.begin(), block.end());
        r.frames += MIX_BLOCK_FRAMES;
    }
    r.loops = dec->loops_hit();
    r.done = tr->done();
    return r;
}

static size_t trailing_zero_frames(const std::vector<int16_t>& pcm) {
    size_t frames = pcm.size() / 2;
    size_t z = 0;
    for (size_t i = frames; i > 0; --i) {
        if (pcm[(i - 1) * 2] == 0 && pcm[(i - 1) * 2 + 1] == 0) z++; else break;
    }
    return z;
}

static double rms(const std::vector<int16_t>& pcm, size_t frame_from, size_t frame_to) {
    double acc = 0; size_t n = 0;
    for (size_t i = frame_from; i < frame_to && i * 2 + 1 < pcm.size(); ++i) {
        acc += (double)pcm[i * 2] * pcm[i * 2] + (double)pcm[i * 2 + 1] * pcm[i * 2 + 1];
        n += 2;
    }
    return n ? std::sqrt(acc / n) : 0.0;
}

static void write_wav(const std::string& path, const std::vector<int16_t>& pcm, uint32_t rate) {
    WavHeader h;
    h.sample_rate = rate;
    h.byte_rate = rate * CHANNELS * sizeof(int16_t);
    h.data_size = (uint32_t)(pcm.size() * sizeof(int16_t));
    h.file_size = 36 + h.data_size;
    std::ofstream f(path, std::ios::binary);
    f.write(reinterpret_cast<const char*>(&h), sizeof(h));
    f.write(reinterpret_cast<const char*>(pcm.data()), (std::streamsize)(pcm.size() * sizeof(int16_t)));
}

static std::shared_ptr<LoadedBwav> load_hour(int h, Weather w) {
    auto lb = load_bwav_file(sound_dir() + "/" + bgm_filename(h, w), hour_title(h, w), h);
    if (!lb) throw std::runtime_error("failed to load " + bgm_filename(h, w));
    return lb;
}

// Hour 00 Sunny: frame 0 -> loop end -> (fade armed) jump -> 10 s play -> 7 s fade -> done.
static void test_loop_once_then_radio_fade() {
    auto lb = load_hour(0, Weather::Sunny);
    const AudioCtx& c = *lb->ctx;
    const size_t play_lim = (size_t)(FADE_PLAY_DURATION * c.rate);
    const size_t fade_lim = (size_t)(FADE_TIME_DURATION * c.rate);
    const size_t expect = (size_t)c.loop_end + play_lim + fade_lim;

    DecodeRun r = decode_with_engine(lb->ctx, 0, expect + 48000 * 5);
    ASSERT(r.done);
    ASSERT_EQ(r.loops, (size_t)1);

    // Exact sample count: everything after `expect` is zero padding, nothing before it is missing.
    size_t audible = r.frames - trailing_zero_frames(r.pcm);
    if (g_verbose) std::cout << "  frames=" << r.frames << " audible=" << audible << " expect=" << expect << std::endl;
    ASSERT(audible <= expect);
    ASSERT(audible + 4800 >= expect);   // the fade's last 0.1 s rounds to 0; allow only that
    // Sample `expect-1` scaled by 1/fade_lim must be 0; sample at expect - 48000 (1 s before end) is still audible.
    ASSERT(rms(r.pcm, expect - 48000, expect - 47000) > 0.0);

    // Starts at frame 0 with real audio in the first second (not silence, not a delay).
    ASSERT(rms(r.pcm, 0, 48000) > 100.0);
    // Genuinely stereo: L and R differ on most frames of the first pass.
    size_t diff = 0;
    for (size_t i = 0; i < (size_t)c.loop_end; ++i) if (r.pcm[i * 2] != r.pcm[i * 2 + 1]) diff++;
    ASSERT(diff > (size_t)c.loop_end / 4);

    // Full level right up to the loop end and through the 10 s post-loop play.
    ASSERT(rms(r.pcm, c.loop_end - 48000, c.loop_end) > 100.0);
    ASSERT(rms(r.pcm, c.loop_end + play_lim - 48000, c.loop_end + play_lim) > 100.0);

    // Fade window: the post-jump audio replays the loop region, so the unfaded reference for
    // fade chunk k is the first pass at loop_start + play_lim + k*0.5 s. The ratio faded/reference
    // must track the engine's linear ramp (1 - t/7 s), chunk by chunk, down to ~0.
    const size_t fade_from = c.loop_end + play_lim;
    const size_t ref_from = (size_t)c.loop_start + play_lim;
    for (size_t k = 0; k < 14; ++k) {
        double faded = rms(r.pcm, fade_from + k * 24000, fade_from + (k + 1) * 24000);
        double ref = rms(r.pcm, ref_from + k * 24000, ref_from + (k + 1) * 24000);
        double expect_gain = 1.0 - ((double)k + 0.5) * 24000.0 / (double)fade_lim;
        double got_gain = ref > 0 ? faded / ref : 0.0;
        if (g_verbose) std::cout << "  fade chunk " << k << " ref=" << ref << " faded=" << faded
                                 << " gain=" << got_gain << " expect~" << expect_gain << std::endl;
        ASSERT(ref > 50.0);
        ASSERT(std::fabs(got_gain - expect_gain) < 0.06);
    }
    ASSERT(rms(r.pcm, fade_from + fade_lim - 4800, fade_from + fade_lim) < 60.0);

    // The loop jump is continuous: the audio right after loop_end equals the audio at loop_start
    // (same ADPCM data, decoder re-seeded from the header loop history).
    size_t mism = 0; int maxd = 0;
    for (size_t i = 0; i < 48000; ++i) {
        for (int ch = 0; ch < 2; ++ch) {
            int a = r.pcm[(c.loop_end + i) * 2 + ch];
            int b = r.pcm[(c.loop_start + i) * 2 + ch];
            int d = std::abs(a - b);
            if (d) mism++;
            if (d > maxd) maxd = d;
        }
    }
    if (g_verbose) std::cout << "  loop-jump mismatches=" << mism << " maxdiff=" << maxd << std::endl;
    ASSERT(maxd < 64);   // converges to the first pass within a few samples

    write_wav(out_dir() + "/loop_once_fade.wav", std::vector<int16_t>(r.pcm.begin(), r.pcm.begin() + (expect + 14) * 2), c.rate);
}

// Hour 09 (shortest): loops twice untouched, then the fade engages at the THIRD loop hit.
static void test_loops_indefinitely_until_armed() {
    auto lb = load_hour(9, Weather::Rainy);
    const AudioCtx& c = *lb->ctx;
    const size_t play_lim = (size_t)(FADE_PLAY_DURATION * c.rate);
    const size_t fade_lim = (size_t)(FADE_TIME_DURATION * c.rate);
    const size_t loop_len = (size_t)c.loop_end - c.loop_start;
    const size_t expect = (size_t)c.loop_end + 2 * loop_len + play_lim + fade_lim;

    DecodeRun r = decode_with_engine(lb->ctx, 2, expect + 48000 * 5);
    ASSERT(r.done);
    ASSERT_EQ(r.loops, (size_t)3);
    size_t audible = r.frames - trailing_zero_frames(r.pcm);
    if (g_verbose) std::cout << "  frames=" << r.frames << " audible=" << audible << " expect=" << expect << std::endl;
    ASSERT(audible <= expect);
    ASSERT(audible + 4800 >= expect);
    // Second and third passes are at full level (no fade before arming).
    ASSERT(rms(r.pcm, c.loop_end + loop_len - 48000, c.loop_end + loop_len) > 100.0);
    ASSERT(rms(r.pcm, c.loop_end + 2 * loop_len - 48000, c.loop_end + 2 * loop_len) > 100.0);
}

// Without arming, the decoder never exhausts (bounded run of 3 full loops).
static void test_never_ends_without_arming() {
    auto lb = load_hour(9, Weather::Sunny);
    const AudioCtx& c = *lb->ctx;
    const size_t loop_len = (size_t)c.loop_end - c.loop_start;
    const size_t bound = (size_t)c.loop_end + 3 * loop_len + 1000;
    Mixer mixer;
    auto tr = mixer.add(lb->ctx, VOL_DEFAULT, 0.0f, false, 0.0f, 0.0f);
    std::vector<int16_t> block(MIX_BLOCK_FRAMES * 2, 0);
    size_t frames = 0;
    while (frames < bound) { mixer.mix_block(block.data(), MIX_BLOCK_FRAMES); frames += MIX_BLOCK_FRAMES; }
    ASSERT(!tr->done());
    // Hits at loop_end, +1, +2 and +3 loop lengths -> 4 jumps within the bound.
    ASSERT_EQ(tr->get_decoder()->loops_hit(), (size_t)4);
    ASSERT(!tr->get_decoder()->is_fading());
}

// Mono context through the stereo mixer: L == R sample for sample (radio parity).
static void test_mono_ctx_mixes_l_equals_r() {
    auto lb = load_hour(0, Weather::Sunny);
    // Build a mono view of channel 0 of a real file.
    auto mono = std::make_shared<AudioCtx>(*lb->ctx);
    mono->channels = 1;
    Mixer mixer;
    auto tr = mixer.add(mono, VOL_DEFAULT, 0.0f, false, 0.0f, 0.0f);
    std::vector<int16_t> block(MIX_BLOCK_FRAMES * 2, 0);
    size_t nonzero = 0;
    for (int k = 0; k < 200; ++k) {
        mixer.mix_block(block.data(), MIX_BLOCK_FRAMES);
        for (int i = 0; i < MIX_BLOCK_FRAMES; ++i) {
            ASSERT_EQ(block[i * 2], block[i * 2 + 1]);
            if (block[i * 2] != 0) nonzero++;
        }
    }
    ASSERT(nonzero > 1000);
    ASSERT(!tr->done());
}

// Player logic with an injected clock: the pick follows the hour, the fade is armed only on an
// hour change, and a weather refresh never touches the playing track.
static void test_player_changeover_only_on_hour() {
    Mixer mixer;
    WeatherState ws(WeatherSource::WeatherKit);
    int fetches = 0;
    ws.set_fetcher([&](std::time_t now) { fetches++; return wk_json(now, [](int) { return 0.0; }); });
    PlayerOptions o;
    o.sound_dir = sound_dir();
    o.heartbeat_s = 0;
    o.gap_s = 0.05f;
    o.test_max_tracks = 1;
    HourlyPlayer p(mixer, ws, o);

    // Clock: hour 9 for the first ~1.5 s, then hour 10 (minute 14 so a refresh is due too).
    auto t0 = std::chrono::steady_clock::now();
    p.set_clock([&]() {
        double el = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
        return el < 1.5 ? make_tm(9, 5) : make_tm(10, 14);
    });

    // Pump the mixer from a thread at real-time pace so the track advances.
    std::atomic<bool> pumping{true};
    std::vector<int16_t> block(MIX_BLOCK_FRAMES * 2, 0);
    std::thread pump([&]() {
        auto next = std::chrono::steady_clock::now();
        while (pumping) {
            mixer.mix_block(block.data(), MIX_BLOCK_FRAMES);
            next += std::chrono::microseconds((int64_t)MIX_BLOCK_FRAMES * 1000000 / 48000 / 40); // 40x realtime
            std::this_thread::sleep_until(next);
        }
    });

    // Run the player until the hour flips and the fade is armed.
    std::thread runner([&]() { p.run(); });
    auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(20);
    while (std::chrono::steady_clock::now() < deadline && p.arms.load() == 0) {
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
    // The clock says hour 10, the fade is armed, and the hour-9 track is still playing.
    ASSERT_EQ(p.arms.load(), 1);
    ASSERT_EQ(p.track_ends.load(), 0);
    ASSERT_EQ(p.picks.load(), 1);
    ASSERT_EQ(p.last_pick_hour.load(), 9);
    ASSERT_EQ(p.last_arm_from.load(), 9);
    ASSERT_EQ(p.last_arm_to.load(), 10);

    // It ends only when the engine finishes the loop pass + fade (test_max_tracks=1 exits after that).
    deadline = std::chrono::steady_clock::now() + std::chrono::seconds(30);
    while (std::chrono::steady_clock::now() < deadline && p.track_ends.load() == 0) {
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
    ASSERT_EQ(p.track_ends.load(), 1);
    runner.join();
    pumping = false;
    pump.join();

    ASSERT_EQ(p.picks.load(), 1);            // never re-picked on the wall clock
    ASSERT_EQ(p.arms.load(), 1);
    ASSERT(fetches >= 2);                    // startup + the :14 slot; refresh never switched anything
}

// Weather changes in the next block while the hour stays: the fade is armed at the :14 refresh,
// the track finishes its loop + fade, and the same hour's rainy file is picked next.
static void test_player_changeover_on_weather_change() {
    Mixer mixer;
    WeatherState ws(WeatherSource::WeatherKit);
    PlayerOptions o;
    o.sound_dir = sound_dir();
    o.heartbeat_s = 0;
    o.gap_s = 0.05f;
    o.test_max_tracks = 2;
    HourlyPlayer p(mixer, ws, o);

    // Startup 09:05 fetch is dry (Sunny pick); the 09:14 refresh sees a 15% chance for [09:15, 09:30).
    int calls = 0;
    ws.set_fetcher([&](std::time_t now) {
        calls++;
        return calls == 1 ? wk_json(now, [](int) { return 0.0; })
                          : wk_json(now, [](int) { return 0.15; });
    });
    auto t0 = std::chrono::steady_clock::now();
    p.set_clock([&]() {
        double el = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
        return el < 1.5 ? make_tm(9, 5) : make_tm(9, 14);
    });

    std::atomic<bool> pumping{true};
    std::vector<int16_t> block(MIX_BLOCK_FRAMES * 2, 0);
    std::thread pump([&]() {
        auto next = std::chrono::steady_clock::now();
        while (pumping) {
            mixer.mix_block(block.data(), MIX_BLOCK_FRAMES);
            next += std::chrono::microseconds((int64_t)MIX_BLOCK_FRAMES * 1000000 / 48000 / 40);
            std::this_thread::sleep_until(next);
        }
    });
    std::thread runner([&]() { p.run(); });

    auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(20);
    while (std::chrono::steady_clock::now() < deadline && p.arms.load() == 0)
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    ASSERT_EQ(p.arms.load(), 1);
    ASSERT_EQ(p.track_ends.load(), 0);            // not cut
    ASSERT_EQ(p.last_arm_from.load(), 9);
    ASSERT_EQ(p.last_arm_to.load(), 9);           // same hour: weather changeover
    ASSERT_EQ(p.last_pick_weather.load(), (int)Weather::Sunny);

    deadline = std::chrono::steady_clock::now() + std::chrono::seconds(40);
    while (std::chrono::steady_clock::now() < deadline && p.picks.load() < 2)
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    p.request_stop();
    runner.join();
    pumping = false;
    pump.join();

    ASSERT_EQ(p.track_ends.load(), 1);
    ASSERT_EQ(p.picks.load(), 2);
    ASSERT_EQ(p.last_pick_hour.load(), 9);
    ASSERT_EQ(p.last_pick_weather.load(), (int)Weather::Rainy);
}

static std::string http_fetch_path(uint16_t port, const std::string& path, size_t want_bytes);

// Skip ('r'): the hour and weather stay, the playing track is not cut; its fade is armed
// and the next track is picked when it ends.
static void test_player_skip_queues_next_track() {
    Mixer mixer;
    WeatherState ws(WeatherSource::OpenMeteo);
    ws.set_override(Weather::Sunny);
    PlayerOptions o;
    o.sound_dir = sound_dir();
    o.heartbeat_s = 0;
    o.gap_s = 0.05f;
    o.test_max_tracks = 2;
    HourlyPlayer p(mixer, ws, o);
    p.set_clock([]() { return make_tm(9, 5); });

    std::atomic<bool> pumping{true};
    std::vector<int16_t> block(MIX_BLOCK_FRAMES * 2, 0);
    std::thread pump([&]() {
        auto next = std::chrono::steady_clock::now();
        while (pumping) {
            mixer.mix_block(block.data(), MIX_BLOCK_FRAMES);
            next += std::chrono::microseconds((int64_t)MIX_BLOCK_FRAMES * 1000000 / 48000 / 40);
            std::this_thread::sleep_until(next);
        }
    });
    std::thread runner([&]() { p.run(); });
    auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(10);
    while (std::chrono::steady_clock::now() < deadline && p.picks.load() < 1)
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    std::this_thread::sleep_for(std::chrono::milliseconds(300));
    ASSERT_EQ(p.arms.load(), 0);
    p.request_skip();
    deadline = std::chrono::steady_clock::now() + std::chrono::seconds(5);
    while (std::chrono::steady_clock::now() < deadline && p.arms.load() == 0)
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    ASSERT_EQ(p.arms.load(), 1);
    ASSERT_EQ(p.last_arm_to.load(), 9);
    // Fades out now: the track ends after the 7 s fade (40x realtime), not at its loop end.
    auto armed_at = std::chrono::steady_clock::now();
    deadline = armed_at + std::chrono::seconds(10);
    while (std::chrono::steady_clock::now() < deadline && p.track_ends.load() == 0)
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    ASSERT_EQ(p.track_ends.load(), 1);
    ASSERT(std::chrono::steady_clock::now() - armed_at < std::chrono::seconds(3));
    deadline = std::chrono::steady_clock::now() + std::chrono::seconds(40);
    while (std::chrono::steady_clock::now() < deadline && p.picks.load() < 2)
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    p.request_stop();
    runner.join();
    pumping = false;
    pump.join();
    ASSERT_EQ(p.track_ends.load(), 1);
    ASSERT_EQ(p.picks.load(), 2);
    ASSERT_EQ(p.last_pick_hour.load(), 9);
}

// /control route: handler reply when set, 404 when not (radio).
static void test_stream_server_control_route() {
    PCMBuffer buffer;
    StreamServer server(buffer);
    const uint16_t port = 18792;
    ASSERT(server.start(port, true));
    std::string r = http_fetch_path(port, "/control/skip", 400);
    ASSERT(r.rfind("HTTP/1.1 404", 0) == 0);
    std::string seen;
    server.set_control_handler([&](const std::string& path) { seen = path; return std::string("ok: done\n"); });
    r = http_fetch_path(port, "/control/weather/rainy", 400);
    ASSERT(r.rfind("HTTP/1.1 200", 0) == 0);
    ASSERT(r.find("ok: done") != std::string::npos);
    ASSERT_STREQ(seen, "/control/weather/rainy");
    server.stop();
}

// StreamServer: libav clients (ffplay/ffmpeg, the dashboard player) probe "Range: bytes=0-",
// receive the 44-byte WAV header, then resume with "Range: bytes=44-". The resume must be a
// 206 that continues with PCM, not a 200 that restarts at the RIFF header.
static std::string http_fetch(uint16_t port, const std::string& range, size_t want_bytes) {
    int fd = ::socket(AF_INET6, SOCK_STREAM, 0);
    if (fd < 0) throw std::runtime_error("socket");
    sockaddr_in6 addr{};
    addr.sin6_family = AF_INET6;
    addr.sin6_addr = in6addr_loopback;
    addr.sin6_port = htons(port);
    if (::connect(fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) < 0) { ::close(fd); throw std::runtime_error("connect"); }
    std::string req = "GET /stream HTTP/1.1\r\nHost: localhost\r\nUser-Agent: test\r\n";
    if (!range.empty()) req += "Range: " + range + "\r\n";
    req += "\r\n";
    ::send(fd, req.data(), req.size(), 0);
    std::string out;
    char buf[4096];
    timeval tv{2, 0};
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    while (out.size() < want_bytes) {
        ssize_t n = ::recv(fd, buf, sizeof(buf), 0);
        if (n <= 0) break;
        out.append(buf, (size_t)n);
    }
    ::close(fd);
    return out;
}

static std::string http_fetch_path(uint16_t port, const std::string& path, size_t want_bytes) {
    int fd = ::socket(AF_INET6, SOCK_STREAM, 0);
    if (fd < 0) throw std::runtime_error("socket");
    sockaddr_in6 addr{};
    addr.sin6_family = AF_INET6;
    addr.sin6_addr = in6addr_loopback;
    addr.sin6_port = htons(port);
    if (::connect(fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) < 0) { ::close(fd); throw std::runtime_error("connect"); }
    std::string req = "GET " + path + " HTTP/1.1\r\nHost: localhost\r\n\r\n";
    ::send(fd, req.data(), req.size(), 0);
    std::string out;
    char buf[1024];
    timeval tv{2, 0};
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    while (out.size() < want_bytes) {
        ssize_t n = ::recv(fd, buf, sizeof(buf), 0);
        if (n <= 0) break;
        out.append(buf, (size_t)n);
    }
    ::close(fd);
    return out;
}

static void test_stream_server_range_resume() {
    PCMBuffer buffer;
    StreamServer server(buffer);
    const uint16_t port = 18791;
    ASSERT(server.start(port, true));
    std::atomic<bool> feeding{true};
    std::thread feeder([&]() {
        std::vector<int16_t> block(MIX_BLOCK_FRAMES * 2);
        int16_t v = 1000;
        while (feeding) {
            for (auto& s : block) s = v;
            buffer.write(block.data(), MIX_BLOCK_FRAMES);
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
        }
    });
    std::this_thread::sleep_for(std::chrono::milliseconds(50));

    // Plain GET: 200 + RIFF header.
    std::string full = http_fetch(port, "", 2000);
    ASSERT(full.rfind("HTTP/1.1 200", 0) == 0);
    size_t body = full.find("\r\n\r\n");
    ASSERT(body != std::string::npos);
    ASSERT(full.compare(body + 4, 4, "RIFF") == 0);

    // Probe: 206 with exactly the header.
    std::string probe = http_fetch(port, "bytes=0-", 400);
    ASSERT(probe.rfind("HTTP/1.1 206", 0) == 0);
    ASSERT(probe.find("Content-Length: 44\r\n") != std::string::npos);

    // Resume from 44: 206, Content-Range from 44, body is PCM (no RIFF), non-empty.
    std::string resume = http_fetch(port, "bytes=44-", 4000);
    ASSERT(resume.rfind("HTTP/1.1 206", 0) == 0);
    ASSERT(resume.find("Content-Range: bytes 44-") != std::string::npos);
    body = resume.find("\r\n\r\n");
    ASSERT(body != std::string::npos);
    ASSERT(resume.compare(body + 4, 4, "RIFF") != 0);
    ASSERT(resume.size() - (body + 4) >= 1000);
    int16_t s0; std::memcpy(&s0, resume.data() + body + 4, 2);
    ASSERT_EQ((int)s0, 1000);

    feeding = false;
    feeder.join();
    server.stop();
}

#ifdef HOURLY_USE_BLOB
// Packed build: every hour/weather resolves inside the blob, parses as the same
// stereo header as the file, and decodes audio straight from rodata.
static void test_packed_assets_cover_all_hours_and_decode() {
    ASSERT_EQ((int)hourly_assets_count, 72);
    const Weather ws[3] = {Weather::Sunny, Weather::Rainy, Weather::Snowy};
    for (int h = 0; h < 24; ++h) {
        for (Weather w : ws) {
            const HourlyAsset* a = find_hourly_asset(h, w);
            ASSERT(a != nullptr);
            ASSERT_STREQ(a->name, bgm_filename(h, w));
            auto lb = load_bwav_blob(h, w, hour_title(h, w));
            ASSERT(lb != nullptr);
            ASSERT_EQ(lb->ctx->channels, 2);
            ASSERT_EQ((int)lb->ctx->rate, 48000);
            ASSERT(lb->ctx->loop_end == lb->ctx->total_samples);
            ASSERT(lb->bytes.empty());   // no copy: points into the blob
            ASSERT(lb->ctx->raw_data >= hourly_assets_blob + a->offset);
            ASSERT(lb->ctx->raw_data < hourly_assets_blob + a->offset + a->size);
        }
    }
    // Blob decode == file decode for the first second of hour 00 Sunny, when the files are present.
    auto blob = load_bwav_blob(0, Weather::Sunny, "x");
    Mixer m1; auto t1 = m1.add(blob->ctx, VOL_DEFAULT, 0.0f, false, 0.0f, 0.0f);
    std::vector<int16_t> b1(MIX_BLOCK_FRAMES * 2), b2(MIX_BLOCK_FRAMES * 2);
    auto file = load_bwav_file(sound_dir() + "/" + bgm_filename(0, Weather::Sunny), "x", 0);
    size_t nonzero = 0;
    if (file) {
        Mixer m2; auto t2 = m2.add(file->ctx, VOL_DEFAULT, 0.0f, false, 0.0f, 0.0f);
        for (int k = 0; k < 110; ++k) {
            m1.mix_block(b1.data(), MIX_BLOCK_FRAMES);
            m2.mix_block(b2.data(), MIX_BLOCK_FRAMES);
            ASSERT(b1 == b2);
            for (auto s : b1) if (s) nonzero++;
        }
    } else {
        for (int k = 0; k < 110; ++k) { m1.mix_block(b1.data(), MIX_BLOCK_FRAMES); for (auto s : b1) if (s) nonzero++; }
    }
    ASSERT(nonzero > 1000);
    ASSERT(!t1->done());
}
#endif

int main(int argc, char** argv) {
    for (int i = 1; i < argc; ++i) if (std::string(argv[i]) == "-v") g_verbose = true;
#ifdef HOURLY_USE_BLOB
    setenv("ACNH_DEBUG", "false", 1);
    run_test("packed_assets_cover_all_hours_and_decode", test_packed_assets_cover_all_hours_and_decode);
    { int failed = 0; for (const auto& r : g_results) if (!r.passed) failed++;
      std::cout << "\n" << g_results.size() << " run, " << failed << " failed" << std::endl;
      return failed > 0 ? 1 : 0; }
#endif
    // Log buffer prints straight to stdout when not a TTY; keep test output readable.
    setenv("ACNH_DEBUG", "false", 1);

    run_test("filename_all_hours_and_weathers", test_filename_all_hours_and_weathers);
    run_test("hour_index_is_zero_based_local_hour", test_hour_index_is_zero_based_local_hour);
    run_test("hour_title", test_hour_title);
    run_test("next_block_start", test_next_block_start);
    run_test("weatherkit_next_block_not_current", test_weatherkit_next_block_not_current);
    run_test("weatherkit_snow_from_summary", test_weatherkit_snow_from_summary);
    run_test("weatherkit_garbage_keeps_previous", test_weatherkit_garbage_keeps_previous);
    run_test("open_meteo_last_15_minutes", test_open_meteo_last_15_minutes);
    run_test("open_meteo_rejects_hourly_and_stale", test_open_meteo_rejects_hourly_and_stale);
    run_test("weather_state_open_meteo_and_manual", test_weather_state_open_meteo_and_manual);
    run_test("base64url_and_der_to_raw", test_base64url_and_der_to_raw);
    run_test("weatherkit_jwt_signature_verifies", test_weatherkit_jwt_signature_verifies);
    run_test("weather_state_refresh_and_failure", test_weather_state_refresh_and_failure);
    run_test("refresh_slots_one_minute_early", test_refresh_slots_one_minute_early);
    run_test("weatherkit_url_targets_slc_proper", test_weatherkit_url_targets_slc_proper);
    run_test("all_72_files_parse_as_stereo", test_all_72_files_parse_as_stereo);
    run_test("mono_header_still_parses_and_rejects_bad", test_mono_header_still_parses_and_rejects_bad);
    run_test("loop_once_then_radio_fade", test_loop_once_then_radio_fade);
    run_test("loops_indefinitely_until_armed", test_loops_indefinitely_until_armed);
    run_test("never_ends_without_arming", test_never_ends_without_arming);
    run_test("mono_ctx_mixes_l_equals_r", test_mono_ctx_mixes_l_equals_r);
    run_test("player_changeover_only_on_hour", test_player_changeover_only_on_hour);
    run_test("player_changeover_on_weather_change", test_player_changeover_on_weather_change);
    run_test("player_skip_queues_next_track", test_player_skip_queues_next_track);
    run_test("stream_server_range_resume", test_stream_server_range_resume);
    run_test("stream_server_control_route", test_stream_server_control_route);

    int failed = 0;
    for (const auto& r : g_results) if (!r.passed) failed++;
    std::cout << "\n" << g_results.size() << " run, " << failed << " failed" << std::endl;
    return failed > 0 ? 1 : 0;
}
