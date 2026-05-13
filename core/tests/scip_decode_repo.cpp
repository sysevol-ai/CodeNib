// SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
//
// SPDX-License-Identifier: Apache-2.0

#include "scip_decode.h"

#include <array>
#include <filesystem>
#include <iostream>
#include <memory>
#include <optional>
#include <string>
#include <string_view>

using codeminer::core::CodeGraph;
using codeminer::core::SCIPDecoderBase;
using codeminer::core::SCIPGoDecoder;
using codeminer::core::SCIPPythonDecoder;
using codeminer::core::SCIPRustDecoder;
using codeminer::core::SCIPTSDecoder;
namespace fs = std::filesystem;

namespace {

struct ProgramOptions {
  fs::path index;
  std::optional<fs::path> project_root;
  fs::path output;
  std::string language = "python";
};

constexpr std::array<std::string_view, 4> kNullRootSynonyms = {"null", "NULL",
                                                               "none", "-"};

void print_usage(std::string_view prog_name) {
  std::cerr
      << "Usage: " << prog_name
      << " <index.decoded> <project_root> <output.json> [language]\n\n"
      << "Arguments:\n"
      << "  index.decoded  - Path to decoded SCIP index file\n"
      << "  project_root   - Project root directory (or 'null' for none)\n"
      << "  output.json    - Output JSON file path\n"
      << "  language       - One of: python (default), go, rust, typescript\n";
}

bool path_exists(const fs::path &path, std::string_view description) {
  if (fs::exists(path))
    return true;
  std::cerr << "Error: " << description << " not found: " << path << "\n";
  return false;
}

bool is_null_root(std::string_view value) {
  for (auto syn : kNullRootSynonyms)
    if (value == syn)
      return true;
  return false;
}

std::optional<ProgramOptions> parse_arguments(int argc, char **argv) {
  if (argc != 4 && argc != 5) {
    print_usage(argv[0]);
    return std::nullopt;
  }

  ProgramOptions options{
      .index = argv[1],
      .project_root = std::nullopt,
      .output = argv[3],
  };
  if (argc == 5)
    options.language = argv[4];

  if (!path_exists(options.index, "Index file"))
    return std::nullopt;

  std::string_view project_root_arg = argv[2];
  if (!is_null_root(project_root_arg)) {
    options.project_root = fs::path{project_root_arg};
    if (!path_exists(*options.project_root, "Project root"))
      return std::nullopt;
  }

  const auto &lang = options.language;
  if (lang != "python" && lang != "go" && lang != "rust" &&
      lang != "typescript" && lang != "ts" && lang != "js") {
    std::cerr << "Error: unknown language '" << lang << "'\n";
    print_usage(argv[0]);
    return std::nullopt;
  }
  if (lang == "ts" || lang == "js")
    options.language = "typescript";

  return options;
}

std::unique_ptr<SCIPDecoderBase>
make_decoder(const std::string &lang, const std::string &index,
             const std::optional<fs::path> &root) {
  std::optional<std::string> root_str;
  if (root.has_value())
    root_str = root->string();

  if (lang == "python")
    return std::make_unique<SCIPPythonDecoder>(index, root_str);
  if (lang == "go")
    return std::make_unique<SCIPGoDecoder>(index, root_str);
  if (lang == "rust")
    return std::make_unique<SCIPRustDecoder>(index, root_str);
  if (lang == "typescript")
    return std::make_unique<SCIPTSDecoder>(index, root_str);
  return nullptr;
}

} // namespace

int main(int argc, char **argv) {
  auto options = parse_arguments(argc, argv);
  if (!options)
    return 1;

  std::cout << "========================================\n"
            << "C++ SCIP Decoder (" << options->language << ")\n"
            << "========================================\n"
            << "Index file:   " << options->index << "\n"
            << "Project root: "
            << (options->project_root ? options->project_root->string()
                                      : std::string("(none)"))
            << "\n"
            << "Output file:  " << options->output << "\n\n";

  try {
    auto decoder = make_decoder(options->language, options->index.string(),
                                options->project_root);
    if (!decoder)
      throw std::runtime_error("unknown language: " + options->language);

    std::cout << "Decoding SCIP index...\n";
    CodeGraph graph = decoder->decode();
    std::cout << "\u2713 Decoding completed\n";

    std::cout << "Saving graph to JSON...\n";
    graph.save_graph(options->output.string());
    std::cout << "\u2713 Saved to: " << options->output << "\n";

    std::cout << "\n========================================\n"
              << "SUCCESS\n"
              << "========================================\n";
    return 0;
  } catch (const std::exception &error) {
    std::cerr << "\n========================================\n"
              << "ERROR\n"
              << "========================================\n"
              << error.what() << "\n";
    return 1;
  }
}
