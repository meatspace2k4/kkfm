#pragma once
#include "PCMBuffer.h"
#include "Utils.h"
#include "AudioDefs.h"
#include <atomic>
#include <thread>
#include <string>
#include <vector>
#include <sstream>
#include <iostream>
#include <cstring>
#include <cerrno>
#include <mutex>
#include <set>
#include <sys/socket.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <unistd.h>
#include <fcntl.h>
#include <fstream>
#include <functional>

extern void log_error(const std::string& msg);
extern void log_info(const std::string& msg);
extern void debug_printf(const char* fmt, ...);

#define C_RESET   "\033[0m"
#define C_RED     "\033[31m"
#define C_GREEN   "\033[32m"
#define C_YELLOW  "\033[33m"
#define C_BLUE    "\033[34m"
#define C_CYAN    "\033[36m"
#define C_BOLD    "\033[1m"

struct ByteRangeRequest {
    bool valid = false;
    uint64_t start = 0;
    uint64_t end = 0;
    bool has_end = false;
};

inline ByteRangeRequest parse_range_header(const std::string& header) {
    ByteRangeRequest req;
    if (header.empty()) return req;
    size_t eq = header.find('=');
    if (eq == std::string::npos) return req;
    std::string unit = trim(header.substr(0, eq));
    if (to_lower_copy(unit) != "bytes") return req;
    std::string spec = trim(header.substr(eq + 1));
    size_t dash = spec.find('-');
    if (dash == std::string::npos) return req;
    std::string start_str = trim(spec.substr(0, dash));
    std::string end_str = trim(spec.substr(dash + 1));
    if (start_str.empty()) return req;
    uint64_t start_val = 0;
    if (!parse_u64(start_str, start_val)) return req;
    req.start = start_val;
    if (!end_str.empty()) {
        uint64_t end_val = 0;
        if (!parse_u64(end_str, end_val)) return req;
        if (end_val < start_val) return req;
        req.end = end_val;
        req.has_end = true;
    }
    req.valid = true;
    return req;
}

class StreamServer {
public:
    using RouteSend = std::function<bool(const void*, size_t)>;
    using RouteHandler = std::function<void(const std::string& headers, const RouteSend& send,
                                            const std::function<bool()>& alive)>;
private:
    PCMBuffer& buffer;
    std::atomic<bool> running;
    std::thread accept_thread;
    int listen_fd;
    uint16_t port;

    // Track active client file descriptors for graceful shutdown
    std::mutex clients_mutex;
    std::set<int> active_client_fds;
    std::atomic<int> active_client_count{0};

    std::mutex control_mutex;
    std::function<std::string(const std::string&)> control_handler;
    std::string route_path;
    RouteHandler route_handler;
    void send_control(int fd, const std::string& path);

public:
    explicit StreamServer(PCMBuffer& buf)
        : buffer(buf), running(false), listen_fd(-1), port(DEFAULT_STREAM_PORT) {}

    ~StreamServer() { stop(); }

    bool start(uint16_t listen_port, bool localhost_only = false);
    void stop();

    // Optional GET /control/... handler (set by a player that accepts commands).
    // Receives the request path; returns the plain-text reply. Unset: 404.
    void set_control_handler(std::function<std::string(const std::string&)> h) {
        std::lock_guard<std::mutex> lock(control_mutex);
        control_handler = std::move(h);
    }

    // Optional extra GET route served by the caller on the client socket (e.g. an
    // MP3 radio stream). The handler writes the whole response with `send`, and
    // keeps going while `alive()` is true. Unset: the path is a 404 as before.
    void set_route(const std::string& path, RouteHandler h) {
        std::lock_guard<std::mutex> lock(control_mutex);
        route_path = path;
        route_handler = std::move(h);
    }

private:
    void accept_loop();
    bool handle_range_probe(int fd, const ByteRangeRequest& range);
    void handle_client(int client_fd);
    void configure_client_socket(int fd);
    bool read_request(int fd, std::string& method, std::string& path, std::string& headers_out);
    void send_index(int fd, bool head_only);
    void send_not_found(int fd);
    void stream_audio(int fd, bool head_only, uint64_t resume_from = 0);

    bool send_all(int fd, const void* data, size_t len) {
        if (fd < 0 || !data || len == 0) return true;
        const uint8_t* p = static_cast<const uint8_t*>(data);
        size_t remaining = len;
        while (remaining > 0) {
            ssize_t n = ::send(fd, p, remaining, MSG_NOSIGNAL);
            if (n < 0) {
                if (errno == EINTR) continue;
                // If the socket buffer is full, wait briefly and retry. 
                // Do not treat EAGAIN as a fatal error.
                if (errno == EAGAIN || errno == EWOULDBLOCK) {
                    std::this_thread::sleep_for(std::chrono::milliseconds(5));
                    continue;
                }
                return false;
            }
            p += n;
            remaining -= n;
        }
        return true;
    }

    bool send_string(int fd, const std::string& s) {
        return send_all(fd, s.data(), s.size());
    }
};

