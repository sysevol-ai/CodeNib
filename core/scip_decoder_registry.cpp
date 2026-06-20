// SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
//
// SPDX-License-Identifier: Apache-2.0

#include "scip_decoder_registry.h"

#include "scip_decode_go.h"
#include "scip_decode_python.h"
#include "scip_decode_ruby.h"
#include "scip_decode_rust.h"
#include "scip_decode_ts.h"

#include <stdexcept>
#include <utility>

namespace codeminer::core {

namespace {

std::string join_languages(const std::vector<std::string> &languages) {
  std::string result;
  for (const auto &language : languages) {
    if (!result.empty())
      result += ", ";
    result += language;
  }
  return result;
}

} // namespace

std::vector<std::string> canonical_scip_decoder_languages() {
  return {"python", "go", "rust", "ruby", "typescript"};
}

std::vector<std::string> accepted_scip_decoder_languages() {
  return {"python", "go", "rust", "ruby", "rb", "typescript", "ts", "js"};
}

std::string accepted_scip_decoder_languages_help() {
  return join_languages(accepted_scip_decoder_languages());
}

std::optional<std::string>
canonical_scip_decoder_language(std::string_view language) {
  if (language == "python" || language == "go" || language == "rust" ||
      language == "ruby" || language == "typescript") {
    return std::string(language);
  }
  if (language == "rb")
    return std::string("ruby");
  if (language == "ts" || language == "js")
    return std::string("typescript");
  return std::nullopt;
}

bool is_scip_decoder_language_supported(std::string_view language) {
  return canonical_scip_decoder_language(language).has_value();
}

std::unique_ptr<SCIPDecoderBase>
make_scip_decoder(std::string_view language, const std::string &index_file,
                  std::optional<std::string> project_root) {
  auto canonical = canonical_scip_decoder_language(language);
  if (!canonical) {
    throw std::invalid_argument(
        "Unknown language: " + std::string(language) +
        " (expected one of: " + accepted_scip_decoder_languages_help() + ")");
  }

  if (*canonical == "python")
    return std::make_unique<SCIPPythonDecoder>(index_file,
                                               std::move(project_root));
  if (*canonical == "go")
    return std::make_unique<SCIPGoDecoder>(index_file, std::move(project_root));
  if (*canonical == "rust")
    return std::make_unique<SCIPRustDecoder>(index_file,
                                             std::move(project_root));
  if (*canonical == "ruby")
    return std::make_unique<SCIPRubyDecoder>(index_file,
                                             std::move(project_root));
  if (*canonical == "typescript")
    return std::make_unique<SCIPTSDecoder>(index_file, std::move(project_root));

  throw std::logic_error("Unhandled canonical SCIP decoder language: " +
                         *canonical);
}

} // namespace codeminer::core
