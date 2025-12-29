#pragma once
#include <vector>
#include <cstdint>
#include <mutex>
#include <condition_variable>
#include <algorithm>
#include "AudioDefs.h"

class PCMBuffer {
    std::vector<int16_t> data;
    const size_t capacity_frames;
    uint64_t write_cursor;
    bool closed;
    mutable std::mutex mut;
    std::condition_variable cv;

public:
    explicit PCMBuffer(size_t frames = PCM_RING_FRAMES)
        : data(frames * 2, 0),
          capacity_frames(frames ? frames : 1),
          write_cursor(0),
          closed(false) {}

    void write(const int16_t* frames, size_t count) {
        if (!frames || count == 0) return;
        std::unique_lock<std::mutex> lock(mut);
        if (closed) return;
        for (size_t i = 0; i < count; ++i) {
            size_t slot = (write_cursor % capacity_frames) * 2;
            data[slot] = frames[i * 2];
            data[slot + 1] = frames[i * 2 + 1];
            ++write_cursor;
        }
        lock.unlock();
        cv.notify_all();
    }

    size_t read(uint64_t& cursor, int16_t* out, size_t max_frames, uint32_t timeout_ms) {
        if (!out || max_frames == 0) return 0;
        std::unique_lock<std::mutex> lock(mut);
        auto ready = [&]() { return closed || write_cursor > cursor; };
        if (!cv.wait_for(lock, std::chrono::milliseconds(timeout_ms), ready)) {
            return 0;
        }
        if (closed && write_cursor <= cursor) {
            return 0;
        }
        size_t available = (write_cursor > cursor) ? static_cast<size_t>(write_cursor - cursor) : 0;
        if (available > capacity_frames) {
            cursor = write_cursor - capacity_frames;
            available = capacity_frames;
        }
        size_t frames = std::min(available, max_frames);
        for (size_t i = 0; i < frames; ++i) {
            size_t slot = ((cursor + i) % capacity_frames) * 2;
            out[i * 2] = data[slot];
            out[i * 2 + 1] = data[slot + 1];
        }
        cursor += frames;
        return frames;
    }

    uint64_t latest_cursor() const {
        std::lock_guard<std::mutex> lock(mut);
        return write_cursor;
    }

    void close() {
        {
            std::lock_guard<std::mutex> lock(mut);
            closed = true;
        }
        cv.notify_all();
    }

    bool is_closed() const {
        std::lock_guard<std::mutex> lock(mut);
        return closed;
    }
};

