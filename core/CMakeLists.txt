cmake_minimum_required(VERSION 3.15)
project(codeminer_core LANGUAGES CXX)

set(CMAKE_POSITION_INDEPENDENT_CODE ON)

option(CODEMINER_ENABLE_DEBUG "Enable debug-friendly flags for codeminer targets" OFF)
option(CODEMINER_ENABLE_O3 "Enable -O3 optimization for codeminer targets" ON)
option(CODEMINER_BUILD_PYBIND "Build pybind11 Python extension module" ON)

find_package(PkgConfig REQUIRED)
pkg_check_modules(IGRAPH REQUIRED igraph)
pkg_check_modules(RE2 REQUIRED re2)

add_library(codeminer_core STATIC
    code_graph.cpp
    scip_decode_common.cpp
    scip_decode_base.cpp
    scip_decode_python.cpp
    scip_decode_go.cpp
    scip_decode_rust.cpp
    scip_decode_ts.cpp
)

target_compile_features(codeminer_core PUBLIC cxx_std_17)

target_include_directories(codeminer_core
    PUBLIC
        ${CMAKE_CURRENT_SOURCE_DIR}
        ${IGRAPH_INCLUDE_DIRS}
        ${RE2_INCLUDE_DIRS}
)

target_link_directories(codeminer_core PRIVATE ${IGRAPH_LIBRARY_DIRS} ${RE2_LIBRARY_DIRS})
target_link_libraries(codeminer_core PUBLIC ${IGRAPH_LIBRARIES} ${RE2_LIBRARIES})

target_compile_options(codeminer_core PRIVATE ${IGRAPH_CFLAGS_OTHER})

add_executable(scip_decode_test
    tests/scip_decode_test.cpp
)

target_link_libraries(scip_decode_test PRIVATE codeminer_core)

add_executable(scip_decode_repo
    tests/scip_decode_repo.cpp
)

target_link_libraries(scip_decode_repo PRIVATE codeminer_core)

if(CODEMINER_ENABLE_O3)
    target_compile_options(codeminer_core PRIVATE -O3)
    if(TARGET scip_decode_test)
        target_compile_options(scip_decode_test PRIVATE -O3)
    endif()
    if(TARGET scip_decode_repo)
        target_compile_options(scip_decode_repo PRIVATE -O3)
    endif()
endif()

if(CODEMINER_BUILD_PYBIND)
    find_package(Python COMPONENTS Interpreter Development REQUIRED)
    execute_process(
        COMMAND ${Python_EXECUTABLE} -c
        "import pybind11; print(pybind11.get_cmake_dir())"
        OUTPUT_VARIABLE _pybind11_cmake_dir
        OUTPUT_STRIP_TRAILING_WHITESPACE
        RESULT_VARIABLE _pybind11_locate_rc
    )
    if(NOT _pybind11_locate_rc EQUAL 0)
        message(FATAL_ERROR "Failed to locate pybind11 via ${Python_EXECUTABLE}. "
                            "Install it with: pip install pybind11")
    endif()
    list(APPEND CMAKE_PREFIX_PATH "${_pybind11_cmake_dir}")
    find_package(pybind11 CONFIG REQUIRED)

    pybind11_add_module(codeminer_core_py
        bindings/pybind_module.cpp
    )
    set_target_properties(codeminer_core_py PROPERTIES
        OUTPUT_NAME "codeminer_core"
    )
    target_link_libraries(codeminer_core_py PRIVATE codeminer_core)
    if(CODEMINER_ENABLE_O3)
        target_compile_options(codeminer_core_py PRIVATE -O3)
    endif()
endif()

if(CODEMINER_ENABLE_DEBUG)
    if(MSVC)
        set(_codeminer_debug_flags /Zi /Od)
    else()
        set(_codeminer_debug_flags -g -O0)
    endif()

    target_compile_definitions(codeminer_core PUBLIC CODEMINER_DEBUG=1)
    target_compile_options(codeminer_core PRIVATE ${_codeminer_debug_flags})

    if(TARGET scip_decode_test)
        target_compile_definitions(scip_decode_test PRIVATE CODEMINER_DEBUG=1)
        target_compile_options(scip_decode_test PRIVATE ${_codeminer_debug_flags})
    endif()

    if(TARGET scip_decode_repo)
        target_compile_definitions(scip_decode_repo PRIVATE CODEMINER_DEBUG=1)
        target_compile_options(scip_decode_repo PRIVATE ${_codeminer_debug_flags})
    endif()
endif()
