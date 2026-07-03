#pragma once
// Forked from StreamServer.h for the on-demand jukebox (kept separate from the
// radio on purpose — reconcile the two servers later). Differences:
//   - no PCMBuffer / broadcast /stream: songs are rendered per-connection via
//     a SongStreamer hook installed by jukebox_pi.cpp
//   - generic sanitized static-file serving rooted at web_root (the Next.js
//     static export of the acnh-jukebox webpage), instead of a route whitelist
//   - Discord Activity support (/discord-token, /discord-config) retained
#include "Utils.h"
#include "AudioDefs.h"
#include <atomic>
#include <thread>
#include <string>
#include <functional>
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

#ifndef C_RESET
#define C_RESET   "\033[0m"
#define C_RED     "\033[31m"
#define C_GREEN   "\033[32m"
#define C_YELLOW  "\033[33m"
#define C_BLUE    "\033[34m"
#define C_CYAN    "\033[36m"
#define C_BOLD    "\033[1m"
#endif

class JukeboxServer {
public:
    // fd is the accepted client socket; the hook owns writing the entire HTTP
    // response (status line, headers, WAV body) and must not close fd.
    using SongStreamer = std::function<void(int fd, int song_id, bool head_only)>;

    explicit JukeboxServer(std::string web_root)
        : web_root(std::move(web_root)), running(false), listen_fd(-1), port(0) {}

    ~JukeboxServer() { stop(); }

    void set_song_streamer(SongStreamer s) { song_streamer = std::move(s); }

    bool start(uint16_t listen_port, bool localhost_only = false);
    void stop();

    // Shared by the song-streamer hook in jukebox_pi.cpp.
    static bool send_all_fd(int fd, const void* data, size_t len);
    static bool send_string_fd(int fd, const std::string& s);

private:
    std::string web_root;
    SongStreamer song_streamer;
    std::atomic<bool> running;
    std::thread accept_thread;
    int listen_fd;
    uint16_t port;

    std::mutex clients_mutex;
    std::set<int> active_client_fds;
    std::atomic<int> active_client_count{0};

    void accept_loop();
    void handle_client(int client_fd);
    void configure_client_socket(int fd);
    bool read_request(int fd, std::string& method, std::string& path,
                      std::string& headers_out, std::string& body_prefix);
    void send_file(int fd, const std::string& disk_path, const char* content_type,
                   bool head_only, const char* cache_control);
    void send_not_found(int fd);
    void send_json(int fd, int status, const char* status_text, const std::string& json);
    void serve_static(int fd, const std::string& url_path, bool head_only);

    // Discord Activity shell support (same behavior as StreamServer's).
    void send_discord_config(int fd);
    void handle_token_exchange(int fd, const std::string& headers, const std::string& body_prefix);
    bool discord_token_exchange(const std::string& code, std::string& response_out);

    bool send_all(int fd, const void* data, size_t len) { return send_all_fd(fd, data, len); }
    bool send_string(int fd, const std::string& s) { return send_string_fd(fd, s); }
};
