#!/usr/bin/env python3
"""The referee: what the record says each station did on the air, judged.

The ether decides who hears what and keeps no opinion about how a station
behaved. This reads the record it wrote (`run/record.tsv`) and the scenario it
ran (`run/scenario.yaml`) afterwards and says:

- **duty**: each station's airtime per sub-band, against the budget the band
  allows, over the busiest window of the run;
- **carrier sense**: every transmission that started while a frame the sender
  could hear was on the air, split by whether the ether had told the sender
  about that frame (it tells a station only at a frame's start, and only if it
  was listening then) and by how long the frame had been on the air;
- **collisions**: receptions spoiled, and which of them had a hidden sender;
- **the unheard**: frames nobody received, grouped by where they were sent,
  which is how a station on the wrong channel shows up.

The levels are the ether's own, recomputed from the scenario with ether.py, so
the referee judges the same air the run had.
"""

import argparse
import asyncio
import bisect
import collections
import json
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "ether"))
import ether as ether_module     # noqa: E402 - the path is set just above

# The sub-bands a duty budget is kept against: ERC/REC 70-03 Annex 1 as the
# reticulum project's band plan reads it (reticulum-rnode-proto, `SRD_BANDS`).
# (low Hz, high Hz, budget in permille per hour, name)
BANDS = [
    (433_050_000, 434_790_000, 1000, "433"),
    (863_000_000, 868_000_000, 10, "g"),
    (868_000_000, 868_600_000, 10, "g1"),
    (868_600_000, 869_200_000, 1, "g2"),
    (869_200_000, 869_400_000, 1, "g2/alarm"),
    (869_400_000, 869_650_000, 100, "g3"),
    (869_650_000, 869_700_000, 100, "g3 upper"),
    (869_700_000, 870_000_000, 10, "g4"),
]

# A frame on the air for less than this cannot be sensed yet: a LoRa receiver
# needs a few symbols of preamble. The bench measured about 4 ms at SF7.
BLIND_S = 0.004


def band_of(freq):
    for lo, hi, permille, name in BANDS:
        if freq is not None and lo <= freq < hi:
            return name, permille
    return "outside", None


def stamp(text):
    return datetime.fromisoformat(text).timestamp()


class Frame:
    __slots__ = ("sid", "fid", "start", "pre", "end", "freq", "bw", "sf", "sync",
                 "power", "size", "told", "heard", "eid")

    def __init__(self, sid, msg, start):
        self.sid = sid
        self.fid = msg.get("id")
        span = max(0, int(msg.get("t_end", 0)) - int(msg.get("t0", 0))) / 1e6
        self.start = start
        self.end = start + span
        pre = (int(msg.get("t_pre", msg.get("t0", 0))) - int(msg.get("t0", 0))) / 1e6
        self.pre = start + min(max(pre, 0.0), span)   # the end of its preamble
        self.freq = msg.get("freq")
        self.bw = msg.get("bw")
        self.sf = msg.get("sf")
        self.sync = msg.get("sync")
        self.power = msg.get("power_dbm", ether_module.DEFAULT_POWER_DBM)
        self.size = (len(msg.get("payload", "")) * 3) // 4
        self.told = set()       # stations the ether told, by rx_begin
        self.heard = {}         # station -> verdict at its rx_end
        self.eid = None


def read_record(path):
    """Frames, receptions and states, in the order the ether saw them."""
    frames = []
    begins = []                 # (stamp, receiver sid, msg)
    ends = []
    states = collections.defaultdict(list)
    first = last = None
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("#"):
                continue
            parts = line.rstrip("\n").split("\t", 3)
            if len(parts) != 4:
                continue
            when, direction, sid, text = parts
            try:
                t = stamp(when)
                msg = json.loads(text)
            except ValueError:
                continue
            first = t if first is None else first
            last = t
            kind = msg.get("type")
            sid = int(sid) if sid.isdigit() else None
            if direction == "in" and kind == "tx":
                frames.append(Frame(sid, msg, t))
            elif direction == "in" and kind == "state":
                states[sid].append((t, msg))
            elif direction == "out" and kind == "rx_begin":
                begins.append((t, sid, msg))
            elif direction == "out" and kind == "rx_end":
                ends.append((t, sid, msg))
    return frames, begins, ends, states, first, last


def match_receptions(frames, begins, ends):
    """Tie each rx_begin to the frame it announced, by timing and carrier.

    The ether numbers frames itself and the transmitter's `tx` does not carry
    that number, so an rx_begin is matched to the frame that started at the
    same instant (the ether sends both in one handler) and still has no
    ether number, and its rx_end follows by the number.
    """
    starts = [f.start for f in frames]
    by_eid = {}
    for t, rsid, msg in begins:
        eid = msg.get("id")
        frame = by_eid.get(eid)
        if frame is None:
            i = bisect.bisect_right(starts, t) - 1
            while i >= 0 and t - frames[i].start < 0.05:
                cand = frames[i]
                if cand.eid is None or cand.eid == eid:
                    span = (int(msg.get("t_end", 0)) - int(msg.get("t0", 0))) / 1e6
                    if abs((cand.end - cand.start) - span) < 0.002:
                        cand.eid = eid
                        frame = by_eid[eid] = cand
                        break
                i -= 1
        if frame is not None:
            frame.told.add(rsid)     # a CAD that sensed it was told of it too
    for t, rsid, msg in ends:
        frame = by_eid.get(msg.get("id"))
        if frame is not None:
            frame.heard[rsid] = msg.get("verdict")


def build_air(scenario_path):
    """An ether with the run's physics and geometry, for levels only."""
    asyncio.set_event_loop(asyncio.new_event_loop())
    got = ether_module.read_scenario(scenario_path)
    physics, places, obstructions = got[0], got[1], got[2]
    links = got[3] if len(got) > 3 else []
    air = ether_module.Ether(None, physics)
    for sid, (x, y, gain) in places.items():
        air.place(sid, x, y, gain)
    for a, b, db in obstructions:
        air.obstruct(a, b, db)
    for a, b, loss in links:
        air.link(a, b, loss)
    return air


def names_of(scenario_path):
    import yaml
    with open(scenario_path, encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    kinds = data.get("kinds") or {}
    first_kind = next(iter(kinds), "reticulous")
    out = {}
    for name, node in (data.get("nodes") or {}).items():
        out[int(node["id"])] = (name, node.get("kind") or first_kind)
    return out


def audible_at(air, frame, rsid):
    level = air.level(frame.sid, rsid, frame.freq or 869_525_000, frame.power)
    return air.audible(level, frame.bw, frame.sf), level


def judge(frames, states, air, window_s, duration):
    stations = sorted({f.sid for f in frames} | set(states))
    report = {"duty": [], "carrier_sense": [], "unheard": [], "collisions": {}}

    # ---- duty ----------------------------------------------------------
    per = collections.defaultdict(list)          # (sid, band) -> [(start, airtime)]
    for f in frames:
        name, _ = band_of(f.freq)
        per[(f.sid, name)].append((f.start, f.end - f.start))
    span = min(window_s, max(duration, 1e-9))
    for (sid, name), items in sorted(per.items()):
        items.sort()
        best = total = 0.0
        j = 0
        running = 0.0
        for i, (t, air_s) in enumerate(items):
            running += air_s
            total += air_s
            while items[j][0] < t - window_s:
                running -= items[j][1]
                j += 1
            best = max(best, running)
        budget = dict((b[3], b[2]) for b in BANDS).get(name)
        report["duty"].append({
            "sid": sid, "band": name, "frames": len(items), "airtime_s": round(total, 3),
            "busiest_window_s": round(best, 3), "window_s": round(span, 1),
            "share_permille": round(1000 * best / span, 2) if span else None,
            "budget_permille": budget,
            "over": budget is not None and best > budget / 1000.0 * window_s,
            "over_pace": budget is not None and 1000 * best / span > budget,
        })

    # ---- carrier sense -------------------------------------------------
    order = sorted(frames, key=lambda f: f.start)
    starts = [f.start for f in order]
    for f in order:
        # every other frame on this carrier already on the air when f began
        i = bisect.bisect_left(starts, f.start)
        k = i - 1
        while k >= 0 and f.start - order[k].start < 120:
            g = order[k]
            k -= 1
            if g.sid == f.sid or g.end <= f.start:
                continue
            if not ether_module.same_carrier(g.freq, f.freq, max(g.bw or 0, f.bw or 0)):
                continue
            ok, level = audible_at(air, g, f.sid)
            if not ok:
                continue
            age = f.start - g.start
            report["carrier_sense"].append({
                "sid": f.sid, "over": g.sid, "at": round(f.start, 3),
                "on_air_ms": round(1000 * age, 1), "level_dbm": round(level, 1),
                "told": f.sid in g.told,
                "window": "blind" if age < BLIND_S else "preamble"
                if f.start < g.pre else "payload",
            })

    # ---- the unheard ---------------------------------------------------
    silent = collections.Counter()
    for f in frames:
        if not f.told:
            silent[(f.sid, f.freq, f.sf, f.bw)] += 1
    report["unheard"] = [{"sid": s, "freq": fr, "sf": sf, "bw": bw, "frames": n}
                         for (s, fr, sf, bw), n in sorted(silent.items())]

    # ---- collisions ----------------------------------------------------
    crc = collections.Counter()
    hidden = collections.Counter()
    for f in frames:
        for rsid, verdict in f.heard.items():
            if verdict != "clean":
                crc[rsid] += 1
    for a_i, f in enumerate(order):
        k = a_i + 1
        while k < len(order) and order[k].start < f.end:
            g = order[k]
            k += 1
            if g.sid == f.sid or not ether_module.same_carrier(
                    g.freq, f.freq, max(g.bw or 0, f.bw or 0)):
                continue
            hears_ab, _ = audible_at(air, f, g.sid)
            hears_ba, _ = audible_at(air, g, f.sid)
            key = "hidden" if not (hears_ab or hears_ba) else "in earshot"
            hidden[key] += 1
    report["collisions"] = {"spoiled_receptions": dict(crc),
                            "overlapping_pairs": dict(hidden)}
    return stations, report


def main(argv=None):
    ap = argparse.ArgumentParser(description="judge a SIMesh run from its record")
    ap.add_argument("--run", default=os.path.join(HERE, "run"),
                    help="the run directory (default: testbed/run)")
    ap.add_argument("--window", type=float, default=3600.0,
                    help="duty window in seconds (default 3600, the regulation's hour)")
    ap.add_argument("--json", action="store_true", help="the whole report as JSON")
    ap.add_argument("--detail", action="store_true",
                    help="list every carrier-sense event, not only the counts")
    args = ap.parse_args(argv)

    record = os.path.join(args.run, "record.tsv")
    scenario = os.path.join(args.run, "scenario.yaml")
    frames, begins, ends, states, first, last = read_record(record)
    match_receptions(frames, begins, ends)
    air = build_air(scenario)
    names = names_of(scenario)
    duration = (last - first) if first is not None else 0.0
    stations, report = judge(frames, states, air, args.window, duration)
    report["run_s"] = round(duration, 1)
    report["frames"] = len(frames)

    if args.json:
        print(json.dumps(report, indent=1))
        return 0

    def who(sid):
        name, kind = names.get(sid, (str(sid), "?"))
        return "%s (%s)" % (name, kind)

    print("run: %.0f s, %d frames, %d stations" % (duration, len(frames), len(stations)))
    print("\nduty (busiest %.0f s window; budget per band):" % min(args.window, duration or 1))
    for d in report["duty"]:
        flag = "  OVER BUDGET" if d["over"] else ("  over pace" if d["over_pace"] else "")
        print("  %-24s %-8s %4d frames %8.1f s air  %6.2f‰ of %s‰%s" % (
            who(d["sid"]), d["band"], d["frames"], d["airtime_s"],
            d["share_permille"] or 0, d["budget_permille"], flag))
    cs = report["carrier_sense"]
    print("\ncarrier sense: %d transmissions began over an audible frame" % len(cs))
    by = collections.Counter((c["sid"], c["told"], c["window"]) for c in cs)
    for (sid, told, window), n in sorted(by.items()):
        print("  %-24s %4d  over a frame in its %-8s %s" % (
            who(sid), n, window,
            "(told of it)" if told else "(never told: not listening when it began)"))
    if args.detail:
        for c in cs:
            print("    %s at %.3f over %s: on the air %.1f ms, %.1f dBm, %s" % (
                who(c["sid"]), c["at"] - (first or 0), who(c["over"]), c["on_air_ms"],
                c["level_dbm"], "told" if c["told"] else "never told"))
    print("\nunheard frames (nobody was told):")
    for u in report["unheard"]:
        print("  %-24s %4d frames on %s Hz sf%s bw%s" % (
            who(u["sid"]), u["frames"], u["freq"], u["sf"], u["bw"]))
    col = report["collisions"]
    print("\ncollisions: overlapping pairs %s" % col["overlapping_pairs"])
    for sid, n in sorted(col["spoiled_receptions"].items()):
        print("  %-24s %4d receptions spoiled" % (who(sid), n))
    return 0


if __name__ == "__main__":
    sys.exit(main())
