/*
 * SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#pragma once

#include <igraph/igraph.h>

#include <optional>
#include <string>
#include <unordered_map>
#include <vector>

namespace codenib::core {

constexpr const char *NODE_TYPE_DIRECTORY = "directory";
constexpr const char *NODE_TYPE_FILE = "file";
constexpr const char *NODE_TYPE_SYMBOL = "symbol";
constexpr const char *NODE_TYPE_CLASS = "class";
constexpr const char *NODE_TYPE_FUNCTION = "function";
constexpr const char *NODE_TYPE_METHOD = "method";
constexpr const char *NODE_TYPE_FIELD = "field";
constexpr const char *EDGE_TYPE_CONTAIN = "contain";
constexpr const char *EDGE_TYPE_REFERENCE = "reference";
constexpr const char *EDGE_TYPE_IMPORT = "import";
constexpr const char *EDGE_TYPE_TYPE_USE = "type-use";
constexpr const char *GRAPH_LAYER_ALL = "all";
constexpr const char *GRAPH_LAYER_CONTAINMENT = "containment";
constexpr const char *GRAPH_LAYER_DEPENDENCY = "dependency";
constexpr const char *GRAPH_LAYER_REFERENCE = "reference";
constexpr const char *GRAPH_LAYER_IMPORT = "import";
constexpr const char *GRAPH_LAYER_TYPE_USE = "type-use";
constexpr const char *ROOT_NODE = ".";

class CodeGraph {
public:
  using VertexId = igraph_integer_t;

  struct VertexData {
    std::string name;
    std::string type;
    std::optional<std::string> file;
    std::optional<int> start_line;
    std::optional<int> end_line;
    // Identifier/declaration line from the SCIP occurrence. Unlike
    // start_line, this excludes decorators, annotations, and doc comments.
    std::optional<int> selection_line;
    // Cross-language display path: usually `{file}:{SymbolDisplay}`.
    // Matches the `unified_name` vertex attribute produced by the
    // Python-side decoders in codenib/scip_interface/.
    std::optional<std::string> unified_name;
  };

  struct EdgeData {
    VertexId source;
    VertexId target;
    std::string type;
    // Optional call-site location (LSP-aligned line-range query support).
    // Set for reference edges that carry `occurrence.range` info; nullopt
    // for structural edges (file/dir containment, inheritance).
    std::optional<std::string> anchor_file;
    std::optional<int> anchor_line;
  };

  CodeGraph();
  explicit CodeGraph(std::string project_root);
  ~CodeGraph();

  CodeGraph(const CodeGraph &) = delete;
  CodeGraph &operator=(const CodeGraph &) = delete;
  CodeGraph(CodeGraph &&other) noexcept;
  CodeGraph &operator=(CodeGraph &&other) noexcept;

  void add_root_node(const std::string &root_name);
  void add_directory_node(const std::string &dir_path);
  void add_file_node(const std::string &file_path);

  void add_symbol_node(const std::string &symbol, int line,
                       std::optional<int> scope_start_line = std::nullopt,
                       std::optional<int> scope_end_line = std::nullopt,
                       const std::string &symbol_type = NODE_TYPE_SYMBOL);

  void add_symbol_reference(
      const std::string &symbol,
      const std::optional<std::string> &module_path = std::nullopt,
      const std::string &symbol_type = NODE_TYPE_SYMBOL,
      std::optional<std::string> anchor_file = std::nullopt,
      std::optional<int> anchor_line = std::nullopt);

  // CONTAIN edges deliberately carry no anchor — containment is a structural
  // relation, not a call/reference site. (See anchor invariant ii on
  // CodeGraph::query_range in the Python side.)
  void add_containment_edge(const std::string &target_symbol);
  void update_current_scope(const std::string &symbol,
                            std::optional<int> start_line = std::nullopt,
                            std::optional<int> end_line = std::nullopt);
  void exit_scopes_by_line(int current_line);

  // Edges between `(source, target)` collapse on the full key
  // `(source, target, edge_type, anchor_file, anchor_line)`. Distinct
  // anchors yield distinct multi-edges; an identical key returns the
  // existing eid. Pass `anchor_file` / `anchor_line` to record the
  // call-site location for range-query support.
  igraph_integer_t
  add_edge(const std::string &source, const std::string &target,
           const std::string &edge_type,
           std::optional<std::string> anchor_file = std::nullopt,
           std::optional<int> anchor_line = std::nullopt);

  void batch_upsert_nodes(const std::vector<VertexData> &nodes);
  // Each entry: (source, target, type, anchor_file, anchor_line).
  // Identical `(source, target, type, anchor_file, anchor_line)` collapses
  // to a single edge; multi-edges across distinct anchors are allowed.
  // Mirrors the per-decoder ``indexed_directories`` tracking the serial
  // Python decoders apply at decoder scope.
  void batch_add_edges(
      const std::vector<
          std::tuple<std::string, std::string, std::string,
                     std::optional<std::string>, std::optional<int>>> &edges);

  std::optional<VertexData>
  get_node_info_by_name(const std::string &node_name) const;
  std::optional<VertexData> get_node_info_by_id(VertexId vertex_id) const;

  // Lookup the vertex id by name (nullopt if absent).
  std::optional<VertexId> get_vertex_id(const std::string &name) const;

  // Set unified_name attribute on a vertex (language-specific decoders
  // compute this; Python decoders store it as a vertex attribute).
  void set_unified_name(VertexId vertex_id, const std::string &value);

  // Direct access to vertex data — used by language-specific post-pass
  // (e.g. Python `_fix_unified_names`).
  const std::vector<VertexData> &vertices() const { return vertices_; }
  std::vector<VertexData> &mutable_vertices() { return vertices_; }

  // Direct access to edges (source/target/type). Parallel to `graph()`'s
  // igraph structure so edges[i] describes the i-th edge in the igraph.
  const std::vector<EdgeData> &edges() const { return edges_; }

  std::vector<VertexId> get_neighbors(const std::string &node_name) const;
  std::string get_node_content(VertexId vertex_id) const;

  void save_graph(const std::string &output_path) const;
  static CodeGraph load_graph(const std::string &input_path);

  const igraph_t &graph() const { return graph_; }
  igraph_t &graph() { return graph_; }

  const std::string &project_root() const { return project_root_; }
  void set_project_root(std::string project_root) {
    project_root_ = std::move(project_root);
  }

  const std::string &current_scope() const { return current_scope_; }

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

  VertexId ensure_vertex(const std::string &name);
  void apply_vertex_update(VertexId vertex_id,
                           const std::optional<std::string> &type,
                           const std::optional<std::string> &file,
                           const std::optional<int> &start_line,
                           const std::optional<int> &end_line);

  void reset_scope_to_file(const std::string &file_symbol);

  igraph_t graph_{};
  std::vector<VertexData> vertices_;
  std::vector<EdgeData> edges_;
  std::unordered_map<std::string, VertexId> name_to_vertex_;
  std::unordered_map<std::string, Range> symbol_ranges_;
  std::vector<ScopeEntry> scope_stack_;

  std::string project_root_;
  std::string current_file_;
  std::string current_scope_;
};

} // namespace codenib::core
