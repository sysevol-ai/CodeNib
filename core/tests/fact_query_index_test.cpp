// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

#include "fact_query_index.h"

#include <algorithm>
#include <cassert>
#include <iostream>
#include <memory>
#include <optional>
#include <stdexcept>
#include <string>
#include <vector>

using codenib::core::CodeGraph;
using codenib::core::DecodedOccurrence;
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

DecodedOccurrence
occurrence(int line, int start_character, int end_character,
           std::uint32_t roles, std::optional<CodeGraph::VertexId> target,
           std::optional<CodeGraph::VertexId> container = std::nullopt) {
  return DecodedOccurrence{"src/main.py", line,  start_character, line,
                           end_character, roles, target,          container};
}

std::shared_ptr<DecodedRecords> make_occurrence_records() {
  auto records = make_records();
  // An empty root makes this a provider-neutral range test without depending
  // on a checkout path. Source availability is covered separately below.
  records->project_root.clear();
  records->position_encoding = "UTF16";
  records->occurrences = {
      occurrence(2, 4, 10,
                 codenib::core::OCCURRENCE_ROLE_DEFINITION |
                     codenib::core::OCCURRENCE_ROLE_PRIMARY_DEFINITION,
                 0),
      occurrence(5, 10, 16, codenib::core::OCCURRENCE_ROLE_REFERENCE, 0, 1),
      occurrence(5, 10, 16,
                 codenib::core::OCCURRENCE_ROLE_REFERENCE |
                     codenib::core::OCCURRENCE_ROLE_SPELLED,
                 0, 1),
      occurrence(1, 4, 10, codenib::core::OCCURRENCE_ROLE_DECLARATION, 0),
      occurrence(7, 0, 5, codenib::core::OCCURRENCE_ROLE_REFERENCE,
                 std::nullopt, 1),
      occurrence(12, 4, 8,
                 codenib::core::OCCURRENCE_ROLE_DEFINITION |
                     codenib::core::OCCURRENCE_ROLE_PRIMARY_DEFINITION,
                 2),
      occurrence(9, 0, 20, codenib::core::OCCURRENCE_ROLE_DEFINITION, 1, 1),
      occurrence(9, 3, 8, codenib::core::OCCURRENCE_ROLE_REFERENCE, 0, 1),
      occurrence(9, 3, 8, codenib::core::OCCURRENCE_ROLE_REFERENCE, 2, 1),
      occurrence(10, 1, 4, codenib::core::OCCURRENCE_ROLE_REFERENCE, 0, 1),
      occurrence(10, 8, 12, codenib::core::OCCURRENCE_ROLE_REFERENCE, 2, 1),
      occurrence(11, 4, 19, codenib::core::OCCURRENCE_ROLE_REFERENCE, 4, 1),
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

  auto custom_order = make_records();
  custom_order->query_resolution_order = {0, 1, 3, 2, 4, 5, 6};
  FactQueryIndex reordered(custom_order);
  assert((reordered.resolve_symbol_candidates("run") ==
          std::vector<std::string>{"canonical-run-two", "canonical-run-one"}));
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

  auto relation = make_records();
  relation->edges.push_back(
      {1, 0, codenib::core::EDGE_TYPE_REFERENCE, std::nullopt, std::nullopt});
  FactQueryIndex permissive(relation, false);
  assert(!permissive.requires_anchored_references());
  const auto references = permissive.incoming_references("canonical-target");
  assert(references.size() == 3);
  assert(!references.front().anchor_file.has_value());
  assert(!references.front().anchor_line.has_value());

  auto partial_anchor = make_records();
  partial_anchor->edges.push_back(
      {1, 0, codenib::core::EDGE_TYPE_REFERENCE, "src/main.py", std::nullopt});
  try {
    (void)FactQueryIndex(partial_anchor, false);
    assert(false);
  } catch (const std::invalid_argument &) {
  }

  auto duplicate_resolution = make_records();
  duplicate_resolution->query_resolution_order = {0, 1, 2, 3, 4, 5, 5};
  try {
    (void)FactQueryIndex(duplicate_resolution);
    assert(false);
  } catch (const std::invalid_argument &) {
  }
}

void assert_exact_occurrence_ranges_and_fallbacks() {
  FactQueryIndex index(make_occurrence_records(), true, "snapshot:test");
  assert(index.snapshot_id() == "snapshot:test");
  assert(index.position_encoding() == "UTF16");
  assert(index.occurrence_count() == 12);
  assert(index.supports_position_queries());

  const auto at_start = index.definitions_at("src/main.py", 5, 10, 8);
  assert(at_start.served);
  assert(at_start.locations.size() == 1);
  assert(at_start.locations[0].start_line == 2);
  assert(index.definitions_at("src/main.py", 5, 15, 8).served);
  const auto at_end = index.definitions_at("src/main.py", 5, 16, 8);
  assert(!at_end.served);
  assert(at_end.fallback_reason == "native_position_occurrence_not_found");

  const auto with_definition =
      index.references_at("src/main.py", 5, 10, true, 40);
  assert(with_definition.served);
  assert(with_definition.locations.size() == 4);
  const auto merged_duplicate = std::find_if(
      with_definition.locations.begin(), with_definition.locations.end(),
      [](const FactQueryIndex::Location &location) {
        return location.start_line == 5 && location.start_character == 10;
      });
  assert(merged_duplicate != with_definition.locations.end());
  assert((merged_duplicate->roles & codenib::core::OCCURRENCE_ROLE_SPELLED) !=
         0);
  const auto without_definition =
      index.references_at("src/main.py", 5, 10, false, 40);
  assert(without_definition.served);
  assert(without_definition.locations.size() == 3);

  const auto overload = index.definitions_at("src/main.py", 9, 4, 8);
  assert(overload.served);
  assert(overload.locations.size() == 2);
  const auto ambiguous = index.definitions_at("src/main.py", 10, 1, 8);
  assert(!ambiguous.served);
  assert(ambiguous.fallback_reason ==
         "native_position_line_ambiguity_requires_legacy_graph");

  const auto declaration = index.definitions_at("src/main.py", 1, 4, 8);
  assert(!declaration.served);
  assert(declaration.fallback_reason ==
         "native_position_declaration_requires_legacy_graph");
  const auto unsupported = index.definitions_at("src/main.py", 7, 1, 8);
  assert(!unsupported.served);
  assert(unsupported.fallback_reason == "native_position_target_unsupported");
  const auto unavailable = index.definitions_at("../src/main.py", 5, 10, 8);
  assert(!unavailable.served);
  assert(unavailable.fallback_reason == "native_position_source_unavailable");

  const auto samples = index.position_samples(100);
  assert(!samples.empty());
  for (const auto &sample : samples)
    assert(sample.start_line != 1 && sample.start_line != 7);
}

void assert_invalid_occurrences_fail_closed() {
  auto invalid_range = make_occurrence_records();
  invalid_range->occurrences.push_back(
      occurrence(13, 8, 4, codenib::core::OCCURRENCE_ROLE_REFERENCE, 0, 1));
  try {
    (void)FactQueryIndex(invalid_range);
    assert(false);
  } catch (const std::invalid_argument &) {
  }

  auto invalid_symbol = make_occurrence_records();
  invalid_symbol->occurrences.push_back(
      occurrence(13, 4, 8, codenib::core::OCCURRENCE_ROLE_REFERENCE, 99, 1));
  try {
    (void)FactQueryIndex(invalid_symbol);
    assert(false);
  } catch (const std::out_of_range &) {
  }
}

} // namespace

int main() {
  assert_symbol_resolution_and_postings();
  assert_invalid_records_fail_closed();
  assert_exact_occurrence_ranges_and_fallbacks();
  assert_invalid_occurrences_fail_closed();
  std::cout << "fact_query_index_test: OK\n";
  return 0;
}
