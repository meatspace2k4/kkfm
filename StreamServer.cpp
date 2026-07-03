#include "StreamServer.h"
#include <cstdlib>
#include <csignal>
#include <sys/wait.h>

bool StreamServer::start(uint16_t listen_port, bool localhost_only) {
    // Writes to a closed pipe (e.g. the token-exchange curl child failing to
    // exec) would otherwise raise SIGPIPE and take the whole player down.
    std::signal(SIGPIPE, SIG_IGN);
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
    std::string body_prefix;
    if (!read_request(client_fd, method, path, headers, body_prefix)) {
        return;
    }
    std::string range_value = get_header_value(headers, "Range");
    ByteRangeRequest range_req = parse_range_header(range_value);
    std::string ua = get_header_value(headers, "User-Agent");
    debug_printf(C_BLUE "[WEB] " C_RESET C_BOLD "%s " C_RESET C_CYAN "%s" C_RESET "%s\n",
        method.c_str(), path.c_str(), (!ua.empty() ? (" UA=" + ua).c_str() : ""));

    for (char& c : method) c = (char)std::toupper(static_cast<unsigned char>(c));

    // Discord Activity OAuth token exchange (POST only). Kept off any /api/
    // prefix so an edge WAF that challenges /api/* cannot block Discord's
    // server-side proxy fetch (see browser/README-discord.md).
    if (method == "POST") {
        if (path == "/discord-token") {
            handle_token_exchange(client_fd, headers, body_prefix);
        } else {
            send_not_found(client_fd);
        }
        return;
    }

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

    if (path == "/" || path == "/index.html") {
        send_index(client_fd, head_only);
    } else if (path == "/discord-shell.js") {
        send_static(client_fd, "browser/discord-shell.js",
                    "text/javascript; charset=utf-8", head_only);
    } else if (path == "/vendor/embedded-app-sdk.js") {
        send_static(client_fd, "browser/vendor/embedded-app-sdk.js",
                    "text/javascript; charset=utf-8", head_only);
    } else if (path == "/discord-config") {
        send_discord_config(client_fd);
    } else if (is_stream_path(path)) {
        if (!range_value.empty() && handle_range_probe(client_fd, range_req)) {
            return;
        }
        log_info(C_BLUE "[WEB] " C_RESET "Stream client " C_GREEN "connected" C_RESET);
        stream_audio(client_fd, head_only);
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

bool StreamServer::read_request(int fd, std::string& method, std::string& path,
                                std::string& headers_out, std::string& body_prefix) {
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
    // Any bytes past the header terminator are the start of the request body.
    body_prefix = request.substr(header_end + 4);
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
    // Strip the query string before routing. Discord launches the Activity at
    // "/?instance_id=...&frame_id=..." and exact-match routes would 404 on it;
    // the client reads those params from location.search, not the server.
    size_t qpos = path.find('?');
    if (qpos != std::string::npos) path = path.substr(0, qpos);
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
        // get_current_dir_name() returns malloc'd memory; must free it
        char* cwd = get_current_dir_name();
        std::string cwd_str = cwd ? cwd : "(unknown)";
        free(cwd);
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

void StreamServer::send_static(int fd, const char* disk_path, const char* content_type, bool head_only) {
    std::ifstream f(disk_path, std::ios::binary);
    if (!f) {
        send_not_found(fd);
        return;
    }
    std::ostringstream ss;
    ss << f.rdbuf();
    std::string payload = ss.str();

    std::ostringstream hdr;
    hdr << "HTTP/1.1 200 OK\r\n"
        << "Content-Type: " << content_type << "\r\n"
        << "Content-Length: " << payload.size() << "\r\n"
        << "Connection: close\r\n"
        << "Cache-Control: no-store\r\n\r\n";
    if (!send_string(fd, hdr.str())) return;
    if (!head_only) {
        send_string(fd, payload);
    }
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

void StreamServer::stream_audio(int fd, bool head_only) {
    std::ostringstream hdr;
    hdr << "HTTP/1.1 200 OK\r\n"
        << "Content-Type: audio/wav\r\n"
        << "Content-Length: " << STREAM_CONTENT_LENGTH << "\r\n"
        << "Accept-Ranges: bytes\r\n"
        << "Cache-Control: no-store\r\n"
        << "Access-Control-Allow-Origin: *\r\n"
        << "Connection: close\r\n\r\n";
    if (!send_string(fd, hdr.str())) return;
    if (head_only) return;

    WavHeader wav_head;
    if (!send_all(fd, &wav_head, sizeof(wav_head))) return;

    std::vector<int16_t> chunk(STREAM_CHUNK_FRAMES * CHANNELS, 0);
    uint64_t cursor = buffer.latest_cursor();

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

// ---------------------------------------------------------------------------
// Discord Activity shell: OAuth token exchange + client config.
// The browser Activity (browser/discord-shell.js) POSTs the authorization code
// here; we forward it to Discord's token endpoint and return the JSON verbatim,
// mirroring the reference Node handler. HTTPS is done by exec'ing the system
// curl so the minimal `-lasound`-only build gains no TLS link dependency.
// ---------------------------------------------------------------------------

namespace {

std::string url_encode(const std::string& s) {
    static const char hex[] = "0123456789ABCDEF";
    std::string out;
    out.reserve(s.size() * 3);
    for (unsigned char c : s) {
        if ((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
            (c >= '0' && c <= '9') || c == '-' || c == '_' || c == '.' || c == '~') {
            out.push_back((char)c);
        } else {
            out.push_back('%');
            out.push_back(hex[c >> 4]);
            out.push_back(hex[c & 0x0F]);
        }
    }
    return out;
}

// OAuth authorization codes are URL-safe tokens; reject anything else so a
// malformed body can never smuggle unexpected bytes into the exchange.
bool is_oauth_code(const std::string& code) {
    if (code.empty() || code.size() > 512) return false;
    for (unsigned char c : code) {
        if ((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
            (c >= '0' && c <= '9') || c == '-' || c == '_' || c == '.' || c == '~') {
            continue;
        }
        return false;
    }
    return true;
}

// Minimal extractor for a top-level JSON string field: {"key":"value"}.
bool extract_json_string(const std::string& body, const std::string& key, std::string& out) {
    std::string needle = "\"" + key + "\"";
    size_t k = body.find(needle);
    if (k == std::string::npos) return false;
    size_t colon = body.find(':', k + needle.size());
    if (colon == std::string::npos) return false;
    size_t q1 = body.find('"', colon + 1);
    if (q1 == std::string::npos) return false;
    std::string val;
    for (size_t i = q1 + 1; i < body.size(); ++i) {
        char c = body[i];
        if (c == '\\') {
            if (i + 1 >= body.size()) return false;
            val.push_back(body[i + 1]);
            ++i;
            continue;
        }
        if (c == '"') { out = val; return true; }
        val.push_back(c);
    }
    return false;
}

} // namespace

void StreamServer::send_json(int fd, int status, const char* status_text, const std::string& json) {
    std::ostringstream hdr;
    hdr << "HTTP/1.1 " << status << " " << status_text << "\r\n"
        << "Content-Type: application/json; charset=utf-8\r\n"
        << "Content-Length: " << json.size() << "\r\n"
        << "Access-Control-Allow-Origin: *\r\n"
        << "Cache-Control: no-store\r\n"
        << "Connection: close\r\n\r\n";
    if (!send_string(fd, hdr.str())) return;
    send_string(fd, json);
}

void StreamServer::send_discord_config(int fd) {
    const char* id = std::getenv("ACNH_DISCORD_CLIENT_ID");
    std::string cid = id ? id : "";
    // The client id is not secret, but strip characters that could break out
    // of the JSON string just in case the env value is malformed.
    std::string safe;
    for (char c : cid) {
        if (c == '"' || c == '\\' || c == '\n' || c == '\r') continue;
        safe.push_back(c);
    }
    send_json(fd, 200, "OK", "{\"client_id\":\"" + safe + "\"}");
}

void StreamServer::handle_token_exchange(int fd, const std::string& headers,
                                         const std::string& body_prefix) {
    const uint64_t MAX_BODY = 8192;
    uint64_t content_length = 0;
    std::string cl = get_header_value(headers, "Content-Length");
    if (!cl.empty()) parse_u64(cl, content_length);
    if (content_length > MAX_BODY) content_length = MAX_BODY;

    std::string body = body_prefix;
    char buf[1024];
    while (body.size() < content_length && body.size() < MAX_BODY) {
        ssize_t n = ::recv(fd, buf, sizeof(buf), 0);
        if (n <= 0) break;
        body.append(buf, buf + n);
    }

    std::string code;
    if (!extract_json_string(body, "code", code) || !is_oauth_code(code)) {
        send_json(fd, 400, "Bad Request",
                  "{\"error\":\"invalid_request\",\"error_description\":\"missing or malformed code\"}");
        return;
    }

    std::string response;
    if (discord_token_exchange(code, response)) {
        send_json(fd, 200, "OK", response);
    } else {
        log_error("[WEB] Discord token exchange failed");
        send_json(fd, 502, "Bad Gateway", response);
    }
}

bool StreamServer::discord_token_exchange(const std::string& code, std::string& response_out) {
    const char* client_id = std::getenv("ACNH_DISCORD_CLIENT_ID");
    const char* client_secret = std::getenv("ACNH_DISCORD_CLIENT_SECRET");
    if (!client_id || !*client_id || !client_secret || !*client_secret) {
        response_out = "{\"error\":\"server_not_configured\","
                       "\"error_description\":\"ACNH_DISCORD_CLIENT_ID/SECRET unset\"}";
        return false;
    }

    // application/x-www-form-urlencoded body, piped to curl via stdin so the
    // client secret never appears in the child's argv (visible via ps/proc).
    std::string form = "client_id=" + url_encode(client_id)
                     + "&client_secret=" + url_encode(client_secret)
                     + "&grant_type=authorization_code"
                     + "&code=" + url_encode(code);

    int in_pipe[2];   // parent -> child stdin
    int out_pipe[2];  // child stdout -> parent
    if (::pipe(in_pipe) != 0) { response_out = "{\"error\":\"pipe_failed\"}"; return false; }
    if (::pipe(out_pipe) != 0) {
        ::close(in_pipe[0]); ::close(in_pipe[1]);
        response_out = "{\"error\":\"pipe_failed\"}";
        return false;
    }

    pid_t pid = ::fork();
    if (pid < 0) {
        ::close(in_pipe[0]); ::close(in_pipe[1]);
        ::close(out_pipe[0]); ::close(out_pipe[1]);
        response_out = "{\"error\":\"fork_failed\"}";
        return false;
    }

    if (pid == 0) {
        // Child: wire pipes to stdin/stdout and exec curl.
        ::dup2(in_pipe[0], STDIN_FILENO);
        ::dup2(out_pipe[1], STDOUT_FILENO);
        ::close(in_pipe[0]); ::close(in_pipe[1]);
        ::close(out_pipe[0]); ::close(out_pipe[1]);
        const char* args[] = {
            "curl", "-s", "-S", "--max-time", "10",
            "-X", "POST", "https://discord.com/api/oauth2/token",
            "-H", "Content-Type: application/x-www-form-urlencoded",
            "--data-binary", "@-",
            nullptr
        };
        ::execvp("curl", const_cast<char* const*>(args));
        _exit(127); // exec failed (curl not installed)
    }

    // Parent.
    ::close(in_pipe[0]);
    ::close(out_pipe[1]);

    const char* p = form.data();
    size_t remaining = form.size();
    while (remaining > 0) {
        ssize_t n = ::write(in_pipe[1], p, remaining);
        if (n < 0) {
            if (errno == EINTR) continue;
            break; // e.g. EPIPE if curl died; child exit status handled below
        }
        p += n;
        remaining -= (size_t)n;
    }
    ::close(in_pipe[1]); // EOF to curl's stdin

    std::string output;
    char rbuf[1024];
    while (true) {
        ssize_t n = ::read(out_pipe[0], rbuf, sizeof(rbuf));
        if (n < 0) { if (errno == EINTR) continue; break; }
        if (n == 0) break;
        output.append(rbuf, rbuf + n);
        if (output.size() > 65536) break; // guard against runaway output
    }
    ::close(out_pipe[0]);

    int status = 0;
    ::waitpid(pid, &status, 0);
    bool exec_ok = WIFEXITED(status) && WEXITSTATUS(status) == 0;

    if (!exec_ok || output.empty()) {
        if (output.empty()) response_out = "{\"error\":\"token_exchange_failed\"}";
        else response_out = output;
        return false;
    }
    response_out = output;
    return true;
}

