#include "scip_decode_ts.h"

#include <algorithm>
#include <cctype>
#include <filesystem>
#include <re2/re2.h>
#include <sstream>

namespace codeminer::core {

namespace {

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

bool ends_with(const std::string &s, const std::string &suffix) {
  return s.size() >= suffix.size() &&
         s.compare(s.size() - suffix.size(), suffix.size(), suffix) == 0;
}

std::string rstrip_chars(std::string s, const std::string &chars) {
  while (!s.empty() && chars.find(s.back()) != std::string::npos)
    s.pop_back();
  return s;
}

std::string strip_backticks(std::string s) {
  s.erase(std::remove(s.begin(), s.end(), '`'), s.end());
  return s;
}

std::vector<std::string> split_ws(const std::string &s) {
  std::vector<std::string> out;
  std::istringstream iss(s);
  std::string tok;
  while (iss >> tok)
    out.push_back(tok);
  return out;
}

std::vector<std::string> all_backtick_segments(const std::string &text) {
  std::vector<std::string> out;
  std::size_t p = 0;
  while (p < text.size()) {
    auto a = text.find('`', p);
    if (a == std::string::npos)
      break;
    auto b = text.find('`', a + 1);
    if (b == std::string::npos)
      break;
    out.emplace_back(text.substr(a + 1, b - a - 1));
    p = b + 1;
  }
  return out;
}

bool is_stdlib_symbol(const std::string &symbol) {
  static const char *const needles[] = {" typescript ", "node_modules/",
                                        "lib.es", "lib.dom"};
  for (const char *n : needles) {
    if (symbol.find(n) != std::string::npos)
      return true;
  }
  return false;
}

} // namespace

void SCIPTSDecoder::prescan(const std::vector<std::string> &document_blocks) {
  // Collect pkg_name from all DEFINITIONS with scip-typescript prefix so that
  // references can be filtered consistently across parallel workers.
  static const re2::RE2 symbol_re(R"re2(symbol:\s*"([^"]+)")re2");
  static const re2::RE2 symbol_roles_re(R"re2(symbol_roles:\s*(\d+))re2");

  for (const auto &doc : document_blocks) {
    auto occurrences = extract_blocks(doc, "occurrences");
    for (const auto &occ : occurrences) {
      std::string symbol;
      if (!re2::RE2::PartialMatch(occ, symbol_re, &symbol))
        continue;
      if (symbol.rfind("scip-typescript ", 0) != 0)
        continue;
      auto parts = split_ws(symbol);
      if (parts.size() < 5)
        continue;
      if (parts[2] == "." || parts[3] == ".")
        continue;
      int roles = 0;
      if (!re2::RE2::PartialMatch(occ, symbol_roles_re, &roles))
        continue;
      if (roles & 1)
        project_packages_.insert(parts[2]);
    }
  }
}

std::string SCIPTSDecoder::extract_symbol_display(const std::string &unified) {
  std::string sym = unified;
  // Strip `pkg@ver:` prefix
  static const re2::RE2 pkg_prefix(R"re2(^[^:]+@[^:]+:)re2");
  re2::RE2::Replace(&sym, pkg_prefix, "");
  // Take part after first colon (if any)
  auto colon = sym.find(':');
  std::string symbol_part =
      (colon == std::string::npos) ? sym : sym.substr(colon + 1);

  // Clean trailing numeric suffix on identifier-like endings (e.g.
  // `__BROWSER__0`)
  static const re2::RE2 num_suffix(R"re2((\D)\d+$)re2");
  re2::RE2::Replace(&symbol_part, num_suffix, "\\1");
  while (!symbol_part.empty() && symbol_part.back() == ':')
    symbol_part.pop_back();

  // Accessor wrappers
  static const re2::RE2 get_wrap(R"re2(<get>(\w+))re2");
  static const re2::RE2 set_wrap(R"re2(<set>(\w+))re2");
  // Replace <constructor> → constructor
  std::string tmp = symbol_part;
  static const std::string ctor_placeholder = "<constructor>";
  std::size_t pos = 0;
  while ((pos = tmp.find(ctor_placeholder, pos)) != std::string::npos) {
    tmp.replace(pos, ctor_placeholder.size(), "constructor");
    pos += std::string("constructor").size();
  }
  re2::RE2::GlobalReplace(&tmp, get_wrap, "\\1");
  re2::RE2::GlobalReplace(&tmp, set_wrap, "\\1");

  // Strip typeLiteral intermediate layers: `Type.typeLiteral14:field` →
  // `Type.field`
  static const re2::RE2 type_literal(R"re2(\.?typeLiteral\d*:)re2");
  re2::RE2::GlobalReplace(&tmp, type_literal, ".");
  // strip leading/trailing dots
  std::size_t l = 0;
  while (l < tmp.size() && tmp[l] == '.')
    ++l;
  tmp = tmp.substr(l);
  while (!tmp.empty() && tmp.back() == '.')
    tmp.pop_back();
  return tmp;
}

std::string SCIPTSDecoder::unify_symbol_name(const std::string &cleaned_symbol,
                                             const std::string &original_symbol,
                                             const std::string &file_path) {
  std::string package_prefix;
  auto parts = split_ws(original_symbol);
  std::string sym_with_bt;
  if (parts.size() >= 5 && parts[0] == "scip-typescript") {
    if (parts[2] != "." && parts[3] != ".") {
      package_prefix = parts[2] + "@" + parts[3];
    }
    std::ostringstream oss;
    for (std::size_t i = 4; i < parts.size(); ++i) {
      if (i > 4)
        oss << ' ';
      oss << parts[i];
    }
    sym_with_bt = oss.str();
  } else {
    auto last_sp = original_symbol.find_last_of(' ');
    sym_with_bt = (last_sp == std::string::npos)
                      ? original_symbol
                      : original_symbol.substr(last_sp + 1);
  }

  std::string module_path;
  std::string descriptor;
  auto bt_segments = all_backtick_segments(sym_with_bt);
  if (!bt_segments.empty()) {
    if (bt_segments.size() == 1) {
      module_path = bt_segments[0];
    } else {
      // First segment might use dots for slashes
      static const re2::RE2 ext_strip(R"re2(\.(ts|tsx|js|jsx)$)re2");
      std::string first = bt_segments[0];
      re2::RE2::Replace(&first, ext_strip, "");
      std::replace(first.begin(), first.end(), '.', '/');
      std::ostringstream oss;
      oss << first;
      for (std::size_t i = 1; i < bt_segments.size(); ++i)
        oss << '/' << bt_segments[i];
      module_path = oss.str();
    }
    auto last_bt = sym_with_bt.rfind('`');
    std::string tail = sym_with_bt.substr(last_bt + 1);
    while (!tail.empty() && tail.front() == '/')
      tail.erase(tail.begin());
    descriptor = tail;
  } else {
    module_path = file_path.empty() ? std::string("unknown") : file_path;
    descriptor = strip_backticks(cleaned_symbol);
  }
  // trim trailing whitespace from descriptor
  while (!descriptor.empty() &&
         std::isspace(static_cast<unsigned char>(descriptor.back()))) {
    descriptor.pop_back();
  }

  std::string symbol_id;
  if (!descriptor.empty()) {
    auto hash = descriptor.find('#');
    if (hash != std::string::npos) {
      std::string cls = descriptor.substr(0, hash);
      std::string method = descriptor.substr(hash + 1);
      if (!method.empty()) {
        method = rstrip_chars(method, ".");
        symbol_id = module_path + ":" + cls + "." + method;
      } else {
        symbol_id = module_path + ":" + cls;
      }
    } else {
      symbol_id = module_path + ":" + rstrip_chars(descriptor, ".");
    }
  } else {
    symbol_id = module_path;
  }

  if (!package_prefix.empty())
    return package_prefix + ":" + symbol_id;
  return symbol_id;
}

std::string SCIPTSDecoder::classify_symbol_type(const std::string & /*unified*/,
                                                const std::string &original) {
  if (original.empty())
    return NODE_TYPE_FIELD;
  std::string sym = original;
  // Strip trailing whitespace
  while (!sym.empty() && std::isspace(static_cast<unsigned char>(sym.back())))
    sym.pop_back();

  if (ends_with(sym, "().")) {
    std::string base = sym.substr(0, sym.size() - 3);
    auto slash = base.rfind('/');
    auto hash = base.rfind('#');
    std::size_t last_sep = std::string::npos;
    if (slash != std::string::npos)
      last_sep = slash;
    if (hash != std::string::npos &&
        (last_sep == std::string::npos || hash > last_sep)) {
      last_sep = hash;
    }
    if (last_sep != std::string::npos && base[last_sep] == '#')
      return NODE_TYPE_METHOD;
    return NODE_TYPE_FUNCTION;
  }
  if (ends_with(sym, "#"))
    return NODE_TYPE_CLASS;
  if (ends_with(sym, "."))
    return NODE_TYPE_FIELD;
  return NODE_TYPE_FIELD;
}

std::string SCIPTSDecoder::unified_name(const std::string &unified,
                                        const std::string &file_path,
                                        const std::string &symbol_type) const {
  std::string display = extract_symbol_display(unified);
  if ((symbol_type == NODE_TYPE_METHOD || symbol_type == NODE_TYPE_FUNCTION) &&
      !ends_with(display, "()")) {
    display += "()";
  }
  if (!file_path.empty() && !display.empty())
    return file_path + ":" + display;
  if (!file_path.empty())
    return file_path;
  if (!display.empty())
    return display;
  return unified;
}

Subgraph
SCIPTSDecoder::process_document(const std::string &document_block) const {
  static const re2::RE2 relative_path_regex(
      R"re2(relative_path:\s*"([^"]+)")re2");
  std::string file_path;
  if (!re2::RE2::PartialMatch(document_block, relative_path_regex,
                              &file_path)) {
    return Subgraph{};
  }

  std::filesystem::path file_fs(file_path);
  SubgraphBuilder builder;
  auto dir = file_fs.parent_path();
  while (!dir.empty() && dir != dir.parent_path()) {
    std::string dir_str = dir.generic_string();
    if (builder.add_directory_if_needed(dir_str)) {
      std::string parent = dir.parent_path().generic_string();
      if (parent.empty())
        parent = ROOT_NODE;
      builder.add_edge(parent, dir_str, EDGE_TYPE_CONTAIN);
    }
    dir = dir.parent_path();
  }
  builder.add_file_node(file_path);
  std::string parent = file_fs.parent_path().generic_string();
  if (parent.empty())
    parent = ROOT_NODE;
  builder.add_edge(parent, file_path, EDGE_TYPE_CONTAIN);

  auto occurrences = extract_blocks(document_block, "occurrences");
  for (const auto &occ : occurrences)
    process_occurrence(occ, file_path, builder);
  return builder.build();
}

void SCIPTSDecoder::process_occurrence(const std::string &occurrence_block,
                                       const std::string &file_path,
                                       SubgraphBuilder &builder) const {
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
  if (is_stdlib_symbol(symbol))
    return;
  if (symbol.rfind("local ", 0) == 0)
    return;

  int symbol_roles = 0;
  re2::RE2::PartialMatch(occurrence_block, symbol_roles_re, &symbol_roles);

  auto parts = split_ws(symbol);
  if (parts.size() >= 5 && parts[0] == "scip-typescript") {
    const std::string &pkg_name = parts[2];
    if (!(symbol_roles & 1)) {
      if (pkg_name.rfind("@types/", 0) == 0)
        return;
      if (!project_packages_.empty() && pkg_name != "." &&
          project_packages_.count(pkg_name) == 0) {
        return;
      }
    }
  }

  auto enclosing = extract_integers(occurrence_block, enclosing_re);
  process_symbol(symbol, line, symbol_roles, enclosing, file_path, builder);
}

void SCIPTSDecoder::process_symbol(const std::string &symbol, int line,
                                   int symbol_roles,
                                   const std::vector<int> &encl_ranges,
                                   const std::string &file_path,
                                   SubgraphBuilder &builder) const {
  static const re2::RE2 arg_re(R"re2(\.\([^)]+\)$)re2");
  if (re2::RE2::PartialMatch(symbol, arg_re))
    return;

  builder.exit_scopes_by_line(line);

  auto last_sp = symbol.find_last_of(' ');
  std::string cleaned =
      (last_sp == std::string::npos) ? symbol : symbol.substr(last_sp + 1);
  cleaned = strip_backticks(cleaned);

  static const re2::RE2 module_re(R"re2(`?([^`]+)`?/[^.]+(?:\.|\(|#))re2");
  if (!re2::RE2::PartialMatch(cleaned, module_re))
    return;

  std::string unified = unify_symbol_name(cleaned, symbol, file_path);

  // Filter generic type parameters like .[T], .[D]
  static const re2::RE2 generic_type_re(R"re2(\.\[[A-Z]\w*\])re2");
  if (re2::RE2::PartialMatch(unified, generic_type_re))
    return;

  // Filter bare file-path exports (post-pkg part matches file extension)
  static const re2::RE2 module_export_re(R"re2(^[^:]+\.(js|ts|jsx|tsx)$)re2");
  std::string sym_after_pkg;
  auto colon = unified.find(':');
  sym_after_pkg =
      (colon == std::string::npos) ? unified : unified.substr(colon + 1);
  if (re2::RE2::FullMatch(sym_after_pkg, module_export_re))
    return;

  std::string type = classify_symbol_type(unified, cleaned);

  // Index file refs collapse to a file-level ref edge (no symbol node).
  bool is_index_file =
      !file_path.empty() && file_path.find("/index.") != std::string::npos;
  bool is_simple_symbol = unified.find('#') == std::string::npos;
  if (is_index_file && is_simple_symbol && !(symbol_roles & 1)) {
    if (builder.current_scope() != file_path) {
      builder.add_edge(builder.current_scope(), file_path, EDGE_TYPE_REFERENCE);
    }
    return;
  }

  bool is_definition = symbol_roles & 1;
  if (is_definition && encl_ranges.size() >= 4) {
    int ss = encl_ranges[0];
    int se = encl_ranges[2];
    builder.add_symbol_node(unified, line, ss, se, type);
    builder.set_unified_name(unified, unified_name(unified, file_path, type));
    builder.add_containment_edge(unified);
    if (type == NODE_TYPE_CLASS || type == NODE_TYPE_FUNCTION ||
        type == NODE_TYPE_METHOD) {
      builder.update_current_scope(unified, ss, se);
    }
  } else if (is_definition) {
    builder.add_symbol_node(unified, line, std::nullopt, std::nullopt, type);
    builder.set_unified_name(unified, unified_name(unified, file_path, type));
    builder.add_edge(builder.current_scope(), unified, EDGE_TYPE_CONTAIN);
  } else {
    builder.add_symbol_reference(unified, file_path, type);
    builder.set_unified_name(unified, unified_name(unified, file_path, type),
                             /*only_if_missing=*/true);
  }
}

} // namespace codeminer::core
