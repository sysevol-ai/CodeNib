// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

// Pybind11 bindings for codenib::core semantic decoders and transports.
//
// Exposes a low-level function `decode_scip(index_file, project_root,
// language)` that returns a transport with two flat Python lists:
//   - vertices: list of dicts
//       {name,type,file,start_line,end_line,selection_line,unified_name,
//        symbol_kind,has_definition}
//   - edges:    list of 5-tuples (source_name, target_name, type,
//                                 anchor_file_or_None, anchor_line_or_None)
//
// The Python-side wrapper (scip_decode_core.py) builds and source-enriches the
// supported CodeGraph contract from this raw transport. In particular, callers
// must not treat this function as a complete schema-v5 graph entry point.
// Anchor info is included so range-query indexes (`build_range_indexes`) on
// the Python side can resolve call-site lines emitted by the C++ decoders.
//
// We keep the binding lean (no custom class for CodeGraph) so that
// serialization cost across the C++/Python boundary is just one igraph vcount +
// ecount worth of tuples/dicts.

#include "fact_batch_buffer.h"
#include "fact_query_index.h"
#include "graph_layers.h"
#include "scip_decode.h"

#include <chrono>
#include <cstdint>
#include <memory>
#include <optional>
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include <stdexcept>
#include <string>
#include <unordered_map>
#include <vector>

namespace py = pybind11;
using codenib::core::CodeGraph;

namespace {

py::dict vertex_to_dict(const CodeGraph::VertexData &v) {
  py::dict d;
  d["name"] = v.name;
  d["type"] = v.type;
  d["file"] = v.file.has_value() ? py::cast(*v.file) : py::none();
  d["start_line"] =
      v.start_line.has_value() ? py::cast(*v.start_line) : py::none();
  d["end_line"] = v.end_line.has_value() ? py::cast(*v.end_line) : py::none();
  d["selection_line"] =
      v.selection_line.has_value() ? py::cast(*v.selection_line) : py::none();
  d["unified_name"] =
      v.unified_name.has_value() ? py::cast(*v.unified_name) : py::none();
  d["symbol_kind"] =
      v.symbol_kind.has_value() ? py::cast(*v.symbol_kind) : py::none();
  d["has_definition"] =
      v.has_definition.has_value() ? py::cast(*v.has_definition) : py::none();
  return d;
}

py::dict fact_query_capabilities() {
  py::dict result;
  result["definition_by_symbol"] = true;
  result["references_by_symbol"] = true;
  result["position_queries"] = false;
  result["route_queries"] = false;
  return result;
}

py::dict
reference_to_dict(const codenib::core::FactQueryIndex::Reference &reference) {
  py::dict result;
  result["source_vid"] = reference.source;
  result["file"] = reference.anchor_file;
  result["line"] = reference.anchor_line;
  return result;
}

py::dict decode_scip(const std::string &index_file,
                     std::optional<std::string> project_root,
                     const std::string &language) {
  auto decoder =
      codenib::core::make_scip_decoder(language, index_file, project_root);

  // Release the GIL while the C++ decoder runs (it does its own
  // std::thread-based parallelism; we don't call back into Python).
  CodeGraph graph = [&]() {
    py::gil_scoped_release release;
    return decoder->decode();
  }();

  // Flatten vertices.
  py::list vertex_list;
  const auto &vertices = graph.vertices();
  for (const auto &v : vertices) {
    vertex_list.append(vertex_to_dict(v));
  }

  // Flatten edges: resolve vertex ids → names for portability.
  // 5-tuple = (src, tgt, type, anchor_file_or_None, anchor_line_or_None).
  // The Python wrapper (scip_decode_core.py) handles either 3- or 5-tuple
  // for forward compatibility.
  py::list edge_list;
  const auto &cpp_edges = graph.edges();
  for (const auto &e : cpp_edges) {
    const std::string &src_name = vertices[e.source].name;
    const std::string &tgt_name = vertices[e.target].name;
    py::object anchor_file =
        e.anchor_file.has_value() ? py::cast(*e.anchor_file) : py::none();
    py::object anchor_line =
        e.anchor_line.has_value() ? py::cast(*e.anchor_line) : py::none();
    edge_list.append(
        py::make_tuple(src_name, tgt_name, e.type, anchor_file, anchor_line));
  }

  py::dict result;
  result["vertices"] = vertex_list;
  result["edges"] = edge_list;
  result["project_root"] = graph.project_root();
  return result;
}

py::bytes bytes_from_vector(const std::vector<std::uint8_t> &buffer) {
  if (buffer.empty())
    return py::bytes();
  return py::bytes(reinterpret_cast<const char *>(buffer.data()),
                   buffer.size());
}

enum class FactBufferTable {
  Meta,
  Batches,
  Symbols,
  Occurrences,
  Edges,
  Diagnostics,
  GraphVertices,
  GraphEdges,
  Arena,
};

class FactBatchOwnedBuffer {
public:
  FactBatchOwnedBuffer(std::shared_ptr<codenib::core::FactBatchBuffers> owner,
                       FactBufferTable table)
      : owner_(std::move(owner)), table_(table) {}

  const std::vector<std::uint8_t> &bytes() const {
    switch (table_) {
    case FactBufferTable::Meta:
      return owner_->meta;
    case FactBufferTable::Batches:
      return owner_->batches;
    case FactBufferTable::Symbols:
      return owner_->symbols;
    case FactBufferTable::Occurrences:
      return owner_->occurrences;
    case FactBufferTable::Edges:
      return owner_->edges;
    case FactBufferTable::Diagnostics:
      return owner_->diagnostics;
    case FactBufferTable::GraphVertices:
      return owner_->graph_vertices;
    case FactBufferTable::GraphEdges:
      return owner_->graph_edges;
    case FactBufferTable::Arena:
      return owner_->arena;
    }
    throw std::logic_error("unknown FactBatchBuffer table");
  }

  py::buffer_info buffer_info() const {
    const auto &buffer = bytes();
    return py::buffer_info(buffer.data(),
                           static_cast<py::ssize_t>(buffer.size()), true);
  }

private:
  std::shared_ptr<codenib::core::FactBatchBuffers> owner_;
  FactBufferTable table_;
};

void add_fact_buffers(py::dict &result, codenib::core::FactBatchBuffers buffers,
                      bool copy_buffers) {
  if (copy_buffers) {
    result["meta"] = bytes_from_vector(buffers.meta);
    result["batches"] = bytes_from_vector(buffers.batches);
    result["symbols"] = bytes_from_vector(buffers.symbols);
    result["occurrences"] = bytes_from_vector(buffers.occurrences);
    result["edges"] = bytes_from_vector(buffers.edges);
    result["diagnostics"] = bytes_from_vector(buffers.diagnostics);
    result["graph_vertices"] = bytes_from_vector(buffers.graph_vertices);
    result["graph_edges"] = bytes_from_vector(buffers.graph_edges);
    result["arena"] = bytes_from_vector(buffers.arena);
    return;
  }

  auto owner =
      std::make_shared<codenib::core::FactBatchBuffers>(std::move(buffers));
  auto add = [&](const char *name, FactBufferTable table) {
    result[name] = py::cast(FactBatchOwnedBuffer(owner, table));
  };
  add("meta", FactBufferTable::Meta);
  add("batches", FactBufferTable::Batches);
  add("symbols", FactBufferTable::Symbols);
  add("occurrences", FactBufferTable::Occurrences);
  add("edges", FactBufferTable::Edges);
  add("diagnostics", FactBufferTable::Diagnostics);
  add("graph_vertices", FactBufferTable::GraphVertices);
  add("graph_edges", FactBufferTable::GraphEdges);
  add("arena", FactBufferTable::Arena);
}

py::dict
decode_profile_to_dict(const codenib::core::SCIPDecodeProfile &profile) {
  py::dict result;
  result["load_metadata"] = profile.load_metadata_ns;
  result["file_read"] = profile.file_read_ns;
  result["extract_blocks"] = profile.extract_blocks_ns;
  result["prescan"] = profile.prescan_ns;
  result["process_documents"] = profile.process_documents_ns;
  result["merge_subgraphs"] = profile.merge_subgraphs_ns;
  result["materialize_graph"] = profile.materialize_graph_ns;
  result["total"] = profile.total_ns;
  result["worker_count"] = profile.worker_count;
  result["document_count"] = profile.document_count;
  return result;
}

py::dict fact_encode_profile_to_dict(
    const codenib::core::FactBatchEncodeProfile &profile) {
  py::dict result;
  result["validate_options"] = profile.validate_options_ns;
  result["collect_indexes"] = profile.collect_indexes_ns;
  result["collect_facts"] = profile.collect_facts_ns;
  result["encode_semantic"] = profile.encode_semantic_ns;
  result["encode_graph"] = profile.encode_graph_ns;
  result["finalize"] = profile.finalize_ns;
  result["total"] = profile.total_ns;
  return result;
}

py::dict decode_scip_fact_buffer(
    const std::string &index_file, const std::string &profile_digest,
    std::optional<std::string> project_root, const std::string &language,
    const std::unordered_map<std::string, std::string> &content_digests,
    bool include_graph_compat, bool copy_buffers) {
  auto decoder =
      codenib::core::make_scip_decoder(language, index_file, project_root);
  codenib::core::FactBatchBufferOptions options;
  options.language = language;
  options.profile_digest = profile_digest;
  options.content_digests = content_digests;
  options.include_graph_compat = include_graph_compat;

  using clock = std::chrono::steady_clock;
  clock::time_point decode_started;
  clock::time_point decode_finished;
  clock::time_point encode_finished;
  codenib::core::SCIPDecodeProfile decode_profile;
  codenib::core::FactBatchBuffers buffers;
  {
    py::gil_scoped_release release;
    decode_started = clock::now();
    codenib::core::SCIPDecodedRecords records = decoder->decode_records();
    decode_finished = clock::now();
    decode_profile = decoder->last_profile();
    buffers = codenib::core::encode_fact_batch_buffer(
        records.vertices, records.edges, records.project_root, options);
    encode_finished = clock::now();
  }

  auto elapsed_ns = [](clock::time_point start, clock::time_point end) {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(end - start)
        .count();
  };
  py::dict result;
  const auto encode_profile = buffers.profile;
  add_fact_buffers(result, std::move(buffers), copy_buffers);
  result["native_decode_ns"] = elapsed_ns(decode_started, decode_finished);
  result["native_encode_ns"] = elapsed_ns(decode_finished, encode_finished);
  result["decode_profile_ns"] = decode_profile_to_dict(decode_profile);
  result["encode_profile_ns"] = fact_encode_profile_to_dict(encode_profile);
  return result;
}

py::dict fact_batch_buffer_contract() {
  py::dict result;
  result["abi_version"] = codenib::core::FACT_BATCH_BUFFER_ABI_VERSION;
  result["schema_version"] = codenib::core::FACT_BATCH_SCHEMA_VERSION;
  result["meta_size"] = codenib::core::FACT_BATCH_META_SIZE;
  result["batch_row_size"] = codenib::core::FACT_BATCH_ROW_SIZE;
  result["symbol_row_size"] = codenib::core::FACT_SYMBOL_ROW_SIZE;
  result["occurrence_row_size"] = codenib::core::FACT_OCCURRENCE_ROW_SIZE;
  result["edge_row_size"] = codenib::core::FACT_EDGE_ROW_SIZE;
  result["diagnostic_row_size"] = codenib::core::FACT_DIAGNOSTIC_ROW_SIZE;
  result["graph_vertex_row_size"] = codenib::core::FACT_GRAPH_VERTEX_ROW_SIZE;
  result["graph_edge_row_size"] = codenib::core::FACT_GRAPH_EDGE_ROW_SIZE;
  return result;
}

py::dict decode_scip_fact_query_index(const std::string &index_file,
                                      std::optional<std::string> project_root,
                                      const std::string &language) {
  auto decoder =
      codenib::core::make_scip_decoder(language, index_file, project_root);

  using clock = std::chrono::steady_clock;
  clock::time_point decode_started;
  clock::time_point decode_finished;
  clock::time_point index_finished;
  codenib::core::SCIPDecodeProfile decode_profile;
  std::shared_ptr<codenib::core::FactQueryIndex> index;
  {
    py::gil_scoped_release release;
    decode_started = clock::now();
    auto records = std::make_shared<codenib::core::SCIPDecodedRecords>(
        decoder->decode_records());
    decode_finished = clock::now();
    decode_profile = decoder->last_profile();
    index = std::make_shared<codenib::core::FactQueryIndex>(records);
    index_finished = clock::now();
  }

  auto elapsed_ns = [](clock::time_point start, clock::time_point end) {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(end - start)
        .count();
  };
  py::dict result;
  result["format"] = codenib::core::FACT_QUERY_INDEX_FORMAT;
  result["index"] = std::move(index);
  result["native_decode_ns"] = elapsed_ns(decode_started, decode_finished);
  result["native_index_ns"] = elapsed_ns(decode_finished, index_finished);
  result["decode_profile_ns"] = decode_profile_to_dict(decode_profile);
  result["graph_materialized"] = false;
  return result;
}

py::dict fact_query_index_contract() {
  py::dict result;
  result["abi_version"] = codenib::core::FACT_QUERY_INDEX_ABI_VERSION;
  result["format"] = codenib::core::FACT_QUERY_INDEX_FORMAT;
  result["requires_anchored_references"] = true;
  result["capabilities"] = fact_query_capabilities();
  return result;
}

codenib::core::LayerBuckets
classify_edge_layers_py(const std::vector<std::string> &edge_types) {
  py::gil_scoped_release release;
  return codenib::core::classify_edge_layers(edge_types);
}

} // namespace

PYBIND11_MODULE(codenib_core, m) {
  m.doc() = "CodeNib native semantic decoders and transports.";

  py::class_<FactBatchOwnedBuffer>(m, "_FactBatchOwnedBuffer",
                                   py::buffer_protocol())
      .def_buffer(&FactBatchOwnedBuffer::buffer_info)
      .def("__len__", [](const FactBatchOwnedBuffer &buffer) {
        return buffer.bytes().size();
      });

  py::class_<codenib::core::FactQueryIndex,
             std::shared_ptr<codenib::core::FactQueryIndex>>(
      m, "_NativeFactQueryIndex")
      .def_property_readonly(
          "fact_query_index",
          [](const codenib::core::FactQueryIndex &) { return true; })
      .def_property_readonly(
          "materializes_graph",
          [](const codenib::core::FactQueryIndex &) { return false; })
      .def_property_readonly("capabilities",
                             [](const codenib::core::FactQueryIndex &) {
                               return fact_query_capabilities();
                             })
      .def_property_readonly("project_root",
                             [](const codenib::core::FactQueryIndex &index) {
                               return index.project_root().empty()
                                          ? py::object(py::none())
                                          : py::object(
                                                py::cast(index.project_root()));
                             })
      .def_property_readonly("record_count",
                             &codenib::core::FactQueryIndex::record_count)
      .def_property_readonly("edge_count",
                             &codenib::core::FactQueryIndex::edge_count)
      .def_property_readonly("symbol_count",
                             &codenib::core::FactQueryIndex::symbol_count)
      .def_property_readonly("reference_count",
                             &codenib::core::FactQueryIndex::reference_count)
      .def("has_symbol", &codenib::core::FactQueryIndex::has_symbol)
      .def("get_node_info_by_name",
           [](const codenib::core::FactQueryIndex &index,
              const std::string &name) -> py::object {
             const auto vertex = index.get_node_info_by_name(name);
             if (!vertex.has_value())
               return py::none();
             return vertex_to_dict(*vertex);
           })
      .def("get_node_info_by_id",
           [](const codenib::core::FactQueryIndex &index,
              CodeGraph::VertexId id) -> py::object {
             const auto vertex = index.get_node_info_by_id(id);
             if (!vertex.has_value())
               return py::none();
             return vertex_to_dict(*vertex);
           })
      .def("resolve_symbol_candidates",
           &codenib::core::FactQueryIndex::resolve_symbol_candidates,
           py::arg("symbol"), py::arg("limit") = 8)
      .def(
          "iter_incoming_references",
          [](const codenib::core::FactQueryIndex &index,
             const std::string &target_name) {
            py::list result;
            for (const auto &reference :
                 index.incoming_references(target_name)) {
              result.append(reference_to_dict(reference));
            }
            return result;
          },
          py::arg("target_name"));

  m.def("decode_scip", &decode_scip, py::arg("index_file"),
        py::arg("project_root") = std::optional<std::string>(std::nullopt),
        py::arg("language") = std::string("python"),
        R"pbdoc(
Decode a SCIP `index.decoded` file into the low-level core transport.

This binding does not apply source-aware post-decode layers. Application code
must use `codenib.scip_interface.scip_decode_core.SCIPDecoderCore` or
`codenib.ls_router.LSIndexer` to obtain the complete CodeGraph contract.

Args:
    index_file: path to the decoded SCIP index file.
    project_root: project root directory (for language-specific config:
        go.mod / Cargo.toml). May be None.
    language: one of "python", "go", "rust", "ruby", "typescript"; aliases
        "rb", "ts", and "js" are accepted.

Returns:
    dict with:
      - "vertices": list of dicts with keys
          name, type, file, start_line, end_line, selection_line, unified_name,
          symbol_kind, has_definition
      - "edges": list of 5-tuples
          (source_name, target_name, edge_type,
           anchor_file_or_None, anchor_line_or_None)
      - "project_root": the effective project_root used.
)pbdoc");

  m.def("decode_scip_fact_buffer", &decode_scip_fact_buffer,
        py::arg("index_file"), py::arg("profile_digest"),
        py::arg("project_root") = std::optional<std::string>(std::nullopt),
        py::arg("language") = std::string("python"),
        py::arg("content_digests") =
            std::unordered_map<std::string, std::string>{},
        py::arg("include_graph_compat") = true, py::arg("copy_buffers") = true,
        R"pbdoc(
Decode a SCIP index into FactBatchBuffer v1 byte tables.

The return value contains a constant number of Python bytes objects rather
than one dict/tuple per vertex or edge. ``graph_vertices`` and ``graph_edges``
are an exact compatibility projection for the existing CodeGraph schema;
the semantic tables remain grouped by file for FactBatch consumers. Set
``include_graph_compat=False`` for consumers that do not materialize the
legacy graph. Set ``copy_buffers=False`` to return ownership-safe read-only
buffer exporters instead of copying every table into Python bytes.
)pbdoc");

  m.def("fact_batch_buffer_contract", &fact_batch_buffer_contract,
        R"pbdoc(Return the compiled FactBatchBuffer ABI contract.)pbdoc");

  m.def("decode_scip_fact_query_index", &decode_scip_fact_query_index,
        py::arg("index_file"),
        py::arg("project_root") = std::optional<std::string>(std::nullopt),
        py::arg("language") = std::string("python"),
        R"pbdoc(
Decode a SCIP index into a graph-free FactQueryIndex v1.

The returned index owns immutable decoded records and integer postings. It
supports symbol-based definitions and anchored references only; position and
route queries remain unavailable and no CodeGraph or igraph is materialized.
)pbdoc");

  m.def(
      "fact_query_index_contract", &fact_query_index_contract,
      R"pbdoc(Return the compiled FactQueryIndex ABI and capabilities.)pbdoc");

  m.def("canonical_scip_decoder_languages",
        &codenib::core::canonical_scip_decoder_languages,
        R"pbdoc(
Return the canonical language names implemented by the C++ SCIP decoder.
)pbdoc");

  m.def("accepted_scip_decoder_languages",
        &codenib::core::accepted_scip_decoder_languages,
        R"pbdoc(
Return canonical language names plus aliases accepted by the C++ SCIP decoder.
)pbdoc");

  m.def("classify_edge_layers", &classify_edge_layers_py, py::arg("edge_types"),
        R"pbdoc(
Classify edge-type strings into overlapping CodeGraph layer id buckets.

Returns a dict with keys:
  all, containment, dependency, reference, import, type-use
)pbdoc");
}
