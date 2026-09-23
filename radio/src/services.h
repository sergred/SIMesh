/**
 * services — the six things a host supplies to the model.
 *
 * The model and its ether link are written against this and nothing else, so
 * the same sources build for ESP-IDF's host target (backend/esp-idf) and for a
 * plain POSIX process (backend/posix). Each backend defines
 * `simradio_services()` and nothing more.
 *
 * The rules every backend keeps, because the model relies on them:
 *
 * - the lock is recursive: a timer callback that took it can call into code
 *   that takes it again;
 * - a timer callback runs with the lock NOT held; the model takes it itself;
 * - `timer_start_once` on a timer that is already running restarts it from
 *   now, and `timer_stop` on one that is not running does nothing;
 * - `now_us` is monotonic, zero near the process's start.
 */
#pragma once

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

enum { SIMRADIO_LOG_ERROR = 1, SIMRADIO_LOG_WARN = 2, SIMRADIO_LOG_INFO = 3 };

struct simradio_services {
    int64_t (*now_us)(void);
    void*   (*timer_create)(void (*cb)(void*), void* arg, const char* name);
    void    (*timer_start_once)(void* timer, int64_t delay_us);   /* restarts if running */
    void    (*timer_stop)(void* timer);
    void    (*lock)(void);                                        /* recursive */
    void    (*unlock)(void);
    int     (*udp_open)(const char* bind_addr, const char* dest_host_port);  /* connected, non-blocking fd; -1 on failure */
    int     (*spawn_reader)(int fd, void (*on_datagram)(const char*, size_t)); /* 0 on success */
    void    (*log)(int level, const char* fmt, ...);
};
const struct simradio_services* simradio_services(void);   /* the backend provides it */

#ifdef __cplusplus
}
#endif
