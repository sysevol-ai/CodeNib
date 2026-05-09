#pragma once

#include "code_graph.h"
#include "scip_decode_common.h"

#include <chrono>
#include <cstdint>
#include <optional>
#include <string>
#include <vector>

namespace codeminer::core {

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

  // Optional post-pass after merge_subgraphs. Default no-op. Python overrides
  // to run `_fix_unified_names` over the fully-built graph.
  virtual void postprocess() {}

  void merge_subgraphs(const std::vector<Subgraph> &subgraphs);

  std::string index_file_path_;
  std::optional<std::string> project_root_;
  CodeGraph code_graph_;
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

} // namespace codeminer::core
