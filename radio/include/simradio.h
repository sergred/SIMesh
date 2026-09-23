/**
 * simradio — an SX1262 on a virtual SPI bus, and the station's link to the
 * ether.
 *
 * A station links this library in place of a radio. Its driver hands each SPI
 * frame to `simradio_transfer` exactly as it would put it on a bus, and reads
 * the reply the datasheet describes; the model times every frame on the air,
 * raises the IRQ bits a real chip raises, and tells the ether what it is doing.
 * The ether (SIMesh/ether) decides who hears what and hands the frames that
 * reach this antenna back.
 *
 * Thread-safe throughout. Nothing here blocks the caller beyond the model's
 * own short critical section.
 */
#pragma once

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif
typedef struct simradio simradio_t;

enum { SIMRADIO_PIN_DIO1 = 1, SIMRADIO_PIN_BUSY = 2 };

/* The station's one link to the ether. Idempotent. An empty `ether_addr`
 * means "no ether": models exist, transmissions go nowhere. */
int simradio_station_open(int sid, const char* bind_addr, const char* ether_addr);

/* One chip per radio slot. `on_pin(ctx, pin, level)` runs on whatever
 * thread moved the line: the bus caller, the timer thread, or the reader. */
simradio_t* simradio_open(int slot, void (*on_pin)(void*, int, int), void* ctx);

/* One complete SPI frame, NSS low to NSS high. out[0] is the opcode; `in`
 * is the status byte until the data starts, then the data. Thread-safe. */
void simradio_transfer(simradio_t*, const uint8_t* out, size_t len, uint8_t* in);

/* The RST line's rising edge. */
void simradio_reset(simradio_t*);

/* What the model drives on a line right now. */
int simradio_pin(simradio_t*, int pin);

/* Microseconds on the model's clock, for a host that wants to log against it. */
int64_t simradio_now_us(void);

void simradio_close(simradio_t*);
#ifdef __cplusplus
}
#endif
