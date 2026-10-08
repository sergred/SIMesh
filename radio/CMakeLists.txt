# The virtual radios sim-mesh provides a station at run time, and its time shim:
#
#     build/libsimradio-sx1262.so   the SX1262, which a firmware links with
#                                   -lsimradio-sx1262 and never carries itself
#     build/libsimradio-lr2021.so   the LR2021, the same way (-lsimradio-lr2021)
#     build/libsimclock.so          the time shim (LD_PRELOAD in a virtual-time run)
#
#     cmake -B build && cmake --build build
#
# A radio library holds the chip model, the ether link and the conductor, with
# the library's own POSIX services; a host with a scheduler of its own hands
# it others at run time (simradio_set_services, simradio.h). A radio is a
# shared library of its own name, so a station can be started with the one
# its firmware was built for.
cmake_minimum_required(VERSION 3.16)
project(simradio LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)
find_package(Threads REQUIRED)

add_library(simradio-sx1262 SHARED
    src/model.cpp
    src/busy.cpp
    src/ether_link.cpp
    src/conductor.cpp
    src/json.cpp
    backend/posix/services.cpp
)
target_include_directories(simradio-sx1262 PUBLIC include PRIVATE src)
target_compile_options(simradio-sx1262 PRIVATE -Wall -Wextra)
target_link_libraries(simradio-sx1262 PUBLIC Threads::Threads)

# The LR2021, for firmware built on its driver (-lsimradio-lr2021): the same
# ether link, conductor and services around another chip.
add_library(simradio-lr2021 SHARED
    src/model_lr2021.cpp
    src/busy.cpp
    src/ether_link.cpp
    src/conductor.cpp
    src/json.cpp
    backend/posix/services.cpp
)
target_include_directories(simradio-lr2021 PUBLIC include PRIVATE src)
target_compile_options(simradio-lr2021 PRIVATE -Wall -Wextra)
target_link_libraries(simradio-lr2021 PUBLIC Threads::Threads)

# The time shim a station is started with in a virtual-time run
# (LD_PRELOAD=build/libsimclock.so): the C library's clocks, sleeps and
# timeouts in node time, from the radio library in the same process.
add_library(simclock SHARED shim/simclock.c)
target_include_directories(simclock PRIVATE include)
target_compile_options(simclock PRIVATE -Wall -Wextra)
target_link_libraries(simclock PRIVATE Threads::Threads ${CMAKE_DL_LIBS})
