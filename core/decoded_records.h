/*
 * SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#pragma once

#include "code_graph.h"

#include <string>
#include <vector>

namespace codenib::core {

// Provider-neutral semantic rows before an igraph instance or Python objects
// are built. Vertex ids in ``edges`` index ``vertices``. Each adapter owns its
// merge, filtering, and deterministic-order policy before publishing rows.
struct DecodedRecords {
  std::vector<CodeGraph::VertexData> vertices;
  std::vector<CodeGraph::EdgeData> edges;
  // Optional permutation used only while building user-facing symbol aliases.
  // Providers whose legacy lookup groups equal display names can preserve that
  // ordering without changing vertex ids or reference adjacency semantics.
  // Empty means natural vertex order.
  std::vector<CodeGraph::VertexId> query_resolution_order;
  std::string project_root;
};

} // namespace codenib::core
