// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

#include "fact_query_index.h"
#include "scip_decode.h"

#include <cassert>
#include <chrono>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <memory>
#include <optional>
#include <stdexcept>
#include <string>
#include <system_error>
#include <utility>
#include <vector>

using codenib::core::CodeGraph;
using codenib::core::FactQueryIndex;
using codenib::core::NODE_TYPE_CLASS;
using codenib::core::NODE_TYPE_DIRECTORY;
using codenib::core::NODE_TYPE_FILE;
using codenib::core::NODE_TYPE_METHOD;
using codenib::core::ROOT_NODE;
using codenib::core::SCIPGraphDecoder;

namespace {

class TempDirectory {
public:
  TempDirectory() {
    auto base = std::filesystem::temp_directory_path();
    auto timestamp =
        std::chrono::steady_clock::now().time_since_epoch().count();
    path_ = base / ("codenib_scip_decoder_test-" + std::to_string(timestamp));
    std::error_code ec;
    std::filesystem::create_directories(path_, ec);
    if (ec) {
      throw std::runtime_error("Failed to create temporary directory: " +
                               ec.message());
    }
  }

  ~TempDirectory() {
    if (!path_.empty()) {
      std::error_code ec;
      std::filesystem::remove_all(path_, ec);
    }
  }

  const std::filesystem::path &path() const noexcept { return path_; }

private:
  std::filesystem::path path_;
};

void write_python_source(const std::filesystem::path &project_root) {
  const auto sample_dir = project_root / "sample";
  std::error_code ec;
  std::filesystem::create_directories(sample_dir, ec);
  if (ec) {
    throw std::runtime_error("Failed to create sample directory: " +
                             ec.message());
  }

  const auto module_path = sample_dir / "module.py";
  std::ofstream python_file(module_path);
  if (!python_file.is_open()) {
    throw std::runtime_error("Failed to create sample module at " +
                             module_path.string());
  }

  python_file << R"(class SampleClass:
    """Synthetic sample to exercise SCIP decoding."""

    def __init__(self, value: int = 0) -> None:
        self._value = value

    def sample_method(self) -> int:
        """Return the stored value."""
        return self._value


def sample_call() -> int:
    obj = SampleClass(10)
    return obj.sample_method()
)";
}

void write_text_file(const std::filesystem::path &path,
                     const std::string &content) {
  std::ofstream output(path, std::ios::binary | std::ios::trunc);
  if (!output.is_open())
    throw std::runtime_error("Failed to write test input at " + path.string());
  output << content;
  if (!output)
    throw std::runtime_error("Failed to finish test input at " + path.string());
}

std::filesystem::path
write_test_index(const std::filesystem::path &project_root) {
  const std::string scip_content = R"(documents {
  relative_path: "sample/module.py"
  occurrences {
    range: 1
    range: 0
    range: 2
    symbol: "sample.module`/SampleClass#"
    symbol_roles: 1
    enclosing_range: 1
    enclosing_range: 0
    enclosing_range: 20
    enclosing_range: 0
  }
  occurrences {
    range: 5
    range: 0
    range: 6
    symbol: "sample.module`/SampleClass#sample_method()."
    symbol_roles: 1
    enclosing_range: 5
    enclosing_range: 0
    enclosing_range: 12
    enclosing_range: 0
  }
  occurrences {
    range: 15
    range: 0
    range: 16
    symbol: "sample.module`/SampleClass#sample_method()."
    symbol_roles: 8
  }
}
)";

  const auto index_path = project_root / "index.scip";
  std::ofstream output(index_path);
  assert(output.is_open());
  output << scip_content;
  output.close();
  return index_path;
}

void assert_parallel_document_order(const std::filesystem::path &project_root) {
  const auto index_path = project_root / "ordered-index.scip";
  write_text_file(index_path, R"scip(documents {
  relative_path: "src/alpha.py"
  occurrences {
    range: 0
    range: 0
    range: 3
    symbol: "src.alpha`/run()."
    symbol_roles: 1
    enclosing_range: 0
    enclosing_range: 0
    enclosing_range: 4
    enclosing_range: 0
  }
  occurrences {
    range: 2
    range: 4
    range: 7
    symbol: "src.beta`/run()."
    symbol_roles: 8
  }
}
documents {
  relative_path: "src/beta.py"
  occurrences {
    range: 0
    range: 0
    range: 3
    symbol: "src.beta`/run()."
    symbol_roles: 1
    enclosing_range: 0
    enclosing_range: 0
    enclosing_range: 4
    enclosing_range: 0
  }
  occurrences {
    range: 2
    range: 4
    range: 7
    symbol: "src.alpha`/run()."
    symbol_roles: 8
  }
}
)scip");

  const std::vector<std::string> expected_names = {
      ROOT_NODE,           "src",         "src/alpha.py", "src/alpha.py:run()",
      "src/beta.py:run()", "src/beta.py",
  };
  for (int attempt = 0; attempt < 8; ++attempt) {
    SCIPGraphDecoder decoder(index_path.string(), project_root.string());
    auto records = decoder.decode_records();
    std::vector<std::string> names;
    names.reserve(records.vertices.size());
    for (const auto &vertex : records.vertices)
      names.push_back(vertex.name);
    assert(names == expected_names);
    assert(records.edges.size() == 7);
    assert(records.edges[0].source == 0 && records.edges[0].target == 1);
    assert(records.edges[1].source == 1 && records.edges[1].target == 2);
    assert(records.edges[2].source == 2 && records.edges[2].target == 3);
    assert(records.edges[3].source == 3 && records.edges[3].target == 4);
    assert(records.edges[3].type == codenib::core::EDGE_TYPE_REFERENCE);
    assert(records.edges[3].anchor_file == "src/alpha.py");
    assert(records.edges[3].anchor_line == 2);
    assert(records.edges[4].source == 1 && records.edges[4].target == 5);
    assert(records.edges[5].source == 5 && records.edges[5].target == 4);
    assert(records.edges[6].source == 4 && records.edges[6].target == 3);
    assert(records.edges[6].type == codenib::core::EDGE_TYPE_REFERENCE);
    assert(records.edges[6].anchor_file == "src/beta.py");
    assert(records.edges[6].anchor_line == 2);

    auto shared_records =
        std::make_shared<codenib::core::DecodedRecords>(std::move(records));
    const FactQueryIndex query_index(std::move(shared_records));
    const auto proof = query_index.prove_filter_identity(
        {"src/alpha.py", "src/beta.py"},
        "dbab38dc2744ef46fd00cd8df1b22bc70212ca7020ef4f5daabb99feaf3d5ed4");
    assert(proof.query_surface_sha256 ==
           "dbab38dc2744ef46fd00cd8df1b22bc70212ca7020ef4f5daabb99feaf3d5ed4");
  }
}

void assert_parallel_rust_document_order(
    const std::filesystem::path &temp_root) {
  const auto project_root = temp_root / "ordered-rust";
  std::error_code error;
  std::filesystem::create_directories(project_root, error);
  assert(!error);
  write_text_file(project_root / "Cargo.toml",
                  "[package]\nname = \"demo\"\nversion = \"0.1.0\"\n");
  const auto index_path = project_root / "ordered-index.scip";
  write_text_file(index_path, R"scip(documents {
  relative_path: "src/alpha.rs"
  occurrences {
    range: 0
    range: 0
    range: 3
    symbol: "rust-analyzer cargo demo 0.1 alpha/run()."
    symbol_roles: 1
    enclosing_range: 0
    enclosing_range: 0
    enclosing_range: 4
    enclosing_range: 0
  }
  occurrences {
    range: 2
    range: 4
    range: 7
    symbol: "rust-analyzer cargo demo 0.1 beta/run()."
    symbol_roles: 8
  }
}
documents {
  relative_path: "src/beta.rs"
  occurrences {
    range: 0
    range: 0
    range: 3
    symbol: "rust-analyzer cargo demo 0.1 beta/run()."
    symbol_roles: 1
    enclosing_range: 0
    enclosing_range: 0
    enclosing_range: 4
    enclosing_range: 0
  }
  occurrences {
    range: 2
    range: 4
    range: 7
    symbol: "rust-analyzer cargo demo 0.1 alpha/run()."
    symbol_roles: 8
  }
}
)scip");

  const std::vector<std::string> expected_names = {
      ROOT_NODE,        "src",           "src/alpha.rs",
      "demo/alpha/run", "demo/beta/run", "src/beta.rs",
  };
  for (int attempt = 0; attempt < 8; ++attempt) {
    codenib::core::SCIPRustDecoder decoder(index_path.string(),
                                           project_root.string());
    auto records = decoder.decode_records();
    std::vector<std::string> names;
    names.reserve(records.vertices.size());
    for (const auto &vertex : records.vertices)
      names.push_back(vertex.name);
    assert(names == expected_names);
    assert(records.edges.size() == 7);
    const std::vector<std::pair<CodeGraph::VertexId, CodeGraph::VertexId>>
        expected_endpoints = {
            {0, 1}, {1, 2}, {2, 3}, {3, 4}, {1, 5}, {5, 4}, {4, 3},
        };
    for (std::size_t index = 0; index < records.edges.size(); ++index) {
      assert(records.edges[index].source == expected_endpoints[index].first);
      assert(records.edges[index].target == expected_endpoints[index].second);
    }
    assert(records.edges[3].type == codenib::core::EDGE_TYPE_REFERENCE);
    assert(records.edges[3].anchor_file == "src/alpha.rs");
    assert(records.edges[3].anchor_line == 2);
    assert(records.edges[6].type == codenib::core::EDGE_TYPE_REFERENCE);
    assert(records.edges[6].anchor_file == "src/beta.rs");
    assert(records.edges[6].anchor_line == 2);

    auto shared_records =
        std::make_shared<codenib::core::DecodedRecords>(std::move(records));
    const FactQueryIndex query_index(std::move(shared_records));
    const auto proof = query_index.prove_filter_identity(
        {"Cargo.toml", "src/alpha.rs", "src/beta.rs"},
        "d7a2026249c1545f20827eda5cd4804a10a8deaa8d75576c7435361ec7113377");
    assert(proof.query_surface_sha256 ==
           "d7a2026249c1545f20827eda5cd4804a10a8deaa8d75576c7435361ec7113377");
  }
}

void assert_decoder_registry() {
  auto rb = codenib::core::canonical_scip_decoder_language("rb");
  assert(rb.has_value());
  assert(*rb == "ruby");

  auto js = codenib::core::canonical_scip_decoder_language("js");
  assert(js.has_value());
  assert(*js == "typescript");

  assert(codenib::core::is_scip_decoder_language_supported("python"));
  assert(!codenib::core::is_scip_decoder_language_supported("java"));

  const std::vector<std::string> accepted =
      codenib::core::accepted_scip_decoder_languages();
  assert(accepted.size() == 8);
  assert(accepted.front() == "python");
  assert(accepted.back() == "js");

  try {
    (void)codenib::core::make_scip_decoder("java", "index.decoded",
                                           std::nullopt);
    assert(false && "unknown languages must throw");
  } catch (const std::invalid_argument &error) {
    const std::string message = error.what();
    assert(message.find("python") != std::string::npos);
    assert(message.find("typescript") != std::string::npos);
  }
}

} // namespace

int main(int argc, char **argv) {
  assert_decoder_registry();

  if (argc > 1) {
    std::filesystem::path external_index = argv[1];
    assert(std::filesystem::exists(external_index));

    SCIPGraphDecoder decoder(external_index.string(), std::nullopt);
    CodeGraph graph = decoder.decode();

    const auto root_info = graph.get_node_info_by_name(ROOT_NODE);
    assert(root_info.has_value());
    return 0;
  }

  TempDirectory temp_project;
  assert_parallel_document_order(temp_project.path());
  assert_parallel_rust_document_order(temp_project.path());
  write_python_source(temp_project.path());
  const auto index_path = write_test_index(temp_project.path());

  SCIPGraphDecoder records_decoder(index_path.string(),
                                   temp_project.path().string());
  const auto records = records_decoder.decode_records();
  assert(records_decoder.last_profile().materialize_graph_ns == 0);
  assert(!records.vertices.empty());
  assert(records.vertices.front().name == ROOT_NODE);
  assert(!records.edges.empty());
  const auto first_receipt = records_decoder.last_input_receipt();
  assert(first_receipt.schema_version == 1);
  assert(first_receipt.index.path ==
         std::filesystem::canonical(index_path).generic_string());
  assert(first_receipt.index.size_bytes ==
         std::filesystem::file_size(index_path));
  assert(first_receipt.index.sha256.size() == 64);
  assert(first_receipt.metadata_kind == "none");
  assert(first_receipt.metadata_complete);
  assert(first_receipt.metadata_inputs.empty());
  assert(first_receipt.internal_crates.empty());
  {
    std::ofstream append(index_path, std::ios::binary | std::ios::app);
    append << '\n';
  }
  (void)records_decoder.decode_records();
  const auto second_receipt = records_decoder.last_input_receipt();
  assert(second_receipt.index.size_bytes == first_receipt.index.size_bytes + 1);
  assert(second_receipt.index.sha256 != first_receipt.index.sha256);
  assert(second_receipt.metadata_kind == "none");

  SCIPGraphDecoder decoder(index_path.string(), temp_project.path().string());
  CodeGraph graph = decoder.decode();
  assert(records.vertices.size() == graph.vertices().size());
  assert(records.edges.size() == graph.edges().size());

  CodeGraph invalid_graph;
  invalid_graph.batch_upsert_nodes({records.vertices.front()});
  try {
    invalid_graph.batch_add_indexed_edges(
        {{0, 1, "contain", std::nullopt, std::nullopt}});
    assert(false && "out-of-range indexed edges must throw");
  } catch (const std::out_of_range &) {
  }

  const auto root_info = graph.get_node_info_by_name(ROOT_NODE);
  assert(root_info.has_value());
  assert(root_info->type == "root");

  const auto directory_info = graph.get_node_info_by_name("sample");
  assert(directory_info.has_value());
  assert(directory_info->type == NODE_TYPE_DIRECTORY);

  const auto file_info = graph.get_node_info_by_name("sample/module.py");
  assert(file_info.has_value());
  assert(file_info->type == NODE_TYPE_FILE);
  assert(!file_info->has_definition.has_value());

  const std::string class_symbol = "sample/module.py:SampleClass";
  const auto class_info = graph.get_node_info_by_name(class_symbol);
  assert(class_info.has_value());
  assert(class_info->type == NODE_TYPE_CLASS);
  assert(class_info->has_definition.has_value());
  assert(*class_info->has_definition);

  const std::string method_symbol =
      "sample/module.py:SampleClass.sample_method()";
  const auto method_info = graph.get_node_info_by_name(method_symbol);
  assert(method_info.has_value());
  assert(method_info->type == NODE_TYPE_METHOD);

  const auto method_neighbors = graph.get_neighbors(class_symbol);
  bool found_method_neighbor = false;
  for (const auto neighbor_id : method_neighbors) {
    const auto neighbor_info = graph.get_node_info_by_id(neighbor_id);
    if (neighbor_info.has_value() && neighbor_info->name == method_symbol) {
      found_method_neighbor = true;
      break;
    }
  }
  assert(found_method_neighbor);

  const auto serialized_path = temp_project.path() / "graph.json";
  graph.save_graph(serialized_path.string());
  std::ifstream serialized_file(serialized_path);
  const std::string serialized(
      (std::istreambuf_iterator<char>(serialized_file)),
      std::istreambuf_iterator<char>());
  assert(serialized.find("\"symbol_kind\": null") != std::string::npos);
  assert(serialized.find("\"has_definition\": true") != std::string::npos);

  const auto rust_root = temp_project.path() / "rust-project";
  std::error_code rust_error;
  std::filesystem::create_directories(rust_root / "crates/a", rust_error);
  assert(!rust_error);
  std::filesystem::create_directories(rust_root / "crates/b", rust_error);
  assert(!rust_error);
  write_text_file(rust_root / "Cargo.toml",
                  "[workspace]\nmembers = [\n  \"crates/*\",\n]\n"
                  "[[bench]]\nname = \"workspace-bench\"\n");
  write_text_file(rust_root / "crates/a/Cargo.toml",
                  "[package]\nname = \"alpha\"\n"
                  "[[bench]]\nname = \"alpha-bench\"\n");
  write_text_file(rust_root / "crates/b/Cargo.toml",
                  "[package]\nname = \"beta\"\n"
                  "[[test]]\nname = \"beta-test\"\n");
  codenib::core::SCIPRustDecoder rust_decoder(index_path.string(),
                                              rust_root.string());
  (void)rust_decoder.decode_records();
  const auto rust_receipt = rust_decoder.last_input_receipt();
  assert(rust_receipt.metadata_kind == "rust-cargo");
  assert(rust_receipt.metadata_complete);
  assert(rust_receipt.metadata_inputs.size() == 3);
  assert(rust_receipt.metadata_inputs[0].path == "Cargo.toml");
  assert(rust_receipt.metadata_inputs[1].path == "crates/a/Cargo.toml");
  assert(rust_receipt.metadata_inputs[2].path == "crates/b/Cargo.toml");
  for (const auto &input : rust_receipt.metadata_inputs) {
    assert(input.size_bytes > 0);
    assert(input.sha256.size() == 64);
    assert(input.path.front() != '/');
    assert(input.path.find('\\') == std::string::npos);
    assert(input.path.find("..") == std::string::npos);
  }
  assert((rust_receipt.internal_crates ==
          std::vector<std::string>{"alpha", "beta"}));

  write_text_file(rust_root / "crates/a/Cargo.toml",
                  "[package]\nname = \"gamma\"\n");
  (void)rust_decoder.decode_records();
  const auto changed_rust_receipt = rust_decoder.last_input_receipt();
  assert(changed_rust_receipt.metadata_complete);
  assert((changed_rust_receipt.internal_crates ==
          std::vector<std::string>{"beta", "gamma"}));
  assert(changed_rust_receipt.metadata_inputs[1].sha256 !=
         rust_receipt.metadata_inputs[1].sha256);

  const auto member_cargo = rust_root / "crates/a/Cargo.toml";
  const auto member_cargo_real = rust_root / "crates/a/Cargo.real.toml";
  std::filesystem::rename(member_cargo, member_cargo_real, rust_error);
  assert(!rust_error);
  std::filesystem::create_symlink(member_cargo_real.filename(), member_cargo,
                                  rust_error);
  if (!rust_error) {
    (void)rust_decoder.decode_records();
    assert(!rust_decoder.last_input_receipt().metadata_complete);
    std::filesystem::remove(member_cargo, rust_error);
    assert(!rust_error);
  }
  std::filesystem::rename(member_cargo_real, member_cargo, rust_error);
  assert(!rust_error);

  write_text_file(rust_root / "Cargo.toml",
                  "[workspace]\nmembers = ['crates/a']\n");
  (void)rust_decoder.decode_records();
  assert(!rust_decoder.last_input_receipt().metadata_complete);

  write_text_file(rust_root / "Cargo.toml",
                  std::string{"[package]\nname = \""} + "\xc0\xaf" + "\"\n");
  (void)rust_decoder.decode_records();
  assert(!rust_decoder.last_input_receipt().metadata_complete);

  write_text_file(rust_root / "Cargo.toml",
                  "[\"workspace\"]\nmembers = [\"crates/a\"]\n");
  (void)rust_decoder.decode_records();
  assert(!rust_decoder.last_input_receipt().metadata_complete);

  write_text_file(rust_root / "Cargo.toml",
                  "[workspace]\nmembers = [\"crates/a\"]\n"
                  "[[workspace]]\nmembers = [\"crates/b\"]\n");
  (void)rust_decoder.decode_records();
  assert(!rust_decoder.last_input_receipt().metadata_complete);

  write_text_file(rust_root / "Cargo.toml",
                  "[workspace]\nmembers = [\"crates/a\"]\n"
                  "[[package.metadata]]\nname = \"ambiguous\"\n");
  (void)rust_decoder.decode_records();
  assert(!rust_decoder.last_input_receipt().metadata_complete);

  write_text_file(rust_root / "Cargo.toml",
                  "[workspace]\nmembers = [\"crates/a\"]\n"
                  "[[bench]\nname = \"malformed\"\n");
  (void)rust_decoder.decode_records();
  assert(!rust_decoder.last_input_receipt().metadata_complete);

  write_text_file(rust_root / "Cargo.toml",
                  "workspace.members = [\"crates/a\"]\n");
  (void)rust_decoder.decode_records();
  assert(!rust_decoder.last_input_receipt().metadata_complete);

  write_text_file(rust_root / "Cargo.toml",
                  "[workspace]\nmembers = [\"crates/\\u0061\"]\n");
  (void)rust_decoder.decode_records();
  assert(!rust_decoder.last_input_receipt().metadata_complete);

  // Preserve the established decoder's broad wildcard expansion while the
  // receipt marks anything except a final "*" component unproven.
  write_text_file(rust_root / "Cargo.toml",
                  "[workspace]\nmembers = [\"crates/a*\"]\n");
  (void)rust_decoder.decode_records();
  assert(!rust_decoder.last_input_receipt().metadata_complete);
  assert((rust_decoder.last_input_receipt().internal_crates ==
          std::vector<std::string>{"beta", "gamma"}));

  write_text_file(rust_root / "Cargo.toml",
                  "[workspace]\nmembers = [\"crates/a\", 1]\n");
  (void)rust_decoder.decode_records();
  assert(!rust_decoder.last_input_receipt().metadata_complete);

  write_text_file(rust_root / "Cargo.toml",
                  "[workspace]\nmembers = [\"crates/a\"]\n"
                  "exclude = [\"crates/a\"]\n");
  (void)rust_decoder.decode_records();
  assert(!rust_decoder.last_input_receipt().metadata_complete);

  std::filesystem::create_directories(rust_root / "crates/missing", rust_error);
  assert(!rust_error);
  write_text_file(rust_root / "Cargo.toml",
                  "[workspace]\nmembers = [\"crates/missing\"]\n");
  (void)rust_decoder.decode_records();
  assert(!rust_decoder.last_input_receipt().metadata_complete);
  assert(rust_decoder.last_input_receipt().metadata_inputs.size() == 1);

  const auto root_cargo = rust_root / "Cargo.toml";
  const auto root_cargo_saved = rust_root / "Cargo.saved.toml";
  std::filesystem::rename(root_cargo, root_cargo_saved, rust_error);
  assert(!rust_error);
  (void)rust_decoder.decode_records();
  assert(!rust_decoder.last_input_receipt().metadata_complete);
  assert(rust_decoder.last_input_receipt().metadata_inputs.empty());
  std::filesystem::rename(root_cargo_saved, root_cargo, rust_error);
  assert(!rust_error);

  return 0;
}
