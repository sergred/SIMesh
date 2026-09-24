#!/usr/bin/env python3
"""The virtual ether: one UDP endpoint that carries frames between stations.

Stations announce themselves with `hello`, describe their radio with `state`
and hand over a transmission with `tx`; the ether answers `welcome`,
`rx_begin` and `rx_end`. A frame reaches every other station that is close
enough for it to rise out of the noise and whose latest state is `RX` on the
same carrier, bandwidth, spreading factor and sync word. A station in `CAD`
on that carrier is told the frame is arriving and nothing more: it is sensing
energy, not receiving. Two frames sharing a
carrier and any instant of air interfere, and each receiver decides the
outcome for itself: the stronger frame survives when it leads the other by the
capture margin.

The level a frame arrives at is computed, not stated. Every station has a
position in metres and an antenna gain, every pair may have an obstruction in
dB and, when the scenario asks for it, a shadowing draw of its own, and

    L = P_tx + G_tx + G_rx − PL(d)
    PL(d) = FSPL(1 m, f) + 10·n·log10(d) + X(tx, rx) + obstruction(tx, rx)

unless the pair has a link: a path loss stated outright, measured or from a
propagation model, which stands in for the distance and the shadowing.

Positions arrive by direct call — `place()`, `obstruct()` and `link()` — from
whatever is driving the run; the UDP wire to the stations never mentions them,
and a station never learns where it is.

Clocks. A station's `t` fields are its own clock and are meaningful only
relative to each other inside one message; the ether rebases every frame onto
its own monotonic clock and schedules from that, so the `t` fields it sends
back are ether microseconds. The offsets inside a frame (`t_pre - t0`,
`t_hdr - t0`, `t_end - t0`) are the transmitter's and are carried through
unchanged, which is what a receiver actually needs.

Every datagram in and out is one tab-separated line of the record:
wall-clock timestamp, direction, station id, JSON.
"""

import argparse
import asyncio
import functools
import hashlib
import json
import math
import os
import random
import sys
from datetime import datetime, timezone

# What a receiver's state must share with a frame for the frame to be heard.
MATCH_KEYS = ("bw", "sf", "sync")

# ...and how far apart two carriers may be and still be one carrier, as a
# fraction of the bandwidth. The synthesizer steps in 32 MHz / 2^25, so two
# drivers asked for the same frequency round it to register values tens of
# hertz apart; an exact match would make them deaf to each other, which no
# receiver is. A LoRa demodulator tolerates an offset of a quarter of its
# bandwidth, and that is the figure here.
CARRIER_TOLERANCE = 0.25


def same_carrier(freq_a, freq_b, bw_hz):
    """True when two stated frequencies are one carrier at this bandwidth."""
    if freq_a is None or freq_b is None:
        return freq_a == freq_b
    return abs(freq_a - freq_b) <= CARRIER_TOLERANCE * float(bw_hz or 125_000)

DEFAULT_EXPONENT = 2.7      # suburban; 2 is free space
DEFAULT_NOISE_FIGURE_DB = 6
DEFAULT_CAPTURE_DB = 6      # how far a frame must lead an interferer to survive it
DEFAULT_POWER_DBM = 14      # a `tx` that did not say what it was sent at
DEFAULT_SHADOWING_DB = 0.0  # the spread of a pair's shadowing draw; 0 is none
DEFAULT_SHADOWING_SEED = 0  # which draws: the same seed is the same ground
DEFAULT_CAPTURE_MODEL = "margin"   # or "bench": capture as a bench measured it
CAPTURE_MODELS = ("margin", "bench")
DEFAULT_SF_ORTHOGONALITY = "none"  # or "croce": another SF interferes less
SF_ORTHOGONALITIES = ("none", "croce")
DEFAULT_CRC_BAND_DB = 0.0   # how far above its threshold a frame can still fail

# How far under a frame at another spreading factor a frame can be and still
# be received, in dB, measured with an SX1272 at 125 kHz: D. Croce et al.,
# "Impact of LoRa Imperfect Orthogonality: Analysis of Link-Level Performance",
# IEEE Communications Letters 22(4), 2018, doi:10.1109/LCOMM.2018.2797057,
# Table II. Keyed by the wanted frame's spreading factor, then the
# interferer's. The table's diagonal (+1 dB) is not used: two frames at one
# spreading factor go by the capture rule. SF5 and SF6 were not measured, and
# a pair involving them interferes as if it shared a spreading factor.
CROCE_SIR_DB = {
    7: {8: -8, 9: -9, 10: -9, 11: -9, 12: -9},
    8: {7: -11, 9: -11, 10: -12, 11: -13, 12: -13},
    9: {7: -15, 8: -13, 10: -13, 11: -14, 12: -15},
    10: {7: -19, 8: -18, 9: -17, 11: -17, 12: -18},
    11: {7: -22, 8: -22, 9: -21, 10: -20, 12: -20},
    12: {7: -25, 8: -25, 9: -25, 10: -24, 11: -23},
}

# Capture as a bench measured it: an SX1262 listening, an SX1262 and an LR2021
# sending, SF7 at 125 kHz, 289 collisions of two frames that started within
# 8 ms of each other (the reticulum project's tools/rncapture, 2026-09-17).
# Within 1.2 dB the two are equals: both were lost 9 times in 39, and otherwise
# one of them survived, either one. From there to 2.7 dB the stronger survived
# 119 times in 136, and from 6.1 dB every time; the straight line between is
# an assumption. The weaker never survived. Other spreading factors are
# assumed to behave the same.
BENCH_EQUAL_DB = 1.2
BENCH_BOTH_LOST = 9 / 39
BENCH_STRONGER = 119 / 136
BENCH_STRONGER_DB = 2.7
BENCH_CERTAIN_DB = 6.1

# The SNR a spreading factor needs before its receiver detects a preamble at
# all, from the SX1262 datasheet: -2.5 dB at SF5 and 2.5 dB lower per step. A
# frame that arrives under its own modem's threshold is not delivered — no
# rx_begin goes out — and that is what "out of range" means here.
#
# It is per spreading factor because that is the whole point of one: SF12 buys
# 17.5 dB of reach over SF7 and pays for it in air time. A flat threshold would
# make the two identical to the medium, and a scenario that moved SF to reach
# further would see no change at all.
SENSITIVITY_DB = {5: -2.5, 6: -5.0, 7: -7.5, 8: -10.0,
                  9: -12.5, 10: -15.0, 11: -17.5, 12: -20.0}
SLOWEST_SENSITIVITY_DB = -20.0      # a frame that named no spreading factor

MIN_DISTANCE_M = 1.0        # two stations at one point are still a metre apart
EARTH_RADIUS_M = 6371008.8  # the mean radius, for the equirectangular projection

MAX_FRAME_US = 60 * 1000 * 1000   # a stated timeline longer than this is junk

SPEED_OF_LIGHT = 299_792_458.0


def wall_stamp():
    """The wall-clock timestamp that heads a record line."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def log(msg):
    """One line of human-readable running commentary."""
    sys.stderr.write("%s  %s\n" % (datetime.now().strftime("%H:%M:%S.%f")[:-3], msg))
    sys.stderr.flush()


def project(origin, lat, lon):
    """Degrees of latitude and longitude to metres east and north of `origin`.

    Equirectangular around the origin: the scale in longitude is the origin's
    own cosine rather than each point's, so the plane is flat and distances
    between two points on it are what the path loss is computed from. Over the
    tens of kilometres a LoRa scenario spans the error is far below anything
    the medium models.
    """
    lat0, lon0 = origin
    x = math.radians(lon - lon0) * math.cos(math.radians(lat0)) * EARTH_RADIUS_M
    y = math.radians(lat - lat0) * EARTH_RADIUS_M
    return x, y


def unproject(origin, x, y):
    """Metres east and north of `origin` back to latitude and longitude."""
    lat0, lon0 = origin
    lat = lat0 + math.degrees(y / EARTH_RADIUS_M)
    lon = lon0 + math.degrees(x / (EARTH_RADIUS_M * math.cos(math.radians(lat0))))
    return lat, lon


def fspl_1m_db(freq_hz):
    """Free-space path loss over the first metre at this carrier, in dB."""
    return 20.0 * math.log10(4.0 * math.pi * max(freq_hz, 1.0) / SPEED_OF_LIGHT)


@functools.lru_cache(maxsize=65536)
def shadowing_unit(seed, a, b):
    """One pair's shadowing in standard deviations, the same draw every time.

    A standard normal from a hash of the seed and the unordered pair, so it
    depends on those three numbers and nothing else: not on the order stations
    joined in, not on which end transmits, not on the platform. The scenario's
    `shadowing_db` scales it, so two runs that differ only in the spread stand
    on the same ground, one of it rougher.
    """
    lo, hi = (a, b) if a <= b else (b, a)
    digest = hashlib.sha256(("%d:%d:%d" % (seed, lo, hi)).encode()).digest()
    u1 = (int.from_bytes(digest[:8], "big") + 1) / 2.0 ** 64     # (0, 1]
    u2 = int.from_bytes(digest[8:16], "big") / 2.0 ** 64         # [0, 1)
    return math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)


def pair_draw(seed, eid_a, eid_b, rsid, what):
    """A uniform draw in [0, 1) for two frames at one receiver.

    The same whichever of the two frames asks, so the verdicts on both and the
    `takes` the receiver was told all read one outcome.
    """
    lo, hi = (eid_a, eid_b) if eid_a <= eid_b else (eid_b, eid_a)
    digest = hashlib.sha256(
        ("%d:%d:%d:%d:%s" % (seed, lo, hi, rsid, what)).encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2.0 ** 64


def crc_band_fails(seed, eid, rsid, margin_db, band_db):
    """Whether a frame this far above its threshold fails its CRC anyway.

    Within `band_db` of the threshold the chance falls in a straight line from
    certain, at the threshold, to nothing at the top of the band; the draw is
    one per frame and receiver, from the ether's seed.
    """
    if band_db <= 0 or margin_db >= band_db:
        return False
    return pair_draw(seed, eid, eid, rsid, "crc band") < 1.0 - max(margin_db, 0.0) / band_db


def bench_stronger_odds(lead):
    """How often the stronger of two frames that met survives, by its lead."""
    if lead >= BENCH_CERTAIN_DB:
        return 1.0
    if lead <= BENCH_STRONGER_DB:
        return BENCH_STRONGER
    return BENCH_STRONGER + (1.0 - BENCH_STRONGER) * (
        (lead - BENCH_STRONGER_DB) / (BENCH_CERTAIN_DB - BENCH_STRONGER_DB))


def bench_outcome(seed, first, second, rsid, lead, locked):
    """Which of two frames that met survive at one receiver, as the bench saw.

    `first` and `second` are the frames' numbers in the order they started;
    `lead` is the first's level over the second's at the receiver, in dB. The
    answer is (the first survives, the second survives).

    A receiver `locked` on the first, which was following it when the second
    started after its preamble, never receives the second, however strong,
    and loses the first too unless the first is the stronger: the bench saw a
    frame 2 dB stronger landing 30 ms in spoil both, six times in six. Frames
    that started within a preamble of each other are the bench's table.
    """
    def draw(what):
        return pair_draw(seed, first, second, rsid, what)

    if locked:
        if lead > BENCH_EQUAL_DB:
            return draw("stronger") < bench_stronger_odds(lead), False
        if lead < -BENCH_EQUAL_DB:
            return False, False
        return draw("equal") >= BENCH_BOTH_LOST, False
    if abs(lead) <= BENCH_EQUAL_DB:
        if draw("equal") < BENCH_BOTH_LOST:
            return False, False
        first_wins = draw("coin") < 0.5
        return first_wins, not first_wins
    if lead > 0:
        return draw("stronger") < bench_stronger_odds(lead), False
    return False, draw("stronger") < bench_stronger_odds(-lead)


class Physics:
    """The scenario's constants: how fast the air eats a signal, and the noise."""

    def __init__(self, exponent=DEFAULT_EXPONENT,
                 noise_figure_db=DEFAULT_NOISE_FIGURE_DB,
                 capture_db=DEFAULT_CAPTURE_DB,
                 shadowing_db=DEFAULT_SHADOWING_DB,
                 shadowing_seed=DEFAULT_SHADOWING_SEED,
                 capture_model=DEFAULT_CAPTURE_MODEL,
                 sf_orthogonality=DEFAULT_SF_ORTHOGONALITY,
                 crc_band_db=DEFAULT_CRC_BAND_DB):
        self.exponent = float(exponent)
        self.noise_figure_db = float(noise_figure_db)
        self.capture_db = float(capture_db)
        self.shadowing_db = float(shadowing_db)
        self.shadowing_seed = int(shadowing_seed)
        if capture_model not in CAPTURE_MODELS:
            raise ValueError("capture_model is one of %s, not %r"
                             % (", ".join(CAPTURE_MODELS), capture_model))
        self.capture_model = capture_model
        if sf_orthogonality not in SF_ORTHOGONALITIES:
            raise ValueError("sf_orthogonality is one of %s, not %r"
                             % (", ".join(SF_ORTHOGONALITIES), sf_orthogonality))
        self.sf_orthogonality = sf_orthogonality
        self.crc_band_db = float(crc_band_db)

    def describe(self):
        if self.capture_model == "bench":
            capture = "capture as the bench measured it"
        else:
            capture = "capture margin %.1f dB" % self.capture_db
        text = "exponent %.2f, noise figure %.1f dB, %s" % (
            self.exponent, self.noise_figure_db, capture)
        if self.sf_orthogonality == "croce":
            text += ", spreading factors apart by Croce's table"
        if self.crc_band_db:
            text += ", a %.1f dB CRC band" % self.crc_band_db
        if self.shadowing_db:
            text += ", shadowing %.1f dB (seed %d)" % (self.shadowing_db,
                                                      self.shadowing_seed)
        return text

    @classmethod
    def from_dict(cls, data):
        data = data or {}
        return cls(data.get("exponent", DEFAULT_EXPONENT),
                   data.get("noise_figure_db", DEFAULT_NOISE_FIGURE_DB),
                   data.get("capture_db", DEFAULT_CAPTURE_DB),
                   data.get("shadowing_db", DEFAULT_SHADOWING_DB),
                   data.get("shadowing_seed", DEFAULT_SHADOWING_SEED),
                   data.get("capture_model", DEFAULT_CAPTURE_MODEL),
                   data.get("sf_orthogonality", DEFAULT_SF_ORTHOGONALITY),
                   data.get("crc_band_db", DEFAULT_CRC_BAND_DB))

    def as_dict(self):
        return {"exponent": self.exponent,
                "noise_figure_db": self.noise_figure_db,
                "capture_db": self.capture_db,
                "shadowing_db": self.shadowing_db,
                "shadowing_seed": self.shadowing_seed,
                "capture_model": self.capture_model,
                "sf_orthogonality": self.sf_orthogonality,
                "crc_band_db": self.crc_band_db}


class Placement:
    """Where a station stands, and what its antenna adds."""

    def __init__(self, x_m, y_m, gain_db=0.0):
        self.x_m = float(x_m)
        self.y_m = float(y_m)
        self.gain_db = float(gain_db)


class Station:
    """A station the ether has heard from, and the radio state it last stated."""

    def __init__(self, sid, addr, slots):
        self.sid = sid
        self.addr = addr
        self.slots = slots
        self.states = {}        # slot -> the last `state` message for it
        self.tx_until = 0       # while its own frame is on the air, it is deaf
        # The power its last frame went out at. A station states this per
        # transmission rather than in its `state`, so it is learned by
        # listening — and it is what `levels()` must answer with, or the map
        # would draw a reach the station does not have.
        self.power_dbm = DEFAULT_POWER_DBM

    def state(self, slot):
        return self.states.get(slot)

    def listening(self, slot):
        """True when this station's slot last said it was receiving or sensing.

        A slot in CAD is listening for energy: it is told a frame is arriving,
        which is what its channel activity detection needs to find, and never
        told how one ended, because it is not demodulating anything.
        """
        st = self.states.get(slot)
        return bool(st) and st.get("mode") in ("RX", "CAD")

    def sensing(self, slot):
        """True when this station's slot last said it was in CAD."""
        st = self.states.get(slot)
        return bool(st) and st.get("mode") == "CAD"


class Frame:
    """A transmission in flight, on the ether's own clock.

    A frame carries the other frames it shared air with. The verdict is not
    one of its properties: each receiver reads that list at its own rx_end,
    against the levels its own position gives, and may keep a frame the
    station beside it lost.
    """

    def __init__(self, eid, fid, sid, msg, start_us, end_us, pre_us, hdr_us):
        self.eid = eid          # the ether's own number, unique across stations
        self.fid = fid          # the transmitter's, unique only to it
        self.sid = sid
        self.freq = msg.get("freq")
        self.bw = msg.get("bw")
        self.sf = msg.get("sf")
        self.sync = msg.get("sync")
        self.power_dbm = msg.get("power_dbm", DEFAULT_POWER_DBM)
        self.payload = msg.get("payload", "")
        self.start_us = start_us
        self.end_us = end_us
        self.pre_us = pre_us
        self.hdr_us = hdr_us
        self.interferers = []   # frames that shared this one's carrier and air
        self.receivers = []     # (sid, slot, level) for each station that locked on

    def overlaps(self, other):
        """True when the two frames share the carrier and any instant of air."""
        return (same_carrier(self.freq, other.freq, max(self.bw or 0, other.bw or 0))
                and self.start_us < other.end_us
                and other.start_us < self.end_us)


class Ether(asyncio.DatagramProtocol):
    """The UDP endpoint: parses station messages and delivers frames.

    Whatever drives a run places the stations and subscribes to `on_tx`,
    `on_rx` and `on_station` to watch the air. Each callback takes the
    arguments named beside it below and returns nothing; exceptions raised in
    one are logged and swallowed, because a page that has gone away must not
    be able to stop the medium.
    """

    def __init__(self, record_path, physics=None, seed=None):
        self.transport = None
        self.loop = asyncio.get_event_loop()
        self.physics = physics or Physics()
        self.stations = {}          # sid -> Station
        self.places = {}            # sid -> Placement, whether or not it has joined
        self.obstructions = {}      # frozenset({a, b}) -> dB
        self.links = {}             # frozenset({a, b}) -> dB: a path loss stated outright
        self.frames = []            # frames still in flight or just ended
        self.locks = {}             # (sid, slot) -> (frame, level): what a receiver follows
        self.next_eid = 0           # the ether's own frame numbering
        self.seed = seed if seed is not None else random.randrange(1 << 31)
        self.on_tx = None           # (sid, eid, freq, t_start, t_end)
        self.on_rx = None           # (sid, rsid, eid, verdict, level)
        self.on_station = None      # (sid, state)
        self.record = None
        if record_path is not None:
            self.record = open(record_path, "a", encoding="utf-8", buffering=1)
            self.record.write("# %s\tether record: stamp\tdir\tsid\tjson\n"
                              % wall_stamp())

    # ---- clock ---------------------------------------------------------

    def now(self):
        """The ether's own monotonic clock, in microseconds."""
        return int(self.loop.time() * 1_000_000)

    # ---- the arrangement of the network ---------------------------------

    def place(self, sid, x_m, y_m, gain_db=0.0):
        """Put a station at a point on the plane, in metres."""
        self.places[sid] = Placement(x_m, y_m, gain_db)

    def unplace(self, sid):
        """Take a station off the plane; it is deaf and inaudible from then on."""
        self.places.pop(sid, None)
        self.obstructions = {pair: db for pair, db in self.obstructions.items()
                             if sid not in pair}
        self.links = {pair: db for pair, db in self.links.items() if sid not in pair}

    def obstruct(self, a, b, db):
        """Put `db` of extra loss between one pair, in both directions."""
        pair = frozenset((a, b))
        if db:
            self.obstructions[pair] = float(db)
        else:
            self.obstructions.pop(pair, None)

    def link(self, a, b, loss_db):
        """State one pair's path loss outright, in both directions; None forgets it."""
        pair = frozenset((a, b))
        if loss_db is None:
            self.links.pop(pair, None)
        else:
            self.links[pair] = float(loss_db)

    def clear(self):
        """Forget every position, obstruction and link, for a scenario being replaced."""
        self.places.clear()
        self.obstructions.clear()
        self.links.clear()

    def distance(self, a, b):
        """Metres between two placed stations, never less than one."""
        pa, pb = self.places.get(a), self.places.get(b)
        if pa is None or pb is None:
            return None
        return max(MIN_DISTANCE_M, math.hypot(pa.x_m - pb.x_m, pa.y_m - pb.y_m))

    def path_loss(self, a, b, freq_hz):
        """The dB between two stations at this carrier, or None if either is off-plane."""
        d = self.distance(a, b)
        if d is None:
            return None
        pair = frozenset((a, b))
        if pair in self.links:
            loss = self.links[pair]
        else:
            loss = fspl_1m_db(freq_hz) + 10.0 * self.physics.exponent * math.log10(d)
            if self.physics.shadowing_db:
                loss += self.physics.shadowing_db * shadowing_unit(
                    self.physics.shadowing_seed, a, b)
        return loss + self.obstructions.get(pair, 0.0)

    def level(self, tx_sid, rx_sid, freq_hz, power_dbm=DEFAULT_POWER_DBM):
        """The level in dBm a frame from `tx_sid` arrives at `rx_sid`, or None."""
        loss = self.path_loss(tx_sid, rx_sid, freq_hz)
        if loss is None:
            return None
        return (float(power_dbm) + self.places[tx_sid].gain_db
                + self.places[rx_sid].gain_db - loss)

    def noise(self, bw_hz):
        """A receiver's noise floor in dBm for this bandwidth: kTB plus the figure."""
        return -174.0 + 10.0 * math.log10(max(float(bw_hz or 125_000), 1.0)) \
            + self.physics.noise_figure_db

    def sensitivity(self, sf):
        """The SNR this spreading factor needs to detect a frame, in dB."""
        return SENSITIVITY_DB.get(sf, SLOWEST_SENSITIVITY_DB)

    def audible(self, level, bw_hz, sf=None):
        """True when a frame at this level is one this modem can detect.

        The threshold is the receiver's noise floor plus what the spreading
        factor needs above it — so the same frame that a SF12 receiver hears
        comfortably is silence to a SF7 one, which is the trade a spreading
        factor is.
        """
        if level is None:
            return False
        return level >= self.noise(bw_hz) + self.sensitivity(sf)

    def levels(self, sid, freq_hz, bw_hz=125_000, power_dbm=None, sf=None):
        """What every other placed station would hear from `sid`, as sid -> dBm.

        Only the ones actually within earshot: the rest are what "no link"
        means here, and saying so is the point of asking.

        The power is the one that station last transmitted at, so the answer
        describes the station as it is rather than as a constant assumed it
        would be. A station that has never transmitted has no such figure and
        is asked about at the default.
        """
        station = self.stations.get(sid)
        if power_dbm is None:
            power_dbm = station.power_dbm if station else DEFAULT_POWER_DBM
        if sf is None and station is not None:
            state = station.state(0)
            sf = state.get("sf") if state else None
        heard = {}
        for other in self.places:
            if other == sid:
                continue
            level = self.level(sid, other, freq_hz, power_dbm)
            if self.audible(level, bw_hz, sf):
                heard[other] = level
        return heard

    # ---- events ---------------------------------------------------------

    def raise_event(self, callback, *args):
        """Hand one event to a subscriber; a broken subscriber is not the medium's
        problem, so it is logged and the frame carries on."""
        if callback is None:
            return
        try:
            callback(*args)
        except Exception as err:                # noqa: BLE001 - see docstring
            log("subscriber raised %r" % (err,))

    # ---- plumbing ------------------------------------------------------

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        try:
            msg = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self.write_record("in", None, {"raw": repr(data[:200])})
            return
        if not isinstance(msg, dict):
            return
        sid = msg.get("sid")
        self.write_record("in", sid, msg)
        kind = msg.get("type")
        if not isinstance(sid, int):
            return
        if kind == "hello":
            self.recv_hello(sid, addr, msg)
        elif kind == "state":
            self.recv_state(sid, addr, msg)
        elif kind == "tx":
            self.recv_tx(sid, addr, msg)
        # Anything else is not ours to understand.

    def send(self, sid, msg):
        """Send one message to a station, at the address it last wrote from."""
        station = self.stations.get(sid)
        if station is None or self.transport is None:
            return
        self.write_record("out", sid, msg)
        self.transport.sendto(json.dumps(msg).encode("utf-8"), station.addr)

    def write_record(self, direction, sid, msg):
        if self.record is None:
            return
        self.record.write("%s\t%s\t%s\t%s\n" % (
            wall_stamp(), direction, "-" if sid is None else sid,
            json.dumps(msg, separators=(",", ":"), sort_keys=True)))

    # ---- station messages ----------------------------------------------

    def station_for(self, sid, addr, slots=None):
        """The station record for `sid`, created on first sight."""
        station = self.stations.get(sid)
        if station is None:
            station = Station(sid, addr, slots or [0])
            self.stations[sid] = station
            log("station %d joined from %s:%d" % (sid, addr[0], addr[1]))
        else:
            station.addr = addr
            if slots:
                station.slots = slots
        return station

    def recv_hello(self, sid, addr, msg):
        slots = msg.get("slots") or [0]
        self.station_for(sid, addr, slots)
        self.send(sid, {"type": "welcome", "t0": self.now(),
                        "seed": self.seed, "mode": "real"})

    def recv_state(self, sid, addr, msg):
        station = self.station_for(sid, addr)
        slot = msg.get("slot", 0)
        station.states[slot] = msg
        if msg.get("mode") != "RX":
            self.locks.pop((sid, slot), None)   # a receiver that leaves RX lets go
        log("station %d slot %s %s freq=%s bw=%s sf=%s sync=%s" % (
            sid, slot, msg.get("mode"), msg.get("freq"), msg.get("bw"),
            msg.get("sf"), msg.get("sync")))
        self.raise_event(self.on_station, sid, msg)

    def recv_tx(self, sid, addr, msg):
        station = self.station_for(sid, addr)
        start = self.now()
        t0 = msg.get("t0", 0)
        span = max(0, min(int(msg.get("t_end", t0)) - int(t0), MAX_FRAME_US))
        pre = max(0, min(int(msg.get("t_pre", t0)) - int(t0), span))
        hdr = max(pre, min(int(msg.get("t_hdr", t0)) - int(t0), span))
        self.next_eid += 1
        frame = Frame(self.next_eid, msg.get("id"), sid, msg, start, start + span,
                      start + pre, start + hdr)

        self.prune(start)
        for other in [f for f in self.frames if f.overlaps(frame)]:
            frame.interferers.append(other)
            other.interferers.append(frame)
            log("frame %d from %d shares air with frame %d from %d on %s Hz" % (
                frame.eid, sid, other.eid, other.sid, frame.freq))
        self.frames.append(frame)
        station.tx_until = frame.end_us
        station.power_dbm = frame.power_dbm
        self.raise_event(self.on_tx, sid, frame.eid, frame.freq,
                         frame.start_us, frame.end_us)

        for rsid, rstation in self.stations.items():
            if rsid == sid:
                continue
            if rstation.tx_until > start:
                continue        # half duplex: its own frame is still going out
            level = self.level(sid, rsid, frame.freq, frame.power_dbm)
            if not self.audible(level, frame.bw, frame.sf):
                continue        # too far below this receiver's noise to exist
            for slot in rstation.slots:
                if not rstation.listening(slot):
                    continue
                if not self.matches(rstation.state(slot), msg):
                    continue
                begin = {"type": "rx_begin", "slot": slot,
                         "id": frame.eid, "t0": frame.start_us,
                         "t_pre": frame.pre_us, "t_hdr": frame.hdr_us,
                         "t_end": frame.end_us, "level": round(level)}
                if rstation.sensing(slot):
                    # Energy for a channel activity detection, not a
                    # reception: no end is scheduled, so nothing is ruled on
                    # and the map draws no reception for it.
                    self.send(rsid, dict(begin, cad=True))
                    continue
                begin["takes"] = self.takes_receiver(rsid, slot, frame, level, start)
                frame.receivers.append((rsid, slot, level))
                self.send(rsid, begin)
                self.loop.call_later(span / 1_000_000.0,
                                     self.deliver_end, frame, rsid, slot, level)

        log("frame %d (station's %s) from %d: %d us on %s Hz sf%s -> %s" % (
            frame.eid, frame.fid, sid, span, frame.freq, frame.sf,
            ["%d@%.1fdBm" % (r[0], r[2]) for r in frame.receivers] or "nobody"))

    @staticmethod
    def matches(state, tx):
        """True when a receiver's stated radio can hear this transmission."""
        return (all(state.get(k) == tx.get(k) for k in MATCH_KEYS)
                and same_carrier(state.get("freq"), tx.get("freq"), state.get("bw")))

    def takes_receiver(self, rsid, slot, frame, level, now):
        """Whether this frame takes a receiver, which then follows it.

        A demodulator follows one frame at a time. A receiver following nothing
        takes the frame that reaches it; one already following a frame keeps it
        unless the new frame would win the pair, by the rule the verdict
        applies. The ether says so in the `rx_begin`, because it is the ether
        that rules on which of the two survives: a chip deciding at a margin of
        its own would hand up a frame the medium had spoiled, or drop one it had
        kept.
        """
        held = self.locks.get((rsid, slot))
        if held is not None and held[0].end_us > now:
            if self.physics.capture_model == "bench":
                takes = self.bench_pair(held[0], frame, rsid, held[1] - level)[1]
            else:
                takes = level - held[1] >= self.physics.capture_db
            if not takes:
                return False
        self.locks[(rsid, slot)] = (frame, level)
        return True

    def bench_pair(self, first, second, rsid, lead):
        """`bench_outcome` for two frames in flight: whether the receiver was
        locked on the first when the second started after its preamble."""
        locked = (second.start_us >= first.pre_us
                  and any(r[0] == rsid for r in first.receivers))
        return bench_outcome(self.seed, first.eid, second.eid, rsid, lead, locked)

    # ---- delivery -------------------------------------------------------

    def verdict_for(self, frame, rsid, level):
        """How this frame ends at one receiver, given what else was in the air.

        Everything this station could hear on the carrier at the same instant
        is interference. The frame survives only by leading all of it by the
        capture margin — so a receiver near one transmitter keeps its frame
        while a receiver that hears both equally keeps neither. With
        `capture_model: bench` it must instead survive each pair as the bench
        saw it (`bench_outcome`).
        """
        for other in frame.interferers:
            against = self.level(other.sid, rsid, other.freq, other.power_dbm)
            if not self.audible(against, other.bw, other.sf):
                continue        # this receiver never heard the other frame
            apart = CROCE_SIR_DB.get(frame.sf, {}).get(other.sf)
            if self.physics.sf_orthogonality == "croce" and apart is not None:
                if level - against < apart:
                    return "crc"
                continue        # another spreading factor: its table, not capture
            if self.physics.capture_model == "bench":
                if (other.start_us, other.eid) < (frame.start_us, frame.eid):
                    survives = self.bench_pair(other, frame, rsid, against - level)[1]
                else:
                    survives = self.bench_pair(frame, other, rsid, level - against)[0]
                if not survives:
                    return "crc"
            elif level - against < self.physics.capture_db:
                return "crc"
        return "clean"

    def deliver_end(self, frame, rsid, slot, level):
        """Close out one receiver's reception of a frame, at its stated end."""
        verdict = self.verdict_for(frame, rsid, level)
        if verdict == "clean" and self.physics.crc_band_db:
            margin = level - (self.noise(frame.bw) + self.sensitivity(frame.sf))
            if crc_band_fails(self.seed, frame.eid, rsid, margin, self.physics.crc_band_db):
                verdict = "crc"
        self.send(rsid, {"type": "rx_end", "slot": slot, "id": frame.eid,
                         "t": self.now(), "verdict": verdict,
                         "payload": frame.payload, "rssi": round(level),
                         "snr": round(level - self.noise(frame.bw))})
        log("frame %d from %d -> station %d slot %s: %s at %.1f dBm" % (
            frame.eid, frame.sid, rsid, slot, verdict, level))
        self.raise_event(self.on_rx, rsid, frame.sid, frame.eid, verdict, level)

    def prune(self, now):
        """Drop frames whose air is long gone; collisions can no longer touch them."""
        self.frames = [f for f in self.frames if f.end_us > now]

    def close(self):
        if self.transport is not None:
            self.transport.close()
        if self.record is not None:
            self.record.close()


def parse_bind(text):
    """`host:port` into a tuple, with a bare port allowed."""
    if ":" in text:
        host, _, port = text.rpartition(":")
        return (host or "127.0.0.1", int(port))
    return ("127.0.0.1", int(text))


def read_scenario(path):
    """A scenario file's physics, placements, obstructions and links, for a run alone.

    `path` is a `scenario.yaml` or the directory holding one. Positions are
    latitude and longitude in the file and metres by the time they are placed,
    which is the one conversion the ether does on the way in.
    """
    import yaml                 # only a standalone run reads a file

    if os.path.isdir(path):
        path = os.path.join(path, "scenario.yaml")
    with open(path, encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    origin = tuple(data.get("origin") or (0.0, 0.0))
    physics = Physics.from_dict(data.get("physics"))
    places, names = {}, {}
    for name, node in (data.get("nodes") or {}).items():
        sid = int(node["id"])
        lat, lon = (node.get("pos") or (0.0, 0.0))[:2]
        x, y = project(origin, float(lat), float(lon))
        places[sid] = (x, y, float(node.get("gain_db", 0.0)))
        names[name] = sid
    obstructions = []
    for item in data.get("obstructions") or []:
        a, b = item["between"]
        if a in names and b in names:
            obstructions.append((names[a], names[b], float(item.get("db", 0))))
    links = []
    for item in data.get("links") or []:
        a, b = item["between"]
        if a in names and b in names:
            links.append((names[a], names[b], float(item["loss_db"])))
    return physics, places, obstructions, links


async def serve(bind, record_path, physics, places, obstructions, seed=None,
                links=()):
    loop = asyncio.get_running_loop()
    transport, ether = await loop.create_datagram_endpoint(
        lambda: Ether(record_path, physics, seed), local_addr=bind)
    for sid, (x, y, gain) in places.items():
        ether.place(sid, x, y, gain)
    for a, b, db in obstructions:
        ether.obstruct(a, b, db)
    for a, b, loss in links:
        ether.link(a, b, loss)
    host, port = transport.get_extra_info("sockname")[:2]
    log("ether listening on %s:%d" % (host, port))
    log("recording to %s" % record_path)
    log("physics: %s" % physics.describe())
    log("stations placed: %s" % (", ".join(
        "%d at (%.0f, %.0f) m" % (sid, x, y) for sid, (x, y, _) in
        sorted(places.items())) or "none — nothing can hear anything"))
    stop = loop.create_future()
    try:
        await stop
    finally:
        ether.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description="the virtual ether")
    ap.add_argument("--bind", default="127.0.0.1:7000",
                    help="host:port to listen on (default 127.0.0.1:7000)")
    ap.add_argument("--record", default="record.tsv",
                    help="record file (default record.tsv in the current directory)")
    ap.add_argument("--scenario", metavar="PATH",
                    help="a scenario directory or scenario.yaml: where the "
                         "stations stand (default: nowhere, so nothing is heard)")
    ap.add_argument("--seed", type=int, default=None,
                    help="the seed of the medium's draws (default: a fresh one)")
    args = ap.parse_args(argv)
    physics, places, obstructions, links = Physics(), {}, [], []
    if args.scenario:
        physics, places, obstructions, links = read_scenario(args.scenario)
    try:
        asyncio.run(serve(parse_bind(args.bind), args.record,
                          physics, places, obstructions, args.seed, links))
    except KeyboardInterrupt:
        log("ether stopping")
    return 0


if __name__ == "__main__":
    sys.exit(main())
