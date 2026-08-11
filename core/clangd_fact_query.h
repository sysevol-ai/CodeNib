/*
 * SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#pragma once

#include "decoded_records.h"

#include <array>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <string>

namespace codenib::core {

inline constexpr std::uint32_t CLANGD_FACT_QUERY_ABI_VERSION = 1;
inline constexpr char CLANGD_FACT_QUERY_FORMAT[] = "clangd-riff-fact-query-v1";

// clangd itself rejects non-current RIFF versions. CodeNib deliberately uses
// an exact allowlist backed by checked fixtures and parity tests instead of
// guessing that an unknown future layout remains compatible.
inline constexpr std::array<std::uint32_t, 3> CLANGD_SUPPORTED_RIFF_VERSIONS{
    {18, 19, 20}};

// The native input is a local clangd artifact, but every attacker-controlled
// size/count must remain finite before it can drive an allocation.
struct ClangdFactDecodeLimits {
  std::size_t max_index_files{200'000};
  std::size_t max_chunks_per_file{128};
  std::uint64_t max_index_file_bytes{512ULL * 1024ULL * 1024ULL};
  std::uint64_t max_aggregate_index_bytes{8ULL * 1024ULL * 1024ULL * 1024ULL};
  std::uint64_t max_string_table_bytes{256ULL * 1024ULL * 1024ULL};
  std::uint64_t max_aggregate_string_table_bytes{2ULL * 1024ULL * 1024ULL *
                                                 1024ULL};
  std::size_t max_string_entries_per_file{1'000'000};
  std::size_t max_aggregate_string_entries{20'000'000};
  std::uint64_t max_materialized_string_bytes_per_file{512ULL * 1024ULL *
                                                       1024ULL};
  std::uint64_t max_aggregate_materialized_string_bytes{4ULL * 1024ULL *
                                                        1024ULL * 1024ULL};
  std::size_t max_records_per_file{2'000'000};
  std::size_t max_aggregate_records{25'000'000};
};

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
  std::uint64_t decompressed_string_bytes{0};
  std::uint64_t materialized_string_bytes{0};
  std::size_t string_entry_count{0};
};

struct ClangdQueryRecords {
  std::shared_ptr<DecodedRecords> records;
  ClangdFactDecodeProfile profile;
};

// Decode every direct *.idx child in stable filename order. Any read or parse
// failure rejects the complete native candidate so Python auto mode can use
// the established ClangdGraphDecoder without publishing partial rows.
ClangdQueryRecords decode_clangd_query_records(
    const std::string &idx_directory, const std::string &project_root,
    const ClangdFactDecodeLimits &limits = ClangdFactDecodeLimits{});

} // namespace codenib::core
