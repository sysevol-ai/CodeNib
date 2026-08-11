/*
 * SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#pragma once

#include "code_graph.h"

#include <cstddef>
#include <cstdint>
#include <string>
#include <unordered_map>
#include <vector>

namespace codenib::core {

// FactBatchBuffer is the stable native/Python wire contract. All integer
// fields are little-endian. Strings are (offset, length) pairs into ``arena``;
// FACT_BATCH_BUFFER_NONE marks an absent string/range component.
constexpr std::uint16_t FACT_BATCH_BUFFER_ABI_VERSION = 1;
constexpr std::uint16_t FACT_BATCH_SCHEMA_VERSION = 1;
constexpr std::uint32_t FACT_BATCH_BUFFER_NONE = 0xffffffffU;

constexpr std::size_t FACT_BATCH_META_SIZE = 64;
constexpr std::size_t FACT_BATCH_ROW_SIZE = 56;
constexpr std::size_t FACT_SYMBOL_ROW_SIZE = 80;
constexpr std::size_t FACT_OCCURRENCE_ROW_SIZE = 48;
constexpr std::size_t FACT_EDGE_ROW_SIZE = 72;
constexpr std::size_t FACT_DIAGNOSTIC_ROW_SIZE = 40;
constexpr std::size_t FACT_GRAPH_VERTEX_ROW_SIZE = 56;
constexpr std::size_t FACT_GRAPH_EDGE_ROW_SIZE = 32;

constexpr std::uint32_t FACT_BUFFER_FLAG_GRAPH_COMPAT = 1U << 0;
constexpr std::uint32_t FACT_BUFFER_FLAG_CONTENT_DIGESTS_COMPLETE = 1U << 1;

constexpr std::uint8_t FACT_COMPLETENESS_COMPLETE = 1;
constexpr std::uint8_t FACT_COMPLETENESS_PARTIAL = 2;
constexpr std::uint8_t FACT_COMPLETENESS_SYNTACTIC = 3;
constexpr std::uint8_t FACT_COMPLETENESS_FAILED = 4;

constexpr std::uint8_t FACT_CAPABILITY_DEFINITIONS = 1U << 0;
constexpr std::uint8_t FACT_CAPABILITY_OCCURRENCES = 1U << 1;
constexpr std::uint8_t FACT_CAPABILITY_EDGES = 1U << 2;
constexpr std::uint8_t FACT_CAPABILITY_DIAGNOSTICS = 1U << 3;

constexpr std::uint8_t FACT_PROVENANCE_SCIP = 1;
constexpr std::uint8_t FACT_PROVENANCE_LSP = 2;
constexpr std::uint8_t FACT_PROVENANCE_CLANGD = 3;
constexpr std::uint8_t FACT_PROVENANCE_SYNTACTIC = 4;
constexpr std::uint8_t FACT_PROVENANCE_FRAMEWORK = 5;
constexpr std::uint8_t FACT_PROVENANCE_HEURISTIC = 6;

struct FactBatchBufferOptions {
  std::string language;
  std::string provider{"scip-core"};
  std::string profile_digest;
  std::string position_encoding{"UTF8"};
  std::unordered_map<std::string, std::string> content_digests;
  bool include_graph_compat{true};
};

struct FactBatchEncodeProfile {
  std::uint64_t validate_options_ns{0};
  std::uint64_t collect_indexes_ns{0};
  std::uint64_t collect_facts_ns{0};
  std::uint64_t encode_semantic_ns{0};
  std::uint64_t encode_graph_ns{0};
  std::uint64_t finalize_ns{0};
  std::uint64_t total_ns{0};
};

struct FactBatchBuffers {
  std::vector<std::uint8_t> meta;
  std::vector<std::uint8_t> batches;
  std::vector<std::uint8_t> symbols;
  std::vector<std::uint8_t> occurrences;
  std::vector<std::uint8_t> edges;
  std::vector<std::uint8_t> diagnostics;

  // Exact compatibility projection of the existing CodeGraph. Python uses
  // these two tables for graph materialization without an intermediate
  // list[dict] / list[tuple] transport. They do not alter persisted schema.
  std::vector<std::uint8_t> graph_vertices;
  std::vector<std::uint8_t> graph_edges;

  std::vector<std::uint8_t> arena;
  FactBatchEncodeProfile profile;
};

FactBatchBuffers
encode_fact_batch_buffer(const CodeGraph &graph,
                         const FactBatchBufferOptions &options);

FactBatchBuffers
encode_fact_batch_buffer(const std::vector<CodeGraph::VertexData> &vertices,
                         const std::vector<CodeGraph::EdgeData> &edges,
                         const std::string &project_root,
                         const FactBatchBufferOptions &options);

} // namespace codenib::core
