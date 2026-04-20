#include "code_graph.h"

#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <unordered_set>
#include <utility>

namespace codeminer::core {

namespace {

bool debug_enabled() {
  static const bool enabled = std::getenv("CODEMINER_SCIP_DEBUG") != nullptr;
  return enabled;
}

void log_debug(const std::string &message) {
  if (debug_enabled()) {
    std::cerr << "[CodeGraph] " << message << '\n';
  }
}

std::string escape_json(const std::string &input) {
  std::ostringstream oss;
  oss << '"';
  for (char ch : input) {
    switch (ch) {
    case '\\':
      oss << "\\\\";
      break;
    case '"':
      oss << "\\\"";
      break;
    case '\n':
      oss << "\\n";
      break;
    case '\r':
      oss << "\\r";
      break;
    case '\t':
      oss << "\\t";
      break;
    default:
      if (static_cast<unsigned char>(ch) < 0x20) {
        oss << "\\u" << std::uppercase << std::hex << std::setw(4)
            << std::setfill('0')
            << static_cast<int>(static_cast<unsigned char>(ch))
            << std::nouppercase << std::dec;
        oss << std::setfill(' ');
      } else {
        oss << ch;
      }
      break;
    }
  }
  oss << '"';
  return oss.str();
}

std::optional<std::string> read_file(const std::string &path) {
  std::ifstream input(path);
  if (!input.is_open()) {
    return std::nullopt;
  }

  std::ostringstream buffer;
  buffer << input.rdbuf();
  return buffer.str();
}

} // namespace

CodeGraph::CodeGraph() : CodeGraph(std::string{}) {}

CodeGraph::CodeGraph(std::string project_root)
    : project_root_(std::move(project_root)) {
  if (igraph_empty(&graph_, /*n=*/0, /*directed=*/1) != IGRAPH_SUCCESS) {
    throw std::runtime_error("Failed to initialize igraph instance");
  }
  vertices_.reserve(128);
  edges_.reserve(256);
}

CodeGraph::~CodeGraph() { igraph_destroy(&graph_); }

CodeGraph::CodeGraph(CodeGraph &&other) noexcept : CodeGraph() {
  std::swap(graph_, other.graph_);
  vertices_ = std::move(other.vertices_);
  edges_ = std::move(other.edges_);
  name_to_vertex_ = std::move(other.name_to_vertex_);
  symbol_ranges_ = std::move(other.symbol_ranges_);
  scope_stack_ = std::move(other.scope_stack_);
  project_root_ = std::move(other.project_root_);
  current_file_ = std::move(other.current_file_);
  current_scope_ = std::move(other.current_scope_);
}

CodeGraph &CodeGraph::operator=(CodeGraph &&other) noexcept {
  if (this == &other) {
    return *this;
  }

  CodeGraph tmp(std::move(other));
  std::swap(graph_, tmp.graph_);
  vertices_ = std::move(tmp.vertices_);
  edges_ = std::move(tmp.edges_);
  name_to_vertex_ = std::move(tmp.name_to_vertex_);
  symbol_ranges_ = std::move(tmp.symbol_ranges_);
  scope_stack_ = std::move(tmp.scope_stack_);
  project_root_ = std::move(tmp.project_root_);
  current_file_ = std::move(tmp.current_file_);
  current_scope_ = std::move(tmp.current_scope_);
  return *this;
}

void CodeGraph::reset_scope_to_file(const std::string &file_symbol) {
  scope_stack_.clear();
  scope_stack_.push_back(ScopeEntry{file_symbol, Range{false, 0, 0}});
  current_scope_ = file_symbol;
}

CodeGraph::VertexId CodeGraph::ensure_vertex(const std::string &name) {
  auto it = name_to_vertex_.find(name);
  if (it != name_to_vertex_.end()) {
    log_debug("Reusing vertex for '" + name + "' with id " +
              std::to_string(it->second));
    return it->second;
  }

  if (igraph_add_vertices(&graph_, 1, nullptr) != IGRAPH_SUCCESS) {
    throw std::runtime_error("Failed to add vertex to graph");
  }

  VertexId vertex_id = static_cast<VertexId>(igraph_vcount(&graph_) - 1);
  name_to_vertex_.emplace(name, vertex_id);
  log_debug("Created vertex '" + name + "' with id " +
            std::to_string(vertex_id));

  VertexData data{};
  data.name = name;

  if (static_cast<std::size_t>(igraph_vcount(&graph_)) > vertices_.size()) {
    vertices_.resize(static_cast<std::size_t>(igraph_vcount(&graph_)));
  }
  vertices_[vertex_id] = std::move(data);

  return vertex_id;
}

std::optional<CodeGraph::VertexId>
CodeGraph::get_vertex_id(const std::string &name) const {
  auto it = name_to_vertex_.find(name);
  if (it == name_to_vertex_.end())
    return std::nullopt;
  return it->second;
}

void CodeGraph::set_unified_name(VertexId vertex_id, const std::string &value) {
  if (vertex_id < 0 ||
      static_cast<std::size_t>(vertex_id) >= vertices_.size()) {
    throw std::out_of_range("Vertex id out of range when setting unified_name");
  }
  if (value.empty()) {
    vertices_[vertex_id].unified_name.reset();
  } else {
    vertices_[vertex_id].unified_name = value;
  }
}

void CodeGraph::apply_vertex_update(VertexId vertex_id,
                                    const std::optional<std::string> &type,
                                    const std::optional<std::string> &file,
                                    const std::optional<int> &start_line,
                                    const std::optional<int> &end_line) {
  if (vertex_id < 0 ||
      static_cast<std::size_t>(vertex_id) >= vertices_.size()) {
    throw std::out_of_range("Vertex id out of range when updating attributes");
  }

  VertexData &data = vertices_[vertex_id];

  if (type.has_value()) {
    data.type = *type;
  }
  if (file.has_value()) {
    if (file->empty()) {
      data.file.reset();
    } else {
      data.file = *file;
    }
  }
  if (start_line.has_value()) {
    data.start_line = *start_line;
  }
  if (end_line.has_value()) {
    data.end_line = *end_line;
  }
}

void CodeGraph::add_root_node(const std::string &root_name) {
  VertexId id = ensure_vertex(root_name);
  apply_vertex_update(id, std::string("root"), std::nullopt, std::nullopt,
                      std::nullopt);
}

void CodeGraph::add_directory_node(const std::string &dir_path) {
  VertexId id = ensure_vertex(dir_path);
  apply_vertex_update(id, NODE_TYPE_DIRECTORY, std::nullopt, std::nullopt,
                      std::nullopt);
}

void CodeGraph::add_file_node(const std::string &file_path) {
  current_file_ = file_path;
  VertexId id = ensure_vertex(file_path);
  apply_vertex_update(id, NODE_TYPE_FILE, std::nullopt, std::nullopt,
                      std::nullopt);
  reset_scope_to_file(file_path);
}

void CodeGraph::add_symbol_node(const std::string &symbol, int line,
                                std::optional<int> scope_start_line,
                                std::optional<int> scope_end_line,
                                const std::string &symbol_type) {
  VertexId id = ensure_vertex(symbol);
  apply_vertex_update(id, symbol_type, current_file_, line, line);

  if (scope_start_line.has_value() && scope_end_line.has_value()) {
    apply_vertex_update(id, symbol_type, current_file_, scope_start_line,
                        scope_end_line);
    symbol_ranges_[symbol] = Range{true, *scope_start_line, *scope_end_line};
  }
}

void CodeGraph::add_symbol_reference(
    const std::string &symbol, const std::optional<std::string> &module_path,
    const std::string &symbol_type) {
  const bool already_exists =
      name_to_vertex_.find(symbol) != name_to_vertex_.end();
  VertexId id = ensure_vertex(symbol);
  std::optional<std::string> file_attr =
      already_exists ? std::nullopt : module_path;
  apply_vertex_update(id, symbol_type, file_attr, std::nullopt, std::nullopt);
  add_edge(current_scope_, symbol, EDGE_TYPE_REFERENCE);
}

void CodeGraph::add_containment_edge(const std::string &target_symbol) {
  add_edge(current_scope_, target_symbol, EDGE_TYPE_CONTAIN);
}

void CodeGraph::update_current_scope(const std::string &symbol,
                                     std::optional<int> start_line,
                                     std::optional<int> end_line) {
  current_scope_ = symbol;
  Range range;
  if (start_line.has_value() && end_line.has_value()) {
    range.has_range = true;
    range.start_line = *start_line;
    range.end_line = *end_line;
  }
  scope_stack_.push_back(ScopeEntry{symbol, range});
}

void CodeGraph::exit_scopes_by_line(int current_line) {
  while (scope_stack_.size() > 1) {
    const ScopeEntry &top = scope_stack_.back();
    if (!top.range.has_range) {
      break;
    }
    if (current_line > top.range.end_line) {
      scope_stack_.pop_back();
    } else {
      break;
    }
  }

  if (!scope_stack_.empty()) {
    current_scope_ = scope_stack_.back().symbol;
  } else if (!current_file_.empty()) {
    reset_scope_to_file(current_file_);
  }
}

igraph_integer_t CodeGraph::add_edge(const std::string &source,
                                     const std::string &target,
                                     const std::string &edge_type) {
  log_debug("Adding edge from '" + source + "' to '" + target + "' type '" +
            edge_type + "'");
  VertexId source_id = ensure_vertex(source);
  VertexId target_id = ensure_vertex(target);

  igraph_integer_t eid = -1;
  if (igraph_get_eid(&graph_, &eid, source_id, target_id, /*directed=*/1,
                     /*error=*/0) == IGRAPH_SUCCESS &&
      eid >= 0) {
    log_debug("Edge already exists; returning eid " + std::to_string(eid) +
              " without updating type");
    return eid;
  }

  log_debug("Edge not present; creating new edge");
  if (igraph_add_edge(&graph_, source_id, target_id) != IGRAPH_SUCCESS) {
    throw std::runtime_error("Failed to add edge to graph");
  }

  igraph_integer_t new_eid =
      static_cast<igraph_integer_t>(igraph_ecount(&graph_) - 1);
  if (static_cast<std::size_t>(igraph_ecount(&graph_)) > edges_.size()) {
    edges_.resize(static_cast<std::size_t>(igraph_ecount(&graph_)));
  }
  edges_[new_eid] = EdgeData{source_id, target_id, edge_type};
  return new_eid;
}

void CodeGraph::batch_upsert_nodes(const std::vector<VertexData> &nodes) {
  if (nodes.empty()) {
    return;
  }

  // Collect new vertices and their update data in a single pass
  std::vector<VertexData> new_vertex_data; // Store data for new vertices only
  std::unordered_set<std::string>
      seen_in_batch; // Track names already seen in this batch

  for (const auto &data : nodes) {
    // Check both existing vertices and already-seen names in this batch
    if (name_to_vertex_.find(data.name) == name_to_vertex_.end() &&
        seen_in_batch.find(data.name) == seen_in_batch.end()) {
      new_vertex_data.push_back(data);
      seen_in_batch.insert(data.name);
    }
  }

  // Batch create new vertices
  if (!new_vertex_data.empty()) {
    igraph_integer_t n_new =
        static_cast<igraph_integer_t>(new_vertex_data.size());
    if (igraph_add_vertices(&graph_, n_new, nullptr) != IGRAPH_SUCCESS) {
      throw std::runtime_error("Failed to batch add vertices to graph");
    }

    igraph_integer_t start_id =
        static_cast<igraph_integer_t>(igraph_vcount(&graph_) - n_new);

    // Resize vertices_ if needed
    if (static_cast<std::size_t>(igraph_vcount(&graph_)) > vertices_.size()) {
      vertices_.resize(static_cast<std::size_t>(igraph_vcount(&graph_)));
    }

    // Register new vertices, initialize and update their data
    for (std::size_t i = 0; i < new_vertex_data.size(); ++i) {
      VertexId vertex_id = start_id + static_cast<VertexId>(i);
      const auto &data = new_vertex_data[i];

      name_to_vertex_.emplace(data.name, vertex_id);

      VertexData vdata{};
      vdata.name = data.name;
      vertices_[vertex_id] = std::move(vdata);

      log_debug("Batch created vertex '" + data.name + "' with id " +
                std::to_string(vertex_id));

      // Update vertex attributes
      apply_vertex_update(vertex_id, std::make_optional<std::string>(data.type),
                          data.file, data.start_line, data.end_line);
      if (data.unified_name.has_value()) {
        vertices_[vertex_id].unified_name = data.unified_name;
      }

      if (data.start_line.has_value() && data.end_line.has_value()) {
        symbol_ranges_[data.name] =
            Range{true, *data.start_line, *data.end_line};
      }
    }
  }
}

void CodeGraph::batch_add_edges(
    const std::vector<std::tuple<std::string, std::string, std::string>>
        &edges) {
  if (edges.empty()) {
    return;
  }

  // Build edge list for igraph, deduplicating using a set
  struct EdgeKey {
    VertexId source;
    VertexId target;

    bool operator==(const EdgeKey &other) const {
      return source == other.source && target == other.target;
    }
  };

  struct EdgeKeyHash {
    std::size_t operator()(const EdgeKey &key) const {
      return std::hash<VertexId>{}(key.source) ^
             (std::hash<VertexId>{}(key.target) << 1);
    }
  };

  std::unordered_set<EdgeKey, EdgeKeyHash> existing_edges;

  // Collect existing edges to avoid duplicates
  igraph_integer_t current_ecount = igraph_ecount(&graph_);
  for (igraph_integer_t i = 0; i < current_ecount; ++i) {
    if (i < static_cast<igraph_integer_t>(edges_.size())) {
      existing_edges.insert(EdgeKey{edges_[i].source, edges_[i].target});
    }
  }

  igraph_vector_int_t edge_vector;
  igraph_vector_int_init(&edge_vector,
                         static_cast<igraph_integer_t>(edges.size() * 2));

  std::vector<EdgeData> new_edge_data;
  new_edge_data.reserve(edges.size());

  igraph_integer_t edge_idx = 0;
  for (const auto &[source, target, type] : edges) {
    // Verify vertices exist (they should have been created by
    // batch_upsert_nodes)
    auto source_it = name_to_vertex_.find(source);
    auto target_it = name_to_vertex_.find(target);

    if (source_it == name_to_vertex_.end()) {
      log_debug("Warning: source vertex '" + source +
                "' not found, skipping edge");
      continue;
    }
    if (target_it == name_to_vertex_.end()) {
      log_debug("Warning: target vertex '" + target +
                "' not found, skipping edge");
      continue;
    }

    VertexId source_id = source_it->second;
    VertexId target_id = target_it->second;

    EdgeKey key{source_id, target_id};

    // Check if edge already exists in our local set
    if (existing_edges.find(key) != existing_edges.end()) {
      log_debug("Edge from '" + source + "' to '" + target +
                "' already exists, skipping");
      continue;
    }

    // Mark as existing to prevent duplicates within this batch
    existing_edges.insert(key);

    // Add to edge vector
    VECTOR(edge_vector)[edge_idx * 2] = source_id;
    VECTOR(edge_vector)[edge_idx * 2 + 1] = target_id;
    edge_idx++;

    new_edge_data.push_back(EdgeData{source_id, target_id, type});
    log_debug("Batch adding edge from '" + source + "' to '" + target +
              "' type '" + type + "'");
  }

  // Resize the vector to actual number of edges to add (excluding duplicates)
  igraph_vector_int_resize(&edge_vector, edge_idx * 2);

  // Batch add edges to igraph
  if (edge_idx > 0) {
    if (igraph_add_edges(&graph_, &edge_vector, nullptr) != IGRAPH_SUCCESS) {
      igraph_vector_int_destroy(&edge_vector);
      throw std::runtime_error("Failed to batch add edges to graph");
    }

    // Resize edges_ and store edge data
    igraph_integer_t new_ecount = igraph_ecount(&graph_);
    if (static_cast<std::size_t>(new_ecount) > edges_.size()) {
      edges_.resize(static_cast<std::size_t>(new_ecount));
    }

    igraph_integer_t start_eid =
        new_ecount - static_cast<igraph_integer_t>(new_edge_data.size());
    for (std::size_t i = 0; i < new_edge_data.size(); ++i) {
      edges_[start_eid + static_cast<igraph_integer_t>(i)] =
          std::move(new_edge_data[i]);
    }
  }

  igraph_vector_int_destroy(&edge_vector);
}

std::optional<CodeGraph::VertexData>
CodeGraph::get_node_info_by_name(const std::string &node_name) const {
  auto it = name_to_vertex_.find(node_name);
  if (it == name_to_vertex_.end()) {
    return std::nullopt;
  }
  return get_node_info_by_id(it->second);
}

std::optional<CodeGraph::VertexData>
CodeGraph::get_node_info_by_id(VertexId vertex_id) const {
  if (vertex_id < 0 ||
      static_cast<std::size_t>(vertex_id) >= vertices_.size()) {
    return std::nullopt;
  }
  return vertices_[vertex_id];
}

std::vector<CodeGraph::VertexId>
CodeGraph::get_neighbors(const std::string &node_name) const {
  std::vector<VertexId> results;
  auto it = name_to_vertex_.find(node_name);
  if (it == name_to_vertex_.end()) {
    return results;
  }

  igraph_vector_int_t neighbors;
  igraph_vector_int_init(&neighbors, 0);
  igraph_neighbors(const_cast<igraph_t *>(&graph_), &neighbors, it->second,
                   IGRAPH_OUT);

  igraph_integer_t count = neighbors.stor_end - neighbors.stor_begin;
  for (igraph_integer_t i = 0; i < count; ++i) {
    results.push_back(static_cast<VertexId>(neighbors.stor_begin[i]));
  }
  igraph_vector_int_destroy(&neighbors);
  return results;
}

std::string CodeGraph::get_node_content(VertexId vertex_id) const {
  auto vertex_opt = get_node_info_by_id(vertex_id);
  if (!vertex_opt.has_value()) {
    return {};
  }
  const VertexData &data = vertex_opt.value();
  if (data.type == NODE_TYPE_FILE) {
    std::string file_path = data.name;
    if (!project_root_.empty()) {
      file_path = std::filesystem::path(project_root_) / file_path;
    }
    auto file_content = read_file(file_path);
    if (file_content.has_value()) {
      return *file_content;
    }
    return {};
  }

  if ((data.type == NODE_TYPE_CLASS || data.type == NODE_TYPE_FUNCTION ||
       data.type == NODE_TYPE_METHOD || data.type == NODE_TYPE_FIELD ||
       data.type == NODE_TYPE_SYMBOL) &&
      data.file.has_value() && data.start_line.has_value() &&
      data.end_line.has_value()) {
    std::string file_path = *data.file;
    if (!project_root_.empty()) {
      file_path = std::filesystem::path(project_root_) / file_path;
    }

    std::ifstream input(file_path);
    if (!input.is_open()) {
      return {};
    }

    std::vector<std::string> lines;
    std::string line;
    while (std::getline(input, line)) {
      lines.push_back(line);
    }

    int start_index = std::max(0, data.start_line.value() - 1);
    int end_index =
        std::min(static_cast<int>(lines.size()), data.end_line.value());
    if (start_index >= end_index) {
      return {};
    }

    std::ostringstream snippet;
    for (int i = start_index; i < end_index; ++i) {
      snippet << lines[i] << '\n';
    }
    return snippet.str();
  }
  return {};
}

void CodeGraph::save_graph(const std::string &output_path) const {
  std::ofstream out(output_path);
  if (!out.is_open()) {
    throw std::runtime_error("Failed to open output file for writing graph");
  }

  out << "{\n";
  out << "  \"project_root\": " << escape_json(project_root_) << ",\n";
  out << "  \"vertices\": [\n";
  for (std::size_t i = 0; i < vertices_.size(); ++i) {
    const auto &v = vertices_[i];
    out << "    {\n";
    out << "      \"id\": " << i << ",\n";
    out << "      \"name\": " << escape_json(v.name) << ",\n";
    out << "      \"type\": " << escape_json(v.type) << ",\n";
    out << "      \"file\": ";
    if (v.file.has_value()) {
      out << escape_json(*v.file);
    } else {
      out << "null";
    }
    out << ",\n";
    out << "      \"start_line\": ";
    if (v.start_line.has_value()) {
      out << *v.start_line;
    } else {
      out << "null";
    }
    out << ",\n";
    out << "      \"end_line\": ";
    if (v.end_line.has_value()) {
      out << *v.end_line;
    } else {
      out << "null";
    }
    out << ",\n";
    out << "      \"unified_name\": ";
    if (v.unified_name.has_value()) {
      out << escape_json(*v.unified_name);
    } else {
      out << "null";
    }
    out << "\n";
    out << "    }";
    if (i + 1 < vertices_.size()) {
      out << ",";
    }
    out << "\n";
  }
  out << "  ],\n";

  out << "  \"edges\": [\n";
  for (std::size_t i = 0; i < edges_.size(); ++i) {
    const auto &e = edges_[i];
    out << "    {\n";
    out << "      \"id\": " << i << ",\n";
    out << "      \"source\": " << e.source << ",\n";
    out << "      \"target\": " << e.target << ",\n";
    out << "      \"type\": " << escape_json(e.type) << "\n";
    out << "    }";
    if (i + 1 < edges_.size()) {
      out << ",";
    }
    out << "\n";
  }
  out << "  ],\n";

  out << "  \"symbol_ranges\": {\n";
  std::size_t counter = 0;
  for (const auto &[symbol, range] : symbol_ranges_) {
    out << "    " << escape_json(symbol) << ": ";
    if (range.has_range) {
      out << "[" << range.start_line << ", " << range.end_line << "]";
    } else {
      out << "null";
    }
    if (++counter < symbol_ranges_.size()) {
      out << ",";
    }
    out << "\n";
  }
  out << "  }\n";
  out << "}\n";
}

CodeGraph CodeGraph::load_graph(const std::string & /*input_path*/) {
  throw std::runtime_error(
      "CodeGraph::load_graph is not implemented yet for the C++ core");
}

} // namespace codeminer::core
