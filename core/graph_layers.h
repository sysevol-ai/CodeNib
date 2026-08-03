// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

#pragma once

#include <cstddef>
#include <string>
#include <unordered_map>
#include <vector>

namespace codenib::core {

using EdgeId = std::size_t;
using LayerEdgeIds = std::vector<EdgeId>;
using LayerBuckets = std::unordered_map<std::string, LayerEdgeIds>;

// Classify edge ids into CodeGraph's default overlapping layer buckets.
//
// This is deliberately language-agnostic: SCIP, clangd, and generic-LSP
// decoders all normalize into the same edge-type strings before this point.
LayerBuckets classify_edge_layers(const std::vector<std::string> &edge_types);

} // namespace codenib::core
