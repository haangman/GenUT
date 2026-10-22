cmake_minimum_required(VERSION 3.16)
project(genut_cpp_sample CXX)

set(CMAKE_EXPORT_COMPILE_COMMANDS ON)
set(CMAKE_CXX_STANDARD 17)

add_library(counter STATIC src/counter.cpp)
target_include_directories(counter PUBLIC include)
