/*
 * SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#pragma once

#include "code_graph.h"

#include <cstdint>
#include <optional>
#include <string>
#include <vector>

namespace codenib::core {

// Provider-neutral occurrence roles. Adapters translate provider-specific
// flags at the decode boundary so query consumers never need to understand
// clangd RefKind, SCIP SymbolRole, or a live LSP wire format.
inline constexpr std::uint32_t OCCURRENCE_ROLE_DECLARATION = 1U << 0;
inline constexpr std::uint32_t OCCURRENCE_ROLE_DEFINITION = 1U << 1;
inline constexpr std::uint32_t OCCURRENCE_ROLE_REFERENCE = 1U << 2;
inline constexpr std::uint32_t OCCURRENCE_ROLE_SPELLED = 1U << 3;
inline constexpr std::uint32_t OCCURRENCE_ROLE_PRIMARY_DEFINITION = 1U << 4;

// Compact semantic occurrence row. Coordinates are zero-based and half-open
// in ``DecodedRecords::position_encoding``. Integer symbol ids avoid repeated
// strings and keep all postings on the native side of the Python boundary.
struct DecodedOccurrence {
  std::string file;
  int start_line{0};
  int start_character{0};
  int end_line{0};
  int end_character{0};
  std::uint32_t roles{0};
  std::optional<CodeGraph::VertexId> target_symbol;
  std::optional<CodeGraph::VertexId> container_symbol;
};

// Provider-neutral semantic rows before an igraph instance or Python objects
// are built. Vertex ids in ``edges`` index ``vertices``. Each adapter owns its
// merge, filtering, and deterministic-order policy before publishing rows.
struct DecodedRecords {
  std::vector<CodeGraph::VertexData> vertices;
  std::vector<CodeGraph::EdgeData> edges;
  std::vector<DecodedOccurrence> occurrences;
  // Optional permutation used only while building user-facing symbol aliases.
  // Providers whose legacy lookup groups equal display names can preserve that
  // ordering without changing vertex ids or reference adjacency semantics.
  // Empty means natural vertex order.
  std::vector<CodeGraph::VertexId> query_resolution_order;
  // Canonical traversal order for graph-free route queries. This remains
  // separate from query resolution because providers may group display-name
  // aliases differently from the legacy graph's vertex insertion order.
  std::vector<CodeGraph::VertexId> route_vertex_order;
  // True only when ``edges`` contains every route-visible structural,
  // reference, and relation edge and route_vertex_order covers each vertex.
  bool route_adjacency_complete{false};
  std::string project_root;
  std::string position_encoding{"UTF8"};
};

} // namespace codenib::core
