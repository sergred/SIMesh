/**
 * The services on a plain POSIX host: std::thread, a monotonic clock, and
 * sockets.
 *
 * Timers are one thread over a min-heap of deadlines. A callback runs on that
 * thread with neither the heap's mutex nor the model's lock held, so a callback
 * that takes the model's lock cannot deadlock against a bus caller arming a
 * timer under it. Every start and stop bumps the timer's generation; a heap
 * entry whose generation is stale is dropped when it comes due, which is what
 * makes a restart a restart and a stop a stop.
 *
 * Blocking is fine here: there is no scheduler underneath that mistakes a
 * thread in poll() for a busy one.
 */
#include "services.h"

#include <arpa/inet.h>
#include <cerrno>
#include <condition_variable>
#include <cstdarg>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <mutex>
#include <netdb.h>
#include <netinet/in.h>
#include <poll.h>
#include <queue>
#include <string>
#include <sys/socket.h>
#include <thread>
#include <time.h>
#include <unistd.h>
#include <vector>

namespace {

/* ---- Clock ---- */

int64_t rawNowUs()
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000000 + ts.tv_nsec / 1000;
}

int64_t nowUs()
{
    static const int64_t origin = rawNowUs();
    return rawNowUs() - origin;
}

/* ---- Timers ---- */

struct Timer {
    void (*cb)(void*);
    void* arg;
    std::string name;
    uint64_t gen = 0;
    bool armed = false;
};

struct Due {
    int64_t  at;
    uint64_t gen;
    Timer*   timer;
    bool operator>(const Due& o) const { return at > o.at; }
};

class TimerThread {
public:
    Timer* create(void (*cb)(void*), void* arg, const char* name)
    {
        Timer* t = new Timer();
        t->cb = cb;
        t->arg = arg;
        t->name = name ? name : "";
        std::lock_guard<std::mutex> g(mu_);
        if (!started_) {
            started_ = true;
            std::thread([this] { run(); }).detach();
        }
        return t;
    }

    void start(Timer* t, int64_t delayUs)
    {
        std::lock_guard<std::mutex> g(mu_);
        t->gen++;
        t->armed = true;
        heap_.push(Due{nowUs() + (delayUs < 0 ? 0 : delayUs), t->gen, t});
        cv_.notify_one();
    }

    void stop(Timer* t)
    {
        std::lock_guard<std::mutex> g(mu_);
        t->gen++;
        t->armed = false;
    }

private:
    void run()
    {
        std::unique_lock<std::mutex> g(mu_);
        for (;;) {
            if (heap_.empty()) {
                cv_.wait(g);
                continue;
            }
            Due next = heap_.top();
            int64_t wait = next.at - nowUs();
            if (wait > 0) {
                cv_.wait_for(g, std::chrono::microseconds(wait));
                continue;
            }
            heap_.pop();
            Timer* t = next.timer;
            if (!t->armed || t->gen != next.gen) continue;   /* stopped or restarted */
            t->armed = false;
            g.unlock();
            t->cb(t->arg);
            g.lock();
        }
    }

    std::mutex mu_;
    std::condition_variable cv_;
    std::priority_queue<Due, std::vector<Due>, std::greater<Due>> heap_;
    bool started_ = false;
};

TimerThread& timers()
{
    static TimerThread* t = new TimerThread();   /* outlives every static that might arm one */
    return *t;
}

void* timerCreate(void (*cb)(void*), void* arg, const char* name)
{
    return timers().create(cb, arg, name);
}

void timerStartOnce(void* timer, int64_t delayUs)
{
    if (timer) timers().start((Timer*)timer, delayUs);
}

void timerStop(void* timer)
{
    if (timer) timers().stop((Timer*)timer);
}

/* ---- The lock ---- */

std::recursive_mutex& modelMutex()
{
    static std::recursive_mutex* m = new std::recursive_mutex();
    return *m;
}

void lock() { modelMutex().lock(); }
void unlock() { modelMutex().unlock(); }

/* ---- The socket ---- */

void logLine(int level, const char* fmt, ...)
{
    const char* tag = level <= 1 ? "error: " : level == 2 ? "warning: " : "";
    char line[512];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(line, sizeof line, fmt, ap);
    va_end(ap);
    fprintf(stderr, "simradio: %s%s\n", tag, line);
}

bool resolve(const char* host, int port, struct sockaddr_in* out)
{
    memset(out, 0, sizeof *out);
    out->sin_family = AF_INET;
    out->sin_port = htons((uint16_t)port);
    if (inet_pton(AF_INET, host, &out->sin_addr) == 1) return true;
    struct addrinfo hints = {};
    hints.ai_family = AF_INET;
    hints.ai_socktype = SOCK_DGRAM;
    struct addrinfo* res = nullptr;
    if (getaddrinfo(host, nullptr, &hints, &res) != 0 || !res) return false;
    out->sin_addr = ((struct sockaddr_in*)res->ai_addr)->sin_addr;
    freeaddrinfo(res);
    return true;
}

int udpOpen(const char* bindAddr, const char* dest)
{
    std::string host = dest ? dest : "";
    size_t colon = host.rfind(':');
    if (colon == std::string::npos) {
        logLine(1, "ether: %s is not host:port", host.c_str());
        return -1;
    }
    int port = atoi(host.c_str() + colon + 1);
    host.resize(colon);

    int fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (fd < 0) {
        logLine(1, "ether: socket: %s", strerror(errno));
        return -1;
    }
    fcntl(fd, F_SETFL, fcntl(fd, F_GETFL, 0) | O_NONBLOCK);

    /* Bound to an ephemeral port on this station's own address, so the ether
     * can tell one station's datagrams from another's by source alone. */
    struct sockaddr_in local;
    if (resolve(bindAddr, 0, &local)) {
        if (bind(fd, (struct sockaddr*)&local, sizeof local) != 0)
            logLine(2, "ether: bind %s: %s", bindAddr, strerror(errno));
    }

    struct sockaddr_in peer;
    if (!resolve(host.c_str(), port, &peer) ||
        connect(fd, (struct sockaddr*)&peer, sizeof peer) != 0) {
        logLine(1, "ether: connect %s: %s", dest, strerror(errno));
        close(fd);
        return -1;
    }
    return fd;
}

int spawnReader(int fd, void (*onDatagram)(const char*, size_t))
{
    try {
        std::thread([fd, onDatagram] {
            std::vector<char> buf(65536 + 1);
            for (;;) {
                struct pollfd p = { fd, POLLIN, 0 };
                if (poll(&p, 1, -1) <= 0) continue;
                for (;;) {
                    ssize_t n = recv(fd, buf.data(), buf.size() - 1, MSG_DONTWAIT);
                    if (n < 0) break;       /* drained, or the ether is not up yet */
                    buf[(size_t)n] = '\0';
                    onDatagram(buf.data(), (size_t)n);
                }
            }
        }).detach();
    } catch (...) {
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
