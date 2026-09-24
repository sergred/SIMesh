# The ether — internals

How the medium decides who hears what, and the choices behind it.
[README.md](README.md) is what it does and the wire it speaks.

## Shape

One `asyncio.DatagramProtocol` on one UDP socket, single-threaded. Everything
is driven from arriving datagrams and from timers the event loop holds:

- a **placement** is where a station stands, in metres, and what its antenna
  adds — held whether or not that station has ever been heard from;
- a **station** is an address, per slot the last `state` message it sent, and
  the instant its own transmission stops occupying it;
- a **frame** is a transmission in flight, on the ether's clock, with the
  receivers it reached and the other frames it shared the air with.

A station is created the first time it is heard from, whatever the message, and
its address is updated on every one — so a station that restarts on a new
ephemeral port is simply followed. A placement is not created that way: it
comes from whatever is driving the run, and a station with none is not in the
medium at all.

The ether runs **inside** the testbed's process rather than beside it, so
positions arrive by method call. That is why there is no wire for them and
nothing to keep in step: dragging a station on the map is one `place()` between
two frames.

## Clocks

Three clocks meet here and only one of them is authoritative.

A station stamps its messages with its own `esp_timer`, which starts at zero
when its process does. Those numbers mean nothing between stations. So on every
`tx` the ether reads only the **offsets** — `t_pre − t0`, `t_hdr − t0`,
`t_end − t0` — and rebases them onto its own monotonic clock at the instant the
datagram arrived. The `rx_begin` it sends carries ether microseconds; the
receiver, in turn, cares only about the gaps between them and schedules from its
own clock. Each hop keeps what it can trust and discards what it cannot.

The offsets are clamped: negative is zero, anything beyond the frame's own span
is the span, and a stated timeline longer than a minute is junk and is cut. A
station cannot make the ether schedule something absurd by stating it.

## The level, and why it is computed

A link is geometry, not a number somebody wrote down:

```
L = P_tx + G_tx + G_rx − PL(d)
PL(d) = FSPL(1 m, f) + 10·n·log10(d) + X(tx, rx) + obstruction(tx, rx)
```

The free-space term is taken at the **frame's own carrier**, because a
scenario that moves band should see the difference without anything being
rewritten. The exponent is the scenario's, and it is the one knob that decides
how big a network feels: at 2.7 a station is audible for tens of kilometres and
almost everything hears almost everything, and at 3.6 the same field breaks into
neighbourhoods. That is a property of the air, not of the map, which is why it
sits in `physics:` beside the noise figure and the capture margin.

Shadowing is what the exponent alone cannot give: two pairs at one distance
that do not hear each other equally, because what stands between them is not
the same. It is one draw per pair from a normal distribution of spread
`shadowing_db`, from a hash of `shadowing_seed` and the two station numbers, so
it is the same in both directions, for every frame, and after a restart. It is
fixed for the run on purpose: a loss drawn afresh for every frame lets every
retry through in the end, which flatters exactly what a scenario is usually run
to judge. The spread scales one standard-normal draw, so two runs that differ
only in `shadowing_db` stand on the same ground, one of it rougher.

Distance is floored at one metre. Without that, two stations dropped at the
same point make `log10(0)`, and the answer a person wants there is "very loud",
not a crash.

What the levels are *for* has not changed: the medium needs to know which of two
frames is the louder one, and by how much. Computing them rather than reading
them buys the thing a testbed most wants — that moving a station changes what it
can hear, and that the change is the same change a person would reason about.

## The sensitivity threshold

`N = −174 + 10·log10(BW) + NF`, and a frame that does not clear the SNR its
spreading factor needs above it is not delivered: no `rx_begin`, nothing
scheduled, nothing to rule on.

With positions there is no such thing as no link — path loss is finite
everywhere — so something has to say where earshot stops, and the only honest
place is the noise.

It is **per spreading factor** because that is the whole point of one: SF12 buys
17.5 dB of reach over SF7 and pays for it in air time, and a flat cut would make
the two identical to the medium. A flat cut low enough for SF12 delivers frames
no SF7 receiver could demodulate, and a scenario laid out against it draws links
that do not exist and behaves far worse than it looks. The CRC band just above the
threshold, where a frame locks but fails at a probability, is the next thing this
could learn; `welcome` already carries a seed for it.

## Who hears a frame

For each other station, for each of its slots: it must be placed and above its
modem's sensitivity, it must not be transmitting itself, the slot must have last
said `RX` or `CAD`, its stated `bw`, `sf` and `sync` must equal the
transmission's, and its `freq` must be within a quarter of its bandwidth of
the transmission's. That list is one constant — the thing to extend when the
medium learns to care about coding rate, header type or preamble length.

The carrier is matched within a tolerance, not exactly, because the
synthesizer steps in 32 MHz / 2^25: two drivers asked for 869.525 MHz round it
to register values tens of hertz apart (RadioLib lands on 869 524 963 Hz, the
berlinmesh driver on 869 524 999), and an exact match makes two stations on
one channel deaf to each other. A quarter of the bandwidth is what a LoRa
demodulator tolerates. Two frames interfere on the same terms.

A slot in `CAD` is sensing, not receiving. It must be told a frame is
arriving, or a channel activity detection is blind to every frame that starts
inside its window and carrier sense says the wrong thing exactly when two
stations contend. It must not be told how the frame ended: it demodulated
nothing, so there is no verdict to rule, and a reception recorded for it would
draw a green flash for a station that only listened for energy. So its
`rx_begin` carries `"cad": true` and no `rx_end` is scheduled; whether the
slot was sensing is decided at the frame's start, from its last `state`.

Matching is on the **last stated** values, not on anything the ether infers.
This is why a station publishes a `state` on every command that changes its mode
or carrier, and why a model that forgot to would go deaf silently. The one thing
the ether does infer is a transmitter's own deafness: a `tx` is not a `state`,
so a station that never said it had left `RX` would otherwise be told about
frames arriving during its own, and a hidden terminal would look like a station
ignoring what it could hear.

## Collisions, and why each receiver rules for itself

Two frames interfere when they share a carrier and overlap in time at all. Each
frame keeps the list of frames it shared air with, and the verdict is computed
**per receiver** at that receiver's `rx_end`: the frame survives if its level
there leads every interferer that station could hear by the capture margin, and
is `crc` otherwise.

That is the difference between a medium and a referee. A collision is not a
property of a transmission — it is what happened at one antenna. Two stations
that cannot hear each other transmit over one another constantly; the station
between them keeps whichever frame is loud enough to be worth keeping, and a
station out of one transmitter's reach never notices the collision at all.

The interference list is held on the frame rather than recomputed from the
frames still in flight, so a reception that ends long after its interferer has
been pruned still knows what spoiled it. Scheduling an `rx_end` does not settle
it: a frame still in the air when a second one starts is spoiled retroactively
for receivers already told it was arriving, which is exactly what a radio does.

`capture_model: bench` swaps the margin for what a bench measured: the
reticulum project's tools/rncapture, with an SX1262 listening to an SX1262 and
an LR2021, SF7 at 125 kHz, 289 collisions. Its table is for frames that start
within one preamble of each other. Its one finding past that, six frames about
2 dB stronger landing 30 ms into a frame the listener had locked on to and
spoiling both, is extended to every later frame: the listener stays on what it
has and receives nothing else. The straight line between 2.7 and 6.1 dB is an
assumption, and so is every spreading factor but 7. Each pair is judged once,
from a draw keyed to the ether's seed, the two frames and the receiver, so the
verdict on either frame and the `takes` the receiver was told read the same
outcome.

An interferer's level is recomputed at the verdict rather than remembered from
when it was delivered. A station can be dragged across the map while two frames
are in the air, and the answer that matters is where it was when the reception
ended.

## Which frame a receiver follows

A demodulator follows one frame at a time, so when a second frame reaches a
receiver already following one, the chip needs to know whether it takes the
receiver or goes unheard. That is the question of which of the two survives,
and the ether answers it with the same rule: `rx_begin` carries `takes`. A
receiver following nothing takes the frame that reaches it; one following a
frame keeps it unless the new frame would win the pair by the rule the verdict
applies: the capture margin, or the bench's table. The ether keeps its own note
of what each receiver
follows, from the `rx_begin`s it sent and the states it was told, and a
receiver that leaves `RX` lets go. A chip that decided for itself at a margin of
its own would hand up a frame the medium had spoiled, or drop one it had kept,
whenever a scenario's margin was not the chip's.

## A frame's two names

A station numbers its own transmissions and knows nothing of anyone else's, so
two stations can have frames in the air under the same number — which happens
the moment a testbed is restarted, since every station starts counting again. So
the ether gives each frame a number of its own and uses that one in `rx_begin`
and `rx_end`. A receiver following one frame while a second arrives, and a
reader matching an end back to its beginning, both need a name that is unique
across the air, and only the medium can issue one.

## Delivery

`rx_begin` goes out immediately, so the receiver can arm its preamble, sync and
header interrupts on the offsets. `rx_end` is a timer at the frame's stated
span. Nothing re-reads the frame in between — a receiver that leaves `RX`
mid-frame is not told, and discards the reception itself.

Both carry the link's level: `rx_begin` as `level`, which is what an
instantaneous RSSI reads and what carrier sense acts on, and `rx_end` as `rssi`
with `snr` the same figure above the noise floor. Both are rounded to whole dB
on the wire, because the station reads them as integers; the verdict is decided
on the unrounded figures, so a pair 6.4 dB apart captures and a pair 5.6 dB
apart does not, whatever the numbers the receiver is shown.

The payload is passed through as the base64 string it arrived as. The ether
never decodes it, which is what keeps it honest: it cannot accidentally know
anything about Reticulum.

## Events, and the rule about subscribers

`on_tx`, `on_rx` and `on_station` exist so something can watch the air without
reading the record — the map, at sixty frames a second. They are plain
callables, and an exception from one is logged and swallowed. A page that has
gone away, or a subscriber with a bug in it, must not be able to stop the
medium: the ether's job is the frames, and everything watching is optional.

## What is deliberately not here

- **Fading and per-frame variation.** A level is computed once, from the
  geometry and the pair's shadowing, and is the same for every frame between
  one pair. No multipath, no antenna pattern, no rain.
- **The CRC band.** The threshold is the spreading factor's own, and above it a
  frame is delivered. A real receiver also has a few dB above that threshold
  where a frame locks but fails its CRC at a probability. `welcome` already
  carries a `seed` so that band, when it arrives, has a reproducible generator
  to draw from.
- **Orthogonality.** Two frames on one carrier interfere whatever their
  spreading factors, though a real receiver can often demodulate through a
  frame at another SF. The medium is pessimistic here, and knowingly.
- **A referee.** The ether does not judge a station's behaviour — it does not
  check that a transmission was preceded by carrier sense, or that a duty cycle
  was respected. The record is there so something else can.
- **Virtual time.** It runs on the event loop's real clock. Stations are
  processes in real time and a person is in the loop.
