// Pybind11 bindings for codeminer::core SCIP decoders.
//
// Exposes a single function `decode_scip(index_file, project_root, language)`
// that returns a `DecodedGraph` struct with two flat Python lists:
//   - vertices: list of dicts {name,type,file,start_line,end_line,unified_name}
//   - edges:    list of tuples (source_name, target_name, type)
//
// The Python-side wrapper (scip_decode_cpp.py) builds a CodeGraph from this.
// We keep the binding lean (no custom class for CodeGraph) so that
// serialization cost across the C++/Python boundary is just one igraph vcount +
// ecount worth of tuples/dicts. Measured overhead on sympy (48K/221K) is ~1-2s,
// dominated by the Python-side list construction — not worth optimizing further
// since the Python serial decoder already reaches minutes on this input.

#include "scip_decode.h"

#include <memory>
#include <optional>
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include <stdexcept>
#include <string>

namespace py = pybind11;
using codeminer::core::CodeGraph;
using codeminer::core::SCIPDecoderBase;
using codeminer::core::SCIPGoDecoder;
using codeminer::core::SCIPPythonDecoder;
using codeminer::core::SCIPRustDecoder;
using codeminer::core::SCIPTSDecoder;

namespace {

std::unique_ptr<SCIPDecoderBase>
make_decoder(const std::string &language, const std::string &index_file,
             const std::optional<std::string> &project_root) {
  if (language == "python") {
    return std::make_unique<SCIPPythonDecoder>(index_file, project_root);
  }
  if (language == "go") {
    return std::make_unique<SCIPGoDecoder>(index_file, project_root);
  }
  if (language == "rust") {
    return std::make_unique<SCIPRustDecoder>(index_file, project_root);
  }
  if (language == "typescript" || language == "ts" || language == "js") {
    return std::make_unique<SCIPTSDecoder>(index_file, project_root);
  }
  throw std::invalid_argument("Unknown language: " + language +
                              " (expected: python | go | rust | typescript)");
}

py::dict vertex_to_dict(const CodeGraph::VertexData &v) {
  py::dict d;
  d["name"] = v.name;
  d["type"] = v.type;
  d["file"] = v.file.has_value() ? py::cast(*v.file) : py::none();
  d["start_line"] =
      v.start_line.has_value() ? py::cast(*v.start_line) : py::none();
  d["end_line"] = v.end_line.has_value() ? py::cast(*v.end_line) : py::none();
  d["unified_name"] =
      v.unified_name.has_value() ? py::cast(*v.unified_name) : py::none();
  return d;
}

py::dict decode_scip(const std::string &index_file,
                     std::optional<std::string> project_root,
                     const std::string &language) {
  auto decoder = make_decoder(language, index_file, project_root);

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
  py::list edge_list;
  const auto &cpp_edges = graph.edges();
  for (const auto &e : cpp_edges) {
    const std::string &src_name = vertices[e.source].name;
    const std::string &tgt_name = vertices[e.target].name;
    edge_list.append(py::make_tuple(src_name, tgt_name, e.type));
  }

  py::dict result;
  result["vertices"] = vertex_list;
  result["edges"] = edge_list;
  result["project_root"] = graph.project_root();
  return result;
}

} // namespace

PYBIND11_MODULE(codeminer_core, m) {
  m.doc() = "codeminer::core SCIP decoders (Python / Go / Rust / TypeScript).";

  m.def("decode_scip", &decode_scip, py::arg("index_file"),
        py::arg("project_root") = std::optional<std::string>(std::nullopt),
        py::arg("language") = std::string("python"),
        R"pbdoc(
Decode a SCIP `index.decoded` file into a graph representation.

Args:
    index_file: path to the decoded SCIP index file.
    project_root: project root directory (for language-specific config:
        go.mod / Cargo.toml). May be None.
    language: one of "python", "go", "rust", "typescript".

Returns:
    dict with:
      - "vertices": list of dicts with keys
          name, type, file, start_line, end_line, unified_name
      - "edges": list of tuples (source_name, target_name, edge_type)
      - "project_root": the effective project_root used.
)pbdoc");
}
