# SIMesh

A real-time LoRa testbed. Stations are real firmware, built as Linux
processes, each on its own loopback address; below their radio driver sits a
model of an SX1262 on a virtual SPI bus, and between the models sits one
medium, the ether, that decides who hears what. A map in the browser places
the stations and shows every frame on the air.

```
                    container                                        browser
python3 simd.py ─┬─ ether        (UDP, in-process)
                 ├─ stations     (firmware processes, one pty each)
                 ├─ proxy        <name>.sim.localhost:9011 ─► <the station's address>:80
                 └─ control      localhost:9011  ◄──── websocket ──── the map
```

| Piece | Where |
|---|---|
| the medium | [`ether/`](ether/README.md) |
| the chip model, as a C library a station links | `radio/` |
| the testbed process, the map, the scenarios | `testbed/` |
| what a station process is promised and owes | [STATION.md](STATION.md) |

It is the firmware under test, built for a different target — not an emulator.
[INTERNALS.md](INTERNALS.md) says how it works and why it is built this way.

## Station kinds

Stations of different firmwares share one ether and one map. Each firmware is
a **kind**: the binary, and how the testbed talks to it (`testbed/kinds/`).
Two exist:

| Kind | The firmware | Up when | Setup lines are | Web UI |
|---|---|---|---|---|
| `reticulous` | Reticulous (spangap/reticulous), built for `hw-linux` | its TCP CLI on `:8081` answers | its CLI, typed over `:8081`, then `save` | port 80 |
| `berlinmesh` | Sergeyculum, the Rust Reticulum stack at [git.emcomm.cc/berlinmesh/reticulum](https://git.emcomm.cc/berlinmesh/reticulum), as its `fw/simesh` target | its `kiss` pty answers `rncfg detect` | `rncfg` without program and port: `name set {name}` runs `rncfg name <dir>/kiss set <name>` | none |

Sergeyculum is a working name; the project calls itself `reticulum` and the
kind is named after its repository.

A scenario that names no kinds has one, `reticulous`, from simd's `--elf` and
`--fixed`.

## The build

The testbed builds no firmware. Build the reticulous station binary once,
for the `spangap/hw-linux` board, from the workspace root:

```sh
spangap build reticulous/reticulous --with spangap/hw-linux \
    --with reticulous/netgraph \
    -x reticulous/rnsh -x reticulous/iface-auto -x reticulous/iface-ble \
    -x reticulous/rnode-ble -x reticulous/nomad -x reticulous/maps \
    -x spangap/viewer -x spangap/acme -x spangap/duckdns -x spangap/sshd \
    -x spangap/upnp -x spangap/wg
```

The excluded straddles are the ones not built for this target. netgraph is not
in the image by default, but a scenario whose stations share a community
(`s.netgraph.community`) needs it: the community's membership announce is what
carries each node's gateway distance. The result is
`reticulous/esp-idf/build.linux/reticulous.elf` in the workspace, which is
what a station is, with its `/fixed` tree in
`reticulous/esp-idf/build.linux/data_merged/`.

Every target builds in its own `esp-idf/build.<target>/`, so a chip build
(`build.esp32s3/`) and this one never touch each other's files, and switching
between them rebuilds nothing.

A `berlinmesh` station and its tool are built in the Sergeyculum tree
(`sergey/reticulum` in this workspace), with Rust:

```sh
cd sergey/reticulum/fw/simesh && cargo build --release    # fw/simesh/target/release/simesh
cd sergey/reticulum && cargo build --release -p rncfg     # target/release/rncfg
```

## Running it

The page is built once, and again when its sources change:

```sh
cd SIMesh/testbed/ui && npm install && npx quasar build
```

Then, in the container, the testbed is one process:

```sh
cd SIMesh/testbed && python3 simd.py
```

That starts the ether, the stations, the proxy and the control page, on
`http://localhost:9011/`. It runs in the foreground: Ctrl-C stops it, and
stops everything it started. It takes `--bind` (default `0.0.0.0:9011`),
`--ether` (default `127.0.0.1:7000`), `--elf` and `--fixed` (the reticulous
binary and its `/fixed` tree, defaulting to the build above), `--stagger`,
and `--net`.

**Two testbeds on one host** need their own port, their own ether and their
own station addresses, because every station binds its own address and two
stations on one address are one port taken twice:

```sh
python3 simd.py --bind 0.0.0.0:9012 --ether 127.0.0.1:7001 --net 127.0.4.0/22
```

takes its addresses from `127.0.4.0/22` instead of `127.0.0.0/22`. A network
is filled one /24 at a time with hosts 5 to 254: node 1 is the network's
first `.5`, node 250 its `.254`, node 251 the next /24's `.5`. The default
/22 holds 1000 stations; a wider network holds more.

## The map

The page opens empty. **Scenario ▸ New…** makes a network, **Load…** opens one
that is already on disk.

| | |
|---|---|
| drag the background | pan |
| wheel | zoom about the cursor |
| right-click the background | **New node here** — asks a name and puts a station down |
| hover a station | the lines to everything within earshot, with the level on each |
| drag a station | move it; the medium is updated as you drag, so you can watch a link fade |
| click a station | its card: its kind, status, position, transport, radio, and **Web UI** (for a kind that has one), **Console**, **Reset**, **Factory reset**, **Setup**, **Remove** |

A dot is grey stopped, amber starting or in setup, white up, red restarting. A
second ring around it means the station is acting as a Reticulum transport
node — read live from the station itself, so flipping it in that station's own
web UI shows here.

A transmission draws an expanding ring from the transmitter for as long as the
frame occupies the air. Each station that hears it flashes green for a clean
reception and red for a CRC failure — so a collision is two rings overlapping
and a row of red flashes.

The grid is in metres, at 1, 2 or 5 times a power of ten, whichever keeps the
lines 60 to 150 pixels apart. The brighter cross is the scenario's origin, and
the view is remembered per scenario.

## A scenario, and a snapshot

Two things are worth keeping, and they are kept apart.

A **scenario** is the network as designed and nothing that has happened to it:
where the stations stand, what the air is like, and the CLI lines each of them
is set up with. It is one file.

```
testbed/scenarios/<name>.yaml
```

A **snapshot** is a scenario plus everything the stations have since become —
their identities, keys, paths and message history, as the firmware keeps them.

```
testbed/snapshots/<name>/scenario.yaml
testbed/snapshots/<name>/nodes/<node name>/state/
```

Loading a scenario gives a factory-fresh network, reproducible from a file you
can read. Loading a snapshot gives back a network that had been running.

```yaml
origin: [52.3740, 4.8897]           # lat, lon of the map's centre
physics: { exponent: 2.7, noise_figure_db: 6, capture_db: 6 }
setup:                              # CLI lines every station is given, in order
  - "hostname {name}"
  - "auth passwd admin admin"
  - "lxmf create {name}"
  - "lora up"
  - "lora 0 freq 869.525"
  - "lora 0 sf 8"
  - "lora 0 bw 125"
nodes:
  alpha:
    id: 1
    pos: [52.3740, 4.8897]
    setup:                          # this station's own lines, after the scenario's
      - "set s.rnsd.transport_enabled 1"
  bravo:   { id: 2, pos: [52.3740, 4.9030] }
  charlie: { id: 3, pos: [52.3740, 4.9295] }
obstructions:
  - { between: [alpha, charlie], db: 60 }
```

**A node is a name and a number.** The name is the station's hostname, the
label on the map and the hostname the proxy routes; the number is its
`SIMESH_NODE_ID`, which fixes its loopback address in the testbed's network
(node 1 is `127.0.0.5` under the default). The proxy answers to both,
so `alpha.sim.localhost` and `1.sim.localhost` are the same station.

**Positions are latitude and longitude in degrees.** The ether projects them to
metres on an equirectangular plane around `origin`, so a scenario placed on
real ground needs only its origin moved.

### Kinds in a scenario

A scenario that mixes firmwares names them under `kinds:`; a node says which
it is with `kind:`, and a node that says nothing is of the first kind:

```yaml
kinds:
  reticulous:                         # the first: the default, and scenario `setup:` is its
    elf: ../../../reticulous/esp-idf/build.linux/reticulous.elf
    fixed: ../../../reticulous/esp-idf/build.linux/data_merged
  berlinmesh:
    elf: ../../../sergey/reticulum/fw/simesh/target/release/simesh
    tools: { rncfg: ../../../sergey/reticulum/target/release/rncfg }
    setup:                            # every node of this kind, before its own lines
      - "set --freq-hz 869525000 --sf 8 --bw-hz 125000 --cr 5"
      - "name set {name}"
nodes:
  alpha:  { id: 1, pos: [52.3740, 4.8897] }
  sergey: { id: 2, kind: berlinmesh, pos: [52.3740, 4.9030] }
```

`testbed/scenarios/mixed.yaml` is such a scenario: three `reticulous`
stations 1 km apart in a line with transport on, a `berlinmesh` station 50 m
beyond each end, and 80 dB between those two. The Reticulous stations are set
to Sergeyculum's sync word (`lora 0 sync 0x12`) and preamble
(`lora 0 preamble 18`), without which the ether delivers nothing between the
two kinds.

Every kind takes `elf:` (the binary), `env:` (extra environment) and
`setup:`; `type:` picks the class and defaults to the kind's name, so two
builds of one firmware can be two kinds of one type. `reticulous` also takes
`fixed:`, `berlinmesh` takes `tools: { rncfg: }` (else `rncfg` on `PATH`).
Paths are relative to `testbed/scenarios/`, where scenario files live — a
snapshot's copy is read as if it lived there too — and an `env:` value is a
path only when it starts with `./` or `../`. Ids are unique across kinds:
two stations on one id would be one station to the ether.

### A mixed run, step by step

What `mixed.yaml` shows once its five stations are up, and the command that
shows it. `rncfg` is Sergeyculum's tool; `<kiss>` is `run/nodes/<name>/kiss`.
A Reticulous station answers on its TCP CLI, port 8081 on its own address
(`{addr}` in a setup line, `127.0.4.5` for node 1 under the `--net` below),
or through **Run command** on the page.

| What | How to see it |
|---|---|
| announces cross both ways | `seq.py --only ANNOUNCE`: every station's announce reaches its neighbours of the other kind. A Sergeyculum announce is 199 B and reads as `?<hash>` because its app name is not one `seq.py` knows |
| a path forms through the Reticulous transports | `rncfg heard <kiss>` on each Sergeyculum station lists the other at `hops 2`; the Reticulous stations repeat Sergeyculum announces as `via <fp> hops=1` in the record. The repeat follows the Reticulous announce schedule, so allow a minute |
| a packet routes through | `rncfg send <kiss> <lxmf.delivery of the other> <text>` (48 B at most; longer needs a link). The record shows the Sergeyculum frame, the Reticulous forward at `hops=1`, the Sergeyculum proof. `rncfg mbox <kiss> count` at the far end |
| two-frame splits both ways | a 260 B Sergeyculum packet leaves as `254B split 1/2` + `6B split 2/2` and all three Reticulous stations reassemble it; `lxmf send <a Sergeyculum lxmf.delivery> <180 chars>` at a Reticulous station is 291 B on the wire and goes the same way. Over 500 B Reticulous LXMF opens a link and offers a resource instead |
| CSMA under contention | count overlapping `tx` spans in `record.tsv` by kind pair. Across kinds, overlaps are starts within one preamble of each other, which no listener can see, plus a frame that starts straight after a station's own transmission ends |
| the hidden terminal | `rncfg announce <kiss>` at both Sergeyculum stations in one instant: alpha keeps sergey1's frame, charlie keeps sergey2's, bravo in the middle keeps neither |

Three things the two stacks do differently, all visible in the record:

- **Sergeyculum receives no resources.** A link to it establishes, and every
  resource advertisement is answered with a resource cancel. A Reticulous
  LXMF message over the 500 B packet limit is therefore never delivered to a
  Sergeyculum station; under it, it is.
- **A proof is forwarded only by the transport that forwarded the packet.**
  When a Sergeyculum proof reaches a Reticulous transport that did not carry
  the packet, it ends there, and the sender gives up on the message after its
  timeout. A frame lost to a collision on the way is the usual cause.
- **Reticulous SUPE announces are foreign frames to a Sergeyculum build
  without SUPE**, and the reverse. The Sergeyculum log counts them as
  `frame(s) dropped — first byte 0xc3 is not our framing`; the type byte is
  disjoint from the split framing by design.

### Setup lines

They are what you would type at the station — CLI commands for `reticulous`,
`rncfg` verbs for `berlinmesh` — so every setting the firmware has or grows is
reachable without the testbed knowing its name. A station of the first kind
gets the scenario's `setup:`, then its kind's, then its own; a station of any
other kind gets its kind's lines and its own, because a shared line is in one
firmware's dialect and means something else, or nothing, in another's.

Three macros are filled in per station, which is what lets one shared list say
node-specific things:

| Macro | Becomes |
|---|---|
| `{name}` | the node's name — `alpha` |
| `{id}` | its station number — `1` |
| `{addr}` | its loopback address — `127.0.0.5` |

Anything else in braces is left exactly as written. The file is the whole of
what a station is told: nothing is added behind your back, which is why
`hostname {name}` is an ordinary line you can see and change.

The lines run **when a station boots with no state** — a node you have just
clicked onto the map, and every node after a factory reset. They do not run
again on an ordinary reset, because the station is already set up.

The one line that is not a setting is `lxmf create {name}`: run twice it makes
two identities. That is safe here because the lines only ever run on an empty
station, but it is worth knowing before putting it through **Run command**.

## The verbs

**On one station** (its card) **and on the whole testbed** (the Simulation menu):

| | |
|---|---|
| **Reset** | presses reset. The process exits and comes straight back; its state is untouched, so it is the same station it was. |
| **Factory reset** | wipes its state, restarts it, and the setup lines run again on the empty store. Identities, keys, paths and message history go; the map and the lines do not. |

Across the whole testbed both are spread over the same `--stagger` window the
start uses, and for the same reason: every station announces itself as it comes
up, and two dozen of those at once is a collision storm no fleet of real boards
would ever have.

**Simulation ▸ Run command…** types one line at every running station of one
kind — the dialog asks which when the scenario has more than one — and lists
what each one said. The macros are expanded per station, so
`lora 0 freq 869.475` retunes every `reticulous` station and `rns` surveys
them, and `announce now` at the `berlinmesh` kind makes every one of those
announce. This is the general tool: there is no separate verb for re-sending
the setup lines.

Beside the line is **spread**, in seconds. Left at 0 every station is asked at
once, which is what a question wants — nothing goes on the air to answer
`show s.net.hostname`. Anything that *transmits* wants a spread: two dozen
stations running `lora 0 a` in the same instant is a collision storm rather
than an announcement, and what comes back describes the storm. Thirty or sixty
seconds across the fleet is enough.

**Scenario** — New, Load, Save, Save As, and Settings (the air and the setup
lines). **Snapshot** — Load and Save As.

| Verb | What it does |
|---|---|
| **Scenario ▸ New** | an empty scenario, written at once so it has a file |
| **Scenario ▸ Load** | the design only: every station comes up with no state and runs its setup lines |
| **Simulation ▸ Start all** | start whatever is stopped |
| **Scenario ▸ Save** | write the map and the lines back over the scenario |
| **Snapshot ▸ Save As** | keep this design *and* every station's state under a new name |
| **Snapshot ▸ Load** | bring both back — the network as it was |

Stations keep running across a snapshot: they are flushed first and the copy is
taken while they run, which for a store that commits whole files is the same
guarantee a power cut gives a board. Loading either kind stops them.

The scenario is **dirty** from the first change after a load or a save — a
moved node, a new one, a removed one, a physics change. The header shows a dot
beside the name, and Load and New ask before discarding it. State is not part
of that: a station writing to its store is not a change to the design.

### The run

The live run is neither a scenario nor a snapshot. It is `testbed/run/`:

| Path | What it is |
|---|---|
| `run/scenario.yaml` | the live map, written on every change |
| `run/nodes/<name>/state/` | the station's state store |
| `run/nodes/<name>/log` | everything the station wrote to its console, across restarts |
| `run/record.tsv` | every message in and out of the ether |

Logs and the record are the run's own and are never copied into a save.
`testbed/scenarios/`, `testbed/snapshots/` and `testbed/run/` are all on the
workspace bind mount, so they survive the container and can be copied about.
Snapshots and the run are not committed; the scenarios committed here are
examples.

### Starting a fleet

Stations are started **spread over a minute**, not all at once. Two dozen
firmware processes forking in the same instant is a thundering herd against
one host, and the network it makes is worse than the load: stations that boot
together announce together, so the opening minute is a collision storm that no
fleet powered up by hand would ever have. `simd --stagger <seconds>` changes
the spread; `0` starts them together.

The map fills in as they come up, and the page stays live throughout — the
start runs behind the menu rather than holding it.

## Who can hear whom

There are no stated links. Where a station stands is the whole of it: the level
a frame arrives at is

```
L = P_tx + G_tx + G_rx − PL(d)
PL(d) = FSPL(1 m, f) + 10·n·log10(d) + obstruction(tx, rx)
```

with `n` the scenario's exponent — 2 is free space, 2.7 suburban, and higher
numbers bring the neighbourhoods in closer. A frame that arrives below the SNR
its spreading factor needs — −7.5 dB at SF7, down to −20 dB at SF12 — is not
delivered at all, and that is what "out of range" means here. So a link is in
range only if it is one the modem could actually hold, and moving to a slower
spreading factor really does reach further. An **obstruction** is a per-pair constant in dB, which is how two
stations near each other are put out of each other's reach.

Two frames that share a carrier and any instant of air interfere, and each
receiver rules on them for itself: a frame survives where it leads everything
else that station could hear by the capture margin.
[`ether/README.md`](ether/README.md) has the whole of what the medium
decides.

## Reading the record

`testbed/seq.py` draws `run/record.tsv` as a sequence diagram — one lifeline per
station, one arrow per station that heard a frame, the verdict at each arrow
head, and the Reticulum packet read out on the right:

```
$ python3 seq.py --tail 4
   t (s)   delta    india    kilo     mike     papa    sierra
   0.118     │        ◀────────┼────────┼────────┤        │  ANNOUNCE  single/4e3874cc  of rnstransport.probe  hops=0  167B
             │        │        │        │        ├────────▶
   0.250     ✗────────┼────────┼────────┼────────┼────────┤  ANNOUNCE  single/edec275b  of rnstransport.probe  hops=0  167B
             │        │        │        │        ✗────────┤
```

One transmission heard by two stations is two arrows on two rows, sharing the
timestamp and the reading: every arrow has one end at the station that
transmitted, so nothing in the picture can be read as a frame travelling
between two stations that cannot hear each other.

The lifelines are named from the loaded scenario. `--only <words>` keeps the
rows whose reading matches, `--record <file>` reads a record kept from an
earlier run, and `--scenario <path>` names a different one.

## A station's own doors

A `reticulous` station serves its web UI on port 80 of its own loopback
address, which is invisible outside the container; the proxy on the published
port 9011 routes by hostname (a station of a kind with no web UI is refused
with a sentence saying so, and its card has no **Web UI** button):

    http://alpha.sim.localhost:9011/    by name
    http://1.sim.localhost:9011/        by number

Chrome and Firefox resolve any `.localhost` name to loopback with no
configuration. Safari does not, and needs entries in `/etc/hosts` on the Mac:

    127.0.0.1  alpha.sim.localhost bravo.sim.localhost charlie.sim.localhost

Port 9011 is the testbed's own: the container publishes it at the same number
on the host, beside flashmon's 9010 and clear of the 9000–9009 range `spangap
dev` allocates from, so a testbed and a dev server can run at once. A container
made before that mapping existed does not have it — the next host `spangap`
command recreates it, which costs nothing since all state is in bind mounts.

A station's web UI is the **whole** UI, not a static shell: it speaks the same
WebRTC DataChannel to the browser that a board does, from the same firmware
source, so the live panes — settings, the log, the CLI, Activity — all work.

Getting that through takes one piece of plumbing, because the DataChannel is
UDP and the station's own address is inside the container:

```
browser ──ws  <name>.sim.localhost:9011/webrtc──► simd ──ws──► station   signalling
browser ──udp localhost:9011───────────────────► simd ──udp─► station   the channel
```

simd stands in the middle of the signalling so it can point the station's SDP
answer at itself, and relays the UDP behind it — picking the station out of
each packet by the ICE ufrag it saw in that answer. Port 9011 is published on
**UDP as well as TCP** for it; a container made before that mapping existed is
recreated by the next host `spangap` command.

Neither end knows. The station is answering ICE from a peer that happens to be
a relay, and the browser is talking to a station that happens to be simulated —
which is the point: the code under test is the shipping code, on both sides.

Besides the map, a station is reachable three other ways:

- **Console** on its card — its serial console, in a terminal window over a
  websocket. For `reticulous`, first-run setup and every CLI command, exactly
  as a board on a cable; for `berlinmesh`, its log lines.
- Its kind's own door, from a shell in the container: `nc <addr> 8081`
  is a `reticulous` station's TCP CLI, and `rncfg <verb> run/nodes/<name>/kiss`
  talks KISS to a `berlinmesh` one, exactly as over USB. These are the doors
  simd itself uses for setup.
- `tail -f run/nodes/<name>/log` — everything it has printed, across restarts.

A station that exits is started again, because a restart on this target is a
process exit: a station rebooting itself comes back on the same address with
the same directory.

## After a container restart

Stations are processes, not a service: stopping the container stops them.
Their state is not in the container though — `scenarios/`, `run/` and the
station binary all live in the workspace, which is a bind mount from the host
— so start `simd.py` again, then **Load** the scenario, and the network comes
back with its names, radio settings, identities and message history as they
were.

## The pieces on their own

The medium is its own program with its own docs:
[`ether/README.md`](ether/README.md) for what it does and the wire it
speaks, [`ether/INTERNALS.md`](ether/INTERNALS.md) for how. It runs alone
against hand-written stations, taking its positions from a scenario file:

```sh
python3 ether/ether.py --bind 127.0.0.1:7000 --record record.tsv \
        --scenario testbed/scenarios/<name>.yaml
python3 -m pytest ether/test_ether.py      # its own tests need no firmware
```

The proxy also runs on its own, for a station set started some other way; alone
it routes by station number only, since names are the scenario's:

```sh
python3 testbed/proxy.py --bind 0.0.0.0:9011
```

## The chip library

`radio/` is the SX1262 model and the station's link to the ether, behind a
C ABI of eight functions (`radio/include/simradio.h`): open the link, open a
chip per radio slot, hand it SPI frames, pulse its reset, read its lines. A
station of any language links it in place of a radio, below an unchanged
driver.

The model reaches its host through six services — a clock, one-shot timers, a
recursive lock, a UDP socket, a reader, a log — and two backends supply them:

| Backend | For |
|---|---|
| `radio/backend/posix/` | a plain process: `std::thread`, `CLOCK_MONOTONIC`, a `std::recursive_mutex` |
| `radio/backend/esp-idf/` | an ESP-IDF firmware built for the Linux host target: esp_timer, a FreeRTOS critical section and task; an IDF component |

```sh
cd SIMesh/radio && cmake -B build && cmake --build build   # libsimradio.a, libsimradio.so
python3 -m pytest SIMesh/radio/tests SIMesh/ether SIMesh/testbed   # every test here; none needs firmware
```

The model's tests load `libsimradio.so` with ctypes, drive it frame by frame
the way a driver does, and play the ether on a UDP socket of their own.

The ESP-IDF backend is proved by a throwaway project that links it against
the IDF host port and sends one frame (`radio/tests/esp-idf-link/`; the
commands are at the top of its `CMakeLists.txt`).

The reticulous firmware in this workspace still carries its own copy of the
model in `iface-lora/esp-idf/src/host/`, listed below; it does not link this
library yet.

## Where the code lives

Two halves. The **host port** is what makes a firmware build and run as a
process at all; the **simulation** is what gives it a radio and a medium.

### The host port of reticulous

It lives with that firmware, not here:

| Where | What |
|---|---|
| [`spangap/build-system`](../spangap/build-system/README.md) | a board straddle's `target:`, exported as `IDF_TARGET`; on `linux`, no flashable image |
| [`spangap/hw-linux`](../hw-linux/README.md) | the board: station identity and directory, the GPIO shim, esp_timer |
| `spangap-core/esp-idf/src/host/` | no power manager, no USB transport, and deflate over the system zlib |
| `spangap-net/esp-idf/src/net_relay.cpp` | the socket relay, shared with the chip: the event bus, the listen sockets, the byte proxy |
| `spangap-net/esp-idf/src/host/` | the link backend — loopback, up from the first instant — in place of the WiFi state machine |
| `spangap-web/esp-idf/src/host/` | the WebRTC half's boot hook, and nothing behind it |

Everywhere else the rule is the same: chip-only code sits behind
`#if !CONFIG_IDF_TARGET_LINUX` or drops out of the source list, and host-only
code lives in that component's `src/host/`.

### The simulation

| Where | What |
|---|---|
| `radio/` | the chip and the station's UDP link to the ether, as a C library |
| `iface-lora/esp-idf/src/host/virtual_sx126x.*`, `ether_task.*` | the reticulous firmware's own copy of the chip and the link, which its build links today |
| `iface-lora/esp-idf/src/host/virtual_hal.*` | RadioLib's HAL over the GPIO shim and that model, in place of the SPI bus |
| [`ether/`](ether/README.md) | the medium: positions, path loss, who hears a frame and how it comes out |
| `testbed/simd.py` | the process: the ether, the stations, the proxy, the control server |
| `testbed/stations.py` | one firmware process, its pty, its log, its supervisor |
| `testbed/kinds/` | one class per firmware: its environment, when it is up, how it is set up and asked things |
| `testbed/scenario.py` | the scenario directory: load, save, save as, reload, new |
| `testbed/setup.py` | a `reticulous` station's TCP CLI, which its kind speaks |
| `testbed/proxy.py` | the hostname proxy |
| `testbed/ui/` | the control page (Quasar 2 on Vue 3, one Pinia store) |
| `testbed/seq.py` | the record as a sequence diagram |

Nothing above the bus is aware of any of it: the LoRa driver, its CSMA and
airtime accounting, Reticulum, LXMF and the web UI are the same code that runs
on a board. The same holds for a `berlinmesh` station: its SX1262 driver,
`LoRaIface` and engine are Sergeyculum's own, unchanged, over an embedded-hal
bus that ends in `radio/`.
