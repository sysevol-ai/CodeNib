// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

#include "fact_query_index.h"

#include <cassert>
#include <iostream>
#include <memory>
#include <optional>
#include <stdexcept>
#include <string>
#include <vector>

using codenib::core::CodeGraph;
using codenib::core::DecodedRecords;
using codenib::core::FactQueryIndex;

namespace {

CodeGraph::VertexData definition(const std::string &name,
                                 const std::string &display, int line) {
  CodeGraph::VertexData vertex;
  vertex.name = name;
  vertex.type = codenib::core::NODE_TYPE_FUNCTION;
  vertex.file = "src/main.py";
  vertex.start_line = line;
  vertex.end_line = line + 2;
  vertex.selection_line = line;
  vertex.unified_name = display;
  vertex.has_definition = true;
  return vertex;
}

CodeGraph::VertexData reference_only(const std::string &name,
                                     const std::string &display) {
  CodeGraph::VertexData vertex;
  vertex.name = name;
  vertex.type = codenib::core::NODE_TYPE_SYMBOL;
  vertex.file = "src/main.py";
  vertex.unified_name = display;
  vertex.has_definition = false;
  return vertex;
}

std::shared_ptr<DecodedRecords> make_records() {
  auto records = std::make_shared<DecodedRecords>();
  records->project_root = "/workspace/project";
  records->vertices = {
      definition("canonical-target", "src/main.py:Widget.target()", 2),
      definition("canonical-caller", "src/main.py:caller()", 8),
      definition("canonical-run-one", "src/one.py:run()", 12),
      definition("canonical-run-two", "src/two.py:run()", 20),
      reference_only("external-target", "dependency.py:external()"),
      definition("canonical-shared-one", "src/shared.py:shared()", 24),
      definition("canonical-shared-two", "src/shared.py:shared()", 28),
  };
  records->edges = {
      {1, 0, codenib::core::EDGE_TYPE_REFERENCE, "src/main.py", 8},
      {1, 0, codenib::core::EDGE_TYPE_REFERENCE, "src/main.py", 10},
      {1, 4, codenib::core::EDGE_TYPE_REFERENCE, "src/main.py", 11},
      {1, 0, codenib::core::EDGE_TYPE_CONTAIN, std::nullopt, std::nullopt},
  };
  return records;
}

void assert_symbol_resolution_and_postings() {
  FactQueryIndex index(make_records());
  assert(index.project_root() == "/workspace/project");
  assert(index.record_count() == 7);
  assert(index.edge_count() == 4);
  assert(index.symbol_count() == 6);
  assert(index.reference_count() == 3);
  assert(index.has_symbol("canonical-target"));
  assert(index.has_symbol("external-target"));
  assert(!index.has_symbol("missing"));

  assert((index.resolve_symbol_candidates("canonical-target") ==
          std::vector<std::string>{"canonical-target"}));
  assert((index.resolve_symbol_candidates("src/main.py:Widget.target()") ==
          std::vector<std::string>{"canonical-target"}));
  assert((index.resolve_symbol_candidates("target") ==
          std::vector<std::string>{"canonical-target"}));
  assert((index.resolve_symbol_candidates("`target`") ==
          std::vector<std::string>{"canonical-target"}));
  assert((index.resolve_symbol_candidates("run") ==
          std::vector<std::string>{"canonical-run-one", "canonical-run-two"}));
  assert((index.resolve_symbol_candidates("shared") ==
          std::vector<std::string>{"canonical-shared-one",
                                   "canonical-shared-two"}));
  assert(index.resolve_symbol_candidates("run", 1).size() == 1);
  assert(index.resolve_symbol_candidates("missing").empty());

  const auto target = index.get_node_info_by_name("canonical-target");
  assert(target.has_value());
  assert(target->selection_line == 2);
  assert(!index.get_node_info_by_name("missing").has_value());
  assert(index.get_node_info_by_id(4)->has_definition == false);
  assert(!index.get_node_info_by_id(99).has_value());

  const auto references = index.incoming_references("canonical-target");
  assert(references.size() == 2);
  assert(references[0].source == 1);
  assert(references[0].anchor_file == "src/main.py");
  assert(references[0].anchor_line == 10);
  assert(references[1].anchor_line == 8);
  assert(index.incoming_references("missing").empty());
}

void assert_invalid_records_fail_closed() {
  try {
    (void)FactQueryIndex(nullptr);
    assert(false);
  } catch (const std::invalid_argument &) {
  }

  auto duplicate = make_records();
  duplicate->vertices.push_back(duplicate->vertices.front());
  try {
    (void)FactQueryIndex(duplicate);
    assert(false);
  } catch (const std::invalid_argument &) {
  }

  auto invalid_endpoint = make_records();
  invalid_endpoint->edges.push_back(
      {99, 0, codenib::core::EDGE_TYPE_REFERENCE, "src/main.py", 1});
  try {
    (void)FactQueryIndex(invalid_endpoint);
    assert(false);
  } catch (const std::out_of_range &) {
  }

  auto unanchored = make_records();
  unanchored->edges.push_back(
      {1, 0, codenib::core::EDGE_TYPE_REFERENCE, std::nullopt, std::nullopt});
  try {
    (void)FactQueryIndex(unanchored);
    assert(false);
  } catch (const std::invalid_argument &) {
  }

  auto invalid_definition = make_records();
  invalid_definition->vertices[0].start_line = 8;
  invalid_definition->vertices[0].end_line = 2;
  try {
    (void)FactQueryIndex(invalid_definition);
    assert(false);
  } catch (const std::invalid_argument &) {
  }
}

} // namespace

int main() {
  assert_symbol_resolution_and_postings();
  assert_invalid_records_fail_closed();
  std::cout << "fact_query_index_test: OK\n";
  return 0;
}
