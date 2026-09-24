"""The scenario file and the kinds it names. No stations are started."""

import os
import sys
import types

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import kinds as kinds_module          # noqa: E402 - the path is set just above
import scenario as scenario_module    # noqa: E402

ELF = "/opt/builds/reticulous.elf"
FIXED = "/opt/builds/data_merged"

MIXED = """\
origin: [52.0, 4.0]
kinds:
  reticulous:
    elf: ../../../reticulous/esp-idf/build.linux/reticulous.elf
    fixed: ../../../reticulous/esp-idf/build.linux/data_merged
  berlinmesh:
    elf: ../../../sergey/reticulum/fw/simesh/target/release/simesh
    tools: { rncfg: ../../../sergey/reticulum/target/release/rncfg }
    env: { RUST_LOG: info }
    setup:
      - "name set {name}"
setup:
  - "hostname {name}"
nodes:
  alpha: { id: 1, pos: [52.0, 4.0] }
  sergey:
    id: 2
    kind: berlinmesh
    pos: [52.0, 4.01]
    setup:
      - "transport off"
"""


@pytest.fixture(autouse=True)
def default_kinds(monkeypatch):
    monkeypatch.setattr(scenario_module, "DEFAULT_KINDS",
                        {"reticulous": {"elf": ELF, "fixed": FIXED}})


def write(tmp_path, text, name="s.yaml"):
    path = tmp_path / name
    path.write_text(text)
    return str(path)


def test_a_scenario_that_names_no_kinds_gets_the_default_and_keeps_its_shape(tmp_path):
    path = write(tmp_path, "nodes:\n  alpha: { id: 1, pos: [1.0, 2.0] }\n")
    data = scenario_module.read(path)
    assert data["kinds"] == {"reticulous": {"elf": ELF, "fixed": FIXED}}
    assert data["nodes"]["alpha"]["kind"] == "reticulous"
    text = scenario_module.dump(data)
    assert "kinds" not in text
    assert "kind:" not in text


def test_kinds_round_trip_through_dump_and_read(tmp_path):
    data = scenario_module.read(write(tmp_path, MIXED))
    assert list(data["kinds"]) == ["reticulous", "berlinmesh"]
    assert data["nodes"]["alpha"]["kind"] == "reticulous"
    assert data["nodes"]["sergey"]["kind"] == "berlinmesh"

    text = scenario_module.dump(data)
    assert "kinds:" in text
    assert "kind: berlinmesh" in text
    assert "kind: reticulous" not in text          # the first kind is implied
    again = scenario_module.read(write(tmp_path, text, "again.yaml"))
    assert again == data


def test_shadowing_is_written_only_when_it_says_something(tmp_path):
    data = scenario_module.read(write(tmp_path, MIXED))
    assert "shadowing" not in scenario_module.dump(data)

    data["physics"].update(shadowing_db=7, shadowing_seed=3)
    text = scenario_module.dump(data)
    assert "shadowing_db: 7" in text and "shadowing_seed: 3" in text
    assert scenario_module.read(write(tmp_path, text, "again.yaml")) == data


def test_the_page_sets_shadowing_and_the_seed_stays_a_whole_number(tmp_path):
    data = scenario_module.read(write(tmp_path, MIXED))
    sc = scenario_module.Scenario("mixed", data, run_dir=str(tmp_path / "run"))
    sc.set_physics({"shadowing_db": "6.5", "shadowing_seed": "4", "unknown": 1})
    assert sc.physics["shadowing_db"] == 6.5
    assert sc.physics["shadowing_seed"] == 4
    assert isinstance(sc.physics["shadowing_seed"], int)
    assert "unknown" not in sc.physics


def test_the_capture_model_is_written_only_when_it_is_the_bench(tmp_path):
    data = scenario_module.read(write(tmp_path, MIXED))
    assert "capture_model" not in scenario_module.dump(data)

    data["physics"]["capture_model"] = "bench"
    text = scenario_module.dump(data)
    assert 'capture_model: "bench"' in text
    assert scenario_module.read(write(tmp_path, text, "again.yaml")) == data


def test_a_capture_model_nobody_knows_is_refused(tmp_path):
    data = scenario_module.read(write(tmp_path, MIXED))
    sc = scenario_module.Scenario("mixed", data, run_dir=str(tmp_path / "run"))
    with pytest.raises(scenario_module.ScenarioError):
        sc.set_physics({"capture_model": "optimistic"})
    text = MIXED.replace("setup:\n  - \"hostname", 'physics: { capture_model: "guess" }\nsetup:\n  - "hostname', 1)
    with pytest.raises(scenario_module.ScenarioError):
        scenario_module.read(write(tmp_path, text, "bad.yaml"))


def test_links_are_written_only_when_there_are_some_and_round_trip(tmp_path):
    data = scenario_module.read(write(tmp_path, MIXED))
    assert "links" not in scenario_module.dump(data)

    data["links"] = [{"between": ["alpha", "sergey"], "loss_db": 118.5}]
    text = scenario_module.dump(data)
    assert "  - { between: [alpha, sergey], loss_db: 118.5 }" in text
    assert scenario_module.read(write(tmp_path, text, "again.yaml")) == data


def test_removing_a_node_drops_its_links(tmp_path):
    text = MIXED + "links:\n  - { between: [alpha, sergey], loss_db: 118.5 }\n"
    data = scenario_module.read(write(tmp_path, text))
    sc = scenario_module.Scenario("mixed", data, run_dir=str(tmp_path / "run"))
    sc.remove_node("sergey")
    assert sc.links == []


def test_a_link_without_a_loss_is_refused(tmp_path):
    text = MIXED + "links:\n  - { between: [alpha, sergey] }\n"
    with pytest.raises(scenario_module.ScenarioError):
        scenario_module.read(write(tmp_path, text))


def test_scenario_setup_goes_to_the_first_kind_only(tmp_path):
    data = scenario_module.read(write(tmp_path, MIXED))
    sc = scenario_module.Scenario("mixed", data, run_dir=str(tmp_path / "run"))
    assert sc.lines_for("alpha") == ["hostname alpha"]
    assert sc.lines_for("sergey") == ["name set sergey", "transport off"]


def test_a_node_of_a_kind_nobody_named_is_refused(tmp_path):
    text = "nodes:\n  alpha: { id: 1, kind: meshtastic, pos: [0, 0] }\n"
    with pytest.raises(scenario_module.ScenarioError, match="meshtastic"):
        scenario_module.read(write(tmp_path, text))


def test_two_nodes_on_one_id_are_refused(tmp_path):
    text = ("nodes:\n  alpha: { id: 3, pos: [0, 0] }\n"
            "  bravo: { id: 3, pos: [0, 1] }\n")
    with pytest.raises(scenario_module.ScenarioError, match="share id 3"):
        scenario_module.read(write(tmp_path, text))


def test_kind_paths_are_relative_to_the_scenarios_directory(tmp_path):
    data = scenario_module.read(write(tmp_path, MIXED))
    base = str(tmp_path / "SIMesh" / "testbed" / "scenarios")
    kinds = kinds_module.make_kinds(data["kinds"], base)
    workspace = str(tmp_path)
    assert kinds["reticulous"].elf == os.path.join(
        workspace, "reticulous", "esp-idf", "build.linux", "reticulous.elf")
    assert kinds["berlinmesh"].rncfg == os.path.join(
        workspace, "sergey", "reticulum", "target", "release", "rncfg")


def test_every_kind_gets_the_contract_and_reticulous_its_own_names(tmp_path):
    data = scenario_module.read(write(tmp_path, MIXED))
    kinds = kinds_module.make_kinds(data["kinds"], str(tmp_path))
    station = types.SimpleNamespace(node_id=4, dir="/run/nodes/delta",
                                    addr="127.0.0.8", ether_addr="127.0.0.1:7000")
    contract = {"SIMESH_NODE_ID": "4", "SIMESH_NODE_DIR": "/run/nodes/delta",
                "SIMESH_BIND_ADDR": "127.0.0.8", "SIMESH_ETHER": "127.0.0.1:7000"}

    theirs = kinds["berlinmesh"].env(station)
    assert theirs == dict(contract, RUST_LOG="info")

    # The reticulous firmware reads the same values under its own names; the
    # names are its kind's business, and the values must be the contract's.
    ours = kinds["reticulous"].env(station)
    assert {k: ours[k] for k in contract} == contract
    own = {v for k, v in ours.items() if k not in contract}
    assert set(contract.values()) <= own
    assert any(v.endswith("data_merged") for v in own)

    assert kinds["reticulous"].web_port() == 80
    assert kinds["berlinmesh"].web_port() is None


def test_a_kind_of_an_unknown_type_is_refused():
    with pytest.raises(kinds_module.CommandError, match="no such type"):
        kinds_module.make_kinds({"lorawan": {"elf": "x"}}, "/")
    made = kinds_module.make_kinds({"older": {"type": "reticulous", "elf": "x"}}, "/")
    assert made["older"].type_name == "reticulous"
