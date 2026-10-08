"""The LR2021 model through its C ABI, against the fake ether. No firmware.

The harness is the SX1262's (test_model.py) with the LR2021's framing: a
command is a two-byte opcode and its parameters under one NSS; a read is that
command, then a second frame of zeros whose reply is two status bytes and the
data; a FIFO moves under one frame. The commands are the ones the
reticulum-lr2021 driver sends, byte for byte.

Each test is named for the rule it holds.
"""

import base64
import ctypes
import os
import time

import pytest

from test_model import (BUILD, HOST_TIMER_LATE_S, PIN_CB, PIN_DIO1, FakeEther, load_library,
                        toa_seconds)

LIBRARY = os.path.join(BUILD, "libsimradio-lr2021.so")
SID = 8

# Commands as (group, opcode).
GET_VERSION, WRITE_REG, WRITE_REG_MASK, READ_REG = (1, 0x01), (1, 0x04), (1, 0x05), (1, 0x06)
GET_ERRORS, CLEAR_ERRORS, DIO_IRQ_CONFIG = (1, 0x10), (1, 0x11), (1, 0x15)
CLEAR_IRQ, GET_AND_CLEAR_IRQ = (1, 0x16), (1, 0x17)
RX_FIFO_LEVEL, TX_FIFO_LEVEL, CLEAR_RX_FIFO, CLEAR_TX_FIFO = (1, 0x1C), (1, 0x1D), (1, 0x1E), (1, 0x1F)
CALIB_FE, GET_TEMP, SET_STANDBY = (1, 0x23), (1, 0x25), (1, 0x28)
RF_FREQUENCY, PA_CONFIG, TX_PARAMS = (2, 0x00), (2, 0x02), (2, 0x03)
FALLBACK, PACKET_TYPE, RSSI_INST, SET_RX, SET_TX = (2, 0x06), (2, 0x07), (2, 0x0B), (2, 0x0C), (2, 0x0D)
TIMESTAMP_SOURCE, TIMESTAMP_VALUE, SET_CCA, CCA_RESULT = (2, 0x16), (2, 0x17), (2, 0x18), (2, 0x19)
MODULATION, PACKET, SYNCWORD, SIDE_DET = (2, 0x20), (2, 0x21), (2, 0x23), (2, 0x24)
CAD_PARAMS, SET_CAD, RX_STATS, PACKET_STATUS = (2, 0x27), (2, 0x28), (2, 0x29), (2, 0x2A)
RD_RX_FIFO, WR_TX_FIFO = (0, 0x01), (0, 0x02)

PREAMBLE, HEADER_VALID, CAD_DETECTED, HEADER_ERR = 1 << 5, 1 << 6, 1 << 7, 1 << 9
RX_DONE, TX_DONE, CAD_DONE, TIMEOUT, CRC_ERROR = 1 << 18, 1 << 19, 1 << 20, 1 << 21, 1 << 22
LORA_TXRX = 0x007F0AE0          # what the driver routes to DIO8

# The tests' carrier: SF8, BW125 (code 4), CR 4/5, an 18-symbol preamble.
FREQ, SF, BW, BW_CODE, CR, PRE = 869_525_000, 8, 125_000, 4, 5, 18
TSYM = (1 << SF) / BW


@pytest.fixture(scope="module")
def station():
    lib = load_library(LIBRARY)
    ether = FakeEther()
    assert lib.simradio_station_open(SID, b"127.0.0.1",
                                     ("127.0.0.1:%d" % ether.port).encode()) == 0
    _, hello = ether.expect("hello")
    assert hello["sid"] == SID
    return lib, ether


class Lr2021:
    """Slot 0, opened fresh, driven as the reticulum-lr2021 driver drives it."""

    def __init__(self, lib, ether):
        self.lib = lib
        self.ether = ether
        self.callback = PIN_CB(lambda _ctx, _pin, _level: None)   # held, or ctypes frees it
        self.handle = lib.simradio_open(0, self.callback, None)
        assert self.handle

    def close(self):
        self.lib.simradio_close(self.handle)

    def frame(self, out):
        out = bytes(out)
        reply = ctypes.create_string_buffer(len(out))
        self.lib.simradio_transfer(self.handle, out, len(out), reply)
        return reply.raw

    def cmd(self, op, *params):
        """The driver's `cmd`: opcode and parameters under one NSS."""
        self.frame([*op, *params])

    def query(self, op, n, *params):
        """The driver's `query`: the command, then n bytes of zeros; the reply
        after its two status bytes."""
        self.frame([*op, *params])
        return self.frame([0] * n)[2:]

    def word(self, op, n, *params):
        return int.from_bytes(self.query(op, n, *params), "big")

    def irq(self):
        return self.word(GET_AND_CLEAR_IRQ, 6)

    def stats(self):
        r = self.query(RX_STATS, 12)
        return [int.from_bytes(r[i:i + 2], "big") for i in range(0, 10, 2)]

    def rx_fifo_level(self):
        return self.word(RX_FIFO_LEVEL, 4)

    def read_rx_fifo(self, n):
        return self.frame([*RD_RX_FIFO] + [0] * n)[2:]

    def dio8(self):
        return self.lib.simradio_pin(self.handle, PIN_DIO1)

    # ---- as the driver sets it up -------------------------------------------

    def configure(self, length=42):
        """The LoRa part of the driver's `configure`, in its order."""
        self.cmd(SET_STANDBY, 0x01)
        self.cmd(PACKET_TYPE, 0x00)
        self.cmd(DIO_IRQ_CONFIG, 0x08, *LORA_TXRX.to_bytes(4, "big"))
        self.cmd(RF_FREQUENCY, *FREQ.to_bytes(4, "big"))
        self.cmd(CALIB_FE, *(FREQ // 4_000_000).to_bytes(2, "big"))
        self.cmd(MODULATION, SF << 4 | BW_CODE, (CR - 4) << 4)
        self.cmd(TIMESTAMP_SOURCE, 0x02)              # slot 0: the last bit received
        self.cmd(PACKET, PRE >> 8, PRE & 0xFF, length, 0x02)
        self.cmd(SYNCWORD, 0x12)
        self.cmd(PA_CONFIG, 0x00, 4 << 4 | 2, 0x10)   # 14 dBm: 34/4/2
        self.cmd(TX_PARAMS, 34, 0x08)
        self.cmd(FALLBACK, 0x02)                      # STBY_XOSC after TX and RX

    def start_rx(self):
        """The driver's Start RX: standby, clear the FIFO and the IRQs, receive
        continuously."""
        self.cmd(SET_STANDBY, 0x01)
        self.cmd(CLEAR_RX_FIFO)
        self.cmd(CLEAR_IRQ, 0xFF, 0xFF, 0xFF, 0xFF)
        self.cmd(SET_RX, 0xFF, 0xFF, 0xFF)

    def send(self, payload):
        self.cmd(CLEAR_TX_FIFO)
        self.frame([*WR_TX_FIFO, *payload])
        self.cmd(SET_TX)

    def wait_irq(self, bits, timeout=2.0):
        """Poll GetAndClearIrq, as the driver does, until one of `bits` comes;
        everything read on the way, together."""
        seen = 0
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            seen |= self.irq()
            if seen & bits:
                return seen
            time.sleep(0.0005)
        raise AssertionError("IRQ 0x%06x never raised (have 0x%06x)" % (bits, seen))

    def until(self, check, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if check():
                return
            time.sleep(0.001)
        raise AssertionError("never came to pass")

    def hear(self, eid, payload, verdict="clean", level=-70, snr=7):
        """A frame from the ether: begun, its header past, then ended."""
        self.ether.rx_begin(eid, level, 1000, 2000, 50_000)
        time.sleep(0.03)
        self.ether.rx_end(eid, payload, verdict=verdict, rssi=level, snr=snr)


@pytest.fixture
def chip(station):
    lib, ether = station
    ether.clear()
    c = Lr2021(lib, ether)
    try:
        yield c
    finally:
        c.cmd(SET_STANDBY, 0x00)
        c.close()
        time.sleep(0.05)
        ether.clear()


# ---------------------------------------------------------------------------
# The bus
# ---------------------------------------------------------------------------

def test_a_read_answers_in_the_frame_after_its_command(chip):
    chip.frame([*GET_VERSION])
    assert chip.frame([0] * 4)[2:] == bytes([0x01, 0x18])
    # Served once: zeros with no command before them read nothing.
    assert chip.frame([0] * 4) == bytes(4)


def test_registers_hold_what_extend_fifos_writes_and_masks_merge(chip):
    for addr, value in ((0xF30034, 0x3FC), (0xF30030, 0x3E8)):
        chip.cmd(WRITE_REG, *addr.to_bytes(3, "big"), *value.to_bytes(4, "big"))
        assert chip.word(READ_REG, 6, *addr.to_bytes(3, "big"), 0x01) == value
    chip.cmd(WRITE_REG, 0xF3, 0x0A, 0x2C, *(0x0003_00FF).to_bytes(4, "big"))
    chip.cmd(WRITE_REG_MASK, 0xF3, 0x0A, 0x2C, *(0x0003_0000).to_bytes(4, "big"),
             *(0x0001_0000).to_bytes(4, "big"))
    assert chip.word(READ_REG, 6, 0xF3, 0x0A, 0x2C, 0x01) == 0x0001_00FF


def test_dio8_is_the_masked_irq_word_and_the_read_clears_both(chip):
    chip.configure(length=4)
    chip.send(b"ping")
    chip.until(chip.dio8)
    assert chip.irq() & TX_DONE
    assert not chip.dio8()
    assert chip.irq() == 0


# ---------------------------------------------------------------------------
# Transmitting
# ---------------------------------------------------------------------------

def test_set_tx_sends_the_tx_fifo_timed_by_the_toa_formula(chip):
    payload = bytes(range(42))
    chip.configure(length=42)
    chip.cmd(CLEAR_TX_FIFO)
    chip.frame([*WR_TX_FIFO, *payload])
    assert chip.word(TX_FIFO_LEVEL, 4) == 42
    chip.ether.clear()
    chip.cmd(SET_TX)
    _, tx = chip.ether.expect("tx")

    assert tx["sid"] == SID
    assert (tx["freq"], tx["bw"], tx["sf"], tx["cr"], tx["pre"], tx["sync"]) == (
        FREQ, BW, SF, CR, PRE, 0x12)
    assert tx["power_dbm"] == 14
    assert tx["t_pre"] - tx["t0"] == int((PRE + 4.25) * TSYM * 1e6)
    assert tx["t_hdr"] - tx["t_pre"] == int(8 * TSYM * 1e6)
    assert tx["t_end"] - tx["t0"] == int(toa_seconds(42) * 1e6)
    assert base64.b64decode(tx["payload"]) == payload
    assert chip.word(TX_FIFO_LEVEL, 4) == 0


def test_tx_done_lands_at_the_end_and_the_chip_falls_back_to_xosc(chip):
    chip.configure(length=10)
    chip.ether.clear()
    sent = time.monotonic()
    chip.send(bytes(10))
    assert chip.wait_irq(TX_DONE) & TX_DONE
    took = time.monotonic() - sent
    assert toa_seconds(10) - 0.001 <= took <= toa_seconds(10) + HOST_TIMER_LATE_S
    chip.ether.expect("tx")
    chip.ether.expect("state", mode="STDBY_XOSC")


def test_standby_aborts_a_transmission_without_tx_done(chip):
    chip.configure(length=10)
    chip.send(bytes(10))
    chip.cmd(SET_STANDBY, 0x01)
    time.sleep(toa_seconds(10) + HOST_TIMER_LATE_S)
    assert chip.irq() & TX_DONE == 0


# ---------------------------------------------------------------------------
# Receiving
# ---------------------------------------------------------------------------

def test_continuous_rx_keeps_two_frames_for_a_late_read(chip):
    """Each frame is appended at its last bit and the chip stays in RX, so a
    driver that reads after two frames finds both, the last one's status."""
    chip.configure()
    chip.start_rx()
    chip.ether.expect("state", mode="RX")
    base = chip.stats()
    chip.hear(1, b"A" * 10, snr=5)
    chip.hear(2, b"B" * 20, level=-80, snr=9)
    chip.until(lambda: chip.rx_fifo_level() == 30)

    received, crc_err, hdr_err, hdr_valid, _ = (a - b for a, b in zip(chip.stats(), base))
    assert (received, crc_err, hdr_err, hdr_valid) == (2, 0, 0, 2)
    assert chip.irq() & (PREAMBLE | HEADER_VALID | RX_DONE) == PREAMBLE | HEADER_VALID | RX_DONE
    status = chip.query(PACKET_STATUS, 8)
    assert (status[1], status[2], -((status[3] << 1) | ((status[5] >> 1) & 1)) / 2) == (
        20, 9 * 4, -80)
    assert status[5] >> 2 & 0xF == 0b0001       # the main detector
    assert chip.read_rx_fifo(10) == b"A" * 10
    assert chip.rx_fifo_level() == 20
    assert chip.read_rx_fifo(20) == b"B" * 20
    assert chip.rx_fifo_level() == 0
    time.sleep(0.02)
    assert all(m.get("type") != "state" for _, m in list(chip.ether.inbox.queue))   # still RX


def test_the_timestamp_counts_32mhz_ticks_back_to_the_last_bit(chip):
    chip.configure()
    chip.start_rx()
    chip.hear(1, b"x" * 8)
    chip.wait_irq(RX_DONE)
    ended = time.monotonic()
    time.sleep(0.03)
    ticks = chip.word(TIMESTAMP_VALUE, 6, 0)
    ago = time.monotonic() - ended
    assert 0.03 <= ticks / 32e6 <= ago + HOST_TIMER_LATE_S


def test_a_crc_failure_is_stored_and_flagged_with_rx_done(chip):
    chip.configure()
    chip.start_rx()
    base = chip.stats()
    chip.hear(1, b"bad!", verdict="crc")
    seen = chip.wait_irq(RX_DONE)
    assert seen & CRC_ERROR
    assert chip.rx_fifo_level() == 4
    received, crc_err = (a - b for a, b in zip(chip.stats()[:2], base[:2]))
    assert (received, crc_err) == (1, 1)


def test_a_header_error_stores_nothing_and_receiving_goes_on(chip):
    chip.configure()
    chip.start_rx()
    base = chip.stats()
    chip.hear(1, b"lost", verdict="hdr")
    seen = chip.wait_irq(HEADER_ERR)
    assert seen & RX_DONE == 0
    assert chip.rx_fifo_level() == 0
    assert chip.stats()[2] - base[2] == 1
    chip.hear(2, b"next")
    chip.wait_irq(RX_DONE)
    assert chip.read_rx_fifo(4) == b"next"


def test_a_single_receive_times_out_in_32768ths_and_falls_back(chip):
    chip.configure()
    chip.cmd(SET_STANDBY, 0x01)
    chip.ether.clear()
    started = time.monotonic()
    chip.cmd(SET_RX, *(32768 // 50).to_bytes(3, "big"))     # 20 ms
    chip.wait_irq(TIMEOUT)
    assert 0.019 <= time.monotonic() - started <= 0.02 + HOST_TIMER_LATE_S
    chip.ether.expect("state", mode="RX")
    chip.ether.expect("state", mode="STDBY_XOSC")


def test_receiving_without_a_front_end_calibration_latches_error_0x0200(chip):
    chip.cmd(SET_STANDBY, 0x01)
    chip.cmd(SET_RX, 0xFF, 0xFF, 0xFF)
    assert chip.word(GET_ERRORS, 4) == 0x0200
    chip.cmd(CLEAR_ERRORS)
    chip.configure()                    # calibrates the front end
    chip.start_rx()
    assert chip.word(GET_ERRORS, 4) == 0


def test_the_temperature_reads_zero_while_receiving(chip):
    chip.configure()
    centi = lambda word: word >> 3 & 0x1FFF        # 32nds of a degree
    assert centi(chip.word(GET_TEMP, 4, 0x0D)) == 25 * 32
    chip.start_rx()
    assert chip.word(GET_TEMP, 4, 0x0D) == 0


def test_the_sync_word_is_one_byte_and_taken_while_receiving(chip):
    chip.configure()
    chip.start_rx()
    chip.ether.expect("state", mode="RX")
    chip.cmd(SYNCWORD, 0x34)
    _, st = chip.ether.expect("state", mode="RX")
    assert st["sync"] == 0x34


def test_side_detectors_hear_their_sfs_until_the_next_modulation(chip):
    chip.configure()
    chip.cmd(MODULATION, 7 << 4 | BW_CODE, (CR - 4) << 4)
    chip.cmd(SIDE_DET, 5 << 4, 6 << 4)
    chip.ether.clear()
    chip.start_rx()
    _, st = chip.ether.expect("state", mode="RX")
    assert (st["sf"], st["sfs"]) == (7, [5, 6, 7])
    chip.cmd(MODULATION, 7 << 4 | BW_CODE, (CR - 4) << 4)
    _, st = chip.ether.expect("state", mode="RX")
    assert "sfs" not in st


# ---------------------------------------------------------------------------
# Sensing the channel
# ---------------------------------------------------------------------------

def test_rssi_inst_reads_the_air_while_receiving(chip):
    def dbm():
        r = chip.query(RSSI_INST, 4)
        return -((r[0] << 1) | (r[1] & 1)) / 2

    chip.configure()
    chip.start_rx()
    assert dbm() == -110
    chip.ether.rx_begin(3, -70, 1000, 2000, 200_000, cad=True)
    chip.until(lambda: dbm() == -70)


def test_cad_finds_a_frame_heard_before_standby(chip):
    """The driver's CSMA: standby, then CAD; a frame the receiver was hearing
    is still on the air."""
    chip.configure()
    chip.cmd(CAD_PARAMS, 0x02, 0x00, 0x00, 0, 0, 0, 48)
    chip.cmd(SET_STANDBY, 0x01)
    chip.cmd(SET_CAD)
    assert chip.wait_irq(CAD_DONE) & CAD_DETECTED == 0

    chip.start_rx()
    chip.ether.rx_begin(4, -90, 1000, 2000, 200_000)
    time.sleep(0.01)
    chip.cmd(SET_STANDBY, 0x01)
    chip.cmd(CLEAR_IRQ, 0xFF, 0xFF, 0xFF, 0xFF)
    chip.cmd(SET_CAD)
    assert chip.wait_irq(CAD_DONE) & CAD_DETECTED


def test_cca_reports_the_loudest_level_it_heard_and_ends_in_timeout(chip):
    chip.configure()
    chip.cmd(SET_STANDBY, 0x01)
    chip.cmd(SET_CCA, *(32 * 30_000).to_bytes(3, "big"), 0x00)    # 30 ms
    chip.ether.rx_begin(5, -60, 1000, 2000, 10_000)
    chip.wait_irq(TIMEOUT)
    r = chip.query(CCA_RESULT, 6)
    assert -((r[1] << 1) | ((r[3] >> 1) & 1)) / 2 == -60
    chip.ether.expect("state", mode="CAD")
    chip.ether.expect("state", mode="STDBY_XOSC")
