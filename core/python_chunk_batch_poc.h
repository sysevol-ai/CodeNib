/*
 * SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

namespace codenib::core {

inline constexpr std::uint16_t PYTHON_CHUNK_BATCH_ABI_VERSION = 1;
inline constexpr std::size_t PYTHON_CHUNK_BATCH_FILE_ROW_SIZE = 8;
inline constexpr std::size_t PYTHON_CHUNK_BATCH_SPAN_ROW_SIZE = 20;
inline constexpr char PYTHON_CHUNK_BATCH_FILE_ROW_FORMAT[] = "<II";
inline constexpr char PYTHON_CHUNK_BATCH_SPAN_ROW_FORMAT[] = "<IIB3xII";
inline constexpr char PYTHON_CHUNK_BATCH_ORDERING[] =
    "input-ordinal-then-span-order-v1";
inline constexpr char PYTHON_CHUNK_BATCH_OVERFLOW_POLICY[] = "fail-closed-v1";

inline constexpr std::uint32_t PYTHON_CHUNK_BATCH_MAX_FILE_COUNT = 10'000;
inline constexpr std::uint64_t PYTHON_CHUNK_BATCH_MAX_SOURCE_BYTES =
    1'000'000'000;
inline constexpr std::uint32_t PYTHON_CHUNK_BATCH_MAX_ROW_COUNT = 10'000'000;
inline constexpr std::uint32_t PYTHON_CHUNK_BATCH_MAX_ARENA_BYTES =
    1'000'000'000;

struct PythonChunkBatchBuffer {
  std::vector<std::uint8_t> file_rows;
  std::vector<std::uint8_t> rows;
  std::vector<std::uint8_t> arena;
  std::uint64_t source_bytes{0};
  std::uint64_t batch_wall_ns{0};
  std::uint64_t parse_work_ns{0};
  std::uint64_t extract_encode_work_ns{0};
  std::uint64_t ordered_merge_ns{0};
  std::uint32_t file_count{0};
  std::uint32_t row_count{0};
  std::uint32_t worker_count{0};
};

// Extract a complete ordered repository batch while reusing the established
// single-source grammar walk. Input paths and CodeChunk construction remain in
// Python. Each call creates a fresh bounded worker set and publishes spans in
// input ordinal followed by per-file span order.
PythonChunkBatchBuffer
extract_python_repository_chunk_buffer(const std::vector<std::string> &sources,
                                       int chunk_depth, bool l2_level_exclusive,
                                       std::size_t worker_count);

} // namespace codenib::core
