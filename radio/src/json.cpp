/**
 * json — see the header.
 */
#include "json.h"

#include <cstdlib>
#include <cstring>

namespace simradio_json {

namespace {

struct Reader {
    const char* p;
    const char* end;

    void ws()
    {
        while (p < end && (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r')) p++;
    }

    bool eat(char c)
    {
        ws();
        if (p < end && *p == c) { p++; return true; }
        return false;
    }

    static int hex(char c)
    {
        if (c >= '0' && c <= '9') return c - '0';
        if (c >= 'a' && c <= 'f') return c - 'a' + 10;
        if (c >= 'A' && c <= 'F') return c - 'A' + 10;
        return -1;
    }

    static void utf8(std::string& out, unsigned cp)
    {
        if (cp < 0x80) {
            out += (char)cp;
        } else if (cp < 0x800) {
            out += (char)(0xC0 | (cp >> 6));
            out += (char)(0x80 | (cp & 0x3F));
        } else if (cp < 0x10000) {
            out += (char)(0xE0 | (cp >> 12));
            out += (char)(0x80 | ((cp >> 6) & 0x3F));
            out += (char)(0x80 | (cp & 0x3F));
        } else {
            out += (char)(0xF0 | (cp >> 18));
            out += (char)(0x80 | ((cp >> 12) & 0x3F));
            out += (char)(0x80 | ((cp >> 6) & 0x3F));
            out += (char)(0x80 | (cp & 0x3F));
        }
    }

    /* A string, the opening quote already next. */
    bool string(std::string& out)
    {
        ws();
        if (p >= end || *p != '"') return false;
        p++;
        while (p < end && *p != '"') {
            char c = *p++;
            if (c != '\\') { out += c; continue; }
            if (p >= end) return false;
            char e = *p++;
            switch (e) {
                case '"': out += '"'; break;
                case '\\': out += '\\'; break;
                case '/': out += '/'; break;
                case 'b': out += '\b'; break;
                case 'f': out += '\f'; break;
                case 'n': out += '\n'; break;
                case 'r': out += '\r'; break;
                case 't': out += '\t'; break;
                case 'u': {
                    if (end - p < 4) return false;
                    unsigned cp = 0;
                    for (int i = 0; i < 4; i++) {
                        int h = hex(p[i]);
                        if (h < 0) return false;
                        cp = (cp << 4) | (unsigned)h;
                    }
                    p += 4;
                    utf8(out, cp);
                    break;
                }
                default: return false;
            }
        }
        if (p >= end) return false;
        p++;
        return true;
    }

    bool number(double& out)
    {
        ws();
        const char* start = p;
        if (p < end && (*p == '-' || *p == '+')) p++;
        while (p < end && ((*p >= '0' && *p <= '9') || *p == '.' || *p == 'e' ||
                           *p == 'E' || *p == '-' || *p == '+'))
            p++;
        if (p == start) return false;
        std::string text(start, (size_t)(p - start));
        char* stop = nullptr;
        out = strtod(text.c_str(), &stop);
        return stop && *stop == '\0';
    }

    bool literal(const char* word)
    {
        size_t n = strlen(word);
        if ((size_t)(end - p) < n || strncmp(p, word, n) != 0) return false;
        p += n;
        return true;
    }

    /* Any value, kept nowhere: what a nested member costs. */
    bool skip(int depth)
    {
        if (depth > 32) return false;
        ws();
        if (p >= end) return false;
        if (*p == '"') { std::string s; return string(s); }
        if (*p == '{' || *p == '[') {
            char close = *p == '{' ? '}' : ']';
            bool object = *p == '{';
            p++;
            if (eat(close)) return true;
            for (;;) {
                if (object) {
                    std::string k;
                    if (!string(k) || !eat(':')) return false;
                }
                if (!skip(depth + 1)) return false;
                if (eat(',')) continue;
                return eat(close);
            }
        }
        if (literal("true") || literal("false") || literal("null")) return true;
        double d;
        return number(d);
    }
};

}  // namespace

bool Object::parse(const char* text, size_t len)
{
    fields_.clear();
    Reader r{text, text + len};
    if (!r.eat('{')) return false;
    if (r.eat('}')) return true;
    for (;;) {
        std::string key;
        if (!r.string(key) || !r.eat(':')) { fields_.clear(); return false; }
        r.ws();
        if (r.p >= r.end) { fields_.clear(); return false; }
        Field f;
        char c = *r.p;
        if (c == '"') {
            if (!r.string(f.str)) { fields_.clear(); return false; }
            f.isStr = true;
            fields_[key] = f;
        } else if (c == '-' || (c >= '0' && c <= '9')) {
            if (!r.number(f.num)) { fields_.clear(); return false; }
            f.isNum = true;
            fields_[key] = f;
        } else if (r.literal("true")) {
            f.isNum = true;
            f.num = 1;
            fields_[key] = f;
        } else if (r.literal("false")) {
            f.isNum = true;
            f.num = 0;
            fields_[key] = f;
        } else if (!r.skip(0)) {
            fields_.clear();
            return false;
        }
        if (r.eat(',')) continue;
        if (r.eat('}')) return true;
        fields_.clear();
        return false;
    }
}

bool Object::has(const char* key) const
{
    return fields_.find(key) != fields_.end();
}

int64_t Object::num(const char* key, int64_t def) const
{
    auto it = fields_.find(key);
    return it != fields_.end() && it->second.isNum ? (int64_t)it->second.num : def;
}

std::string Object::str(const char* key, const char* def) const
{
    auto it = fields_.find(key);
    return it != fields_.end() && it->second.isStr ? it->second.str : std::string(def);
}

/* ---- base64 ---- */

static const char kAlphabet[] =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

size_t base64Encode(const uint8_t* in, size_t n, char* out, size_t cap)
{
    size_t need = 4 * ((n + 2) / 3);
    if (need > cap) {
        if (cap) out[0] = '\0';
        return 0;
    }
    size_t o = 0;
    for (size_t i = 0; i < n; i += 3) {
        uint32_t v = (uint32_t)in[i] << 16;
        if (i + 1 < n) v |= (uint32_t)in[i + 1] << 8;
        if (i + 2 < n) v |= in[i + 2];
        out[o++] = kAlphabet[(v >> 18) & 0x3F];
        out[o++] = kAlphabet[(v >> 12) & 0x3F];
        out[o++] = i + 1 < n ? kAlphabet[(v >> 6) & 0x3F] : '=';
        out[o++] = i + 2 < n ? kAlphabet[v & 0x3F] : '=';
    }
    if (o < cap) out[o] = '\0';
    return o;
}

long base64Decode(const char* in, size_t n, uint8_t* out, size_t cap)
{
    uint32_t acc = 0;
    int bits = 0;
    size_t o = 0;
    for (size_t i = 0; i < n; i++) {
        char c = in[i];
        if (c == '=') break;
        const char* at = strchr(kAlphabet, c);
        if (!at || c == '\0') return -1;
        acc = (acc << 6) | (uint32_t)(at - kAlphabet);
        bits += 6;
        if (bits >= 8) {
            bits -= 8;
            if (o >= cap) return -1;
            out[o++] = (uint8_t)((acc >> bits) & 0xFF);
        }
    }
    return (long)o;
}

}  // namespace simradio_json
