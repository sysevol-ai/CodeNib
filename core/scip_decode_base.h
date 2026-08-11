/*
 * SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#pragma once

#include "code_graph.h"
#include "decoded_records.h"
#include "scip_decode_common.h"

#include <chrono>
#include <cstdint>
#include <optional>
#include <string>
#include <vector>

namespace codenib::core {

struct SCIPDecodeProfile {
  std::uint64_t load_metadata_ns{0};
  std::uint64_t file_read_ns{0};
  std::uint64_t extract_blocks_ns{0};
  std::uint64_t prescan_ns{0};
  std::uint64_t process_documents_ns{0};
  std::uint64_t merge_subgraphs_ns{0};
  std::uint64_t materialize_graph_ns{0};
  std::uint64_t total_ns{0};
  std::size_t worker_count{0};
  std::size_t document_count{0};
};

// Compatibility name for SCIP decoder APIs. Query/storage consumers use the
// provider-neutral DecodedRecords contract; clangd can emit the same boundary
// without pretending to be a SCIP backend.
using SCIPDecodedRecords = DecodedRecords;

// Common orchestration for SCIP decoders across languages. Subclasses
// implement `process_document` (language-specific symbol parsing + subgraph
// construction). Shared code:
//   1. file read
//   2. extract document blocks (brace matcher)
//   3. optional prescan() hook (TypeScript)
//   4. parallel process_document across hardware_concurrency threads
//   5. merge subgraphs with last-def-wins / first-ref-wins semantics
class SCIPDecoderBase {
public:
  explicit SCIPDecoderBase(
      std::string index_file_path,
      std::optional<std::string> project_root = std::nullopt);
  virtual ~SCIPDecoderBase() = default;

  CodeGraph decode();
  SCIPDecodedRecords decode_records();
  const SCIPDecodeProfile &last_profile() const { return last_profile_; }

protected:
  virtual Subgraph
  process_document(const std::string &document_block) const = 0;

  // Optional pre-scan hook. Default no-op. TypeScript overrides to build
  // `_project_packages` so references can be filtered consistently across
  // parallel workers.
  virtual void prescan(const std::vector<std::string> &document_blocks) {
    (void)document_blocks;
  }

  // Optional per-decoder setup (parent-process only): read go.mod, Cargo.toml.
  virtual void load_metadata() {}

  // Optional post-pass after record merge. Default no-op. Python overrides to
  // run `_fix_unified_names` without requiring an igraph materialization.
  virtual void postprocess_records(SCIPDecodedRecords &) {}

  SCIPDecodedRecords
  merge_subgraphs(const std::vector<Subgraph> &subgraphs) const;

  std::string index_file_path_;
  std::optional<std::string> project_root_;
  CodeGraph code_graph_;
  SCIPDecodeProfile last_profile_;

private:
  SCIPDecodedRecords decode_records_impl();
  void log_profile() const;
};

// Shared helpers — brace-matching block extractor and integer-list regex.
// Implemented in scip_decode_base.cpp so every language decoder can use them.
std::vector<std::string> extract_blocks(const std::string &text,
                                        const std::string &keyword);

// Return the unescaped value of the first ``symbol: "..."`` field.
// Handles protobuf TextFormat escapes (\", \\, \', \n, \t, \r). Mirrors
// ``extract_symbol`` in ``scip_indexer_base.py`` so both decoders see
// byte-identical symbol strings. Returns std::nullopt when no symbol
// field is present or the string is unterminated.
std::optional<std::string> extract_symbol(const std::string &text);

} // namespace codenib::core
