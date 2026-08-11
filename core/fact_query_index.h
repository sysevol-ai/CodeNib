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
#include <optional>
#include <string>
#include <unordered_map>
#include <vector>

namespace codenib::core {

inline constexpr std::uint32_t FACT_QUERY_INDEX_ABI_VERSION = 1;
inline constexpr char FACT_QUERY_INDEX_FORMAT[] = "fact-query-index-v1";

// Graph-free read index for the symbol-based definition/reference subset of
// the LSP-shaped API. The index owns one immutable DecodedRecords allocation
// and stores only vertex ids and edge indexes as postings. Position and route
// queries are deliberately outside the v1 capability contract.
class FactQueryIndex {
public:
  struct Reference {
    CodeGraph::VertexId source;
    std::string anchor_file;
    int anchor_line{0};
  };

  explicit FactQueryIndex(std::shared_ptr<const DecodedRecords> records);

  bool has_symbol(const std::string &name) const;
  std::optional<CodeGraph::VertexData>
  get_node_info_by_name(const std::string &name) const;
  std::optional<CodeGraph::VertexData>
  get_node_info_by_id(CodeGraph::VertexId id) const;
  std::vector<std::string>
  resolve_symbol_candidates(const std::string &symbol,
                            std::size_t limit = 8) const;
  std::vector<Reference>
  incoming_references(const std::string &target_name) const;

  const std::string &project_root() const { return records_->project_root; }
  std::size_t record_count() const { return records_->vertices.size(); }
  std::size_t edge_count() const { return records_->edges.size(); }
  std::size_t symbol_count() const { return definition_by_name_.size(); }
  std::size_t reference_count() const { return reference_count_; }

private:
  static std::string trim_symbol(std::string value);
  static std::string bare_symbol(const std::string &value);
  static std::string graph_bare_symbol(const std::string &value);
  static std::string lowercase(std::string value);
  void append_candidate(std::vector<CodeGraph::VertexId> &output,
                        CodeGraph::VertexId id, std::size_t limit) const;

  std::shared_ptr<const DecodedRecords> records_;
  std::unordered_map<std::string, CodeGraph::VertexId> record_by_name_;
  std::unordered_map<std::string, CodeGraph::VertexId> definition_by_name_;
  std::unordered_map<std::string, std::vector<CodeGraph::VertexId>> aliases_;
  std::unordered_map<std::string, std::vector<CodeGraph::VertexId>>
      lsp_aliases_;
  std::vector<std::string> alias_order_;
  std::unordered_map<CodeGraph::VertexId, std::vector<std::size_t>>
      incoming_reference_indexes_;
  std::size_t reference_count_{0};
};

} // namespace codenib::core
