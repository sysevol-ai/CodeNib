#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include <pybind11/functional.h>

#include "code_graph.h"
#include "scip_decode.h"

namespace py = pybind11;
using namespace codeminer::core;

PYBIND11_MODULE(codeminer_core, m) {
    m.doc() = "CodeMiner C++ Core - High-performance SCIP decoder and code graph";

    // Export constants
    m.attr("NODE_TYPE_DIRECTORY") = NODE_TYPE_DIRECTORY;
    m.attr("NODE_TYPE_FILE") = NODE_TYPE_FILE;
    m.attr("NODE_TYPE_SYMBOL") = NODE_TYPE_SYMBOL;
    m.attr("NODE_TYPE_CLASS") = NODE_TYPE_CLASS;
    m.attr("NODE_TYPE_FUNCTION") = NODE_TYPE_FUNCTION;
    m.attr("NODE_TYPE_METHOD") = NODE_TYPE_METHOD;
    m.attr("NODE_TYPE_FIELD") = NODE_TYPE_FIELD;
    m.attr("EDGE_TYPE_CONTAIN") = EDGE_TYPE_CONTAIN;
    m.attr("EDGE_TYPE_REFERENCE") = EDGE_TYPE_REFERENCE;
    m.attr("ROOT_NODE") = ROOT_NODE;

    // Bind VertexData struct
    py::class_<CodeGraph::VertexData>(m, "VertexData")
        .def(py::init<>())
        .def_readwrite("name", &CodeGraph::VertexData::name)
        .def_readwrite("type", &CodeGraph::VertexData::type)
        .def_readwrite("file", &CodeGraph::VertexData::file)
        .def_readwrite("start_line", &CodeGraph::VertexData::start_line)
        .def_readwrite("end_line", &CodeGraph::VertexData::end_line)
        .def("__repr__", [](const CodeGraph::VertexData& v) {
            std::string repr = "VertexData(name='" + v.name + "', type='" + v.type + "'";
            if (v.file.has_value()) {
                repr += ", file='" + *v.file + "'";
            }
            if (v.start_line.has_value()) {
                repr += ", start_line=" + std::to_string(*v.start_line);
            }
            if (v.end_line.has_value()) {
                repr += ", end_line=" + std::to_string(*v.end_line);
            }
            repr += ")";
            return repr;
        });

    // Bind EdgeData struct
    py::class_<CodeGraph::EdgeData>(m, "EdgeData")
        .def(py::init<>())
        .def_readwrite("source", &CodeGraph::EdgeData::source)
        .def_readwrite("target", &CodeGraph::EdgeData::target)
        .def_readwrite("type", &CodeGraph::EdgeData::type)
        .def("__repr__", [](const CodeGraph::EdgeData& e) {
            return "EdgeData(source=" + std::to_string(e.source) +
                   ", target=" + std::to_string(e.target) +
                   ", type='" + e.type + "')";
        });

    // Bind CodeGraph class
    py::class_<CodeGraph>(m, "CodeGraph")
        .def(py::init<>())
        .def(py::init<std::string>(), py::arg("project_root"))

        // Node manipulation methods
        .def("add_root_node", &CodeGraph::add_root_node, py::arg("root_name"),
             "Add a root node to the graph")
        .def("add_directory_node", &CodeGraph::add_directory_node, py::arg("dir_path"),
             "Add a directory node to the graph")
        .def("add_file_node", &CodeGraph::add_file_node, py::arg("file_path"),
             "Add a file node to the graph")
        .def("add_symbol_node", &CodeGraph::add_symbol_node,
             py::arg("symbol"),
             py::arg("line"),
             py::arg("scope_start_line") = std::nullopt,
             py::arg("scope_end_line") = std::nullopt,
             py::arg("symbol_type") = NODE_TYPE_SYMBOL,
             "Add a symbol node to the graph")
        .def("add_symbol_reference", &CodeGraph::add_symbol_reference,
             py::arg("symbol"),
             py::arg("module_path") = std::nullopt,
             py::arg("symbol_type") = NODE_TYPE_SYMBOL,
             "Add a symbol reference to the graph")

        // Edge methods
        .def("add_containment_edge", &CodeGraph::add_containment_edge,
             py::arg("target_symbol"),
             "Add a containment edge from current scope to target symbol")
        .def("add_edge", &CodeGraph::add_edge,
             py::arg("source"), py::arg("target"), py::arg("edge_type"),
             "Add an edge between two nodes")

        // Batch operations
        .def("batch_upsert_nodes", &CodeGraph::batch_upsert_nodes,
             py::arg("nodes"),
             "Batch insert or update multiple nodes")
        .def("batch_add_edges", &CodeGraph::batch_add_edges,
             py::arg("edges"),
             "Batch add multiple edges")

        // Scope management
        .def("update_current_scope", &CodeGraph::update_current_scope,
             py::arg("symbol"),
             py::arg("start_line") = std::nullopt,
             py::arg("end_line") = std::nullopt,
             "Update the current scope to the given symbol")
        .def("exit_scopes_by_line", &CodeGraph::exit_scopes_by_line,
             py::arg("current_line"),
             "Exit scopes that have ended before the current line")

        // Query methods
        .def("get_node_info_by_name", &CodeGraph::get_node_info_by_name,
             py::arg("node_name"),
             "Get node information by name")
        .def("get_node_info_by_id", &CodeGraph::get_node_info_by_id,
             py::arg("vertex_id"),
             "Get node information by vertex ID")
        .def("get_neighbors", &CodeGraph::get_neighbors,
             py::arg("node_name"),
             "Get all neighbor vertex IDs for a given node")
        .def("get_node_content", &CodeGraph::get_node_content,
             py::arg("vertex_id"),
             "Get the content of a node (file content or code snippet)")

        // I/O methods
        .def("save_graph", &CodeGraph::save_graph,
             py::arg("output_path"),
             "Save the graph to a JSON file")
        .def_static("load_graph", &CodeGraph::load_graph,
             py::arg("input_path"),
             "Load a graph from a JSON file")

        // Properties
        .def_property("project_root",
                      &CodeGraph::project_root,
                      &CodeGraph::set_project_root,
                      "Get or set the project root directory")
        .def_property_readonly("current_scope", &CodeGraph::current_scope,
                               "Get the current scope")

        // Special methods
        .def("__repr__", [](const CodeGraph& g) {
            return "<CodeGraph project_root='" + g.project_root() + "'>";
        });

    // Bind SCIPGraphDecoder class
    py::class_<SCIPGraphDecoder>(m, "SCIPGraphDecoder")
        .def(py::init<std::string, std::optional<std::string>>(),
             py::arg("index_file_path"),
             py::arg("project_root") = std::nullopt,
             "Initialize SCIP graph decoder with index file path and optional project root")
        .def("decode", &SCIPGraphDecoder::decode,
             "Decode the SCIP index file and return a CodeGraph")
        .def("__repr__", [](const SCIPGraphDecoder&) {
            return "<SCIPGraphDecoder>";
        });

    // Version info
    m.attr("__version__") = "0.1.0";
}
