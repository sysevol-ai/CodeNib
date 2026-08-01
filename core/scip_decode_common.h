/*
 * SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#pragma once

#include "code_graph.h"

#include <optional>
#include <re2/re2.h>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

namespace codenib::core {

// Reusable language-neutral helpers for SCIP text-format decoders. Keep
// language policy in language decoders; put only syntax/string primitives here.
std::vector<int> extract_integers(const std::string &text,
                                  const re2::RE2 &pattern);
bool ends_with(const std::string &s, const std::string &suffix);
std::string rstrip_chars(std::string s, const std::string &chars);
std::string strip_backticks(std::string s);
std::vector<std::string> split_ws(const std::string &s);
std::vector<std::string> split_ws_limit(const std::string &s, int limit);

// Per-document result produced by each language decoder worker. Language-
// agnostic (symbol payload lives in VertexData + extras). Merged by
// `SCIPDecoderBase::merge_subgraphs` in document order to reproduce serial
// semantics.
struct Subgraph {
  struct Edge {
    std::string source;
    std::string target;
    std::string type;
    // Optional call-site location (LSP-aligned line-range query support).
    // Set for reference edges threaded with `occurrence.range`; nullopt for
    // structural edges (file/dir containment, inheritance) where there is
    // no meaningful anchor.
    std::optional<std::string> anchor_file;
    std::optional<int> anchor_line;
  };

  struct Node {
    CodeGraph::VertexData data; // includes unified_name
    // This flag applies only to symbol definitions. Structural file and
    // directory nodes must not acquire symbol-definition metadata when
    // per-document subgraphs are merged.
    bool is_definition{false};
    // File and directory insertion uses the same upsert semantics as the
    // serial graph: a later structural node replaces only the node type,
    // even when the name previously denoted a symbol.
    bool updates_structural_type{false};
    bool updates_unified_name{false};
  };

  std::unordered_map<std::string, Node> nodes;
  std::vector<Edge> edges;
};

// Per-document builder — instantiated per worker thread so each parallel
// document parse owns its mutable state (current_file_, scope_stack_,
// subgraph_). Merged into the final graph by
// `SCIPDecoderBase::merge_subgraphs`.
class SubgraphBuilder {
public:
  SubgraphBuilder() = default;

  void add_directory_node(const std::string &dir_path);
  bool add_directory_if_needed(const std::string &dir_path);
  void add_file_node(const std::string &file_path);
  void add_file_hierarchy(const std::string &file_path);

  // Definition — always sets type/file/start/end/is_definition.
  void add_symbol_node(const std::string &symbol, int line,
                       std::optional<int> scope_start_line,
                       std::optional<int> scope_end_line,
                       const std::string &symbol_type,
                       std::optional<std::string> symbol_kind = std::nullopt);

  // Reference — mirrors serial CodeGraph.add_symbol_reference:
  //   attributes are set only the FIRST time the name is seen; subsequent
  //   references leave existing attrs alone (defs via add_symbol_node are
  //   the only thing that overwrites). Pass `anchor_line` for the call-site
  //   line; `anchor_file` defaults to the current document.
  void
  add_symbol_reference(const std::string &symbol,
                       const std::optional<std::string> &module_path,
                       const std::string &symbol_type,
                       std::optional<std::string> anchor_file = std::nullopt,
                       std::optional<int> anchor_line = std::nullopt,
                       std::optional<std::string> symbol_kind = std::nullopt);
  void add_symbol_reference_from(
      const std::string &source, const std::string &symbol,
      const std::optional<std::string> &module_path,
      const std::string &symbol_type,
      std::optional<std::string> anchor_file = std::nullopt,
      std::optional<int> anchor_line = std::nullopt,
      std::optional<std::string> symbol_kind = std::nullopt);

  // CONTAIN edges carry no anchor — see CodeGraph::add_containment_edge.
  void add_containment_edge(const std::string &target_symbol);
  void add_edge(const std::string &source, const std::string &target,
                const std::string &edge_type,
                std::optional<std::string> anchor_file = std::nullopt,
                std::optional<int> anchor_line = std::nullopt);

  // Set unified_name on a node. When `only_if_missing`, preserves an
  // already-set value (matches serial "first-ref-wins for unified_name"
  // behavior for references).
  void set_unified_name(const std::string &symbol, const std::string &value,
                        bool only_if_missing = false);

  void update_current_scope(const std::string &symbol,
                            std::optional<int> start_line = std::nullopt,
                            std::optional<int> end_line = std::nullopt);
  void exit_scopes_by_line(int current_line);

  const std::string &current_scope() const { return current_scope_; }
  const std::string &current_file() const { return current_file_; }
  bool has_node(const std::string &name) const;
  const Subgraph::Node *node(const std::string &name) const;

  Subgraph build() { return std::move(subgraph_); }

private:
  struct Range {
    bool has_range{false};
    int start_line{0};
    int end_line{0};
  };

  struct ScopeEntry {
    std::string symbol;
    Range range;
  };

  Subgraph::Node &ensure_node(const std::string &name);
  void apply_update(Subgraph::Node &node,
                    const std::optional<std::string> &type,
                    const std::optional<std::string> &file,
                    const std::optional<int> &start_line,
                    const std::optional<int> &end_line);

  void reset_scope_to_file(const std::string &file_symbol);

  Subgraph subgraph_;
  std::unordered_set<std::string> indexed_directories_;
  std::vector<ScopeEntry> scope_stack_;
  std::string current_file_;
  std::string current_scope_;
};

} // namespace codenib::core
