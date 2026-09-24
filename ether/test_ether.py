"""Fake stations on real UDP sockets, against the ether as a child process.

Stations are placed rather than linked: every test says where its stations
stand, in metres, and the levels asserted below are the path loss those
positions give. `expected_level` computes the same figure the ether does, so
a test reads as geometry and not as a table of numbers somebody tuned.
"""

import base64
import json
import math
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ETHER = os.path.join(HERE, "ether.py")
sys.path.insert(0, HERE)

import ether as ether_module     # noqa: E402 - the path is set just above

FREQ = 868_100_000
BW = 125_000
SF = 9
SYNC = 0x34
POWER_DBM = 14

# A frame long enough that begin and end are plainly separate events, short
# enough that the tests stay quick.
FRAME_US = 300_000

# Distances that keep the arithmetic legible: at the default exponent of 2.7
# a kilometre costs 27 dB more than the first metre, and doubling it costs
# 8.1 dB more again — which is what puts one frame over another.
NEAR_M = 100
FAR_M = 1000

# Enough dB to put a pair a couple of kilometres apart out of earshot entirely
# (a frame 30 dB under the noise is never delivered).
WALL_DB = 60


def expected_level(distance_m, obstruction_db=0.0, gain_db=0.0, power_dbm=POWER_DBM):
    """The level the ether will compute for this geometry, in dBm."""
    physics = ether_module.Physics()
    distance_m = max(distance_m, ether_module.MIN_DISTANCE_M)
    loss = (ether_module.fspl_1m_db(FREQ)
            + 10.0 * physics.exponent * math.log10(distance_m)
            + obstruction_db)
    return power_dbm + gain_db - loss


def expected_snr(level):
    """What the ether reports as SNR: the level above the receiver's noise."""
    noise = -174.0 + 10.0 * math.log10(BW) + ether_module.DEFAULT_NOISE_FIGURE_DB
    return round(level - noise)


def radio(mode="RX", **over):
    """The radio fields shared by a state and a transmission."""
    fields = {"mode": mode, "ready_at": 0, "freq": FREQ, "bw": BW, "sf": SF,
              "cr": 5, "sync": SYNC, "hdr": "explicit", "crc": True, "pre": 8}
    fields.update(over)
    return fields


class FakeStation:
    """One station's UDP socket, its clock and its inbox.

    The ether's address is resolved on the first datagram rather than at
    construction, because the ether does not exist until every station in the
    test has been placed — see `Bench`.
    """

    def __init__(self, sid, bench):
        self.sid = sid
        self.bench = bench
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.t = sid * 1_000_000      # each station's own clock, its own origin

    @property
    def ether(self):
        return ("127.0.0.1", self.bench.ensure_started())

    def close(self):
        self.sock.close()

    def send(self, msg):
        msg = dict(msg, sid=self.sid, t=self.t)
        self.sock.sendto(json.dumps(msg).encode(), self.ether)

    def hello(self):
        self.send({"type": "hello", "slots": [0]})
        return self.expect("welcome")

    def state(self, mode="RX", **over):
        self.send(dict({"type": "state", "slot": 0}, **radio(mode, **over)))

    def tx(self, fid, payload=b"hello", span_us=FRAME_US, **over):
        t0 = self.t
        self.send(dict({"type": "tx", "slot": 0, "id": fid, "t0": t0,
                        "t_pre": t0 + span_us // 10,
                        "t_hdr": t0 + span_us // 5,
                        "t_end": t0 + span_us, "power_dbm": POWER_DBM,
                        "payload": base64.b64encode(payload).decode()},
                       **radio("TX", **over)))

    def recv(self, timeout=2.0):
        self.sock.settimeout(timeout)
        try:
            data, _ = self.sock.recvfrom(65535)
        except socket.timeout:
            return None
        return json.loads(data.decode())

    def expect(self, kind, timeout=2.0):
        """The next message of this type, ignoring anything else."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            msg = self.recv(max(0.05, deadline - time.monotonic()))
            if msg is None:
                break
            if msg.get("type") == kind:
                return msg
        raise AssertionError("station %d never saw %s" % (self.sid, kind))

    def expect_nothing(self, timeout=0.5):
        assert self.recv(timeout) is None

    def ends(self, count, timeout=2.0):
        """The next `count` receptions to close, keyed by the bytes they carry.

        A frame's id belongs to the ether, not to the station that sent it, so
        a test that has two frames in the air tells them apart by what is in
        them.
        """
        closed = {}
        for _ in range(count):
            end = self.expect("rx_end", timeout=timeout)
            closed[base64.b64decode(end["payload"])] = end
        return closed


class Bench:
    """The ether as a child process, and the stations that talk to it.

    Calling it makes a station and places it: `ether(1, x_m)` puts station 1
    that many metres east of the origin. The ether starts on the first
    datagram any of them sends, so every position and obstruction a test wants
    is stated before then — which is also how a scenario file works.
    """

    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.record = tmp_path / "record.tsv"
        self.proc = None
        self.port = None
        self.stations = []
        self.places = {}            # sid -> (x_m, y_m, gain_db)
        self.walls = []             # (sid, sid, dB)
        self.physics = {}           # the scenario's `physics:`, when a test states it
        self.seed = None            # the ether's --seed, when a test fixes it
        self.links = []             # (sid, sid, dB): a pair's path loss, stated

    def place(self, sid, x_m, y_m=0.0, gain_db=0.0):
        assert self.proc is None, "the ether is already running"
        self.places[sid] = (float(x_m), float(y_m), float(gain_db))

    def obstruct(self, a, b, db):
        assert self.proc is None, "the ether is already running"
        self.walls.append((a, b, float(db)))

    def link(self, a, b, loss_db):
        assert self.proc is None, "the ether is already running"
        self.links.append((a, b, float(loss_db)))

    def write_scenario(self):
        """The placements as a scenario file, which is how the ether reads them.

        Positions go in as latitude and longitude, so the projection the ether
        does on the way in is exercised by every test rather than bypassed.
        """
        nodes = []
        for sid, (x_m, y_m, gain_db) in sorted(self.places.items()):
            lat, lon = ether_module.unproject((0.0, 0.0), x_m, y_m)
            nodes.append("  n%d: { id: %d, pos: [%.12f, %.12f], gain_db: %g }"
                         % (sid, sid, lat, lon, gain_db))
        walls = ["  - { between: [n%d, n%d], db: %g }" % (a, b, db)
                 for a, b, db in self.walls]
        text = "origin: [0.0, 0.0]\n"
        if self.physics:
            text += "physics: { %s }\n" % ", ".join(
                "%s: %s" % (key, value) for key, value in self.physics.items())
        text += "nodes:\n" + "\n".join(nodes) + "\n"
        if walls:
            text += "obstructions:\n" + "\n".join(walls) + "\n"
        if self.links:
            text += "links:\n" + "\n".join(
                "  - { between: [n%d, n%d], loss_db: %g }" % link
                for link in self.links) + "\n"
        path = self.tmp_path / "scenario.yaml"
        path.write_text(text)
        return path

    def ensure_started(self):
        """The ether's port, starting it on the first station to speak."""
        if self.proc is None:
            self.start()
        return self.port

    def start(self):
        argv = [sys.executable, ETHER, "--bind", "127.0.0.1:0",
                "--record", str(self.record)]
        if self.places:
            argv += ["--scenario", str(self.write_scenario())]
        if self.seed is not None:
            argv += ["--seed", str(self.seed)]
        self.proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE, text=True)
        lines = queue.Queue()

        def drain():
            for line in self.proc.stderr:
                lines.put(line)
            lines.put(None)

        threading.Thread(target=drain, daemon=True).start()

        deadline = time.monotonic() + 10
        while self.port is None and time.monotonic() < deadline:
            line = lines.get(timeout=10)
            if line is None:
                break
            found = re.search(r"listening on 127\.0\.0\.1:(\d+)", line)
            if found:
                self.port = int(found.group(1))
        assert self.port, "the ether never reported a listening port"

    def __call__(self, sid, x_m=None, y_m=0.0, gain_db=0.0):
        if x_m is not None:
            self.place(sid, x_m, y_m, gain_db)
        elif sid not in self.places:
            self.place(sid, sid * NEAR_M, 0.0, 0.0)   # a row, one step apart
        station = FakeStation(sid, self)
        self.stations.append(station)
        return station

    def close(self):
        for station in self.stations:
            station.close()
        if self.proc is not None:
            self.proc.terminate()
            self.proc.wait(timeout=5)


@pytest.fixture
def ether(tmp_path):
    """The ether on an ephemeral port, its record in the test's directory."""
    bed = Bench(tmp_path)
    try:
        yield bed
    finally:
        bed.close()


def test_hello_gets_a_welcome(ether):
    welcome = ether(1, 0).hello()
    assert welcome["mode"] == "real"
    assert isinstance(welcome["t0"], int)
    assert isinstance(welcome["seed"], int)


def test_frame_reaches_a_listening_station(ether):
    sender, receiver = ether(1, 0), ether(2, NEAR_M)
    sender.hello()
    receiver.hello()
    receiver.state("RX")
    time.sleep(0.1)

    level = expected_level(NEAR_M)
    sent_at = time.monotonic()
    sender.tx(7, payload=b"over the air")

    begin = receiver.expect("rx_begin")
    assert time.monotonic() - sent_at < 0.2, "rx_begin must arrive at once"
    assert begin["slot"] == 0
    assert begin["level"] == round(level)
    assert begin["t_end"] - begin["t0"] == FRAME_US
    assert begin["t_pre"] - begin["t0"] == FRAME_US // 10
    assert begin["t_hdr"] - begin["t0"] == FRAME_US // 5

    end = receiver.expect("rx_end")
    elapsed = time.monotonic() - sent_at
    assert FRAME_US / 1e6 - 0.05 < elapsed < FRAME_US / 1e6 + 0.3, elapsed
    assert end["id"] == begin["id"], "one reception, one id from beginning to end"
    assert end["verdict"] == "clean"
    assert base64.b64decode(end["payload"]) == b"over the air"
    assert end["rssi"] == round(level)
    assert end["snr"] == expected_snr(level)

    sender.expect_nothing()      # a transmitter never hears itself


def test_the_level_falls_with_distance(ether):
    """Ten times the distance is ten times the exponent in dB — 27 at 2.7."""
    sender, near, far = ether(1, 0), ether(2, NEAR_M), ether(3, FAR_M)
    for station in (sender, near, far):
        station.hello()
    near.state("RX")
    far.state("RX")
    time.sleep(0.1)

    sender.tx(1)
    near_level = near.expect("rx_begin")["level"]
    far_level = far.expect("rx_begin")["level"]
    assert near_level == round(expected_level(NEAR_M))
    assert far_level == round(expected_level(FAR_M))
    assert near_level - far_level == pytest.approx(27, abs=1)


def test_antenna_gain_lifts_both_ends(ether):
    """Gain is the receiver's as much as the transmitter's: the link has both."""
    sender, plain, tall = ether(1, 0), ether(2, FAR_M), ether(3, -FAR_M, gain_db=9)
    for station in (sender, plain, tall):
        station.hello()
    plain.state("RX")
    tall.state("RX")
    time.sleep(0.1)

    sender.tx(1)
    assert plain.expect("rx_begin")["level"] == round(expected_level(FAR_M))
    assert tall.expect("rx_begin")["level"] == round(expected_level(FAR_M, gain_db=9))


def test_two_stations_at_one_point_are_held_a_metre_apart(ether):
    """Nothing divides by a zero distance; the pair is simply very loud."""
    sender, receiver = ether(1, 0), ether(2, 0)
    sender.hello()
    receiver.hello()
    receiver.state("RX")
    time.sleep(0.1)

    sender.tx(1)
    assert receiver.expect("rx_begin")["level"] == round(expected_level(1.0))


def test_shadowing_is_one_draw_per_pair_the_same_both_ways_and_every_time():
    draw = ether_module.shadowing_unit
    assert draw(3, 1, 2) == draw(3, 2, 1)
    assert draw(3, 1, 2) == draw(3, 1, 2)
    assert draw(3, 1, 2) != draw(3, 1, 3)
    assert draw(3, 1, 2) != draw(4, 1, 2)


def test_shadowing_draws_spread_like_a_standard_normal():
    draws = [ether_module.shadowing_unit(7, a, b)
             for a in range(1, 41) for b in range(a + 1, 41)]
    mean = sum(draws) / len(draws)
    spread = math.sqrt(sum((d - mean) ** 2 for d in draws) / (len(draws) - 1))
    assert abs(mean) < 0.12
    assert spread == pytest.approx(1.0, abs=0.08)


def test_shadowing_moves_a_link_by_its_pairs_draw_times_the_spread(ether):
    ether.physics = {"shadowing_db": 7, "shadowing_seed": 3}
    sender, receiver = ether(1, 0), ether(2, FAR_M)
    sender.hello()
    receiver.hello()
    receiver.state("RX")
    time.sleep(0.1)

    sender.tx(1)
    shadow = 7 * ether_module.shadowing_unit(3, 1, 2)
    assert receiver.expect("rx_begin")["level"] == round(expected_level(FAR_M) - shadow)


def test_the_sender_is_not_a_receiver_and_others_must_match(ether):
    sender, same = ether(1, 0), ether(2, NEAR_M)
    other_freq, sleeping = ether(3, 2 * NEAR_M), ether(4, 3 * NEAR_M)
    for station in (sender, same, other_freq, sleeping):
        station.hello()
    same.state("RX")
    other_freq.state("RX", freq=869_500_000)
    sleeping.state("SLEEP")
    time.sleep(0.1)

    sender.tx(11, payload=b"x")
    assert base64.b64decode(same.expect("rx_end")["payload"]) == b"x"
    other_freq.expect_nothing()
    sleeping.expect_nothing()


def test_wrong_sync_word_is_not_heard(ether):
    sender, receiver = ether(1, 0), ether(2, NEAR_M)
    sender.hello()
    receiver.hello()
    receiver.state("RX", sync=0x12)
    time.sleep(0.1)

    sender.tx(12)
    receiver.expect_nothing()


def test_a_carrier_a_few_register_steps_off_is_the_same_carrier(ether):
    """Two drivers round one frequency to registers tens of hertz apart; a
    receiver hears within a quarter of its bandwidth and not beyond."""
    sender, near_miss, other = ether(1, 0), ether(2, NEAR_M), ether(3, NEAR_M, 50)
    for station in (sender, near_miss, other):
        station.hello()
    near_miss.state("RX", freq=FREQ + 36)
    other.state("RX", freq=FREQ + BW // 4 + 1000)
    time.sleep(0.1)

    sender.tx(13)
    near_miss.expect("rx_begin")
    assert near_miss.expect("rx_end")["verdict"] == "clean"
    other.expect_nothing()


def test_overlapping_frames_both_end_as_crc(ether):
    """Two transmitters the same distance away: neither leads by the margin."""
    first, second, receiver = ether(1, -FAR_M), ether(2, FAR_M), ether(3, 0)
    for station in (first, second, receiver):
        station.hello()
    receiver.state("RX")
    time.sleep(0.1)

    first.tx(21, payload=b"first")
    receiver.expect("rx_begin")
    time.sleep(FRAME_US / 2e6)          # squarely inside the first frame
    second.tx(22, payload=b"second")
    receiver.expect("rx_begin")

    # The bytes of a spoiled frame are still the sender's bytes.
    ends = receiver.ends(2)
    assert set(ends) == {b"first", b"second"}
    assert ends[b"first"]["verdict"] == "crc"
    assert ends[b"second"]["verdict"] == "crc"


def test_frames_that_do_not_overlap_stay_clean(ether):
    sender, receiver = ether(1, 0), ether(2, NEAR_M)
    sender.hello()
    receiver.hello()
    receiver.state("RX")
    time.sleep(0.1)

    sender.tx(31, span_us=100_000)
    receiver.expect("rx_begin")
    assert receiver.expect("rx_end")["verdict"] == "clean"
    sender.tx(32, span_us=100_000)
    receiver.expect("rx_begin")
    assert receiver.expect("rx_end")["verdict"] == "clean"


def test_leaving_rx_before_a_frame_means_it_is_not_heard(ether):
    sender, receiver = ether(1, 0), ether(2, NEAR_M)
    sender.hello()
    receiver.hello()
    receiver.state("RX")
    time.sleep(0.05)
    receiver.state("STDBY_RC")
    time.sleep(0.05)

    sender.tx(41)
    receiver.expect_nothing()


def test_a_station_in_cad_senses_a_frame_but_is_never_told_how_it_ended(ether):
    """CAD is energy, not reception: an rx_begin marked `cad`, and no rx_end,
    so nothing is ruled on and nothing is recorded as received."""
    sender, sensing = ether(1, 0), ether(2, NEAR_M)
    sender.hello()
    sensing.hello()
    sensing.state("CAD")
    time.sleep(0.05)

    sender.tx(43, payload=b"is anyone there")
    begin = sensing.expect("rx_begin")
    assert begin["cad"] is True
    assert begin["level"] == round(expected_level(NEAR_M))
    assert begin["t_end"] - begin["t0"] == FRAME_US
    sensing.expect_nothing(timeout=FRAME_US / 1e6 + 0.3)

    time.sleep(0.2)             # the record is written as the ether goes
    ends = [line for line in ether.record.read_text().splitlines()
            if '"type":"rx_end"' in line]
    assert ends == []


def test_an_obstruction_puts_a_pair_out_of_earshot(ether):
    """The line of three: a wall between the outer pair, and nothing between
    either of them and the station in the middle."""
    ether.obstruct(1, 3, WALL_DB)
    a, b, c = ether(1, 0), ether(2, FAR_M), ether(3, 3 * FAR_M)
    for station in (a, b, c):
        station.hello()
        station.state("RX")
    time.sleep(0.1)

    a.tx(71, payload=b"from a")
    assert b.expect("rx_begin")["level"] == round(expected_level(FAR_M))
    assert b.expect("rx_end")["verdict"] == "clean"
    c.expect_nothing()          # the far end of the line is deaf to a

    c.tx(72, payload=b"from c")
    assert b.expect("rx_begin")["level"] == round(expected_level(2 * FAR_M))
    end = b.expect("rx_end")
    assert end["verdict"] == "clean"
    assert end["rssi"] == round(expected_level(2 * FAR_M))
    a.expect_nothing()          # and a is deaf to c, by the same wall


def test_an_obstruction_works_in_both_directions(ether):
    """A wall is a pair's property, not a direction's: neither end hears the
    other, however the pair was written down."""
    ether.obstruct(2, 1, WALL_DB)
    a, b = ether(1, 0), ether(2, 3 * FAR_M)
    for station in (a, b):
        station.hello()
        station.state("RX")
    time.sleep(0.1)

    a.tx(81)
    b.expect_nothing()
    b.tx(82)
    a.expect_nothing()


def test_a_station_is_deaf_while_its_own_frame_is_going_out(ether):
    """Half duplex: a radio transmitting hears nothing, however loud."""
    a, b = ether(1, 0), ether(2, NEAR_M)
    for station in (a, b):
        station.hello()
        station.state("RX")
    time.sleep(0.1)

    b.tx(61, payload=b"b is talking")
    a.expect("rx_begin")
    a.tx(62, payload=b"and so is a")
    b.expect_nothing()


def test_frames_that_share_a_station_id_are_still_told_apart(ether):
    """Each station numbers its own frames, so a receiver cannot use that
    number: the id in a reception is the ether's, and no two frames share it."""
    a, b, c = ether(1, -NEAR_M), ether(2, 0), ether(3, NEAR_M)
    for station in (a, b, c):
        station.hello()
        station.state("RX")
    time.sleep(0.1)

    a.tx(1, payload=b"from a")
    c.tx(1, payload=b"from c")
    first, second = b.expect("rx_begin"), b.expect("rx_begin")
    assert first["id"] != second["id"]


def test_the_nearer_of_two_concurrent_frames_is_the_one_that_survives(ether):
    """The hidden terminal: a and c cannot hear each other, so both transmit.

    b is a kilometre from a and two from c, which at the default exponent is
    8 dB of lead — over the capture margin, so b keeps a's frame and loses c's.
    """
    ether.obstruct(1, 3, WALL_DB)
    a, b, c = ether(1, 0), ether(2, FAR_M), ether(3, 3 * FAR_M)
    for station in (a, b, c):
        station.hello()
        station.state("RX")
    time.sleep(0.1)

    a.tx(91, payload=b"from a")
    c.tx(92, payload=b"from c")

    ends = b.ends(2)
    assert set(ends) == {b"from a", b"from c"}
    assert ends[b"from a"]["verdict"] == "clean", "a leads c by more than the margin"
    assert ends[b"from c"]["verdict"] == "crc", "c is the one that loses the air"

    a.expect_nothing()
    c.expect_nothing()


def test_two_frames_within_the_margin_spoil_each_other(ether):
    """The same line, with c moved in until its lead is under the margin."""
    ether.obstruct(1, 3, WALL_DB)
    a, b, c = ether(1, 0), ether(2, FAR_M), ether(3, 2 * FAR_M)
    for station in (a, b, c):
        station.hello()
        station.state("RX")
    time.sleep(0.1)

    a.tx(101, payload=b"from a")
    c.tx(102, payload=b"from c")
    ends = b.ends(2)
    assert ends[b"from a"]["verdict"] == "crc"
    assert ends[b"from c"]["verdict"] == "crc"


def test_a_receiver_that_hears_only_one_of_two_colliding_frames_keeps_it(ether):
    """The collision is at b; d is walled off from the station that spoils it."""
    ether.obstruct(1, 3, WALL_DB)
    ether.obstruct(1, 4, WALL_DB)
    a, b = ether(1, 0), ether(2, FAR_M)
    c, d = ether(3, 2 * FAR_M), ether(4, 3 * FAR_M)
    for station in (a, b, c, d):
        station.hello()
        station.state("RX")
    time.sleep(0.1)

    a.tx(111, payload=b"from a")
    c.tx(112, payload=b"from c")

    ends = b.ends(2)             # equidistant from both, so it keeps neither
    assert ends[b"from a"]["verdict"] == "crc"
    assert ends[b"from c"]["verdict"] == "crc"

    end = d.expect("rx_end")     # d heard only c, and cleanly
    assert base64.b64decode(end["payload"]) == b"from c"
    assert end["verdict"] == "clean"


def test_a_frame_that_finds_its_receiver_idle_takes_it(ether):
    sender, receiver = ether(1, 0), ether(2, NEAR_M)
    sender.hello()
    receiver.hello()
    receiver.state("RX")
    time.sleep(0.1)

    sender.tx(121)
    assert receiver.expect("rx_begin")["takes"] is True


def collide_at(ether, a_m, c_m):
    """b at the origin follows a's frame when c's lands on it; both rx_begins."""
    a, b, c = ether(1, a_m), ether(2, 0), ether(3, c_m)
    for station in (a, b, c):
        station.hello()
        station.state("RX")
    time.sleep(0.1)

    a.tx(131, payload=b"from a")
    first = b.expect("rx_begin")
    time.sleep(FRAME_US / 4e6)          # squarely inside a's frame
    c.tx(132, payload=b"from c")
    return first, b.expect("rx_begin")


def test_a_later_frame_takes_a_busy_receiver_past_the_capture_margin(ether):
    """Half the distance is 8 dB at the default exponent: over the margin."""
    first, second = collide_at(ether, 2 * FAR_M, -FAR_M)
    assert first["takes"] is True
    assert second["takes"] is True


def test_a_later_frame_under_the_capture_margin_does_not_take_it(ether):
    """1.4 km against 2 km is 4 dB at the default exponent: under it."""
    first, second = collide_at(ether, 2 * FAR_M, -1.4 * FAR_M)
    assert first["takes"] is True
    assert second["takes"] is False


def test_the_receiver_is_told_the_scenarios_margin_not_a_fixed_one(ether):
    """The same 4 dB takes the receiver when the scenario's margin is 3 dB."""
    ether.physics = {"capture_db": 3}
    first, second = collide_at(ether, 2 * FAR_M, -1.4 * FAR_M)
    assert second["takes"] is True


def test_a_receiver_that_leaves_rx_lets_go_of_the_frame_it_followed(ether):
    a, b, c = ether(1, 2 * FAR_M), ether(2, 0), ether(3, -1.4 * FAR_M)
    for station in (a, b, c):
        station.hello()
        station.state("RX")
    time.sleep(0.1)

    a.tx(141, payload=b"from a")
    assert b.expect("rx_begin")["takes"] is True
    b.state("STDBY_RC")
    time.sleep(0.02)
    b.state("RX")
    time.sleep(0.02)
    c.tx(142, payload=b"from c")
    assert b.expect("rx_begin")["takes"] is True


def outcomes(lead, locked=False, pairs=3000):
    """bench_outcome over many pairs of frames at one receiver."""
    return [ether_module.bench_outcome(11, 2 * n + 1, 2 * n + 2, 5, lead, locked)
            for n in range(pairs)]


def test_bench_equals_are_both_lost_one_time_in_four_and_never_both_kept():
    seen = outcomes(0.5)
    assert (True, True) not in seen
    both_lost = seen.count((False, False)) / len(seen)
    assert both_lost == pytest.approx(9 / 39, abs=0.03)
    first = seen.count((True, False)) / (len(seen) - seen.count((False, False)))
    assert first == pytest.approx(0.5, abs=0.04)


def test_bench_keeps_the_stronger_nine_times_in_ten_at_2_db_and_never_the_weaker():
    seen = outcomes(2.0)
    assert all(second is False for _, second in seen)
    kept = sum(first for first, _ in seen) / len(seen)
    assert kept == pytest.approx(119 / 136, abs=0.025)
    assert set(outcomes(-2.0)) <= {(False, True), (False, False)}


def test_bench_keeps_the_stronger_every_time_from_6_1_db():
    assert set(outcomes(6.1, pairs=500)) == {(True, False)}
    assert set(outcomes(-7.0, pairs=500)) == {(False, True)}


def test_bench_a_late_frame_is_never_received_and_a_stronger_one_spoils_both():
    assert set(outcomes(-2.0, locked=True, pairs=500)) == {(False, False)}
    assert set(outcomes(7.0, locked=True, pairs=500)) == {(True, False)}
    assert all(second is False for _, second in outcomes(0.0, locked=True))


def test_bench_capture_keeps_a_frame_8_db_up_as_the_margin_does(ether):
    """The hidden terminal from before, judged as the bench saw it."""
    ether.physics = {"capture_model": "bench"}
    ether.obstruct(1, 3, WALL_DB)
    a, b, c = ether(1, 0), ether(2, FAR_M), ether(3, 3 * FAR_M)
    for station in (a, b, c):
        station.hello()
        station.state("RX")
    time.sleep(0.1)

    a.tx(151, payload=b"from a")
    c.tx(152, payload=b"from c")
    ends = b.ends(2)
    assert ends[b"from a"]["verdict"] == "clean"
    assert ends[b"from c"]["verdict"] == "crc"


def test_bench_capture_a_stronger_frame_after_the_preamble_spoils_both(ether):
    """b follows a's frame; c's lands after its preamble, 8 dB louder."""
    ether.physics = {"capture_model": "bench"}
    a, b, c = ether(1, 2 * FAR_M), ether(2, 0), ether(3, -FAR_M)
    for station in (a, b, c):
        station.hello()
        station.state("RX")
    time.sleep(0.1)

    a.tx(161, payload=b"from a")
    assert b.expect("rx_begin")["takes"] is True
    time.sleep(FRAME_US / 4e6)          # past a's preamble, a tenth of the frame
    c.tx(162, payload=b"from c")
    assert b.expect("rx_begin")["takes"] is False
    ends = b.ends(2)
    assert ends[b"from a"]["verdict"] == "crc"
    assert ends[b"from c"]["verdict"] == "crc"


def test_bench_capture_a_weaker_frame_after_the_preamble_leaves_the_first(ether):
    """b follows a's frame; c's lands after its preamble, 8 dB quieter."""
    ether.physics = {"capture_model": "bench"}
    a, b, c = ether(1, FAR_M), ether(2, 0), ether(3, -2 * FAR_M)
    for station in (a, b, c):
        station.hello()
        station.state("RX")
    time.sleep(0.1)

    a.tx(171, payload=b"from a")
    b.expect("rx_begin")
    time.sleep(FRAME_US / 4e6)
    c.tx(172, payload=b"from c")
    assert b.expect("rx_begin")["takes"] is False
    ends = b.ends(2)
    assert ends[b"from a"]["verdict"] == "clean"
    assert ends[b"from c"]["verdict"] == "crc"


def test_a_link_states_a_pairs_loss_whatever_the_distance(ether):
    """Ten kilometres apart and heard as the stated 110 dB, not the 139 dB
    the distance would cost."""
    ether.link(1, 2, 110)
    sender, receiver = ether(1, 0), ether(2, 10 * FAR_M)
    sender.hello()
    receiver.hello()
    receiver.state("RX")
    time.sleep(0.1)

    sender.tx(181)
    assert receiver.expect("rx_begin")["level"] == POWER_DBM - 110


def test_a_link_is_the_same_both_ways_and_an_obstruction_still_adds(ether):
    ether.link(1, 2, 110)
    ether.obstruct(1, 2, 10)
    a, b = ether(1, 0), ether(2, NEAR_M)
    for station in (a, b):
        station.hello()
        station.state("RX")
    time.sleep(0.1)

    b.tx(191)
    assert a.expect("rx_begin")["level"] == POWER_DBM - 120


def other_sf_pair(ether, sf_a, sf_b, a_m, b_m, orthogonality):
    """a at SF a and b at SF b transmit together; one listener per SF."""
    if orthogonality:
        ether.physics = {"sf_orthogonality": "croce"}
    ether.obstruct(1, 2, WALL_DB)
    a, b = ether(1, a_m), ether(2, b_m)
    ra, rb = ether(3, 0), ether(4, 0)
    for station in (a, b, ra, rb):
        station.hello()
    ra.state("RX", sf=sf_a)
    rb.state("RX", sf=sf_b)
    time.sleep(0.1)
    a.tx(201, payload=b"from a", sf=sf_a)
    b.tx(202, payload=b"from b", sf=sf_b)
    return ra.expect("rx_end")["verdict"], rb.expect("rx_end")["verdict"]


def test_frames_at_two_spreading_factors_spoil_each_other_by_default(ether):
    assert other_sf_pair(ether, 9, 10, FAR_M, -FAR_M, False) == ("crc", "crc")


def test_croces_table_lets_frames_at_two_spreading_factors_both_through(ether):
    """Equal levels: SF9 under SF10 by 0 dB, well inside its -13 dB."""
    assert other_sf_pair(ether, 9, 10, FAR_M, -FAR_M, True) == ("clean", "clean")


def test_croces_table_still_spoils_a_frame_far_under_the_other(ether):
    """The SF10 sender is 16 times nearer: 32.5 dB up at the listeners,
    past what either table entry allows."""
    assert other_sf_pair(ether, 9, 10, 16 * NEAR_M, -NEAR_M, True) == ("crc", "clean")


def test_the_crc_band_fails_frames_in_proportion_to_how_near_the_threshold_they_are():
    fails = ether_module.crc_band_fails
    for margin, expected in ((0.0, 1.0), (1.0, 2 / 3), (2.0, 1 / 3), (3.0, 0.0)):
        share = sum(fails(7, eid, 3, margin, 3.0) for eid in range(4000)) / 4000
        assert share == pytest.approx(expected, abs=0.03), margin
    assert not any(fails(7, eid, 3, 0.5, 0.0) for eid in range(100))


def test_the_crc_band_verdict_is_the_draw_for_that_frame_and_receiver(ether):
    """With the seed fixed, the verdict is exactly what crc_band_fails says."""
    ether.physics = {"crc_band_db": 60}
    ether.seed = 5
    sender, receiver = ether(1, 0), ether(2, FAR_M)
    sender.hello()
    receiver.hello()
    receiver.state("RX")
    time.sleep(0.1)
    sender.tx(211)
    margin = expected_level(FAR_M) - (-174 + 10 * math.log10(BW)
                                      + ether_module.DEFAULT_NOISE_FIGURE_DB
                                      + ether_module.SENSITIVITY_DB[SF])
    failed = ether_module.crc_band_fails(5, 1, 2, margin, 60)
    assert receiver.expect("rx_end")["verdict"] == ("crc" if failed else "clean")


def test_a_station_that_was_never_placed_hears_nothing(ether):
    """Position is the whole of a station's presence in the medium: one that
    has none is not on the plane, and no distance to it exists."""
    sender = ether(1, 0)
    stray = FakeStation(9, ether)        # a station id no scenario ever placed
    ether.stations.append(stray)
    sender.hello()
    stray.hello()
    stray.state("RX")
    time.sleep(0.1)

    sender.tx(121)
    stray.expect_nothing()


def test_unknown_messages_are_ignored(ether):
    sender, receiver = ether(1, 0), ether(2, NEAR_M)
    sender.hello()
    receiver.hello()
    receiver.state("RX")
    sender.send({"type": "wait", "until": 5})
    sender.sock.sendto(b"not json at all", sender.ether)
    time.sleep(0.1)

    sender.tx(51, payload=b"still working")
    assert receiver.expect("rx_begin")["level"] == round(expected_level(NEAR_M))
    assert base64.b64decode(receiver.expect("rx_end")["payload"]) == b"still working"


def test_the_record_holds_every_message(ether):
    sender, receiver = ether(1, 0), ether(2, NEAR_M)
    sender.hello()
    receiver.hello()
    receiver.state("RX")
    time.sleep(0.1)
    sender.tx(61, payload=b"recorded")
    receiver.expect("rx_begin")
    receiver.expect("rx_end")
    time.sleep(0.2)

    lines = [l for l in ether.record.read_text().splitlines() if not l.startswith("#")]
    rows = [l.split("\t") for l in lines]
    assert all(len(r) == 4 for r in rows)
    kinds = [(r[1], r[2], json.loads(r[3])["type"]) for r in rows]
    assert ("in", "1", "hello") in kinds
    assert ("out", "1", "welcome") in kinds
    assert ("in", "2", "state") in kinds
    assert ("in", "1", "tx") in kinds
    assert ("out", "2", "rx_begin") in kinds
    assert ("out", "2", "rx_end") in kinds
