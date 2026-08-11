/*
 * SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#include "fact_batch_buffer.h"

#include <algorithm>
#include <chrono>
#include <cstring>
#include <limits>
#include <optional>
#include <set>
#include <stdexcept>
#include <string_view>
#include <tuple>
#include <unordered_map>
#include <utility>

namespace codenib::core {
namespace {

struct StringRef {
  std::uint32_t offset{FACT_BATCH_BUFFER_NONE};
  std::uint32_t length{0};
};

struct SourceRange {
  std::uint32_t start_line{FACT_BATCH_BUFFER_NONE};
  std::uint32_t start_character{FACT_BATCH_BUFFER_NONE};
  std::uint32_t end_line{FACT_BATCH_BUFFER_NONE};
  std::uint32_t end_character{FACT_BATCH_BUFFER_NONE};

  auto key() const {
    return std::make_tuple(start_line, start_character, end_line,
                           end_character);
  }
};

class StringArena {
public:
  StringRef add(std::string_view value) {
    auto found = refs_.find(value);
    if (found != refs_.end())
      return found->second;
    if (value.size() > std::numeric_limits<std::uint32_t>::max())
      throw std::overflow_error("FactBatchBuffer string exceeds u32 length");
    if (bytes_.size() >
        std::numeric_limits<std::uint32_t>::max() - value.size())
      throw std::overflow_error(
          "FactBatchBuffer string arena exceeds u32 size");
    StringRef ref{static_cast<std::uint32_t>(bytes_.size()),
                  static_cast<std::uint32_t>(value.size())};
    bytes_.insert(bytes_.end(), value.begin(), value.end());
    refs_.emplace(value, ref);
    return ref;
  }

  StringRef add(const std::string &value) {
    return add(std::string_view(value));
  }

  StringRef add(const std::optional<std::string> &value) {
    return value.has_value() ? add(std::string_view(*value)) : StringRef{};
  }

  StringRef add(const std::optional<std::string_view> &value) {
    return value.has_value() ? add(*value) : StringRef{};
  }

  std::vector<std::uint8_t> take() { return std::move(bytes_); }

private:
  std::vector<std::uint8_t> bytes_;
  std::unordered_map<std::string_view, StringRef> refs_;
};

void append_u8(std::vector<std::uint8_t> &out, std::uint8_t value) {
  out.push_back(value);
}

void append_u16(std::vector<std::uint8_t> &out, std::uint16_t value) {
  out.push_back(static_cast<std::uint8_t>(value & 0xffU));
  out.push_back(static_cast<std::uint8_t>((value >> 8U) & 0xffU));
}

void append_u32(std::vector<std::uint8_t> &out, std::uint32_t value) {
  for (unsigned shift = 0; shift < 32; shift += 8)
    out.push_back(static_cast<std::uint8_t>((value >> shift) & 0xffU));
}

void append_u64(std::vector<std::uint8_t> &out, std::uint64_t value) {
  for (unsigned shift = 0; shift < 64; shift += 8)
    out.push_back(static_cast<std::uint8_t>((value >> shift) & 0xffU));
}

void append_f64(std::vector<std::uint8_t> &out, double value) {
  static_assert(sizeof(double) == sizeof(std::uint64_t));
  std::uint64_t bits = 0;
  std::memcpy(&bits, &value, sizeof(bits));
  append_u64(out, bits);
}

void append_ref(std::vector<std::uint8_t> &out, StringRef ref) {
  append_u32(out, ref.offset);
  append_u32(out, ref.length);
}

void append_range(std::vector<std::uint8_t> &out, const SourceRange &range) {
  append_u32(out, range.start_line);
  append_u32(out, range.start_character);
  append_u32(out, range.end_line);
  append_u32(out, range.end_character);
}

std::uint32_t checked_count(std::size_t value, std::string_view label) {
  if (value > std::numeric_limits<std::uint32_t>::max())
    throw std::overflow_error(std::string(label) + " exceeds u32 count");
  return static_cast<std::uint32_t>(value);
}

std::uint32_t checked_line(int value, std::string_view label) {
  if (value < 0)
    throw std::invalid_argument(std::string(label) + " must be non-negative");
  return static_cast<std::uint32_t>(value);
}

SourceRange line_range(const std::optional<int> &start,
                       const std::optional<int> &end) {
  if (!start.has_value() || !end.has_value())
    return {};
  const auto end_line = checked_line(*end, "range end_line");
  if (end_line == std::numeric_limits<std::uint32_t>::max())
    throw std::overflow_error("range end_line cannot be made half-open");
  return {checked_line(*start, "range start_line"), 0, end_line + 1, 0};
}

SourceRange selection_range(const std::optional<int> &line) {
  if (!line.has_value())
    return {};
  const auto value = checked_line(*line, "selection_line");
  if (value == std::numeric_limits<std::uint32_t>::max())
    throw std::overflow_error("selection_line cannot be made half-open");
  return {value, 0, value + 1, 0};
}

SourceRange anchor_range(const std::optional<int> &line) {
  return selection_range(line);
}

bool has_range(const SourceRange &range) {
  return range.start_line != FACT_BATCH_BUFFER_NONE;
}

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

std::string normalize_path(std::string value) {
  std::replace(value.begin(), value.end(), '\\', '/');
  if (value.rfind("./", 0) == 0)
    value.erase(0, 2);
  if (value.empty() || value.front() == '/')
    throw std::invalid_argument(
        "FactBatchBuffer file paths must be repository-relative POSIX paths");
  std::size_t begin = 0;
  while (begin <= value.size()) {
    const auto end = value.find('/', begin);
    const auto part = value.substr(begin, end - begin);
    if (part.empty() || part == "." || part == "..")
      throw std::invalid_argument(
          "FactBatchBuffer file paths must be canonical POSIX paths");
    if (end == std::string::npos)
      break;
    begin = end + 1;
  }
  return value;
}

bool is_canonical_digest(const std::string &value) {
  if (value.size() != 71 || value.rfind("sha256:", 0) != 0)
    return false;
  return std::all_of(value.begin() + 7, value.end(), [](char ch) {
    return (ch >= '0' && ch <= '9') || (ch >= 'a' && ch <= 'f');
  });
}

void require_text(const std::string &value, std::string_view label) {
  if (value.empty() || value.find('\0') != std::string::npos)
    throw std::invalid_argument(std::string(label) + " must not be empty");
}

struct SymbolRecord {
  std::uint32_t batch_index;
  std::string_view symbol_id;
  std::string_view moniker;
  std::string_view display_name;
  std::string_view kind;
  SourceRange definition;
  SourceRange selection;
  std::optional<std::string_view> enclosing_symbol_id;

  auto key() const {
    return std::make_tuple(batch_index, symbol_id, definition.key());
  }
};

struct OccurrenceRecord {
  std::uint32_t batch_index;
  std::string_view moniker;
  SourceRange source;
  std::uint32_t roles;
  SourceRange enclosing;

  auto key() const {
    return std::make_tuple(batch_index, source.key(), moniker, roles,
                           enclosing.key());
  }
};

struct EdgeRecord {
  std::uint32_t batch_index;
  std::string_view kind;
  std::optional<std::string_view> source_symbol_id;
  std::optional<std::string_view> target_symbol_id;
  std::optional<std::string_view> target_moniker;
  SourceRange anchor;
  std::uint8_t provenance{FACT_PROVENANCE_SCIP};
  double confidence{1.0};
  std::optional<std::string_view> resolver;

  auto key() const {
    return std::make_tuple(batch_index, source_symbol_id, kind,
                           target_symbol_id, target_moniker, anchor.key(),
                           provenance, confidence, resolver);
  }
};

template <typename Record> void sort_unique(std::vector<Record> &records) {
  std::sort(records.begin(), records.end(),
            [](const Record &left, const Record &right) {
              return left.key() < right.key();
            });
  records.erase(std::unique(records.begin(), records.end(),
                            [](const Record &left, const Record &right) {
                              return left.key() == right.key();
                            }),
                records.end());
}

void require_row_size(const std::vector<std::uint8_t> &out, std::size_t before,
                      std::size_t expected, std::string_view label) {
  if (out.size() - before != expected)
    throw std::logic_error(std::string(label) + " encoder row-size mismatch");
}

} // namespace

FactBatchBuffers
encode_fact_batch_buffer(const std::vector<CodeGraph::VertexData> &vertices,
                         const std::vector<CodeGraph::EdgeData> &graph_edges,
                         const std::string &project_root,
                         const FactBatchBufferOptions &options) {
  using clock = std::chrono::steady_clock;
  const auto started = clock::now();
  require_text(options.language, "language");
  require_text(options.provider, "provider");
  require_text(options.position_encoding, "position_encoding");
  if (!is_canonical_digest(options.profile_digest))
    throw std::invalid_argument(
        "profile_digest must use canonical sha256:<64 lowercase hex>");

  std::unordered_map<std::string, std::string> content_digests;
  for (const auto &[raw_path, digest] : options.content_digests) {
    const auto path = normalize_path(raw_path);
    if (!is_canonical_digest(digest))
      throw std::invalid_argument(
          "content digests must use canonical sha256:<64 lowercase hex>");
    if (!content_digests.emplace(path, digest).second)
      throw std::invalid_argument("duplicate normalized content digest path");
  }
  const auto validated = clock::now();

  std::set<std::string> file_paths;
  for (const auto &vertex : vertices) {
    if (vertex.type == NODE_TYPE_FILE)
      file_paths.insert(normalize_path(vertex.name));
    if (vertex.file.has_value())
      file_paths.insert(normalize_path(*vertex.file));
  }
  for (const auto &edge : graph_edges)
    if (edge.anchor_file.has_value())
      file_paths.insert(normalize_path(*edge.anchor_file));

  std::vector<std::string> files(file_paths.begin(), file_paths.end());
  std::unordered_map<std::string, std::uint32_t> batch_by_file;
  for (std::size_t index = 0; index < files.size(); ++index)
    batch_by_file.emplace(files[index], checked_count(index, "batch index"));

  std::unordered_map<std::size_t, std::size_t> parent_symbols;
  for (const auto &edge : graph_edges) {
    const auto source = static_cast<std::size_t>(edge.source);
    const auto target = static_cast<std::size_t>(edge.target);
    if (source >= vertices.size() || target >= vertices.size())
      throw std::out_of_range(
          "CodeGraph edge endpoint is outside vertex table");
    if (edge.type == EDGE_TYPE_CONTAIN &&
        is_symbol_type(vertices[source].type) &&
        is_symbol_type(vertices[target].type))
      parent_symbols[target] = source;
  }
  const auto indexes_collected = clock::now();

  std::vector<SymbolRecord> symbols;
  std::vector<OccurrenceRecord> occurrences;
  for (std::size_t index = 0; index < vertices.size(); ++index) {
    const auto &vertex = vertices[index];
    if (!is_symbol_type(vertex.type) || !has_definition(vertex) ||
        !vertex.file.has_value())
      continue;
    const auto file = normalize_path(*vertex.file);
    const auto batch = batch_by_file.at(file);
    const auto definition = line_range(vertex.start_line, vertex.end_line);
    const auto selection = selection_range(vertex.selection_line);
    const std::string_view moniker =
        vertex.unified_name.has_value() ? std::string_view(*vertex.unified_name)
                                        : std::string_view(vertex.name);
    auto parent = parent_symbols.find(index);
    std::optional<std::string_view> enclosing;
    if (parent != parent_symbols.end())
      enclosing = vertices[parent->second].name;
    symbols.push_back(SymbolRecord{batch, vertex.name, moniker, moniker,
                                   vertex.type, definition, selection,
                                   enclosing});
    occurrences.push_back(
        OccurrenceRecord{batch, moniker, definition, 1U, SourceRange{}});
  }
  sort_unique(symbols);
  sort_unique(occurrences);

  std::vector<EdgeRecord> fact_edges;
  for (const auto &edge : graph_edges) {
    const auto source_index = static_cast<std::size_t>(edge.source);
    const auto target_index = static_cast<std::size_t>(edge.target);
    const auto &source = vertices[source_index];
    const auto &target = vertices[target_index];
    if (!is_symbol_type(target.type))
      continue;

    std::optional<std::string> raw_file;
    if (edge.type == EDGE_TYPE_CONTAIN) {
      raw_file = target.file;
    } else if (edge.anchor_file.has_value()) {
      raw_file = edge.anchor_file;
    } else if (source.file.has_value()) {
      raw_file = source.file;
    } else if (source.type == NODE_TYPE_FILE) {
      raw_file = source.name;
    }
    if (!raw_file.has_value())
      continue;
    const auto file = normalize_path(*raw_file);
    auto batch = batch_by_file.find(file);
    if (batch == batch_by_file.end())
      continue;

    std::optional<std::string_view> source_symbol;
    if (is_symbol_type(source.type))
      source_symbol = source.name;
    fact_edges.push_back(EdgeRecord{
        batch->second,
        edge.type,
        source_symbol,
        target.name,
        std::nullopt,
        anchor_range(edge.anchor_line),
        FACT_PROVENANCE_SCIP,
        1.0,
        std::string_view("legacy_code_graph"),
    });
  }
  sort_unique(fact_edges);
  const auto facts_collected = clock::now();

  FactBatchBuffers result;
  StringArena arena;

  for (const auto &file : files) {
    const auto before = result.batches.size();
    append_ref(result.batches, arena.add(file));
    append_ref(result.batches, arena.add(options.language));
    auto digest = content_digests.find(file);
    append_ref(result.batches, digest == content_digests.end()
                                   ? StringRef{}
                                   : arena.add(digest->second));
    append_ref(result.batches, arena.add(options.profile_digest));
    append_ref(result.batches, arena.add(options.provider));
    append_ref(result.batches, arena.add(options.position_encoding));
    append_u8(result.batches, FACT_COMPLETENESS_PARTIAL);
    append_u8(result.batches, FACT_CAPABILITY_DEFINITIONS |
                                  FACT_CAPABILITY_OCCURRENCES |
                                  FACT_CAPABILITY_EDGES);
    append_u16(result.batches, 0);
    append_u32(result.batches, 0);
    require_row_size(result.batches, before, FACT_BATCH_ROW_SIZE, "batch");
  }

  for (const auto &symbol : symbols) {
    const auto before = result.symbols.size();
    append_u32(result.symbols, symbol.batch_index);
    std::uint32_t flags = 1U;
    if (has_range(symbol.selection))
      flags |= 1U << 1;
    if (symbol.enclosing_symbol_id.has_value())
      flags |= 1U << 2;
    append_u32(result.symbols, flags);
    append_ref(result.symbols, arena.add(symbol.symbol_id));
    append_ref(result.symbols, arena.add(symbol.moniker));
    append_ref(result.symbols, arena.add(symbol.display_name));
    append_ref(result.symbols, arena.add(symbol.kind));
    append_range(result.symbols, symbol.definition);
    append_range(result.symbols, symbol.selection);
    append_ref(result.symbols, arena.add(symbol.enclosing_symbol_id));
    require_row_size(result.symbols, before, FACT_SYMBOL_ROW_SIZE, "symbol");
  }

  for (const auto &occurrence : occurrences) {
    const auto before = result.occurrences.size();
    append_u32(result.occurrences, occurrence.batch_index);
    append_u32(result.occurrences, occurrence.roles);
    append_ref(result.occurrences, arena.add(occurrence.moniker));
    append_range(result.occurrences, occurrence.source);
    append_range(result.occurrences, occurrence.enclosing);
    require_row_size(result.occurrences, before, FACT_OCCURRENCE_ROW_SIZE,
                     "occurrence");
  }

  for (const auto &edge : fact_edges) {
    const auto before = result.edges.size();
    append_u32(result.edges, edge.batch_index);
    append_u8(result.edges, edge.provenance);
    std::uint8_t flags = 0;
    if (edge.source_symbol_id.has_value())
      flags |= 1U << 0;
    if (edge.target_symbol_id.has_value())
      flags |= 1U << 1;
    if (edge.target_moniker.has_value())
      flags |= 1U << 2;
    if (has_range(edge.anchor))
      flags |= 1U << 3;
    if (edge.resolver.has_value())
      flags |= 1U << 4;
    append_u8(result.edges, flags);
    append_u16(result.edges, 0);
    append_f64(result.edges, edge.confidence);
    append_ref(result.edges, arena.add(edge.kind));
    append_ref(result.edges, arena.add(edge.source_symbol_id));
    append_ref(result.edges, arena.add(edge.target_symbol_id));
    append_ref(result.edges, arena.add(edge.target_moniker));
    append_ref(result.edges, arena.add(edge.resolver));
    append_range(result.edges, edge.anchor);
    require_row_size(result.edges, before, FACT_EDGE_ROW_SIZE, "edge");
  }
  const auto semantic_encoded = clock::now();

  if (options.include_graph_compat) {
    for (const auto &vertex : vertices) {
      const auto before = result.graph_vertices.size();
      append_ref(result.graph_vertices, arena.add(vertex.name));
      append_ref(result.graph_vertices, arena.add(vertex.type));
      append_ref(result.graph_vertices, arena.add(vertex.file));
      append_ref(result.graph_vertices, arena.add(vertex.unified_name));
      append_ref(result.graph_vertices, arena.add(vertex.symbol_kind));
      append_u32(result.graph_vertices,
                 vertex.start_line.has_value()
                     ? checked_line(*vertex.start_line, "vertex start_line")
                     : FACT_BATCH_BUFFER_NONE);
      append_u32(result.graph_vertices,
                 vertex.end_line.has_value()
                     ? checked_line(*vertex.end_line, "vertex end_line")
                     : FACT_BATCH_BUFFER_NONE);
      append_u32(
          result.graph_vertices,
          vertex.selection_line.has_value()
              ? checked_line(*vertex.selection_line, "vertex selection_line")
              : FACT_BATCH_BUFFER_NONE);
      std::uint32_t flags = 0;
      if (vertex.has_definition.has_value()) {
        flags |= 1U;
        if (*vertex.has_definition)
          flags |= 1U << 1;
      }
      append_u32(result.graph_vertices, flags);
      require_row_size(result.graph_vertices, before,
                       FACT_GRAPH_VERTEX_ROW_SIZE, "graph vertex");
    }

    for (const auto &edge : graph_edges) {
      const auto before = result.graph_edges.size();
      append_u32(result.graph_edges,
                 checked_count(static_cast<std::size_t>(edge.source),
                               "graph edge source"));
      append_u32(result.graph_edges,
                 checked_count(static_cast<std::size_t>(edge.target),
                               "graph edge target"));
      append_ref(result.graph_edges, arena.add(edge.type));
      append_ref(result.graph_edges, arena.add(edge.anchor_file));
      append_u32(result.graph_edges,
                 edge.anchor_line.has_value()
                     ? checked_line(*edge.anchor_line, "edge anchor_line")
                     : FACT_BATCH_BUFFER_NONE);
      append_u32(result.graph_edges, 0);
      require_row_size(result.graph_edges, before, FACT_GRAPH_EDGE_ROW_SIZE,
                       "graph edge");
    }
  }
  const auto graph_encoded = clock::now();

  const auto project_root_ref = arena.add(project_root);
  result.arena = arena.take();

  std::uint32_t flags =
      options.include_graph_compat ? FACT_BUFFER_FLAG_GRAPH_COMPAT : 0U;
  if (content_digests.size() >= files.size() &&
      std::all_of(files.begin(), files.end(), [&](const std::string &file) {
        return content_digests.find(file) != content_digests.end();
      }))
    flags |= FACT_BUFFER_FLAG_CONTENT_DIGESTS_COMPLETE;

  result.meta.insert(result.meta.end(), {'C', 'N', 'F', 'B'});
  append_u16(result.meta, FACT_BATCH_BUFFER_ABI_VERSION);
  append_u16(result.meta, FACT_BATCH_SCHEMA_VERSION);
  append_u32(result.meta, flags);
  append_u32(result.meta, checked_count(files.size(), "batch count"));
  append_u32(result.meta, checked_count(symbols.size(), "symbol count"));
  append_u32(result.meta,
             checked_count(occurrences.size(), "occurrence count"));
  append_u32(result.meta, checked_count(fact_edges.size(), "fact edge count"));
  append_u32(result.meta, 0); // diagnostics are reserved in v1
  append_u32(result.meta, options.include_graph_compat
                              ? checked_count(vertices.size(), "vertex count")
                              : 0U);
  append_u32(result.meta,
             options.include_graph_compat
                 ? checked_count(graph_edges.size(), "graph edge count")
                 : 0U);
  append_u32(result.meta, checked_count(result.arena.size(), "arena size"));
  append_ref(result.meta, project_root_ref);
  append_u32(result.meta, 0);
  append_u64(result.meta, 0);
  if (result.meta.size() != FACT_BATCH_META_SIZE)
    throw std::logic_error("FactBatchBuffer meta-size mismatch");

  const auto finished = clock::now();
  auto elapsed_ns = [](auto begin, auto end) {
    return static_cast<std::uint64_t>(
        std::chrono::duration_cast<std::chrono::nanoseconds>(end - begin)
            .count());
  };
  result.profile = FactBatchEncodeProfile{
      elapsed_ns(started, validated),
      elapsed_ns(validated, indexes_collected),
      elapsed_ns(indexes_collected, facts_collected),
      elapsed_ns(facts_collected, semantic_encoded),
      elapsed_ns(semantic_encoded, graph_encoded),
      elapsed_ns(graph_encoded, finished),
      elapsed_ns(started, finished),
  };

  return result;
}

FactBatchBuffers
encode_fact_batch_buffer(const CodeGraph &graph,
                         const FactBatchBufferOptions &options) {
  return encode_fact_batch_buffer(graph.vertices(), graph.edges(),
                                  graph.project_root(), options);
}

} // namespace codenib::core
