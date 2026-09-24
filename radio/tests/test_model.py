"""The chip model through its C ABI, against a fake ether. No firmware.

The library is loaded with ctypes and driven the way a driver drives a chip:
each call to `cmd` is one SPI frame, built the way a driver builds it (a write
is the opcode and its parameters; a read sends the NOP bytes itself and takes
the data from where the datasheet puts it). The fake ether is a UDP socket
this file binds; the library is pointed at it once, reports what it does on
it, and is handed frames through it exactly as the real ether hands them.

Each test is named for the rule it holds.
"""

import base64
import ctypes
import json
import math
import os
import queue
import shutil
import socket
import subprocess
import threading
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
RADIO = os.path.dirname(HERE)
BUILD = os.path.join(RADIO, "build")
LIBRARY = os.path.join(BUILD, "libsimradio.so")

SID = 7
PIN_DIO1, PIN_BUSY = 1, 2

# Opcodes, IRQ bits and status values, as the datasheet numbers them.
SET_STANDBY, SET_RX, SET_TX, SET_RF_FREQUENCY = 0x80, 0x82, 0x83, 0x86
SET_CAD_PARAMS, SET_PACKET_TYPE, SET_MODULATION = 0x88, 0x8A, 0x8B
SET_PACKET_PARAMS, SET_RXTX_FALLBACK, SET_CAD = 0x8C, 0x93, 0xC5
SET_DIO_IRQ_PARAMS, CLEAR_IRQ, WRITE_REGISTER, WRITE_BUFFER = 0x08, 0x02, 0x0D, 0x0E
GET_IRQ, GET_RX_BUF_STATUS, GET_PACKET_STATUS, GET_RSSI_INST = 0x12, 0x13, 0x14, 0x15
READ_REGISTER, READ_BUFFER, GET_STATUS = 0x1D, 0x1E, 0xC0

TX_DONE, RX_DONE, PREAMBLE, SYNC, HEADER_VALID = 0x01, 0x02, 0x04, 0x08, 0x10
HEADER_ERR, CRC_ERR, CAD_DONE, CAD_DETECTED = 0x20, 0x40, 0x80, 0x100
ALL_IRQ = 0x03FF

ST_STDBY_RC, ST_FS, ST_RX = 0x20, 0x40, 0x50
DATA_AVAIL = 0x04

# The carrier the tests use: SF8, BW125, CR 4/5, an 18-symbol preamble.
FREQ, SF, BW, CR, PRE = 869_525_000, 8, 125_000, 5, 18
TSYM = (1 << SF) / BW


def toa_seconds(payload, sf=SF, bw=BW, cr=CR, pre=PRE, implicit=False, crc=True):
    """AN1200.13, the same arithmetic the model times a frame with."""
    tsym = (1 << sf) / bw
    de = 1 if tsym > 0.016 else 0
    num = 8 * payload - 4 * sf + 28 + (16 if crc else 0) - (20 if implicit else 0)
    payload_sym = 8 + max(math.ceil(num / (4 * (sf - 2 * de))) * cr, 0)
    return (pre + 4.25) * tsym + payload_sym * tsym


# ---------------------------------------------------------------------------
# The library and the fake ether
# ---------------------------------------------------------------------------

PIN_CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_int, ctypes.c_int)


def load_library():
    """Build the library if it is not there, then load it."""
    if not os.path.exists(LIBRARY):
        cmake = shutil.which("cmake")
        if cmake is None:
            pytest.fail("no cmake on PATH to build %s" % LIBRARY)
        subprocess.run([cmake, "-B", BUILD, "-S", RADIO], check=True,
                       stdout=subprocess.DEVNULL)
        subprocess.run([cmake, "--build", BUILD], check=True, stdout=subprocess.DEVNULL)
    lib = ctypes.CDLL(LIBRARY)
    lib.simradio_station_open.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_char_p]
    lib.simradio_station_open.restype = ctypes.c_int
    lib.simradio_open.argtypes = [ctypes.c_int, PIN_CB, ctypes.c_void_p]
    lib.simradio_open.restype = ctypes.c_void_p
    lib.simradio_transfer.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t,
                                      ctypes.c_char_p]
    lib.simradio_transfer.restype = None
    lib.simradio_reset.argtypes = [ctypes.c_void_p]
    lib.simradio_pin.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.simradio_pin.restype = ctypes.c_int
    lib.simradio_now_us.restype = ctypes.c_int64
    lib.simradio_close.argtypes = [ctypes.c_void_p]
    return lib


class FakeEther:
    """A UDP socket in the ether's place: it collects what the station says
    and speaks `rx_begin` and `rx_end` to it."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.station = None
        self.inbox = queue.Queue()
        threading.Thread(target=self.drain, daemon=True).start()

    def drain(self):
        while True:
            data, addr = self.sock.recvfrom(65535)
            msg = json.loads(data.decode())
            if msg.get("type") == "hello":
                self.station = addr
            self.inbox.put((time.monotonic(), msg))

    def clear(self):
        while not self.inbox.empty():
            self.inbox.get_nowait()

    def expect(self, kind, timeout=2.0, **match):
        """The next message of this type whose fields match, with its arrival."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                at, msg = self.inbox.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                break
            if msg.get("type") == kind and all(msg.get(k) == v for k, v in match.items()):
                return at, msg
        raise AssertionError("the station never sent %s %r" % (kind, match))

    def send(self, msg):
        assert self.station is not None, "the station never said hello"
        self.sock.sendto(json.dumps(msg).encode(), self.station)

    def rx_begin(self, eid, level, pre_us, hdr_us, end_us, t0=5_000_000, **extra):
        self.send(dict({"type": "rx_begin", "slot": 0, "id": eid, "t0": t0,
                        "t_pre": t0 + pre_us, "t_hdr": t0 + hdr_us,
                        "t_end": t0 + end_us, "level": level}, **extra))

    def rx_end(self, eid, payload, verdict="clean", rssi=-80, snr=7):
        self.send({"type": "rx_end", "slot": 0, "id": eid, "t": 0, "verdict": verdict,
                   "payload": base64.b64encode(payload).decode(),
                   "rssi": rssi, "snr": snr})


@pytest.fixture(scope="module")
def station():
    lib = load_library()
    ether = FakeEther()
    assert lib.simradio_station_open(SID, b"127.0.0.1",
                                     ("127.0.0.1:%d" % ether.port).encode()) == 0
    _, hello = ether.expect("hello")
    assert hello["sid"] == SID
    # Idempotent: a second open neither fails nor says hello again.
    assert lib.simradio_station_open(SID, b"127.0.0.1", b"127.0.0.1:1") == 0
    return lib, ether


class Chip:
    """Slot 0, opened fresh, with every DIO1 edge recorded."""

    def __init__(self, lib, ether):
        self.lib = lib
        self.ether = ether
        self.edges = []                         # (monotonic, level)
        self.callback = PIN_CB(self.on_pin)     # held, or ctypes frees it
        self.handle = lib.simradio_open(0, self.callback, None)
        assert self.handle

    def on_pin(self, _ctx, pin, level):
        if pin == PIN_DIO1:
            self.edges.append((time.monotonic(), level))

    def close(self):
        self.lib.simradio_close(self.handle)

    # ---- one SPI frame each ------------------------------------------------

    def frame(self, out):
        out = bytes(out)
        reply = ctypes.create_string_buffer(len(out))
        self.lib.simradio_transfer(self.handle, out, len(out), reply)
        return reply.raw

    def write(self, op, *params):
        """A driver's write_cmd: the opcode, then its parameters."""
        return self.frame([op, *params])

    def read(self, op, n):
        """A driver's read_cmd: the opcode and its own NOP, then n bytes."""
        return self.frame([op, 0x00] + [0] * n)[2:]

    def read_register(self, addr, n):
        return self.frame([READ_REGISTER, addr >> 8, addr & 0xFF, 0x00] + [0] * n)[4:]

    def read_buffer(self, offset, n):
        return self.frame([READ_BUFFER, offset, 0x00] + [0] * n)[3:]

    def irq(self):
        hi, lo = self.read(GET_IRQ, 2)
        return (hi << 8) | lo

    def clear_irq(self, bits=ALL_IRQ):
        self.write(CLEAR_IRQ, bits >> 8, bits & 0xFF)

    def status(self):
        return self.frame([GET_STATUS, 0x00])[1]

    def dio1(self):
        return self.lib.simradio_pin(self.handle, PIN_DIO1)

    # ---- the radio as a driver sets it up -----------------------------------

    def configure(self, length=42, dio1=ALL_IRQ):
        frf = (FREQ << 25) // 32_000_000
        self.write(SET_PACKET_TYPE, 0x01)
        self.write(SET_RF_FREQUENCY, *frf.to_bytes(4, "big"))
        self.write(SET_MODULATION, SF, 0x04, CR - 4, 0x00)
        self.set_length(length)
        self.write(SET_DIO_IRQ_PARAMS, ALL_IRQ >> 8, ALL_IRQ & 0xFF,
                   dio1 >> 8, dio1 & 0xFF, 0, 0, 0, 0)

    def set_length(self, length):
        self.write(SET_PACKET_PARAMS, PRE >> 8, PRE & 0xFF, 0x00, length, 0x01, 0x00)

    def wait_irq(self, bits, timeout=2.0):
        """Poll GetIrqStatus, as a driver that never uses DIO1 does."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.irq() & bits:
                return time.monotonic()
            time.sleep(0.0005)
        raise AssertionError("IRQ 0x%03x never raised (have 0x%03x)" % (bits, self.irq()))

    def cad_params(self, symbols_code=0x01, exit_mode=0x00):
        self.write(SET_CAD_PARAMS, symbols_code, SF + 13, 10, exit_mode, 0, 0, 0)


@pytest.fixture
def chip(station):
    lib, ether = station
    ether.clear()
    c = Chip(lib, ether)
    try:
        yield c
    finally:
        c.write(SET_STANDBY, 0x00)
        c.close()
        time.sleep(0.05)
        ether.clear()


def settle(seconds=0.05):
    time.sleep(seconds)


# ---------------------------------------------------------------------------
# 1. The reply
# ---------------------------------------------------------------------------

def test_reply_is_the_status_byte_until_the_data_starts(chip):
    idle = ST_STDBY_RC | DATA_AVAIL
    reply = chip.frame([GET_IRQ, 0x00, 0x00, 0x00])
    assert reply[:2] == bytes([idle, idle])

    reply = chip.frame([READ_REGISTER, 0x03, 0x20, 0x00] + [0] * 16)
    assert reply[:4] == bytes([idle] * 4)
    assert reply[4:] == b"SX1261 V2D 2D02\x00"

    reply = chip.frame([WRITE_BUFFER, 0x00, 1, 2, 3])
    assert reply == bytes([idle] * 5)

    chip.write(SET_RX, 0xFF, 0xFF, 0xFF)
    assert chip.frame([GET_STATUS, 0x00]) == bytes([ST_RX | DATA_AVAIL] * 2)


# ---------------------------------------------------------------------------
# 2-3. Transmitting
# ---------------------------------------------------------------------------

def test_set_tx_publishes_a_frame_timed_by_the_toa_formula(chip):
    payload = bytes(range(42))
    chip.configure(length=42)
    chip.frame([WRITE_BUFFER, 0x00, *payload])
    chip.ether.clear()
    chip.write(SET_TX, 0x00, 0x00, 0x00)
    _, tx = chip.ether.expect("tx")

    assert tx["sid"] == SID
    assert (tx["freq"], tx["bw"], tx["sf"], tx["cr"], tx["pre"]) == (
        pytest.approx(FREQ, abs=40), BW, SF, CR, PRE)
    assert tx["t_pre"] - tx["t0"] == int((PRE + 4.25) * TSYM * 1e6)
    assert tx["t_hdr"] - tx["t_pre"] == int(8 * TSYM * 1e6)
    assert tx["t_end"] - tx["t0"] == int(toa_seconds(42) * 1e6)
    assert base64.b64decode(tx["payload"]) == payload


def test_tx_done_lands_at_the_end_and_the_chip_falls_back(chip):
    chip.configure(length=10, dio1=TX_DONE)
    chip.write(SET_RXTX_FALLBACK, 0x40)          # FS after a transmission
    chip.ether.clear()
    sent = time.monotonic()
    chip.write(SET_TX, 0x00, 0x00, 0x00)
    assert chip.irq() & TX_DONE == 0

    deadline = time.monotonic() + 2
    while chip.dio1() == 0 and time.monotonic() < deadline:
        time.sleep(0.0005)
    took = time.monotonic() - sent
    assert took == pytest.approx(toa_seconds(10), abs=0.010)
    assert chip.irq() == TX_DONE
    assert chip.edges and chip.edges[-1][1] == 1
    _, state = chip.ether.expect("state", mode="FS")
    assert chip.status() == ST_FS | DATA_AVAIL


# ---------------------------------------------------------------------------
# 4-8. Receiving
# ---------------------------------------------------------------------------

def test_rx_begin_raises_preamble_and_header_then_rx_end_delivers(chip):
    chip.configure()
    chip.write(SET_RX, 0xFF, 0xFF, 0xFF)
    settle()
    begun = time.monotonic()
    chip.ether.rx_begin(101, -80, pre_us=60_000, hdr_us=100_000, end_us=250_000)

    at = chip.wait_irq(PREAMBLE)
    assert at - begun == pytest.approx(0.060, abs=0.010)
    assert chip.irq() & (PREAMBLE | SYNC) == PREAMBLE | SYNC
    assert chip.irq() & HEADER_VALID == 0
    at = chip.wait_irq(HEADER_VALID)
    assert at - begun == pytest.approx(0.100, abs=0.010)

    payload = b"a frame out of the air"
    chip.ether.rx_end(101, payload, rssi=-80, snr=7)
    chip.wait_irq(RX_DONE)
    assert chip.irq() & (CRC_ERR | HEADER_ERR) == 0
    length, start = chip.read(GET_RX_BUF_STATUS, 2)
    assert length == len(payload)
    assert chip.read_buffer(start, length) == payload
    rssi, snr, signal = chip.read(GET_PACKET_STATUS, 3)
    assert rssi == 160 and signal == 160          # -2 x -80
    assert snr == 28                              # 4 x 7


def test_rx_end_with_a_crc_verdict_raises_crc_err(chip):
    chip.configure()
    chip.write(SET_RX, 0xFF, 0xFF, 0xFF)
    settle()
    chip.ether.rx_begin(102, -80, 10_000, 20_000, 50_000)
    chip.wait_irq(HEADER_VALID)
    chip.ether.rx_end(102, b"spoiled", verdict="crc")
    chip.wait_irq(RX_DONE)
    assert chip.irq() & (RX_DONE | CRC_ERR) == RX_DONE | CRC_ERR


def test_a_louder_frame_takes_the_receiver_only_past_the_capture_margin(chip):
    chip.configure()
    chip.write(SET_RX, 0xFF, 0xFF, 0xFF)
    settle()

    # 3 dB louder: the receiver stays on the first frame.
    chip.ether.rx_begin(201, -90, 10_000, 20_000, 300_000)
    settle(0.02)
    chip.ether.rx_begin(202, -87, 10_000, 20_000, 300_000)
    settle(0.05)
    chip.ether.rx_end(202, b"the second")
    settle(0.1)
    assert chip.irq() & RX_DONE == 0
    chip.ether.rx_end(201, b"the first")
    chip.wait_irq(RX_DONE)
    length, start = chip.read(GET_RX_BUF_STATUS, 2)
    assert chip.read_buffer(start, length) == b"the first"

    # 7 dB louder: the receiver drops the first and takes the second.
    chip.clear_irq()
    chip.ether.rx_begin(203, -90, 10_000, 20_000, 300_000)
    settle(0.02)
    chip.ether.rx_begin(204, -83, 10_000, 20_000, 300_000)
    settle(0.05)
    chip.ether.rx_end(203, b"the quiet one")
    settle(0.1)
    assert chip.irq() & RX_DONE == 0
    chip.ether.rx_end(204, b"the loud one")
    chip.wait_irq(RX_DONE)
    length, start = chip.read(GET_RX_BUF_STATUS, 2)
    assert chip.read_buffer(start, length) == b"the loud one"


def test_the_ether_decides_whether_a_frame_takes_a_busy_receiver(chip):
    chip.configure()
    chip.write(SET_RX, 0xFF, 0xFF, 0xFF)
    settle()

    # Only 3 dB louder, but the ether says it takes the receiver: it does.
    chip.ether.rx_begin(211, -90, 10_000, 20_000, 300_000)
    settle(0.02)
    chip.ether.rx_begin(212, -87, 10_000, 20_000, 300_000, takes=True)
    settle(0.05)
    chip.ether.rx_end(211, b"the first")
    settle(0.1)
    assert chip.irq() & RX_DONE == 0
    chip.ether.rx_end(212, b"the second")
    chip.wait_irq(RX_DONE)
    length, start = chip.read(GET_RX_BUF_STATUS, 2)
    assert chip.read_buffer(start, length) == b"the second"

    # 7 dB louder, but the ether says it does not: the receiver stays.
    chip.clear_irq()
    chip.ether.rx_begin(213, -90, 10_000, 20_000, 300_000)
    settle(0.02)
    chip.ether.rx_begin(214, -83, 10_000, 20_000, 300_000, takes=False)
    settle(0.05)
    chip.ether.rx_end(214, b"the loud one")
    settle(0.1)
    assert chip.irq() & RX_DONE == 0
    chip.ether.rx_end(213, b"the quiet one")
    chip.wait_irq(RX_DONE)
    length, start = chip.read(GET_RX_BUF_STATUS, 2)
    assert chip.read_buffer(start, length) == b"the quiet one"


def test_standby_during_a_reception_drops_it(chip):
    chip.configure()
    chip.write(SET_RX, 0xFF, 0xFF, 0xFF)
    settle()
    chip.ether.rx_begin(301, -80, 10_000, 20_000, 200_000)
    chip.wait_irq(HEADER_VALID)
    chip.write(SET_STANDBY, 0x00)
    chip.ether.rx_end(301, b"never")
    settle(0.1)
    assert chip.irq() & RX_DONE == 0

    # Back in RX before the end: the lock is gone, so the end is not this
    # receiver's either.
    chip.clear_irq()
    chip.ether.clear()
    chip.write(SET_RX, 0xFF, 0xFF, 0xFF)
    settle()
    chip.ether.rx_begin(302, -80, 10_000, 20_000, 200_000)
    chip.wait_irq(HEADER_VALID)
    chip.write(SET_STANDBY, 0x00)
    chip.write(SET_RX, 0xFF, 0xFF, 0xFF)
    chip.ether.rx_end(302, b"never either")
    settle(0.1)
    assert chip.irq() & RX_DONE == 0


def test_rssi_inst_reads_the_air_then_the_floor_and_nothing_outside_rx(chip):
    chip.configure()
    chip.write(SET_RX, 0xFF, 0xFF, 0xFF)
    settle()
    chip.ether.rx_begin(401, -75, 10_000, 20_000, 150_000)
    settle(0.03)
    assert chip.read(GET_RSSI_INST, 1)[0] == 150        # -2 x -75
    settle(0.2)
    assert chip.read(GET_RSSI_INST, 1)[0] == 220        # -2 x -110, the floor
    chip.write(SET_STANDBY, 0x00)
    assert chip.read(GET_RSSI_INST, 1)[0] == 0xFF


# ---------------------------------------------------------------------------
# 9. The sync word
# ---------------------------------------------------------------------------

def test_the_sync_word_register_publishes_the_word_it_encodes(chip):
    chip.ether.clear()
    chip.write(WRITE_REGISTER, 0x07, 0x40, 0x14, 0x24)
    _, state = chip.ether.expect("state")
    assert state["sync"] == 0x12
    chip.write(WRITE_REGISTER, 0x07, 0x40, 0x44, 0x24)
    _, state = chip.ether.expect("state")
    assert state["sync"] == 0x42


# ---------------------------------------------------------------------------
# 10. Channel activity detection
# ---------------------------------------------------------------------------

def start_cad(chip, symbols_code=0x01):
    """Carrier sense the way a driver that polls starts it."""
    chip.write(SET_STANDBY, 0x00)
    chip.cad_params(symbols_code)
    chip.clear_irq()
    chip.write(SET_CAD)
    return time.monotonic()


def test_cad_on_a_quiet_channel_is_done_and_detects_nothing(chip):
    chip.configure(dio1=CAD_DONE)
    chip.ether.clear()
    started = start_cad(chip)
    _, state = chip.ether.expect("state", mode="CAD")
    assert chip.status() == ST_RX | DATA_AVAIL
    done = chip.wait_irq(CAD_DONE)
    assert done - started == pytest.approx(2 * TSYM, abs=0.010)
    assert chip.irq() == CAD_DONE
    assert chip.dio1() == 1
    chip.ether.expect("state", mode="STDBY_RC")


def test_cad_with_a_frame_in_flight_detects_it(chip):
    chip.configure()
    chip.write(SET_RX, 0xFF, 0xFF, 0xFF)
    settle()
    chip.ether.rx_begin(501, -95, 50_000, 80_000, 400_000)
    settle(0.02)
    start_cad(chip)                 # standby first, as a driver does
    chip.wait_irq(CAD_DONE)
    assert chip.irq() == CAD_DONE | CAD_DETECTED


def test_cad_started_while_following_a_frame_abandons_the_lock(chip):
    chip.configure()
    chip.write(SET_RX, 0xFF, 0xFF, 0xFF)
    settle()
    chip.ether.rx_begin(601, -80, 10_000, 20_000, 200_000)
    chip.wait_irq(HEADER_VALID)
    chip.clear_irq()
    chip.cad_params()
    chip.write(SET_CAD)
    chip.wait_irq(CAD_DONE)
    assert chip.irq() & CAD_DETECTED
    chip.write(SET_RX, 0xFF, 0xFF, 0xFF)
    chip.ether.rx_end(601, b"abandoned")
    settle(0.1)
    assert chip.irq() & RX_DONE == 0


def test_a_frame_that_starts_during_cad_is_energy(chip):
    chip.configure()
    start_cad(chip, symbols_code=0x04)       # 16 symbols, 33 ms at SF8
    settle(0.005)
    chip.ether.rx_begin(701, -100, 50_000, 80_000, 300_000, cad=True)
    chip.wait_irq(CAD_DONE)
    assert chip.irq() == CAD_DONE | CAD_DETECTED
    assert chip.read(GET_IRQ, 2)            # and nothing was demodulated:
    settle(0.1)
    assert chip.irq() & (PREAMBLE | HEADER_VALID | RX_DONE) == 0


# ---------------------------------------------------------------------------
# 11. The IRQ register
# ---------------------------------------------------------------------------

def test_clear_irq_clears_only_the_given_bits_and_reads_never_tear(chip):
    chip.configure(length=4)
    chip.write(SET_TX, 0x00, 0x00, 0x00)
    chip.wait_irq(TX_DONE)
    start_cad(chip)
    chip.wait_irq(CAD_DONE)
    # start_cad cleared TX_DONE on the way in; raise it again beside CAD_DONE.
    chip.write(SET_TX, 0x00, 0x00, 0x00)
    chip.wait_irq(TX_DONE)
    assert chip.irq() == TX_DONE | CAD_DONE
    chip.clear_irq(TX_DONE)
    assert chip.irq() == CAD_DONE
    chip.clear_irq(CAD_DONE)
    assert chip.irq() == 0

    # Hammer the register from a thread while the timer thread raises two
    # bits in both of its bytes at once: every read is all or nothing.
    chip.write(SET_RX, 0xFF, 0xFF, 0xFF)
    settle()
    chip.ether.rx_begin(801, -90, 900_000, 950_000, 2_000_000)
    settle(0.02)
    seen = set()
    stop = threading.Event()

    def hammer():
        while not stop.is_set():
            seen.add(chip.irq())

    reader = threading.Thread(target=hammer)
    reader.start()
    try:
        for _ in range(20):
            start_cad(chip)
            chip.wait_irq(CAD_DONE)
    finally:
        stop.set()
        reader.join()
    assert seen <= {0, CAD_DONE | CAD_DETECTED}, sorted(hex(v) for v in seen)
    assert CAD_DONE | CAD_DETECTED in seen


# ---------------------------------------------------------------------------
# 12. Reset
# ---------------------------------------------------------------------------

def test_reset_restores_the_mode_and_clears_the_irqs(chip):
    chip.configure(dio1=CAD_DONE)
    start_cad(chip)
    chip.wait_irq(CAD_DONE)
    chip.write(SET_RX, 0xFF, 0xFF, 0xFF)
    assert chip.dio1() == 1
    assert chip.status() == ST_RX | DATA_AVAIL

    chip.lib.simradio_reset(chip.handle)
    assert chip.status() == ST_STDBY_RC | DATA_AVAIL
    assert chip.irq() == 0
    assert chip.dio1() == 0
    assert chip.edges[-1][1] == 0
    assert chip.lib.simradio_pin(chip.handle, PIN_BUSY) == 0
