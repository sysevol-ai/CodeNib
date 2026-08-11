/*
 * SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#pragma once

#include <cstddef>
#include <cstdint>
#include <string_view>
#include <vector>

namespace codenib::core {

inline constexpr std::uint16_t PYTHON_CHUNK_BUFFER_ABI_VERSION = 1;
inline constexpr std::size_t PYTHON_CHUNK_BUFFER_ROW_SIZE = 20;
inline constexpr char PYTHON_CHUNK_GRAMMAR[] = "tree-sitter-python@v0.25.0";
inline constexpr char PYTHON_CHUNK_RUNTIME[] = "tree-sitter@v0.25.2";
inline constexpr char PYTHON_CHUNK_SEMANTIC_PROFILE[] =
    "python-definition-spans-v1";

struct PythonChunkBuffer {
  std::vector<std::uint8_t> rows;
  std::vector<std::uint8_t> arena;
  std::uint64_t parse_ns{0};
  std::uint64_t extract_ns{0};
  std::uint32_t row_count{0};
};

// Extract the same L1/L2 Python definition spans used by PythonCodeChunker.
// The caller still owns content prefixing, line splitting, and CodeChunk
// construction; only parsing and AST traversal cross into the native core.
PythonChunkBuffer extract_python_chunk_buffer(std::string_view source,
                                              int chunk_depth,
                                              bool l2_level_exclusive);

} // namespace codenib::core
