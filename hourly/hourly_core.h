/*
 * KKFM hourly BGM fork: facade over the radio engine.
 *
 * Pulls the whole radio translation unit in (BWAVParser, Decoder, Track,
 * Mixer, DacOutput, AudioPump, StreamServer, TUI, logging) with UNIT_TEST
 * defined so the radio's main() is excluded. Nothing in this file decodes
 * audio or drives the DAC; it only decides WHICH BGM file plays and WHEN the
 * engine's own fade is armed.
 *
 * Behaviour:
 *  - hour of day (local wall clock, 0..23) selects BGM_24Hour_HH_<Weather>.bwav
 *  - <Weather> is the in-memory SLC "now" observation (Sunny/Rainy/Snowy),
 *    refreshed at :14 :29 :44 :59; a refresh never changes the playing track
 *  - the track plays from frame 0 and loops in the Decoder until the hour
 *    changes; then the Decoder's radio fade is armed for the next loop hit,
 *    the track ends, 1 s of silence follows, and the next hour's file starts
 */
#pragma once

#ifndef UNIT_TEST
#define UNIT_TEST
#endif
#include "../acnh_aircheck_pi.cpp"

#include <cstdio>
#include <fstream>
#include <functional>
#include <regex>
#include <string>
#include <vector>

namespace hourly {

// ---------------------------------------------------------------------------
// Weather
// ---------------------------------------------------------------------------
enum class Weather { Sunny = 0, Rainy = 1, Snowy = 2 };

inline const char* weather_name(Weather w) {
    switch (w) {
        case Weather::Rainy: return "Rainy";
        case Weather::Snowy: return "Snowy";
        default: return "Sunny";
    }
}

inline bool weather_from_name(const std::string& s, Weather& out) {
    std::string l = to_lower_copy(trim(s));
    if (l == "sunny") { out = Weather::Sunny; return true; }
    if (l == "rainy" || l == "rain") { out = Weather::Rainy; return true; }
    if (l == "snowy" || l == "snow") { out = Weather::Snowy; return true; }
    return false;
}

// WMO weather interpretation codes (Open-Meteo "weather_code") -> game weather.
//   71,73,75,77 snow / snow grains; 85,86 snow showers        -> Snowy
//   51-57 drizzle; 61-67 rain / freezing rain; 80-82 showers;
//   95,96,99 thunderstorm                                        -> Rainy
//   everything else (clear, cloud, fog, unknown)                 -> Sunny
// Measured precipitation overrides the code so a stale code cannot hide rain/snow.
inline Weather weather_from_wmo(int code, double snowfall_cm, double rain_mm, double showers_mm) {
    if (snowfall_cm > 0.0) return Weather::Snowy;
    if ((code >= 71 && code <= 77) || code == 85 || code == 86) return Weather::Snowy;
    if (rain_mm > 0.0 || showers_mm > 0.0) return Weather::Rainy;
    if ((code >= 51 && code <= 67) || (code >= 80 && code <= 82) || (code >= 95 && code <= 99)) return Weather::Rainy;
    return Weather::Sunny;
}

struct WeatherObservation {
    bool valid = false;
    Weather weather = Weather::Sunny;
    int wmo = -1;
    double rain_mm = 0.0;
    double showers_mm = 0.0;
    double snowfall_cm = 0.0;
    double precipitation_mm = 0.0;
    std::string time;   // ISO local time reported by the service
};

// Salt Lake City proper (city centre), not South Salt Lake / West Valley.
constexpr double SLC_LATITUDE  = 40.7608;
constexpr double SLC_LONGITUDE = -111.8910;

inline std::string open_meteo_url() {
    char buf[512];
    std::snprintf(buf, sizeof(buf),
                  "https://api.open-meteo.com/v1/forecast?latitude=%.4f&longitude=%.4f"
                  "&current=weather_code,precipitation,rain,showers,snowfall&timezone=America%%2FDenver",
                  SLC_LATITUDE, SLC_LONGITUDE);
    return std::string(buf);
}

// Extract the "current" object from an Open-Meteo response. Only the fields we
// use are read; anything malformed yields false and leaves `out` untouched.
inline bool parse_open_meteo_current(const std::string& json, WeatherObservation& out) {
    size_t cur = json.find("\"current\"");
    if (cur == std::string::npos) return false;
    size_t open = json.find('{', cur);
    if (open == std::string::npos) return false;
    size_t close = json.find('}', open);
    if (close == std::string::npos) return false;
    std::string obj = json.substr(open, close - open + 1);

    auto num = [&](const char* key, double& v) -> bool {
        std::regex re(std::string("\"") + key + "\"\\s*:\\s*(-?[0-9]+(?:\\.[0-9]+)?)");
        std::smatch m;
        if (!std::regex_search(obj, m, re)) return false;
        v = std::atof(m[1].str().c_str());
        return true;
    };

    double code = 0.0;
    if (!num("weather_code", code)) return false;
    WeatherObservation o;
    o.wmo = (int)code;
    num("rain", o.rain_mm);
    num("showers", o.showers_mm);
    num("snowfall", o.snowfall_cm);
    num("precipitation", o.precipitation_mm);
    {
        std::regex re("\"time\"\\s*:\\s*\"([^\"]*)\"");
        std::smatch m;
        if (std::regex_search(obj, m, re)) o.time = m[1].str();
    }
    o.weather = weather_from_wmo(o.wmo, o.snowfall_cm, o.rain_mm, o.showers_mm);
    o.valid = true;
    out = o;
    return true;
}

// Refresh cadence: every 15 minutes, one minute early (:14 :29 :44 :59).
// The slot number changes exactly at those minutes; a refresh fires whenever the
// slot differs from the last one fetched.
inline int weather_refresh_slot(const std::tm& t) {
    return (t.tm_hour * 60 + t.tm_min + 1) / 15;
}
inline bool weather_refresh_due(const std::tm& now, int last_slot) {
    return weather_refresh_slot(now) != last_slot;
}

// Runs `curl` (present on the Mac and the Pi) and returns the body, or "" on failure.
inline std::string http_get_via_curl(const std::string& url, int timeout_s = 15) {
    std::string cmd = "curl -sS --max-time " + std::to_string(timeout_s) + " '" + url + "' 2>/dev/null";
    FILE* f = popen(cmd.c_str(), "r");
    if (!f) return "";
    std::string body;
    char buf[4096];
    size_t n;
    while ((n = fread(buf, 1, sizeof(buf), f)) > 0) body.append(buf, n);
    int rc = pclose(f);
    if (rc != 0) return "";
    return body;
}

// In-memory "now" weather. The pick reads current(); it never fetches.
class WeatherState {
    mutable std::mutex mut;
    WeatherObservation obs;
    bool has_override = false;
    Weather override_w = Weather::Sunny;
    std::string url;
    std::function<std::string(const std::string&)> fetcher;

public:
    WeatherState()
        : url(open_meteo_url()), fetcher([](const std::string& u) { return http_get_via_curl(u); }) {}

    void set_url(const std::string& u) { std::lock_guard<std::mutex> l(mut); url = u; }
    void set_fetcher(std::function<std::string(const std::string&)> f) { std::lock_guard<std::mutex> l(mut); fetcher = std::move(f); }
    void set_override(Weather w) { std::lock_guard<std::mutex> l(mut); has_override = true; override_w = w; }
    bool overridden() const { std::lock_guard<std::mutex> l(mut); return has_override; }

    Weather current() const {
        std::lock_guard<std::mutex> l(mut);
        if (has_override) return override_w;
        return obs.weather;   // Sunny until the first successful fetch
    }
    WeatherObservation observation() const { std::lock_guard<std::mutex> l(mut); return obs; }

    // Fetch and replace the in-memory observation. On any failure the previous
    // observation is kept and false is returned.
    bool refresh() {
        std::string u;
        std::function<std::string(const std::string&)> f;
        { std::lock_guard<std::mutex> l(mut); u = url; f = fetcher; }
        std::string body = f ? f(u) : "";
        WeatherObservation o;
        if (body.empty() || !parse_open_meteo_current(body, o)) {
            log_error("[HOURLY] Weather fetch failed; keeping previous observation");
            return false;
        }
        { std::lock_guard<std::mutex> l(mut); obs = o; }
        log_info_f(C_YELLOW "[HOURLY] " C_RESET "Weather fetched: " C_BOLD "%s" C_RESET
                   " (wmo=%d rain=%.2fmm showers=%.2fmm snow=%.2fcm at %s)",
                   weather_name(o.weather), o.wmo, o.rain_mm, o.showers_mm, o.snowfall_cm,
                   o.time.empty() ? "?" : o.time.c_str());
        return true;
    }
};

// ---------------------------------------------------------------------------
// Hour -> file
// ---------------------------------------------------------------------------
inline int hour_index(const std::tm& t) { return t.tm_hour; }   // 0 = 12 AM ... 23 = 11 PM

inline std::string bgm_filename(int hour, Weather w) {
    char buf[64];
    std::snprintf(buf, sizeof(buf), "BGM_24Hour_%02d_%s.bwav", hour, weather_name(w));
    return std::string(buf);
}

inline std::string hour_title(int hour, Weather w) {
    int h12 = hour % 12;
    if (h12 == 0) h12 = 12;
    char buf[64];
    std::snprintf(buf, sizeof(buf), "%d %s (%s)", h12, hour < 12 ? "AM" : "PM", weather_name(w));
    return std::string(buf);
}

// ---------------------------------------------------------------------------
// File-backed BWAV (the fork keeps audio on disk, not in the binary)
// ---------------------------------------------------------------------------
struct LoadedBwav {
    std::vector<uint8_t> bytes;     // owns the file image the AudioCtx points into
    std::string name;
    std::string title;
    std::shared_ptr<AudioCtx> ctx;
};

inline std::shared_ptr<LoadedBwav> load_bwav_file(const std::string& path, const std::string& title, int id) {
    std::ifstream f(path, std::ios::binary);
    if (!f) return nullptr;
    auto lb = std::make_shared<LoadedBwav>();
    lb->bytes.assign(std::istreambuf_iterator<char>(f), std::istreambuf_iterator<char>());
    size_t slash = path.find_last_of('/');
    lb->name = (slash == std::string::npos) ? path : path.substr(slash + 1);
    lb->title = title;
    lb->ctx = BWAVParser::parse_bytes(lb->bytes.data(), lb->bytes.size(), lb->name.c_str(), lb->title.c_str(), id);
    if (!lb->ctx) return nullptr;
    return lb;
}

// ---------------------------------------------------------------------------
// Player
// ---------------------------------------------------------------------------
struct PlayerOptions {
    std::string sound_dir = "hourly/Stream";
    double heartbeat_s = 240.0;          // periodic "Now Playing:" so the watchdog sees activity
    float fade_play_s = FADE_PLAY_DURATION;
    float fade_time_s = FADE_TIME_DURATION;
    float gap_s = 1.0f;                  // silence between tracks
    double retry_s = 5.0;                // after a missing/unparseable file
    // Test hooks (env-gated, logged loudly when set)
    int test_loops_before_switch = 0;    // >0: arm the fade after N loop hits regardless of the hour
    int test_max_tracks = 0;             // >0: stop after N tracks have finished
    std::string wav_dump_path;           // tee of mixer output (handled by main)
};

inline PlayerOptions options_from_env() {
    PlayerOptions o;
    if (const char* d = std::getenv("HOURLY_SOUND_DIR")) if (*d) o.sound_dir = d;
    if (const char* h = std::getenv("HOURLY_HEARTBEAT_S")) o.heartbeat_s = std::atof(h);
    if (const char* n = std::getenv("HOURLY_TEST_LOOPS_BEFORE_SWITCH")) o.test_loops_before_switch = std::atoi(n);
    if (const char* n = std::getenv("HOURLY_TEST_MAX_TRACKS")) o.test_max_tracks = std::atoi(n);
    if (const char* p = std::getenv("HOURLY_WAV_DUMP")) if (*p) o.wav_dump_path = p;
    return o;
}

class HourlyPlayer {
    Mixer& mixer;
    WeatherState& weather;
    PlayerOptions opt;
    std::atomic<bool> stop_sig{false};
    std::function<std::tm()> clock;     // injectable for tests

    static std::tm local_now() {
        std::time_t t = std::time(nullptr);
        std::tm out{};
        if (std::tm* p = std::localtime(&t)) out = *p;
        return out;
    }

public:
    HourlyPlayer(Mixer& m, WeatherState& w, PlayerOptions o)
        : mixer(m), weather(w), opt(std::move(o)), clock(&HourlyPlayer::local_now) {}

    void set_clock(std::function<std::tm()> c) { clock = std::move(c); }
    void request_stop() { stop_sig.store(true); }
    bool stopping() const { return stop_sig.load(); }

    // Observable state for tests / TUI (the log buffer is not readable when stdout is not a TTY).
    std::atomic<int> picks{0};
    std::atomic<int> arms{0};
    std::atomic<int> track_ends{0};
    std::atomic<int> last_pick_hour{-1};
    std::atomic<int> last_arm_from{-1};
    std::atomic<int> last_arm_to{-1};

    // One hour's file, loaded from disk. nullptr + error log if missing/bad.
    std::shared_ptr<LoadedBwav> load_for(int hour, Weather w) {
        std::string fname = bgm_filename(hour, w);
        std::string path = opt.sound_dir + "/" + fname;
        auto lb = load_bwav_file(path, hour_title(hour, w), hour);
        if (!lb) {
            log_error("[HOURLY] BGM failed to load: " + path);
            return nullptr;
        }
        log_info_f(C_YELLOW "[HOURLY] " C_RESET "Hour %02d -> picked " C_CYAN "%s" C_RESET " (weather=%s)",
                   hour, fname.c_str(), weather_name(w));
        log_info_f(C_GRAY "[FILE] " C_RESET C_CYAN "%s" C_RESET " (id=%d, size=%u)",
                   fname.c_str(), hour, (unsigned)lb->bytes.size());
        return lb;
    }

    void run() {
        if (opt.test_loops_before_switch > 0)
            log_info_f(C_RED "[HOURLY] TEST HOOK: forcing changeover after %d loop hit(s)" C_RESET, opt.test_loops_before_switch);
        if (opt.test_max_tracks > 0)
            log_info_f(C_RED "[HOURLY] TEST HOOK: exiting after %d track(s)" C_RESET, opt.test_max_tracks);

        mixer.start();

        // Startup: fill the in-memory weather before the first pick.
        int last_slot = weather_refresh_slot(clock());
        if (!weather.overridden()) weather.refresh();

        int tracks_done = 0;
        while (!stop_sig) {
            std::tm now = clock();
            int hour = hour_index(now);
            Weather w = weather.current();

            auto lb = load_for(hour, w);
            if (!lb) {
                // No radio fallback: wait and try the same hour again.
                auto until = std::chrono::steady_clock::now() + std::chrono::duration<double>(opt.retry_s);
                while (!stop_sig && std::chrono::steady_clock::now() < until) {
                    g_tui.render();
                    std::this_thread::sleep_for(std::chrono::milliseconds(50));
                }
                continue;
            }

            picks++;
            last_pick_hour = hour;

            // Frame 0, no delay, loop forever (fp = 0 disables the first-loop fade).
            auto tr = mixer.add(lb->ctx, VOL_DEFAULT, 0.0f, false, 0.0f, 0.0f);
            auto dec = tr->get_decoder();
            const int playing_hour = hour;
            bool armed = false;
            auto last_heartbeat = std::chrono::steady_clock::now();

            while (!tr->done() && !stop_sig) {
                now = clock();

                // Weather refresh only updates memory; it never touches the playing track.
                if (!weather.overridden() && weather_refresh_due(now, last_slot)) {
                    last_slot = weather_refresh_slot(now);
                    weather.refresh();
                }

                if (!armed && dec) {
                    bool hour_changed = hour_index(now) != playing_hour;
                    bool test_force = opt.test_loops_before_switch > 0 &&
                                      dec->loops_hit() >= (size_t)opt.test_loops_before_switch;
                    if (hour_changed || test_force) {
                        dec->arm_fade_at_next_loop(opt.fade_play_s, opt.fade_time_s);
                        armed = true;
                        arms++;
                        last_arm_from = playing_hour;
                        last_arm_to = hour_index(now);
                        log_info_f(C_YELLOW "[HOURLY] " C_RESET "Changeover %02d -> %02d: " C_CYAN "%s" C_RESET
                                   " fades at its next loop end (%s)",
                                   playing_hour, hour_index(now), lb->name.c_str(),
                                   test_force && !hour_changed ? "TEST HOOK" : "hour changed");
                    }
                }

                if (opt.heartbeat_s > 0) {
                    auto t = std::chrono::steady_clock::now();
                    if (std::chrono::duration<double>(t - last_heartbeat).count() >= opt.heartbeat_s) {
                        last_heartbeat = t;
                        log_info(std::string(C_BOLD "Now Playing: " C_RESET) + lb->title);
                    }
                }

                g_tui.render();
                std::this_thread::sleep_for(std::chrono::milliseconds(50));
            }
            if (stop_sig) break;

            log_info_f(C_YELLOW "[HOURLY] " C_RESET "Track end: " C_CYAN "%s" C_RESET " (loops=%zu)",
                       lb->name.c_str(), dec ? dec->loops_hit() : (size_t)0);
            track_ends++;

            // 1 s pause, then the next pick (hour and weather re-read at that moment).
            auto gap = mixer.add_silence(opt.gap_s);
            while (!gap->done() && !stop_sig) {
                g_tui.render();
                std::this_thread::sleep_for(std::chrono::milliseconds(20));
            }

            tracks_done++;
            if (opt.test_max_tracks > 0 && tracks_done >= opt.test_max_tracks) {
                log_info_f(C_RED "[HOURLY] TEST HOOK: %d track(s) done, exiting" C_RESET, tracks_done);
                break;
            }
        }
    }
};

} // namespace hourly
