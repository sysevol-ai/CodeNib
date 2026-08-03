// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

#include "graph_layers.h"

#include "code_graph.h"

namespace codenib::core {
namespace {

LayerBuckets make_empty_buckets(std::size_t edge_count) {
  LayerBuckets buckets;
  buckets[GRAPH_LAYER_ALL].reserve(edge_count);
  buckets[GRAPH_LAYER_CONTAINMENT];
  buckets[GRAPH_LAYER_DEPENDENCY];
  buckets[GRAPH_LAYER_REFERENCE];
  buckets[GRAPH_LAYER_IMPORT];
  buckets[GRAPH_LAYER_TYPE_USE];
  return buckets;
}

} // namespace

LayerBuckets classify_edge_layers(const std::vector<std::string> &edge_types) {
  LayerBuckets buckets = make_empty_buckets(edge_types.size());

  auto &all = buckets[GRAPH_LAYER_ALL];
  auto &containment = buckets[GRAPH_LAYER_CONTAINMENT];
  auto &dependency = buckets[GRAPH_LAYER_DEPENDENCY];
  auto &reference = buckets[GRAPH_LAYER_REFERENCE];
  auto &import_layer = buckets[GRAPH_LAYER_IMPORT];
  auto &type_use = buckets[GRAPH_LAYER_TYPE_USE];

  for (EdgeId edge_id = 0; edge_id < edge_types.size(); ++edge_id) {
    const std::string &type = edge_types[edge_id];
    all.push_back(edge_id);

    if (type == EDGE_TYPE_CONTAIN) {
      containment.push_back(edge_id);
      continue;
    }

    if (type == EDGE_TYPE_REFERENCE) {
      reference.push_back(edge_id);
      dependency.push_back(edge_id);
      continue;
    }

    if (type == EDGE_TYPE_IMPORT) {
      import_layer.push_back(edge_id);
      dependency.push_back(edge_id);
      continue;
    }

    if (type == EDGE_TYPE_TYPE_USE) {
      type_use.push_back(edge_id);
      dependency.push_back(edge_id);
    }
  }

  return buckets;
}

} // namespace codenib::core
