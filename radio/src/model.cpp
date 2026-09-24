/**
 * model — see the header.
 *
 * Everything that touches a chip's state does so under the services' lock,
 * because the bus caller reaches it from one side and the reader and the timer
 * thread reach it from the other. The DIO1 line is driven outside that lock:
 * raising it runs the host's pin callback on the calling thread, and a host's
 * interrupt handler issues SPI commands of its own.
 */
#include "simradio.h"

#include "ether_link.h"
#include "model.h"
#include "services.h"
#include "toa.h"

#include <cmath>
#include <cstdio>
#include <cstring>
#include <map>

/* ---- The SX126x command and register surface the drivers use ---- */

enum {
    CMD_RESET_STATS        = 0x00,
    CMD_CLEAR_IRQ_STATUS   = 0x02,
    CMD_CLEAR_DEVICE_ERR   = 0x07,
    CMD_SET_DIO_IRQ_PARAMS = 0x08,
    CMD_WRITE_REGISTER     = 0x0D,
    CMD_WRITE_BUFFER       = 0x0E,
    CMD_GET_STATS          = 0x10,
    CMD_GET_PACKET_TYPE    = 0x11,
    CMD_GET_IRQ_STATUS     = 0x12,
    CMD_GET_RX_BUF_STATUS  = 0x13,
    CMD_GET_PACKET_STATUS  = 0x14,
    CMD_GET_RSSI_INST      = 0x15,
    CMD_GET_DEVICE_ERRORS  = 0x17,
    CMD_READ_REGISTER      = 0x1D,
    CMD_READ_BUFFER        = 0x1E,
    CMD_SET_STANDBY        = 0x80,
    CMD_SET_RX             = 0x82,
    CMD_SET_TX             = 0x83,
    CMD_SET_SLEEP          = 0x84,
    CMD_SET_RF_FREQUENCY   = 0x86,
    CMD_SET_CAD_PARAMS     = 0x88,
    CMD_CALIBRATE          = 0x89,
    CMD_SET_PACKET_TYPE    = 0x8A,
    CMD_SET_MODULATION     = 0x8B,
    CMD_SET_PACKET_PARAMS  = 0x8C,
    CMD_SET_TX_PARAMS      = 0x8E,
    CMD_SET_BUFFER_BASE    = 0x8F,
    CMD_SET_RXTX_FALLBACK  = 0x93,
    CMD_SET_PA_CONFIG      = 0x95,
    CMD_SET_REGULATOR      = 0x96,
    CMD_SET_DIO3_TCXO      = 0x97,
    CMD_CALIBRATE_IMAGE    = 0x98,
    CMD_SET_DIO2_RF_SWITCH = 0x9D,
    CMD_STOP_TIMER_ON_PRE  = 0x9F,
    CMD_SET_LORA_SYMB_TO   = 0xA0,
    CMD_GET_STATUS         = 0xC0,
    CMD_SET_FS             = 0xC1,
    CMD_SET_CAD            = 0xC5,
};

enum {
    IRQ_TX_DONE           = 1u << 0,
    IRQ_RX_DONE           = 1u << 1,
    IRQ_PREAMBLE_DETECTED = 1u << 2,
    IRQ_SYNC_WORD_VALID   = 1u << 3,
    IRQ_HEADER_VALID      = 1u << 4,
    IRQ_HEADER_ERR        = 1u << 5,
    IRQ_CRC_ERR           = 1u << 6,
    IRQ_CAD_DONE          = 1u << 7,
    IRQ_CAD_DETECTED      = 1u << 8,
};

/* SetCadParams' exit mode: back to standby, or straight into RX when
 * something was found. */
enum { CAD_ONLY = 0x00, CAD_RX = 0x01 };

enum {
    REG_VERSION_STRING  = 0x0320,
    REG_IQ_CONFIG       = 0x0736,
    REG_SYNC_WORD_MSB   = 0x0740,
    REG_SYNC_WORD_LSB   = 0x0741,
    REG_SENSITIVITY     = 0x0889,
    REG_RX_GAIN         = 0x08AC,
    REG_TX_CLAMP        = 0x08D8,
    REG_OCP             = 0x08E7,
    REG_RTC_CTRL        = 0x0902,
    REG_EVENT_MASK      = 0x0944,
};

/* The status byte's mode field, and the one command-status value that is
 * neither an error nor silence. */
enum {
    ST_STDBY_RC   = 0x20,
    ST_STDBY_XOSC = 0x30,
    ST_FS         = 0x40,
    ST_RX         = 0x50,
    ST_TX         = 0x60,
    ST_DATA_AVAIL = 0x04,
    ST_CMD_INVALID = 0x08,
};

static const struct simradio_services* S() { return simradio_services(); }

/* The LoRa bandwidth register codes, in hertz. */
static uint32_t bwFromCode(uint8_t code)
{
    switch (code) {
        case 0x00: return 7810;
        case 0x08: return 10420;
        case 0x01: return 15630;
        case 0x09: return 20830;
        case 0x02: return 31250;
        case 0x0A: return 41670;
        case 0x03: return 62500;
        case 0x04: return 125000;
        case 0x05: return 250000;
        case 0x06: return 500000;
        default:   return 125000;
    }
}

/* What the SX1262 radiates for a PA configuration and a SetTxParams power.
 * The power register alone does not say: the chip reaches +14 dBm with the
 * register at 20 and the PA throttled (paDutyCycle 1, hpMax 4), and RadioLib's
 * setOutputPower picks exactly such throttled settings for every power below
 * +22. The table is RadioLib's paOptTable, measured on hardware
 * (jgromes/RadioLib#1628): entry i radiates i − 9 dBm. A triple the table does
 * not name falls back to the datasheet's reference points — full PA radiates
 * the register value, and the three throttled settings the datasheet lists for
 * +20/+17/+14 sit 2/5/8 dB under it. */
struct PaSetting { uint8_t dutyCycle, hpMax; int8_t paVal; };

static const PaSetting kPaMeasured[32] = {
    {2, 2, -5}, {2, 1, 0},  {1, 1, 3},  {1, 2, 0},  {1, 1, 6},  {1, 2, 3},
    {2, 2, 2},  {4, 1, 6},  {1, 1, 11}, {2, 1, 11}, {1, 1, 14}, {2, 1, 14},
    {1, 1, 20}, {1, 1, 22}, {2, 2, 11}, {3, 1, 21}, {1, 2, 17}, {4, 2, 13},
    {1, 2, 20}, {1, 2, 22}, {2, 2, 21}, {3, 2, 21}, {1, 4, 19}, {1, 4, 20},
    {3, 3, 20}, {2, 5, 19}, {1, 6, 22}, {2, 5, 22}, {3, 5, 22}, {3, 6, 22},
    {4, 6, 22}, {4, 7, 22},
};

static int radiatedDbm(uint8_t dutyCycle, uint8_t hpMax, int8_t paVal)
{
    for (int i = 0; i < 32; i++) {
        const PaSetting& s = kPaMeasured[i];
        if (s.dutyCycle == dutyCycle && s.hpMax == hpMax && s.paVal == paVal)
            return i - 9;
    }
    int offset = 0;
    if      (dutyCycle == 3 && hpMax == 5) offset = -2;
    else if (dutyCycle == 2 && hpMax == 3) offset = -5;
    else if (dutyCycle == 2 && hpMax == 2) offset = -8;
    int dbm = paVal + offset;
    return dbm < -9 ? -9 : dbm > 22 ? 22 : dbm;
}

/* The chip's state: everything a power cycle puts back. */
struct ChipState {
    const char* mode = "STDBY_RC";
    uint8_t     modeBits = ST_STDBY_RC;
    uint8_t     fallbackBits = ST_STDBY_RC;
    const char* fallbackMode = "STDBY_RC";

    uint16_t irqStatus = 0;
    uint16_t irqMask = 0;
    uint16_t dio1Mask = 0;

    std::map<uint16_t, uint8_t> regs;
    uint8_t  buf[256] = {};
    uint8_t  txBase = 0, rxBase = 0;

    uint32_t freqHz = 869525000;
    uint32_t bwHz = 125000;
    int      sf = 8;
    int      cr = 5;
    int      preamble = 8;
    bool     hdrImplicit = false;
    bool     crcOn = true;
    uint8_t  payloadLen = 0;
    int      paVal = 14;
    uint8_t  paDutyCycle = 4;
    uint8_t  paHpMax = 7;

    uint8_t  rxLen = 0, rxPtr = 0;
    uint8_t  rssiPkt = 220, snrPkt = 40, sigRssiPkt = 220;

    /* The air, and the one frame being demodulated out of it. A receiver
     * follows a single frame at a time: the rest is energy, which is what an
     * instantaneous RSSI reads and what carrier sense acts on. */
    int      airLevel = 0;           /* the strongest level in flight */
    int64_t  airEndUs = 0;           /* when the last of it is over */
    int      lockId = 0;             /* the frame this receiver is following */
    int      lockLevel = 0;
    int64_t  lockEndUs = 0;

    /* When the last frame this antenna has been told of leaves the air. Not
     * the chip's state but the air's, so no mode change clears it: a driver
     * goes RX → standby → CAD, and the frame it was hearing is still in the
     * air when the CAD looks. The medium tells a station about a frame only
     * while it listens, so this holds the frames that began while it did. */
    int64_t  heardEndUs = 0;

    /* Channel activity detection, as SetCadParams left it. */
    int      cadSymbols = 2;
    uint8_t  cadDetPeak = 0, cadDetMin = 0;
    uint8_t  cadExitMode = CAD_ONLY;

    int      txId = 0;

    VirtualRxEnd pendingEnd = {};
    uint8_t      pendingPayload[256] = {};
    size_t       pendingLen = 0;
    bool         pendingValid = false;

    ChipState()
    {
        /* The datasheet reset values the drivers read-modify-write. Anything
         * a driver writes and reads back lands in the map on the way past;
         * these are the ones it reads before it has written them. */
        regs[REG_SENSITIVITY] = 0x94;
        regs[REG_TX_CLAMP]    = 0x18;
        regs[REG_IQ_CONFIG]   = 0x0D;
        regs[REG_RTC_CTRL]    = 0x00;
        regs[REG_EVENT_MASK]  = 0x00;
        regs[REG_RX_GAIN]     = 0x94;
        regs[REG_OCP]         = 0x38;
        regs[REG_SYNC_WORD_MSB] = 0x14;
        regs[REG_SYNC_WORD_LSB] = 0x24;

        /* The part answers for itself at the version register. An SX1262
         * reports "SX1261" there — the two are the same silicon — and that is
         * the string drivers match on. */
        static const char kVersion[16] = { 'S','X','1','2','6','1',' ','V','2','D',' ','2','D','0','2', 0 };
        for (int i = 0; i < 16; i++)
            regs[(uint16_t)(REG_VERSION_STRING + i)] = (uint8_t)kVersion[i];
    }

    uint8_t status() const { return (uint8_t)(modeBits | ST_DATA_AVAIL); }
};

struct simradio {
    int  slot = 0;
    void (*onPin)(void*, int, int) = nullptr;
    void* ctx = nullptr;

    ChipState st;

    /* Every scheduled instant of a frame in flight, in or out. Created on
     * first use and kept for the chip's life. */
    void* tTxDone = nullptr;
    void* tPre = nullptr;
    void* tHdr = nullptr;
    void* tCad = nullptr;
};

namespace {

constexpr int kMaxSlots = 8;
simradio* s_chips[kMaxSlots] = {};

/* Who to tell when DIO1 moves, taken under the lock and called outside it. */
struct PinCall {
    void (*fn)(void*, int, int) = nullptr;
    void* ctx = nullptr;
    bool  high = false;

    void operator()() const { if (fn) fn(ctx, SIMRADIO_PIN_DIO1, high ? 1 : 0); }
};

PinCall dio1Of(const simradio* c)
{
    PinCall p;
    p.fn = c->onPin;
    p.ctx = c->ctx;
    p.high = (c->st.irqStatus & c->st.dio1Mask) != 0;
    return p;
}

/* The sync word as the ether matches on it: the two nibble-expanded register
 * bytes read back as the one 8-bit word the driver set. */
uint8_t syncWordOf(const ChipState& d)
{
    auto msb = d.regs.find(REG_SYNC_WORD_MSB);
    auto lsb = d.regs.find(REG_SYNC_WORD_LSB);
    uint8_t m = msb == d.regs.end() ? 0x14 : msb->second;
    uint8_t l = lsb == d.regs.end() ? 0x24 : lsb->second;
    return (uint8_t)((m & 0xF0) | ((l & 0xF0) >> 4));
}

void fillState(const simradio* c, EtherState& s)
{
    const ChipState& d = c->st;
    s.slot        = c->slot;
    s.mode        = d.mode;
    s.readyAt     = S()->now_us();   /* transitions are instantaneous here */
    s.freqHz      = d.freqHz;
    s.bwHz        = d.bwHz;
    s.sf          = d.sf;
    s.cr          = d.cr;
    s.syncWord    = syncWordOf(d);
    s.hdrImplicit = d.hdrImplicit;
    s.crc         = d.crcOn;
    s.preamble    = d.preamble;
}

void setMode(ChipState& d, const char* mode, uint8_t bits)
{
    d.mode = mode;
    d.modeBits = bits;
}

/* Arm a one-shot on this chip, making the timer the first time. Starting a
 * running timer restarts it — the services promise that. */
void armOnce(simradio* c, void** h, void (*cb)(void*), int64_t delayUs)
{
    if (!*h) *h = S()->timer_create(cb, c, "sx126x");
    if (!*h) return;
    S()->timer_start_once(*h, delayUs < 0 ? 0 : delayUs);
}

void stopTimer(void* h)
{
    if (h) S()->timer_stop(h);
}

/* The demodulator lets go of the frame it was following. */
void dropLock(simradio* c)
{
    ChipState& d = c->st;
    d.lockId = 0;
    d.lockEndUs = 0;
    d.pendingValid = false;
    stopTimer(c->tPre);
    stopTimer(c->tHdr);
}

/* Leaving RX for anything but CAD abandons whatever was arriving, the energy
 * with it. CAD keeps the energy: a frame the receiver was following is still
 * on the air, and it is what a CAD is for. */
void abandonReception(simradio* c)
{
    c->st.airLevel = 0;
    c->st.airEndUs = 0;
    dropLock(c);
}

void txDoneCb(void* arg);
void rxPreCb(void* arg);
void rxHdrCb(void* arg);
void rxEndCb(void* arg);
void cadDoneCb(void* arg);

}  // namespace

/* ---- The registry ---- */

simradio* modelChip(int slot)
{
    if (slot < 0 || slot >= kMaxSlots) return nullptr;
    S()->lock();
    simradio* c = s_chips[slot];
    S()->unlock();
    return c;
}

extern "C" simradio_t* simradio_open(int slot, void (*on_pin)(void*, int, int), void* ctx)
{
    if (slot < 0 || slot >= kMaxSlots) return nullptr;
    S()->lock();
    simradio* c = s_chips[slot];
    if (!c) {
        c = new simradio();
        c->slot = slot;
        s_chips[slot] = c;
    } else {
        /* A chip opened again is a chip powered up again. The object itself
         * lives for the process, because a timer may be about to fire on it. */
        stopTimer(c->tTxDone);
        stopTimer(c->tPre);
        stopTimer(c->tHdr);
        stopTimer(c->tCad);
        c->st = ChipState();
    }
    c->onPin = on_pin;
    c->ctx = ctx;
    S()->unlock();
    return c;
}

extern "C" void simradio_close(simradio_t* c)
{
    if (!c) return;
    S()->lock();
    stopTimer(c->tTxDone);
    stopTimer(c->tPre);
    stopTimer(c->tHdr);
    stopTimer(c->tCad);
    c->onPin = nullptr;
    c->ctx = nullptr;
    S()->unlock();
}

extern "C" int64_t simradio_now_us(void)
{
    return S()->now_us();
}

/* ---- The lines ---- */

extern "C" int simradio_pin(simradio_t* c, int pin)
{
    if (!c) return 0;
    /* BUSY is never busy: the model answers a command the moment it is
     * handed one, so the line a driver polls is always clear. */
    if (pin != SIMRADIO_PIN_DIO1) return 0;
    S()->lock();
    int high = (c->st.irqStatus & c->st.dio1Mask) != 0;
    S()->unlock();
    return high;
}

extern "C" void simradio_reset(simradio_t* c)
{
    if (!c) return;
    S()->lock();
    setMode(c->st, "STDBY_RC", ST_STDBY_RC);
    c->st.irqStatus = 0;
    PinCall pin = dio1Of(c);
    S()->unlock();
    pin();
}

/* ---- The command interpreter ---- */

extern "C" void simradio_transfer(simradio_t* c, const uint8_t* out, size_t len, uint8_t* in)
{
    if (!c || !out || !in || len == 0) return;

    const uint8_t op = out[0];
    bool     publishState = false;
    bool     publishTx = false;
    EtherTxFrame frame = {};
    EtherState   snap = {};
    uint8_t  txPayload[256];

    S()->lock();
    ChipState& d = c->st;

    /* Every byte of the reply is the status until the data starts. */
    memset(in, d.status(), len);

    switch (op) {
    case CMD_SET_SLEEP:
        setMode(d, "SLEEP", ST_STDBY_RC);
        publishState = true;
        break;

    case CMD_SET_STANDBY:
        if (len >= 2 && out[1] == 0x01) setMode(d, "STDBY_XOSC", ST_STDBY_XOSC);
        else                            setMode(d, "STDBY_RC", ST_STDBY_RC);
        publishState = true;
        break;

    case CMD_SET_FS:
        setMode(d, "FS", ST_FS);
        publishState = true;
        break;

    case CMD_SET_RX:
        setMode(d, "RX", ST_RX);
        publishState = true;
        break;

    case CMD_SET_TX: {
        setMode(d, "TX", ST_TX);
        /* `cr` is already the denominator of 4/n, which is what the formula
         * takes — a frame timed with anything outside 5..8 comes back as no
         * time on the air at all, and then nothing in the medium can ever
         * collide with anything. */
        double toa = loraToaSeconds(d.sf, (int)d.bwHz, d.cr, d.preamble,
                                    d.payloadLen, d.hdrImplicit, d.crcOn);
        double tSym = (double)((uint32_t)1 << d.sf) / (double)d.bwHz;
        int64_t now = S()->now_us();
        fillState(c, frame.state);
        frame.id   = ++d.txId;
        frame.t0   = now;
        frame.tPre = now + (int64_t)((d.preamble + 4.25) * tSym * 1e6);
        frame.tHdr = frame.tPre + (int64_t)(8.0 * tSym * 1e6);
        frame.tEnd = now + (int64_t)(toa * 1e6);
        frame.powerDbm = radiatedDbm(d.paDutyCycle, d.paHpMax, (int8_t)d.paVal);
        for (int i = 0; i < d.payloadLen; i++) txPayload[i] = d.buf[(uint8_t)(d.txBase + i)];
        frame.payload = txPayload;
        frame.len = d.payloadLen;
        publishTx = true;
        armOnce(c, &c->tTxDone, txDoneCb, frame.tEnd - now);
        break;
    }

    case CMD_SET_RF_FREQUENCY:
        if (len >= 5) {
            uint32_t frf = ((uint32_t)out[1] << 24) | ((uint32_t)out[2] << 16) |
                           ((uint32_t)out[3] << 8) | out[4];
            d.freqHz = (uint32_t)(((uint64_t)frf * 32000000ULL) >> 25);
            publishState = true;
        }
        break;

    case CMD_SET_MODULATION:
        if (len >= 4) {
            d.sf   = out[1];
            d.bwHz = bwFromCode(out[2]);
            d.cr   = out[3] + 4;          /* the register holds 4/(4+n) */
            if (d.cr < 5) d.cr = 5;
            if (d.cr > 8) d.cr = 8;
            publishState = true;
        }
        break;

    case CMD_SET_PACKET_PARAMS:
        if (len >= 7) {
            d.preamble    = ((int)out[1] << 8) | out[2];
            d.hdrImplicit = out[3] != 0;
            d.payloadLen  = out[4];
            d.crcOn       = out[5] != 0;
            publishState = true;
        }
        break;

    case CMD_SET_TX_PARAMS:
        if (len >= 2) d.paVal = (int8_t)out[1];
        break;

    case CMD_SET_PA_CONFIG:
        if (len >= 3) {
            d.paDutyCycle = out[1];
            d.paHpMax     = out[2];
        }
        break;

    case CMD_SET_BUFFER_BASE:
        if (len >= 3) { d.txBase = out[1]; d.rxBase = out[2]; }
        break;

    case CMD_WRITE_REGISTER:
        if (len >= 3) {
            uint16_t addr = ((uint16_t)out[1] << 8) | out[2];
            for (size_t i = 3; i < len; i++) d.regs[(uint16_t)(addr + (i - 3))] = out[i];
            publishState = true;   /* the sync word lives here */
        }
        break;

    case CMD_READ_REGISTER:
        if (len >= 4) {
            uint16_t addr = ((uint16_t)out[1] << 8) | out[2];
            for (size_t i = 4; i < len; i++) {
                auto it = d.regs.find((uint16_t)(addr + (i - 4)));
                in[i] = it == d.regs.end() ? 0x00 : it->second;
            }
        }
        break;

    case CMD_WRITE_BUFFER:
        if (len >= 2) {
            uint8_t off = out[1];
            for (size_t i = 2; i < len; i++) d.buf[(uint8_t)(off + (i - 2))] = out[i];
        }
        break;

    case CMD_READ_BUFFER:
        if (len >= 3) {
            uint8_t off = out[1];
            for (size_t i = 3; i < len; i++) in[i] = d.buf[(uint8_t)(off + (i - 3))];
        }
        break;

    case CMD_SET_DIO_IRQ_PARAMS:
        if (len >= 5) {
            d.irqMask  = (uint16_t)(((uint16_t)out[1] << 8) | out[2]);
            d.dio1Mask = (uint16_t)(((uint16_t)out[3] << 8) | out[4]);
        }
        break;

    case CMD_GET_IRQ_STATUS:
        if (len >= 4) {
            in[2] = (uint8_t)(d.irqStatus >> 8);
            in[3] = (uint8_t)(d.irqStatus & 0xFF);
        }
        break;

    case CMD_CLEAR_IRQ_STATUS:
        if (len >= 3) {
            uint16_t clear = (uint16_t)(((uint16_t)out[1] << 8) | out[2]);
            d.irqStatus &= (uint16_t)~clear;
        }
        break;

    case CMD_GET_RX_BUF_STATUS:
        if (len >= 4) { in[2] = d.rxLen; in[3] = d.rxPtr; }
        break;

    case CMD_GET_PACKET_STATUS:
        if (len >= 5) { in[2] = d.rssiPkt; in[3] = d.snrPkt; in[4] = d.sigRssiPkt; }
        break;

    case CMD_GET_RSSI_INST:
        if (len >= 3) {
            /* The chip's -x/2 encoding, and nothing at all outside RX. */
            int dbm = S()->now_us() < d.airEndUs ? d.airLevel : kNoiseFloorDbm;
            in[2] = strcmp(d.mode, "RX") == 0 ? (uint8_t)(-2 * dbm) : 0xFF;
        }
        break;

    case CMD_GET_PACKET_TYPE:
        if (len >= 3) in[2] = 0x01;      /* LoRa */
        break;

    case CMD_GET_STATUS:
        if (len >= 2) in[1] = d.status();
        break;

    case CMD_GET_DEVICE_ERRORS:
        if (len >= 4) { in[2] = 0; in[3] = 0; }
        break;

    case CMD_GET_STATS:
        for (size_t i = 2; i < len; i++) in[i] = 0;
        break;

    /* Accepted and without effect at this depth. */
    case CMD_SET_PACKET_TYPE:
    case CMD_SET_REGULATOR:
    case CMD_CALIBRATE:
    case CMD_CALIBRATE_IMAGE:
    case CMD_SET_DIO2_RF_SWITCH:
    case CMD_SET_DIO3_TCXO:
    case CMD_STOP_TIMER_ON_PRE:
    case CMD_SET_LORA_SYMB_TO:
    case CMD_CLEAR_DEVICE_ERR:
    case CMD_RESET_STATS:
        break;

    case CMD_SET_CAD_PARAMS:
        if (len >= 5) {
            /* cadSymbolNum 0x00..0x04 is 1, 2, 4, 8, 16 symbols; the timeout
             * (bytes 5..7) only matters to CAD_RX's receive, which ends at
             * the frame here. */
            d.cadSymbols  = 1 << (out[1] > 4 ? 4 : out[1]);
            d.cadDetPeak  = out[2];
            d.cadDetMin   = out[3];
            d.cadExitMode = out[4] == CAD_RX ? CAD_RX : CAD_ONLY;
        }
        break;

    case CMD_SET_CAD: {
        /* The datasheet reports RX in the status byte during CAD; the wire
         * says CAD, which is what the medium delivers energy to. */
        setMode(d, "CAD", ST_RX);
        double tSym = (double)((uint32_t)1 << d.sf) / (double)d.bwHz;
        armOnce(c, &c->tCad, cadDoneCb, (int64_t)(d.cadSymbols * tSym * 1e6));
        publishState = true;
        break;
    }

    case CMD_SET_RXTX_FALLBACK:
        if (len >= 2) {
            switch (out[1]) {
                case 0x40: d.fallbackMode = "FS";         d.fallbackBits = ST_FS; break;
                case 0x30: d.fallbackMode = "STDBY_XOSC"; d.fallbackBits = ST_STDBY_XOSC; break;
                default:   d.fallbackMode = "STDBY_RC";   d.fallbackBits = ST_STDBY_RC; break;
            }
        }
        break;

    default:
        memset(in, (uint8_t)(d.modeBits | ST_CMD_INVALID), len);
        break;
    }

    if (op == CMD_SET_STANDBY || op == CMD_SET_SLEEP || op == CMD_SET_FS || op == CMD_SET_TX)
        abandonReception(c);
    if (op == CMD_SET_CAD)
        dropLock(c);
    /* Any other mode ends a CAD in progress without an answer. */
    if (op == CMD_SET_STANDBY || op == CMD_SET_SLEEP || op == CMD_SET_FS ||
        op == CMD_SET_TX || op == CMD_SET_RX)
        stopTimer(c->tCad);

    if (publishState) fillState(c, snap);
    PinCall pin = dio1Of(c);
    S()->unlock();

    if (publishTx)    etherPublishTx(frame);
    if (publishState) etherPublishState(snap);
    pin();
}

/* ---- Timer callbacks ---- */

namespace {

void raise(simradio* c, uint16_t bits)
{
    S()->lock();
    c->st.irqStatus |= bits;
    PinCall pin = dio1Of(c);
    S()->unlock();
    pin();
}

/* TX_DONE lands at the end of the frame, and the chip falls back to whatever
 * SetRxTxFallbackMode named. */
void txDoneCb(void* arg)
{
    auto* c = (simradio*)arg;
    EtherState s;
    S()->lock();
    setMode(c->st, c->st.fallbackMode, c->st.fallbackBits);
    fillState(c, s);
    S()->unlock();
    etherPublishState(s);
    raise(c, IRQ_TX_DONE);
}

void rxPreCb(void* arg)
{
    raise((simradio*)arg, IRQ_PREAMBLE_DETECTED | IRQ_SYNC_WORD_VALID);
}

void rxHdrCb(void* arg)
{
    raise((simradio*)arg, IRQ_HEADER_VALID);
}

void rxEndCb(void* arg)
{
    auto* c = (simradio*)arg;
    uint16_t bits = 0;
    S()->lock();
    ChipState& d = c->st;
    if (d.pendingValid) {
        for (size_t i = 0; i < d.pendingLen; i++)
            d.buf[(uint8_t)(d.rxBase + i)] = d.pendingPayload[i];
        d.rxLen = (uint8_t)d.pendingLen;
        d.rxPtr = d.rxBase;
        d.rssiPkt    = (uint8_t)(-2 * d.pendingEnd.rssiDbm);
        d.sigRssiPkt = d.rssiPkt;
        d.snrPkt     = (uint8_t)(int8_t)(d.pendingEnd.snrDb * 4);
        bits = IRQ_RX_DONE;
        if (!d.pendingEnd.crcOk)    bits |= IRQ_CRC_ERR;
        if (!d.pendingEnd.headerOk) bits |= IRQ_HEADER_ERR;
        d.pendingValid = false;
    }
    S()->unlock();
    if (bits) raise(c, bits);
}

/* The CAD window is over: done, and detected if energy is in the air at this
 * antenna now. The chip then goes where SetCadParams said. */
void cadDoneCb(void* arg)
{
    auto* c = (simradio*)arg;
    ChipState& d = c->st;
    EtherState s;
    S()->lock();
    if (strcmp(d.mode, "CAD") != 0) { S()->unlock(); return; }   /* ended by a command */
    bool detected = S()->now_us() < d.heardEndUs;
    uint16_t bits = IRQ_CAD_DONE | (detected ? IRQ_CAD_DETECTED : 0);
    if (detected && d.cadExitMode == CAD_RX) setMode(d, "RX", ST_RX);
    else                                     setMode(d, "STDBY_RC", ST_STDBY_RC);
    fillState(c, s);
    S()->unlock();
    etherPublishState(s);
    raise(c, bits);
}

}  // namespace

/* ---- What the ether hands back ---- */

void modelRxBegin(simradio* c, const VirtualRxBegin& f)
{
    int64_t now = S()->now_us();
    S()->lock();
    ChipState& d = c->st;
    bool rx = strcmp(d.mode, "RX") == 0;
    bool cad = strcmp(d.mode, "CAD") == 0;
    if (!rx && !cad) { S()->unlock(); return; }

    /* Energy first: every frame in the air raises the instantaneous reading,
     * whether or not this receiver is following it. */
    int64_t endUs = now + (f.tEnd - f.t0);
    if (now >= d.airEndUs) d.airLevel = 0;
    if (f.levelDbm > d.airLevel || d.airLevel == 0) d.airLevel = f.levelDbm;
    if (endUs > d.airEndUs) d.airEndUs = endUs;
    if (endUs > d.heardEndUs) d.heardEndUs = endUs;

    /* A CAD senses; it demodulates nothing. */
    if (cad) { S()->unlock(); return; }

    /* Then the demodulator, which follows one frame at a time. A frame that
     * starts while another is being demodulated is not received at all unless
     * it takes the receiver, in which case the receiver drops what it had and
     * follows the new frame instead. The ether says whether it does, because
     * the ether rules on which of the two survives; an ether that does not say
     * is taken to mean the capture margin. Without this the chip would hand up
     * whichever frame ended last and the medium's verdict — which says only
     * one of them survived — would mean nothing. */
    bool busy = now < d.lockEndUs;
    bool takes = f.takes >= 0 ? f.takes == 1 : f.levelDbm >= d.lockLevel + kCaptureDb;
    if (busy && !takes) {
        S()->unlock();
        return;
    }
    d.lockId    = f.id;
    d.lockLevel = f.levelDbm;
    d.lockEndUs = now + (f.tEnd - f.t0);

    /* The sender's stamps are its own clock's; only the gaps between them mean
     * anything here, and they are measured from this instant. */
    armOnce(c, &c->tPre, rxPreCb, f.tPre - f.t0);
    armOnce(c, &c->tHdr, rxHdrCb, f.tHdr - f.t0);
    S()->unlock();
}

void modelRxEnd(simradio* c, const VirtualRxEnd& f)
{
    S()->lock();
    ChipState& d = c->st;
    if (strcmp(d.mode, "RX") != 0 || f.id != d.lockId) { S()->unlock(); return; }
    d.lockEndUs = 0;
    size_t n = f.len > sizeof(d.pendingPayload) ? sizeof(d.pendingPayload) : f.len;
    if (f.payload && n) memcpy(d.pendingPayload, f.payload, n);
    d.pendingLen = n;
    d.pendingEnd = f;
    d.pendingEnd.payload = nullptr;
    d.pendingValid = true;
    S()->unlock();

    rxEndCb(c);
}
