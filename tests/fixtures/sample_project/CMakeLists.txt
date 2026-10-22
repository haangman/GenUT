cmake_minimum_required(VERSION 3.16)
project(genut_sample C CXX)

set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

add_library(rb STATIC src/ring_buffer.c)
target_include_directories(rb PUBLIC include)

add_library(rbslot STATIC src/slot.c)
target_include_directories(rbslot PUBLIC include)

add_library(strutil STATIC src/str_util.c)
target_include_directories(strutil PUBLIC include)
