# The station contract

What SIMesh gives every station process, and what it expects back. A firmware
that keeps this contract, and links `radio/` below its radio driver, runs on
the testbed; what differs between firmwares beyond it is a **kind**
(`testbed/kinds/`).

## The environment

| Variable | Meaning |
|---|---|
| `SIMESH_NODE_ID` | a small integer, unique on the host; the last byte of any MAC the station makes, and the `sid` it gives the ether |
| `SIMESH_NODE_DIR` | the station's directory; its working directory; its state lives under `state/` |
| `SIMESH_BIND_ADDR` | its own loopback address, fixed by its id in the testbed's network (`simd --net`, a /22 holding 1000 stations by default); every socket it opens binds here |
| `SIMESH_ETHER` | `host:port` of the ether |
| the kind's `env:` | anything the binary needs beyond that |

Nothing else is promised. A station reads its identity from these and from
nowhere else, so two stations on one host never collide.

## The process

- **stdin and stdout are the console**: a pty, text, shown to a person in the
  map's console window and appended to `log` in its directory. Nothing binary
  goes there.
- **Exit to reboot.** The supervisor starts the binary again, on the same
  directory and address, half a second later. A station that wants to reboot
  exits.
- **State is its directory.** Loading a scenario empties `state/`; a factory
  reset empties it; a reset leaves it. Whatever marks "this directory has been
  set up" is the kind's to name.
- **Ports are its own**, on its own address. Which ones, and what answers on
  them, is the kind's to say.

## The radio

The station links `radio/` (`simradio.h`) and calls
`simradio_station_open(SIMESH_NODE_ID, SIMESH_BIND_ADDR, SIMESH_ETHER)` once,
then `simradio_open(slot, …)` per radio. Its driver talks to the chip model
frame by frame, exactly as to an SX1262 on a bus.

The medium matches receivers on carrier, bandwidth, spreading factor and sync
word, and **does not model preamble length**: two radios whose preambles
differ hear each other here and may not on a bench. Set them equal in a
scenario that means to say anything about hardware.

## A kind

A scenario names its kinds under `kinds:`; a kind is a class in
`testbed/kinds/` that says, for one firmware:

| | |
|---|---|
| `env` | the environment above, plus what that firmware reads |
| `wait_up` | when a started station counts as up |
| `run` | how one setup line, or one **Run command** line, is put to it |
| `flush` | how to make what it was told durable, before a stop or a snapshot |
| `transport` | whether it forwards for others, or unknown |
| `web_port` | the port of its web UI, or none |
| `configured` | whether its directory has been set up already |
