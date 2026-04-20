#include "scip_decode_common.h"

#include <utility>

namespace codeminer::core {

Subgraph::Node &SubgraphBuilder::ensure_node(const std::string &name) {
  auto it = subgraph_.nodes.find(name);
  if (it != subgraph_.nodes.end()) {
    return it->second;
  }
  Subgraph::Node record;
  record.data.name = name;
  auto inserted = subgraph_.nodes.emplace(name, std::move(record));
  return inserted.first->second;
}

void SubgraphBuilder::apply_update(Subgraph::Node &node,
                                   const std::optional<std::string> &type,
                                   const std::optional<std::string> &file,
                                   const std::optional<int> &start_line,
                                   const std::optional<int> &end_line) {
  if (type.has_value()) {
    node.data.type = *type;
  }
  if (file.has_value()) {
    if (file->empty()) {
      node.data.file.reset();
    } else {
      node.data.file = *file;
    }
  }
  if (start_line.has_value()) {
    node.data.start_line = *start_line;
  }
  if (end_line.has_value()) {
    node.data.end_line = *end_line;
  }
}

void SubgraphBuilder::add_directory_node(const std::string &dir_path) {
  Subgraph::Node &node = ensure_node(dir_path);
  apply_update(node, std::make_optional<std::string>(NODE_TYPE_DIRECTORY),
               std::nullopt, std::nullopt, std::nullopt);
}

bool SubgraphBuilder::add_directory_if_needed(const std::string &dir_path) {
  if (dir_path.empty())
    return false;
  if (indexed_directories_.insert(dir_path).second) {
    add_directory_node(dir_path);
    return true;
  }
  return false;
}

void SubgraphBuilder::add_file_node(const std::string &file_path) {
  current_file_ = file_path;
  Subgraph::Node &node = ensure_node(file_path);
  apply_update(node, std::make_optional<std::string>(NODE_TYPE_FILE),
               std::nullopt, std::nullopt, std::nullopt);
  reset_scope_to_file(file_path);
}

void SubgraphBuilder::add_symbol_node(const std::string &symbol, int line,
                                      std::optional<int> scope_start_line,
                                      std::optional<int> scope_end_line,
                                      const std::string &symbol_type) {
  Subgraph::Node &node = ensure_node(symbol);
  // Definitions ALWAYS overwrite (matches serial add_symbol_node semantics).
  apply_update(node, std::make_optional<std::string>(symbol_type),
               std::make_optional<std::string>(current_file_),
               std::make_optional<int>(line), std::make_optional<int>(line));
  if (scope_start_line.has_value() && scope_end_line.has_value()) {
    apply_update(node, std::make_optional<std::string>(symbol_type),
                 std::make_optional<std::string>(current_file_),
                 scope_start_line, scope_end_line);
  }
  node.is_definition = true;
}

void SubgraphBuilder::add_symbol_reference(
    const std::string &symbol, const std::optional<std::string> &module_path,
    const std::string &symbol_type) {
  // Serial CodeGraph.add_symbol_reference: only populates attrs on FIRST
  // add. Subsequent refs leave the node alone (defs still overwrite via
  // add_symbol_node).
  bool newly_created = subgraph_.nodes.find(symbol) == subgraph_.nodes.end();
  Subgraph::Node &node = ensure_node(symbol);
  if (newly_created) {
    apply_update(node, std::make_optional<std::string>(symbol_type),
                 module_path.has_value() && !module_path->empty()
                     ? std::make_optional<std::string>(*module_path)
                     : std::nullopt,
                 std::nullopt, std::nullopt);
    node.is_definition = false;
  }
  add_edge(current_scope_, symbol, EDGE_TYPE_REFERENCE);
}

void SubgraphBuilder::add_containment_edge(const std::string &target_symbol) {
  add_edge(current_scope_, target_symbol, EDGE_TYPE_CONTAIN);
}

void SubgraphBuilder::add_edge(const std::string &source,
                               const std::string &target,
                               const std::string &edge_type) {
  subgraph_.edges.push_back(Subgraph::Edge{source, target, edge_type});
}

void SubgraphBuilder::set_unified_name(const std::string &symbol,
                                       const std::string &value,
                                       bool only_if_missing) {
  Subgraph::Node &node = ensure_node(symbol);
  if (only_if_missing && node.data.unified_name.has_value()) {
    return;
  }
  node.data.unified_name = value;
}

void SubgraphBuilder::update_current_scope(const std::string &symbol,
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

void SubgraphBuilder::exit_scopes_by_line(int current_line) {
  while (scope_stack_.size() > 1) {
    const ScopeEntry &top = scope_stack_.back();
    if (!top.range.has_range)
      break;
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

void SubgraphBuilder::reset_scope_to_file(const std::string &file_symbol) {
  scope_stack_.clear();
  scope_stack_.push_back(ScopeEntry{file_symbol, Range{false, 0, 0}});
  current_scope_ = file_symbol;
}

} // namespace codeminer::core
