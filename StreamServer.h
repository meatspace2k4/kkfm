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
    PCMBuffer& buffer;
    std::atomic<bool> running;
    std::thread accept_thread;
    int listen_fd;
    uint16_t port;

    // Track active client file descriptors for graceful shutdown
    std::mutex clients_mutex;
    std::set<int> active_client_fds;
    std::atomic<int> active_client_count{0};

public:
    explicit StreamServer(PCMBuffer& buf)
        : buffer(buf), running(false), listen_fd(-1), port(DEFAULT_STREAM_PORT) {}

    ~StreamServer() { stop(); }

    bool start(uint16_t listen_port, bool localhost_only = false);
    void stop();

private:
    void accept_loop();
    bool handle_range_probe(int fd, const ByteRangeRequest& range);
    void handle_client(int client_fd);
    void configure_client_socket(int fd);
    bool read_request(int fd, std::string& method, std::string& path,
                      std::string& headers_out, std::string& body_prefix);
    void send_index(int fd, bool head_only);
    void send_static(int fd, const char* disk_path, const char* content_type, bool head_only);
    void send_not_found(int fd);
    void send_json(int fd, int status, const char* status_text, const std::string& json);
    void stream_audio(int fd, bool head_only);

    // Discord Activity shell support (see browser/README-discord.md).
    void send_discord_config(int fd);
    void handle_token_exchange(int fd, const std::string& headers, const std::string& body_prefix);
    bool discord_token_exchange(const std::string& code, std::string& response_out);

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

