#pragma once
#include <string>
#include <algorithm>
#include <cctype>
#include <vector>
#include <sstream>
#include <cmath>

inline std::string trim(const std::string& s) {
    size_t start = s.find_first_not_of(" \t\r\n\"'");
    if (start == std::string::npos) return "";
    size_t end = s.find_last_not_of(" \t\r\n\"'");
    return s.substr(start, end - start + 1);
}

inline std::string to_lower_copy(const std::string& s) {
    std::string out;
    out.reserve(s.size());
    for (char c : s) out.push_back((char)std::tolower((unsigned char)c));
    return out;
}

inline bool env_truthy(const char* value) {
    if (!value) return false;
    std::string lowered = to_lower_copy(trim(std::string(value)));
    return lowered == "1" || lowered == "true" || lowered == "yes" || lowered == "on";
}

inline bool parse_u64(const std::string& s, uint64_t& out) {
    if (s.empty()) return false;
    uint64_t value = 0;
    for (char c : s) {
        if (!std::isdigit((unsigned char)c)) return false;
        value = value * 10 + (uint64_t)(c - '0');
    }
    out = value;
    return true;
}

inline std::string get_header_value(const std::string& headers, const std::string& key) {
    if (headers.empty()) return {};
    std::string key_lower = to_lower_copy(key);
    size_t pos = 0;
    while (pos < headers.size()) {
        size_t end = headers.find("\r\n", pos);
        std::string line = headers.substr(pos, (end == std::string::npos) ? std::string::npos : end - pos);
        if (end == std::string::npos) {
            pos = headers.size();
        } else {
            pos = end + 2;
        }
        if (line.empty()) continue;
        size_t colon = line.find(':');
        if (colon == std::string::npos) continue;
        std::string name = line.substr(0, colon);
        std::string value = line.substr(colon + 1);
        name = trim(name);
        value = trim(value);
        if (to_lower_copy(name) == key_lower) return value;
    }
    return {};
}

inline int clamp16(int v) {
    if (v < -32768) return -32768;
    if (v > 32767) return 32767;
    return v;
}

inline uint16_t read_u16(const uint8_t* p) { return p[0] | (p[1] << 8); }
inline int16_t read_s16(const uint8_t* p) { return (int16_t)(p[0] | (p[1] << 8)); }
inline uint32_t read_u32(const uint8_t* p) { return p[0] | (p[1] << 8) | (p[2] << 16) | (p[3] << 24); }

inline int safe_stoi(const std::string& s, int def = 0) {
    try {
        if (s.empty()) return def;
        size_t idx = 0;
        int v = std::stoi(s, &idx, 10);
        return (idx == s.size()) ? v : def;
    } catch (...) {
        return def;
    }
}

