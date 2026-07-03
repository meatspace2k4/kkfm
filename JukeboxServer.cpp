// Forked from StreamServer.cpp for the on-demand jukebox — see JukeboxServer.h
// for what changed. Socket lifecycle, request parsing, and the Discord OAuth
// token exchange are byte-for-byte the radio server's logic.
#include "JukeboxServer.h"
#include <cstdlib>
#include <csignal>
#include <sys/wait.h>

bool JukeboxServer::send_all_fd(int fd, const void* data, size_t len) {
    if (fd < 0 || !data || len == 0) return true;
    const uint8_t* p = static_cast<const uint8_t*>(data);
    size_t remaining = len;
    while (remaining > 0) {
        ssize_t n = ::send(fd, p, remaining, MSG_NOSIGNAL);
        if (n < 0) {
            if (errno == EINTR) continue;
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

bool JukeboxServer::send_string_fd(int fd, const std::string& s) {
    return send_all_fd(fd, s.data(), s.size());
}

bool JukeboxServer::start(uint16_t listen_port, bool localhost_only) {
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
    accept_thread = std::thread(&JukeboxServer::accept_loop, this);
    const char* host_str = localhost_only ? "127.0.0.1" : "0.0.0.0";
    debug_printf(C_BLUE "[WEB] " C_RESET "Jukebox listening on " C_CYAN "http://%s:%d/" C_RESET "\n", host_str, port);
    return true;
}

void JukeboxServer::stop() {
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

    {
        std::lock_guard<std::mutex> lock(clients_mutex);
        for (int fd : active_client_fds) {
            ::shutdown(fd, SHUT_RDWR);
        }
    }

    auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(5);
    while (active_client_count.load() > 0) {
        if (std::chrono::steady_clock::now() > deadline) {
            log_error("[WEB] Timeout waiting for client threads to finish");
            break;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
}

void JukeboxServer::accept_loop() {
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
        {
            std::lock_guard<std::mutex> lock(clients_mutex);
            active_client_fds.insert(client);
        }
        active_client_count.fetch_add(1);
        std::thread(&JukeboxServer::handle_client, this, client).detach();
    }
}

void JukeboxServer::handle_client(int client_fd) {
    struct ClientGuard {
        JukeboxServer* server;
        int fd;
        ClientGuard(JukeboxServer* s, int f) : server(s), fd(f) {}
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
    std::string ua = get_header_value(headers, "User-Agent");
    debug_printf(C_BLUE "[WEB] " C_RESET C_BOLD "%s " C_RESET C_CYAN "%s" C_RESET "%s\n",
        method.c_str(), path.c_str(), (!ua.empty() ? (" UA=" + ua).c_str() : ""));

    for (char& c : method) c = (char)std::toupper(static_cast<unsigned char>(c));

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

    // Strip any query string before routing (Next.js adds cache-busters).
    size_t q = path.find('?');
    if (q != std::string::npos) path.resize(q);

    if (path == "/discord-config") {
        send_discord_config(client_fd);
        return;
    }

    // /song/<id>.bwav — raw BWAV bytes, delegated to the engine hook (the
    // client decodes them). id must be purely numeric.
    if (path.rfind("/song/", 0) == 0) {
        std::string rest = path.substr(6);
        const std::string suffix = ".bwav";
        if (rest.size() > suffix.size() &&
            rest.compare(rest.size() - suffix.size(), suffix.size(), suffix) == 0) {
            rest.resize(rest.size() - suffix.size());
        }
        bool numeric = !rest.empty() && rest.size() <= 6;
        for (char c : rest) if (c < '0' || c > '9') { numeric = false; break; }
        if (numeric && song_streamer) {
            song_streamer(client_fd, std::atoi(rest.c_str()), head_only);
        } else {
            send_not_found(client_fd);
        }
        return;
    }

    serve_static(client_fd, path, head_only);
}

void JukeboxServer::serve_static(int fd, const std::string& url_path, bool head_only) {
    std::string p = url_path;
    if (p.empty() || p == "/") p = "/index.html";

    // Sanitize: absolute, no "..", no "//", conservative charset. Anything
    // else 404s, so requests can never escape web_root.
    if (p[0] != '/' || p.find("..") != std::string::npos || p.find("//") != std::string::npos) {
        send_not_found(fd);
        return;
    }
    for (char c : p) {
        if (!((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') ||
              c == '/' || c == '.' || c == '-' || c == '_' || c == '%')) {
            send_not_found(fd);
            return;
        }
    }

    // Next export serves extensionless routes as <route>.html.
    std::string disk = web_root + p;
    if (p.find('.') == std::string::npos) disk += ".html";

    size_t dot = disk.rfind('.');
    std::string ext = (dot == std::string::npos) ? "" : disk.substr(dot);
    const char* ctype = "application/octet-stream";
    const char* cache = "no-store";
    if (ext == ".html")            { ctype = "text/html; charset=utf-8"; }
    else if (ext == ".js")         { ctype = "text/javascript; charset=utf-8"; cache = "public, max-age=3600"; }
    else if (ext == ".css")        { ctype = "text/css; charset=utf-8"; cache = "public, max-age=3600"; }
    else if (ext == ".json")       { ctype = "application/json; charset=utf-8"; }
    else if (ext == ".png")        { ctype = "image/png"; cache = "public, max-age=86400, immutable"; }
    else if (ext == ".jpg" || ext == ".jpeg") { ctype = "image/jpeg"; cache = "public, max-age=86400, immutable"; }
    else if (ext == ".svg")        { ctype = "image/svg+xml"; cache = "public, max-age=86400"; }
    else if (ext == ".ico")        { ctype = "image/x-icon"; cache = "public, max-age=86400"; }
    else if (ext == ".woff2")      { ctype = "font/woff2"; cache = "public, max-age=86400, immutable"; }
    else if (ext == ".woff")       { ctype = "font/woff"; cache = "public, max-age=86400, immutable"; }
    else if (ext == ".txt")        { ctype = "text/plain; charset=utf-8"; }
    else if (ext == ".map")        { ctype = "application/json; charset=utf-8"; }
    else if (ext == ".webmanifest"){ ctype = "application/manifest+json"; }

    send_file(fd, disk, ctype, head_only, cache);
}

void JukeboxServer::configure_client_socket(int fd) {
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

bool JukeboxServer::read_request(int fd, std::string& method, std::string& path,
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
    if (path.empty()) path = "/";
    return true;
}

void JukeboxServer::send_file(int fd, const std::string& disk_path, const char* content_type,
                              bool head_only, const char* cache_control) {
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
        << "Cache-Control: " << cache_control << "\r\n\r\n";
    if (!send_string(fd, hdr.str())) return;
    if (!head_only) {
        send_string(fd, payload);
    }
}

void JukeboxServer::send_not_found(int fd) {
    const std::string payload = "Not Found";
    std::ostringstream hdr;
    hdr << "HTTP/1.1 404 Not Found\r\n"
        << "Content-Type: text/plain; charset=utf-8\r\n"
        << "Content-Length: " << payload.size() << "\r\n"
        << "Connection: close\r\n\r\n";
    send_string(fd, hdr.str());
    send_string(fd, payload);
}

// ---------------------------------------------------------------------------
// Discord Activity shell (identical to StreamServer.cpp).
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

void JukeboxServer::send_json(int fd, int status, const char* status_text, const std::string& json) {
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

void JukeboxServer::send_discord_config(int fd) {
    const char* id = std::getenv("ACNH_DISCORD_CLIENT_ID");
    std::string cid = id ? id : "";
    std::string safe;
    for (char c : cid) {
        if (c == '"' || c == '\\' || c == '\n' || c == '\r') continue;
        safe.push_back(c);
    }
    send_json(fd, 200, "OK", "{\"client_id\":\"" + safe + "\"}");
}

void JukeboxServer::handle_token_exchange(int fd, const std::string& headers,
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

bool JukeboxServer::discord_token_exchange(const std::string& code, std::string& response_out) {
    const char* client_id = std::getenv("ACNH_DISCORD_CLIENT_ID");
    const char* client_secret = std::getenv("ACNH_DISCORD_CLIENT_SECRET");
    if (!client_id || !*client_id || !client_secret || !*client_secret) {
        response_out = "{\"error\":\"server_not_configured\","
                       "\"error_description\":\"ACNH_DISCORD_CLIENT_ID/SECRET unset\"}";
        return false;
    }

    std::string form = "client_id=" + url_encode(client_id)
                     + "&client_secret=" + url_encode(client_secret)
                     + "&grant_type=authorization_code"
                     + "&code=" + url_encode(code);

    int in_pipe[2];
    int out_pipe[2];
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
        _exit(127);
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
