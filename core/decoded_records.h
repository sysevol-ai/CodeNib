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
  std::string project_root;
};

} // namespace codenib::core
