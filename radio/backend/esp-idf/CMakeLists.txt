# simradio as an ESP-IDF component, for a station built for the Linux host
# target. The sources are the library's own; only services.cpp is this
# backend's.
#
# The component takes its name from the directory that registers it. A board
# that wants it called `simradio` gives it a directory of that name whose
# CMakeLists.txt is one line:
#
#     include(<path to SIMesh>/radio/backend/esp-idf/CMakeLists.txt)
#
# CMAKE_CURRENT_LIST_DIR below is this file's directory either way.
set(simradio_root "${CMAKE_CURRENT_LIST_DIR}/../..")

idf_component_register(
    SRCS "${simradio_root}/src/model.cpp"
         "${simradio_root}/src/ether_link.cpp"
         "${simradio_root}/src/json.cpp"
         "${CMAKE_CURRENT_LIST_DIR}/services.cpp"
    INCLUDE_DIRS "${simradio_root}/include"
    PRIV_INCLUDE_DIRS "${simradio_root}/src"
    REQUIRES esp_timer freertos log
)
