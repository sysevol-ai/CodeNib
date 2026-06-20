// SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
//
// SPDX-License-Identifier: Apache-2.0

#include "scip_decode.h"

#include <array>
#include <filesystem>
#include <iostream>
#include <optional>
#include <string>
#include <string_view>

using codeminer::core::CodeGraph;
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
      << "  language       - One of: "
      << codeminer::core::accepted_scip_decoder_languages_help()
      << " (default: python)\n";
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

  auto canonical =
      codeminer::core::canonical_scip_decoder_language(options.language);
  if (!canonical) {
    std::cerr << "Error: unknown language '" << options.language << "'\n";
    print_usage(argv[0]);
    return std::nullopt;
  }
  options.language = *canonical;

  return options;
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
    std::optional<std::string> root_str;
    if (options->project_root.has_value())
      root_str = options->project_root->string();
    auto decoder = codeminer::core::make_scip_decoder(
        options->language, options->index.string(), root_str);

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
