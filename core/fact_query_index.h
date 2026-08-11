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
#include <utility>
#include <vector>

namespace codenib::core {

inline constexpr std::uint32_t FACT_QUERY_INDEX_ABI_VERSION = 1;
inline constexpr char FACT_QUERY_INDEX_FORMAT[] = "fact-query-index-v1";

// Graph-free read index for definition/reference subsets of the LSP-shaped
// API. The index owns one immutable DecodedRecords allocation and stores only
// integer postings. The generic v1 contract remains symbol-only; providers may
// opt into occurrence postings, while route queries remain outside this type.
class FactQueryIndex {
public:
  struct Reference {
    CodeGraph::VertexId source;
    std::optional<std::string> anchor_file;
    std::optional<int> anchor_line;
  };

  struct Location {
    std::string file_path;
    int start_line{0};
    int start_character{0};
    int end_line{0};
    int end_character{0};
    std::uint32_t roles{0};
    std::optional<CodeGraph::VertexId> target_symbol;
    std::optional<CodeGraph::VertexId> container_symbol;

    bool operator==(const Location &other) const {
      return file_path == other.file_path && start_line == other.start_line &&
             start_character == other.start_character &&
             end_line == other.end_line &&
             end_character == other.end_character && roles == other.roles &&
             target_symbol == other.target_symbol &&
             container_symbol == other.container_symbol;
    }
  };

  struct PositionQueryResult {
    bool served{false};
    std::string fallback_reason;
    std::vector<Location> locations;
    std::vector<CodeGraph::VertexId> targets;
  };

  explicit FactQueryIndex(std::shared_ptr<const DecodedRecords> records,
                          bool require_anchored_references = true,
                          std::string snapshot_id = {});

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
  PositionQueryResult definitions_at(const std::string &file_path, int line,
                                     int character,
                                     std::size_t limit = 8) const;
  PositionQueryResult references_at(const std::string &file_path, int line,
                                    int character, bool include_declaration,
                                    std::size_t limit = 40) const;
  bool has_occurrence_at(const std::string &file_path, int line,
                         int character) const;
  std::vector<Location> position_samples(std::size_t limit = 100) const;

  const std::string &project_root() const { return records_->project_root; }
  std::size_t record_count() const { return records_->vertices.size(); }
  std::size_t edge_count() const { return records_->edges.size(); }
  std::size_t symbol_count() const { return definition_by_name_.size(); }
  std::size_t reference_count() const { return reference_count_; }
  std::size_t occurrence_count() const { return records_->occurrences.size(); }
  const std::string &position_encoding() const {
    return records_->position_encoding;
  }
  bool supports_position_queries() const {
    return !records_->occurrences.empty();
  }
  bool requires_anchored_references() const {
    return require_anchored_references_;
  }
  const std::string &snapshot_id() const { return snapshot_id_; }

private:
  struct FileOccurrencePostings {
    std::vector<std::size_t> indexes;
    std::vector<std::pair<int, int>> prefix_max_end;
  };

  static std::string trim_symbol(std::string value);
  static std::string bare_symbol(const std::string &value);
  static std::string graph_bare_symbol(const std::string &value);
  static std::string lowercase(std::string value);
  void append_candidate(std::vector<CodeGraph::VertexId> &output,
                        CodeGraph::VertexId id, std::size_t limit) const;
  std::vector<std::size_t> occurrences_at(const std::string &file_path,
                                          int line, int character) const;
  PositionQueryResult
  targets_at(const std::string &file_path, int line, int character,
             std::vector<CodeGraph::VertexId> &targets) const;
  std::optional<Location> primary_definition(CodeGraph::VertexId target) const;
  std::vector<CodeGraph::VertexId>
  reference_targets_on_line(const std::string &file_path, int line) const;
  bool source_is_available(const std::string &file_path) const;

  std::shared_ptr<const DecodedRecords> records_;
  std::string snapshot_id_;
  std::unordered_map<std::string, CodeGraph::VertexId> record_by_name_;
  std::unordered_map<std::string, CodeGraph::VertexId> definition_by_name_;
  std::unordered_map<std::string, std::vector<CodeGraph::VertexId>> aliases_;
  std::unordered_map<std::string, std::vector<CodeGraph::VertexId>>
      lsp_aliases_;
  std::vector<std::string> alias_order_;
  std::unordered_map<CodeGraph::VertexId, std::vector<std::size_t>>
      incoming_reference_indexes_;
  std::unordered_map<std::string, FileOccurrencePostings>
      occurrence_indexes_by_file_;
  std::unordered_map<CodeGraph::VertexId, std::vector<std::size_t>>
      occurrence_indexes_by_target_;
  std::size_t reference_count_{0};
  bool require_anchored_references_{true};
};

} // namespace codenib::core
