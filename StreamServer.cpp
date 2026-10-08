#include "StreamServer.h"
#include <cstdlib>

bool StreamServer::start(uint16_t listen_port, bool localhost_only) {
    port = listen_port;
    int fd = ::socket(AF_INET6, SOCK_STREAM, 0);
    if (fd < 0) {
        log_error("[WEB] socket: " + std::string(std::strerror(errno)));
        return false;
    }
    int opt = 1;
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));
#ifdef IPV6_V6ONLY
    int off = 0;
    setsockopt(fd, IPPROTO_IPV6, IPV6_V6ONLY, &off, sizeof(off));
#endif
    sockaddr_in6 addr{};
    addr.sin6_family = AF_INET6;
    if (localhost_only) {
        addr.sin6_addr = in6addr_loopback;
    } else {
        addr.sin6_addr = in6addr_any;
    }
    addr.sin6_port = htons(port);
    if (::bind(fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) < 0) {
        log_error("[WEB] bind: " + std::string(std::strerror(errno)));
        ::close(fd);
        return false;
    }
    if (::listen(fd, HTTP_BACKLOG) < 0) {
        log_error("[WEB] listen: " + std::string(std::strerror(errno)));
        ::close(fd);
        return false;
    }
    listen_fd = fd;
    running.store(true);
    accept_thread = std::thread(&StreamServer::accept_loop, this);
    const char* host_str = localhost_only ? "127.0.0.1" : "0.0.0.0";
    debug_printf(C_BLUE "[WEB] " C_RESET "Listening on " C_CYAN "http://%s:%d/stream" C_RESET "\n", host_str, port);
    return true;
}

void StreamServer::stop() {
    if (!running.exchange(false)) {
        if (listen_fd >= 0) {
            ::close(listen_fd);
            listen_fd = -1;
        }
        return;
    }
    if (listen_fd >= 0) {
        ::shutdown(listen_fd, SHUT_RDWR);
        ::close(listen_fd);
        listen_fd = -1;
    }
    if (accept_thread.joinable()) accept_thread.join();

    // Shutdown all active client sockets to unblock their threads
    {
        std::lock_guard<std::mutex> lock(clients_mutex);
        for (int fd : active_client_fds) {
            ::shutdown(fd, SHUT_RDWR);
        }
    }

    // Wait for all client handlers to finish (with timeout)
    auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(5);
    while (active_client_count.load() > 0) {
        if (std::chrono::steady_clock::now() > deadline) {
            log_error("[WEB] Timeout waiting for client threads to finish");
            break;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
}

void StreamServer::accept_loop() {
    while (running.load()) {
        sockaddr_in6 addr{};
        socklen_t len = sizeof(addr);
        int client = ::accept(listen_fd, reinterpret_cast<sockaddr*>(&addr), &len);
        if (client < 0) {
            if (!running.load()) break;
            if (errno == EINTR) continue;
            if (errno == EAGAIN || errno == EWOULDBLOCK) {
                std::this_thread::sleep_for(std::chrono::milliseconds(10));
                continue;
            }
            log_error("[WEB] accept: " + std::string(std::strerror(errno)));
            continue;
        }
        // Track client fd before spawning thread
        {
            std::lock_guard<std::mutex> lock(clients_mutex);
            active_client_fds.insert(client);
        }
        active_client_count.fetch_add(1);
        std::thread(&StreamServer::handle_client, this, client).detach();
    }
}

bool StreamServer::handle_range_probe(int fd, const ByteRangeRequest& range) {
    if (!range.valid) return false;
    if (range.start >= WAV_HEADER_SIZE) {
        return false;
    }
    uint64_t actual_end = range.has_end
        ? std::min<uint64_t>(range.end, WAV_HEADER_SIZE - 1)
        : WAV_HEADER_SIZE - 1;
    if (actual_end < range.start) return false;
    uint64_t length = actual_end - range.start + 1;
    WavHeader wav_head;
    const uint8_t* header_bytes = reinterpret_cast<const uint8_t*>(&wav_head);
    std::ostringstream hdr;
    hdr << "HTTP/1.1 206 Partial Content\r\n"
        << "Content-Type: audio/wav\r\n"
        << "Content-Length: " << length << "\r\n"
        << "Content-Range: bytes " << range.start << "-" << (range.start + length - 1)
        << "/" << STREAM_CONTENT_LENGTH << "\r\n"
        << "Accept-Ranges: bytes\r\n"
        << "Cache-Control: no-store\r\n"
        << "Connection: close\r\n\r\n";
    if (!send_string(fd, hdr.str())) return true;
    send_all(fd, header_bytes + range.start, (size_t)length);
    return true;
}

void StreamServer::handle_client(int client_fd) {
    // RAII guard to ensure cleanup on all exit paths
    struct ClientGuard {
        StreamServer* server;
        int fd;
        ClientGuard(StreamServer* s, int f) : server(s), fd(f) {}
        ~ClientGuard() {
            ::close(fd);
            {
                std::lock_guard<std::mutex> lock(server->clients_mutex);
                server->active_client_fds.erase(fd);
            }
            server->active_client_count.fetch_sub(1);
        }
    } guard(this, client_fd);

    configure_client_socket(client_fd);
    std::string method;
    std::string path;
    std::string headers;
    if (!read_request(client_fd, method, path, headers)) {
        return;
    }
    std::string range_value = get_header_value(headers, "Range");
    ByteRangeRequest range_req = parse_range_header(range_value);
    std::string ua = get_header_value(headers, "User-Agent");
    debug_printf(C_BLUE "[WEB] " C_RESET C_BOLD "%s " C_RESET C_CYAN "%s" C_RESET "%s\n",
        method.c_str(), path.c_str(), (!ua.empty() ? (" UA=" + ua).c_str() : ""));

    for (char& c : method) c = (char)std::toupper(static_cast<unsigned char>(c));
    bool head_only = false;
    if (method == "HEAD") {
        head_only = true;
        method = "GET";
    } else if (method != "GET") {
        send_not_found(client_fd);
        return;
    }

    auto is_stream_path = [](const std::string& p) {
        return p == "/stream" || p == "/stream.wav" || p == "/stream.pcm" || p == "/stream.raw";
    };

    RouteHandler route;
    {
        std::lock_guard<std::mutex> lock(control_mutex);
        if (!route_path.empty() && path == route_path) route = route_handler;
    }
    if (route) {
        if (head_only) {
            const std::string h = "HTTP/1.0 200 OK\r\nContent-Type: audio/mpeg\r\nConnection: close\r\n\r\n";
            send_string(client_fd, h);
            return;
        }
        log_info(C_BLUE "[WEB] " C_RESET "Radio client " C_GREEN "connected" C_RESET);
        route(headers,
              [&](const void* d, size_t n) { return send_all(client_fd, d, n); },
              [&]() { return running.load(); });
        log_info(C_BLUE "[WEB] " C_RESET "Radio client " C_YELLOW "disconnected" C_RESET);
    } else if (path == "/control" || path.rfind("/control/", 0) == 0) {
        send_control(client_fd, path);
    } else if (path == "/" || path == "/index.html") {
        send_index(client_fd, head_only);
    } else if (is_stream_path(path)) {
        if (!range_value.empty() && handle_range_probe(client_fd, range_req)) {
            return;
        }
        // A client that already fetched the 44-byte WAV header via a range probe
        // (ffplay/ffmpeg/libav, browsers) comes back with "Range: bytes=44-" and
        // expects PCM from there; answering 200 from byte 0 makes it bail out.
        uint64_t resume_from = (range_req.valid && range_req.start >= WAV_HEADER_SIZE) ? range_req.start : 0;
        log_info(C_BLUE "[WEB] " C_RESET "Stream client " C_GREEN "connected" C_RESET);
        stream_audio(client_fd, head_only, resume_from);
        log_info(C_BLUE "[WEB] " C_RESET "Stream client " C_YELLOW "disconnected" C_RESET);
    } else {
        send_not_found(client_fd);
    }
}

void StreamServer::configure_client_socket(int fd) {
    int flags = fcntl(fd, F_GETFD, 0);
    if (flags >= 0) fcntl(fd, F_SETFD, flags | FD_CLOEXEC);
#ifdef SO_NOSIGPIPE
    int set = 1;
    setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &set, sizeof(set));
#endif
    int one = 1;
    setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
    timeval tv{};
    tv.tv_sec = 10;
    tv.tv_usec = 0;
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
}

bool StreamServer::read_request(int fd, std::string& method, std::string& path, std::string& headers_out) {
    std::string request;
    request.reserve(1024);
    char buf[1024];
    while (request.find("\r\n\r\n") == std::string::npos && request.size() < 8192) {
        ssize_t n = ::recv(fd, buf, sizeof(buf), 0);
        if (n <= 0) return false;
        request.append(buf, buf + n);
    }
    size_t header_end = request.find("\r\n\r\n");
    if (header_end == std::string::npos) return false;
    std::string head_section = request.substr(0, header_end);
    std::istringstream ss(head_section);
    std::string line;
    if (!std::getline(ss, line)) return false;
    if (!line.empty() && line.back() == '\r') line.pop_back();
    std::istringstream ls(line);
    if (!(ls >> method >> path)) return false;
    headers_out.clear();
    std::string header_line;
    while (std::getline(ss, header_line)) {
        if (!header_line.empty() && header_line.back() == '\r') header_line.pop_back();
        headers_out.append(header_line);
        headers_out.append("\r\n");
    }
    if (path.empty()) path = "/";
    return true;
}

void StreamServer::send_index(int fd, bool head_only) {
    std::string payload;
    std::ifstream f("browser/index.html", std::ios::binary);
    if (f) {
        std::ostringstream ss;
        ss << f.rdbuf();
        payload = ss.str();
    } else {
#if defined(__GLIBC__)
        // get_current_dir_name() returns malloc'd memory; must free it
        char* cwd = get_current_dir_name();
        std::string cwd_str = cwd ? cwd : "(unknown)";
        free(cwd);
#else
        // Portable fallback (macOS/BSD have no get_current_dir_name).
        char cwd_buf[4096];
        std::string cwd_str = getcwd(cwd_buf, sizeof(cwd_buf)) ? std::string(cwd_buf) : std::string("(unknown)");
#endif
        log_error("Failed to load browser/index.html (cwd: " + cwd_str + ")");
        send_not_found(fd);
        return;
    }

    std::ostringstream hdr;
    hdr << "HTTP/1.1 200 OK\r\n"
        << "Content-Type: text/html; charset=utf-8\r\n"
        << "Content-Length: " << payload.size() << "\r\n"
        << "Connection: close\r\n"
        << "Cache-Control: no-store\r\n\r\n";
    send_string(fd, hdr.str());
    if (!head_only) {
        send_string(fd, payload);
    }
}

void StreamServer::send_control(int fd, const std::string& path) {
    std::function<std::string(const std::string&)> h;
    {
        std::lock_guard<std::mutex> lock(control_mutex);
        h = control_handler;
    }
    if (!h) {
        send_not_found(fd);
        return;
    }
    std::string payload = h(path);
    std::ostringstream hdr;
    hdr << "HTTP/1.1 200 OK\r\n"
        << "Content-Type: text/plain; charset=utf-8\r\n"
        << "Content-Length: " << payload.size() << "\r\n"
        << "Cache-Control: no-store\r\n"
        << "Connection: close\r\n\r\n";
    send_string(fd, hdr.str());
    send_string(fd, payload);
}

void StreamServer::send_not_found(int fd) {
    const std::string payload = "Not Found";
    std::ostringstream hdr;
    hdr << "HTTP/1.1 404 Not Found\r\n"
        << "Content-Type: text/plain; charset=utf-8\r\n"
        << "Content-Length: " << payload.size() << "\r\n"
        << "Connection: close\r\n\r\n";
    send_string(fd, hdr.str());
    send_string(fd, payload);
}

void StreamServer::stream_audio(int fd, bool head_only, uint64_t resume_from) {
    std::ostringstream hdr;
    if (resume_from > 0) {
        // Live stream: any byte offset past the header maps to "audio from now".
        hdr << "HTTP/1.1 206 Partial Content\r\n"
            << "Content-Type: audio/wav\r\n"
            << "Content-Length: " << (STREAM_CONTENT_LENGTH - resume_from) << "\r\n"
            << "Content-Range: bytes " << resume_from << "-" << (STREAM_CONTENT_LENGTH - 1)
            << "/" << STREAM_CONTENT_LENGTH << "\r\n";
    } else {
        hdr << "HTTP/1.1 200 OK\r\n"
            << "Content-Type: audio/wav\r\n"
            << "Content-Length: " << STREAM_CONTENT_LENGTH << "\r\n";
    }
    hdr << "Accept-Ranges: bytes\r\n"
        << "Cache-Control: no-store\r\n"
        << "Access-Control-Allow-Origin: *\r\n"
        << "Connection: close\r\n\r\n";
    if (!send_string(fd, hdr.str())) return;
    if (head_only) return;

    if (resume_from == 0) {
        WavHeader wav_head;
        if (!send_all(fd, &wav_head, sizeof(wav_head))) return;
    }

    std::vector<int16_t> chunk(STREAM_CHUNK_FRAMES * CHANNELS, 0);
    // Start each client a few seconds back in the ring so it receives a burst and
    // keeps a cushion; starting exactly at "now" leaves players (Sonos, ffplay)
    // with an empty buffer that runs dry on any network hiccup or clock drift.
    // ACNH_STREAM_PREBUFFER_MS (default 0 = start at now), capped by the ring size.
    uint64_t cursor = buffer.latest_cursor();
    {
        uint64_t pre_ms = 0;
        if (const char* v = std::getenv("ACNH_STREAM_PREBUFFER_MS")) parse_u64(v, pre_ms);
        uint64_t pre = pre_ms * (uint64_t)SAMPLE_RATE / 1000;
        uint64_t cap = (uint64_t)PCM_RING_FRAMES - (uint64_t)SAMPLE_RATE;   // keep 1 s clear of the writer
        if (pre > cap) pre = cap;
        cursor = (cursor > pre) ? cursor - pre : 0;
    }

    while (running.load() && !buffer.is_closed()) {
        size_t frames = buffer.read(cursor, chunk.data(), STREAM_CHUNK_FRAMES, PCM_READ_TIMEOUT_MS);
        if (frames == 0) {
            if (!running.load() || buffer.is_closed()) break;
            continue;
        }
        size_t bytes = frames * CHANNELS * sizeof(int16_t);
        if (!send_all(fd, chunk.data(), bytes)) break;
    }
}

