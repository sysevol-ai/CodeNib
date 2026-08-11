// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

#include "fact_batch_buffer.h"

#include <cassert>
#include <cstdint>
#include <optional>
#include <string>
#include <tuple>
#include <vector>

using codenib::core::CodeGraph;
using codenib::core::FactBatchBufferOptions;
using codenib::core::FactBatchBuffers;

namespace {

std::uint16_t read_u16(const std::vector<std::uint8_t> &buffer,
                       std::size_t offset) {
  return static_cast<std::uint16_t>(buffer.at(offset)) |
         static_cast<std::uint16_t>(buffer.at(offset + 1)) << 8U;
}

std::uint32_t read_u32(const std::vector<std::uint8_t> &buffer,
                       std::size_t offset) {
  std::uint32_t value = 0;
  for (unsigned shift = 0; shift < 32; shift += 8)
    value |= static_cast<std::uint32_t>(buffer.at(offset + shift / 8U))
             << shift;
  return value;
}

CodeGraph make_graph() {
  CodeGraph graph("/workspace/project");
  graph.batch_upsert_nodes({
      CodeGraph::VertexData{".", "root", std::nullopt, std::nullopt,
                            std::nullopt, std::nullopt, std::nullopt,
                            std::nullopt, std::nullopt},
      CodeGraph::VertexData{
          "src", codenib::core::NODE_TYPE_DIRECTORY, std::nullopt, std::nullopt,
          std::nullopt, std::nullopt, std::nullopt, std::nullopt, std::nullopt},
      CodeGraph::VertexData{"src/main.py", codenib::core::NODE_TYPE_FILE,
                            std::string("src/main.py"), std::nullopt,
                            std::nullopt, std::nullopt, std::nullopt,
                            std::nullopt, std::nullopt},
      CodeGraph::VertexData{
          "src/main.py:run()", codenib::core::NODE_TYPE_FUNCTION,
          std::string("src/main.py"), 0, 2, 0, std::string("src/main.py:run"),
          std::string("function"), true},
      CodeGraph::VertexData{
          "python-stdlib:print", codenib::core::NODE_TYPE_FUNCTION,
          std::nullopt, std::nullopt, std::nullopt, std::nullopt,
          std::string("python-stdlib:print"), std::string("function"), false},
  });
  graph.batch_add_edges({
      {".", "src", codenib::core::EDGE_TYPE_CONTAIN, std::nullopt,
       std::nullopt},
      {"src", "src/main.py", codenib::core::EDGE_TYPE_CONTAIN, std::nullopt,
       std::nullopt},
      {"src/main.py", "src/main.py:run()", codenib::core::EDGE_TYPE_CONTAIN,
       std::nullopt, std::nullopt},
      {"src/main.py:run()", "python-stdlib:print",
       codenib::core::EDGE_TYPE_REFERENCE, std::string("src/main.py"), 1},
  });
  return graph;
}

void assert_same(const FactBatchBuffers &left, const FactBatchBuffers &right) {
  assert(left.meta == right.meta);
  assert(left.batches == right.batches);
  assert(left.symbols == right.symbols);
  assert(left.occurrences == right.occurrences);
  assert(left.edges == right.edges);
  assert(left.diagnostics == right.diagnostics);
  assert(left.graph_vertices == right.graph_vertices);
  assert(left.graph_edges == right.graph_edges);
  assert(left.arena == right.arena);
}

} // namespace

int main() {
  auto graph = make_graph();
  FactBatchBufferOptions options;
  options.language = "python";
  options.profile_digest = "sha256:" + std::string(64, 'a');
  options.content_digests.emplace("src/main.py",
                                  "sha256:" + std::string(64, 'b'));

  const auto encoded = codenib::core::encode_fact_batch_buffer(graph, options);
  const auto repeated = codenib::core::encode_fact_batch_buffer(graph, options);
  assert_same(encoded, repeated);

  assert(encoded.meta.size() == codenib::core::FACT_BATCH_META_SIZE);
  assert(encoded.meta[0] == 'C' && encoded.meta[1] == 'N' &&
         encoded.meta[2] == 'F' && encoded.meta[3] == 'B');
  assert(read_u16(encoded.meta, 4) ==
         codenib::core::FACT_BATCH_BUFFER_ABI_VERSION);
  assert(read_u16(encoded.meta, 6) == codenib::core::FACT_BATCH_SCHEMA_VERSION);
  assert(read_u32(encoded.meta, 12) == 1); // batches
  assert(read_u32(encoded.meta, 16) == 1); // definition symbols
  assert(read_u32(encoded.meta, 20) == 1); // definition occurrences
  assert(read_u32(encoded.meta, 24) == 2); // contain + reference facts
  assert(read_u32(encoded.meta, 28) == 0); // diagnostics
  assert(read_u32(encoded.meta, 32) == 5); // compatibility vertices
  assert(read_u32(encoded.meta, 36) == 4); // compatibility edges
  assert(read_u32(encoded.meta, 40) == encoded.arena.size());

  assert(encoded.batches.size() == codenib::core::FACT_BATCH_ROW_SIZE);
  assert(encoded.symbols.size() == codenib::core::FACT_SYMBOL_ROW_SIZE);
  assert(encoded.occurrences.size() == codenib::core::FACT_OCCURRENCE_ROW_SIZE);
  assert(encoded.edges.size() == 2 * codenib::core::FACT_EDGE_ROW_SIZE);
  assert(encoded.diagnostics.empty());
  assert(encoded.graph_vertices.size() ==
         5 * codenib::core::FACT_GRAPH_VERTEX_ROW_SIZE);
  assert(encoded.graph_edges.size() ==
         4 * codenib::core::FACT_GRAPH_EDGE_ROW_SIZE);

  const auto flags = read_u32(encoded.meta, 8);
  assert((flags & codenib::core::FACT_BUFFER_FLAG_GRAPH_COMPAT) != 0);
  assert((flags & codenib::core::FACT_BUFFER_FLAG_CONTENT_DIGESTS_COMPLETE) !=
         0);

  FactBatchBufferOptions missing_digest = options;
  missing_digest.content_digests.clear();
  const auto partial =
      codenib::core::encode_fact_batch_buffer(graph, missing_digest);
  assert((read_u32(partial.meta, 8) &
          codenib::core::FACT_BUFFER_FLAG_CONTENT_DIGESTS_COMPLETE) == 0);

  return 0;
}
