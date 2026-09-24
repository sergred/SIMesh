"""The referee against a record written by hand. No ether, no stations."""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import referee   # noqa: E402 - the path is set just above

SCENARIO = """\
origin: [52.0, 13.0]
physics: { exponent: 2.7, noise_figure_db: 6, capture_db: 6 }
nodes:
  a: { id: 1, pos: [52.0, 13.0] }
  b: { id: 2, pos: [52.0, 13.00147] }
  c: { id: 3, pos: [52.0, 13.8] }
"""


def line(t, direction, sid, msg):
    stamp = "2026-09-24T12:00:%09.6f+00:00" % t
    return "%s\t%s\t%s\t%s\n" % (stamp, direction, sid, json.dumps(msg))


def tx(sid, fid, t0_us, span_us, freq=869_525_000, sf=8):
    return {"type": "tx", "sid": sid, "slot": 0, "id": fid, "freq": freq, "bw": 125_000,
            "sf": sf, "sync": 18, "power_dbm": 14, "payload": "AAAA",
            "t0": t0_us, "t_pre": t0_us + span_us // 10, "t_hdr": t0_us + span_us // 5,
            "t_end": t0_us + span_us}


def write_run(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "scenario.yaml").write_text(SCENARIO)
    lines = [
        # a sends half a second; the ether tells b and closes it clean
        line(1.000, "in", 1, tx(1, 7, 10_000_000, 500_000)),
        line(1.0002, "out", 2, {"type": "rx_begin", "slot": 0, "id": 1, "t0": 5, "t_pre": 50_005,
                                "t_hdr": 100_005, "t_end": 500_005, "level": -60, "takes": True}),
        line(1.500, "out", 2, {"type": "rx_end", "slot": 0, "id": 1, "verdict": "clean",
                               "payload": "AAAA", "rssi": -60, "snr": 12}),
        # b, told of a's frame 200 ms before, sends into its payload anyway
        line(1.200, "in", 2, tx(2, 3, 20_000_000, 300_000)),
        # c sends on another channel, a long way off: nobody is told
        line(5.000, "in", 3, tx(3, 1, 30_000_000, 300_000, freq=869_475_000, sf=7)),
    ]
    lines.sort(key=lambda s: s.split("\t")[0])
    (run / "record.tsv").write_text("# test record\n" + "".join(lines))
    return run


def test_the_referee_names_a_sender_that_talked_over_a_frame_it_was_told_of(tmp_path, capsys):
    run = write_run(tmp_path)
    assert referee.main(["--run", str(run), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    events = report["carrier_sense"]
    assert len(events) == 1
    event = events[0]
    assert (event["sid"], event["over"], event["told"]) == (2, 1, True)
    assert event["window"] == "payload"
    assert event["on_air_ms"] == 200.0


def test_the_referee_lists_frames_nobody_was_told_of_by_where_they_were_sent(tmp_path, capsys):
    run = write_run(tmp_path)
    referee.main(["--run", str(run), "--json"])
    report = json.loads(capsys.readouterr().out)
    unheard = {(u["sid"], u["freq"], u["sf"]): u["frames"] for u in report["unheard"]}
    assert unheard == {(2, 869_525_000, 8): 1, (3, 869_475_000, 7): 1}


def test_the_referee_holds_airtime_against_the_band_it_was_sent_in(tmp_path, capsys):
    run = write_run(tmp_path)
    referee.main(["--run", str(run), "--json"])
    duty = {d["sid"]: d for d in json.loads(capsys.readouterr().out)["duty"]}
    assert duty[1]["band"] == "g3" and duty[1]["budget_permille"] == 100
    assert duty[1]["airtime_s"] == 0.5
    assert not duty[1]["over"]
