// SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
//
// SPDX-License-Identifier: Apache-2.0

// Pybind11 bindings for codeminer::core SCIP decoders.
//
// Exposes a single function `decode_scip(index_file, project_root, language)`
// that returns a `DecodedGraph` struct with two flat Python lists:
//   - vertices: list of dicts
//       {name,type,file,start_line,end_line,selection_line,unified_name}
//   - edges:    list of 5-tuples (source_name, target_name, type,
//                                 anchor_file_or_None, anchor_line_or_None)
//
// The Python-side wrapper (scip_decode_core.py) builds a CodeGraph from this.
// Anchor info is included so range-query indexes (`build_range_indexes`) on
// the Python side can resolve call-site lines emitted by the C++ decoders.
//
// We keep the binding lean (no custom class for CodeGraph) so that
// serialization cost across the C++/Python boundary is just one igraph vcount +
// ecount worth of tuples/dicts.

#include "graph_layers.h"
#include "scip_decode.h"

#include <memory>
#include <optional>
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include <stdexcept>
#include <string>
#include <vector>

namespace py = pybind11;
using codeminer::core::CodeGraph;

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
  return d;
}

py::dict decode_scip(const std::string &index_file,
                     std::optional<std::string> project_root,
                     const std::string &language) {
  auto decoder =
      codeminer::core::make_scip_decoder(language, index_file, project_root);

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

codeminer::core::LayerBuckets
classify_edge_layers_py(const std::vector<std::string> &edge_types) {
  py::gil_scoped_release release;
  return codeminer::core::classify_edge_layers(edge_types);
}

} // namespace

PYBIND11_MODULE(codeminer_core, m) {
  m.doc() =
      "codeminer::core SCIP decoders (Python / Go / Rust / Ruby / TypeScript).";

  m.def("decode_scip", &decode_scip, py::arg("index_file"),
        py::arg("project_root") = std::optional<std::string>(std::nullopt),
        py::arg("language") = std::string("python"),
        R"pbdoc(
Decode a SCIP `index.decoded` file into a graph representation.

Args:
    index_file: path to the decoded SCIP index file.
    project_root: project root directory (for language-specific config:
        go.mod / Cargo.toml). May be None.
    language: one of "python", "go", "rust", "ruby", "typescript"; aliases
        "rb", "ts", and "js" are accepted.

Returns:
    dict with:
      - "vertices": list of dicts with keys
          name, type, file, start_line, end_line, selection_line, unified_name
      - "edges": list of 5-tuples
          (source_name, target_name, edge_type,
           anchor_file_or_None, anchor_line_or_None)
      - "project_root": the effective project_root used.
)pbdoc");

  m.def("canonical_scip_decoder_languages",
        &codeminer::core::canonical_scip_decoder_languages,
        R"pbdoc(
Return the canonical language names implemented by the C++ SCIP decoder.
)pbdoc");

  m.def("accepted_scip_decoder_languages",
        &codeminer::core::accepted_scip_decoder_languages,
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
