/*
 * Streams the mixed radio output over HTTP so multiple browser clients
 * can listen in real-time without needing a local audio device.
 */

#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wstringop-overflow"
#define MINIAUDIO_IMPLEMENTATION
#include "miniaudio.h"
#pragma GCC diagnostic pop
#include "assets.h"
#include "StreamServer.h"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cctype>
#include <csignal>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <cerrno>
#include <iomanip>
#include <iostream>
#include <map>
#include <memory>
#include <cstdio>
#include <cstdint>
#include <mutex>
#include <netdb.h>
#include <netinet/in.h>
#include <random>
#include <regex>
#include <string>
#include <thread>
#include <vector>
#include <arpa/inet.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <sys/time.h>
#include <fcntl.h>
#include <unistd.h>
#include <initializer_list>
#include <iterator>
#include <unordered_map>
#include <cstdarg>
#include <filesystem>

// ---------------------------------------------------------------------------
// Runtime Sample Rate (derived from BWAV headers)
// ---------------------------------------------------------------------------
static std::atomic<uint32_t> g_engine_sample_rate{(uint32_t)SAMPLE_RATE};

static inline uint32_t engine_sample_rate() {
    return std::max<uint32_t>(1u, g_engine_sample_rate.load());
}

static uint32_t try_read_bwav_sample_rate(const AssetMeta& meta) {
    const uint8_t* data = assets_blob + meta.offset;
    size_t len = meta.size;
    if (len < 96 || std::memcmp(data, "BWAV", 4) != 0) return 0;
    uint16_t bom = read_u16(data + 4);
    uint16_t chans = read_u16(data + 14);
    if (chans != 1 || bom != 0xFEFF) return 0;
    const uint8_t* ci = data + 16;
    uint32_t rate = read_u32(ci + 4);
    if (rate < 8000 || rate > 192000) return 0;
    return rate;
}

static void init_engine_sample_rate_from_assets() {
    uint32_t rate = 0;
    if (bin_songs_count > 1) {
        rate = try_read_bwav_sample_rate(bin_songs[1]);
    } else if (bin_songs_count > 0) {
        rate = try_read_bwav_sample_rate(bin_songs[0]);
    }
    if (rate == 0 && bin_intros_count > 0) {
        rate = try_read_bwav_sample_rate(bin_intros[0]);
    }
    if (rate == 0 && bin_jingles_count > 0) {
        rate = try_read_bwav_sample_rate(bin_jingles[0]);
    }
    if (rate == 0 && bin_commercials_count > 0) {
        rate = try_read_bwav_sample_rate(bin_commercials[0]);
    }
    if (rate == 0) return;

    g_engine_sample_rate.store(rate);
    {
        char msg[160];
        snprintf(msg, sizeof(msg), C_BLUE "[AUDIO] " C_RESET "Engine sample rate from BWAV header: %uHz", rate);
        log_info(msg);
    }
}

// ---------------------------------------------------------------------------
// Logging Formatting
// ---------------------------------------------------------------------------
#define C_RESET   "\033[0m"
#define C_RED     "\033[31m"
#define C_GREEN   "\033[32m"
#define C_YELLOW  "\033[33m"
#define C_BLUE    "\033[34m"
#define C_MAGENTA "\033[35m"
#define C_CYAN    "\033[36m"
#define C_GRAY    "\033[90m"
#define C_BOLD    "\033[1m"

// ---------------------------------------------------------------------------
// Debug Logging Wrapper
// ---------------------------------------------------------------------------

#include <sys/ioctl.h>
#include <unistd.h>

// Forward declaration
struct LogBuffer;
// extern LogBuffer g_log_buffer; // Not needed if defined before use

bool is_debug_enabled() {
    static bool init = false;
    static bool debug = false;
    if (!init) {
        const char* env = std::getenv("ACNH_DEBUG");
        if (env) {
            std::string s = env;
            // trim
            size_t first = s.find_first_not_of(" \t\r\n");
            if (first != std::string::npos) {
                size_t last = s.find_last_not_of(" \t\r\n");
                s = s.substr(first, (last - first + 1));
                for(char &c : s) c = std::tolower(c);
                debug = (s == "1" || s == "true" || s == "yes" || s == "on");
            }
        }
        init = true;
    }
    return debug;
}

struct LogBuffer {
    std::vector<std::string> lines;
    std::mutex mut;
    const size_t max_lines = 100; // Increased buffer size, render will clip
    bool is_tty;

    LogBuffer() {
        is_tty = isatty(STDOUT_FILENO);
    }

    void add(const std::string& msg) {
        if (!is_tty) {
             // If not TTY, just print immediately
            std::cout << msg << std::endl;
            return;
        }
        std::lock_guard<std::mutex> lock(mut);
        lines.push_back(msg);
        if (lines.size() > max_lines) {
            lines.erase(lines.begin());
        }
    }
    
    std::vector<std::string> get_lines() {
        std::lock_guard<std::mutex> lock(mut);
        return lines;
    }
};

LogBuffer g_log_buffer;

template<typename F>
void log_debug(F&& log_func) {
    if (is_debug_enabled()) {
        log_func();
    }
}

std::string vformat(const char* fmt, va_list args) {
    va_list args_copy;
    va_copy(args_copy, args);
    int len = std::vsnprintf(nullptr, 0, fmt, args_copy);
    va_end(args_copy);
    if (len <= 0) return "";
    std::vector<char> vec(len + 1);
    std::vsnprintf(&vec[0], len + 1, fmt, args);
    return std::string(vec.begin(), vec.begin() + len);
}

void debug_printf(const char* fmt, ...) {
    if (!is_debug_enabled()) return;
    va_list args;
    va_start(args, fmt);
    std::string msg = vformat(fmt, args);
    va_end(args);
    if (!msg.empty() && msg.back() == '\n') msg.pop_back();
    g_log_buffer.add(msg);
}

void log_info(const std::string& msg) {
    g_log_buffer.add(msg);
}

void log_info_f(const char* fmt, ...) {
    va_list args;
    va_start(args, fmt);
    std::string msg = vformat(fmt, args);
    va_end(args);
    log_info(msg);
}

void log_error(const std::string& msg) {
    std::string formatted = std::string(C_RED "ERROR: ") + msg + C_RESET;
    g_log_buffer.add(formatted);
    // Ensure errors are printed to stderr in case of early exit or TTY issues
    // BUT only if NOT a TTY, to avoid breaking the TUI layout.
    if (!isatty(STDOUT_FILENO) && isatty(STDERR_FILENO)) {
        std::cerr << formatted << std::endl;
    }
}

struct BWAVInfo {
    std::string name;
    uint32_t rate = 0;
    uint32_t len = 0;
    uint32_t loop_start = 0;
    uint32_t loop_end = 0;
    int16_t h1 = 0;
    int16_t h2 = 0;
    bool valid = false;
};

struct UIState {
    std::mutex mut;
    BWAVInfo current_bwav;
    std::string now_playing;
    std::string status_text = "Ready";
    std::string status_color = C_GRAY;
};

UIState g_ui_state;

class TUI {
    bool is_tty;
    struct winsize w;
public:
    TUI() {
        is_tty = isatty(STDOUT_FILENO);
        if (is_tty) {
            // Enter alternate screen buffer and hide cursor
            std::cout << "\033[?1049h\033[?25l" << std::flush;
            
            // Note: atexit is hard to capture "this" context or restore correctly if object destroyed first
            // We rely on destructor.
        }
    }
    
    ~TUI() {
        if (is_tty) {
             // Restore cursor and exit alternate buffer
             std::cout << "\033[?25h\033[?1049l" << std::flush;
        }
    }

    void render() {
        if (!is_tty) return;
        
        // Get terminal size
        if (ioctl(STDOUT_FILENO, TIOCGWINSZ, &w) == -1) {
            w.ws_row = 24; 
            w.ws_col = 80;
        }
        int height = w.ws_row;
        if (height < 10) height = 10; 

        std::lock_guard<std::mutex> ui_lock(g_ui_state.mut);
        auto lines = g_log_buffer.get_lines();

        std::stringstream ss;
        // Home cursor
        ss << "\033[H";
        
        // Header
        ss << C_BOLD C_GREEN "  KKFM Aircheck  " C_RESET "\033[K\n";
        ss << C_GRAY "=======================" C_RESET "\033[K\n";
        ss << "\033[K\n"; // Empty line

        // Status
        ss << "Status: " << g_ui_state.status_color << g_ui_state.status_text << C_RESET << "\033[K\n";
        ss << "Now Playing: " << C_BOLD << (g_ui_state.now_playing.empty() ? "..." : g_ui_state.now_playing) << C_RESET << "\033[K\n";
        
        int used_lines = 6; // Header(1) + Div(1) + Empty(1) + Status(1) + NowPlaying(1) + LogHeaderEmpty(1) = 6

        if (g_ui_state.current_bwav.valid && is_debug_enabled()) {
            const auto& b = g_ui_state.current_bwav;
            ss << C_GRAY << "Debug: " << b.name << " (" << b.rate << "Hz) Loop: " << b.loop_start << "-" << b.loop_end << C_RESET << "\033[K\n";
            used_lines++;
        }

        ss << "\033[K\n" << C_GRAY << "--- Log ---" << C_RESET << "\033[K\n";
        used_lines++; // Log Header itself
        
        int available_log_lines = height - used_lines - 1; // -1 for safety/cursor line
        if (available_log_lines < 0) available_log_lines = 0;
        
        // Take last N lines that fit
        int start_idx = 0;
        if (lines.size() > (size_t)available_log_lines) {
            start_idx = lines.size() - available_log_lines;
        }
        
        for (size_t i = start_idx; i < lines.size(); ++i) {
            ss << lines[i] << "\033[K\n";
        }
        
        // Clear remaining screen (from cursor to end of screen)
        ss << "\033[J";
        
        std::cout << ss.str() << std::flush;
    }
};

TUI g_tui;



// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------
const float VOL_DEFAULT = 1.0f;
const float VOL_DUCK = 0.25f;
const float VOL_MAX = 1.5f;
const float DELAY_AFTER_CM_S = 0.75f;  // was 0.90f; game uses 0.75f convention (matches OUTRO, in BgmStatic Fanfare→Outdoor dst_off=0.75)
const float DELAY_AFTER_OUTRO = 0.75f;
const float OVERLAY_MIN_DELAY = 1.0f;
const float OVERLAY_MAX_DELAY = 13.0f;
const float OVERLAY_LATEST_IF_GRID = 2.5f;
const bool  OVERLAY_ALLOW_WRAP = true;
const float OVERLAY_RANGE_MIN = -5.0f;  // was -3.0f; symmetric with RANGE_MAX = 5.0f, and -5.0f IS in game radio code (2 occurrences)
const float OVERLAY_RANGE_MAX = 5.0f;

const float INTRO_ALIGN_PAD_SEC = 0.0f;
const int   OVERLAY_DELTA_SONG_FIRST_MIN = 1;
const int   OVERLAY_DELTA_SONG_FIRST_MAX = 13;
const int   OVERLAY_DELTA_INTRO_FIRST_MIN = -7;
const int   OVERLAY_DELTA_INTRO_FIRST_MAX = -1;

const float SONG_TIMEBASE_FPS = 60.0f;
const int   SONG_BIAS_FRAMES_MOD = 5;
const int   SONG_BIAS_MULT = 7;

const float RAMP_DURATION_SEC = 1.0f;

const float MASTER_GAIN_DB = 0.0f;
const float MASTER_GAIN = std::pow(10.0f, MASTER_GAIN_DB / 20.0f);
const int   MASTER_GAIN_Q15 = (int)(MASTER_GAIN * 32768.0f);

const float HARDWARE_GAIN_DB = -8.0f;
const float HARDWARE_GAIN = std::pow(10.0f, HARDWARE_GAIN_DB / 20.0f);
const int   HARDWARE_GAIN_Q15 = (int)(HARDWARE_GAIN * 32768.0f);

const float DEFAULT_NO_OVERLAY_DELAY = 11.0f;

// Kept in sync with lib/bwav.js's FADE_PLAY_SECONDS/FADE_SECONDS (jukebox's
// client-side decode) -- this jukebox_pi.cpp fork's copy of the constant was
// forked before origin/dev commit 6150710 bumped it 10.0f -> 11.0f; matching
// it here keeps the server's broadcast-schedule duration estimate in step
// with what the client actually decodes and plays.
const float FADE_PLAY_DURATION = 11.0f;
const float FADE_TIME_DURATION = 7.0f;

// ---------------------------------------------------------------------------
// Utils
// ---------------------------------------------------------------------------
int volume_to_q15(float v) {
    float clamped = std::max(0.0f, std::min(VOL_MAX, v));
    return (int)(clamped * 32768.0f);
}

float canonical_bpm_for_class(int tempo_class) {
    switch(tempo_class) {
        case 40: return 84.0f;
        case 50: return 102.0f;
        case 60: return 126.0f;
        case 70: return 150.0f;
        case 80: return 168.0f;
        default: return 0.0f;
    }
}

int choose_phrase_beats(float loop_start, float beat_len) {
    if (beat_len <= 0.0f) return 4;
    const int options[] = {16, 8, 32, 4}; 
    for (int beats : options) {
        // Allow negative start time up to one full beat.
        // This accounts for tracks starting mid-measure (pickup notes) or slight tempo drift
        // while preserving the phrase structure (e.g. 8 beats back from loop).
        if (loop_start - beats * beat_len >= -beat_len) return beats;
    }
    return 4;
}

float compute_song_start(float loop_start, int tempo_class, int unique_id) {
    float bpm = canonical_bpm_for_class(tempo_class);
    if (bpm <= 0.0f || loop_start <= 0.0f) return 0.0f;
    float beat_len = 60.0f / bpm;
    int phrase_beats = choose_phrase_beats(loop_start, beat_len);
    float raw = loop_start - phrase_beats * beat_len;
    if (raw < 0.0f) raw = 0.0f;
    int bias_frames = 0;
    if (unique_id != 0 && SONG_BIAS_FRAMES_MOD > 0) {
        long long product = static_cast<long long>(unique_id) * SONG_BIAS_MULT;
        if (product < 0) product = -product;
        bias_frames = static_cast<int>(product % SONG_BIAS_FRAMES_MOD);
    }
    int frames = (int)std::floor(raw * SONG_TIMEBASE_FPS - bias_frames);
    if (frames < 0) frames = 0;
    return frames / SONG_TIMEBASE_FPS;
}

// ---------------------------------------------------------------------------
// Date / Time
// ---------------------------------------------------------------------------
struct Date {
    int year, month, day;
    bool operator==(const Date& o) const { return year == o.year && month == o.month && day == o.day; }
};

Date get_game_date() {
    std::time_t t = std::time(nullptr);
    std::tm* now = std::localtime(&t);
    if (now->tm_hour < 5) {
        t -= 24 * 60 * 60;
        now = std::localtime(&t);
    }
    return {now->tm_year + 1900, now->tm_mon + 1, now->tm_mday};
}

int get_weekday(Date d) {
    std::tm t = {};
    t.tm_year = d.year - 1900;
    t.tm_mon = d.month - 1;
    t.tm_mday = d.day;
    std::mktime(&t);
    int w = t.tm_wday - 1; 
    if (w < 0) w = 6;
    return w;
}

std::time_t date_to_time_t(Date d) {
    std::tm t = {};
    t.tm_year = d.year - 1900;
    t.tm_mon = d.month - 1;
    t.tm_mday = d.day;
    t.tm_hour = 12;
    t.tm_isdst = -1;
    return std::mktime(&t);
}

int days_between(Date from, Date to) {
    std::time_t a = date_to_time_t(from);
    std::time_t b = date_to_time_t(to);
    if (a == (time_t)-1 || b == (time_t)-1) return 0;
    double diff = std::difftime(b, a);
    return (int)std::llround(diff / (24.0 * 60.0 * 60.0));
}

Date add_days(Date d, int delta) {
    std::time_t t = date_to_time_t(d);
    t += static_cast<long long>(delta) * 24LL * 60LL * 60LL;
    std::tm* now = std::localtime(&t);
    if (!now) return {1970, 1, 1};
    return {now->tm_year + 1900, now->tm_mon + 1, now->tm_mday};
}

bool is_event_window(Date today, Date event_date, int before_days) {
    if (before_days < 0) before_days = 0;
    int remaining = days_between(today, event_date);
    return remaining >= 0 && remaining <= before_days;
}

// ---------------------------------------------------------------------------
// Managers
// ---------------------------------------------------------------------------

class CalendarManager {
public:
    const AssetMeta* jingle_asset_for(int music_no) const {
        if (music_no >= 0 && music_no < 1024) {
            int16_t idx = jingle_lookup[music_no];
            if (idx >= 0 && idx < (int)bin_jingles_count) {
                return &bin_jingles[idx];
            }
        }
        return nullptr;
    }

    const AssetMeta* commercial_asset_for(int music_no) const {
        if (music_no >= 0 && music_no < 1024) {
            int16_t idx = commercial_lookup[music_no];
            if (idx >= 0 && idx < (int)bin_commercials_count) {
                return &bin_commercials[idx];
            }
        }
        return nullptr;
    }

    // Binary search for event occurrence
    bool event_occurs_on(EventId eid, Date date) const {
        if (eid == EventId::None) return false;
        uint32_t ymd = date.year * 10000 + date.month * 100 + date.day;
        
        // Schedule is sorted by ymd
        size_t left = 0;
        size_t right = bin_schedule_count;
        while (left < right) {
            size_t mid = left + (right - left) / 2;
            if (bin_schedule[mid].ymd < ymd) {
                left = mid + 1;
            } else if (bin_schedule[mid].ymd > ymd) {
                right = mid;
            } else {
                // Found the date, now check if any entry on this date matches eid
                // (There might be multiple events on the same day)
                // Scan backwards
                size_t scan = mid;
                while (scan > 0 && bin_schedule[scan].ymd == ymd) {
                    if (bin_schedule[scan].id == eid) return true;
                    scan--;
                }
                if (scan == 0 && bin_schedule[0].ymd == ymd && bin_schedule[0].id == eid) return true;

                // Scan forwards
                scan = mid;
                while (scan < bin_schedule_count && bin_schedule[scan].ymd == ymd) {
                    if (bin_schedule[scan].id == eid) return true;
                    scan++;
                }
                return false; 
            }
        }
        return false;
    }

    // Check if any relevant event occurs on date
    bool event_occurs_on_any(Date date) const {
        uint32_t ymd = date.year * 10000 + date.month * 100 + date.day;
        size_t left = 0;
        size_t right = bin_schedule_count;
        while (left < right) {
            size_t mid = left + (right - left) / 2;
            if (bin_schedule[mid].ymd < ymd) {
                left = mid + 1;
            } else if (bin_schedule[mid].ymd > ymd) {
                right = mid;
            } else {
                // Found an event
                // Check if it's Totakeke (skip) or generic
                size_t scan = mid;
                while (scan < bin_schedule_count && bin_schedule[scan].ymd == ymd) {
                    if (bin_schedule[scan].id != EventId::Totakekelive && 
                        bin_schedule[scan].id != EventId::None) return true;
                    scan++;
                }
                scan = mid;
                while (scan > 0 && bin_schedule[scan-1].ymd == ymd) {
                    scan--;
                    if (bin_schedule[scan].id != EventId::Totakekelive && 
                        bin_schedule[scan].id != EventId::None) return true;
                }
                return false;
            }
        }
        return false;
    }

    bool is_event_active(EventId eid, int before_days, Date gday) const {
        if (eid == EventId::None) return true;
        
        // Optimization: Check if event occurs today or in the next 'before_days'
        // Just iterate days? Efficient enough for small range.
        for (int i = 0; i <= before_days; ++i) {
            Date d = add_days(gday, i);
            if (event_occurs_on(eid, d)) return true;
        }
        return false;
    }

    std::unordered_map<EventId, int> jingle_id_weights(Date gday) const {
        std::unordered_map<EventId, int> weights;
        for (size_t i = 0; i < bin_jingle_rules_count; ++i) {
            const auto& rule = bin_jingle_rules[i];
            if (rule.event_id == EventId::None) continue;
            if (is_event_active(rule.event_id, rule.before_days, gday)) {
                weights[rule.event_id] += rule.rate;
            }
        }
        return weights;
    }

    bool should_play_totakeke(Date gday) const {
        int wd = get_weekday(gday);
        if (wd == 4 || wd == 5) return true; // Friday/Saturday always play commercial
                
        // Play Sunday only if Saturday had an event.
        if (wd == 6) {
            Date sat = add_days(gday, -1);
            return event_occurs_on_any(sat);
        }
        
        // Play Monday only if BOTH Saturday AND Sunday had events.
        if (wd == 0) { // Monday
            Date sun = add_days(gday, -1);
            Date sat = add_days(gday, -2);
            return event_occurs_on_any(sat) && event_occurs_on_any(sun);
        }

        return false;
    }

    static bool matches_bucket(int music_no, const std::string& bucket) {
        if (bucket == "morning") return music_no >= 307 && music_no <= 312;
        if (bucket == "night") return music_no >= 313 && music_no <= 318;
        return music_no >= 301 && music_no <= 306;
    }

    const AssetMeta* pick_weighted(const std::vector<std::pair<const AssetMeta*, int>>& pool) const {
        int total = 0;
        for (const auto& entry : pool) total += entry.second;
        if (total <= 0) return nullptr;
        int r = rand() % total;
        for (const auto& entry : pool) {
            if (r < entry.second) return entry.first;
            r -= entry.second;
        }
        return pool.back().first;
    }

    const AssetMeta* pick_commercial(Date gday) const {
        std::vector<std::pair<const AssetMeta*, int>> pool;
        for (size_t i = 0; i < bin_commercial_rules_count; ++i) {
            const auto& rule = bin_commercial_rules[i];
            const AssetMeta* asset = commercial_asset_for(rule.music_no);
            if (!asset) continue;

            bool include = false;
            int weight = rule.rate;
            if (rule.event_id == EventId::None) {
                include = true;
                weight = 1;
            } else if (rule.event_id == EventId::Totakekelive) {
                include = should_play_totakeke(gday);
            } else {
                include = is_event_active(rule.event_id, rule.before_days, gday);
            }

            if (include) pool.push_back({asset, std::max(1, weight)});
        }
        if (pool.empty()) return nullptr;
        return pick_weighted(pool);
    }

    const AssetMeta* pick_jingle(Date gday) const {
        std::time_t t = std::time(nullptr);
        std::tm* now = std::localtime(&t);
        if (!now) return nullptr;
        return pick_jingle_for_time(gday, *now);
    }

    const AssetMeta* pick_jingle_for_time(Date gday, const std::tm& local_time) const {
        std::string bucket = "day";
        if (local_time.tm_hour >= 5 && local_time.tm_hour < 12) bucket = "morning";
        else if (local_time.tm_hour >= 18 || local_time.tm_hour < 5) bucket = "night";

        std::vector<std::pair<const AssetMeta*, int>> pool;
        
        // Add normal jingles
        for (size_t i = 0; i < bin_jingle_rules_count; ++i) {
            const auto& rule = bin_jingle_rules[i];
            if (rule.event_id != EventId::None) continue;
            
            if (!matches_bucket(rule.music_no, bucket)) continue;
            const AssetMeta* asset = jingle_asset_for(rule.music_no);
            if (!asset) continue;
            // Some catalog entries are placeholders with size==0 (not present in blob); skip them.
            if (asset->size < 96) continue;
            pool.push_back({asset, std::max(1, rule.rate)});
        }

        // Add event jingles
        auto weights = jingle_id_weights(gday);
        for (const auto& entry : weights) {
            // Find all rules matching this event
            for (size_t i = 0; i < bin_jingle_rules_count; ++i) {
                const auto& rule = bin_jingle_rules[i];
                if (rule.event_id == entry.first) {
                    const AssetMeta* asset = jingle_asset_for(rule.music_no);
                    if (asset && asset->size >= 96) {
                        pool.push_back({asset, std::max(1, entry.second)});
                    }
                }
            }
        }

        if (pool.empty()) return nullptr;
        return pick_weighted(pool);
    }
};

class TempoMap {
public:
    struct Alignment {
        float lead_in_sec = 0.0f;
        float beat_len_sec = 0.0f;
        float bar_len_sec = 0.0f;
        bool valid = false;
    };

    Alignment get_alignment(int music_no, float loop_start) const {
        Alignment info;
        const TempoEntry* entry = nullptr;
        if (music_no >= 0 && music_no < 1024) {
            int16_t idx = tempo_lookup[music_no];
            if (idx >= 0 && idx < (int)bin_tempo_count) {
                entry = &bin_tempo_entries[idx];
            }
        }

        if (!entry) return info;
        info.beat_len_sec = entry->beat_len;
        info.bar_len_sec = entry->beat_len * 4.0f;
        float song_start = compute_song_start(loop_start, entry->tempo_class, entry->unique_id);
        info.lead_in_sec = std::max(0.0f, loop_start - song_start);
        info.valid = true;
        return info;
    }
};

std::vector<size_t> build_song_rotation_indices() {
    std::vector<size_t> indices;
    if (bin_songs_count <= 1) return indices;
    indices.reserve(bin_songs_count - 1);
    for (size_t i = 1; i < bin_songs_count; ++i) {
        indices.push_back(i);
    }
    return indices;
}

float clamp_delay(float d) {
    if (d > 0.0f && d < OVERLAY_MIN_DELAY) d = OVERLAY_MIN_DELAY;
    if (d > OVERLAY_MAX_DELAY) d = OVERLAY_MAX_DELAY;
    if (d < 0.0f) d = 0.0f;
    return d;
}

float snap_delay_to_grid(float residual, float grid) {
    float r = residual;
    if (r < 0.0f && grid > 0.0f && OVERLAY_ALLOW_WRAP) {
        int k = (int)std::ceil(-r / grid);
        r += k * grid;
    }
    r = std::max(0.0f, r);
    if (grid <= 0.0f) {
        return clamp_delay(r);
    }
    int j_low = (r > OVERLAY_MAX_DELAY) ? (int)std::ceil((r - OVERLAY_MAX_DELAY) / grid) : 0;
    int j_high = (r >= OVERLAY_MIN_DELAY) ? (int)std::floor((r - OVERLAY_MIN_DELAY) / grid) : 0;
    float d = clamp_delay(r);
    if (j_low <= j_high) {
        bool prefer_latest = (grid >= OVERLAY_LATEST_IF_GRID);
        if (prefer_latest) {
            d = r - j_low * grid;
        } else {
            float target = std::fmod(r, grid);
            int best_j = j_low;
            float best_err = std::fabs((r - j_low * grid) - target);
            if (j_high != j_low) {
                float err_high = std::fabs((r - j_high * grid) - target);
                if (err_high < best_err) {
                    best_j = j_high;
                    best_err = err_high;
                }
            }
            if (j_high - j_low > 1) {
                int mid = (j_low + j_high) / 2;
                float err_mid = std::fabs((r - mid * grid) - target);
                if (err_mid < best_err) {
                    best_j = mid;
                }
            }
            d = r - best_j * grid;
        }
    }
    return clamp_delay(d);
}

float compute_intro_first_song_delay(float intro_sec, float lead_in_sec, float beat_len, float bar_len) {
    float grid = (bar_len > 0.0f) ? bar_len : beat_len;
    float residual = intro_sec - lead_in_sec;
    return snap_delay_to_grid(residual, grid);
}

float compute_song_first_intro_delay(float loop_start_sec, float lead_in_sec, float intro_sec, float beat_len, float bar_len) {
    float grid = (bar_len > 0.0f) ? bar_len : beat_len;
    float residual = loop_start_sec - intro_sec;
    return snap_delay_to_grid(residual, grid);
}

// ---------------------------------------------------------------------------
// Audio Core
// ---------------------------------------------------------------------------
struct AudioCtx {
    const uint8_t* raw_data;
    size_t raw_len;
    const char* name;
    const char* title;
    int id;
    uint32_t rate;
    uint32_t total_samples;
    uint32_t start_offset;
    uint32_t loops;
    uint32_t loop_end;
    uint32_t loop_start;
    int16_t  hist1;
    int16_t  hist2;
    std::vector<std::pair<int16_t, int16_t>> coeffs;
};

class BWAVParser {
public:
    static std::shared_ptr<AudioCtx> parse(const AssetMeta& meta) {
        const uint8_t* data = assets_blob + meta.offset;
        size_t len = meta.size;

        if (len < 96 || std::memcmp(data, "BWAV", 4) != 0) return nullptr;
        uint16_t bom = read_u16(data + 4);
        uint16_t chans = read_u16(data + 14);
        if (chans != 1 || bom != 0xFEFF) return nullptr;

        const uint8_t* ci = data + 16;
        
        uint32_t rate = read_u32(ci + 4);
        uint32_t total = read_u32(ci + 8);
        
        std::vector<std::pair<int16_t, int16_t>> coeffs;
        for(int i=0; i<8; ++i) {
            int off = 16 + (i*4);
            coeffs.push_back({read_s16(ci + off), read_s16(ci + off + 2)});
        }

        uint32_t start  = read_u32(ci + 52); 
        uint32_t loops  = read_u32(ci + 56);
        uint32_t lend   = read_u32(ci + 60);
        uint32_t lstart = read_u32(ci + 64);
        
        int16_t h1      = read_s16(ci + 70); 
        int16_t h2      = read_s16(ci + 72);

        // Update UI State
        {
            std::lock_guard<std::mutex> lock(g_ui_state.mut);
            g_ui_state.current_bwav.name = meta.name;
            g_ui_state.current_bwav.rate = rate;
            g_ui_state.current_bwav.len = total;
            g_ui_state.current_bwav.loop_start = lstart;
            g_ui_state.current_bwav.loop_end = lend;
            g_ui_state.current_bwav.h1 = h1;
            g_ui_state.current_bwav.h2 = h2;
            g_ui_state.current_bwav.valid = true;
        }

        debug_printf(C_YELLOW "[BWAV] " C_CYAN "%s" C_RESET " | Len: " C_GREEN "%u" C_RESET " | Loop: " C_GREEN "%u" C_RESET "-" C_GREEN "%u" C_RESET " | Hist: " C_GREEN "%d/%d" C_RESET "\n", 
               meta.name, total, lstart, lend, h1, h2);

        if (start >= len) return nullptr;

        auto ctx = std::make_shared<AudioCtx>();
        ctx->raw_data = data + start;
        ctx->raw_len = len - start;
        ctx->name = meta.name;
        ctx->title = meta.title;
        ctx->id = meta.id;
        ctx->rate = rate;
        ctx->total_samples = total;
        ctx->start_offset = start;
        ctx->loops = loops;
        ctx->loop_end = lend;
        ctx->loop_start = lstart;
        ctx->hist1 = h1;
        ctx->hist2 = h2;
        ctx->coeffs = coeffs;
        return ctx;
    }
};

class Decoder {
    std::shared_ptr<AudioCtx> ctx;
    size_t ofs;     
    int16_t h1, h2;
    size_t sample_pos;
    bool looped;
    size_t post_loop_pos;
    size_t play_lim;
    size_t fade_lim;
    bool fading_enabled;
    bool oneshot_forced;

public:
   Decoder(std::shared_ptr<AudioCtx> c, bool oneshot, float fade_play_s, float fade_time_s) 
       : ctx(c), ofs(0), h1(c->hist1), h2(c->hist2),
         sample_pos(0), looped(false), post_loop_pos(0), oneshot_forced(oneshot)
   {
        if (!oneshot && fade_play_s > 0) {
            fading_enabled = true;
            play_lim = (size_t)(fade_play_s * ctx->rate);
            fade_lim = (size_t)(fade_time_s * ctx->rate);
        } else {
            fading_enabled = false;
            play_lim = 0; fade_lim = 0;
        }
    }

    bool has_looped() const { return looped; }

    bool is_exhausted() const {
        if (fading_enabled && looped && post_loop_pos >= play_lim + fade_lim) return true;
        if (!looped && ofs >= ctx->raw_len) {
            if (oneshot_forced || ctx->loop_end == 0 || ctx->loop_start == 0xFFFFFFFF) return true;
        }
        return false;
    }

   int decode_block(int16_t* out_14) {
       bool is_loopable = !oneshot_forced && (ctx->loop_end > 0) && (ctx->loop_start != 0xFFFFFFFF);
       bool hit_loop = is_loopable && (sample_pos >= ctx->loop_end);
       
       if (ofs >= ctx->raw_len && is_loopable) hit_loop = true;
       
       int skip_samples = 0;

       if (hit_loop) {
          debug_printf(C_MAGENTA "[LOOP] " C_RESET "Hit Loop: " C_GREEN "%zu" C_RESET "\n", sample_pos);
           size_t blk_idx = ctx->loop_start / BLK_SAMP;
           ofs = blk_idx * BLK_BYTES;
           
           h1 = ctx->hist1;
           h2 = ctx->hist2;

           skip_samples = ctx->loop_start % BLK_SAMP;

           debug_printf(C_MAGENTA "[LOOP] " C_RESET "Jump: " C_GREEN "%zu" C_RESET " | Blk: %zu | Skip: %d | Hist: " C_GREEN "%d/%d" C_RESET "\n", 
                  (size_t)ctx->loop_start, blk_idx, skip_samples, h1, h2);

           if (!looped) {
               looped = true;
               post_loop_pos = 0;
           }
           
           sample_pos = ctx->loop_start;
       }

       if (ofs >= ctx->raw_len) {
           if (!is_loopable) return 0;
       }

       uint8_t hdr = ctx->raw_data[ofs++];
       int scale = 1 << (hdr & 0x0F);
       int c_idx = (hdr >> 4) & 0x7; 
       
       int16_t c1 = ctx->coeffs[c_idx].first;
       int16_t c2 = ctx->coeffs[c_idx].second;

       int out_idx = 0;

       auto process_sample = [&](int n, int sample_index_in_block) -> bool {
           if (sample_index_in_block < skip_samples) return false;

           // Branchless sign extension: (n >= 8) ? n - 16 : n
           int sn = n - ((n & 8) << 1);
           
           int pred = ((sn * scale) << 11) + ((int)c1 * h1 + (int)c2 * h2) + 1024;
           int16_t samp = clamp16(pred >> 11);
           h2 = h1; h1 = samp;

           int16_t out_samp = samp;

           if (looped) {
               if (fading_enabled) {
                   if (post_loop_pos >= play_lim + fade_lim) {
                       while(out_idx < 14) out_14[out_idx++] = 0;
                       return true; 
                   }
                   if (post_loop_pos >= play_lim) {
                       size_t fade_idx = post_loop_pos - play_lim;
                       int64_t s64 = (int64_t)out_samp;
                       int64_t num = (int64_t)(fade_lim - fade_idx);
                       int64_t den = (int64_t)fade_lim;
                       if (den > 0) {
                           out_samp = (int16_t)((s64 * num) / den);
                       }
                   }
               }
               post_loop_pos++;
           }
           out_14[out_idx++] = out_samp;
           sample_pos++;
           return false;
       };

       for (int i = 0; i < 7; ++i) {
           if (is_loopable && sample_pos >= ctx->loop_end) return out_idx;

           if (ofs >= ctx->raw_len) return out_idx;
           uint8_t b = ctx->raw_data[ofs++];
           
           // High nibble (first sample)
           if (process_sample((b >> 4) & 0xF, i * 2)) return out_idx;

           if (is_loopable && sample_pos >= ctx->loop_end) return out_idx;

           // Low nibble (second sample)
           if (process_sample(b & 0xF, i * 2 + 1)) return out_idx;
       }
       return out_idx;
   }
};

class ITrack { public: virtual ~ITrack() = default; virtual bool done() = 0; virtual void pull(int16_t* out, int frames) = 0; virtual void set_vol(float v) = 0; virtual void ramp_to(float t, int f) = 0; virtual std::shared_ptr<Decoder> get_decoder() { return nullptr; } };

class Track : public ITrack {
   std::shared_ptr<Decoder> dec;
   int vol_q15;
   int ramp_total_samples;
   int ramp_remaining_samples;
   int ramp_start_q15;
   int ramp_target_q15;
   int delay_left;
   bool is_done, announced;
   std::string title;
   int16_t spill_buf[14]; int spill_idx, spill_avail;
public:
   Track(std::shared_ptr<AudioCtx> ctx, float vol, int delay_samps, std::string t, bool oneshot, float fp, float ft)
       : vol_q15(volume_to_q15(vol)),
         ramp_total_samples(0),
         ramp_remaining_samples(0),
         ramp_start_q15(volume_to_q15(vol)),
         ramp_target_q15(volume_to_q15(vol)),
         delay_left(delay_samps),
         is_done(false),
         announced(false),
         spill_idx(0),
         spill_avail(0) 
    {
        if (ctx->title) title = std::string(ctx->title);
        
        dec = std::make_shared<Decoder>(ctx, oneshot, fp, ft);
        ramp_target_q15 = vol_q15;
    }
    std::shared_ptr<Decoder> get_decoder() override { return dec; }
    bool done() override { return is_done; }
   void set_vol(float v) override {
       vol_q15 = volume_to_q15(v);
       ramp_remaining_samples = 0;
       ramp_total_samples = 0;
       ramp_start_q15 = vol_q15;
       ramp_target_q15 = vol_q15;
   }
   void ramp_to(float t, int f) override {
       int tgt = volume_to_q15(t);
       ramp_target_q15 = tgt;
       if (f <= 0) {
           vol_q15 = tgt;
           ramp_remaining_samples = 0;
           ramp_total_samples = 0;
           ramp_start_q15 = tgt;
           return;
       }
       ramp_start_q15 = vol_q15;
       ramp_total_samples = f;
       ramp_remaining_samples = f;
   }
    void pull(int16_t* out, int frames) override {
        if (is_done) return;

        int p = 0;
        if (delay_left > 0) { int take = std::min(delay_left, frames); delay_left -= take; p += take; }

        if (p < frames && !announced && !title.empty()) { 
            std::string msg = C_BOLD "Now Playing: " C_RESET + title;
            g_log_buffer.add(msg);
            
            {
                std::lock_guard<std::mutex> lock(g_ui_state.mut);
                g_ui_state.now_playing = title;
                g_ui_state.status_text = "LIVE";
                g_ui_state.status_color = C_GREEN;
            }
            announced = true; 
            g_tui.render();
        }

        while (p < frames) {
            while (spill_idx < spill_avail && p < frames) {
               int32_t s = spill_buf[spill_idx++];
               int current_vol = vol_q15;
               if (ramp_remaining_samples > 0) {
                   int elapsed = ramp_total_samples - ramp_remaining_samples;
                   if (elapsed > 0) {
                       int64_t delta = (int64_t)(ramp_target_q15 - ramp_start_q15) * elapsed;
                       current_vol = ramp_start_q15 + (int)(delta / ramp_total_samples);
                   } else {
                       current_vol = ramp_start_q15;
                   }
                   ramp_remaining_samples--;
                   if (ramp_remaining_samples == 0) {
                       vol_q15 = ramp_target_q15;
                       current_vol = ramp_target_q15;
                   } else {
                       vol_q15 = current_vol;
                   }
               }
               s = (s * current_vol) >> 15;
               out[p++] = (int16_t)s;
            }
           if (p >= frames) break;
           if (dec->is_exhausted()) { is_done = true; break; }
           int produced = dec->decode_block(spill_buf);
           if (produced == 0) { is_done = true; break; }
           spill_idx = 0; spill_avail = produced;
       }
    }
};

class SilenceTrack : public ITrack {
    int remaining; bool is_done;
public:
    SilenceTrack(float dur) : remaining((int)(dur * (float)engine_sample_rate())), is_done(false) {}
    bool done() override { return is_done; }
    void pull(int16_t* out, int frames) override { if (remaining <= 0) { is_done = true; return; } remaining -= frames; if (remaining <= 0) is_done = true; }
    void set_vol(float) override {}
    void ramp_to(float, int) override {}
};

// ---------------------------------------------------------------------------
// Web Infrastructure
// ---------------------------------------------------------------------------
#ifndef MSG_NOSIGNAL
#define MSG_NOSIGNAL 0
#endif

void ignore_sigpipe() {
#ifdef SIGPIPE
    std::signal(SIGPIPE, SIG_IGN);
#endif
}

class Mixer {
    std::vector<std::shared_ptr<ITrack>> tracks;
    mutable std::mutex mut;
    std::vector<int32_t> mix_buf;
    std::vector<int16_t> tr_buf;

public:
    Mixer() {
        mix_buf.resize(MIX_BLOCK_FRAMES);
        tr_buf.resize(MIX_BLOCK_FRAMES);
    }

    void start() {}
    void stop() {}

    void mix_block(int16_t* out, int frames) {
        std::lock_guard<std::mutex> lock(mut);
        if ((int)mix_buf.size() < frames) {
            mix_buf.resize(frames);
            tr_buf.resize(frames);
        }
        std::fill(mix_buf.begin(), mix_buf.begin() + frames, 0);
        tracks.erase(std::remove_if(tracks.begin(), tracks.end(),
                                    [](const std::shared_ptr<ITrack>& t) { return t->done(); }),
                     tracks.end());
        if (tracks.empty()) {
            for (int i = 0; i < frames; ++i) {
                out[i * 2] = 0;
                out[i * 2 + 1] = 0;
            }
            return;
        }
        for (auto& tr : tracks) {
            std::fill(tr_buf.begin(), tr_buf.begin() + frames, 0);
            tr->pull(tr_buf.data(), frames);
            for (int i = 0; i < frames; ++i) {
                mix_buf[i] += tr_buf[i];
            }
        }
        for (int i = 0; i < frames; ++i) {
            int32_t s = (mix_buf[i] * MASTER_GAIN_Q15) >> 15;
            int16_t sample = clamp16(s);
            out[i * 2] = sample;
            out[i * 2 + 1] = sample;
        }
    }

    std::shared_ptr<ITrack> add(std::shared_ptr<AudioCtx> ctx, float vol = 1.0f, float delay = 0.0f,
                                bool oneshot = false, float fp = FADE_PLAY_DURATION, float ft = FADE_TIME_DURATION) {
        auto tr = std::make_shared<Track>(ctx, vol, (int)(delay * (float)engine_sample_rate()), "Track", oneshot, fp, ft);
        std::lock_guard<std::mutex> lock(mut);
        tracks.push_back(tr);
        return tr;
    }

    std::shared_ptr<ITrack> add_silence(float dur) {
        auto tr = std::make_shared<SilenceTrack>(dur);
        std::lock_guard<std::mutex> lock(mut);
        tracks.push_back(tr);
        return tr;
    }

    void ramp(const std::shared_ptr<ITrack>& tr, float t, float d) {
        if (!tr) return;
        std::lock_guard<std::mutex> lock(mut);
        if (tr->done()) return;
        tr->ramp_to(t, (int)(d * (float)engine_sample_rate()));
    }
};

class DacOutput {
    ma_device device{};
    ma_pcm_rb ring{};
    ma_context context{};
    bool context_ready = false;
    bool ring_ready = false;
    ma_uint32 ring_capacity_frames = 0;
    bool have_device_id = false;
    ma_device_id device_id{};
    std::atomic<bool> ready{false};
    std::atomic<bool> started{false};
    std::atomic<int64_t> last_overflow_log_ns{0};
    std::atomic<int64_t> last_underflow_log_ns{0};
    std::atomic<uint32_t> device_sample_rate{(uint32_t)SAMPLE_RATE};
    int hardware_gain_q15 = 32768;
    bool hardware_gain_unity = true;

    void log_playback_devices(const char* reason) {
        if (!context_ready) return;

        ma_device_info* playback_infos = nullptr;
        ma_uint32 playback_count = 0;
        if (ma_context_get_devices(&context, &playback_infos, &playback_count, nullptr, nullptr) != MA_SUCCESS) {
            log_error("[DAC] Failed to enumerate audio devices.");
            return;
        }

        log_info_f(C_BLUE "[DAC] " C_RESET "%s (%u playback devices)",
                   (reason ? reason : "Playback devices"),
                   (unsigned)playback_count);

        for (ma_uint32 i = 0; i < playback_count; ++i) {
            const char* name = playback_infos[i].name;
            log_info_f(C_BLUE "[DAC] " C_RESET "  [%u] %s", (unsigned)i, (name ? name : "(unknown)"));
        }
    }

    bool select_device_index(int idx) {
        if (!context_ready || idx < 0) return false;

        ma_device_info* playback_infos = nullptr;
        ma_uint32 playback_count = 0;
        if (ma_context_get_devices(&context, &playback_infos, &playback_count, nullptr, nullptr) != MA_SUCCESS) {
            log_error("[DAC] Failed to enumerate audio devices.");
            return false;
        }
        if (playback_count == 0) return false;
        if ((ma_uint32)idx >= playback_count) return false;

        device_id = playback_infos[(ma_uint32)idx].id;
        have_device_id = true;
        debug_printf(C_BLUE "[DAC] " C_RESET "Selected device index %d: " C_BOLD "%s" C_RESET "\n",
                     idx,
                     (playback_infos[(ma_uint32)idx].name ? playback_infos[(ma_uint32)idx].name : "(unknown)"));
        return true;
    }

    bool auto_select_device() {
        if (!context_ready) return false;

        ma_device_info* playback_infos = nullptr;
        ma_uint32 playback_count = 0;
        if (ma_context_get_devices(&context, &playback_infos, &playback_count, nullptr, nullptr) != MA_SUCCESS) {
            log_error("[DAC] Failed to enumerate audio devices.");
            return false;
        }
        if (playback_count == 0) return false;

        // Prefer dedicated DAC/HAT/USB devices over HDMI sinks.
        // Ordering matters: the first match wins.
        const std::initializer_list<const char*> preferred = {
            "snd_rpi", "rpi-dac", "rpi dac", "pcm1794", "pcm512", "pcm510",
            "hifiberry", "audioinjector", "iqaudio", "allo",
            "usb", "headphone",
            "i2s", "dac", "digi", "bcm2835"
        };
        const std::initializer_list<const char*> avoid = {"dummy", "null", "monitor", "loopback", "hdmi", "vc4"};

        for (const char* needle_c : preferred) {
            const std::string needle = to_lower_copy(std::string(needle_c));
            for (ma_uint32 i = 0; i < playback_count; ++i) {
                const char* raw = playback_infos[i].name;
                const std::string name = raw ? to_lower_copy(std::string(raw)) : std::string();
                if (name.empty()) continue;
                bool bad = false;
                for (const char* a : avoid) {
                    if (name.find(a) != std::string::npos) {
                        bad = true;
                        break;
                    }
                }
                if (bad) continue;
                if (name.find(needle) != std::string::npos) {
                    device_id = playback_infos[i].id;
                    have_device_id = true;
                    debug_printf(C_BLUE "[DAC] " C_RESET "Auto-selected device: " C_BOLD "%s" C_RESET "\n",
                                 (raw ? raw : "(unknown)"));
                    return true;
                }
            }
        }

        return false;
    }

    static void data_callback(ma_device* dev, void* output, const void*, ma_uint32 frameCount) {
        auto* self = reinterpret_cast<DacOutput*>(dev->pUserData);
        if (!self) {
            std::memset(output, 0, frameCount * CHANNELS * sizeof(int16_t));
            return;
        }
        self->pull_audio(static_cast<int16_t*>(output), frameCount);
    }

    void pull_audio(int16_t* out, ma_uint32 frameCount) {
        ma_uint32 remaining = frameCount;
        size_t offset_frames = 0;
        while (remaining > 0) {
            ma_uint32 chunk = remaining;
            void* src = nullptr;
            if (ma_pcm_rb_acquire_read(&ring, &chunk, &src) != MA_SUCCESS || chunk == 0) {
                // Underrun: the device asked for audio but the ring was empty.
                // This manifests as periodic stutter/clicks. Log at a low rate.
                const int64_t now_ns = std::chrono::duration_cast<std::chrono::nanoseconds>(
                                            std::chrono::steady_clock::now().time_since_epoch())
                                            .count();
                const int64_t prev_ns = last_underflow_log_ns.load();
                if (prev_ns == 0 || (now_ns - prev_ns) > 2'000'000'000LL) {
                    last_underflow_log_ns.store(now_ns);
                    log_error("[DAC] Ring buffer underrun; outputting silence (stutter). Increase buffering or reduce load.");
                }
                std::memset(out + offset_frames * CHANNELS, 0,
                            remaining * CHANNELS * sizeof(int16_t));
                break;
            }
            std::memcpy(out + offset_frames * CHANNELS, src, chunk * CHANNELS * sizeof(int16_t));
            ma_pcm_rb_commit_read(&ring, chunk);
            offset_frames += chunk;
            remaining -= chunk;
        }
    }

    bool init_ring() {
        if (ring_ready) return true;
        ma_result res = ma_pcm_rb_init(ma_format_s16, CHANNELS,
                                       static_cast<ma_uint32>(DAC_BUFFER_FRAMES),
                                       nullptr, nullptr, &ring);
        if (res != MA_SUCCESS) {
            std::cerr << C_RED << "[DAC] Failed to allocate ring buffer (" << (int)res << ")." << C_RESET << std::endl;
            return false;
        }
        ring_ready = true;
        ring_capacity_frames = static_cast<ma_uint32>(DAC_BUFFER_FRAMES);
        return true;
    }

    void uninit_ring() {
        if (ring_ready) {
            ma_pcm_rb_uninit(&ring);
            ring_ready = false;
        }
    }

    bool init_context() {
        if (context_ready) return true;

        ma_result res = MA_ERROR;
#if defined(__linux__)
        // Prefer ALSA on the Pi so I2S/HAT DACs show up reliably.
        ma_backend backends[] = { ma_backend_alsa };
        res = ma_context_init(backends, (ma_uint32)(sizeof(backends) / sizeof(backends[0])), nullptr, &context);
        if (res != MA_SUCCESS) {
            // Fall back to miniaudio's default backend selection.
            res = ma_context_init(nullptr, 0, nullptr, &context);
        }
#else
        res = ma_context_init(nullptr, 0, nullptr, &context);
#endif

        if (res != MA_SUCCESS) {
            std::cerr << C_RED << "[DAC] Failed to initialize audio context (" << (int)res << ")." << C_RESET << std::endl;
            return false;
        }
        context_ready = true;
        return true;
    }

    void uninit_context() {
        if (context_ready) {
            ma_context_uninit(&context);
            context_ready = false;
        }
    }

    bool select_device(const std::string& needle) {
        if (!context_ready) return false;
        std::string search = to_lower_copy(trim(needle));
        if (search.empty()) return false;

        ma_device_info* playback_infos = nullptr;
        ma_uint32 playback_count = 0;
        if (ma_context_get_devices(&context, &playback_infos, &playback_count, nullptr, nullptr) != MA_SUCCESS) {
            std::cerr << C_RED << "[DAC] Failed to enumerate audio devices." << C_RESET << std::endl;
            return false;
        }

        for (ma_uint32 i = 0; i < playback_count; ++i) {
            const char* raw_name = playback_infos[i].name;
            std::string dev_name = raw_name ? to_lower_copy(std::string(raw_name)) : "";
            if (dev_name.find(search) != std::string::npos) {
                device_id = playback_infos[i].id;
                have_device_id = true;
                debug_printf(C_BLUE "[DAC] " C_RESET "Selected device: " C_BOLD "%s" C_RESET "\n",
                          (raw_name ? raw_name : "(unknown)"));
                return true;
            }
        }

        log_error(std::string("[DAC] No device matched \"") + needle + "\".");
        return false;
    }

public:
    explicit DacOutput(int gain_q15 = 32768)
        : hardware_gain_q15(gain_q15),
          hardware_gain_unity(gain_q15 >= 32768) {}
    ~DacOutput() { shutdown(); }

    bool init_from_env() {
        if (env_truthy(std::getenv("ACNH_DISABLE_DAC"))) {
            log_info(C_BLUE "[DAC] " C_RESET "Disabled via ACNH_DISABLE_DAC.");
            return false;
        }

        // Always try to initialize a context so we can reliably enumerate/select devices.
        // If it fails, we can still attempt ma_device_init() with a null context.
        init_context();

        if (context_ready && (is_debug_enabled() || env_truthy(std::getenv("ACNH_LIST_AUDIO_DEVICES")))) {
            log_playback_devices("Available playback devices");
        }

        if (context_ready) {
            if (const char* idx_text = std::getenv("ACNH_AUDIO_DEVICE_INDEX")) {
                char* end = nullptr;
                long idx = std::strtol(idx_text, &end, 10);
                if (end && *end == '\0' && idx >= 0 && idx <= 1024) {
                    if (!select_device_index((int)idx)) {
                        log_error(std::string("[DAC] ACNH_AUDIO_DEVICE_INDEX did not match a playback device: ") + idx_text);
                    }
                } else {
                    log_error(std::string("[DAC] Invalid ACNH_AUDIO_DEVICE_INDEX: ") + idx_text);
                }
            }
        }

        const char* device_hint = std::getenv("ACNH_AUDIO_DEVICE");
        if (device_hint && *device_hint) {
            if (context_ready) {
                select_device(device_hint);
            }
        } else {
            // If no explicit hint is provided, try to pick a likely Pi DAC device (I2S/HAT/USB) instead
            // of whatever the system default is (often HDMI or a null sink).
            if (context_ready && !have_device_id) {
                auto_select_device();
            }
        }

        if (!init_ring()) {
            uninit_context();
            return false;
        }

        ma_device_config cfg = ma_device_config_init(ma_device_type_playback);
        cfg.playback.format = ma_format_s16;
        cfg.playback.channels = CHANNELS;
        cfg.sampleRate = (ma_uint32)engine_sample_rate();
        cfg.periodSizeInFrames = MIX_BLOCK_FRAMES;
        // A bit more buffering reduces the odds of ALSA xruns on a busy Pi.
        cfg.periods = 6;
        cfg.dataCallback = data_callback;
        cfg.pUserData = this;
        if (have_device_id) {
            cfg.playback.pDeviceID = &device_id;
        }

        ma_context* ctx_ptr = context_ready ? &context : nullptr;
        ma_result dev_res = ma_device_init(ctx_ptr, &cfg, &device);
        if (dev_res != MA_SUCCESS) {
            char buf[128];
            snprintf(buf, sizeof(buf), "[DAC] Failed to initialize playback device (%d).", (int)dev_res);
            log_error(buf);
            uninit_ring();
            uninit_context();
            return false;
        }

        device_sample_rate.store((uint32_t)device.sampleRate);
        ready.store(true);
        {
            char msg[160];
            snprintf(msg, sizeof(msg),
                     C_BLUE "[DAC] " C_RESET "Hardware playback ready. Requested=%dHz, Actual=%dHz, Period=%d frames, Periods=%d",
                     (int)cfg.sampleRate, (int)device.sampleRate, (int)cfg.periodSizeInFrames, (int)cfg.periods);
            log_info(msg);
        }
        return true;
    }

    bool is_ready() const { return ready.load(); }
    uint32_t sample_rate() const { return device_sample_rate.load(); }

    uint32_t buffered_frames() const {
        if (!ring_ready) return 0;
        return static_cast<uint32_t>(ma_pcm_rb_available_read(const_cast<ma_pcm_rb*>(&ring)));
    }

    uint32_t capacity_frames() const {
        return static_cast<uint32_t>(ring_capacity_frames);
    }

    void start() {
        if (!is_ready() || started.load()) return;
        if (ma_device_start(&device) == MA_SUCCESS) {
            started.store(true);
        } else {
            log_error("[DAC] Failed to start playback device.");
        }
    }

    void stop() {
        if (!started.load()) return;
        ma_device_stop(&device);
        started.store(false);
    }

   void write(const int16_t* frames, size_t frameCount) {
        if (!is_ready() || !frames || frameCount == 0) return;
        size_t processed = 0;
        while (processed < frameCount) {
            ma_uint32 chunk = static_cast<ma_uint32>(std::min<size_t>(frameCount - processed, DAC_BUFFER_FRAMES));
            void* dst = nullptr;
            ma_result r = ma_pcm_rb_acquire_write(&ring, &chunk, &dst);
            if (r != MA_SUCCESS || chunk == 0) {
                // Backpressure: if the DAC isn't consuming fast enough (or we had a scheduling hiccup),
                // throttle the producer instead of dropping samples (which audibly cuts/warps intros).
                const int64_t now_ns = std::chrono::duration_cast<std::chrono::nanoseconds>(
                                            std::chrono::steady_clock::now().time_since_epoch())
                                            .count();
                const int64_t prev_ns = last_overflow_log_ns.load();
                // Rate-limit: persistent backpressure can otherwise spam journald.
                if (prev_ns == 0 || (now_ns - prev_ns) > 5'000'000'000LL) {
                    last_overflow_log_ns.store(now_ns);
                    log_error("[DAC] Ring buffer full; throttling producer to avoid dropping samples.");
                }
                if (!started.load()) break;

                // Sleep roughly for a small fraction of a block to give the callback time to drain.
                const uint32_t sr = std::max<uint32_t>(1u, sample_rate());
                const int wait_frames = std::max<int>(1, MIX_BLOCK_FRAMES / 8);
                const int64_t wait_us = (int64_t)wait_frames * 1000000LL / (int64_t)sr;
                std::this_thread::sleep_for(std::chrono::microseconds(std::max<int64_t>(200, wait_us)));
                continue;
            }
           int16_t* dst_samples = static_cast<int16_t*>(dst);
           const int16_t* src_samples = frames + processed * CHANNELS;
           if (hardware_gain_unity) {
               std::memcpy(dst_samples, src_samples, chunk * CHANNELS * sizeof(int16_t));
           } else {
               size_t total = chunk * CHANNELS;
               for (size_t i = 0; i < total; ++i) {
                   int32_t scaled = (int32_t)src_samples[i] * hardware_gain_q15;
                   dst_samples[i] = clamp16((int)(scaled >> 15));
               }
           }
            ma_pcm_rb_commit_write(&ring, chunk);
            processed += chunk;
        }
        if (processed == frameCount) last_overflow_log_ns.store(0);
    }

    void shutdown() {
        stop();
        if (is_ready()) {
            ma_device_uninit(&device);
            ready.store(false);
        }
        uninit_ring();
        uninit_context();
        last_overflow_log_ns.store(0);
        last_underflow_log_ns.store(0);
        have_device_id = false;
    }
};

class AudioPump {
    Mixer& mixer;
    PCMBuffer& buffer;
    DacOutput* dac;
    std::atomic<bool> running;
    std::thread worker;

public:
    AudioPump(Mixer& m, PCMBuffer& b, DacOutput* d = nullptr)
        : mixer(m), buffer(b), dac(d), running(false) {}

    void start() {
        if (running.exchange(true)) return;
        worker = std::thread(&AudioPump::run, this);

        // If we have a real DAC, prefill a little audio into the ring buffer *before*
        // starting the hardware device. This avoids an immediate underrun at startup
        // (audible click/stutter) when systemd restarts the service.
        if (dac && dac->is_ready()) {
            const uint32_t prefill_frames = (uint32_t)MIX_BLOCK_FRAMES * 16u; // ~150ms @48k
            auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(600);
            while (running.load() && std::chrono::steady_clock::now() < deadline) {
                if (dac->buffered_frames() >= prefill_frames) break;
                std::this_thread::sleep_for(std::chrono::milliseconds(5));
            }
            dac->start();
        }
    }

    void stop() {
        if (!running.exchange(false)) {
            buffer.close();
            if (dac && dac->is_ready()) {
                dac->stop();
            }
            return;
        }
        if (worker.joinable()) worker.join();
        buffer.close();
        if (dac && dac->is_ready()) {
            dac->stop();
        }
    }

private:
    void run() {
        using clock = std::chrono::steady_clock;
        const bool have_dac = (dac && dac->is_ready());
        const uint32_t sr = have_dac ? std::max<uint32_t>(1u, dac->sample_rate()) : engine_sample_rate();
        const auto block_duration = std::chrono::microseconds(
            static_cast<int64_t>(MIX_BLOCK_FRAMES) * 1000000LL / (int64_t)sr);
        std::vector<int16_t> stereo_block(MIX_BLOCK_FRAMES * 2, 0);
        auto next = clock::now();
        while (running.load()) {
            mixer.mix_block(stereo_block.data(), MIX_BLOCK_FRAMES);
            buffer.write(stereo_block.data(), MIX_BLOCK_FRAMES);
            if (dac && dac->is_ready()) {
                dac->write(stereo_block.data(), MIX_BLOCK_FRAMES);
            }

            if (have_dac) {
                // Maintain a small, stable cushion in the ring buffer. If we fall behind, run a bit faster
                // (skip the sleep) until we recover; if we get too far ahead, sleep a little extra.
                const uint32_t cap = std::max<uint32_t>(1u, dac->capacity_frames());
                const uint32_t fill = dac->buffered_frames();
                // Use a larger cushion to tolerate OS scheduling jitter and reduce audible underruns.
                const uint32_t target = std::min<uint32_t>(cap - 1u, (uint32_t)MIX_BLOCK_FRAMES * 32u);  // ~300ms @48k
                const uint32_t low = std::min<uint32_t>(target, (uint32_t)MIX_BLOCK_FRAMES * 16u);
                const uint32_t high = std::min<uint32_t>(cap - 1u, (uint32_t)MIX_BLOCK_FRAMES * 48u);

                if (fill < low) {
                    // Catch up quickly; small sleep avoids busy-spin.
                    std::this_thread::sleep_for(std::chrono::microseconds(200));
                } else if (fill > high) {
                    const uint32_t extra_frames = (fill > target) ? (fill - target) : 0u;
                    const int64_t extra_us = (int64_t)extra_frames * 1000000LL / (int64_t)sr;
                    std::this_thread::sleep_for(std::chrono::microseconds(std::clamp<int64_t>(extra_us, 0, 10000)));
                } else {
                    std::this_thread::sleep_for(block_duration);
                }
            } else {
                next += block_duration;
                // If we ever fall behind (decode hiccup / OS scheduling), do NOT "catch up" by
                // mixing multiple blocks back-to-back — that can overfill downstream buffers.
                const auto now = clock::now();
                if (next < now) {
                    next = now + block_duration;
                }
                std::this_thread::sleep_until(next);
            }
        }
    }
};

// ---------------------------------------------------------------------------
// Logic
// ---------------------------------------------------------------------------
enum class OverlayPlan { NoOverlay, SongFirst, IntroFirst };

struct OverlayDecision {
    OverlayPlan plan = OverlayPlan::NoOverlay;
    float intro_delay = 0.0f;
    float song_delay = 0.0f;
    float intro_sec = 0.0f;
    float lead_in_sec = 0.0f;
    float loop_start_sec = 0.0f;
    int delta = 0;

    bool is_overlay() const { return plan != OverlayPlan::NoOverlay; }
};

const char* overlay_plan_name(OverlayPlan plan) {
    switch(plan) {
        case OverlayPlan::SongFirst: return "song_first";
        case OverlayPlan::IntroFirst: return "intro_first";
        default: return "no_overlay";
    }
}

class Radio {
    Mixer& mixer;
    CalendarManager calendar;
    TempoMap tempo;
    std::atomic<bool> stop_sig;
    std::mt19937 rng;
    std::vector<size_t> song_indices;
    std::unordered_map<const AssetMeta*, std::shared_ptr<AudioCtx>> ctx_cache;

    const AssetMeta* pick(const AssetMeta* bin, size_t count) {
        if (count == 0) return nullptr;
        std::uniform_int_distribution<size_t> dist(0, count - 1);
        return &bin[dist(rng)];
    }

    std::shared_ptr<AudioCtx> get_ctx(const AssetMeta* meta) {
        if (!meta) return nullptr;
        auto it = ctx_cache.find(meta);
        if (it != ctx_cache.end()) {
            // BWAVParser::parse() (and its [BWAV] log) only runs on first load.
            // Emit a stable log line when we reuse a cached context.
            log_info_f(C_GRAY "[CACHE] " C_RESET C_CYAN "%s" C_RESET " (id=%d, size=%u)",
                       (meta->name ? meta->name : "(null)"), meta->id, (unsigned)meta->size);
            return it->second;
        }
        auto ctx = BWAVParser::parse(*meta);
        if (ctx) ctx_cache[meta] = ctx;
        return ctx;
    }

   OverlayDecision plan_overlay(const std::shared_ptr<AudioCtx>& intro,
                                const std::shared_ptr<AudioCtx>& song) {
       OverlayDecision decision;
       if (!song) return decision;

       // Use song's sample rate as the reference timebase for sample-domain math.
       // This is what the game does in radio_pool_slot_overlay_init: all timing
       // diffs are computed as (sample_pos - playhead) / sample_rate before
       // any plan-selection bounds are applied.
       const int64_t song_rate = (song->rate > 0) ? (int64_t)song->rate : 1;

       int64_t intro_samples = 0;  // intro length expressed at song's sample rate
       if (intro) {
           const int64_t intro_rate = std::max<int64_t>(1, (int64_t)intro->rate);
           // Resample to song timebase: intro->total_samples * song_rate / intro_rate
           // No int64 overflow risk: 11 sec * 48 kHz * 48000 / 32000 ≈ 8e5, well within int64.
           intro_samples = (int64_t)intro->total_samples * song_rate / intro_rate;
           decision.intro_sec = (float)intro_samples / (float)song_rate;
       } else {
           decision.intro_sec = 0.0f;
       }

       const int64_t loop_start_samples = (int64_t)song->loop_start;
       decision.loop_start_sec = (float)loop_start_samples / (float)song_rate;

       // Early NoOverlay: no intro, or song's loop_start is less than 1 sec in.
       const int64_t one_sec_samples = song_rate;
       if (!intro || loop_start_samples < one_sec_samples) {
           decision.plan = OverlayPlan::NoOverlay;
           decision.intro_delay = 0.0f;
           decision.song_delay = DEFAULT_NO_OVERLAY_DELAY;
           return decision;
       }

       // Use the ID from AudioCtx to use the cache
       auto align = tempo.get_alignment(song->id, decision.loop_start_sec);
       decision.lead_in_sec = align.lead_in_sec;
       float beat_len = align.beat_len_sec;
       float bar_len = align.bar_len_sec;

       // === Sample-domain plan selection (bit-perfect match to game) ===
       //
       // The game's radio_pool_slot_overlay_init at 0x4565c4630 computes
       // (target_sample_pos - playhead_sample_pos) entirely in sample-position
       // integers, then divides by sample_rate only when storing the final
       // overlay_start_sec into the slot.  Plan-selection bounds are
       // therefore implicitly in the sample domain.
       //
       // The earlier ceil()-based delta math introduced up to ±1-second rounding
       // jitter at integer-second boundaries: e.g. lead_in=10.001 vs 9.999 would
       // produce different plans even though the audio difference is 2 ms.
       const int64_t pad_samples = (int64_t)std::llround((double)INTRO_ALIGN_PAD_SEC * (double)song_rate);
       const int64_t lead_in_samples = (int64_t)std::llround((double)decision.lead_in_sec * (double)song_rate);
       const int64_t intro_padded_samples = intro_samples + pad_samples;
       const int64_t delta_samples = lead_in_samples - intro_padded_samples;

       // Expose decision.delta as truncated integer seconds for debug + telemetry.
       decision.delta = (int)(delta_samples / song_rate);

       // Range-check (only when no alignment data): unchanged in semantics,
       // also lifted to sample domain for the same precision reason.
       if (!align.valid) {
            const int64_t diff_samples = loop_start_samples - intro_samples;
            const int64_t range_min_samples = (int64_t)std::llround((double)OVERLAY_RANGE_MIN * (double)song_rate);
            const int64_t range_max_samples = (int64_t)std::llround((double)OVERLAY_RANGE_MAX * (double)song_rate);
            if (diff_samples < range_min_samples || diff_samples > range_max_samples) {
               decision.plan = OverlayPlan::NoOverlay;
               decision.intro_delay = 0.0f;
               decision.song_delay = DEFAULT_NO_OVERLAY_DELAY;
               return decision;
            }
       }

       // Plan-selection bounds, lifted to sample domain.
       const int64_t sf_min_samples = (int64_t)OVERLAY_DELTA_SONG_FIRST_MIN  * song_rate;
       const int64_t sf_max_samples = (int64_t)OVERLAY_DELTA_SONG_FIRST_MAX  * song_rate;
       const int64_t if_min_samples = (int64_t)OVERLAY_DELTA_INTRO_FIRST_MIN * song_rate;
       const int64_t if_max_samples = (int64_t)OVERLAY_DELTA_INTRO_FIRST_MAX * song_rate;

       const bool song_first_allowed  = delta_samples >= sf_min_samples && delta_samples <= sf_max_samples;
       const bool intro_first_allowed = delta_samples >= if_min_samples && delta_samples <= if_max_samples;

       if (song_first_allowed) {
           decision.plan = OverlayPlan::SongFirst;
           decision.intro_delay = compute_song_first_intro_delay(decision.loop_start_sec,
                                                                 decision.lead_in_sec,
                                                                 decision.intro_sec,
                                                                 beat_len,
                                                                 bar_len);
           decision.song_delay = 0.0f;
       } else if (intro_first_allowed) {
           decision.plan = OverlayPlan::IntroFirst;
           decision.intro_delay = 0.0f;
           decision.song_delay = compute_intro_first_song_delay(decision.intro_sec,
                                                                decision.lead_in_sec,
                                                                beat_len,
                                                                bar_len);
       } else {
           decision.plan = OverlayPlan::NoOverlay;
           decision.intro_delay = 0.0f;
           decision.song_delay = DEFAULT_NO_OVERLAY_DELAY;
       }

       // (50% IntroFirst→NoOverlay coin flip removed: no game-side evidence
       //  for this random downgrade.)

       decision.intro_delay = std::max(0.0f, decision.intro_delay);
       decision.song_delay = std::max(0.0f, decision.song_delay);
       return decision;
   }

public:
    explicit Radio(Mixer& mix) : mixer(mix), stop_sig(false) {
        std::random_device rd;
        rng.seed(rd());
        song_indices = build_song_rotation_indices();
    }

    void request_stop() {
        stop_sig.store(true);
    }
    
    void wait_track(std::shared_ptr<ITrack> tr) { 
        while (!tr->done() && !stop_sig) {
             g_tui.render();
             std::this_thread::sleep_for(std::chrono::milliseconds(50));
        }
    }

   void play_intro_song(std::shared_ptr<AudioCtx> intro, std::shared_ptr<AudioCtx> song) {
       if (!song) return;
       if (!intro) {
           wait_track(mixer.add(song));
           return;
       }

       OverlayDecision decision = plan_overlay(intro, song);
       debug_printf(C_MAGENTA "[OVERLAY] " C_RESET C_BOLD "%s" C_RESET " | IntroDly: " C_GREEN "%.2f" C_RESET " | SongDly: " C_GREEN "%.2f" C_RESET " | IntroLen: " C_GREEN "%.2fs" C_RESET " | LeadIn: " C_GREEN "%.2fs" C_RESET " | Delta: " C_GREEN "%d" C_RESET "\n",
              overlay_plan_name(decision.plan),
              decision.intro_delay,
              decision.song_delay,
              decision.intro_sec,
              decision.lead_in_sec,
              decision.delta);

       mixer.start();

       if (!decision.is_overlay() && decision.song_delay > 0.0f) {
           auto intro_track = mixer.add(intro, VOL_MAX, decision.intro_delay, true);
           auto song_track = mixer.add(song, VOL_DEFAULT, decision.song_delay, false);
           wait_track(intro_track);
           wait_track(song_track);
           return;
       }

       float intro_len = decision.intro_sec;
       float intro_start = decision.intro_delay;
       float intro_end = intro_start + intro_len;
       float song_start = decision.song_delay;

       bool intro_active_at_song_start = (song_start >= intro_start) && (song_start < intro_end);
       bool intro_starts_first = (song_start > intro_start);
       float song_initial_vol = intro_active_at_song_start ? VOL_DUCK : (intro_starts_first ? VOL_DUCK : VOL_DEFAULT);

       auto song_track = mixer.add(song, song_initial_vol, song_start, false);
       auto intro_track = mixer.add(intro, VOL_MAX, intro_start, true);

       auto start_time = std::chrono::steady_clock::now();
       bool ducked = intro_active_at_song_start;
       bool unducked = false;

       while ((!song_track->done() || !intro_track->done()) && !stop_sig) {
           float t = std::chrono::duration<float>(std::chrono::steady_clock::now() - start_time).count();
           bool song_active = (t >= song_start);
           bool intro_playing = (t >= intro_start) && (t < intro_end);

           if (song_active) {
               if (intro_playing && !ducked) {
                   debug_printf(C_MAGENTA "[OVERLAY] " C_RESET "Ducking song at t=" C_GREEN "%.2f" C_RESET "\n", t);
                   mixer.ramp(song_track, VOL_DUCK, RAMP_DURATION_SEC);
                   ducked = true;
               } else if (!intro_playing && ducked && !unducked && t >= intro_end) {
                   debug_printf(C_MAGENTA "[OVERLAY] " C_RESET "Restoring song volume at t=" C_GREEN "%.2f" C_RESET "\n", t);
                   mixer.ramp(song_track, VOL_DEFAULT, RAMP_DURATION_SEC);
                   unducked = true;
               }
           }

           g_tui.render();
           std::this_thread::sleep_for(std::chrono::milliseconds(50));
       }
   }

   void play_block(size_t offset, bool use_intro) {
       if (stop_sig) return;
       const size_t pool_limit = std::min((size_t)12, song_indices.size());
       if (offset >= pool_limit) return;

       const AssetMeta* intro = use_intro ? pick(bin_intros, bin_intros_count) : nullptr;
       const AssetMeta* outro = pick(bin_outros, bin_outros_count);
       if (outro) {
           debug_printf(C_YELLOW "[RADIO] " C_RESET "Outro picked: " C_CYAN "%s" C_RESET " (size=%u)\n",
                        (outro->name ? outro->name : "(null)"), (unsigned)outro->size);
       }
       size_t chunk = std::min((size_t)4, pool_limit - offset);

       for (size_t i = 0; i < chunk && !stop_sig; ++i) {
           auto s_ctx = get_ctx(&bin_songs[song_indices[offset+i]]);
           if (!s_ctx) continue;

           if (intro && i == 0) {
               auto i_ctx = get_ctx(intro);
               if (i_ctx) play_intro_song(i_ctx, s_ctx);
               else wait_track(mixer.add(s_ctx));
               intro = nullptr; 
           } else if (i == chunk - 1 && outro) {
               if(auto o_ctx = get_ctx(outro)) {
                   auto s_tr = mixer.add(s_ctx, VOL_DEFAULT, 0, false, 7.0f, 3.0f);
                   mixer.start();
                   bool fired = false;
                   std::shared_ptr<ITrack> o_tr = nullptr;
                   while (!s_tr->done() && !stop_sig) {
                       if (s_tr->get_decoder()->has_looped() && !fired) {
                           debug_printf(C_YELLOW "[RADIO] " C_RESET "Outro start: " C_CYAN "%s" C_RESET "\n",
                                        (outro->name ? outro->name : "(null)"));
                           mixer.ramp(s_tr, VOL_DUCK, 0.25f);
                           o_tr = mixer.add(o_ctx, VOL_DEFAULT + 0.5f, 0, true);
                           fired = true;
                       }
                       std::this_thread::sleep_for(std::chrono::milliseconds(20));
                   }
                   if(o_tr) {
                       wait_track(o_tr);
                       debug_printf(C_YELLOW "[RADIO] " C_RESET "Outro end: " C_CYAN "%s" C_RESET "\n",
                                    (outro->name ? outro->name : "(null)"));
                   }
                   wait_track(mixer.add_silence(DELAY_AFTER_OUTRO));
               } else wait_track(mixer.add(s_ctx));
           } else wait_track(mixer.add(s_ctx));
       }

       if (stop_sig) return;
       Date gday = get_game_date();

       // Jingle segment (log selection + playback explicitly; BWAV logs only reflect first-time parsing)
       if (const AssetMeta* j = calendar.pick_jingle(gday)) {
           if (auto ctx = get_ctx(j)) {
               log_info_f(C_YELLOW "[RADIO] " C_RESET "Jingle picked: " C_CYAN "%s" C_RESET " (size=%u)",
                          (j->name ? j->name : "(null)"), (unsigned)j->size);
               log_info_f(C_YELLOW "[RADIO] " C_RESET "Jingle start: " C_CYAN "%s" C_RESET,
                          (j->name ? j->name : "(null)"));
               auto tr = mixer.add(ctx, VOL_MAX, 0, true);
               wait_track(tr);
               log_info_f(C_YELLOW "[RADIO] " C_RESET "Jingle end: " C_CYAN "%s" C_RESET,
                          (j->name ? j->name : "(null)"));
           } else {
               log_error(std::string("[RADIO] Jingle failed to load: ") + (j->name ? j->name : "(null)"));
           }
       }
       
       if (stop_sig) return;

       // Commercial segment (log selection + playback explicitly)
       if (const AssetMeta* c = calendar.pick_commercial(gday)) {
           if (auto ctx = get_ctx(c)) {
               log_info_f(C_YELLOW "[RADIO] " C_RESET "Commercial picked: " C_CYAN "%s" C_RESET " (size=%u)",
                          (c->name ? c->name : "(null)"), (unsigned)c->size);
               log_info_f(C_YELLOW "[RADIO] " C_RESET "Commercial start: " C_CYAN "%s" C_RESET,
                          (c->name ? c->name : "(null)"));
               auto tr = mixer.add(ctx, VOL_MAX, 0, true);
               wait_track(tr);
               log_info_f(C_YELLOW "[RADIO] " C_RESET "Commercial end: " C_CYAN "%s" C_RESET,
                          (c->name ? c->name : "(null)"));
               wait_track(mixer.add_silence(DELAY_AFTER_CM_S));
           } else {
               log_error(std::string("[RADIO] Commercial failed to load: ") + (c->name ? c->name : "(null)"));
           }
       }
   }

   void loop() {
       mixer.start();
       
       std::shuffle(song_indices.begin(), song_indices.end(), rng);
       for (size_t i = 0; i < bin_songs_count; ++i) {
           if (bin_songs[i].id == 95) { // Welcome
               auto it = std::find(song_indices.begin(), song_indices.end(), i);
               if (it != song_indices.end()) {
                   std::swap(song_indices[0], *it);
                   log_info(std::string(C_YELLOW) + "[RADIO] " + C_RESET + "First Launch: Queued " + C_BOLD + bin_songs[i].name + C_RESET + " (Index " + std::to_string(i) + ")");
               }
               break;
           }
       }

       // // Special First Block: No Intro — DISABLED 2026-05-13 so intro plays
       // //                                  before Welcome Horizons on first run.
       // //                                  Welcome Horizons is still pinned to
       // //                                  song_indices[0] by the swap above.
       // play_block(0, false);

       // // Start main rotation from second block (if any)
       // size_t offset = 4;
       size_t offset = 0;  // rotation now covers block 0 (with intro) too

       while (!stop_sig) {
           const size_t pool_limit = std::min((size_t)12, song_indices.size());
           
           // Play remaining blocks in this rotation
           for (; offset < pool_limit && !stop_sig; offset += 4) {
               play_block(offset, true);
           }

           // New Rotation
           if (stop_sig) break;
           std::shuffle(song_indices.begin(), song_indices.end(), rng);
           offset = 0;
           
           g_tui.render();
           std::this_thread::sleep_for(std::chrono::milliseconds(50));
       }
   }
};


// ============================================================================
// Jukebox entry point — forked from the radio's main() (acnh_aircheck_pi.cpp).
// Same engine (BWAVParser -> Decoder -> Track), no Radio scheduler, no DAC/FM
// output, no shared broadcast mixer. Each GET /song/<id>.wav renders that song
// per-connection: one full pass, one repeat of the loop body ("loops twice"),
// then the standard fade — via the Decoder's existing fade machinery.
// ============================================================================
#ifndef UNIT_TEST

#include "JukeboxServer.h"

namespace {

std::atomic<bool> g_jukebox_stop{false};

uint16_t jukebox_parse_port(const char* text, uint16_t fallback) {
    if (!text || !*text) return fallback;
    char* end = nullptr;
    long value = std::strtol(text, &end, 10);
    if (!end || *end != '\0' || value <= 0 || value > 65535) return fallback;
    return static_cast<uint16_t>(value);
}

void jukebox_shutdown_signal(int) {
    g_jukebox_stop.store(true);
}

const AssetMeta* find_song_by_id(int id) {
    for (size_t i = 0; i < bin_songs_count; ++i) {
        // Require a title: entries without one (e.g. id 0 "Silent") are not
        // jukebox-selectable, mirroring the radio's own song rotation rule.
        if (bin_songs[i].id == id && bin_songs[i].title) return &bin_songs[i];
    }
    return nullptr;
}

void send_song_error(int fd, const char* status_line) {
    std::ostringstream hdr;
    hdr << "HTTP/1.1 " << status_line << "\r\n"
        << "Content-Type: text/plain; charset=utf-8\r\n"
        << "Content-Length: 0\r\n"
        << "Connection: close\r\n\r\n";
    JukeboxServer::send_string_fd(fd, hdr.str());
}

// Directory of raw bwav files, named <binId>.bwav. Set from
// ACNH_JUKEBOX_BWAV_ROOT (default alongside the web root).
std::string g_bwav_root = "bwav";

// Serve the raw BWAV bytes for a song. The client decodes them with the exact
// same GC-ADPCM algorithm as the radio engine (lib/bwav.js is a bit-exact port
// of BWAVParser + Decoder). We do NOT transcode to WAV/PCM here: ADPCM is
// stateful, so the compressed stream must reach the client intact — shipping
// the original bytes is both smaller and immune to any re-encode drift.
void serve_raw_bwav(int fd, int song_id, bool head_only) {
    // song_id is already validated numeric by JukeboxServer. Compose the path
    // from the integer only (never from raw request text) so there is no way
    // to traverse out of g_bwav_root.
    std::string path = g_bwav_root + "/" + std::to_string(song_id) + ".bwav";

    std::ifstream f(path, std::ios::binary | std::ios::ate);
    if (!f) {
        send_song_error(fd, "404 Not Found");
        return;
    }
    std::streamsize size = f.tellg();
    f.seekg(0, std::ios::beg);

    std::ostringstream hdr;
    hdr << "HTTP/1.1 200 OK\r\n"
        << "Content-Type: application/octet-stream\r\n"
        << "Content-Length: " << size << "\r\n"
        << "Accept-Ranges: none\r\n"
        << "Cache-Control: public, max-age=86400, immutable\r\n"
        << "Connection: close\r\n\r\n";
    if (!JukeboxServer::send_string_fd(fd, hdr.str())) return;
    if (head_only) return;

    log_info(std::string(C_GREEN "[JUKEBOX] " C_RESET) + "Serving bwav id=" + std::to_string(song_id));

    std::vector<char> buf(64 * 1024);
    std::streamsize remaining = size;
    while (remaining > 0 && !g_jukebox_stop.load()) {
        std::streamsize chunk = std::min<std::streamsize>((std::streamsize)buf.size(), remaining);
        f.read(buf.data(), chunk);
        if (!JukeboxServer::send_all_fd(fd, buf.data(), (size_t)chunk)) return;
        remaining -= chunk;
    }
}

// ============================================================================
// Broadcast sync: all listeners in the same Discord Activity session (voice
// channel) hear the same song at the same position, "tune in" radio-style.
//
// Discord's Embedded App SDK exposes `instanceId`, shared by everyone
// connected to the same Activity instance (docs.discord.com/developers/
// activities/development-guides/multiplayer-experience: "Any users joining
// the same application will receive the same instanceId... should be used as
// a key to save and load the shared data relevant to an application"). Discord
// provides no relay itself, just that key -- so this server is the shared
// backend the docs say to bring: one authoritative {songId, startedAtMs} per
// instanceId, computed lazily (no background thread needed) by advancing
// through a shuffle-bag pool every time elapsed real time crosses a song's
// duration, however many requests come in.
//
// Duration per song is computed once at startup by reading each bwav's
// header (rate/loop points) with the SAME loop-once-more(twice
// total)+FADE_PLAY_DURATION+FADE_TIME_DURATION formula the client's
// lib/bwav.js decodeFullPlayback uses, so the server's schedule and the
// client's actual decoded buffer length agree to within float rounding
// (a client further clamps its own seek offset to its own decoded length,
// so sub-second server/client duration drift is harmless -- this doesn't
// need to be sample-accurate, just close).
// ============================================================================

int64_t now_ms() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::system_clock::now().time_since_epoch()).count();
}

// Reads just the bwav header (channel-info block) to compute total playback
// duration without touching the (possibly multi-MB) audio body.
double compute_bwav_duration_seconds(const std::string& path) {
    std::ifstream f(path, std::ios::binary);
    if (!f) return 0.0;
    unsigned char hdr[96] = {0};
    f.read(reinterpret_cast<char*>(hdr), sizeof(hdr));
    if (f.gcount() < (std::streamsize)sizeof(hdr)) return 0.0;
    if (std::memcmp(hdr, "BWAV", 4) != 0) return 0.0;

    auto ru16 = [&](int off) -> uint16_t {
        return (uint16_t)(hdr[off] | (hdr[off + 1] << 8));
    };
    auto ru32 = [&](int off) -> uint32_t {
        return (uint32_t)hdr[off] | ((uint32_t)hdr[off + 1] << 8) |
               ((uint32_t)hdr[off + 2] << 16) | ((uint32_t)hdr[off + 3] << 24);
    };

    if (ru16(4) != 0xFEFF || ru16(14) != 1) return 0.0; // bom / mono channel count

    const int ci = 16;
    uint32_t rate = ru32(ci + 4);
    uint32_t samples_this = ru32(ci + 12);
    uint32_t loop_end = ru32(ci + 60);
    uint32_t loop_start = ru32(ci + 64);
    if (rate == 0) return 0.0;

    bool loopable = loop_end > 0 && loop_start != 0xFFFFFFFFu && loop_end > loop_start;
    double total_frames;
    if (loopable) {
        double body_len = (double)(loop_end - loop_start);
        double extra_play_samples = std::trunc((double)FADE_PLAY_DURATION * (double)rate);
        double fade_samples = std::trunc((double)FADE_TIME_DURATION * (double)rate);
        total_frames = (double)loop_end + body_len + extra_play_samples + fade_samples;
    } else {
        total_frames = (double)samples_this;
    }
    return total_frames / (double)rate;
}

struct SongInfo {
    int id;
    double duration_sec;
};

std::vector<SongInfo> g_playable_songs; // built at startup from g_bwav_root

void load_playable_songs() {
    g_playable_songs.clear();
    std::error_code ec;
    for (const auto& entry : std::filesystem::directory_iterator(g_bwav_root, ec)) {
        if (ec || !entry.is_regular_file()) continue;
        std::string name = entry.path().filename().string();
        if (name.size() < 6 || name.substr(name.size() - 5) != ".bwav") continue;
        std::string stem = name.substr(0, name.size() - 5);
        if (stem.empty() || !std::all_of(stem.begin(), stem.end(), ::isdigit)) continue;

        int id = std::atoi(stem.c_str());
        double dur = compute_bwav_duration_seconds(entry.path().string());
        if (dur <= 0.0) {
            log_error("[JUKEBOX] skipping unparseable bwav for broadcast schedule: " + name);
            continue;
        }
        g_playable_songs.push_back({id, dur});
    }
    std::sort(g_playable_songs.begin(), g_playable_songs.end(),
              [](const SongInfo& a, const SongInfo& b) { return a.id < b.id; });
    log_info("[JUKEBOX] broadcast schedule loaded: " + std::to_string(g_playable_songs.size()) + " songs");
}

const SongInfo* find_song_info(int id) {
    for (const auto& s : g_playable_songs) if (s.id == id) return &s;
    return nullptr;
}

struct BroadcastSession {
    int current_song_id = -1;
    int64_t started_at_ms = 0;
    std::vector<int> pool;    // remaining shuffle-bag draw pool
    int64_t last_access_ms = 0;
};

std::mutex g_sessions_mutex;
std::unordered_map<std::string, BroadcastSession> g_sessions;
std::mt19937 g_broadcast_rng{std::random_device{}()};

void rehydrate_pool(std::vector<int>& pool) {
    pool.clear();
    for (const auto& s : g_playable_songs) pool.push_back(s.id);
}

int draw_from_pool(std::vector<int>& pool) {
    if (pool.empty()) rehydrate_pool(pool);
    std::shuffle(pool.begin(), pool.end(), g_broadcast_rng);
    int id = pool.back();
    pool.pop_back();
    if (pool.empty()) rehydrate_pool(pool);
    return id;
}

// Returns a snapshot {songId, startedAtMs} for the given instance, advancing
// through the shuffle-bag pool as many times as real time has actually
// elapsed since the last request (so a session nobody polled for an hour
// catches up correctly instead of getting stuck replaying one long song).
struct BroadcastState { int song_id; int64_t started_at_ms; double duration_sec; };

BroadcastState get_broadcast_state(const std::string& instance_id) {
    std::lock_guard<std::mutex> lock(g_sessions_mutex);
    BroadcastSession& sess = g_sessions[instance_id]; // creates on first use
    int64_t now = now_ms();
    sess.last_access_ms = now;

    if (sess.current_song_id < 0) {
        sess.current_song_id = draw_from_pool(sess.pool);
        sess.started_at_ms = now;
    }

    // Advance through however many songs have fully elapsed since we were
    // last asked. Bounded loop: g_playable_songs is never empty here (main()
    // refuses to start the server if it is), so each iteration always makes
    // forward progress.
    for (int guard = 0; guard < 100000; ++guard) {
        const SongInfo* info = find_song_info(sess.current_song_id);
        double dur = info ? info->duration_sec : 30.0; // defensive fallback
        int64_t ends_at = sess.started_at_ms + (int64_t)(dur * 1000.0);
        if (now < ends_at) break;
        sess.current_song_id = draw_from_pool(sess.pool);
        sess.started_at_ms = ends_at;
    }

    const SongInfo* info = find_song_info(sess.current_song_id);
    return { sess.current_song_id, sess.started_at_ms, info ? info->duration_sec : 30.0 };
}

// Idle sessions (nobody's asked about that instance in a while -- the voice
// channel Activity ended) are pruned so g_sessions doesn't grow unbounded
// across a long-running server. Called opportunistically from the
// /now-playing handler, not a dedicated thread.
void sweep_idle_sessions() {
    constexpr int64_t IDLE_LIMIT_MS = 30LL * 60 * 1000; // 30 minutes
    int64_t now = now_ms();
    std::lock_guard<std::mutex> lock(g_sessions_mutex);
    for (auto it = g_sessions.begin(); it != g_sessions.end(); ) {
        if (now - it->second.last_access_ms > IDLE_LIMIT_MS) it = g_sessions.erase(it);
        else ++it;
    }
}

// Handles GET /now-playing?instance=<id>. instance_id comes from the request
// path's query string, already extracted by JukeboxServer before this is
// called; an empty id is valid (a single shared "no instance" broadcast,
// used for plain-browser testing outside Discord where there's no shared
// instanceId to key off of).
void handle_now_playing(int fd, const std::string& instance_id) {
    if (g_playable_songs.empty()) {
        send_song_error(fd, "503 Service Unavailable");
        return;
    }
    static std::atomic<int> sweep_counter{0};
    if ((sweep_counter.fetch_add(1) % 64) == 0) sweep_idle_sessions();

    BroadcastState state = get_broadcast_state(instance_id);
    int64_t server_now = now_ms();

    std::ostringstream json;
    json << "{"
         << "\"songId\":" << state.song_id << ","
         << "\"startedAtMs\":" << state.started_at_ms << ","
         << "\"serverNowMs\":" << server_now << ","
         << "\"durationMs\":" << (int64_t)(state.duration_sec * 1000.0)
         << "}";

    std::string body = json.str();
    std::ostringstream hdr;
    hdr << "HTTP/1.1 200 OK\r\n"
        << "Content-Type: application/json; charset=utf-8\r\n"
        << "Content-Length: " << body.size() << "\r\n"
        << "Access-Control-Allow-Origin: *\r\n"
        << "Cache-Control: no-store\r\n"
        << "Connection: close\r\n\r\n";
    if (!JukeboxServer::send_string_fd(fd, hdr.str())) return;
    JukeboxServer::send_string_fd(fd, body);
}

} // namespace

int main(int argc, char** argv) {
    setenv("ACNH_DEBUG", "true", 1);

    init_engine_sample_rate_from_assets();

    if (bin_songs_count == 0) {
        log_error("No songs.");
        return 1;
    }

    ignore_sigpipe();

    uint16_t port = 9100;
    if (const char* env_port = std::getenv("ACNH_JUKEBOX_PORT")) {
        port = jukebox_parse_port(env_port, port);
    }
    if (argc > 1) {
        port = jukebox_parse_port(argv[1], port);
    }

    bool localhost_only = false;
    if (const char* env_local = std::getenv("ACNH_LOCALHOST_ONLY")) {
        std::string s = env_local;
        if (s == "1" || s == "true" || s == "TRUE") localhost_only = true;
    }

    std::string web_root = "jukebox-web/out";
    if (const char* env_root = std::getenv("ACNH_JUKEBOX_WEB_ROOT")) {
        web_root = env_root;
    }
    if (const char* env_bwav = std::getenv("ACNH_JUKEBOX_BWAV_ROOT")) {
        g_bwav_root = env_bwav;
    }

    load_playable_songs();
    if (g_playable_songs.empty()) {
        log_error("[JUKEBOX] no playable songs found under " + g_bwav_root + " -- broadcast sync disabled");
    }

    JukeboxServer server(web_root);
    server.set_song_streamer(serve_raw_bwav);
    server.set_now_playing_handler(handle_now_playing);
    if (!server.start(port, localhost_only)) {
        log_error("[WEB] Failed to start jukebox server on port " + std::to_string(port));
        return 1;
    }

    std::signal(SIGINT, jukebox_shutdown_signal);
#ifdef SIGTERM
    std::signal(SIGTERM, jukebox_shutdown_signal);
#endif

    log_info(std::string(C_GREEN) + "[JUKEBOX] " + C_RESET + "Serving on port " + C_BOLD +
             std::to_string(port) + C_RESET + " (web root: " + web_root + ")");

    while (!g_jukebox_stop.load()) {
        std::this_thread::sleep_for(std::chrono::milliseconds(200));
    }

    server.stop();
    log_info(std::string(C_GREEN) + "[JUKEBOX] " + C_RESET + "Shutdown complete.");
    return 0;
}
#endif
