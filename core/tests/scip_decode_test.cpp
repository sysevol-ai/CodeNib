#include "scip_decode.h"

#include <cassert>
#include <chrono>
#include <filesystem>
#include <fstream>
#include <optional>
#include <string>
#include <system_error>

using codeminer::core::CodeGraph;
using codeminer::core::NODE_TYPE_CLASS;
using codeminer::core::NODE_TYPE_DIRECTORY;
using codeminer::core::NODE_TYPE_FILE;
using codeminer::core::NODE_TYPE_METHOD;
using codeminer::core::ROOT_NODE;
using codeminer::core::SCIPGraphDecoder;

namespace {

class TempDirectory {
public:
  TempDirectory() {
    auto base = std::filesystem::temp_directory_path();
    auto timestamp =
        std::chrono::steady_clock::now().time_since_epoch().count();
    path_ = base / ("codeminer_scip_decoder_test-" + std::to_string(timestamp));
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

} // namespace

int main(int argc, char **argv) {
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
  write_python_source(temp_project.path());
  const auto index_path = write_test_index(temp_project.path());

  SCIPGraphDecoder decoder(index_path.string(), temp_project.path().string());
  CodeGraph graph = decoder.decode();

  const auto root_info = graph.get_node_info_by_name(ROOT_NODE);
  assert(root_info.has_value());
  assert(root_info->type == "root");

  const auto directory_info = graph.get_node_info_by_name("sample");
  assert(directory_info.has_value());
  assert(directory_info->type == NODE_TYPE_DIRECTORY);

  const auto file_info = graph.get_node_info_by_name("sample/module.py");
  assert(file_info.has_value());
  assert(file_info->type == NODE_TYPE_FILE);

  const std::string class_symbol = "sample/module.py:SampleClass";
  const auto class_info = graph.get_node_info_by_name(class_symbol);
  assert(class_info.has_value());
  assert(class_info->type == NODE_TYPE_CLASS);

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

  return 0;
}
