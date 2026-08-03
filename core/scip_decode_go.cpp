// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

#include "scip_decode_go.h"

#include <algorithm>
#include <filesystem>
#include <fstream>
#include <re2/re2.h>

namespace codenib::core {

namespace {

// Strip backtick-module-prefix: `github.com/user/proj/calc`/Sym... -> Sym...
std::string strip_backtick_prefix(const std::string &descriptor) {
  static const re2::RE2 pat(R"re2(`[^`]+`/)re2");
  std::string result = descriptor;
  re2::RE2::Replace(&result, pat, "");
  return result;
}

// Extract content inside first pair of backticks.
std::string first_backtick(const std::string &text) {
  auto a = text.find('`');
  if (a == std::string::npos)
    return {};
  auto b = text.find('`', a + 1);
  if (b == std::string::npos)
    return {};
  return text.substr(a + 1, b - a - 1);
}

} // namespace

void SCIPGoDecoder::load_metadata() {
  if (!project_root_.has_value())
    return;
  std::filesystem::path go_mod =
      std::filesystem::path(*project_root_) / "go.mod";
  if (!std::filesystem::exists(go_mod))
    return;
  std::ifstream in(go_mod);
  if (!in.is_open())
    return;
  std::string line;
  while (std::getline(in, line)) {
    // trim leading whitespace
    std::size_t start = 0;
    while (start < line.size() &&
           std::isspace(static_cast<unsigned char>(line[start])))
      ++start;
    std::string trimmed = line.substr(start);
    if (trimmed.rfind("module ", 0) == 0) {
      std::string value = trimmed.substr(7);
      // trim trailing whitespace
      while (!value.empty() &&
             std::isspace(static_cast<unsigned char>(value.back())))
        value.pop_back();
      internal_module_ = value;
      break;
    }
  }
}

std::string SCIPGoDecoder::make_symbol_key(const std::string &symbol) const {
  auto parts = split_ws(symbol);
  if (parts.size() < 5)
    return {};
  const std::string &descriptor = parts.back();
  std::string full_pkg = first_backtick(descriptor);
  if (full_pkg.empty())
    return {};

  std::string rel_pkg;
  if (!internal_module_.empty() && full_pkg.rfind(internal_module_, 0) == 0) {
    rel_pkg = full_pkg.substr(internal_module_.size());
    while (!rel_pkg.empty() && rel_pkg.front() == '/')
      rel_pkg.erase(rel_pkg.begin());
  }

  std::string symbol_part = strip_backtick_prefix(descriptor);
  if (symbol_part.empty())
    return {};
  symbol_part = rstrip_chars(symbol_part, ".");
  if (ends_with(symbol_part, "()"))
    symbol_part.resize(symbol_part.size() - 2);
  if (ends_with(symbol_part, "#"))
    symbol_part.resize(symbol_part.size() - 1);
  if (symbol_part.empty())
    return {};

  if (!rel_pkg.empty())
    return rel_pkg + "/" + symbol_part;
  return symbol_part;
}

std::string
SCIPGoDecoder::classify_symbol_type(const std::string &original_symbol) {
  auto parts = split_ws(original_symbol);
  std::string last = parts.empty() ? original_symbol : parts.back();
  bool has_hash = last.find('#') != std::string::npos;
  if (has_hash && ends_with(last, "()."))
    return NODE_TYPE_METHOD;
  if (ends_with(last, "#"))
    return NODE_TYPE_CLASS;
  if (has_hash && ends_with(last, "."))
    return NODE_TYPE_FIELD;
  if (ends_with(last, "()."))
    return NODE_TYPE_FUNCTION;
  if (ends_with(last, "."))
    return NODE_TYPE_FIELD;
  return NODE_TYPE_FUNCTION;
}

std::string
SCIPGoDecoder::extract_symbol_display(const std::string &symbol_key,
                                      const std::string &symbol_type) {
  std::string sym = symbol_key;
  auto pos = sym.rfind('/');
  if (pos != std::string::npos)
    sym = sym.substr(pos + 1);
  std::replace(sym.begin(), sym.end(), '#', '.');
  // strip leading/trailing dots
  std::size_t l = 0;
  while (l < sym.size() && sym[l] == '.')
    ++l;
  sym = sym.substr(l);
  while (!sym.empty() && sym.back() == '.')
    sym.pop_back();
  if ((symbol_type == NODE_TYPE_FUNCTION || symbol_type == NODE_TYPE_METHOD) &&
      !ends_with(sym, "()")) {
    sym += "()";
  }
  return sym;
}

std::string SCIPGoDecoder::unified_name(const std::string &symbol_key,
                                        const std::string &file_path,
                                        const std::string &symbol_type) const {
  std::string display = extract_symbol_display(symbol_key, symbol_type);
  if (!file_path.empty() && !display.empty())
    return file_path + ":" + display;
  if (!file_path.empty())
    return file_path;
  if (!display.empty())
    return display;
  return symbol_key;
}

Subgraph
SCIPGoDecoder::process_document(const std::string &document_block) const {
  static const re2::RE2 relative_path_regex(
      R"re2(relative_path:\s*"([^"]+)")re2");
  std::string file_path;
  if (!re2::RE2::PartialMatch(document_block, relative_path_regex,
                              &file_path)) {
    return Subgraph{};
  }
  if (!ends_with(file_path, ".go"))
    return Subgraph{};
  if (ends_with(file_path, "_test.go"))
    return Subgraph{};

  SubgraphBuilder builder;
  builder.add_file_hierarchy(file_path);

  auto occurrences = extract_blocks(document_block, "occurrences");
  for (const auto &occ : occurrences)
    process_occurrence(occ, file_path, builder);
  return builder.build();
}

void SCIPGoDecoder::process_occurrence(const std::string &occurrence_block,
                                       const std::string &file_path,
                                       SubgraphBuilder &builder) const {
  static const re2::RE2 range_re(R"re2(range:\s*(\d+))re2");
  static const re2::RE2 symbol_roles_re(R"re2(symbol_roles:\s*(\d+))re2");
  static const re2::RE2 enclosing_re(R"re2(enclosing_range:\s*(\d+))re2");

  auto ranges = extract_integers(occurrence_block, range_re);
  if (ranges.size() < 3)
    return;
  int line = ranges[0];

  auto symbol_opt = extract_symbol(occurrence_block);
  if (!symbol_opt || symbol_opt->empty())
    return;
  const std::string &symbol = *symbol_opt;
  if (symbol.rfind("local ", 0) == 0)
    return;

  auto parts = split_ws(symbol);
  if (parts.size() >= 5 && parts[0] == "scip-go" && parts[1] == "gomod") {
    const std::string &mod = parts[2];
    if (mod.rfind("github.com/golang/go", 0) == 0)
      return;
    if (!internal_module_.empty() && mod.rfind(internal_module_, 0) != 0)
      return;
  }

  const std::string &descriptor = parts.size() >= 5 ? parts.back() : symbol;
  std::string cleaned = strip_backtick_prefix(descriptor);
  if (cleaned.empty() || cleaned == "/")
    return;

  int symbol_roles = 0;
  re2::RE2::PartialMatch(occurrence_block, symbol_roles_re, &symbol_roles);

  auto enclosing = extract_integers(occurrence_block, enclosing_re);
  process_symbol(symbol, file_path, line, symbol_roles, enclosing, builder);
}

void SCIPGoDecoder::process_symbol(const std::string &symbol,
                                   const std::string &file_path, int line,
                                   int symbol_roles,
                                   const std::vector<int> &encl_ranges,
                                   SubgraphBuilder &builder) const {
  std::string key = make_symbol_key(symbol);
  if (key.empty())
    return;
  std::string type = classify_symbol_type(symbol);

  builder.exit_scopes_by_line(line);

  const bool is_definition = symbol_roles & 1;
  if (is_definition) {
    if (encl_ranges.size() >= 4) {
      int ss = encl_ranges[0];
      int se = encl_ranges[2];
      builder.add_symbol_node(key, line, ss, se, type);
      builder.set_unified_name(key, unified_name(key, file_path, type));
      builder.add_containment_edge(key);
      if (type == NODE_TYPE_CLASS || type == NODE_TYPE_FUNCTION ||
          type == NODE_TYPE_METHOD) {
        builder.update_current_scope(key, ss, se);
      }
    } else if (encl_ranges.size() == 3) {
      int enc_line = encl_ranges[0];
      builder.add_symbol_node(key, line, enc_line, enc_line, type);
      builder.set_unified_name(key, unified_name(key, file_path, type));
      builder.add_containment_edge(key);
    } else {
      builder.add_symbol_node(key, line, std::nullopt, std::nullopt, type);
      builder.set_unified_name(key, unified_name(key, file_path, type));
      builder.add_edge(builder.current_scope(), key, EDGE_TYPE_CONTAIN);
    }
  } else {
    builder.add_symbol_reference(key, file_path, type,
                                 /*anchor_file=*/std::nullopt,
                                 /*anchor_line=*/line);
    builder.set_unified_name(key, unified_name(key, file_path, type),
                             /*only_if_missing=*/true);
  }
}

} // namespace codenib::core
