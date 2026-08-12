// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

#include "fact_query_index.h"

#include <algorithm>
#include <cassert>
#include <future>
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

void assert_compact_route_adjacency() {
  auto records = make_records();
  records->route_vertex_order = {4, 0, 1, 2, 3, 5, 6};
  records->route_adjacency_complete = true;
  FactQueryIndex index(records);

  assert(index.supports_route_adjacency());
  assert((index.route_names() ==
          std::vector<std::string>{"external-target", "canonical-target",
                                   "canonical-caller", "canonical-run-one",
                                   "canonical-run-two", "canonical-shared-one",
                                   "canonical-shared-two"}));
  assert((index.get_successors("canonical-caller") ==
          std::vector<CodeGraph::VertexId>{4, 0, 0, 0}));
  assert((index.get_predecessors("canonical-target") ==
          std::vector<CodeGraph::VertexId>{1, 1, 1}));
  assert(index.get_successors("missing").empty());

  const auto scan = index.route_scan_records(2);
  assert(scan.size() == 2);
  assert(scan[0].name == "external-target");
  assert(scan[0].display_name == "dependency.py:external()");
  assert(scan[1].name == "canonical-target");

  auto unavailable_records = make_records();
  FactQueryIndex unavailable(unavailable_records);
  assert(!unavailable.supports_route_adjacency());
  assert(unavailable.route_names().empty());
  assert(unavailable.get_successors("canonical-caller").empty());

  auto incomplete = make_records();
  incomplete->route_adjacency_complete = true;
  incomplete->route_vertex_order = {0, 1};
  try {
    (void)FactQueryIndex(incomplete);
    assert(false);
  } catch (const std::invalid_argument &) {
  }

  auto duplicate = make_records();
  duplicate->route_adjacency_complete = true;
  duplicate->route_vertex_order = {0, 1, 2, 3, 4, 5, 5};
  try {
    (void)FactQueryIndex(duplicate);
    assert(false);
  } catch (const std::invalid_argument &) {
  }
}

constexpr const char *BASE_FILTER_SURFACE =
    "0112871fe1aafef3930d910a15c12b7632f73803fd55f5620f219c9dd87d64df";
constexpr const char *ROOT_FILTER_SURFACE =
    "5f22cb22ddb5647ecd8b9a58c611f32fc5b67c5ad63bf0db4f5977f2a4f80179";
constexpr const char *EXTERNAL_FILTER_SURFACE =
    "f719c83c8abe745e9473aa287ce64a19865e8dac5691f6ce2c8ca2f5881b2e77";

std::shared_ptr<DecodedRecords> make_filter_proof_records() {
  auto records = std::make_shared<DecodedRecords>();
  CodeGraph::VertexData root;
  root.name = codenib::core::ROOT_NODE;
  root.type = "root";
  CodeGraph::VertexData directory;
  directory.name = "src";
  directory.type = codenib::core::NODE_TYPE_DIRECTORY;
  directory.unified_name = "src";
  CodeGraph::VertexData file;
  file.name = "src/main.py";
  file.type = codenib::core::NODE_TYPE_FILE;
  file.unified_name = "src/main.py";
  records->vertices = {
      root,
      directory,
      file,
      definition("canonical-caller", "src/main.py:caller()", 8),
      definition("canonical-target", "src/main.py:target()", 2),
  };
  records->edges = {
      {0, 1, codenib::core::EDGE_TYPE_CONTAIN, std::nullopt, std::nullopt},
      {1, 2, codenib::core::EDGE_TYPE_CONTAIN, std::nullopt, std::nullopt},
      {2, 3, codenib::core::EDGE_TYPE_CONTAIN, std::nullopt, std::nullopt},
      {3, 4, codenib::core::EDGE_TYPE_REFERENCE, "src/main.py", 9},
  };
  return records;
}

void assert_filter_identity_proof() {
  FactQueryIndex index(make_filter_proof_records());
  const auto proof = index.prove_filter_identity(
      {"src/helper.py", "src/main.py"}, BASE_FILTER_SURFACE);
  assert(proof.schema_version == 1);
  assert(proof.identity);
  assert(proof.allowed_file_count == 2);
  assert(proof.allowed_files_sha256 ==
         "eba6f15171dd9ec7a051c2e9d51b0d72989bf55c6b0c1108d11fd929c636f6b3");
  assert(proof.query_surface_sha256 ==
         "0112871fe1aafef3930d910a15c12b7632f73803fd55f5620f219c9dd87d64df");
  assert(proof.record_count == 5);
  assert(proof.directory_count == 1);
  assert(proof.file_count == 1);
  assert(proof.definition_count == 2);
  assert(proof.reference_only_count == 0);
  assert(proof.edge_count == 4);
  assert(proof.reference_count == 1);
  assert(proof.occurrence_count == 0);
  std::vector<std::future<FactQueryIndex::FilterIdentityProof>> concurrent;
  for (int worker = 0; worker < 4; ++worker) {
    concurrent.emplace_back(std::async(std::launch::async, [&index]() {
      return index.prove_filter_identity({"src/helper.py", "src/main.py"},
                                         BASE_FILTER_SURFACE);
    }));
  }
  for (auto &result : concurrent)
    assert(result.get().query_surface_sha256 == proof.query_surface_sha256);
  const auto narrower =
      index.prove_filter_identity({"src/main.py"}, BASE_FILTER_SURFACE);
  assert(narrower.allowed_files_sha256 != proof.allowed_files_sha256);
  assert(narrower.query_surface_sha256 == proof.query_surface_sha256);

  auto changed_selection = make_filter_proof_records();
  changed_selection->vertices[4].selection_line = 3;
  const auto changed_selection_proof =
      FactQueryIndex(changed_selection)
          .prove_filter_identity({"src/main.py"},
                                 "f9605a33aeabcc7a9dc8217df0cf2799eab9e2e5c762d"
                                 "16f98a8a2bf979e9365");
  assert(changed_selection_proof.record_count == proof.record_count);
  assert(changed_selection_proof.edge_count == proof.edge_count);
  assert(changed_selection_proof.query_surface_sha256 !=
         proof.query_surface_sha256);

  auto changed_anchor = make_filter_proof_records();
  changed_anchor->edges.back().anchor_line = 10;
  const auto changed_anchor_proof =
      FactQueryIndex(changed_anchor)
          .prove_filter_identity({"src/main.py"},
                                 "4843be465daee641c550915a75873739879b5e89f0814"
                                 "c932a964ef5713106cd");
  assert(changed_anchor_proof.record_count == proof.record_count);
  assert(changed_anchor_proof.edge_count == proof.edge_count);
  assert(changed_anchor_proof.query_surface_sha256 !=
         proof.query_surface_sha256);

  auto root_only = std::make_shared<DecodedRecords>();
  CodeGraph::VertexData root;
  root.name = codenib::core::ROOT_NODE;
  root.type = "root";
  root_only->vertices.push_back(root);
  const auto empty =
      FactQueryIndex(root_only).prove_filter_identity({}, ROOT_FILTER_SURFACE);
  assert(empty.allowed_file_count == 0);
  assert(empty.allowed_files_sha256 ==
         "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
  const auto unicode = FactQueryIndex(root_only).prove_filter_identity(
      {"src/a.py", "src/\xc3\xa9.py"}, ROOT_FILTER_SURFACE);
  assert(unicode.allowed_files_sha256 ==
         "d2d8f5e99942bf3b1dde637675b3b908e1b68e0c78bda25fe9ef6eb197a9aed8");

  auto rejects = [](std::shared_ptr<DecodedRecords> records,
                    std::vector<std::string> allowed_files,
                    const std::string &expected_surface = BASE_FILTER_SURFACE) {
    try {
      (void)FactQueryIndex(std::move(records))
          .prove_filter_identity(allowed_files, expected_surface);
      assert(false && "unsafe filter proof must fail closed");
    } catch (const std::invalid_argument &) {
    }
  };

  rejects(make_filter_proof_records(), {"src/main.py"}, std::string(64, '0'));
  rejects(make_filter_proof_records(), {"src/main.py"}, "invalid");
  rejects(make_filter_proof_records(), {"src/main.py"}, std::string(64, 'A'));

  rejects(make_filter_proof_records(), {"src/main.py", "src/main.py"});
  rejects(make_filter_proof_records(), {"src/main.py", "src/helper.py"});
  for (const auto &path :
       {std::string{}, std::string{"/src/main.py"}, std::string{"src\\main.py"},
        std::string{"src/./main.py"}, std::string{"src/../main.py"},
        std::string{"src//main.py"}, std::string{"src/main.py/"},
        std::string{"C:/src/main.py"}, std::string{"src/ma\0in.py", 13},
        std::string{"src/"
                    "\xc0\xaf"
                    "main.py"}}) {
    rejects(make_filter_proof_records(), {path});
  }
  rejects(make_filter_proof_records(), {"src/helper.py"});

  auto filtered_anchor = make_filter_proof_records();
  filtered_anchor->edges.back().anchor_file = "vendor/main.py";
  rejects(filtered_anchor, {"src/main.py"});

  auto occurrence_metadata = make_filter_proof_records();
  occurrence_metadata->occurrences = {
      occurrence(9, 2, 8, codenib::core::OCCURRENCE_ROLE_REFERENCE, 4, 3),
  };
  rejects(occurrence_metadata, {"src/main.py"});

  auto query_order_metadata = make_filter_proof_records();
  query_order_metadata->query_resolution_order = {0, 1, 2, 3, 4};
  rejects(query_order_metadata, {"src/main.py"});

  auto route_order_metadata = make_filter_proof_records();
  route_order_metadata->route_vertex_order = {0, 1, 2, 3, 4};
  rejects(route_order_metadata, {"src/main.py"});

  auto route_adjacency_metadata = make_filter_proof_records();
  route_adjacency_metadata->route_adjacency_complete = true;
  rejects(route_adjacency_metadata, {"src/main.py"});

  auto external = make_filter_proof_records();
  external->vertices.push_back(
      reference_only("external-target", "vendor/lib.py:external()"));
  external->edges.push_back(
      {3, 5, codenib::core::EDGE_TYPE_REFERENCE, "src/main.py", 10});
  const auto external_proof = FactQueryIndex(external).prove_filter_identity(
      {"src/main.py"}, EXTERNAL_FILTER_SURFACE);
  assert(external_proof.reference_only_count == 1);
  assert(external_proof.query_surface_sha256 == EXTERNAL_FILTER_SURFACE);

  auto reference_without_provenance = make_filter_proof_records();
  auto null_reference =
      reference_only("external-target", "vendor/lib.py:external()");
  null_reference.has_definition.reset();
  reference_without_provenance->vertices.push_back(null_reference);
  reference_without_provenance->edges.push_back(
      {3, 5, codenib::core::EDGE_TYPE_REFERENCE, "src/main.py", 10});
  rejects(reference_without_provenance, {"src/main.py"},
          "7c5301fc83aae5751d1414c2db1ee527d887f778a2bdd6d41978fa92e16cf72f");

  auto ranged_reference = make_filter_proof_records();
  auto invalid_reference =
      reference_only("external-target", "vendor/lib.py:external()");
  invalid_reference.start_line = 1;
  invalid_reference.end_line = 2;
  invalid_reference.selection_line = 1;
  ranged_reference->vertices.push_back(invalid_reference);
  ranged_reference->edges.push_back(
      {3, 5, codenib::core::EDGE_TYPE_REFERENCE, "src/main.py", 10});
  rejects(ranged_reference, {"src/main.py"},
          "841dca4d50387287e714ae580c4a406ca3b98cc749ddc90a97554c45a6e73c03");

  auto orphan_reference = make_filter_proof_records();
  orphan_reference->vertices.push_back(
      reference_only("external-target", "vendor/lib.py:external()"));
  rejects(orphan_reference, {"src/main.py"});

  auto reference_source = make_filter_proof_records();
  reference_source->vertices.push_back(
      reference_only("external-target", "vendor/lib.py:external()"));
  reference_source->edges.push_back(
      {5, 4, codenib::core::EDGE_TYPE_REFERENCE, "src/main.py", 10});
  rejects(reference_source, {"src/main.py"});

  auto structural_reference_target = make_filter_proof_records();
  structural_reference_target->vertices.push_back(
      reference_only("external-target", "vendor/lib.py:external()"));
  structural_reference_target->edges.push_back(
      {3, 5, codenib::core::EDGE_TYPE_REFERENCE, "src/main.py", 10});
  structural_reference_target->edges.push_back(
      {2, 5, codenib::core::EDGE_TYPE_CONTAIN, std::nullopt, std::nullopt});
  rejects(structural_reference_target, {"src/main.py"});

  auto unknown_edge = make_filter_proof_records();
  unknown_edge->edges.push_back({3, 4, "unknown", std::nullopt, std::nullopt});
  rejects(unknown_edge, {"src/main.py"});

  auto unknown_vertex = make_filter_proof_records();
  CodeGraph::VertexData unknown;
  unknown.name = "unknown-record";
  unknown.type = "unknown";
  unknown_vertex->vertices.push_back(unknown);
  rejects(unknown_vertex, {"src/main.py"});

  auto root_reference = make_filter_proof_records();
  root_reference->edges.push_back(
      {0, 4, codenib::core::EDGE_TYPE_REFERENCE, "src/main.py", 11});
  rejects(root_reference, {"src/main.py"});

  auto mismatched_unified = make_filter_proof_records();
  mismatched_unified->vertices[4].unified_name = "vendor/main.py:target()";
  rejects(mismatched_unified, {"src/main.py"});

  auto invalid_utf8_name = make_filter_proof_records();
  invalid_utf8_name->vertices[4].name = "symbol-\xc0\xaf";
  rejects(invalid_utf8_name, {"src/main.py"});

  auto anchored_containment = make_filter_proof_records();
  anchored_containment->edges.front().anchor_file = "src/main.py";
  anchored_containment->edges.front().anchor_line = 1;
  rejects(anchored_containment, {"src/main.py"});

  auto colon_path = std::make_shared<DecodedRecords>();
  CodeGraph::VertexData colon_file;
  colon_file.name = "a:b.py";
  colon_file.type = codenib::core::NODE_TYPE_FILE;
  auto colon_definition = definition("colon-symbol", "a:b.py:thing()", 1);
  colon_definition.file = "a:b.py";
  colon_path->vertices = {root, colon_file, colon_definition};
  colon_path->edges = {
      {0, 1, codenib::core::EDGE_TYPE_CONTAIN, std::nullopt, std::nullopt},
      {1, 2, codenib::core::EDGE_TYPE_CONTAIN, std::nullopt, std::nullopt},
  };
  rejects(colon_path, {"a:b.py"});
}

} // namespace

int main() {
  assert_symbol_resolution_and_postings();
  assert_invalid_records_fail_closed();
  assert_exact_occurrence_ranges_and_fallbacks();
  assert_invalid_occurrences_fail_closed();
  assert_compact_route_adjacency();
  assert_filter_identity_proof();
  std::cout << "fact_query_index_test: OK\n";
  return 0;
}
