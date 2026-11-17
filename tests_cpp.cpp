#include <iostream>
#include <vector>
#include <string>
#include <cassert>
#include <cmath>
#include <memory>
#include <stdexcept>

// Enable test mode for compile-time mocking of audio hardware
#define KKFM_TEST_MODE

// Mock assets_blob for testing
extern "C" { const uint8_t assets_blob[1024] = {0}; }

#define UNIT_TEST
#include "acnh_aircheck_pi.cpp"
// Note: test_mocks.h is included by acnh_aircheck_pi.cpp when KKFM_TEST_MODE is defined

bool g_verbose = false;

struct TestResult {
    std::string name;
    bool passed;
    std::string error;
};

std::vector<TestResult> g_results;

#define ASSERT(cond) if (!(cond)) { throw std::runtime_error("Assertion failed: " #cond " is true"); }
#define ASSERT_EQ(a, b) if (!((a) == (b))) { throw std::runtime_error("Assertion failed: " #a " == " #b " (" + std::to_string(a) + " != " + std::to_string(b) + ")"); }
#define ASSERT_TRUE(cond) if (!(cond)) { throw std::runtime_error("Assertion failed: " #cond " is true"); }
#define ASSERT_NEAR(a, b, eps) if (std::abs((a) - (b)) > (eps)) { throw std::runtime_error("Assertion failed: " #a " near " #b); }

void run_test(const std::string& name, void (*func)()) {
    try {
        func();
        g_results.push_back({name, true, ""});
        if (g_verbose) std::cout << "✓ " << name << std::endl;
    } catch (const std::exception& e) {
        g_results.push_back({name, false, e.what()});
        std::cout << "✗ " << name << " (Error: " << e.what() << ")" << std::endl;
    }
}

void test_volume_scaling() {
    ASSERT_EQ(volume_to_q15(0.0f), 0);
    ASSERT_EQ(volume_to_q15(1.0f), 32768);
    ASSERT_EQ(volume_to_q15(0.5f), 16384);
}

void test_clamp16() {
    ASSERT_EQ(clamp16(40000), 32767);
    ASSERT_EQ(clamp16(-40000), -32768);
    ASSERT_EQ(clamp16(100), 100);
}

void test_snap_to_grid() {
    // snap_delay_to_grid(delay, grid)
    ASSERT_NEAR(snap_delay_to_grid(0.5f, 1.0f), 1.0f, 0.001f);
    ASSERT_NEAR(snap_delay_to_grid(2.2f, 1.0f), 1.2f, 0.001f);
}

void test_mixer_basic() {
    Mixer mixer;
    int16_t out[MIX_BLOCK_FRAMES * 2];
    
    mixer.mix_block(out, MIX_BLOCK_FRAMES);
    for (int i = 0; i < MIX_BLOCK_FRAMES * 2; ++i) {
        ASSERT_EQ(out[i], 0);
    }
}

void test_bwav_parser_null() {
    AssetMeta meta = { 0, 10, 0.0f, 1, "test" };
    auto ctx = BWAVParser::parse(meta);
    ASSERT_TRUE(ctx == nullptr);
}

void test_overlay_decision_none() {
    Mixer mixer;
    Radio radio(mixer);
    auto res = radio.plan_overlay(nullptr, nullptr);
    ASSERT_TRUE(res.plan == OverlayPlan::NoOverlay);
}

void test_overlay_decision_logic() {
    Mixer mixer;
    Radio radio(mixer);
    auto intro = std::make_shared<AudioCtx>();
    intro->total_samples = 48000 * 4; // 4s
    intro->rate = 48000;
    
    auto song = std::make_shared<AudioCtx>();
    song->total_samples = 48000 * 180; // 3m
    song->rate = 48000;
    song->loop_start = 48000 * 7; // 7s
    
    auto res = radio.plan_overlay(intro, song);
    if (res.plan == OverlayPlan::NoOverlay && g_verbose) {
        std::cout << "  Plan was NoOverlay. intro_sec=" << (float)intro->total_samples/intro->rate 
                  << " loop_start_sec=" << (float)song->loop_start/song->rate << std::endl;
    }
    ASSERT_TRUE(res.plan != OverlayPlan::NoOverlay);
}

// The radio's end of a song: after the loop point the song plays on for FADE_PLAY_DURATION at full level, then fades
// over FADE_TIME_DURATION, and the next song follows: 11 s and 7 s, 18 s from the loop point. Measured on the decoder's
// own output, a looping track of one constant level: the first sample below that level, and the last sample.
// (A build with the play at 10 s is quiet a second early and ends at 17 s: both assertions fail on it. That is the
// build e65d59b made, the Pi's deployed binary: 17.03 s from its log's loop hit to the next Now Playing.)
void test_radio_post_loop_play_then_fade() {
    const uint32_t rate = 32000;
    const int blocks = 100;
    static std::vector<uint8_t> raw;
    raw.assign((size_t)blocks * BLK_BYTES, 0x11);                  // every nibble 1
    for (int b = 0; b < blocks; ++b) raw[(size_t)b * BLK_BYTES] = 0x0c;   // coefficient pair 0, scale 4096

    auto ctx = std::make_shared<AudioCtx>();
    ctx->raw_data = raw.data(); ctx->raw_len = raw.size();
    ctx->name = "test"; ctx->title = "test"; ctx->id = 0;
    ctx->rate = rate; ctx->total_samples = (uint32_t)blocks * BLK_SAMP;
    ctx->start_offset = 0; ctx->loops = 1;
    ctx->loop_start = 0; ctx->loop_end = (uint32_t)blocks * BLK_SAMP;
    ctx->hist1 = 0; ctx->hist2 = 0;
    ctx->coeffs = { {0, 0} };

    // as the radio adds a song (Mixer::add's defaults)
    Decoder d(ctx, false, FADE_PLAY_DURATION, FADE_TIME_DURATION);
    int16_t buf[BLK_SAMP];
    const int16_t level = 4096;
    long post = 0, first_quiet = -1;
    for (long guard = 0; !d.is_exhausted() && guard < 4000000; ++guard) {
        for (int i = 0; i < BLK_SAMP; ++i) buf[i] = level;
        d.decode_block(buf);
        if (!d.has_looped()) { for (int i = 0; i < BLK_SAMP; ++i) ASSERT_EQ(buf[i], level); continue; }
        for (int i = 0; i < BLK_SAMP; ++i) if (first_quiet < 0 && buf[i] < level) first_quiet = post + i;
        post += BLK_SAMP;
    }
    ASSERT_TRUE(d.is_exhausted());
    const double play_s = (double)first_quiet / rate, total_s = (double)post / rate;
    if (g_verbose) std::cout << "  after the loop point: full level for " << play_s << " s, the next song at " << total_s << " s" << std::endl;
    ASSERT_NEAR(play_s, 11.0, 0.01);
    ASSERT_NEAR(total_s, 18.0, 0.01);
}

int main(int argc, char** argv) {
    for (int i = 1; i < argc; ++i) {
        if (std::string(argv[i]) == "-v") g_verbose = true;
    }

    run_test("test_volume_scaling", test_volume_scaling);
    run_test("test_clamp16", test_clamp16);
    run_test("test_snap_to_grid", test_snap_to_grid);
    run_test("test_mixer_basic", test_mixer_basic);
    run_test("test_bwav_parser_null", test_bwav_parser_null);
    run_test("test_overlay_decision_none", test_overlay_decision_none);
    run_test("test_overlay_decision_logic", test_overlay_decision_logic);
    run_test("test_radio_post_loop_play_then_fade", test_radio_post_loop_play_then_fade);

    int failed = 0;
    for (const auto& r : g_results) {
        if (!r.passed) failed++;
    }

    if (failed > 0 || g_verbose) {
        std::cout << "\n" << g_results.size() << " run, " << failed << " failed" << std::endl;
    }

    return failed > 0 ? 1 : 0;
}
