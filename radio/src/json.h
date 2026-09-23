/**
 * json — just enough JSON for the ether's wire, and base64 for its payloads.
 *
 * Every message the ether sends a station is one flat object of numbers and
 * strings. This reads exactly that: the top-level members that are numbers,
 * strings or booleans are kept, and anything nested (an array, an object) is
 * stepped over whole. A document that is not an object, or not well formed,
 * reads as nothing, and the caller ignores it as it ignores any message it does
 * not understand.
 *
 * Written here rather than borrowed because a station built for a plain POSIX
 * host can assume no JSON library, and both backends must read the wire the
 * same way.
 */
#pragma once

#include <stddef.h>
#include <stdint.h>

#include <map>
#include <string>

namespace simradio_json {

class Object {
public:
    /** False when `text` is not one well-formed JSON object. */
    bool parse(const char* text, size_t len);

    bool        has(const char* key) const;
    int64_t     num(const char* key, int64_t def) const;
    std::string str(const char* key, const char* def) const;

private:
    struct Field {
        bool        isNum = false;
        bool        isStr = false;
        double      num = 0;
        std::string str;
    };
    std::map<std::string, Field> fields_;
};

/** Standard base64 with padding. Returns the characters written, without a
 *  terminator; `out` is terminated when there is room. 0 when it does not fit. */
size_t base64Encode(const uint8_t* in, size_t n, char* out, size_t cap);

/** Decodes into `out`; returns the bytes written, or -1 on bad input or no room. */
long base64Decode(const char* in, size_t n, uint8_t* out, size_t cap);

}  // namespace simradio_json
