/*
 * SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#pragma once

#include "decoded_records.h"

#include <cstddef>
#include <cstdint>
#include <memory>
#include <string>

namespace codenib::core {

inline constexpr std::uint32_t CLANGD_FACT_QUERY_ABI_VERSION = 1;
inline constexpr char CLANGD_FACT_QUERY_FORMAT[] = "clangd-riff-fact-query-v1";

// Timings cover the baseline query-ready path from an existing project-local
// clangd index. External clangd generation is deliberately outside this
// contract and must be reported separately by callers.
struct ClangdFactDecodeProfile {
  std::uint64_t discover_files_ns{0};
  std::uint64_t read_files_ns{0};
  std::uint64_t parse_files_ns{0};
  std::uint64_t merge_records_ns{0};
  std::uint64_t build_query_records_ns{0};
  std::uint64_t total_ns{0};
  std::size_t file_count{0};
  std::size_t raw_symbol_count{0};
  std::size_t raw_reference_count{0};
  std::size_t definition_count{0};
  std::size_t reference_count{0};
  std::size_t decoded_record_count{0};
  std::uint64_t index_bytes{0};
};

struct ClangdQueryRecords {
  std::shared_ptr<DecodedRecords> records;
  ClangdFactDecodeProfile profile;
};

// Decode every direct *.idx child in stable filename order. Any read or parse
// failure rejects the complete native candidate so Python auto mode can use
// the established ClangdGraphDecoder without publishing partial rows.
ClangdQueryRecords decode_clangd_query_records(const std::string &idx_directory,
                                               const std::string &project_root);

} // namespace codenib::core
