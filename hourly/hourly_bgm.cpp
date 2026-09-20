/*
 * KKFM hourly BGM: new main on the radio engine.
 *
 * Build (macOS / Pi, from the repo root):
 *   g++ -std=c++17 -O2 -I. hourly/hourly_bgm.cpp StreamServer.cpp -o hourly_bgm -pthread -ldl -lm
 *
 * Run:
 *   ACNH_AUDIO_DEVICE=BlackHole ACNH_LOCALHOST_ONLY=1 ./hourly_bgm [port]
 *
 * Same env as the radio: ACNH_STREAM_PORT, ACNH_LOCALHOST_ONLY, ACNH_AUDIO_DEVICE,
 * ACNH_AUDIO_DEVICE_INDEX, ACNH_DISABLE_DAC, ACNH_DEBUG.
 * Fork env: HOURLY_SOUND_DIR (default hourly/Stream), HOURLY_HEARTBEAT_S,
 * HOURLY_WEATHER_OVERRIDE=Sunny|Rainy|Snowy, HOURLY_WEATHER_URL,
 * test hooks HOURLY_TEST_LOOPS_BEFORE_SWITCH, HOURLY_TEST_MAX_TRACKS, HOURLY_WAV_DUMP.
 */
#include "hourly_core.h"

// The engine links against a packed asset blob; the fork keeps audio on disk.
const uint8_t assets_blob[16] = {0};

namespace {

// Mixer that also tees its stereo output to a WAV file (sample analysis of the
// exact bytes the DAC and /stream receive). Only active with HOURLY_WAV_DUMP.
class TeeMixer : public Mixer {
    FILE* f = nullptr;
    uint32_t frames_written = 0;
public:
    bool open(const std::string& path, uint32_t rate) {
        f = std::fopen(path.c_str(), "wb");
        if (!f) return false;
        WavHeader h;
        h.sample_rate = rate;
        h.byte_rate = rate * CHANNELS * sizeof(int16_t);
        std::fwrite(&h, 1, sizeof(h), f);
        return true;
    }
    void mix_block(int16_t* out, int frames) override {
        Mixer::mix_block(out, frames);
        if (f) {
            std::fwrite(out, sizeof(int16_t) * CHANNELS, (size_t)frames, f);
            frames_written += (uint32_t)frames;
        }
    }
    void close(uint32_t rate) {
        if (!f) return;
        WavHeader h;
        h.sample_rate = rate;
        h.byte_rate = rate * CHANNELS * sizeof(int16_t);
        h.data_size = frames_written * CHANNELS * sizeof(int16_t);
        h.file_size = 36 + h.data_size;
        std::fseek(f, 0, SEEK_SET);
        std::fwrite(&h, 1, sizeof(h), f);
        std::fclose(f);
        f = nullptr;
    }
};

hourly::HourlyPlayer* g_player = nullptr;

uint16_t parse_port(const char* text, uint16_t fallback) {
    if (!text) return fallback;
    char* end = nullptr;
    long value = std::strtol(text, &end, 10);
    if (!end || *end != '\0' || value <= 0 || value > 65535) return fallback;
    return static_cast<uint16_t>(value);
}

void handle_shutdown_signal(int) {
    if (g_player) g_player->request_stop();
}

// Engine sample rate from a BGM header (the radio reads it from its blob).
void init_engine_sample_rate_from_dir(const std::string& dir) {
#ifdef HOURLY_USE_BLOB
    (void)dir;
    for (size_t i = 0; i < hourly_assets_count; ++i) {
        const HourlyAsset& a = hourly_assets[i];
        const uint8_t* head = hourly_assets_blob + a.offset;
        if (a.size < 96 || std::memcmp(head, "BWAV", 4) != 0) continue;
        uint16_t chans = read_u16(head + 14);
        if (chans != 1 && chans != 2) continue;
        uint32_t rate = read_u32(head + 16 + 4);
        if (rate < 8000 || rate > 192000) continue;
        g_engine_sample_rate.store(rate);
        log_info_f(C_BLUE "[AUDIO] " C_RESET "Engine sample rate from packed BWAV header: %uHz (%u ch, %zu assets)", rate, (unsigned)chans, hourly_assets_count);
        return;
    }
    log_error("[AUDIO] No BGM header in the packed assets; using default sample rate");
    return;
#endif
    for (int hour = 0; hour < 24; ++hour) {
        for (hourly::Weather w : {hourly::Weather::Sunny, hourly::Weather::Rainy, hourly::Weather::Snowy}) {
            std::ifstream f(dir + "/" + hourly::bgm_filename(hour, w), std::ios::binary);
            if (!f) continue;
            std::vector<uint8_t> head(256, 0);
            f.read(reinterpret_cast<char*>(head.data()), (std::streamsize)head.size());
            size_t got = (size_t)f.gcount();
            AssetMeta meta{0, (uint32_t)got, 0.0f, hour, "probe", nullptr};
            (void)meta;
            if (got < 96 || std::memcmp(head.data(), "BWAV", 4) != 0) continue;
            uint16_t chans = read_u16(head.data() + 14);
            if (chans != 1 && chans != 2) continue;
            uint32_t rate = read_u32(head.data() + 16 + 4);
            if (rate < 8000 || rate > 192000) continue;
            g_engine_sample_rate.store(rate);
            log_info_f(C_BLUE "[AUDIO] " C_RESET "Engine sample rate from BWAV header: %uHz (%u ch)", rate, (unsigned)chans);
            return;
        }
    }
    log_error("[AUDIO] No BGM header found under " + dir + "; using default sample rate");
}

} // namespace

int main(int argc, char** argv) {
    setenv("ACNH_DEBUG", "true", 1);

    hourly::PlayerOptions opt = hourly::options_from_env();
    init_engine_sample_rate_from_dir(opt.sound_dir);

    ignore_sigpipe();

    uint16_t port = DEFAULT_STREAM_PORT;
    if (const char* env_port = std::getenv("ACNH_STREAM_PORT")) port = parse_port(env_port, port);
    if (argc > 1) port = parse_port(argv[1], port);

    bool localhost_only = env_truthy(std::getenv("ACNH_LOCALHOST_ONLY"));

    hourly::WeatherState weather;
    if (const char* u = std::getenv("HOURLY_WEATHER_URL")) if (*u) weather.set_url(u);
    if (const char* ov = std::getenv("HOURLY_WEATHER_OVERRIDE")) {
        hourly::Weather w;
        if (hourly::weather_from_name(ov, w)) {
            weather.set_override(w);
            log_info_f(C_RED "[HOURLY] TEST HOOK: weather overridden to %s" C_RESET, hourly::weather_name(w));
        } else {
            log_error(std::string("[HOURLY] Bad HOURLY_WEATHER_OVERRIDE: ") + ov);
        }
    }

    TeeMixer mixer;
    if (!opt.wav_dump_path.empty()) {
        if (mixer.open(opt.wav_dump_path, engine_sample_rate()))
            log_info_f(C_RED "[HOURLY] TEST HOOK: mixer output teed to %s" C_RESET, opt.wav_dump_path.c_str());
        else
            log_error("[HOURLY] Could not open WAV dump: " + opt.wav_dump_path);
    }

    PCMBuffer buffer;
    DacOutput dac(HARDWARE_GAIN_Q15);
    DacOutput* dac_ptr = nullptr;
    if (dac.init_from_env()) dac_ptr = &dac;

    AudioPump pump(mixer, buffer, dac_ptr);
    StreamServer server(buffer);
    hourly::HourlyPlayer player(mixer, weather, opt);
    g_player = &player;

    pump.start();
    if (!server.start(port, localhost_only)) {
        log_error("[WEB] Failed to start HTTP server on port " + std::to_string(port));
        pump.stop();
        return 1;
    }

    std::signal(SIGINT, handle_shutdown_signal);
#ifdef SIGTERM
    std::signal(SIGTERM, handle_shutdown_signal);
#endif

    log_info(std::string(C_GREEN) + "[AIRCHECK] " + C_RESET + "Streaming hourly BGM on port " + C_BOLD + std::to_string(port) + C_RESET + " (visit /stream)");
    g_tui.render();
    player.run();

    server.stop();
    pump.stop();
    dac.shutdown();
    mixer.close(engine_sample_rate());
    log_info(std::string(C_GREEN) + "[AIRCHECK] " + C_RESET + "Shutdown complete.");
    return 0;
}
