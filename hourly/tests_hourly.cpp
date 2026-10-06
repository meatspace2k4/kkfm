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
static void test_wmo_mapping() {
    ASSERT(weather_from_wmo(0, 0, 0, 0) == Weather::Sunny);    // clear
    ASSERT(weather_from_wmo(3, 0, 0, 0) == Weather::Sunny);    // overcast
    ASSERT(weather_from_wmo(45, 0, 0, 0) == Weather::Sunny);   // fog
    ASSERT(weather_from_wmo(51, 0, 0, 0) == Weather::Rainy);   // drizzle
    ASSERT(weather_from_wmo(61, 0, 0, 0) == Weather::Rainy);   // rain
    ASSERT(weather_from_wmo(67, 0, 0, 0) == Weather::Rainy);   // freezing rain
    ASSERT(weather_from_wmo(80, 0, 0, 0) == Weather::Rainy);   // showers
    ASSERT(weather_from_wmo(95, 0, 0, 0) == Weather::Rainy);   // thunderstorm
    ASSERT(weather_from_wmo(99, 0, 0, 0) == Weather::Rainy);
    ASSERT(weather_from_wmo(71, 0, 0, 0) == Weather::Snowy);   // snow
    ASSERT(weather_from_wmo(77, 0, 0, 0) == Weather::Snowy);   // snow grains
    ASSERT(weather_from_wmo(85, 0, 0, 0) == Weather::Snowy);   // snow showers
    ASSERT(weather_from_wmo(86, 0, 0, 0) == Weather::Snowy);
    // Measured precipitation overrides a stale code.
    ASSERT(weather_from_wmo(0, 0.0, 0.3, 0.0) == Weather::Rainy);
    ASSERT(weather_from_wmo(0, 0.0, 0.0, 0.2) == Weather::Rainy);
    ASSERT(weather_from_wmo(61, 0.5, 1.0, 0.0) == Weather::Snowy); // snow wins
    ASSERT(weather_from_wmo(-1, 0, 0, 0) == Weather::Sunny);
}

static const char* SAMPLE_JSON =
    "{\"latitude\":40.746216,\"longitude\":-111.905396,\"generationtime_ms\":0.148,\"utc_offset_seconds\":-21600,"
    "\"timezone\":\"America/Denver\",\"timezone_abbreviation\":\"GMT-6\",\"elevation\":1302.0,"
    "\"current_units\":{\"time\":\"iso8601\",\"interval\":\"seconds\",\"weather_code\":\"wmo code\",\"temperature_2m\":\"°C\","
    "\"precipitation\":\"mm\",\"rain\":\"mm\",\"snowfall\":\"cm\",\"showers\":\"mm\"},"
    "\"current\":{\"time\":\"2026-09-20T12:45\",\"interval\":900,\"weather_code\":0,\"temperature_2m\":21.8,"
    "\"precipitation\":0.00,\"rain\":0.00,\"snowfall\":0.00,\"showers\":0.00}}";

static void test_parse_open_meteo_sample() {
    WeatherObservation o;
    ASSERT(parse_open_meteo_current(SAMPLE_JSON, o));
    ASSERT(o.valid);
    ASSERT_EQ(o.wmo, 0);
    ASSERT(o.weather == Weather::Sunny);
    ASSERT_STREQ(o.time, "2026-09-20T12:45");
}

static void test_parse_open_meteo_rain_and_snow() {
    WeatherObservation o;
    std::string rain = "{\"current\":{\"time\":\"2026-01-01T03:00\",\"interval\":900,\"weather_code\":63,\"precipitation\":1.2,\"rain\":1.2,\"snowfall\":0.00,\"showers\":0.00}}";
    ASSERT(parse_open_meteo_current(rain, o));
    ASSERT(o.weather == Weather::Rainy);
    ASSERT_EQ(o.wmo, 63);
    std::string snow = "{\"current\":{\"time\":\"2026-01-01T03:00\",\"interval\":900,\"weather_code\":73,\"precipitation\":0.7,\"rain\":0.00,\"snowfall\":0.98,\"showers\":0.00}}";
    ASSERT(parse_open_meteo_current(snow, o));
    ASSERT(o.weather == Weather::Snowy);
    ASSERT(std::fabs(o.snowfall_cm - 0.98) < 1e-9);
}

static void test_parse_open_meteo_garbage_keeps_previous() {
    WeatherObservation o;
    o.valid = true; o.weather = Weather::Rainy; o.wmo = 61;
    ASSERT(!parse_open_meteo_current("", o));
    ASSERT(!parse_open_meteo_current("<html>502</html>", o));
    ASSERT(!parse_open_meteo_current("{\"error\":true,\"reason\":\"x\"}", o));
    ASSERT(!parse_open_meteo_current("{\"current\":{\"time\":\"t\"}}", o));   // no weather_code
    ASSERT(o.weather == Weather::Rainy);
    ASSERT_EQ(o.wmo, 61);
}

static void test_weather_state_refresh_and_failure() {
    WeatherState ws;
    ASSERT(ws.current() == Weather::Sunny);                 // before any fetch
    ws.set_fetcher([](const std::string&) { return std::string(
        "{\"current\":{\"time\":\"t\",\"weather_code\":61,\"rain\":0.4,\"showers\":0,\"snowfall\":0}}"); });
    ASSERT(ws.refresh());
    ASSERT(ws.current() == Weather::Rainy);
    ws.set_fetcher([](const std::string&) { return std::string(""); });
    ASSERT(!ws.refresh());
    ASSERT(ws.current() == Weather::Rainy);                 // previous kept
    ws.set_override(Weather::Snowy);
    ASSERT(ws.current() == Weather::Snowy);
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

static void test_open_meteo_url_targets_slc_proper() {
    std::string u = open_meteo_url();
    ASSERT(u.find("latitude=40.7608") != std::string::npos);
    ASSERT(u.find("longitude=-111.8910") != std::string::npos);
    ASSERT(u.find("current=weather_code") != std::string::npos);
    ASSERT(u.find("hourly=") == std::string::npos);   // "now" weather, not the hourly forecast
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
    WeatherState ws;
    int fetches = 0;
    ws.set_fetcher([&](const std::string&) { fetches++; return std::string(
        "{\"current\":{\"time\":\"t\",\"weather_code\":0,\"rain\":0,\"showers\":0,\"snowfall\":0}}"); });
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

int main(int argc, char** argv) {
    for (int i = 1; i < argc; ++i) if (std::string(argv[i]) == "-v") g_verbose = true;
    // Log buffer prints straight to stdout when not a TTY; keep test output readable.
    setenv("ACNH_DEBUG", "false", 1);

    run_test("filename_all_hours_and_weathers", test_filename_all_hours_and_weathers);
    run_test("hour_index_is_zero_based_local_hour", test_hour_index_is_zero_based_local_hour);
    run_test("hour_title", test_hour_title);
    run_test("wmo_mapping", test_wmo_mapping);
    run_test("parse_open_meteo_sample", test_parse_open_meteo_sample);
    run_test("parse_open_meteo_rain_and_snow", test_parse_open_meteo_rain_and_snow);
    run_test("parse_open_meteo_garbage_keeps_previous", test_parse_open_meteo_garbage_keeps_previous);
    run_test("weather_state_refresh_and_failure", test_weather_state_refresh_and_failure);
    run_test("refresh_slots_one_minute_early", test_refresh_slots_one_minute_early);
    run_test("open_meteo_url_targets_slc_proper", test_open_meteo_url_targets_slc_proper);
    run_test("all_72_files_parse_as_stereo", test_all_72_files_parse_as_stereo);
    run_test("mono_header_still_parses_and_rejects_bad", test_mono_header_still_parses_and_rejects_bad);
    run_test("loop_once_then_radio_fade", test_loop_once_then_radio_fade);
    run_test("loops_indefinitely_until_armed", test_loops_indefinitely_until_armed);
    run_test("never_ends_without_arming", test_never_ends_without_arming);
    run_test("mono_ctx_mixes_l_equals_r", test_mono_ctx_mixes_l_equals_r);
    run_test("player_changeover_only_on_hour", test_player_changeover_only_on_hour);
    run_test("stream_server_range_resume", test_stream_server_range_resume);

    int failed = 0;
    for (const auto& r : g_results) if (!r.passed) failed++;
    std::cout << "\n" << g_results.size() << " run, " << failed << " failed" << std::endl;
    return failed > 0 ? 1 : 0;
}
