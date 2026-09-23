# simradio for a plain POSIX host: a static library for a station to link, and
# the same objects as a shared library for the ctypes tests.
#
#     cmake -B build && cmake --build build
#
# The ESP-IDF backend is not built here: it is an IDF component
# (backend/esp-idf/CMakeLists.txt) and builds inside a firmware's own project.
# Keep the source list flat and free of generated files: a Rust station
# compiles exactly these four with the `cc` crate.
cmake_minimum_required(VERSION 3.16)
project(simradio LANGUAGES CXX)

set(SIMRADIO_BACKEND "posix" CACHE STRING "posix|esp-idf")
set_property(CACHE SIMRADIO_BACKEND PROPERTY STRINGS posix esp-idf)
if(NOT SIMRADIO_BACKEND STREQUAL "posix")
    message(FATAL_ERROR "SIMRADIO_BACKEND=${SIMRADIO_BACKEND}: only the posix backend "
                        "builds standalone; the esp-idf one is an IDF component "
                        "(backend/esp-idf/CMakeLists.txt)")
endif()

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)
find_package(Threads REQUIRED)

set(SIMRADIO_SOURCES
    src/model.cpp
    src/ether_link.cpp
    src/json.cpp
    backend/posix/services.cpp
)

add_library(simradio_objects OBJECT ${SIMRADIO_SOURCES})
target_include_directories(simradio_objects PUBLIC include PRIVATE src)
target_compile_options(simradio_objects PRIVATE -Wall -Wextra)

add_library(simradio STATIC $<TARGET_OBJECTS:simradio_objects>)
target_include_directories(simradio PUBLIC include)
target_link_libraries(simradio PUBLIC Threads::Threads)

add_library(simradio_shared SHARED $<TARGET_OBJECTS:simradio_objects>)
set_target_properties(simradio_shared PROPERTIES OUTPUT_NAME simradio)
target_link_libraries(simradio_shared PUBLIC Threads::Threads)
