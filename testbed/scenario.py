#!/usr/bin/env python3
"""Scenarios and snapshots: the two things worth keeping, and the run between.

A **scenario** is the network as designed, and nothing that happened to it:
where the stations stand, what the air is like, and the CLI lines each of them
is set up with. It is one file.

    testbed/scenarios/<name>.yaml

A **snapshot** is a scenario plus everything the stations have since become —
their identities, keys, paths and message history, as the firmware keeps them.

    testbed/snapshots/<name>/scenario.yaml
    testbed/snapshots/<name>/nodes/<node name>/state/

The split is the whole point. Loading a scenario gives a factory-fresh network
that is reproducible from a file you can read; loading a snapshot gives back a
network that had been running. Saving a scenario records a design, saving a
snapshot records a moment.

The live run is neither: `testbed/run/` holds the loaded map, the stations' state
as they are writing it, their logs and the ether's record. Logs and the record
are run output and are never copied into either kind of save.
"""

import copy
import json
import os
import re
import shutil

import yaml

import stations as stations_module

SIM_DIR = os.path.dirname(os.path.abspath(__file__))
SCENARIOS_DIR = os.path.join(SIM_DIR, "scenarios")
SNAPSHOTS_DIR = os.path.join(SIM_DIR, "snapshots")
RUN_DIR = os.path.join(SIM_DIR, "run")

SCENARIO_FILE = "scenario.yaml"
NODES_DIR = "nodes"
# Which scenario a snapshot was taken of. Kept beside it so that loading the
# snapshot and then saving writes the scenario it came from, rather than
# quietly minting a new one named after the snapshot.
ORIGIN_FILE = "from"

# A node's name is its hostname, the label the proxy routes and the label on
# the map, so it is what all three can carry.
NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$")

# What every station in a new scenario is told, in order. These are the answers
# a board on a cable asks for: a name, a password (Reticulum is held until one
# is set), an identity to speak as, and a radio with a region, since one
# without will not start. `{name}` and friends are expanded per station, which
# is what lets one shared list say node-specific things.
DEFAULT_SETUP = [
    "hostname {name}",
    "auth passwd admin admin",
    "lxmf create {name}",
    "set s.rnsd.transport_enabled 0",
    "lora up",
    "lora 0 freq 869.525",
    "lora 0 sf 8",
    "lora 0 bw 125",
]

DEFAULT_PHYSICS = {"exponent": 2.7, "noise_figure_db": 6, "capture_db": 6,
                   "shadowing_db": 0, "shadowing_seed": 0,
                   "capture_model": "margin"}

# Physics a file carries only when it says something, so a scenario written
# before they existed, or one that leaves them at their defaults, is written
# back exactly as it was read.
OPTIONAL_PHYSICS = ("shadowing_db", "shadowing_seed", "capture_model")


def physics_value(key, value):
    """One physics setting as the file holds it: a whole seed, a named capture
    model, a number otherwise."""
    if key == "capture_model":
        if value not in ("margin", "bench"):
            raise ScenarioError("capture_model is margin or bench, not %r" % (value,))
        return value
    if key == "shadowing_seed":
        return int(value)
    return float(value)

# The kinds a scenario that names none has: one `reticulous` kind, from simd's
# --elf and --fixed. Set by simd before anything is read, so every scenario
# written before kinds existed loads unchanged, and a file is given a `kinds:`
# block only when it says more than this.
DEFAULT_KINDS = {}

MACRO_RE = re.compile(r"\{([a-z_]+)\}")


class ScenarioError(Exception):
    """A scenario or snapshot could not be read, written or changed as asked."""


def check_name(name, what="scenario"):
    """A name that can be a directory, a hostname and a proxy label at once."""
    if not NAME_RE.match(name or ""):
        raise ScenarioError(
            "%r is not a usable %s name: lower-case letters, digits and "
            "hyphens, starting and ending with a letter or digit" % (name, what))
    return name


def scenario_path(name):
    return os.path.join(SCENARIOS_DIR, check_name(name) + ".yaml")


def snapshot_path(name):
    return os.path.join(SNAPSHOTS_DIR, check_name(name, "snapshot"))


def scenarios():
    """Every scenario on disk, by name."""
    if not os.path.isdir(SCENARIOS_DIR):
        return []
    return sorted(entry[:-5] for entry in os.listdir(SCENARIOS_DIR)
                  if entry.endswith(".yaml"))


def snapshots():
    """Every snapshot on disk, by name."""
    if not os.path.isdir(SNAPSHOTS_DIR):
        return []
    return sorted(entry for entry in os.listdir(SNAPSHOTS_DIR)
                  if os.path.isfile(os.path.join(SNAPSHOTS_DIR, entry, SCENARIO_FILE)))


# ---- macros --------------------------------------------------------------

def expand(line, name, node):
    """Fill `{name}`, `{id}` and `{addr}` in one setup line for one station.

    A macro the list does not define is left exactly as written. A CLI line is
    somebody's text and may legitimately contain braces, and silently emptying
    something that only looked like a macro is worse than passing it through
    for the station to complain about.
    """
    values = {"name": name,
              "id": str(node["id"]),
              "addr": stations_module.bind_addr(node["id"])}
    return MACRO_RE.sub(lambda m: values.get(m.group(1), m.group(0)), line)


def expand_all(lines, name, node):
    """The lines a station is actually given: expanded, minus blanks and comments."""
    out = []
    for line in lines:
        line = expand(line, name, node).strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


# ---- the file ------------------------------------------------------------

def blank():
    """An empty scenario: an origin, the default air, kinds and setup."""
    return {"origin": [0.0, 0.0],
            "physics": dict(DEFAULT_PHYSICS),
            "kinds": copy.deepcopy(DEFAULT_KINDS),
            "setup": list(DEFAULT_SETUP),
            "nodes": {},
            "obstructions": [],
            "links": []}


def first_kind(data):
    """The kind a node that names none is, and the one scenario `setup:` is for."""
    return next(iter(data.get("kinds") or {}), None)


def dump_value(value):
    """A value inside a kind's spec: a scalar, a flow mapping, a flow list."""
    if isinstance(value, dict):
        return "{ %s }" % ", ".join("%s: %s" % (k, dump_value(v)) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return "[%s]" % ", ".join(dump_value(v) for v in value)
    return scalar(value)


def dump_kinds(kinds):
    out = ["kinds:"]
    for name, spec in kinds.items():
        if not spec:
            out.append("  %s: {}" % name)
            continue
        out.append("  %s:" % name)
        for key, value in spec.items():
            if key == "setup" and value:
                out.append("    setup:")
                out += ["      - %s" % scalar(line) for line in value]
            else:
                out.append("    %s: %s" % (key, dump_value(value)))
    return out


def scalar(value):
    """One YAML scalar. JSON's spelling of a string is also YAML's."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(round(value, 9) if isinstance(value, float) else value)
    return json.dumps(str(value))


def dump(data):
    """The scenario as the file, written out in the shape the format documents.

    Hand-composed rather than emitted by the YAML writer so that a node stays
    one readable line and the sections keep their order — the file is meant to
    be opened and edited, not only round-tripped.
    """
    out = ["origin: [%s, %s]" % (scalar(data["origin"][0]), scalar(data["origin"][1]))]
    physics = data.get("physics") or DEFAULT_PHYSICS
    keys = ["exponent", "noise_figure_db", "capture_db"] + [
        key for key in OPTIONAL_PHYSICS
        if physics.get(key, DEFAULT_PHYSICS[key]) != DEFAULT_PHYSICS[key]]
    out.append("physics: { %s }" % ", ".join(
        "%s: %s" % (key, scalar(physics.get(key, DEFAULT_PHYSICS[key])))
        for key in keys))

    kinds = data.get("kinds") or {}
    if kinds and kinds != DEFAULT_KINDS:
        out += dump_kinds(kinds)
    default_kind = first_kind(data)

    setup = data.get("setup") or []
    out.append("setup:" if setup else "setup: []")
    out += ["  - %s" % scalar(line) for line in setup]

    nodes = data.get("nodes") or {}
    out.append("nodes:" if nodes else "nodes: {}")
    for name, node in sorted(nodes.items(), key=lambda kv: kv[1]["id"]):
        kind = node.get("kind")
        kind = kind if kind and kind != default_kind else None
        if node.get("setup"):
            # A node with its own lines cannot stay on one line; write it as a
            # block so those lines are as editable as the scenario's own.
            out.append("  %s:" % name)
            out.append("    id: %s" % scalar(node["id"]))
            if kind:
                out.append("    kind: %s" % kind)
            out.append("    pos: [%s, %s]" % (scalar(node["pos"][0]),
                                              scalar(node["pos"][1])))
            if node.get("gain_db"):
                out.append("    gain_db: %s" % scalar(node["gain_db"]))
            out.append("    setup:")
            out += ["      - %s" % scalar(line) for line in node["setup"]]
        else:
            head = "  %s: { id: %s" % (name, scalar(node["id"]))
            if kind:
                head += ", kind: %s" % kind
            head += ", pos: [%s, %s]" % (scalar(node["pos"][0]), scalar(node["pos"][1]))
            if node.get("gain_db"):
                head += ", gain_db: %s" % scalar(node["gain_db"])
            out.append(head + " }")

    walls = data.get("obstructions") or []
    out.append("obstructions:" if walls else "obstructions: []")
    out += ["  - { between: [%s, %s], db: %s }"
            % (wall["between"][0], wall["between"][1], scalar(wall.get("db", 0)))
            for wall in walls]
    links = data.get("links") or []
    if links:
        out.append("links:")
        out += ["  - { between: [%s, %s], loss_db: %s }"
                % (link["between"][0], link["between"][1], scalar(link["loss_db"]))
                for link in links]
    return "\n".join(out) + "\n"


def read(path):
    """A scenario file, filled out with the defaults for anything it omits."""
    if os.path.isdir(path):
        path = os.path.join(path, SCENARIO_FILE)
    try:
        with open(path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except (OSError, yaml.YAMLError) as err:
        raise ScenarioError("%s: %s" % (path, err)) from err
    if not isinstance(data, dict):
        raise ScenarioError("%s: not a scenario" % path)
    filled = blank()
    filled["origin"] = [float(v) for v in (data.get("origin") or [0.0, 0.0])[:2]]
    filled["physics"] = {**DEFAULT_PHYSICS, **(data.get("physics") or {})}
    try:
        physics_value("capture_model", filled["physics"]["capture_model"])
    except ScenarioError as err:
        raise ScenarioError("%s: %s" % (path, err)) from err
    kinds = data.get("kinds")
    if kinds:
        if not isinstance(kinds, dict) or not all(
                isinstance(spec, dict) or spec is None for spec in kinds.values()):
            raise ScenarioError("%s: `kinds:` is a mapping of kind name to its spec" % path)
        filled["kinds"] = {str(name): dict(spec or {}) for name, spec in kinds.items()}
    default_kind = first_kind(filled)
    filled["setup"] = list(data.get("setup") or [])
    filled["obstructions"] = [
        {"between": list(wall["between"])[:2], "db": float(wall.get("db", 0))}
        for wall in (data.get("obstructions") or [])]
    try:
        filled["links"] = [
            {"between": list(link["between"])[:2], "loss_db": float(link["loss_db"])}
            for link in (data.get("links") or [])]
    except (KeyError, TypeError, ValueError) as err:
        raise ScenarioError("%s: a link is { between: [a, b], loss_db: <dB> }" % path) from err
    filled["nodes"] = {}
    ids = {}
    for name, node in (data.get("nodes") or {}).items():
        check_name(name, "node")
        kind = node.get("kind") or default_kind
        kind = None if kind is None else str(kind)
        if kind not in filled["kinds"]:
            raise ScenarioError("%s: node %s is of kind %r, which `kinds:` does not name"
                                % (path, name, kind))
        node_id = int(node["id"])
        if node_id in ids:
            # Two stations under one id are two processes answering the ether
            # as one station, and two sockets on one address.
            raise ScenarioError("%s: nodes %s and %s share id %d"
                                % (path, ids[node_id], name, node_id))
        ids[node_id] = name
        filled["nodes"][name] = {
            "id": node_id,
            "kind": kind,
            "pos": [float(v) for v in (node.get("pos") or [0.0, 0.0])[:2]],
            "gain_db": float(node.get("gain_db", 0.0)),
            "setup": list(node.get("setup") or []),
        }
    return filled


def write(path, data):
    if os.path.isdir(path):
        path = os.path.join(path, SCENARIO_FILE)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(dump(data))


# ---- state, and moving it about ------------------------------------------

def copy_state(src_root, dst_root, wanted=None):
    """Every station's state store from one tree to another, and nothing else.

    A run directory also holds logs and the ether's record; those are an
    account of one run rather than part of a snapshot, so they are left where
    they fall on the way out and left alone on the way in. A node the source
    has nothing for is a node this copy is clearing.
    """
    src_nodes = os.path.join(src_root, NODES_DIR)
    dst_nodes = os.path.join(dst_root, NODES_DIR)
    have = set(os.listdir(src_nodes)) if os.path.isdir(src_nodes) else set()
    keep = have if wanted is None else (have & set(wanted))
    for name in sorted(keep):
        state = os.path.join(src_nodes, name, "state")
        if not os.path.isdir(state):
            continue
        target = os.path.join(dst_nodes, name, "state")
        shutil.rmtree(target, ignore_errors=True)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copytree(state, target)
    if os.path.isdir(dst_nodes):
        for name in os.listdir(dst_nodes):
            if name not in keep:
                shutil.rmtree(os.path.join(dst_nodes, name), ignore_errors=True)


def wipe_state(root, name=None):
    """Throw away one station's state store, or every one of them.

    This is the whole of a factory reset: the station comes back to a directory
    with nothing in it, which is the same station a node clicked onto the map
    for the first time is, and the setup lines run again for the same reason.
    """
    nodes = os.path.join(root, NODES_DIR)
    if not os.path.isdir(nodes):
        return
    for entry in ([name] if name else os.listdir(nodes)):
        shutil.rmtree(os.path.join(nodes, entry, "state"), ignore_errors=True)


class Scenario:
    """The loaded map, the run directory it drives, and whether it is dirty.

    Everything that changes the map goes through a method here, and every one
    of them sets `dirty` — which is the whole of the unsaved-changes rule, and
    why no caller has to remember it. State is not the scenario's business:
    `dirty` is about the design, and a station writing to its store is not a
    change to the design.
    """

    def __init__(self, name, data, run_dir=RUN_DIR):
        self.name = name
        self.data = data
        self.run_dir = run_dir
        self.dirty = False

    # ---- the map ---------------------------------------------------------

    @property
    def origin(self):
        return tuple(self.data["origin"])

    @property
    def physics(self):
        return self.data["physics"]

    @property
    def setup(self):
        return self.data["setup"]

    @property
    def nodes(self):
        return self.data["nodes"]

    @property
    def obstructions(self):
        return self.data["obstructions"]

    @property
    def links(self):
        return self.data.setdefault("links", [])

    @property
    def kinds(self):
        """Kind name -> its spec, in the file's order; the first is the default."""
        return self.data["kinds"]

    @property
    def default_kind(self):
        return first_kind(self.data)

    def node(self, name):
        node = self.nodes.get(name)
        if node is None:
            raise ScenarioError("no node called %r" % name)
        return node

    def node_dir(self, name):
        return os.path.join(self.run_dir, NODES_DIR, name)

    def lines_for(self, name):
        """Everything a station is told, in order, with the macros filled in.

        The scenario's own `setup:` goes only to nodes of the first kind: a
        line is in one firmware's dialect, and the same text typed at another
        means something else or nothing. Then the node's kind's lines, then
        its own.
        """
        node = self.node(name)
        kind = node.get("kind") or self.default_kind
        shared = self.setup if kind == self.default_kind else []
        spec = self.kinds.get(kind) or {}
        return (expand_all(shared, name, node)
                + expand_all(spec.get("setup") or [], name, node)
                + expand_all(node.get("setup") or [], name, node))

    def next_id(self):
        """The lowest station id this scenario is not already using.

        Ids are allocated once and never reused while a node exists, so a
        node's address and its directory stay put; a removed node's id does
        come back, because nothing is left that answers to it.
        """
        taken = {node["id"] for node in self.nodes.values()}
        limit = stations_module.max_node_id()
        for candidate in range(1, limit + 1):
            if candidate not in taken:
                return candidate
        raise ScenarioError("a scenario holds at most %d nodes on the network %s"
                            % (limit, stations_module.NET))

    def add_node(self, name, pos, setup=None, kind=None):
        check_name(name, "node")
        if name in self.nodes:
            raise ScenarioError("there is already a node called %r" % name)
        kind = kind or self.default_kind
        if kind not in self.kinds:
            raise ScenarioError("no kind called %r in this scenario" % kind)
        node = {"id": self.next_id(), "kind": kind,
                "pos": [float(pos[0]), float(pos[1])],
                "gain_db": 0.0, "setup": list(setup or [])}
        self.nodes[name] = node
        self.dirty = True
        return node

    def remove_node(self, name):
        self.node(name)
        del self.nodes[name]
        self.data["obstructions"] = [
            wall for wall in self.obstructions if name not in wall["between"]]
        self.data["links"] = [
            link for link in self.links if name not in link["between"]]
        shutil.rmtree(self.node_dir(name), ignore_errors=True)
        self.dirty = True

    def move_node(self, name, pos):
        self.node(name)["pos"] = [float(pos[0]), float(pos[1])]
        self.dirty = True

    def set_node_setup(self, name, lines):
        self.node(name)["setup"] = list(lines)
        self.dirty = True

    def set_setup(self, lines):
        self.data["setup"] = list(lines)
        self.dirty = True

    def set_physics(self, values):
        self.data["physics"] = {**self.physics,
                                **{k: physics_value(k, v)
                                   for k, v in values.items()
                                   if k in DEFAULT_PHYSICS}}
        self.dirty = True

    def set_origin(self, origin):
        self.data["origin"] = [float(origin[0]), float(origin[1])]
        self.dirty = True

    def set_obstruction(self, a, b, db):
        self.node(a), self.node(b)
        pair = {a, b}
        self.data["obstructions"] = [
            wall for wall in self.obstructions if set(wall["between"]) != pair]
        if db:
            self.obstructions.append({"between": [a, b], "db": float(db)})
        self.dirty = True

    # ---- saving ----------------------------------------------------------

    def flush(self):
        """Write the map into the run directory, where the stations are."""
        write(self.run_dir, self.data)

    def save(self):
        """Write the design back over the scenario it came from."""
        self.flush()
        write(scenario_path(self.name), self.data)
        self.dirty = False

    def save_as(self, name):
        """Write the design to a new scenario, which becomes the loaded one."""
        check_name(name)
        if os.path.exists(scenario_path(name)):
            raise ScenarioError("there is already a scenario called %r" % name)
        self.name = name
        self.save()

    def save_snapshot(self, name):
        """Keep the whole moment: this design and every station's state."""
        check_name(name, "snapshot")
        target = snapshot_path(name)
        if os.path.exists(target):
            raise ScenarioError("there is already a snapshot called %r" % name)
        self.flush()
        os.makedirs(target, exist_ok=True)
        write(target, self.data)
        with open(os.path.join(target, ORIGIN_FILE), "w", encoding="utf-8") as handle:
            handle.write(self.name + "\n")
        copy_state(self.run_dir, target, wanted=self.nodes)

    def as_dict(self):
        """What the page is told about the scenario itself."""
        return {"name": self.name, "dirty": self.dirty,
                "origin": list(self.origin), "physics": dict(self.physics),
                "setup": list(self.setup), "kinds": list(self.kinds),
                "obstructions": [dict(w) for w in self.obstructions],
                "links": [dict(link) for link in self.links]}


# ---- loading -------------------------------------------------------------

def load_scenario(name, run_dir=RUN_DIR):
    """Put a scenario into the run directory, factory fresh.

    Nothing of the previous run's state survives: a scenario describes a
    network that has not happened yet, and loading one that quietly inherited
    the last network's identities would be a scenario that behaved differently
    on the second load than on the first.
    """
    path = scenario_path(name)
    if not os.path.isfile(path):
        raise ScenarioError("no scenario called %r" % name)
    data = read(path)
    os.makedirs(run_dir, exist_ok=True)
    wipe_state(run_dir)
    write(run_dir, data)
    return Scenario(name, data, run_dir)


def load_snapshot(name, run_dir=RUN_DIR):
    """Put a snapshot into the run directory: its map and its state.

    The loaded scenario keeps the name of the scenario the snapshot was taken
    of, not the snapshot's own. Saving after restoring a snapshot means "write
    this design back where it came from"; naming it after the snapshot instead
    wrote a new scenario nobody asked for and left the real one untouched,
    which reads exactly like Save having done nothing.
    """
    source = snapshot_path(name)
    if not os.path.isfile(os.path.join(source, SCENARIO_FILE)):
        raise ScenarioError("no snapshot called %r" % name)
    data = read(source)
    os.makedirs(run_dir, exist_ok=True)
    write(run_dir, data)
    copy_state(source, run_dir, wanted=data["nodes"])
    came_from = name
    try:
        with open(os.path.join(source, ORIGIN_FILE), encoding="utf-8") as handle:
            came_from = handle.read().strip() or name
    except OSError:
        pass            # a snapshot taken before this was recorded
    return Scenario(came_from, data, run_dir)


def create(name, run_dir=RUN_DIR):
    """Make an empty scenario, written at once so it has a file, and load it."""
    check_name(name)
    if os.path.exists(scenario_path(name)):
        raise ScenarioError("there is already a scenario called %r" % name)
    write(scenario_path(name), blank())
    return load_scenario(name, run_dir)
