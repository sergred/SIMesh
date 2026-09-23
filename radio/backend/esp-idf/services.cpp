/**
 * The services on ESP-IDF's Linux host target: esp_timer, a FreeRTOS critical
 * section, and a FreeRTOS task over a non-blocking socket.
 *
 * The rules of that port apply, and each one is a way it breaks:
 *
 * - no FreeRTOS task blocks in a host system call. The port only knows a task
 *   is blocked when it blocked on a FreeRTOS primitive; a task sitting in
 *   recv() is, to the scheduler, the running task. So the socket is
 *   non-blocking and the reader's only wait is a select() with a one-tick
 *   timeout, which IDF interposes: it polls, then sleeps on a delay;
 * - every task stack is at least 20 KB and has no core affinity: a task is a
 *   pthread with a real mapping, and there is one core;
 * - one critical section for everything. On this port it nests (a
 *   per-thread signal mask with a global count), which is the recursive lock
 *   the model needs, and contention between chips is nil.
 *
 * Logging goes to ESP-IDF's log under the tag `simradio`, which is where the
 * firmware's own logger reads everything else.
 */
#include "services.h"

#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include <arpa/inet.h>
#include <cerrno>
#include <cstdarg>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <netinet/in.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <unistd.h>

namespace {

constexpr int      kMaxDatagram = 4096;
constexpr uint32_t kTaskStack   = 32768;
constexpr int      kTaskPrio    = 6;      /* above a radio task at 5 */

const char* const kTag = "simradio";

portMUX_TYPE s_mux = portMUX_INITIALIZER_UNLOCKED;

int64_t nowUs() { return esp_timer_get_time(); }

void* timerCreate(void (*cb)(void*), void* arg, const char* name)
{
    esp_timer_create_args_t a = {};
    a.callback = cb;
    a.arg = arg;
    a.name = name;
    esp_timer_handle_t h = nullptr;
    if (esp_timer_create(&a, &h) != ESP_OK) return nullptr;
    return h;
}

void timerStartOnce(void* timer, int64_t delayUs)
{
    if (!timer) return;
    auto h = (esp_timer_handle_t)timer;
    esp_timer_stop(h);                  /* restart, not "already running" */
    esp_timer_start_once(h, (uint64_t)(delayUs < 0 ? 0 : delayUs));
}

void timerStop(void* timer)
{
    if (timer) esp_timer_stop((esp_timer_handle_t)timer);
}

void lock() { portENTER_CRITICAL(&s_mux); }
void unlock() { portEXIT_CRITICAL(&s_mux); }

void logLine(int level, const char* fmt, ...)
{
    esp_log_level_t l = level <= SIMRADIO_LOG_ERROR ? ESP_LOG_ERROR
                      : level == SIMRADIO_LOG_WARN  ? ESP_LOG_WARN
                                                    : ESP_LOG_INFO;
    char line[256];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(line, sizeof line, fmt, ap);
    va_end(ap);
    ESP_LOG_LEVEL(l, kTag, "%s", line);     /* the prefix and the newline are the log's */
}

int udpOpen(const char* bindAddr, const char* dest)
{
    char host[96];
    snprintf(host, sizeof host, "%s", dest ? dest : "");
    char* colon = strrchr(host, ':');
    if (!colon) {
        logLine(SIMRADIO_LOG_ERROR, "ether: %s is not host:port", host);
        return -1;
    }
    *colon = '\0';
    int port = atoi(colon + 1);

    int fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (fd < 0) {
        logLine(SIMRADIO_LOG_ERROR, "ether: socket: %s", strerror(errno));
        return -1;
    }
    fcntl(fd, F_SETFL, fcntl(fd, F_GETFL, 0) | O_NONBLOCK);

    /* Bound to an ephemeral port on this station's own address, so the ether
     * can tell one station's datagrams from another's by source alone. */
    struct sockaddr_in local = {};
    local.sin_family = AF_INET;
    local.sin_addr.s_addr = inet_addr(bindAddr);
    local.sin_port = 0;
    if (bind(fd, (struct sockaddr*)&local, sizeof local) != 0)
        logLine(SIMRADIO_LOG_WARN, "ether: bind %s: %s", bindAddr, strerror(errno));

    struct sockaddr_in peer = {};
    peer.sin_family = AF_INET;
    peer.sin_addr.s_addr = inet_addr(host);
    peer.sin_port = htons((uint16_t)port);
    if (connect(fd, (struct sockaddr*)&peer, sizeof peer) != 0) {
        logLine(SIMRADIO_LOG_ERROR, "ether: connect %s: %s", dest, strerror(errno));
        close(fd);
        return -1;
    }
    return fd;
}

struct Reader {
    int fd;
    void (*onDatagram)(const char*, size_t);
};

void readerTask(void* arg)
{
    Reader r = *(Reader*)arg;
    delete (Reader*)arg;
    static char buf[kMaxDatagram + 1];
    for (;;) {
        fd_set rd;
        FD_ZERO(&rd);
        FD_SET(r.fd, &rd);
        struct timeval tv = { 0, 1000 * portTICK_PERIOD_MS };
        if (select(r.fd + 1, &rd, nullptr, nullptr, &tv) <= 0) continue;
        for (;;) {
            ssize_t n = recv(r.fd, buf, kMaxDatagram, MSG_DONTWAIT);
            if (n <= 0) break;
            buf[n] = '\0';
            r.onDatagram(buf, (size_t)n);
        }
    }
}

int spawnReader(int fd, void (*onDatagram)(const char*, size_t))
{
    auto* r = new Reader{fd, onDatagram};
    TaskHandle_t h = nullptr;
    if (xTaskCreatePinnedToCore(readerTask, "ether", kTaskStack, r, kTaskPrio, &h,
                                tskNO_AFFINITY) != pdPASS) {
        delete r;
        return -1;
    }
    return 0;
}

const struct simradio_services kServices = {
    nowUs,
    timerCreate,
    timerStartOnce,
    timerStop,
    lock,
    unlock,
    udpOpen,
    spawnReader,
    logLine,
};

}  // namespace

extern "C" const struct simradio_services* simradio_services(void)
{
    return &kServices;
}
