/*
 * SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#include "fact_query_index.h"

#include <algorithm>
#include <cctype>
#include <stdexcept>
#include <unordered_set>
#include <utility>

namespace codenib::core {
namespace {

bool is_symbol_type(const std::string &type) {
  return type == NODE_TYPE_SYMBOL || type == NODE_TYPE_CLASS ||
         type == NODE_TYPE_FUNCTION || type == NODE_TYPE_METHOD ||
         type == NODE_TYPE_FIELD;
}

bool has_definition(const CodeGraph::VertexData &vertex) {
  if (vertex.has_definition.has_value())
    return *vertex.has_definition;
  return is_symbol_type(vertex.type) && vertex.file.has_value() &&
         vertex.start_line.has_value() && vertex.end_line.has_value();
}

bool has_suffix(const std::string &value, const std::string &suffix) {
  return value.size() >= suffix.size() &&
         value.compare(value.size() - suffix.size(), suffix.size(), suffix) ==
             0;
}

} // namespace

FactQueryIndex::FactQueryIndex(std::shared_ptr<const DecodedRecords> records,
                               bool require_anchored_references)
    : records_(std::move(records)),
      require_anchored_references_(require_anchored_references) {
  if (!records_)
    throw std::invalid_argument("FactQueryIndex records must not be null");

  record_by_name_.reserve(records_->vertices.size());
  definition_by_name_.reserve(records_->vertices.size());
  aliases_.reserve(records_->vertices.size() * 2);
  lsp_aliases_.reserve(records_->vertices.size() * 2);
  alias_order_.reserve(records_->vertices.size() * 2);

  auto add_alias = [&](const std::string &alias, CodeGraph::VertexId id) {
    if (alias.empty())
      return;
    auto [entry, inserted] = aliases_.try_emplace(alias);
    if (inserted)
      alias_order_.push_back(alias);
    entry->second.push_back(id);
  };

  for (std::size_t index = 0; index < records_->vertices.size(); ++index) {
    const auto id = static_cast<CodeGraph::VertexId>(index);
    const auto &vertex = records_->vertices[index];
    if (vertex.name.empty())
      throw std::invalid_argument(
          "FactQueryIndex vertex name must not be empty");
    if (!record_by_name_.emplace(vertex.name, id).second)
      throw std::invalid_argument(
          "FactQueryIndex requires globally unique vertex names");

    if (has_definition(vertex)) {
      if (!is_symbol_type(vertex.type) || !vertex.file.has_value() ||
          vertex.file->empty() || !vertex.start_line.has_value() ||
          !vertex.end_line.has_value() || *vertex.start_line < 0 ||
          *vertex.end_line < *vertex.start_line ||
          (vertex.selection_line.has_value() && *vertex.selection_line < 0)) {
        throw std::invalid_argument(
            "FactQueryIndex definition row has an invalid source range");
      }
      definition_by_name_.emplace(vertex.name, id);
    }
  }

  auto add_vertex_aliases = [&](CodeGraph::VertexId id) {
    if (id < 0 || static_cast<std::size_t>(id) >= records_->vertices.size())
      throw std::out_of_range(
          "FactQueryIndex query resolution vertex is out of range");
    const auto &vertex = records_->vertices[static_cast<std::size_t>(id)];
    if (!vertex.unified_name.has_value() || vertex.unified_name->empty())
      return;
    add_alias(*vertex.unified_name, id);
    add_alias(graph_bare_symbol(*vertex.unified_name), id);
    lsp_aliases_[*vertex.unified_name].push_back(id);
    lsp_aliases_[bare_symbol(*vertex.unified_name)].push_back(id);
  };
  if (records_->query_resolution_order.empty()) {
    for (std::size_t index = 0; index < records_->vertices.size(); ++index)
      add_vertex_aliases(static_cast<CodeGraph::VertexId>(index));
  } else {
    if (records_->query_resolution_order.size() != records_->vertices.size())
      throw std::invalid_argument(
          "FactQueryIndex query resolution order must cover every vertex");
    std::vector<bool> observed(records_->vertices.size(), false);
    for (const auto id : records_->query_resolution_order) {
      if (id < 0 || static_cast<std::size_t>(id) >= observed.size())
        throw std::out_of_range(
            "FactQueryIndex query resolution vertex is out of range");
      const auto index = static_cast<std::size_t>(id);
      if (observed[index])
        throw std::invalid_argument(
            "FactQueryIndex query resolution order contains a duplicate");
      observed[index] = true;
      add_vertex_aliases(id);
    }
  }

  incoming_reference_indexes_.reserve(definition_by_name_.size());
  std::vector<std::vector<std::size_t>> reference_indexes_by_source(
      records_->vertices.size());
  for (std::size_t index = 0; index < records_->edges.size(); ++index) {
    const auto &edge = records_->edges[index];
    if (edge.source < 0 || edge.target < 0 ||
        static_cast<std::size_t>(edge.source) >= records_->vertices.size() ||
        static_cast<std::size_t>(edge.target) >= records_->vertices.size()) {
      throw std::out_of_range("FactQueryIndex edge endpoint is out of range");
    }
    if (edge.type != EDGE_TYPE_REFERENCE)
      continue;
    const bool has_file =
        edge.anchor_file.has_value() && !edge.anchor_file->empty();
    const bool has_line =
        edge.anchor_line.has_value() && *edge.anchor_line >= 0;
    if (has_file != has_line ||
        (require_anchored_references_ && (!has_file || !has_line))) {
      throw std::invalid_argument(
          "FactQueryIndex reference has an invalid source anchor");
    }
    reference_indexes_by_source[static_cast<std::size_t>(edge.source)]
        .push_back(index);
    ++reference_count_;
  }

  // igraph's incoming iterator groups rows by source id and prepends edge ids
  // within one source adjacency list. Build the same order in O(E), without
  // constructing igraph or sorting every target posting independently.
  for (const auto &source_postings : reference_indexes_by_source) {
    for (auto posting = source_postings.rbegin();
         posting != source_postings.rend(); ++posting) {
      const auto target = records_->edges[*posting].target;
      incoming_reference_indexes_[target].push_back(*posting);
    }
  }
}

bool FactQueryIndex::has_symbol(const std::string &name) const {
  return record_by_name_.find(name) != record_by_name_.end();
}

std::optional<CodeGraph::VertexData>
FactQueryIndex::get_node_info_by_name(const std::string &name) const {
  const auto found = record_by_name_.find(name);
  if (found == record_by_name_.end())
    return std::nullopt;
  return get_node_info_by_id(found->second);
}

std::optional<CodeGraph::VertexData>
FactQueryIndex::get_node_info_by_id(CodeGraph::VertexId id) const {
  if (id < 0 || static_cast<std::size_t>(id) >= records_->vertices.size())
    return std::nullopt;
  return records_->vertices[static_cast<std::size_t>(id)];
}

std::string FactQueryIndex::trim_symbol(std::string value) {
  auto is_space = [](unsigned char character) {
    return std::isspace(character) != 0;
  };
  value.erase(value.begin(),
              std::find_if_not(value.begin(), value.end(), is_space));
  value.erase(std::find_if_not(value.rbegin(), value.rend(), is_space).base(),
              value.end());
  auto is_quote = [](char character) {
    return character == '`' || character == '\'' || character == '"';
  };
  while (!value.empty() && is_quote(value.front()))
    value.erase(value.begin());
  while (!value.empty() && is_quote(value.back()))
    value.pop_back();
  return value;
}

std::string FactQueryIndex::bare_symbol(const std::string &value) {
  const auto start = value.find_last_of(":.#");
  std::string result = value.substr(start == std::string::npos ? 0 : start + 1);
  while (!result.empty() &&
         (result.back() == '(' || result.back() == ')' ||
          std::isspace(static_cast<unsigned char>(result.back())))) {
    result.pop_back();
  }
  result.erase(result.begin(),
               std::find_if_not(result.begin(), result.end(),
                                [](unsigned char character) {
                                  return std::isspace(character) != 0;
                                }));
  return result;
}

std::string FactQueryIndex::graph_bare_symbol(const std::string &value) {
  const auto colon = value.find_last_of(':');
  std::string result = value.substr(colon == std::string::npos ? 0 : colon + 1);
  const auto dot = result.find_last_of('.');
  if (dot != std::string::npos)
    result.erase(0, dot + 1);
  while (!result.empty() &&
         (result.back() == '(' || result.back() == ')' ||
          std::isspace(static_cast<unsigned char>(result.back())))) {
    result.pop_back();
  }
  result.erase(result.begin(),
               std::find_if_not(result.begin(), result.end(),
                                [](unsigned char character) {
                                  return std::isspace(character) != 0;
                                }));
  return result;
}

std::string FactQueryIndex::lowercase(std::string value) {
  std::transform(value.begin(), value.end(), value.begin(),
                 [](unsigned char character) {
                   return static_cast<char>(std::tolower(character));
                 });
  return value;
}

void FactQueryIndex::append_candidate(std::vector<CodeGraph::VertexId> &output,
                                      CodeGraph::VertexId id,
                                      std::size_t limit) const {
  if (output.size() >= limit ||
      std::find(output.begin(), output.end(), id) != output.end()) {
    return;
  }
  output.push_back(id);
}

std::vector<std::string>
FactQueryIndex::resolve_symbol_candidates(const std::string &raw_symbol,
                                          std::size_t limit) const {
  if (limit == 0)
    return {};
  const std::string symbol = trim_symbol(raw_symbol);
  if (symbol.empty())
    return {};

  std::vector<CodeGraph::VertexId> candidates;
  const auto exact = record_by_name_.find(symbol);
  if (exact != record_by_name_.end()) {
    candidates.push_back(exact->second);
  } else {
    constexpr std::size_t graph_resolution_limit = 8;
    const std::string graph_bare = graph_bare_symbol(symbol);
    for (const auto &key : {symbol, symbol + "()", graph_bare}) {
      const auto found = aliases_.find(key);
      if (found == aliases_.end())
        continue;
      for (const auto id : found->second)
        append_candidate(candidates, id, graph_resolution_limit);
      if (!candidates.empty())
        break;
    }

    if (candidates.empty() && !graph_bare.empty()) {
      const std::string needle = lowercase(graph_bare);
      for (const auto &alias : alias_order_) {
        if (lowercase(alias).find(needle) == std::string::npos)
          continue;
        for (const auto id : aliases_.at(alias))
          append_candidate(candidates, id, graph_resolution_limit);
        if (candidates.size() >= graph_resolution_limit)
          break;
      }
    }

    if (candidates.empty()) {
      const std::string suffix = "." + symbol;
      std::string base = symbol;
      if (const auto dot = base.find_last_of('.'); dot != std::string::npos)
        base.erase(0, dot + 1);
      if (const auto colon = base.find_last_of(':'); colon != std::string::npos)
        base.erase(0, colon + 1);
      for (std::size_t index = 0; index < records_->vertices.size(); ++index) {
        const auto &name = records_->vertices[index].name;
        std::string leaf = name;
        if (const auto dot = leaf.find_last_of('.'); dot != std::string::npos)
          leaf.erase(0, dot + 1);
        if (has_suffix(name, suffix) || leaf == base)
          append_candidate(candidates, static_cast<CodeGraph::VertexId>(index),
                           graph_resolution_limit);
        if (candidates.size() >= graph_resolution_limit)
          break;
      }
    }

    const bool graph_resolved = !candidates.empty();
    if (candidates.empty()) {
      const std::string bare = bare_symbol(symbol);
      const std::string suffix = "." + symbol;
      for (std::size_t index = 0; index < records_->vertices.size(); ++index) {
        const auto &name = records_->vertices[index].name;
        if (has_suffix(name, suffix) || bare_symbol(name) == bare)
          append_candidate(candidates, static_cast<CodeGraph::VertexId>(index),
                           limit);
        if (candidates.size() >= limit)
          break;
      }
      if (candidates.empty() && !bare.empty()) {
        const std::string needle = lowercase(bare);
        for (std::size_t index = 0; index < records_->vertices.size();
             ++index) {
          if (lowercase(records_->vertices[index].name).find(needle) !=
              std::string::npos) {
            append_candidate(candidates,
                             static_cast<CodeGraph::VertexId>(index), limit);
          }
          if (candidates.size() >= limit)
            break;
        }
      }
    }

    // CodeGraph.resolve_symbol returns display labels for ambiguous matches;
    // lsp_graph maps each display back to every canonical name sharing it
    // before applying the caller's final limit.
    if (graph_resolved && candidates.size() > 1) {
      std::vector<CodeGraph::VertexId> expanded;
      for (const auto id : candidates) {
        const auto &vertex = records_->vertices[static_cast<std::size_t>(id)];
        const std::string &display = vertex.unified_name.has_value()
                                         ? *vertex.unified_name
                                         : vertex.name;
        const auto canonical_display = record_by_name_.find(display);
        if (canonical_display != record_by_name_.end()) {
          append_candidate(expanded, canonical_display->second, limit);
          continue;
        }
        for (const auto &key :
             {display, display + "()", bare_symbol(display)}) {
          const auto found = lsp_aliases_.find(key);
          if (found == lsp_aliases_.end())
            continue;
          for (const auto candidate : found->second)
            append_candidate(expanded, candidate, limit);
        }
        if (expanded.size() >= limit)
          break;
      }
      candidates = std::move(expanded);
    }
  }

  std::vector<std::string> result;
  result.reserve(candidates.size());
  for (const auto id : candidates)
    result.push_back(records_->vertices[static_cast<std::size_t>(id)].name);
  return result;
}

std::vector<FactQueryIndex::Reference>
FactQueryIndex::incoming_references(const std::string &target_name) const {
  const auto target = record_by_name_.find(target_name);
  if (target == record_by_name_.end())
    return {};
  const auto postings = incoming_reference_indexes_.find(target->second);
  if (postings == incoming_reference_indexes_.end())
    return {};

  std::vector<Reference> result;
  result.reserve(postings->second.size());
  for (const auto index : postings->second) {
    const auto &edge = records_->edges[index];
    result.push_back(
        Reference{edge.source, edge.anchor_file, edge.anchor_line});
  }
  return result;
}

} // namespace codenib::core
