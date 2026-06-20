// SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
//
// SPDX-License-Identifier: Apache-2.0

#include "graph_layers.h"

#include "code_graph.h"

#include <cassert>
#include <string>
#include <vector>

using codeminer::core::EDGE_TYPE_CONTAIN;
using codeminer::core::EDGE_TYPE_IMPORT;
using codeminer::core::EDGE_TYPE_REFERENCE;
using codeminer::core::EDGE_TYPE_TYPE_USE;
using codeminer::core::GRAPH_LAYER_ALL;
using codeminer::core::GRAPH_LAYER_CONTAINMENT;
using codeminer::core::GRAPH_LAYER_DEPENDENCY;
using codeminer::core::GRAPH_LAYER_IMPORT;
using codeminer::core::GRAPH_LAYER_REFERENCE;
using codeminer::core::GRAPH_LAYER_TYPE_USE;
using codeminer::core::LayerEdgeIds;

namespace {

void assert_layer(const codeminer::core::LayerBuckets &buckets,
                  const std::string &layer, const LayerEdgeIds &expected) {
  const auto it = buckets.find(layer);
  assert(it != buckets.end());
  assert(it->second == expected);
}

} // namespace

int main() {
  const std::vector<std::string> edge_types = {
      EDGE_TYPE_CONTAIN, EDGE_TYPE_REFERENCE,
      EDGE_TYPE_IMPORT,  EDGE_TYPE_TYPE_USE,
      "custom",          EDGE_TYPE_REFERENCE,
  };

  const auto buckets = codeminer::core::classify_edge_layers(edge_types);

  assert_layer(buckets, GRAPH_LAYER_ALL, {0, 1, 2, 3, 4, 5});
  assert_layer(buckets, GRAPH_LAYER_CONTAINMENT, {0});
  assert_layer(buckets, GRAPH_LAYER_REFERENCE, {1, 5});
  assert_layer(buckets, GRAPH_LAYER_IMPORT, {2});
  assert_layer(buckets, GRAPH_LAYER_TYPE_USE, {3});
  assert_layer(buckets, GRAPH_LAYER_DEPENDENCY, {1, 2, 3, 5});

  const auto empty = codeminer::core::classify_edge_layers({});
  assert_layer(empty, GRAPH_LAYER_ALL, {});
  assert_layer(empty, GRAPH_LAYER_CONTAINMENT, {});
  assert_layer(empty, GRAPH_LAYER_DEPENDENCY, {});
  assert_layer(empty, GRAPH_LAYER_REFERENCE, {});
  assert_layer(empty, GRAPH_LAYER_IMPORT, {});
  assert_layer(empty, GRAPH_LAYER_TYPE_USE, {});

  return 0;
}
