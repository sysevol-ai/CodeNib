# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

cmake_minimum_required(VERSION 3.15)
project(codenib_core LANGUAGES CXX C)

set(CMAKE_POSITION_INDEPENDENT_CODE ON)

option(CODENIB_ENABLE_DEBUG "Enable debug-friendly flags for codenib targets" OFF)
option(CODENIB_ENABLE_O3 "Enable -O3 optimization for codenib targets" ON)
option(CODENIB_BUILD_PYBIND "Build pybind11 Python extension module" ON)
option(
    CODENIB_BUILD_TREE_SITTER_POC
    "Build the experimental native Python tree-sitter chunk extractor"
    OFF
)

# -------------------------------------------------------------------------
# Vendored c-igraph
# -------------------------------------------------------------------------
# We deliberately do NOT link system libigraph. Reason: Python's `igraph`
# package (PyPI wheel) bundles its own c-igraph inside _igraph.abi3.so and
# exports a few interop symbols (igraph_rng_Python_*). When codenib_core
# also links system libigraph.so.3, the two c-igraphs fight over the
# global symbol table at process load time and the parity test segfaults
# inside libigraph. Vendoring c-igraph here + --exclude-libs,ALL on the
# pybind module keeps our copy private to codenib_core.so so no symbol
# clash is possible. Each .so owns an independent c-igraph instance.
include(FetchContent)
set(BUILD_SHARED_LIBS OFF CACHE BOOL "" FORCE)
set(IGRAPH_OPENMP_SUPPORT OFF CACHE BOOL "" FORCE)
set(IGRAPH_USE_INTERNAL_BLAS ON CACHE BOOL "" FORCE)
set(IGRAPH_USE_INTERNAL_LAPACK ON CACHE BOOL "" FORCE)
set(IGRAPH_USE_INTERNAL_ARPACK ON CACHE BOOL "" FORCE)
set(IGRAPH_USE_INTERNAL_GLPK ON CACHE BOOL "" FORCE)
set(IGRAPH_USE_INTERNAL_GMP ON CACHE BOOL "" FORCE)
set(IGRAPH_USE_INTERNAL_PLFIT ON CACHE BOOL "" FORCE)
set(IGRAPH_GRAPHML_SUPPORT OFF CACHE BOOL "" FORCE)
set(IGRAPH_ENABLE_TLS OFF CACHE BOOL "" FORCE)
set(IGRAPH_WARNINGS_AS_ERRORS OFF CACHE BOOL "" FORCE)

FetchContent_Declare(
    igraph
    GIT_REPOSITORY https://github.com/igraph/igraph.git
    GIT_TAG        0.10.15
    GIT_SHALLOW    TRUE
)
FetchContent_MakeAvailable(igraph)

if(CODENIB_BUILD_TREE_SITTER_POC)
    # Keep the proof of concept opt-in until its repository-level benchmark
    # clears the acceleration gate. Both dependencies are immutable commits.
    FetchContent_Declare(
        tree_sitter_runtime
        GIT_REPOSITORY https://github.com/tree-sitter/tree-sitter.git
        GIT_TAG        6e0618704ad758ba2ea5822faa80bcd36fbeba3d # v0.25.2
        GIT_SHALLOW    TRUE
    )
    FetchContent_Declare(
        tree_sitter_python_grammar
        GIT_REPOSITORY https://github.com/tree-sitter/tree-sitter-python.git
        GIT_TAG        293fdc02038ee2bf0e2e206711b69c90ac0d413f # v0.25.0
        GIT_SHALLOW    TRUE
    )
    if(POLICY CMP0169)
        cmake_policy(PUSH)
        cmake_policy(SET CMP0169 OLD)
    endif()
    FetchContent_GetProperties(tree_sitter_runtime)
    if(NOT tree_sitter_runtime_POPULATED)
        FetchContent_Populate(tree_sitter_runtime)
    endif()
    FetchContent_GetProperties(tree_sitter_python_grammar)
    if(NOT tree_sitter_python_grammar_POPULATED)
        FetchContent_Populate(tree_sitter_python_grammar)
    endif()
    if(POLICY CMP0169)
        cmake_policy(POP)
    endif()

    add_library(codenib_tree_sitter_python STATIC
        ${tree_sitter_runtime_SOURCE_DIR}/lib/src/lib.c
        ${tree_sitter_python_grammar_SOURCE_DIR}/src/parser.c
        ${tree_sitter_python_grammar_SOURCE_DIR}/src/scanner.c
    )
    target_include_directories(codenib_tree_sitter_python
        PUBLIC ${tree_sitter_runtime_SOURCE_DIR}/lib/include
        PRIVATE
            ${tree_sitter_runtime_SOURCE_DIR}/lib/src
            ${tree_sitter_python_grammar_SOURCE_DIR}/src
    )
endif()

find_package(PkgConfig REQUIRED)
pkg_check_modules(RE2 REQUIRED re2)
find_package(ZLIB REQUIRED)

add_library(codenib_core STATIC
    clangd_fact_query.cpp
    code_graph.cpp
    content_digest.cpp
    fact_batch_buffer.cpp
    fact_query_index.cpp
    graph_layers.cpp
    scip_decoder_registry.cpp
    scip_decode_common.cpp
    scip_decode_base.cpp
    scip_decode_python.cpp
    scip_decode_go.cpp
    scip_decode_rust.cpp
    scip_decode_ruby.cpp
    scip_decode_ts.cpp
)

target_compile_features(codenib_core PUBLIC cxx_std_17)

target_include_directories(codenib_core
    PUBLIC
        ${CMAKE_CURRENT_SOURCE_DIR}
        ${RE2_INCLUDE_DIRS}
)

target_link_directories(codenib_core PRIVATE ${RE2_LIBRARY_DIRS})
target_link_libraries(codenib_core
    PUBLIC
        igraph
        ${RE2_LIBRARIES}
        ZLIB::ZLIB
)

if(CODENIB_BUILD_TREE_SITTER_POC)
    target_sources(codenib_core PRIVATE python_chunk_poc.cpp)
    target_compile_definitions(codenib_core PUBLIC CODENIB_TREE_SITTER_POC=1)
    target_link_libraries(codenib_core PUBLIC codenib_tree_sitter_python)
endif()

add_executable(scip_decode_test
    tests/scip_decode_test.cpp
)

target_link_libraries(scip_decode_test PRIVATE codenib_core)

add_executable(graph_layers_test
    tests/graph_layers_test.cpp
)

target_link_libraries(graph_layers_test PRIVATE codenib_core)

add_executable(fact_batch_buffer_test
    tests/fact_batch_buffer_test.cpp
)

target_link_libraries(fact_batch_buffer_test PRIVATE codenib_core)

add_executable(content_digest_test
    tests/content_digest_test.cpp
)

target_link_libraries(content_digest_test PRIVATE codenib_core)

add_executable(fact_query_index_test
    tests/fact_query_index_test.cpp
)

target_link_libraries(fact_query_index_test PRIVATE codenib_core)

add_executable(scip_decode_repo
    tests/scip_decode_repo.cpp
)

target_link_libraries(scip_decode_repo PRIVATE codenib_core)

if(CODENIB_ENABLE_O3)
    target_compile_options(codenib_core PRIVATE -O3)
    if(TARGET codenib_tree_sitter_python)
        target_compile_options(codenib_tree_sitter_python PRIVATE -O3)
    endif()
    if(TARGET scip_decode_test)
        target_compile_options(scip_decode_test PRIVATE -O3)
    endif()
    if(TARGET scip_decode_repo)
        target_compile_options(scip_decode_repo PRIVATE -O3)
    endif()
    if(TARGET graph_layers_test)
        target_compile_options(graph_layers_test PRIVATE -O3)
    endif()
    if(TARGET fact_batch_buffer_test)
        target_compile_options(fact_batch_buffer_test PRIVATE -O3)
    endif()
    if(TARGET content_digest_test)
        target_compile_options(content_digest_test PRIVATE -O3)
    endif()
    if(TARGET fact_query_index_test)
        target_compile_options(fact_query_index_test PRIVATE -O3)
    endif()
endif()

if(CODENIB_BUILD_PYBIND)
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

    pybind11_add_module(codenib_core_py
        bindings/pybind_module.cpp
    )
    set_target_properties(codenib_core_py PROPERTIES
        OUTPUT_NAME "codenib_core"
    )
    target_link_libraries(codenib_core_py PRIVATE codenib_core)
    # Statically link libstdc++ and libgcc_s so the pybind module does
    # not depend on the runtime Python env's bundled libstdc++, which
    # may lack the GLIBCXX symbols our system GCC references.
    #
    # --exclude-libs,ALL hides every symbol coming from a static library
    # (vendored c-igraph, codenib_core, RE2, ...) from the .so's
    # dynamic symbol table. Combined with vendored c-igraph above, this
    # makes our c-igraph fully private and prevents clashes with the
    # c-igraph that Python's `igraph` wheel bundles in _igraph.abi3.so.
    # The PYBIND11_MODULE entry symbol (PyInit_codenib_core) is
    # defined directly in bindings/pybind_module.cpp (not from a static
    # archive), so it stays visible.
    target_link_options(codenib_core_py PRIVATE
        $<$<CXX_COMPILER_ID:GNU>:-static-libstdc++>
        $<$<CXX_COMPILER_ID:GNU>:-static-libgcc>
        $<$<CXX_COMPILER_ID:GNU>:LINKER:--exclude-libs,ALL>
    )
    if(CODENIB_ENABLE_O3)
        target_compile_options(codenib_core_py PRIVATE -O3)
    endif()
endif()

if(CODENIB_ENABLE_DEBUG)
    if(MSVC)
        set(_codenib_debug_flags /Zi /Od)
    else()
        set(_codenib_debug_flags -g -O0)
    endif()

    target_compile_definitions(codenib_core PUBLIC CODENIB_DEBUG=1)
    target_compile_options(codenib_core PRIVATE ${_codenib_debug_flags})

    if(TARGET scip_decode_test)
        target_compile_definitions(scip_decode_test PRIVATE CODENIB_DEBUG=1)
        target_compile_options(scip_decode_test PRIVATE ${_codenib_debug_flags})
    endif()

    if(TARGET scip_decode_repo)
        target_compile_definitions(scip_decode_repo PRIVATE CODENIB_DEBUG=1)
        target_compile_options(scip_decode_repo PRIVATE ${_codenib_debug_flags})
    endif()

    if(TARGET graph_layers_test)
        target_compile_definitions(graph_layers_test PRIVATE CODENIB_DEBUG=1)
        target_compile_options(graph_layers_test PRIVATE ${_codenib_debug_flags})
    endif()

    if(TARGET fact_batch_buffer_test)
        target_compile_definitions(fact_batch_buffer_test PRIVATE CODENIB_DEBUG=1)
        target_compile_options(fact_batch_buffer_test PRIVATE ${_codenib_debug_flags})
    endif()

    if(TARGET content_digest_test)
        target_compile_definitions(content_digest_test PRIVATE CODENIB_DEBUG=1)
        target_compile_options(content_digest_test PRIVATE ${_codenib_debug_flags})
    endif()

    if(TARGET fact_query_index_test)
        target_compile_definitions(fact_query_index_test PRIVATE CODENIB_DEBUG=1)
        target_compile_options(fact_query_index_test PRIVATE ${_codenib_debug_flags})
    endif()
endif()
