// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

#include "scip_decode_common.h"

#include <algorithm>
#include <cctype>
#include <filesystem>
#include <sstream>
#include <utility>

namespace codenib::core {

std::vector<int> extract_integers(const std::string &text,
                                  const re2::RE2 &pattern) {
  std::vector<int> results;
  re2::StringPiece input(text);
  int value = 0;
  while (re2::RE2::FindAndConsume(&input, pattern, &value)) {
    results.push_back(value);
  }
  return results;
}

bool ends_with(const std::string &s, const std::string &suffix) {
  return s.size() >= suffix.size() &&
         s.compare(s.size() - suffix.size(), suffix.size(), suffix) == 0;
}

std::string rstrip_chars(std::string s, const std::string &chars) {
  while (!s.empty() && chars.find(s.back()) != std::string::npos)
    s.pop_back();
  return s;
}

std::string strip_backticks(std::string s) {
  s.erase(std::remove(s.begin(), s.end(), '`'), s.end());
  return s;
}

std::vector<std::string> split_ws(const std::string &s) {
  std::vector<std::string> out;
  std::istringstream iss(s);
  std::string tok;
  while (iss >> tok)
    out.push_back(tok);
  return out;
}

std::vector<std::string> split_ws_limit(const std::string &s, int limit) {
  if (limit <= 1) {
    std::string value = s;
    std::size_t start = 0;
    while (start < value.size() &&
           std::isspace(static_cast<unsigned char>(value[start]))) {
      ++start;
    }
    value = value.substr(start);
    while (!value.empty() &&
           std::isspace(static_cast<unsigned char>(value.back()))) {
      value.pop_back();
    }
    return value.empty() ? std::vector<std::string>{}
                         : std::vector<std::string>{std::move(value)};
  }

  std::vector<std::string> out;
  std::size_t i = 0;
  while (i < s.size() && static_cast<int>(out.size()) < limit - 1) {
    while (i < s.size() && std::isspace(static_cast<unsigned char>(s[i])))
      ++i;
    std::size_t start = i;
    while (i < s.size() && !std::isspace(static_cast<unsigned char>(s[i])))
      ++i;
    if (start < i)
      out.emplace_back(s.substr(start, i - start));
  }

  while (i < s.size() && std::isspace(static_cast<unsigned char>(s[i])))
    ++i;
  if (i < s.size())
    out.emplace_back(s.substr(i));
  return out;
}

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

void SubgraphBuilder::add_file_hierarchy(const std::string &file_path) {
  std::filesystem::path file_fs(file_path);
  auto dir = file_fs.parent_path();
  while (!dir.empty() && dir != dir.parent_path()) {
    std::string dir_str = dir.generic_string();
    if (add_directory_if_needed(dir_str)) {
      std::string parent = dir.parent_path().generic_string();
      if (parent.empty()) {
        parent = ROOT_NODE;
      }
      add_edge(parent, dir_str, EDGE_TYPE_CONTAIN);
    }
    dir = dir.parent_path();
  }

  add_file_node(file_path);
  std::string parent = file_fs.parent_path().generic_string();
  if (parent.empty()) {
    parent = ROOT_NODE;
  }
  add_edge(parent, file_path, EDGE_TYPE_CONTAIN);
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
  node.data.selection_line = line;
  node.is_definition = true;
}

void SubgraphBuilder::add_symbol_reference(
    const std::string &symbol, const std::optional<std::string> &module_path,
    const std::string &symbol_type, std::optional<std::string> anchor_file,
    std::optional<int> anchor_line) {
  add_symbol_reference_from(current_scope_, symbol, module_path, symbol_type,
                            std::move(anchor_file), anchor_line);
}

void SubgraphBuilder::add_symbol_reference_from(
    const std::string &source, const std::string &symbol,
    const std::optional<std::string> &module_path,
    const std::string &symbol_type, std::optional<std::string> anchor_file,
    std::optional<int> anchor_line) {
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
  // Default anchor_file to the current document so range queries can
  // route reference edges to the file they originate from.
  if (!anchor_file.has_value() && !current_file_.empty()) {
    anchor_file = current_file_;
  }
  add_edge(source, symbol, EDGE_TYPE_REFERENCE, anchor_file, anchor_line);
}

void SubgraphBuilder::add_containment_edge(const std::string &target_symbol) {
  // CONTAIN edges carry no anchor — see anchor invariant ii on the Python
  // CodeGraph::query_range docstring.
  add_edge(current_scope_, target_symbol, EDGE_TYPE_CONTAIN);
}

void SubgraphBuilder::add_edge(const std::string &source,
                               const std::string &target,
                               const std::string &edge_type,
                               std::optional<std::string> anchor_file,
                               std::optional<int> anchor_line) {
  subgraph_.edges.push_back(Subgraph::Edge{
      source, target, edge_type, std::move(anchor_file), anchor_line});
}

void SubgraphBuilder::set_unified_name(const std::string &symbol,
                                       const std::string &value,
                                       bool only_if_missing) {
  Subgraph::Node &node = ensure_node(symbol);
  if (only_if_missing && node.data.unified_name.has_value()) {
    return;
  }
  node.data.unified_name = value;
  if (!only_if_missing) {
    node.updates_unified_name = true;
  }
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

bool SubgraphBuilder::has_node(const std::string &name) const {
  return subgraph_.nodes.find(name) != subgraph_.nodes.end();
}

const Subgraph::Node *SubgraphBuilder::node(const std::string &name) const {
  auto it = subgraph_.nodes.find(name);
  if (it == subgraph_.nodes.end())
    return nullptr;
  return &it->second;
}

void SubgraphBuilder::reset_scope_to_file(const std::string &file_symbol) {
  scope_stack_.clear();
  scope_stack_.push_back(ScopeEntry{file_symbol, Range{false, 0, 0}});
  current_scope_ = file_symbol;
}

} // namespace codenib::core
