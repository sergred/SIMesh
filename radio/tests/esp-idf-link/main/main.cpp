/* Open a chip, send one frame into no ether, and wait for TX_DONE. */
#include "simradio.h"

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include <cstdio>
#include <cstdlib>
#include <initializer_list>

static volatile int s_dio1 = 0;

static void onPin(void*, int pin, int level)
{
    if (pin == SIMRADIO_PIN_DIO1) s_dio1 = level;
}

static void cmd(simradio_t* c, std::initializer_list<uint8_t> bytes, uint8_t* in = nullptr)
{
    uint8_t out[16], scratch[16];
    size_t n = 0;
    for (uint8_t b : bytes) out[n++] = b;
    simradio_transfer(c, out, n, in ? in : scratch);
}

extern "C" void app_main(void)
{
    simradio_station_open(1, "127.0.0.1", "");
    simradio_t* c = simradio_open(0, onPin, nullptr);
    cmd(c, {0x08, 0x02, 0x01, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00});   /* TX_DONE on DIO1 */
    cmd(c, {0x8C, 0x00, 0x08, 0x00, 0x04, 0x01, 0x00});               /* 4 bytes, CRC */
    cmd(c, {0x0E, 0x00, 1, 2, 3, 4});
    int64_t t0 = simradio_now_us();
    cmd(c, {0x83, 0x00, 0x00, 0x00});
    while (!s_dio1 && simradio_now_us() - t0 < 2000000) vTaskDelay(1);
    uint8_t in[4] = {};
    cmd(c, {0x12, 0x00, 0x00, 0x00}, in);
    int64_t took = simradio_now_us() - t0;
    printf("simradio-link: TX_DONE=%d irq=0x%02x%02x after %lld us\n",
           s_dio1, in[2], in[3], (long long)took);
    fflush(stdout);
    exit(s_dio1 && (in[3] & 0x01) ? 0 : 1);
}
