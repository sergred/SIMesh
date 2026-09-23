/**
 * model — the SX1262 behind the C ABI.
 *
 * The driver above it is the one that runs on hardware, unchanged: it writes
 * the same opcodes, reads the same registers, polls or waits on the same DIO1
 * line and reads the same IRQ bits. What this supplies is the other end of the
 * bus — a command interpreter, a register file, a payload buffer, and the
 * timing of a frame, which is where a radio actually lives.
 *
 * A frame in flight is three instants: the end of its preamble, the end of its
 * header, and the end of the frame. The model schedules the receiver's
 * interrupts on those instants with one-shot timers, so a driver that disables
 * its interrupt, drains, and re-enables sees exactly the edges it sees on a
 * board.
 *
 * What is between two radios is the ether (ether_link.cpp): the model hands it
 * every transmission and every change of mode or carrier, and is handed back
 * the frames that reach its antenna.
 */
#pragma once

#include <stddef.h>
#include <stdint.h>

/** A frame arriving at this receiver: when its stages land, relative to the
 *  `t0` the sender stamped, and how strongly it arrives. */
struct VirtualRxBegin {
    int     id;
    int64_t t0, tPre, tHdr, tEnd;   /* the sender's own microsecond stamps */
    int     levelDbm;
};

/** The same frame, finished: what it carried and how it came out. */
struct VirtualRxEnd {
    int            id;
    const uint8_t* payload;
    size_t         len;
    bool           crcOk;       /* false when the ether's verdict is not clean */
    bool           headerOk;    /* false when the header itself did not survive */
    int            rssiDbm;
    int            snrDb;
};

/** The noise floor a receiver reports when nothing is arriving. */
constexpr int kNoiseFloorDbm = -110;

/** How far a frame must lead one already being demodulated to take the
 *  receiver off it. The medium decides the same question with the same
 *  margin when it rules on a frame that shared the air. */
constexpr int kCaptureDb = 6;

/** The chip for a slot, if one has been opened. */
struct simradio* modelChip(int slot);

void modelRxBegin(struct simradio* chip, const VirtualRxBegin& f);
void modelRxEnd(struct simradio* chip, const VirtualRxEnd& f);
