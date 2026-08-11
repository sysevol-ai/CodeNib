/*
 * SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#include "fact_query_index.h"

#include <algorithm>
#include <cctype>
#include <filesystem>
#include <limits>
#include <stdexcept>
#include <tuple>
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

using Position = std::pair<int, int>;

Position start_position(const DecodedOccurrence &occurrence) {
  return {occurrence.start_line, occurrence.start_character};
}

Position end_position(const DecodedOccurrence &occurrence) {
  return {occurrence.end_line, occurrence.end_character};
}

bool contains(const DecodedOccurrence &occurrence, Position position) {
  return start_position(occurrence) <= position &&
         position < end_position(occurrence);
}

auto span_key(const DecodedOccurrence &occurrence) {
  return std::make_tuple(occurrence.end_line - occurrence.start_line,
                         static_cast<long long>(occurrence.end_character) -
                             static_cast<long long>(occurrence.start_character),
                         occurrence.start_line, occurrence.start_character,
                         occurrence.end_line, occurrence.end_character);
}

FactQueryIndex::Location location_from(const DecodedOccurrence &occurrence) {
  return FactQueryIndex::Location{occurrence.file,
                                  occurrence.start_line,
                                  occurrence.start_character,
                                  occurrence.end_line,
                                  occurrence.end_character,
                                  occurrence.roles,
                                  occurrence.target_symbol,
                                  occurrence.container_symbol};
}

bool location_less(const FactQueryIndex::Location &left,
                   const FactQueryIndex::Location &right) {
  return std::tie(left.file_path, left.start_line, left.start_character,
                  left.end_line, left.end_character, left.target_symbol,
                  left.container_symbol, left.roles) <
         std::tie(right.file_path, right.start_line, right.start_character,
                  right.end_line, right.end_character, right.target_symbol,
                  right.container_symbol, right.roles);
}

bool same_location_identity(const FactQueryIndex::Location &left,
                            const FactQueryIndex::Location &right) {
  return left.file_path == right.file_path &&
         left.start_line == right.start_line &&
         left.start_character == right.start_character &&
         left.end_line == right.end_line &&
         left.end_character == right.end_character &&
         left.target_symbol == right.target_symbol &&
         left.container_symbol == right.container_symbol;
}

void sort_unique_locations(std::vector<FactQueryIndex::Location> &locations,
                           std::size_t limit) {
  std::sort(locations.begin(), locations.end(), location_less);
  std::vector<FactQueryIndex::Location> compact;
  compact.reserve(locations.size());
  for (auto &location : locations) {
    if (!compact.empty() && same_location_identity(compact.back(), location)) {
      compact.back().roles |= location.roles;
      continue;
    }
    compact.push_back(std::move(location));
  }
  locations = std::move(compact);
  if (locations.size() > limit)
    locations.resize(limit);
}

} // namespace

FactQueryIndex::FactQueryIndex(std::shared_ptr<const DecodedRecords> records,
                               bool require_anchored_references,
                               std::string snapshot_id)
    : records_(std::move(records)), snapshot_id_(std::move(snapshot_id)),
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

  occurrence_indexes_by_file_.reserve(records_->occurrences.size() / 8 + 1);
  occurrence_indexes_by_target_.reserve(definition_by_name_.size());
  for (std::size_t index = 0; index < records_->occurrences.size(); ++index) {
    const auto &occurrence = records_->occurrences[index];
    if (occurrence.start_line < 0 || occurrence.start_character < 0 ||
        occurrence.end_line < 0 || occurrence.end_character < 0 ||
        end_position(occurrence) < start_position(occurrence)) {
      throw std::invalid_argument("FactQueryIndex occurrence range is invalid");
    }
    for (const auto symbol :
         {occurrence.target_symbol, occurrence.container_symbol}) {
      if (symbol.has_value() &&
          (*symbol < 0 ||
           static_cast<std::size_t>(*symbol) >= records_->vertices.size())) {
        throw std::out_of_range(
            "FactQueryIndex occurrence symbol is out of range");
      }
    }
    if (!occurrence.file.empty())
      occurrence_indexes_by_file_[occurrence.file].indexes.push_back(index);
    if (occurrence.target_symbol.has_value())
      occurrence_indexes_by_target_[*occurrence.target_symbol].push_back(index);
  }

  for (auto &[file, postings] : occurrence_indexes_by_file_) {
    (void)file;
    std::sort(
        postings.indexes.begin(), postings.indexes.end(),
        [&](std::size_t left, std::size_t right) {
          const auto &a = records_->occurrences[left];
          const auto &b = records_->occurrences[right];
          return std::make_tuple(a.start_line, a.start_character, a.end_line,
                                 a.end_character, a.target_symbol.value_or(-1),
                                 a.roles, left) <
                 std::make_tuple(b.start_line, b.start_character, b.end_line,
                                 b.end_character, b.target_symbol.value_or(-1),
                                 b.roles, right);
        });
    postings.prefix_max_end.reserve(postings.indexes.size());
    Position maximum{-1, -1};
    for (const auto index : postings.indexes) {
      maximum = std::max(maximum, end_position(records_->occurrences[index]));
      postings.prefix_max_end.push_back(maximum);
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

bool FactQueryIndex::source_is_available(const std::string &file_path) const {
  if (file_path.empty() || file_path.find('\\') != std::string::npos)
    return false;
  const std::filesystem::path relative(file_path);
  if (relative.is_absolute() || relative.lexically_normal() != relative)
    return false;
  for (const auto &part : relative) {
    if (part == "." || part == ".." || part.empty())
      return false;
  }
  if (records_->project_root.empty())
    return true;
  std::error_code error;
  const auto root = std::filesystem::weakly_canonical(
      std::filesystem::path(records_->project_root), error);
  if (error)
    return false;
  const auto source = std::filesystem::weakly_canonical(root / relative, error);
  if (error)
    return false;
  auto root_part = root.begin();
  auto source_part = source.begin();
  for (; root_part != root.end(); ++root_part, ++source_part) {
    if (source_part == source.end() || *source_part != *root_part)
      return false;
  }
  return std::filesystem::is_regular_file(source, error) && !error;
}

std::vector<std::size_t>
FactQueryIndex::occurrences_at(const std::string &file_path, int line,
                               int character) const {
  if (line < 0 || character < 0)
    return {};
  const auto found = occurrence_indexes_by_file_.find(file_path);
  if (found == occurrence_indexes_by_file_.end())
    return {};
  const Position position{line, character};
  const auto &postings = found->second;
  auto upper = std::upper_bound(
      postings.indexes.begin(), postings.indexes.end(), position,
      [&](Position requested, std::size_t index) {
        return requested < start_position(records_->occurrences[index]);
      });

  std::vector<std::size_t> matches;
  auto cursor = static_cast<std::size_t>(upper - postings.indexes.begin());
  while (cursor > 0) {
    --cursor;
    if (!(position < postings.prefix_max_end[cursor]))
      break;
    const auto index = postings.indexes[cursor];
    if (contains(records_->occurrences[index], position))
      matches.push_back(index);
  }
  if (matches.empty())
    return matches;

  const auto best = *std::min_element(
      matches.begin(), matches.end(), [&](std::size_t left, std::size_t right) {
        return span_key(records_->occurrences[left]) <
               span_key(records_->occurrences[right]);
      });
  const auto &best_occurrence = records_->occurrences[best];
  matches.erase(std::remove_if(matches.begin(), matches.end(),
                               [&](std::size_t index) {
                                 const auto &occurrence =
                                     records_->occurrences[index];
                                 return start_position(occurrence) !=
                                            start_position(best_occurrence) ||
                                        end_position(occurrence) !=
                                            end_position(best_occurrence);
                               }),
                matches.end());
  std::sort(matches.begin(), matches.end());
  return matches;
}

FactQueryIndex::PositionQueryResult
FactQueryIndex::targets_at(const std::string &file_path, int line,
                           int character,
                           std::vector<CodeGraph::VertexId> &targets) const {
  if (line < 0 || character < 0)
    return {false, "native_position_invalid_request", {}};
  if (!source_is_available(file_path))
    return {false, "native_position_source_unavailable", {}};
  const auto matches = occurrences_at(file_path, line, character);
  if (matches.empty())
    return {false, "native_position_occurrence_not_found", {}};

  const bool has_navigable =
      std::any_of(matches.begin(), matches.end(), [&](std::size_t index) {
        return (records_->occurrences[index].roles &
                (OCCURRENCE_ROLE_DEFINITION | OCCURRENCE_ROLE_REFERENCE)) != 0;
      });
  if (!has_navigable)
    return {false, "native_position_declaration_requires_legacy_graph", {}};
  for (const auto index : matches) {
    const auto &occurrence = records_->occurrences[index];
    const bool navigable =
        (occurrence.roles &
         (OCCURRENCE_ROLE_DEFINITION | OCCURRENCE_ROLE_REFERENCE)) != 0;
    if (!navigable)
      continue;
    if (!occurrence.target_symbol.has_value())
      return {false, "native_position_target_unsupported", {}};
    const auto target = *occurrence.target_symbol;
    const auto &vertex = records_->vertices[static_cast<std::size_t>(target)];
    if (!is_symbol_type(vertex.type) || !has_definition(vertex))
      return {false, "native_position_definition_unavailable", {}};
    if (std::find(targets.begin(), targets.end(), target) == targets.end())
      targets.push_back(target);
  }
  const auto line_targets = reference_targets_on_line(file_path, line);
  if (std::any_of(line_targets.begin(), line_targets.end(),
                  [&](CodeGraph::VertexId target) {
                    return std::find(targets.begin(), targets.end(), target) ==
                           targets.end();
                  })) {
    return {false, "native_position_line_ambiguity_requires_legacy_graph", {}};
  }
  return {true, {}, {}, targets};
}

std::optional<FactQueryIndex::Location>
FactQueryIndex::primary_definition(CodeGraph::VertexId target) const {
  const auto found = occurrence_indexes_by_target_.find(target);
  if (found == occurrence_indexes_by_target_.end())
    return std::nullopt;
  for (const auto index : found->second) {
    const auto &occurrence = records_->occurrences[index];
    if ((occurrence.roles & OCCURRENCE_ROLE_PRIMARY_DEFINITION) != 0)
      return location_from(occurrence);
  }
  return std::nullopt;
}

std::vector<CodeGraph::VertexId>
FactQueryIndex::reference_targets_on_line(const std::string &file_path,
                                          int line) const {
  const auto found = occurrence_indexes_by_file_.find(file_path);
  if (found == occurrence_indexes_by_file_.end())
    return {};
  std::vector<CodeGraph::VertexId> targets;
  for (const auto index : found->second.indexes) {
    const auto &occurrence = records_->occurrences[index];
    if (occurrence.start_line < line)
      continue;
    if (occurrence.start_line > line)
      break;
    if ((occurrence.roles & OCCURRENCE_ROLE_REFERENCE) == 0 ||
        !occurrence.target_symbol.has_value() ||
        !occurrence.container_symbol.has_value()) {
      continue;
    }
    const auto target = *occurrence.target_symbol;
    if (std::find(targets.begin(), targets.end(), target) == targets.end())
      targets.push_back(target);
  }
  return targets;
}

FactQueryIndex::PositionQueryResult
FactQueryIndex::definitions_at(const std::string &file_path, int line,
                               int character, std::size_t limit) const {
  std::vector<CodeGraph::VertexId> targets;
  auto decision = targets_at(file_path, line, character, targets);
  if (!decision.served)
    return decision;
  for (const auto target : targets) {
    const auto definition = primary_definition(target);
    if (!definition.has_value())
      return {false, "native_position_definition_unavailable", {}};
    decision.locations.push_back(*definition);
  }
  sort_unique_locations(decision.locations, std::max<std::size_t>(1, limit));
  return decision;
}

FactQueryIndex::PositionQueryResult
FactQueryIndex::references_at(const std::string &file_path, int line,
                              int character, bool include_declaration,
                              std::size_t limit) const {
  std::vector<CodeGraph::VertexId> targets;
  auto decision = targets_at(file_path, line, character, targets);
  if (!decision.served)
    return decision;
  std::vector<CodeGraph::VertexId> resolved_targets;
  for (const auto target : targets) {
    const auto &vertex = records_->vertices[static_cast<std::size_t>(target)];
    const auto &display =
        vertex.unified_name.has_value() ? *vertex.unified_name : vertex.name;
    const auto candidates = resolve_symbol_candidates(display);
    if (candidates.size() == 1 && candidates.front() == vertex.name)
      resolved_targets.push_back(target);
  }
  for (const auto target : resolved_targets) {
    const auto incoming = incoming_reference_indexes_.find(target);
    if (incoming != incoming_reference_indexes_.end() &&
        std::any_of(incoming->second.begin(), incoming->second.end(),
                    [&](std::size_t index) {
                      const auto &edge = records_->edges[index];
                      return !edge.anchor_file.has_value() ||
                             !edge.anchor_line.has_value();
                    })) {
      return {false,
              "native_position_unanchored_reference_requires_legacy_graph",
              {}};
    }
    if (include_declaration) {
      const auto definition = primary_definition(target);
      if (!definition.has_value())
        return {false, "native_position_definition_unavailable", {}};
      decision.locations.push_back(*definition);
    }
    const auto found = occurrence_indexes_by_target_.find(target);
    if (found == occurrence_indexes_by_target_.end())
      continue;
    for (const auto index : found->second) {
      const auto &occurrence = records_->occurrences[index];
      if ((occurrence.roles & OCCURRENCE_ROLE_REFERENCE) == 0 ||
          !occurrence.container_symbol.has_value()) {
        continue;
      }
      decision.locations.push_back(location_from(occurrence));
    }
  }
  sort_unique_locations(decision.locations, std::max<std::size_t>(1, limit));
  return decision;
}

bool FactQueryIndex::has_occurrence_at(const std::string &file_path, int line,
                                       int character) const {
  std::vector<CodeGraph::VertexId> targets;
  return targets_at(file_path, line, character, targets).served;
}

std::vector<FactQueryIndex::Location>
FactQueryIndex::position_samples(std::size_t limit) const {
  if (limit == 0)
    return {};
  std::vector<Location> references;
  std::vector<Location> definitions;
  for (const auto &occurrence : records_->occurrences) {
    if (!occurrence.target_symbol.has_value() ||
        !source_is_available(occurrence.file) ||
        !primary_definition(*occurrence.target_symbol).has_value()) {
      continue;
    }
    if ((occurrence.roles & OCCURRENCE_ROLE_REFERENCE) != 0 &&
        occurrence.container_symbol.has_value()) {
      references.push_back(location_from(occurrence));
    } else if ((occurrence.roles & OCCURRENCE_ROLE_PRIMARY_DEFINITION) != 0) {
      definitions.push_back(location_from(occurrence));
    }
  }
  sort_unique_locations(references, std::numeric_limits<std::size_t>::max());
  sort_unique_locations(definitions, std::numeric_limits<std::size_t>::max());
  std::vector<Location> result;
  result.reserve(std::min(limit, references.size() + definitions.size()));
  for (const auto *candidates : {&references, &definitions}) {
    for (const auto &location : *candidates) {
      if (!definitions_at(location.file_path, location.start_line,
                          location.start_character, 1)
               .served ||
          !references_at(location.file_path, location.start_line,
                         location.start_character, true, 1)
               .served ||
          std::find(result.begin(), result.end(), location) != result.end()) {
        continue;
      }
      result.push_back(location);
      if (result.size() >= limit)
        return result;
    }
  }
  return result;
}

} // namespace codenib::core
