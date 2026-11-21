/*
 * Internet-radio output for the hourly player: in-process MP3 (libmp3lame)
 * served as /radio.mp3 with Icecast-style ICY metadata (StreamTitle).
 *
 * One encoder thread reads the same PCM ring the WAV /stream uses, encodes
 * MP3 (CBR, 48 kHz joint stereo) once, and appends the frames to an MP3 byte
 * ring that every radio client reads. Track titles are pinned to the PCM
 * position where the track starts and carried over to the MP3 byte position
 * the encoder reaches there, so each client's StreamTitle changes with the audio.
 *
 * The LAME prototypes are declared here (opaque context), so only the shared
 * library is needed to build: the Pi has libmp3lame.so.0 without the -dev headers.
 */
#pragma once

#include "../PCMBuffer.h"
#include "../AudioDefs.h"

#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <cstring>
#include <deque>
#include <functional>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

extern "C" {
typedef struct lame_global_struct lame_global_flags;
lame_global_flags* lame_init(void);
int lame_set_in_samplerate(lame_global_flags*, int);
int lame_set_out_samplerate(lame_global_flags*, int);
int lame_set_num_channels(lame_global_flags*, int);
int lame_set_brate(lame_global_flags*, int);
int lame_set_mode(lame_global_flags*, int);      // MPEG_mode: 1 = JOINT_STEREO
int lame_set_quality(lame_global_flags*, int);
int lame_set_bWriteVbrTag(lame_global_flags*, int);
int lame_init_params(lame_global_flags*);
int lame_encode_buffer_interleaved(lame_global_flags*, short int pcm[], int num_samples,
                                   unsigned char* mp3buf, int mp3buf_size);
int lame_close(lame_global_flags*);
}

namespace hourly {

// ---------------------------------------------------------------------------
// ICY metadata
// ---------------------------------------------------------------------------
constexpr size_t ICY_METAINT = 16000;   // audio bytes between metadata blocks

// One metadata block: length byte (in 16-byte units) + "StreamTitle='...';" zero-padded.
// An empty string encodes "no change" (single 0 byte).
inline std::string icy_block(const std::string& title) {
    if (title.empty()) return std::string(1, '\0');
    std::string t = title;
    for (char& c : t) if (c == '\'') c = '`';     // a quote would end the field
    std::string body = "StreamTitle='" + t + "';";
    if (body.size() > 255 * 16) body.resize(255 * 16);
    size_t n = (body.size() + 15) / 16;
    body.resize(n * 16, '\0');
    return std::string(1, (char)n) + body;
}

// ---------------------------------------------------------------------------
// MP3 byte ring with absolute positions, frame-chunk starts, and title marks
// ---------------------------------------------------------------------------
class Mp3Ring {
    std::vector<uint8_t> data;
    uint64_t write_pos = 0;                    // absolute bytes written
    std::deque<uint64_t> chunk_starts;         // absolute positions where encoder output chunks begin
    std::deque<std::pair<uint64_t, std::string>> titles;   // (absolute pos, title)
    bool closed = false;
    mutable std::mutex mut;
    std::condition_variable cv;

public:
    explicit Mp3Ring(size_t capacity) : data(capacity ? capacity : 1) {}

    size_t capacity() const { return data.size(); }

    void append(const uint8_t* p, size_t n) {
        if (!n) return;
        {
            std::lock_guard<std::mutex> l(mut);
            if (closed) return;
            chunk_starts.push_back(write_pos);
            for (size_t i = 0; i < n; ++i) data[(write_pos + i) % data.size()] = p[i];
            write_pos += n;
            uint64_t oldest = write_pos > data.size() ? write_pos - data.size() : 0;
            while (!chunk_starts.empty() && chunk_starts.front() < oldest) chunk_starts.pop_front();
            while (titles.size() > 1 && titles[1].first <= oldest) titles.pop_front();
        }
        cv.notify_all();
    }

    void mark_title(uint64_t pos, const std::string& title) {
        std::lock_guard<std::mutex> l(mut);
        titles.emplace_back(pos, title);
    }

    std::string title_at(uint64_t pos) const {
        std::lock_guard<std::mutex> l(mut);
        std::string t;
        for (const auto& e : titles) {
            if (e.first <= pos) t = e.second;
            else break;
        }
        return t;
    }

    uint64_t position() const { std::lock_guard<std::mutex> l(mut); return write_pos; }

    // Start for a new client: the latest chunk start at least `back` bytes behind the
    // writer (a frame boundary), so the client gets a burst and keeps a cushion.
    uint64_t start_cursor(uint64_t back) const {
        std::lock_guard<std::mutex> l(mut);
        uint64_t target = write_pos > back ? write_pos - back : 0;
        uint64_t best = chunk_starts.empty() ? write_pos : chunk_starts.back();
        for (auto it = chunk_starts.rbegin(); it != chunk_starts.rend(); ++it) {
            if (*it <= target) { best = *it; break; }
            best = *it;
        }
        return best;
    }

    // Copies up to `max` bytes from `cursor`; waits up to timeout_ms for data.
    // A cursor that fell out of the ring jumps to the oldest chunk start.
    size_t read(uint64_t& cursor, uint8_t* out, size_t max, uint32_t timeout_ms) {
        std::unique_lock<std::mutex> l(mut);
        cv.wait_for(l, std::chrono::milliseconds(timeout_ms), [&] { return closed || write_pos > cursor; });
        if (write_pos <= cursor) return 0;
        // A client that has drifted far behind the writer (all-night listening: the player's clock
        // runs slower than the encoder's) jumps once to a chunk start a few seconds behind live.
        // Jumping to the oldest chunk instead left it parked at the ring's tail, where it was
        // overrun again and again: repeated skips, heard as stutter.
        const uint64_t max_lag = data.size() / 2;          // ~15 s at the default 30 s ring
        const uint64_t resync_back = data.size() / 10;     // ~3 s cushion after the jump
        if (write_pos - cursor > max_lag) {
            uint64_t target = write_pos > resync_back ? write_pos - resync_back : 0;
            uint64_t best = write_pos;
            for (auto it = chunk_starts.rbegin(); it != chunk_starts.rend(); ++it) {
                best = *it;
                if (*it <= target) break;
            }
            cursor = best;
        }
        size_t n = (size_t)std::min<uint64_t>(max, write_pos - cursor);
        for (size_t i = 0; i < n; ++i) out[i] = data[(cursor + i) % data.size()];
        cursor += n;
        return n;
    }

    void close() {
        { std::lock_guard<std::mutex> l(mut); closed = true; }
        cv.notify_all();
    }
    bool is_closed() const { std::lock_guard<std::mutex> l(mut); return closed; }
};

// ---------------------------------------------------------------------------
// Encoder thread: PCM ring -> LAME -> Mp3Ring
// ---------------------------------------------------------------------------
class RadioEncoder {
    PCMBuffer& pcm;
    Mp3Ring ring;
    int bitrate_kbps;
    lame_global_flags* gf = nullptr;
    std::thread worker;
    std::atomic<bool> running{false};
    std::mutex title_mut;
    std::deque<std::pair<uint64_t, std::string>> pending_titles;   // (PCM frame pos, title)
    std::atomic<uint64_t> pcm_cursor{0};

public:
    explicit RadioEncoder(PCMBuffer& p, int kbps = 192, size_t ring_seconds = 30)
        : pcm(p), ring((size_t)kbps * 1000 / 8 * ring_seconds), bitrate_kbps(kbps) {}
    ~RadioEncoder() { stop(); }

    Mp3Ring& mp3() { return ring; }
    int bitrate() const { return bitrate_kbps; }

    bool start(int sample_rate) {
        gf = lame_init();
        if (!gf) return false;
        lame_set_in_samplerate(gf, sample_rate);
        lame_set_out_samplerate(gf, sample_rate);
        lame_set_num_channels(gf, CHANNELS);
        lame_set_brate(gf, bitrate_kbps);
        lame_set_mode(gf, 1);            // JOINT_STEREO
        lame_set_quality(gf, 5);
        lame_set_bWriteVbrTag(gf, 0);    // live stream: no Xing/Info frame
        if (lame_init_params(gf) < 0) { lame_close(gf); gf = nullptr; return false; }
        pcm_cursor = pcm.latest_cursor();
        running = true;
        worker = std::thread([this] { run(); });
        return true;
    }

    void stop() {
        if (running.exchange(false) && worker.joinable()) worker.join();
        ring.close();
        if (gf) { lame_close(gf); gf = nullptr; }
    }

    // Title takes effect at the PCM position the writer is at now (a new track's first frames).
    void set_title(const std::string& title) {
        std::lock_guard<std::mutex> l(title_mut);
        pending_titles.emplace_back(pcm.latest_cursor(), title);
    }

private:
    void run() {
        const int frames = 1152 * 4;
        std::vector<int16_t> buf((size_t)frames * CHANNELS);
        std::vector<unsigned char> out((size_t)(1.25 * frames) + 7200);
        uint64_t cursor = pcm_cursor.load();
        size_t have = 0;
        while (running) {
            size_t got = pcm.read(cursor, buf.data() + have * CHANNELS, (size_t)frames - have, PCM_READ_TIMEOUT_MS);
            if (got == 0) { if (pcm.is_closed()) break; continue; }
            have += got;
            if (have < (size_t)frames) continue;
            int n = lame_encode_buffer_interleaved(gf, reinterpret_cast<short*>(buf.data()), frames,
                                                   out.data(), (int)out.size());
            have = 0;
            uint64_t mp3_pos = ring.position();
            {
                std::lock_guard<std::mutex> l(title_mut);
                while (!pending_titles.empty() && pending_titles.front().first <= cursor) {
                    ring.mark_title(mp3_pos, pending_titles.front().second);
                    pending_titles.pop_front();
                }
            }
            if (n > 0) ring.append(out.data(), (size_t)n);
            pcm_cursor = cursor;
        }
    }
};

// ---------------------------------------------------------------------------
// Client: /radio.mp3. `send` writes bytes to the socket (false = client gone).
// ---------------------------------------------------------------------------
inline void serve_radio_client(Mp3Ring& ring, int bitrate_kbps, bool want_icy, uint64_t prebuffer_ms,
                               const std::string& station,
                               const std::function<bool(const void*, size_t)>& send,
                               const std::function<bool()>& alive) {
    std::string hdr = "HTTP/1.0 200 OK\r\n"
                      "Content-Type: audio/mpeg\r\n"
                      "icy-name: " + station + "\r\n"
                      "icy-br: " + std::to_string(bitrate_kbps) + "\r\n"
                      "Cache-Control: no-cache, no-store\r\n"
                      "Access-Control-Allow-Origin: *\r\n";
    if (want_icy) hdr += "icy-metaint: " + std::to_string(ICY_METAINT) + "\r\n";
    hdr += "Connection: close\r\n\r\n";
    if (!send(hdr.data(), hdr.size())) return;

    uint64_t back = prebuffer_ms * (uint64_t)bitrate_kbps / 8;   // bytes
    uint64_t cursor = ring.start_cursor(back);
    std::vector<uint8_t> buf(8192);
    size_t until_meta = ICY_METAINT;
    std::string sent_title;
    bool first_meta = true;
    while (alive() && !ring.is_closed()) {
        size_t want = want_icy ? std::min(buf.size(), until_meta) : buf.size();
        uint64_t at = cursor;
        size_t n = ring.read(cursor, buf.data(), want, PCM_READ_TIMEOUT_MS);
        if (n == 0) continue;
        if (!send(buf.data(), n)) return;
        if (!want_icy) continue;
        until_meta -= n;
        if (until_meta == 0) {
            std::string t = ring.title_at(at + n);
            std::string block = (first_meta || t != sent_title) ? icy_block(t) : icy_block("");
            if (first_meta || t != sent_title) { sent_title = t; first_meta = false; }
            if (!send(block.data(), block.size())) return;
            until_meta = ICY_METAINT;
        }
    }
}

} // namespace hourly
