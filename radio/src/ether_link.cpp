/**
 * ether_link — see the header.
 *
 * The socket and the thread that reads it are the backend's (services.h), so
 * this file is the wire and nothing else: what goes out, and what an arriving
 * message does to which chip.
 */
#include "ether_link.h"

#include "json.h"
#include "model.h"
#include "services.h"
#include "simradio.h"

#include <cstdio>
#include <cstring>
#include <string>
#include <sys/socket.h>
#include <sys/types.h>

namespace {

constexpr size_t kMaxPayload = 256;

const struct simradio_services* S() { return simradio_services(); }

int  s_fd = -1;
int  s_sid = 1;
bool s_started = false;

void sendLine(const char* s, size_t n)
{
    if (s_fd < 0) return;
    ssize_t w = send(s_fd, s, n, MSG_DONTWAIT);
    (void)w;
}

void appendState(char* p, size_t cap, size_t* at, const EtherState& s)
{
    *at += (size_t)snprintf(p + *at, cap - *at,
        "\"slot\":%d,\"freq\":%u,\"bw\":%u,\"sf\":%d,\"cr\":%d,\"sync\":%d,"
        "\"hdr\":\"%s\",\"crc\":%s,\"pre\":%d",
        s.slot, (unsigned)s.freqHz, (unsigned)s.bwHz, s.sf, s.cr, s.syncWord,
        s.hdrImplicit ? "implicit" : "explicit", s.crc ? "true" : "false", s.preamble);
}

/* ---- Inbound ---- */

void handleMessage(const char* text, size_t len)
{
    simradio_json::Object msg;
    if (!msg.parse(text, len)) return;
    std::string type = msg.str("type", "");

    if (type == "rx_begin") {
        simradio* chip = modelChip((int)msg.num("slot", 0));
        if (chip) {
            VirtualRxBegin f = {};
            f.id       = (int)msg.num("id", 0);
            f.t0       = msg.num("t0", 0);
            f.tPre     = msg.num("t_pre", 0);
            f.tHdr     = msg.num("t_hdr", 0);
            f.tEnd     = msg.num("t_end", 0);
            f.levelDbm = (int)msg.num("level", kNoiseFloorDbm);
            modelRxBegin(chip, f);
        }
    } else if (type == "rx_end") {
        simradio* chip = modelChip((int)msg.num("slot", 0));
        if (chip) {
            uint8_t payload[kMaxPayload];
            long plen = 0;
            std::string b = msg.str("payload", "");
            if (!b.empty()) {
                plen = simradio_json::base64Decode(b.data(), b.size(), payload, sizeof payload);
                if (plen < 0) plen = 0;
            }
            std::string verdict = msg.str("verdict", "clean");
            VirtualRxEnd f = {};
            f.id       = (int)msg.num("id", 0);
            f.payload  = payload;
            f.len      = (size_t)plen;
            f.crcOk    = verdict != "crc" && verdict != "hdr";
            f.headerOk = verdict != "hdr";
            f.rssiDbm  = (int)msg.num("rssi", -80);
            f.snrDb    = (int)msg.num("snr", 10);
            modelRxEnd(chip, f);
        }
    } else if (type == "energy") {
        simradio* chip = modelChip((int)msg.num("slot", 0));
        if (chip) {
            VirtualRxBegin f = {};
            f.id       = (int)msg.num("id", 0);
            f.t0       = msg.num("t0", 0);
            f.tEnd     = msg.num("t_end", 0);
            f.levelDbm = (int)msg.num("level", kNoiseFloorDbm);
            modelEnergy(chip, f);
        }
    } else if (type == "welcome") {
        S()->log(SIMRADIO_LOG_INFO, "ether: %s at t0 %lld",
                 msg.str("mode", "real").c_str(), (long long)msg.num("t0", 0));
    }
    /* Anything else is a message this station does not understand. */
}

}  // namespace

/* ---- Outbound ---- */

void etherPublishState(const EtherState& s)
{
    if (s_fd < 0) return;
    char line[512];
    size_t at = (size_t)snprintf(line, sizeof line,
        "{\"type\":\"state\",\"sid\":%d,\"t\":%lld,\"mode\":\"%s\",\"ready_at\":%lld,",
        s_sid, (long long)S()->now_us(), s.mode, (long long)s.readyAt);
    appendState(line, sizeof line, &at, s);
    at += (size_t)snprintf(line + at, sizeof line - at, "}");
    sendLine(line, at);
}

void etherPublishTx(const EtherTxFrame& f)
{
    if (s_fd < 0) return;
    char payload[4 * ((kMaxPayload + 2) / 3) + 1];
    simradio_json::base64Encode(f.payload, f.len, payload, sizeof payload);

    char line[1024];
    size_t at = (size_t)snprintf(line, sizeof line,
        "{\"type\":\"tx\",\"sid\":%d,\"t\":%lld,\"id\":%d,"
        "\"t0\":%lld,\"t_pre\":%lld,\"t_hdr\":%lld,\"t_end\":%lld,\"power_dbm\":%d,",
        s_sid, (long long)S()->now_us(), f.id,
        (long long)f.t0, (long long)f.tPre, (long long)f.tHdr, (long long)f.tEnd,
        f.powerDbm);
    appendState(line, sizeof line, &at, f.state);
    at += (size_t)snprintf(line + at, sizeof line - at, ",\"payload\":\"%s\"}", payload);
    sendLine(line, at);
}

/* ---- Bring-up ---- */

extern "C" int simradio_station_open(int sid, const char* bind_addr, const char* ether_addr)
{
    S()->lock();
    if (s_started) { S()->unlock(); return 0; }
    s_started = true;
    s_sid = sid;
    S()->unlock();

    if (!ether_addr || !*ether_addr) {
        S()->log(SIMRADIO_LOG_INFO, "ether: none named, the radio transmits into nothing");
        return 0;
    }

    int fd = S()->udp_open(bind_addr && *bind_addr ? bind_addr : "127.0.0.1", ether_addr);
    if (fd < 0) {
        S()->log(SIMRADIO_LOG_ERROR, "ether: cannot reach %s", ether_addr);
        return -1;
    }
    s_fd = fd;

    char hello[160];
    int n = snprintf(hello, sizeof hello,
        "{\"type\":\"hello\",\"sid\":%d,\"t\":%lld,\"slots\":[0]}",
        s_sid, (long long)S()->now_us());
    sendLine(hello, (size_t)n);

    if (S()->spawn_reader(fd, handleMessage) != 0) {
        S()->log(SIMRADIO_LOG_ERROR, "ether: no reader");
        return -1;
    }
    S()->log(SIMRADIO_LOG_INFO, "ether: %s, station %d", ether_addr, s_sid);
    return 0;
}
