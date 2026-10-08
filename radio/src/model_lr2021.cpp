/**
 * model_lr2021 — the LR2021 behind the C ABI, LoRa only.
 *
 * The same contract as the SX1262's model (model.h): the driver above it is
 * the one that runs on a board, unchanged, and this is the other end of its
 * bus. The LR2021 speaks another command set: two-byte opcodes in three groups
 * (00 FIFO, 01 system, 02 radio), replies read in a second NSS frame that
 * begins 00 00, a 32-bit interrupt word cleared by the read that reports it,
 * receive and transmit FIFOs instead of one buffer, 32-bit registers, and a
 * timestamp unit that says how long ago a frame's last bit came.
 *
 * What a board showed of this part, and what the model therefore does (bench
 * notes of the XIAO nRF54L15 + Wio-LR2021 board, October 2026):
 * - in continuous receive every frame is appended to the RX FIFO at its last
 *   bit, so two can wait there for a driver that read late;
 * - GetTimestampValue counts 32 MHz ticks back from the command to the event;
 * - GetTemp reads zero while receiving;
 * - receiving with no front-end calibration since power-up latches device
 *   error 0x0200;
 * - SetRxPath only takes from standby;
 * - the sync word is one byte, the SX127x form (0x12 is the SX126x's 0x1424).
 *
 * BUSY is the SX1262's model's (busy.h): never busy unless a run gives the
 * board's figures, and then high for a command's time and the host's, the
 * radio deaf meanwhile after a retune (`lrBusy`).
 *
 * Not modelled yet: FLRC, FSK and the chip's other engines; duty-cycled
 * receive beyond a plain one.
 */
#include "simradio.h"

#include "busy.h"
#include "conductor.h"
#include "ether_link.h"
#include "json.h"
#include "model.h"
#include "services.h"
#include "toa.h"

#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <map>
#include <mutex>
#include <string>
#include <vector>

/* ---- The LR2021 command surface the reticulum-lr2021 driver uses ---- */

enum {
    G_FIFO = 0x00,
    G_SYS = 0x01,
    G_RADIO = 0x02,
};

enum {  /* group 00 */
    FIFO_READ_RX = 0x01,
    FIFO_WRITE_TX = 0x02,
};

enum {  /* group 01 */
    SYS_GET_STATUS = 0x00,
    SYS_GET_VERSION = 0x01,
    SYS_WRITE_REG32 = 0x04,
    SYS_WRITE_REG32_MASK = 0x05,
    SYS_READ_REG32 = 0x06,
    SYS_GET_ERRORS = 0x10,
    SYS_CLEAR_ERRORS = 0x11,
    SYS_SET_DIO_FUNCTION = 0x12,
    SYS_SET_DIO_IRQ_CONFIG = 0x15,
    SYS_CLEAR_IRQ = 0x16,
    SYS_GET_AND_CLEAR_IRQ = 0x17,
    SYS_CONFIG_LF_CLOCK = 0x18,
    SYS_GET_RX_FIFO_LEVEL = 0x1C,
    SYS_GET_TX_FIFO_LEVEL = 0x1D,
    SYS_CLEAR_RX_FIFO = 0x1E,
    SYS_CLEAR_TX_FIFO = 0x1F,
    SYS_SET_TCXO_MODE = 0x20,
    SYS_SET_REG_MODE = 0x21,
    SYS_CALIBRATE = 0x22,
    SYS_CALIB_FE = 0x23,
    SYS_GET_TEMP = 0x25,
    SYS_SET_SLEEP = 0x27,
    SYS_SET_STANDBY = 0x28,
    SYS_SET_FS = 0x29,
    SYS_SET_RETAIN = 0x2A,
};

enum {  /* group 02 */
    RADIO_SET_RF_FREQUENCY = 0x00,
    RADIO_SET_RX_PATH = 0x01,
    RADIO_SET_PA_CONFIG = 0x02,
    RADIO_SET_TX_PARAMS = 0x03,
    RADIO_SET_FALLBACK = 0x06,
    RADIO_SET_PACKET_TYPE = 0x07,
    RADIO_GET_RSSI_INST = 0x0B,
    RADIO_SET_RX = 0x0C,
    RADIO_SET_TX = 0x0D,
    RADIO_SEL_PA = 0x0F,
    RADIO_SET_RX_DUTY_CYCLE = 0x10,
    RADIO_SET_TIMESTAMP_SOURCE = 0x16,
    RADIO_GET_TIMESTAMP_VALUE = 0x17,
    RADIO_SET_CCA = 0x18,
    RADIO_GET_CCA_RESULT = 0x19,
    LORA_SET_MODULATION = 0x20,
    LORA_SET_PACKET = 0x21,
    LORA_SET_SYNCH_TIMEOUT = 0x22,
    LORA_SET_SYNCWORD = 0x23,
    LORA_SET_SIDE_DET_CONFIG = 0x24,
    LORA_SET_SIDE_DET_SYNCWORD = 0x25,
    LORA_SET_CAD_PARAMS = 0x27,
    LORA_SET_CAD = 0x28,
    LORA_GET_RX_STATS = 0x29,
    LORA_GET_PACKET_STATUS = 0x2A,
};

/* The interrupt word, bit by bit. */
enum : uint32_t {
    IRQ_RX_FIFO = 1u << 0,
    IRQ_TX_FIFO = 1u << 1,
    IRQ_PREAMBLE_DETECTED = 1u << 5,
    IRQ_HEADER_VALID = 1u << 6,
    IRQ_CAD_DETECTED = 1u << 7,
    IRQ_HEADER_ERR = 1u << 9,
    IRQ_PA = 1u << 11,
    IRQ_ERROR = 1u << 16,
    IRQ_CMD = 1u << 17,
    IRQ_RX_DONE = 1u << 18,
    IRQ_TX_DONE = 1u << 19,
    IRQ_CAD_DONE = 1u << 20,
    IRQ_TIMEOUT = 1u << 21,
    IRQ_CRC_ERROR = 1u << 22,
    IRQ_LEN_ERROR = 1u << 23,
};

/* GetErrors: receiving with no front-end calibration since power-up. */
constexpr uint16_t ERR_RXFREQ_NO_FE_CAL = 0x0200;

/* Timestamp sources (SetTimestampSource's low nibble). */
enum { TS_LAST_BIT_SENT = 0x1, TS_LAST_BIT_RECEIVED = 0x2, TS_HEADER_DETECTED = 0x4 };

/* The FIFOs as extend_fifos sets them up on the boards: 1000 bytes in,
 * 1020 out. The driver reads the sizes back and stops if they are not so. */
constexpr size_t kRxFifoBytes = 1000;
constexpr size_t kTxFifoBytes = 1020;

/* A preamble is found a few symbols in, as on the SX1262 (model.cpp). */
constexpr double kPreambleFoundSymbols = 4.0;

static const struct simradio_services* S() { return conductor::modelServices(); }

/* The LoRa bandwidth codes of SetLoraModulationParams, in hertz. */
static uint32_t lrBwFromCode(uint8_t code)
{
    static const uint32_t kBw[16] = {
        7810, 15630, 31250, 62500, 125000, 250000, 500000, 1000000,
        10420, 20830, 41670, 83330, 101560, 203130, 406250, 812500,
    };
    return kBw[code & 0x0F];
}

/* The Wio-LR2021's measured low-frequency PA table (Semtech's
 * tx-power-cfg-lf, the reticulum-lr2021 driver's WIO_LR2021_LF): one row per
 * dBm from -10 to +22, each (half_power, duty, slices) used once, so the triple
 * the driver sends names the power it meant. */
struct PaRow { int8_t halfPower; uint8_t duty, slices; int8_t dbm; };

static const PaRow kPaTable[] = {
    {-8, 3, 0, -10}, {-6, 3, 0, -9}, {0, 1, 0, -8},  {2, 1, 0, -7},  {2, 2, 0, -6},
    {4, 2, 0, -5},   {8, 0, 1, -4},  {6, 1, 2, -3},  {6, 5, 0, -2},  {12, 1, 1, -1},
    {16, 1, 0, 0},   {18, 1, 0, 1},  {18, 2, 0, 2},  {20, 2, 0, 3},  {22, 2, 0, 4},
    {16, 5, 1, 5},   {28, 0, 1, 6},  {28, 0, 2, 7},  {28, 4, 0, 8},  {30, 1, 2, 9},
    {32, 1, 2, 10},  {28, 4, 2, 11}, {30, 4, 2, 12}, {32, 4, 2, 13}, {34, 4, 2, 14},
    {36, 4, 2, 15},  {34, 7, 3, 16}, {38, 4, 4, 17}, {38, 5, 7, 18}, {40, 5, 7, 19},
    {42, 5, 6, 20},  {44, 5, 6, 21}, {44, 7, 7, 22},
};

/* A triple the table does not name: half_power is in half dB, and the table's
 * rows sit 7 to 12 dB under half of it; the nearest row with the same drive
 * stands in, and failing that half of the drive less 7. */
static int lrRadiatedDbm(int8_t halfPower, uint8_t duty, uint8_t slices)
{
    for (const PaRow& r : kPaTable)
        if (r.halfPower == halfPower && r.duty == duty && r.slices == slices) return r.dbm;
    for (const PaRow& r : kPaTable)
        if (r.halfPower == halfPower) return r.dbm;
    int dbm = halfPower / 2 - 7;
    return dbm < -10 ? -10 : dbm > 22 ? 22 : dbm;
}

/* The front end between chip and connector, from SIM_MESH_BOARD: the same
 * reading as the SX1262's model (model.cpp), a flat gain each way here. */
struct LrFrontEnd { int gainDb = 0; int rxGainDb = 0; };

static const LrFrontEnd& lrFrontEnd()
{
    static const LrFrontEnd fe = [] {
        LrFrontEnd f;
        const char* env = getenv("SIM_MESH_BOARD");
        simradio_json::Object board;
        if (env && board.parse(env, strlen(env))) {
            f.gainDb = (int)board.num("fem_gain_db", 0);
            f.rxGainDb = (int)board.num("fem_rx_gain_db", 0);
        }
        return f;
    }();
    return fe;
}

/* BUSY as the board's LR2021 spends it, from SIM_MESH_BOARD, read as the
 * SX1262's model reads it (busy.h): `busy_us` entries qualified `lr2021.`, each
 * command named as the LR2021's datasheet names it, keep BUSY high for their
 * time after the command; and `host_us` after every transaction, reads and
 * FIFO transfers included, where the board SIM_MESH_BOARD describes is an
 * LR2021's (its `chip`): the SX1262 board's per-transaction time is not this
 * board's, and none has been measured on it yet. While BUSY is high after a command that changes the
 * radio's mode or tuning, or calibrates it, the radio is deaf: the state it
 * sends says when it is ready again (`ready_at`), and the ether tells it of
 * nothing that starts before then but energy; a CAD or a CCA listens only once
 * BUSY has fallen. SetTx is not charged: its BUSY would hold the frame back,
 * and the frame goes on the air at the command. Unset, BUSY is never busy.
 *
 * Measured on the reticulum project's LR2021 board: 10.9 ms a switch of
 * channel and spreading factor, 10.5 ms of it BUSY in CalibFE, with a
 * calibration at every configure, as the stock driver makes one
 * (`lr2021.CalibFE:10500`); calibrating per channel, 1.30-1.33 ms a switch
 * (0.94-0.97 ms BUSY), up to 6.1 ms on the first switch to a channel, and
 * some 0.95 ms once each channel has been calibrated once. The rest of a
 * switch has not been split by command. */
struct LrBusy {
    int32_t us[3][256] = {};    /* by group and opcode */
    int32_t host = 0;           /* every transaction's */
};

static const struct { const char* name; uint8_t group, op; } kLrBusyCommands[] = {
    {"SetStandby", G_SYS, SYS_SET_STANDBY},       {"SetSleep", G_SYS, SYS_SET_SLEEP},
    {"SetFs", G_SYS, SYS_SET_FS},                 {"Calibrate", G_SYS, SYS_CALIBRATE},
    {"CalibFE", G_SYS, SYS_CALIB_FE},             {"GetTemp", G_SYS, SYS_GET_TEMP},
    {"SetRegMode", G_SYS, SYS_SET_REG_MODE},      {"ConfigLfClock", G_SYS, SYS_CONFIG_LF_CLOCK},
    {"SetTcxoMode", G_SYS, SYS_SET_TCXO_MODE},    {"SetDioFunction", G_SYS, SYS_SET_DIO_FUNCTION},
    {"SetDioIrqConfig", G_SYS, SYS_SET_DIO_IRQ_CONFIG}, {"ClearIrq", G_SYS, SYS_CLEAR_IRQ},
    {"ClearRxFifo", G_SYS, SYS_CLEAR_RX_FIFO},    {"ClearTxFifo", G_SYS, SYS_CLEAR_TX_FIFO},
    {"WriteRegMem32", G_SYS, SYS_WRITE_REG32},    {"WriteRegMemMask32", G_SYS, SYS_WRITE_REG32_MASK},
    {"SetAdditionalRegToRetain", G_SYS, SYS_SET_RETAIN},
    {"SetRfFrequency", G_RADIO, RADIO_SET_RF_FREQUENCY}, {"SetRxPath", G_RADIO, RADIO_SET_RX_PATH},
    {"SetPaConfig", G_RADIO, RADIO_SET_PA_CONFIG}, {"SetTxParams", G_RADIO, RADIO_SET_TX_PARAMS},
    {"SetRxTxFallbackMode", G_RADIO, RADIO_SET_FALLBACK},
    {"SetPacketType", G_RADIO, RADIO_SET_PACKET_TYPE}, {"SetRx", G_RADIO, RADIO_SET_RX},
    {"SelPa", G_RADIO, RADIO_SEL_PA},             {"SetRxDutyCycle", G_RADIO, RADIO_SET_RX_DUTY_CYCLE},
    {"SetTimestampSource", G_RADIO, RADIO_SET_TIMESTAMP_SOURCE}, {"SetCca", G_RADIO, RADIO_SET_CCA},
    {"SetLoraModulationParams", G_RADIO, LORA_SET_MODULATION},
    {"SetLoraPacketParams", G_RADIO, LORA_SET_PACKET},
    {"SetLoraSynchTimeout", G_RADIO, LORA_SET_SYNCH_TIMEOUT},
    {"SetLoraSyncword", G_RADIO, LORA_SET_SYNCWORD},
    {"SetLoraSideDetConfig", G_RADIO, LORA_SET_SIDE_DET_CONFIG},
    {"SetLoraSideDetSyncword", G_RADIO, LORA_SET_SIDE_DET_SYNCWORD},
    {"SetLoraCadParams", G_RADIO, LORA_SET_CAD_PARAMS}, {"SetLoraCad", G_RADIO, LORA_SET_CAD},
};

static LrBusy loadLrBusy()
{
    LrBusy b;
    const BoardBusy board = readBoardBusy("lr2021");
    b.host = board.hostUs;
    for (const BusyEntry& e : board.entries) {
        bool known = false;
        for (const auto& c : kLrBusyCommands) {
            if (e.command != c.name) continue;
            b.us[c.group][c.op] = e.us;
            known = true;
        }
        if (!known)
            fprintf(stderr, "simradio: SIM_MESH_BOARD busy_us entry \"lr2021.%s:%d\" is not "
                            "an LR2021 command BUSY is charged to; ignored\n",
                    e.command.c_str(), (int)e.us);
    }
    return b;
}

static const LrBusy& lrBusy()
{
    static const LrBusy b = loadLrBusy();
    return b;
}

/* What a frame of `len` bytes beginning `group`, `op` keeps BUSY high for: its
 * command's figure (none for SetTx, see `lrBusy`) and the host's time. */
static int64_t lrBusyFor(uint8_t group, uint8_t op, size_t len)
{
    const LrBusy& b = lrBusy();
    int64_t us = 0;
    if (len >= 2 && group <= G_RADIO && !(group == G_RADIO && op == RADIO_SET_TX))
        us = b.us[group][op];
    return us + b.host;
}

/* A connector level as the chip reads it. */
static int lrChipLevelDbm(int connectorLevel)
{
    const int dbm = connectorLevel + lrFrontEnd().rxGainDb;
    return dbm < -127 ? -127 : dbm > 0 ? 0 : dbm;
}

/* SNR as the packet status reports it, x/4 dB; a LoRa receiver's estimate
 * saturates a little above 10 dB (an LR2021 on a desk read 14 dB at -16 dBm),
 * so no more than +14 here. */
static int8_t lrSnrQuarter(int db)
{
    const int v = 4 * (db > 14 ? 14 : db);
    return (int8_t)(v < -128 ? -128 : v);
}

struct LrAirFrame {
    int id = 0;
    int levelDbm = 0;
    int64_t endUs = 0;
};

constexpr int kLrAirFrames = 16;

struct LrState {
    const char* mode = "STDBY_RC";
    bool rxContinuous = false;
    const char* fallbackMode = "STDBY_RC";

    uint32_t irq = 0;
    uint32_t dioMask = 0;
    uint16_t errors = 0;
    bool calibFe = false;

    std::map<uint32_t, uint32_t> regs;
    std::deque<uint8_t> rxFifo;
    std::vector<uint8_t> txFifo;

    uint32_t freqHz = 869525000;
    uint32_t bwHz = 125000;
    int sf = 7;
    int cr = 5;
    bool ldro = false;
    int preamble = 8;
    bool hdrImplicit = false;
    bool crcOn = true;
    bool iqInverted = false;
    uint8_t payloadLen = 0;
    uint8_t syncWord = 0x12;
    int sideSf[3] = {};
    int sideCount = 0;

    int8_t halfPower = 34;
    uint8_t paDuty = 4, paSlices = 2;
    uint8_t rxBoost = 0;

    int tsSource[4] = {};
    int64_t tsLastRx = -1, tsLastTx = -1, tsHeader = -1;

    uint16_t nReceived = 0, nCrcErr = 0, nHdrErr = 0, nHdrValid = 0, nFalseSync = 0;

    uint8_t pktLen = 0;
    int8_t pktSnrQ = 0;
    int pktRssiHalf = 220, pktSigHalf = 220;   /* half-dB magnitudes */

    int cadSymbols = 2;
    uint8_t cadExit = 0;
    int ccaMaxHalf = 0;                      /* the loudest level, half-dB magnitude */

    LrAirFrame air[kLrAirFrames];
    int lockId = 0;
    bool hdrOk = true;      /* the header's fate, known at rx_begin (VirtualRxBegin.hdrOk) */
    int64_t heardEndUs = 0;

    int txId = 0;

    /* BUSY (`lrBusy`): when the last transaction's falls. */
    int64_t busyUntil = 0;

    /* A two-phase read's reply, latched when the command runs and served by
     * the next frame that begins 00 00. */
    uint8_t reply[16] = {};
    size_t replyLen = 0;
    bool replyValid = false;

    int tempC = 25;
};

struct simradio {
    int slot = 0;
    void (*onPin)(void*, int, int) = nullptr;
    void* ctx = nullptr;

    LrState st;

    void* tTxDone = nullptr;
    int64_t txEnd = 0;
    void* tPre = nullptr;
    void* tHdr = nullptr;
    void* tCad = nullptr;
    void* tCca = nullptr;
    void* tRxTimeout = nullptr;
    void* tBusy = nullptr;
};

namespace {

constexpr int kMaxSlots = 8;
simradio* s_chips[kMaxSlots] = {};

struct PinCall {
    void (*fn)(void*, int, int) = nullptr;
    void* ctx = nullptr;
    int pin = SIMRADIO_PIN_DIO1;
    bool high = false;
    void operator()() const;
    void tell() const { if (fn) fn(ctx, pin, high ? 1 : 0); }
};

std::atomic<bool> s_pinsHeld{false};
std::mutex s_pinsMu;
std::vector<PinCall> s_pinsKept;

void PinCall::operator()() const
{
    if (!fn) return;
    if (s_pinsHeld.load()) {
        std::lock_guard<std::mutex> g(s_pinsMu);
        s_pinsKept.push_back(*this);
        return;
    }
    tell();
}

PinCall irqLineOf(const simradio* c)
{
    PinCall p;
    p.fn = c->onPin;
    p.ctx = c->ctx;
    p.high = (c->st.irq & c->st.dioMask) != 0;
    return p;
}

PinCall busyOf(const simradio* c)
{
    PinCall p;
    p.fn = c->onPin;
    p.ctx = c->ctx;
    p.pin = SIMRADIO_PIN_BUSY;
    p.high = S()->now_us() < c->st.busyUntil;
    return p;
}

double symbolSeconds(const LrState& d)
{
    return (double)((uint32_t)1 << d.sf) / (double)d.bwHz;
}

void fillState(const simradio* c, EtherState& s)
{
    const LrState& d = c->st;
    s.slot = c->slot;
    s.mode = d.mode;
    /* Ready once BUSY has fallen (`lrBusy`). */
    s.readyAt = std::max(S()->now_us(), d.busyUntil);
    s.freqHz = d.freqHz;
    s.bwHz = d.bwHz;
    s.sf = d.sf;
    s.cr = d.cr;
    s.syncWord = d.syncWord;
    s.hdrImplicit = d.hdrImplicit;
    s.crc = d.crcOn;
    s.preamble = d.preamble;
    s.iqInverted = d.iqInverted;
    /* The side detectors beside the main one (ether_link.h's EtherState):
     * `d.sideSf`, deduplicated against `d.sf` and each other, ascending.
     * None: single-SF. Each takes the main detector's own sync word and IQ,
     * since the LR2021's SetLoraSideDetSyncword is not modelled per side yet
     * (model.cpp's SIM_MESH_MULTI_SF sweep makes the same simplification). */
    s.nSide = 0;
    if (d.sideCount > 0) {
        int sfs[4];
        int n = 0;
        sfs[n++] = d.sf;
        for (int i = 0; i < d.sideCount && n < 4; i++) {
            bool seen = false;
            for (int j = 0; j < n; j++) seen |= sfs[j] == d.sideSf[i];
            if (!seen) sfs[n++] = d.sideSf[i];
        }
        std::sort(sfs, sfs + n);
        for (int i = 0; i < n && s.nSide < kMaxSideDetectors; i++) {
            if (sfs[i] == d.sf) continue;
            s.side[s.nSide].sf = sfs[i];
            s.side[s.nSide].syncWord = d.syncWord;
            s.side[s.nSide].iqInverted = d.iqInverted;
            s.nSide++;
        }
    }
}

void armOnce(simradio* c, void** h, void (*cb)(void*), int64_t delayUs)
{
    if (!*h) *h = S()->timer_create(cb, c, "lr2021");
    if (!*h) return;
    S()->timer_start_once(*h, delayUs < 0 ? 0 : delayUs);
}

void stopTimer(void* h)
{
    if (h) S()->timer_stop(h);
}

void dropLock(simradio* c)
{
    c->st.lockId = 0;
    stopTimer(c->tPre);
    stopTimer(c->tHdr);
}

void abandonReception(simradio* c)
{
    for (LrAirFrame& a : c->st.air) a = LrAirFrame();
    dropLock(c);
    stopTimer(c->tRxTimeout);
}

void feelAir(LrState& d, int id, int levelDbm, int64_t endUs, int64_t now)
{
    int room = -1, soonest = 0;
    for (int i = 0; i < kLrAirFrames; i++) {
        LrAirFrame& a = d.air[i];
        if (a.endUs > now && a.id == id) {
            a.levelDbm = levelDbm;
            a.endUs = endUs;
            return;
        }
        if (a.endUs <= now && room < 0) room = i;
        if (a.endUs < d.air[soonest].endUs) soonest = i;
    }
    LrAirFrame& a = d.air[room >= 0 ? room : soonest];
    a.id = id;
    a.levelDbm = levelDbm;
    a.endUs = endUs;
}

int airLevelDbm(const LrState& d, int64_t now)
{
    double mw = 0.0;
    for (const LrAirFrame& a : d.air)
        if (a.endUs > now) mw += std::pow(10.0, a.levelDbm / 10.0);
    return mw > 0.0 ? (int)std::lround(10.0 * std::log10(mw)) : kNoiseFloorDbm;
}

/* A level in the chip's half-dB magnitude below 0 dBm. */
int halfDbMagnitude(int dbm)
{
    int v = -2 * lrChipLevelDbm(dbm);
    return v < 1 ? 1 : v > 0x1FE ? 0x1FE : v;
}

void latch(LrState& d, const uint8_t* bytes, size_t n)
{
    n = n > sizeof(d.reply) ? sizeof(d.reply) : n;
    memset(d.reply, 0, sizeof(d.reply));
    memcpy(d.reply, bytes, n);
    d.replyLen = n;
    d.replyValid = true;
}

void be16(uint8_t* p, uint16_t v) { p[0] = (uint8_t)(v >> 8); p[1] = (uint8_t)v; }
void be32(uint8_t* p, uint32_t v)
{
    p[0] = (uint8_t)(v >> 24); p[1] = (uint8_t)(v >> 16); p[2] = (uint8_t)(v >> 8); p[3] = (uint8_t)v;
}
uint32_t rd24(const uint8_t* p) { return ((uint32_t)p[0] << 16) | ((uint32_t)p[1] << 8) | p[2]; }
uint32_t rd32(const uint8_t* p) { return ((uint32_t)p[0] << 24) | rd24(p + 1); }

void txDoneCb(void* arg);
void rxPreCb(void* arg);
void rxHdrCb(void* arg);
void cadDoneCb(void* arg);
void ccaDoneCb(void* arg);
void rxTimeoutCb(void* arg);
void busyDoneCb(void* arg);

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

void modelHoldPins()
{
    s_pinsHeld.store(true);
}

void modelReleasePins()
{
    std::vector<PinCall> kept;
    {
        std::lock_guard<std::mutex> g(s_pinsMu);
        kept.swap(s_pinsKept);
        s_pinsHeld.store(false);
    }
    for (const PinCall& p : kept) p.tell();
}

static void stopAll(simradio* c)
{
    stopTimer(c->tTxDone);
    stopTimer(c->tPre);
    stopTimer(c->tHdr);
    stopTimer(c->tCad);
    stopTimer(c->tCca);
    stopTimer(c->tRxTimeout);
    stopTimer(c->tBusy);
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
        stopAll(c);
        c->st = LrState();
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
    stopAll(c);
    c->onPin = nullptr;
    c->ctx = nullptr;
    S()->unlock();
}

extern "C" int64_t simradio_now_us(void)
{
    return S()->now_us();
}

extern "C" void simradio_host_floor(int station)
{
    etherPublishFloor(station != 0);
}

extern "C" int64_t simradio_quiet_for_us(simradio_t* c)
{
    if (!c || !conductor::isVirtual()) return -1;
    S()->lock();
    int64_t end = strcmp(c->st.mode, "TX") == 0 ? c->txEnd : -1;
    int64_t now = S()->now_us();
    S()->unlock();
    if (end <= now) return -1;
    int64_t left = conductor::firstNodeAt(end) - conductor::nodeNowUs();
    return left > 0 ? left : -1;
}

extern "C" int simradio_pin(simradio_t* c, int pin)
{
    if (!c) return 0;
    /* BUSY is high while the last transaction's time lasts (`lrBusy`), and
     * with none charged never: every command is answered the moment it
     * arrives. */
    if (pin != SIMRADIO_PIN_DIO1 && pin != SIMRADIO_PIN_BUSY) return 0;
    S()->lock();
    int high = pin == SIMRADIO_PIN_BUSY ? S()->now_us() < c->st.busyUntil
                                        : (c->st.irq & c->st.dioMask) != 0;
    S()->unlock();
    return high;
}

/* RESET: everything back to power-up, the retain table included. */
extern "C" void simradio_reset(simradio_t* c)
{
    if (!c) return;
    S()->lock();
    stopAll(c);
    c->st = LrState();
    PinCall pin = irqLineOf(c);
    EtherState s;
    fillState(c, s);
    S()->unlock();
    etherPublishState(s);
    pin();
}

/* ---- The command interpreter ---- */

extern "C" void simradio_transfer(simradio_t* c, const uint8_t* out, size_t len, uint8_t* in)
{
    if (!c || !out || !in || len == 0) return;

    bool publishState = false;
    bool publishTx = false;
    EtherTxFrame frame = {};
    EtherState snap = {};
    std::vector<uint8_t> txPayload;

    S()->lock();
    LrState& d = c->st;
    memset(in, 0, len);

    /* Any frame wakes a sleeping chip (the driver's NSS pulse). */
    if (strcmp(d.mode, "SLEEP") == 0) {
        d.mode = "STDBY_RC";
        publishState = true;
    }

    const uint8_t group = out[0];
    const uint8_t op = len >= 2 ? out[1] : 0xFF;
    const int64_t now = S()->now_us();
    /* BUSY after this transaction (`lrBusy`); a CAD or a CCA listens once it falls. */
    const int64_t busyUs = lrBusyFor(group, op, len);
    uint8_t r[16] = {};          /* a read's reply: stat1, stat2, data */

    if (group == G_FIFO && op == 0x00 && d.replyValid) {
        /* The second phase of a read: what the command latched. */
        for (size_t i = 0; i < len && i < d.replyLen; i++) in[i] = d.reply[i];
        d.replyValid = false;
    } else if (group == G_FIFO && op == FIFO_READ_RX) {
        for (size_t i = 2; i < len; i++) {
            if (d.rxFifo.empty()) break;
            in[i] = d.rxFifo.front();
            d.rxFifo.pop_front();
        }
    } else if (group == G_FIFO && op == FIFO_WRITE_TX) {
        for (size_t i = 2; i < len && d.txFifo.size() < kTxFifoBytes; i++) d.txFifo.push_back(out[i]);
    } else if (group == G_SYS) {
        switch (op) {
        case SYS_GET_STATUS:
            latch(d, r, 6);
            break;
        case SYS_GET_VERSION:
            r[2] = 0x01;
            r[3] = 0x18;
            latch(d, r, 4);
            break;
        case SYS_WRITE_REG32:
            if (len >= 9) d.regs[rd24(out + 2)] = rd32(out + 5);
            break;
        case SYS_WRITE_REG32_MASK:
            if (len >= 13) {
                uint32_t a = rd24(out + 2), m = rd32(out + 5), v = rd32(out + 9);
                d.regs[a] = (d.regs[a] & ~m) | (v & m);
            }
            break;
        case SYS_READ_REG32:
            if (len >= 5) {
                auto it = d.regs.find(rd24(out + 2));
                be32(r + 2, it == d.regs.end() ? 0 : it->second);
            }
            latch(d, r, 6);
            break;
        case SYS_GET_ERRORS:
            be16(r + 2, d.errors);
            latch(d, r, 4);
            break;
        case SYS_CLEAR_ERRORS:
            d.errors = 0;
            break;
        case SYS_SET_DIO_IRQ_CONFIG:
            if (len >= 7) d.dioMask = rd32(out + 3);
            break;
        case SYS_CLEAR_IRQ:
            if (len >= 6) d.irq &= ~rd32(out + 2);
            break;
        case SYS_GET_AND_CLEAR_IRQ:
            be32(r + 2, d.irq);
            latch(d, r, 6);
            d.irq = 0;
            break;
        case SYS_GET_RX_FIFO_LEVEL:
            be16(r + 2, (uint16_t)d.rxFifo.size());
            latch(d, r, 4);
            break;
        case SYS_GET_TX_FIFO_LEVEL:
            be16(r + 2, (uint16_t)d.txFifo.size());
            latch(d, r, 4);
            break;
        case SYS_CLEAR_RX_FIFO:
            d.rxFifo.clear();
            break;
        case SYS_CLEAR_TX_FIFO:
            d.txFifo.clear();
            break;
        case SYS_CALIBRATE:
            d.mode = "STDBY_RC";
            publishState = true;
            break;
        case SYS_CALIB_FE:
            d.calibFe = true;
            break;
        case SYS_GET_TEMP: {
            /* Zero while receiving, as the part does; otherwise a steady 25 °C.
             * Format 1 (bit 3 of the parameter): degrees in 32nds, 13 bits at
             * the top of the word; format 0: a raw mid-scale reading. */
            int celsius = strcmp(d.mode, "RX") == 0 ? 0 : d.tempC;
            uint16_t word = (len >= 3 && (out[2] & 0x08)) ? (uint16_t)(((celsius * 32) & 0x1FFF) << 3)
                            : strcmp(d.mode, "RX") == 0 ? 0 : 0x1000;
            be16(r + 2, word);
            latch(d, r, 4);
            break;
        }
        case SYS_SET_SLEEP:
            d.mode = "SLEEP";
            publishState = true;
            break;
        case SYS_SET_STANDBY:
            d.mode = (len >= 3 && out[2] == 0x01) ? "STDBY_XOSC" : "STDBY_RC";
            publishState = true;
            break;
        case SYS_SET_FS:
            d.mode = "FS";
            publishState = true;
            break;
        case SYS_SET_DIO_FUNCTION:
        case SYS_CONFIG_LF_CLOCK:
        case SYS_SET_TCXO_MODE:
        case SYS_SET_REG_MODE:
        case SYS_SET_RETAIN:
            break;
        default:
            break;
        }
    } else if (group == G_RADIO) {
        switch (op) {
        case RADIO_SET_RF_FREQUENCY:
            if (len >= 6) {
                d.freqHz = rd32(out + 2);
                publishState = true;
            }
            break;
        case RADIO_SET_RX_PATH:
            /* Only from standby; ignored while receiving (the part does so). */
            if (len >= 4 && strncmp(d.mode, "STDBY", 5) == 0) d.rxBoost = out[3];
            break;
        case RADIO_SET_PA_CONFIG:
            if (len >= 4) {
                d.paDuty = (uint8_t)(out[3] >> 4);
                d.paSlices = (uint8_t)(out[3] & 0x0F);
            }
            break;
        case RADIO_SET_TX_PARAMS:
            if (len >= 3) d.halfPower = (int8_t)out[2];
            break;
        case RADIO_SET_FALLBACK:
            if (len >= 3) {
                switch (out[2]) {
                case 0x02: d.fallbackMode = "STDBY_XOSC"; break;
                case 0x03: d.fallbackMode = "FS"; break;
                default: d.fallbackMode = "STDBY_RC"; break;
                }
            }
            break;
        case RADIO_SET_PACKET_TYPE:
        case RADIO_SEL_PA:
            break;
        case RADIO_GET_RSSI_INST: {
            /* The power in the air summed, in half-dB below 0 dBm; railed (0)
             * outside receive, which the driver refuses as no reading. */
            int raw = strcmp(d.mode, "RX") == 0 ? halfDbMagnitude(airLevelDbm(d, now)) : 0;
            r[2] = (uint8_t)(raw >> 1);
            r[3] = (uint8_t)(raw & 1);
            latch(d, r, 4);
            break;
        }
        case RADIO_SET_RX: {
            /* No parameter: a single receive with no timeout. FFFFFF:
             * continuous. Anything else: a single receive that times out
             * after that many 1/32768 s with nothing found. */
            uint32_t t = len >= 5 ? rd24(out + 2) : 0;
            d.mode = "RX";
            d.rxContinuous = len >= 5 && t == 0xFFFFFF;
            if (!d.calibFe) d.errors |= ERR_RXFREQ_NO_FE_CAL;
            dropLock(c);
            stopTimer(c->tRxTimeout);
            if (len >= 5 && t != 0 && t != 0xFFFFFF)
                armOnce(c, &c->tRxTimeout, rxTimeoutCb, (int64_t)((double)t * 1e6 / 32768.0));
            publishState = true;
            break;
        }
        case RADIO_SET_TX: {
            d.mode = "TX";
            size_t n = d.txFifo.size();
            if (d.payloadLen && n > d.payloadLen) n = d.payloadLen;
            txPayload.assign(d.txFifo.begin(), d.txFifo.begin() + (long)n);
            d.txFifo.clear();
            double toa = loraToaSeconds(d.sf, (int)d.bwHz, d.cr, d.preamble, (int)n,
                                        d.hdrImplicit, d.crcOn);
            double tSym = symbolSeconds(d);
            fillState(c, frame.state);
            frame.id = ++d.txId;
            frame.t0 = now;
            frame.tPre = now + (int64_t)((d.preamble + 4.25) * tSym * 1e6);
            frame.tHdr = frame.tPre + (int64_t)(8.0 * tSym * 1e6);
            frame.tEnd = now + (int64_t)(toa * 1e6);
            frame.powerDbm = lrRadiatedDbm(d.halfPower, d.paDuty, d.paSlices) + lrFrontEnd().gainDb;
            frame.payload = txPayload.data();
            frame.len = txPayload.size();
            publishTx = true;
            c->txEnd = frame.tEnd;
            armOnce(c, &c->tTxDone, txDoneCb, frame.tEnd - now);
            break;
        }
        case RADIO_SET_RX_DUTY_CYCLE:
            /* Not modelled beyond a plain continuous receive. */
            d.mode = "RX";
            d.rxContinuous = true;
            if (!d.calibFe) d.errors |= ERR_RXFREQ_NO_FE_CAL;
            dropLock(c);
            publishState = true;
            break;
        case RADIO_SET_TIMESTAMP_SOURCE:
            if (len >= 3) d.tsSource[(out[2] >> 4) & 0x3] = out[2] & 0x0F;
            break;
        case RADIO_GET_TIMESTAMP_VALUE: {
            /* 32 MHz ticks from the event to this command. */
            int slot = len >= 3 ? (out[2] & 0x3) : 0;
            int64_t at = -1;
            switch (d.tsSource[slot]) {
            case TS_LAST_BIT_RECEIVED: at = d.tsLastRx; break;
            case TS_LAST_BIT_SENT: at = d.tsLastTx; break;
            case TS_HEADER_DETECTED: at = d.tsHeader; break;
            default: break;
            }
            uint64_t ticks = at < 0 ? 0xFFFFFFFFu : (uint64_t)(now - at) * 32u;
            be32(r + 2, (uint32_t)(ticks > 0xFFFFFFFFu ? 0xFFFFFFFFu : ticks));
            latch(d, r, 6);
            break;
        }
        case RADIO_SET_CCA: {
            /* Listen for that many 32 MHz ticks, keep the loudest level, end
             * with TIMEOUT and fall back. The medium delivers energy to a
             * listening chip as to a CAD. */
            uint32_t ticks = len >= 5 ? rd24(out + 2) : 32000;
            d.mode = "CAD";
            d.ccaMaxHalf = halfDbMagnitude(airLevelDbm(d, now));
            dropLock(c);
            armOnce(c, &c->tCca, ccaDoneCb, busyUs + (int64_t)(ticks / 32u));
            publishState = true;
            break;
        }
        case RADIO_GET_CCA_RESULT:
            r[3] = (uint8_t)(d.ccaMaxHalf >> 1);
            r[5] = (uint8_t)((d.ccaMaxHalf & 1) << 1);
            latch(d, r, 6);
            break;
        case LORA_SET_MODULATION:
            if (len >= 4) {
                d.sf = out[2] >> 4;
                d.bwHz = lrBwFromCode(out[2] & 0x0F);
                int code = out[3] >> 4;
                d.cr = code < 1 ? 5 : code > 4 ? 8 : code + 4;
                d.ldro = (out[3] & 0x01) != 0;
                d.sideCount = 0;           /* the side detectors go off */
                publishState = true;
            }
            break;
        case LORA_SET_PACKET:
            if (len >= 6) {
                d.preamble = ((int)out[2] << 8) | out[3];
                d.payloadLen = out[4];
                d.hdrImplicit = (out[5] & 0x04) != 0;
                d.crcOn = (out[5] & 0x02) != 0;
                d.iqInverted = (out[5] & 0x01) != 0;
                publishState = true;
            }
            break;
        case LORA_SET_SYNCWORD:
            /* Taken at once, in receive too. */
            if (len >= 3) {
                d.syncWord = out[2];
                publishState = true;
            }
            break;
        case LORA_SET_SIDE_DET_CONFIG:
            d.sideCount = 0;
            for (size_t i = 2; i < len && d.sideCount < 3; i++) d.sideSf[d.sideCount++] = out[i] >> 4;
            publishState = true;
            break;
        case LORA_SET_SYNCH_TIMEOUT:
        case LORA_SET_SIDE_DET_SYNCWORD:
            break;
        case LORA_SET_CAD_PARAMS:
            if (len >= 5) {
                d.cadSymbols = out[2] ? out[2] : 1;
                d.cadExit = out[4];
            }
            break;
        case LORA_SET_CAD:
            d.mode = "CAD";
            dropLock(c);
            armOnce(c, &c->tCad, cadDoneCb,
                    busyUs + (int64_t)(d.cadSymbols * symbolSeconds(d) * 1e6));
            publishState = true;
            break;
        case LORA_GET_RX_STATS:
            be16(r + 2, d.nReceived);
            be16(r + 4, d.nCrcErr);
            be16(r + 6, d.nHdrErr);
            be16(r + 8, d.nHdrValid);
            be16(r + 10, d.nFalseSync);
            latch(d, r, 12);
            break;
        case LORA_GET_PACKET_STATUS:
            r[2] = (uint8_t)(((d.crcOn ? 1 : 0) << 4) | (d.cr - 4));
            r[3] = d.pktLen;
            r[4] = (uint8_t)d.pktSnrQ;
            r[5] = (uint8_t)(d.pktRssiHalf >> 1);
            r[6] = (uint8_t)(d.pktSigHalf >> 1);
            r[7] = (uint8_t)((1u << 2) | ((d.pktRssiHalf & 1) << 1) | (d.pktSigHalf & 1));
            latch(d, r, 8);
            break;
        default:
            break;
        }
    }

    const bool leftRx = strcmp(d.mode, "RX") != 0;
    /* Standby, sleep and FS abort whatever the chip was doing, silently. */
    if (group == G_SYS && (op == SYS_SET_STANDBY || op == SYS_SET_SLEEP || op == SYS_SET_FS ||
                           op == SYS_CALIBRATE)) {
        abandonReception(c);
        stopTimer(c->tTxDone);
    }
    if (group == G_RADIO && op == RADIO_SET_TX)
        abandonReception(c);
    if (leftRx)
        stopTimer(c->tRxTimeout);
    /* Any new mode ends a CAD or a CCA in progress without an answer. */
    if ((group == G_SYS && (op == SYS_SET_STANDBY || op == SYS_SET_SLEEP || op == SYS_SET_FS)) ||
        (group == G_RADIO && (op == RADIO_SET_TX || op == RADIO_SET_RX))) {
        stopTimer(c->tCad);
        stopTimer(c->tCca);
    }

    /* BUSY for the transaction's time. After a change of mode or tuning, or a
     * calibration, the radio is deaf until it falls, which the state tells
     * the ether. */
    PinCall busyPin;
    if (busyUs > 0) {
        d.busyUntil = now + busyUs;
        publishState = publishState ||
                       (group == G_SYS && (op == SYS_CALIBRATE || op == SYS_CALIB_FE));
        armOnce(c, &c->tBusy, busyDoneCb, busyUs);
        busyPin = busyOf(c);
    }

    if (publishState) fillState(c, snap);
    PinCall pin = irqLineOf(c);
    S()->unlock();

    if (publishTx) etherPublishTx(frame);
    if (publishState) etherPublishState(snap);
    busyPin();
    pin();
}

/* ---- Timer callbacks ---- */

namespace {

void raise(simradio* c, uint32_t bits)
{
    S()->lock();
    c->st.irq |= bits;
    PinCall pin = irqLineOf(c);
    S()->unlock();
    pin();
}

void fallBack(simradio* c)
{
    EtherState s;
    S()->lock();
    c->st.mode = c->st.fallbackMode;
    fillState(c, s);
    S()->unlock();
    etherPublishState(s);
}

void txDoneCb(void* arg)
{
    auto* c = (simradio*)arg;
    S()->lock();
    if (strcmp(c->st.mode, "TX") != 0) { S()->unlock(); return; }   /* aborted by a command */
    c->st.tsLastTx = S()->now_us();
    S()->unlock();
    fallBack(c);
    raise(c, IRQ_TX_DONE);
}

void rxPreCb(void* arg)
{
    raise((simradio*)arg, IRQ_PREAMBLE_DETECTED);
}

void rxHdrCb(void* arg)
{
    auto* c = (simradio*)arg;
    S()->lock();
    bool ok = c->st.hdrOk;
    if (ok) {
        c->st.nHdrValid++;
        c->st.tsHeader = S()->now_us();
    } else {
        /* A header error ends this lock right here, same as the SX1262
         * model's rxHdrCb (model.cpp): the chip never reaches RxDone for
         * this frame and goes back to hunting the next preamble. Without
         * this, HeaderErr is computed but HeaderValid has already latched
         * and the frame runs on to RxDone/CrcErr instead. */
        dropLock(c);
    }
    S()->unlock();
    raise(c, ok ? IRQ_HEADER_VALID : IRQ_HEADER_ERR);
}

void cadDoneCb(void* arg)
{
    auto* c = (simradio*)arg;
    S()->lock();
    if (strcmp(c->st.mode, "CAD") != 0) { S()->unlock(); return; }
    bool detected = S()->now_us() < c->st.heardEndUs;
    S()->unlock();
    fallBack(c);
    raise(c, IRQ_CAD_DONE | (detected ? (uint32_t)IRQ_CAD_DETECTED : 0u));
}

void ccaDoneCb(void* arg)
{
    auto* c = (simradio*)arg;
    S()->lock();
    if (strcmp(c->st.mode, "CAD") != 0) { S()->unlock(); return; }
    int now = halfDbMagnitude(airLevelDbm(c->st, S()->now_us()));
    /* Smaller magnitude is louder. */
    if (now < c->st.ccaMaxHalf) c->st.ccaMaxHalf = now;
    S()->unlock();
    fallBack(c);
    raise(c, IRQ_TIMEOUT);
}

void rxTimeoutCb(void* arg)
{
    auto* c = (simradio*)arg;
    S()->lock();
    bool stillWaiting = strcmp(c->st.mode, "RX") == 0 && c->st.lockId == 0;
    S()->unlock();
    if (!stillWaiting) return;
    fallBack(c);
    raise(c, IRQ_TIMEOUT);
}

/* BUSY falls when the last transaction's time is up. */
void busyDoneCb(void* arg)
{
    auto* c = (simradio*)arg;
    S()->lock();
    PinCall pin = busyOf(c);
    S()->unlock();
    pin();
}

}  // namespace

/* ---- What the ether hands back ---- */

void modelRxBegin(simradio* c, const VirtualRxBegin& f)
{
    int64_t now = S()->now_us();
    S()->lock();
    LrState& d = c->st;
    bool rx = strcmp(d.mode, "RX") == 0;
    bool listening = strcmp(d.mode, "CAD") == 0;     /* a CAD or a CCA */
    if (!rx && !listening) { S()->unlock(); return; }

    int64_t endUs = now + (f.tEnd - f.t0);
    feelAir(d, f.id, f.levelDbm, endUs, now);
    if (endUs > d.heardEndUs) d.heardEndUs = endUs;
    if (listening) {
        int level = halfDbMagnitude(airLevelDbm(d, now));
        if (level < d.ccaMaxHalf) d.ccaMaxHalf = level;
    }
    if (listening || f.energyOnly) { S()->unlock(); return; }

    d.lockId = f.id;
    d.hdrOk = f.hdrOk;
    stopTimer(c->tRxTimeout);   /* something was found */
    int64_t syncUs = f.tPre - f.t0;
    int64_t foundUs = (int64_t)(kPreambleFoundSymbols * symbolSeconds(d) * 1e6);
    armOnce(c, &c->tPre, rxPreCb, foundUs < syncUs ? foundUs : syncUs);
    armOnce(c, &c->tHdr, rxHdrCb, f.tHdr - f.t0);
    S()->unlock();
}

void modelRxEnd(simradio* c, const VirtualRxEnd& f)
{
    uint32_t bits = 0;
    bool single = false;
    S()->lock();
    LrState& d = c->st;
    if (strcmp(d.mode, "RX") != 0 || f.id != d.lockId) { S()->unlock(); return; }
    d.lockId = 0;
    stopTimer(c->tPre);
    stopTimer(c->tHdr);
    if (!d.hdrOk || !f.hdrOk) {
        /* A header error stores nothing; the receiver goes on. `d.hdrOk`
         * is rx_begin's early word (already acted on by rxHdrCb, if its
         * timer got there first); `f.hdrOk` is this rx_end's own, for an
         * ether that only names the verdict this late. Either way, same
         * outcome: nothing kept, no RxDone/CrcErr raised for this frame. */
        d.nHdrErr++;
        bits = IRQ_HEADER_ERR;
    } else {
        /* Appended at the last bit, CRC failures too: two frames can wait. */
        size_t n = f.len;
        if (f.payload && n && d.rxFifo.size() + n <= kRxFifoBytes)
            d.rxFifo.insert(d.rxFifo.end(), f.payload, f.payload + n);
        d.nReceived++;
        d.pktLen = (uint8_t)(n > 255 ? 255 : n);
        d.pktSnrQ = lrSnrQuarter(f.snrDb);
        d.pktRssiHalf = halfDbMagnitude(f.rssiDbm);
        d.pktSigHalf = d.pktRssiHalf;
        d.tsLastRx = S()->now_us();
        bits = IRQ_RX_DONE;
        if (!f.crcOk) {
            d.nCrcErr++;
            bits |= IRQ_CRC_ERROR;
        }
    }
    single = !d.rxContinuous;
    S()->unlock();
    /* A single receive falls back after its frame; continuous stays. */
    if (single && (bits & IRQ_RX_DONE)) fallBack(c);
    raise(c, bits);
}
