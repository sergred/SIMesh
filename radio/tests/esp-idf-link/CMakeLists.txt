# Proves the ESP-IDF backend compiles and links against the IDF Linux host
# port, and runs: a chip is opened and sent SetTx, and TX_DONE comes back on
# esp_timer. Not a firmware; nothing here ships.
#
#     . $IDF_PATH/export.sh
#     idf.py -C SIMesh/radio/tests/esp-idf-link -B SIMesh/radio/build/esp-idf-link \
#            -D SDKCONFIG=SIMesh/radio/build/esp-idf-link/sdkconfig set-target linux build
#     SIMesh/radio/build/esp-idf-link/simradio_link.elf
#
# esp_timer on the host target is hw-linux's (the IDF registers only headers
# there), so that component comes from the workspace beside SIMesh.
cmake_minimum_required(VERSION 3.16)
set(EXTRA_COMPONENT_DIRS
    "${CMAKE_CURRENT_LIST_DIR}/../../backend/esp-idf"
    "${CMAKE_CURRENT_LIST_DIR}/../../../../hw-linux/esp-idf/components/esp_timer")
set(COMPONENTS main)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(simradio_link)
