#include "scip_decode_python.h"

#include <algorithm>
#include <filesystem>
#include <re2/re2.h>

namespace codeminer::core {

namespace {

std::string rstrip_periods(std::string value) {
  while (!value.empty() && value.back() == '.')
    value.pop_back();
  return value;
}

bool starts_with_upper(const std::string &value) {
  if (value.empty())
    return false;
  unsigned char c = static_cast<unsigned char>(value.front());
  return c >= 'A' && c <= 'Z';
}

bool contains_function_parentheses(const std::string &symbol) {
  return symbol.find("()") != std::string::npos ||
         symbol.find('(') != std::string::npos;
}

std::vector<int> extract_integers(const std::string &text,
                                  const re2::RE2 &pattern) {
  std::vector<int> results;
  re2::StringPiece input(text);
  int value = 0;
  while (re2::RE2::FindAndConsume(&input, pattern, &value)) {
    results.push_back(value);
  }
  return results;
}

} // namespace

Subgraph
SCIPPythonDecoder::process_document(const std::string &document_block) const {
  static const re2::RE2 relative_path_regex(
      R"re2(relative_path:\s*"([^"]+)")re2");
  std::string file_path;
  if (!re2::RE2::PartialMatch(document_block, relative_path_regex,
                              &file_path)) {
    return Subgraph{};
  }

  std::filesystem::path file_fs_path(file_path);
  SubgraphBuilder builder;

  std::filesystem::path dir_path = file_fs_path.parent_path();
  while (!dir_path.empty() && dir_path != dir_path.parent_path()) {
    std::string dir_str = dir_path.generic_string();
    if (builder.add_directory_if_needed(dir_str)) {
      std::string parent = dir_path.parent_path().generic_string();
      if (parent.empty())
        parent = ROOT_NODE;
      builder.add_edge(parent, dir_str, EDGE_TYPE_CONTAIN);
    }
    dir_path = dir_path.parent_path();
  }

  builder.add_file_node(file_path);
  std::string parent = file_fs_path.parent_path().generic_string();
  if (parent.empty())
    parent = ROOT_NODE;
  builder.add_edge(parent, file_path, EDGE_TYPE_CONTAIN);

  auto occurrences = extract_blocks(document_block, "occurrences");
  for (const auto &occ : occurrences)
    process_occurrence(occ, builder);

  return builder.build();
}

void SCIPPythonDecoder::postprocess() {
  // Mirror Python _fix_unified_names post-pass:
  //   - file / directory → unified_name = name
  //   - symbol with ':' in name and a file attribute →
  //     unified_name = `{file}:{symbol_part_after_colon}`
  for (auto &v : code_graph_.mutable_vertices()) {
    if (v.type == NODE_TYPE_FILE || v.type == NODE_TYPE_DIRECTORY) {
      v.unified_name = v.name;
      continue;
    }
    auto colon = v.name.find(':');
    if (colon != std::string::npos && v.file.has_value() && !v.file->empty()) {
      v.unified_name = *v.file + ":" + v.name.substr(colon + 1);
    }
  }
}

void SCIPPythonDecoder::process_occurrence(const std::string &occurrence_block,
                                           SubgraphBuilder &builder) const {
  if (occurrence_block.find("python-stdlib") != std::string::npos)
    return;

  static const re2::RE2 range_re(R"re2(range:\s*(\d+))re2");
  static const re2::RE2 symbol_re(R"re2(symbol:\s*"([^"]+)")re2");
  static const re2::RE2 symbol_roles_re(R"re2(symbol_roles:\s*(\d+))re2");
  static const re2::RE2 enclosing_re(R"re2(enclosing_range:\s*(\d+))re2");

  auto ranges = extract_integers(occurrence_block, range_re);
  if (ranges.size() < 3)
    return;
  int line = ranges[0];

  std::string symbol;
  if (!re2::RE2::PartialMatch(occurrence_block, symbol_re, &symbol))
    return;
  if (symbol.rfind("local ", 0) == 0)
    return;

  int symbol_roles = 0;
  if (!re2::RE2::PartialMatch(occurrence_block, symbol_roles_re, &symbol_roles))
    return;

  auto enclosing = extract_integers(occurrence_block, enclosing_re);
  process_symbol(symbol, line, symbol_roles, enclosing, builder);
}

std::string
SCIPPythonDecoder::unify_symbol_name(const std::string &symbol) const {
  std::string clean = symbol;
  clean.erase(std::remove(clean.begin(), clean.end(), '`'), clean.end());

  auto slash = clean.find('/');
  if (slash == std::string::npos)
    return rstrip_periods(clean);

  std::string module = clean.substr(0, slash);
  std::replace(module.begin(), module.end(), '.', '/');
  module += ".py";

  std::string symbol_part;
  if (slash + 1 < clean.size())
    symbol_part = clean.substr(slash + 1);
  if (symbol_part.empty())
    return module;

  auto hash = symbol_part.find('#');
  if (hash != std::string::npos) {
    std::string cls = symbol_part.substr(0, hash);
    std::string rest = symbol_part.substr(hash + 1);
    rest = rstrip_periods(rest);
    if (!rest.empty())
      return module + ":" + cls + "." + rest;
    return module + ":" + cls;
  }
  return module + ":" + rstrip_periods(symbol_part);
}

std::string
SCIPPythonDecoder::classify_symbol_type(const std::string &unified,
                                        const std::string &original) const {
  auto colon = unified.find(':');
  if (colon != std::string::npos) {
    std::string sym_part = unified.substr(colon + 1);
    if (sym_part.find('.') != std::string::npos) {
      return contains_function_parentheses(original) ? NODE_TYPE_METHOD
                                                     : NODE_TYPE_FIELD;
    }
    if (starts_with_upper(sym_part))
      return NODE_TYPE_CLASS;
    return NODE_TYPE_FUNCTION;
  }
  return NODE_TYPE_FUNCTION;
}

void SCIPPythonDecoder::process_symbol(const std::string &symbol, int line,
                                       int symbol_roles,
                                       const std::vector<int> &enclosing_ranges,
                                       SubgraphBuilder &builder) const {
  static const re2::RE2 arg_re(R"re2(\.\([^)]+\)$)re2");
  if (re2::RE2::PartialMatch(symbol, arg_re))
    return;

  builder.exit_scopes_by_line(line);

  static const re2::RE2 module_re(R"re2(`?([^`]+)`?/[^.]+(?:\.|\(|#))re2");
  std::string module_path;
  if (!re2::RE2::PartialMatch(symbol, module_re, &module_path))
    return;

  std::string cleaned = symbol;
  if (auto sp = cleaned.find_last_of(' '); sp != std::string::npos) {
    cleaned = cleaned.substr(sp + 1);
  }
  cleaned.erase(std::remove(cleaned.begin(), cleaned.end(), '`'),
                cleaned.end());

  std::string unified = unify_symbol_name(cleaned);
  std::string symbol_type = classify_symbol_type(unified, cleaned);

  const bool is_definition = symbol_roles == 1;
  const bool is_reference = symbol_roles == 8;

  if (unified.find("/__init__") != std::string::npos) {
    re2::RE2 init_re(R"re2((.+)/(?:__init__))re2");
    std::string module_dir;
    if (re2::RE2::PartialMatch(unified, init_re, &module_dir)) {
      std::replace(module_dir.begin(), module_dir.end(), '.', '/');
      std::string file = module_dir + ".py";
      if (is_reference) {
        builder.add_edge(builder.current_scope(), file, EDGE_TYPE_REFERENCE);
      }
    }
    return;
  }

  if (is_definition && enclosing_ranges.size() >= 4) {
    int scope_start = enclosing_ranges[0];
    int scope_end = enclosing_ranges[2];
    builder.add_symbol_node(unified, line, scope_start, scope_end, symbol_type);
    builder.add_containment_edge(unified);
    if (symbol_type == NODE_TYPE_CLASS || symbol_type == NODE_TYPE_FUNCTION ||
        symbol_type == NODE_TYPE_METHOD) {
      builder.update_current_scope(unified, scope_start, scope_end);
    }
  } else if (is_definition) {
    builder.add_symbol_node(unified, line, std::nullopt, std::nullopt,
                            symbol_type);
    builder.add_edge(builder.current_scope(), unified, EDGE_TYPE_CONTAIN);
  } else if (is_reference) {
    builder.add_symbol_reference(unified, module_path, symbol_type);
  }
}

} // namespace codeminer::core
