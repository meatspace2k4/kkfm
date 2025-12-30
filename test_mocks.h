/*
 * KKFM Test Mocks Header
 *
 * This file provides compile-time mocks for testing the C++ player
 * without actual hardware dependencies.
 *
 * Usage:
 *   Compile with -DKKFM_TEST_MODE to enable test mocks.
 *   This disables all audio output and provides mock implementations
 *   of hardware-dependent functions.
 *
 * Example:
 *   g++ -DKKFM_TEST_MODE -c tests_cpp.cpp -o tests.o
 */

#ifndef KKFM_TEST_MOCKS_H
#define KKFM_TEST_MOCKS_H

#include <atomic>
#include <cstdint>
#include <cstring>
#include <functional>
#include <stdexcept>
#include <string>
#include <vector>

#ifdef KKFM_TEST_MODE

// ============================================================================
// Test Mode Configuration
// ============================================================================

// Disable actual audio output in test mode
#define KKFM_NO_AUDIO_OUTPUT 1

// Use mock miniaudio callbacks
#define KKFM_MOCK_MINIAUDIO 1

// Reduced buffer sizes for faster tests
#define KKFM_TEST_BUFFER_SIZE 1024
#define KKFM_TEST_SAMPLE_RATE 48000

// ============================================================================
// Miniaudio Type Stubs (for compilation without miniaudio.h)
// ============================================================================

// Miniaudio type aliases
typedef uint32_t ma_uint32;
typedef int32_t ma_int32;

// Result codes
typedef int ma_result;
#define MA_SUCCESS 0
#define MA_ERROR -1

// Format types
typedef int ma_format;
#define ma_format_s16 2

// Device type
typedef int ma_device_type;
#define ma_device_type_playback 1

// Backend type
typedef int ma_backend;
#define ma_backend_alsa 1

// Stub structures
struct ma_device_id { char id[256]; };
struct ma_device_info { char name[256]; ma_device_id id; };
struct ma_device {
    ma_uint32 sampleRate;
    void* pUserData;
};
struct ma_context {};
struct ma_pcm_rb {
    uint8_t* buffer;
    size_t capacity;
    size_t read_pos;
    size_t write_pos;
};

// Device config
struct ma_device_config {
    ma_device_type deviceType;
    struct { ma_format format; int channels; ma_device_id* pDeviceID; } playback;
    uint32_t sampleRate;
    uint32_t periodSizeInFrames;
    uint32_t periods;
    void (*dataCallback)(ma_device*, void*, const void*, uint32_t);
    void* pUserData;
};

// Stub functions
inline ma_device_config ma_device_config_init(ma_device_type) {
    return ma_device_config{};
}

inline ma_result ma_context_init(ma_backend*, uint32_t, void*, ma_context*) {
    return MA_SUCCESS;
}

inline void ma_context_uninit(ma_context*) {}

inline ma_result ma_context_get_devices(ma_context*, ma_device_info**, uint32_t*,
                                        ma_device_info**, uint32_t*) {
    return MA_SUCCESS;
}

inline ma_result ma_device_init(ma_context*, ma_device_config* cfg, ma_device* dev) {
    dev->sampleRate = cfg->sampleRate;
    dev->pUserData = cfg->pUserData;
    return MA_SUCCESS;
}

inline ma_result ma_device_start(ma_device*) { return MA_SUCCESS; }
inline ma_result ma_device_stop(ma_device*) { return MA_SUCCESS; }
inline void ma_device_uninit(ma_device*) {}

// Ring buffer stubs
inline ma_result ma_pcm_rb_init(ma_format, uint32_t, uint32_t, void*, void*, ma_pcm_rb* rb) {
    rb->buffer = nullptr;
    rb->capacity = 0;
    rb->read_pos = 0;
    rb->write_pos = 0;
    return MA_SUCCESS;
}

inline void ma_pcm_rb_uninit(ma_pcm_rb*) {}

inline uint32_t ma_pcm_rb_available_read(ma_pcm_rb*) { return 0; }

inline ma_result ma_pcm_rb_acquire_write(ma_pcm_rb*, uint32_t* count, void** dst) {
    static uint8_t dummy[8192];
    *dst = dummy;
    return MA_SUCCESS;
}

inline ma_result ma_pcm_rb_commit_write(ma_pcm_rb*, uint32_t) { return MA_SUCCESS; }

inline ma_result ma_pcm_rb_acquire_read(ma_pcm_rb*, uint32_t* count, void** src) {
    *count = 0;
    *src = nullptr;
    return MA_SUCCESS;
}

inline ma_result ma_pcm_rb_commit_read(ma_pcm_rb*, uint32_t) { return MA_SUCCESS; }

// ============================================================================
// Mock Types
// ============================================================================

namespace kkfm_test {

// Callback type for capturing audio samples during tests
using AudioCallback = std::function<void(const int16_t* samples, size_t frame_count)>;

// Mock device state
struct MockDevice {
    std::atomic<bool> initialized{false};
    std::atomic<bool> started{false};
    uint32_t sample_rate{KKFM_TEST_SAMPLE_RATE};
    uint32_t channels{2};
    AudioCallback on_audio;

    void reset() {
        initialized.store(false);
        started.store(false);
        sample_rate = KKFM_TEST_SAMPLE_RATE;
        channels = 2;
        on_audio = nullptr;
    }
};

// Global mock device (thread-safe)
inline MockDevice& get_mock_device() {
    static MockDevice device;
    return device;
}

// ============================================================================
// Mock Audio Functions
// ============================================================================

// Mock miniaudio device init - always succeeds
inline int mock_device_init(uint32_t sample_rate, uint32_t channels) {
    auto& dev = get_mock_device();
    dev.sample_rate = sample_rate;
    dev.channels = channels;
    dev.initialized.store(true);
    return 0;  // MA_SUCCESS
}

// Mock device start - always succeeds
inline int mock_device_start() {
    auto& dev = get_mock_device();
    if (!dev.initialized.load()) return -1;
    dev.started.store(true);
    return 0;
}

// Mock device stop
inline void mock_device_stop() {
    auto& dev = get_mock_device();
    dev.started.store(false);
}

// Mock device uninit
inline void mock_device_uninit() {
    auto& dev = get_mock_device();
    dev.reset();
}

// Set callback to capture audio samples during tests
inline void set_audio_callback(AudioCallback cb) {
    get_mock_device().on_audio = std::move(cb);
}

// ============================================================================
// Mock BWAV Data for Testing
// ============================================================================

// Minimal valid BWAV header for testing
inline std::vector<uint8_t> create_test_bwav(
    size_t sample_count = 1000,
    uint32_t sample_rate = 48000
) {
    std::vector<uint8_t> data;
    data.reserve(96 + sample_count);

    // BWAV magic
    data.push_back('B');
    data.push_back('W');
    data.push_back('A');
    data.push_back('V');

    // BOM (little endian)
    data.push_back(0xFF);
    data.push_back(0xFE);

    // Version, file size, etc. (padding to offset 14)
    while (data.size() < 14) data.push_back(0);

    // Channels (1 = mono)
    data.push_back(1);
    data.push_back(0);

    // Channel info starts at 16
    // Sample rate at offset 20 (relative to channel info at 16)
    // So absolute offset 16 + 4 = 20
    while (data.size() < 20) data.push_back(0);

    // Sample rate
    data.push_back((sample_rate >> 0) & 0xFF);
    data.push_back((sample_rate >> 8) & 0xFF);
    data.push_back((sample_rate >> 16) & 0xFF);
    data.push_back((sample_rate >> 24) & 0xFF);

    // Pad to 96 bytes (minimum BWAV header)
    while (data.size() < 96) data.push_back(0);

    // Add silent sample data (ADPCM would be more complex)
    for (size_t i = 0; i < sample_count; ++i) {
        data.push_back(0);
    }

    return data;
}

// ============================================================================
// Test Assertions
// ============================================================================

// Simple test assertion that throws on failure
inline void test_assert(bool condition, const char* message) {
    if (!condition) {
        throw std::runtime_error(std::string("Test assertion failed: ") + message);
    }
}

// Assert equal with message
template<typename T>
inline void test_assert_eq(const T& expected, const T& actual, const char* message) {
    if (expected != actual) {
        throw std::runtime_error(std::string("Test assertion failed: ") + message);
    }
}

// ============================================================================
// Sample Capture Buffer
// ============================================================================

class SampleCapture {
public:
    std::vector<int16_t> samples;
    size_t max_samples;

    explicit SampleCapture(size_t max = 1000000) : max_samples(max) {
        samples.reserve(max);
    }

    void capture(const int16_t* data, size_t count) {
        size_t to_add = std::min(count, max_samples - samples.size());
        samples.insert(samples.end(), data, data + to_add);
    }

    void clear() { samples.clear(); }

    bool is_silent() const {
        for (int16_t s : samples) {
            if (s != 0) return false;
        }
        return true;
    }

    int16_t peak() const {
        int16_t max_val = 0;
        for (int16_t s : samples) {
            int16_t abs_val = s < 0 ? -s : s;
            if (abs_val > max_val) max_val = abs_val;
        }
        return max_val;
    }
};

}  // namespace kkfm_test

#endif  // KKFM_TEST_MODE

#endif  // KKFM_TEST_MOCKS_H
