#include <iostream>
#include <vector>
#include <string>
#include <cassert>
#include <cmath>
#include <memory>
#include <stdexcept>

// Mock assets_blob for testing
extern "C" { const uint8_t assets_blob[1024] = {0}; }

#define UNIT_TEST
#include "acnh_aircheck_pi_final.cpp"

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

    int failed = 0;
    for (const auto& r : g_results) {
        if (!r.passed) failed++;
    }

    if (failed > 0 || g_verbose) {
        std::cout << "\n" << g_results.size() << " run, " << failed << " failed" << std::endl;
    }

    return failed > 0 ? 1 : 0;
}
