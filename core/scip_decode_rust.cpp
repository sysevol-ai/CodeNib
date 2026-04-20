#include "scip_decode_rust.h"

#include <algorithm>
#include <cctype>
#include <filesystem>
#include <fstream>
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

std::vector<std::string> split_ws_limit(const std::string &s, int limit) {
  std::vector<std::string> out;
  std::size_t i = 0;
  while (i < s.size() && static_cast<int>(out.size()) < limit - 1) {
    while (i < s.size() && std::isspace(static_cast<unsigned char>(s[i])))
      ++i;
    std::size_t start = i;
    while (i < s.size() && !std::isspace(static_cast<unsigned char>(s[i])))
      ++i;
    if (start < i)
      out.emplace_back(s.substr(start, i - start));
  }
  // rest as single token
  while (i < s.size() && std::isspace(static_cast<unsigned char>(s[i])))
    ++i;
  if (i < s.size())
    out.emplace_back(s.substr(i));
  return out;
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

// Very small TOML-ish parser: pulls "name" from [package] and "members"
// (array of strings) from [workspace]. Works on the subset of Cargo.toml
// files encountered in practice (simple key=value, no inline tables).
struct MinimalCargoToml {
  std::string package_name;
  std::vector<std::string> workspace_members;
};

MinimalCargoToml parse_cargo_toml(const std::filesystem::path &path) {
  MinimalCargoToml result;
  std::ifstream in(path);
  if (!in.is_open())
    return result;
  std::string line;
  std::string section;
  while (std::getline(in, line)) {
    // strip comments
    auto hash = line.find('#');
    if (hash != std::string::npos)
      line = line.substr(0, hash);
    // strip trailing
    while (!line.empty() &&
           (line.back() == '\r' || line.back() == ' ' || line.back() == '\t'))
      line.pop_back();

    std::size_t start = 0;
    while (start < line.size() &&
           std::isspace(static_cast<unsigned char>(line[start])))
      ++start;
    std::string trimmed = line.substr(start);
    if (trimmed.empty())
      continue;

    if (trimmed.front() == '[') {
      auto close = trimmed.find(']');
      if (close != std::string::npos)
        section = trimmed.substr(1, close - 1);
      continue;
    }

    auto eq = trimmed.find('=');
    if (eq == std::string::npos)
      continue;
    std::string key = trimmed.substr(0, eq);
    std::string val = trimmed.substr(eq + 1);
    // strip key/value whitespace
    while (!key.empty() && std::isspace(static_cast<unsigned char>(key.back())))
      key.pop_back();
    std::size_t vs = 0;
    while (vs < val.size() && std::isspace(static_cast<unsigned char>(val[vs])))
      ++vs;
    val = val.substr(vs);

    if (section == "package" && key == "name") {
      if (!val.empty() && val.front() == '"') {
        auto end = val.find('"', 1);
        if (end != std::string::npos)
          result.package_name = val.substr(1, end - 1);
      }
    } else if (section == "workspace" && key == "members") {
      // val starts with '['; collect until ']' (may span multiple lines)
      std::string collected = val;
      while (collected.find(']') == std::string::npos) {
        if (!std::getline(in, line))
          break;
        collected += " " + line;
      }
      // extract quoted strings
      std::size_t p = 0;
      while (p < collected.size()) {
        auto q1 = collected.find('"', p);
        if (q1 == std::string::npos)
          break;
        auto q2 = collected.find('"', q1 + 1);
        if (q2 == std::string::npos)
          break;
        result.workspace_members.emplace_back(
            collected.substr(q1 + 1, q2 - q1 - 1));
        p = q2 + 1;
      }
    }
  }
  return result;
}

// Minimal glob: supports leading path segments + trailing "*" wildcard on the
// final component (e.g. "crates/*"). Returns matching directories.
std::vector<std::filesystem::path>
resolve_glob(const std::filesystem::path &root, const std::string &pattern) {
  std::vector<std::filesystem::path> out;
  auto slash = pattern.rfind('/');
  std::filesystem::path base =
      (slash == std::string::npos) ? root : root / pattern.substr(0, slash);
  std::string leaf =
      (slash == std::string::npos) ? pattern : pattern.substr(slash + 1);

  if (leaf.find('*') == std::string::npos) {
    std::filesystem::path candidate = base / leaf;
    if (std::filesystem::is_directory(candidate))
      out.push_back(candidate);
    return out;
  }

  // Trailing-* support only (good enough for common Cargo.toml members).
  if (!std::filesystem::is_directory(base))
    return out;
  for (const auto &entry : std::filesystem::directory_iterator(base)) {
    if (!entry.is_directory())
      continue;
    out.push_back(entry.path());
  }
  return out;
}

} // namespace

void SCIPRustDecoder::load_metadata() {
  if (!project_root_.has_value())
    return;
  std::filesystem::path root(*project_root_);
  std::filesystem::path cargo_toml = root / "Cargo.toml";
  if (!std::filesystem::exists(cargo_toml))
    return;

  MinimalCargoToml cargo = parse_cargo_toml(cargo_toml);
  if (!cargo.package_name.empty())
    internal_crates_.insert(cargo.package_name);

  if (cargo.workspace_members.empty())
    return;

  for (const auto &pattern : cargo.workspace_members) {
    for (const auto &dir : resolve_glob(root, pattern)) {
      std::filesystem::path member_cargo = dir / "Cargo.toml";
      if (!std::filesystem::exists(member_cargo))
        continue;
      MinimalCargoToml m = parse_cargo_toml(member_cargo);
      if (!m.package_name.empty())
        internal_crates_.insert(m.package_name);
    }
  }
}

std::string SCIPRustDecoder::unify_symbol_name(const std::string &symbol) {
  // Split into at most 5 parts — the symbol_path may contain spaces inside
  // generic angle brackets, so we keep parts[4] intact.
  auto parts = split_ws_limit(symbol, 5);
  if (parts.size() < 5)
    return {};
  std::string crate_prefix;
  if (parts[1] == "cargo")
    crate_prefix = parts[2];
  std::string symbol_part = rstrip_chars(parts[4], ".");
  std::string unified = rstrip_chars(symbol_part, "#/()");
  if (!crate_prefix.empty())
    unified = crate_prefix + "/" + unified;
  return unified;
}

std::string SCIPRustDecoder::classify_symbol_type(const std::string &unified,
                                                  const std::string &original) {
  auto hash = unified.find('#');
  if (hash != std::string::npos) {
    std::string after = unified.substr(hash + 1);
    if (!after.empty()) {
      if (!original.empty() && ends_with(original, "()."))
        return NODE_TYPE_METHOD;
      return NODE_TYPE_FIELD;
    }
    return NODE_TYPE_CLASS;
  }
  auto slash = unified.rfind('/');
  std::string last =
      (slash == std::string::npos) ? unified : unified.substr(slash + 1);
  if (!last.empty()) {
    unsigned char c = static_cast<unsigned char>(last.front());
    if (c >= 'A' && c <= 'Z')
      return NODE_TYPE_CLASS;
  }
  return NODE_TYPE_FUNCTION;
}

std::string
SCIPRustDecoder::extract_impl_display(const std::string &impl_member_in,
                                      const std::string &symbol_type) {
  std::string rest = strip_backticks(impl_member_in);
  std::vector<std::string> brackets;
  while (!rest.empty() && rest.front() == '[') {
    int depth = 0;
    std::size_t end = std::string::npos;
    for (std::size_t i = 0; i < rest.size(); ++i) {
      char ch = rest[i];
      if (ch == '[')
        ++depth;
      else if (ch == ']') {
        --depth;
        if (depth == 0) {
          end = i;
          break;
        }
      }
    }
    if (end == std::string::npos)
      break;
    brackets.push_back(rest.substr(1, end - 1));
    rest = rest.substr(end + 1);
  }
  std::string impl_type = brackets.empty() ? std::string{} : brackets[0];
  std::string trait = brackets.size() > 1 ? brackets[1] : std::string{};
  std::string method = rest;

  // Lifetime-only generics: <'a>, <'a,'b>
  static const re2::RE2 lifetime_generic_all(R"re2(<['\\'a-z_,\s]+>)re2");
  static const re2::RE2 lifetime_generic_trait(R"re2(^<['\\'a-z_,\s]+>$)re2");
  re2::RE2::Replace(&trait, lifetime_generic_trait, "");
  re2::RE2::GlobalReplace(&impl_type, lifetime_generic_all, "");

  std::string type_display =
      trait.empty() ? impl_type : impl_type + "<" + trait + ">";
  if (!method.empty()) {
    std::string suffix = (symbol_type == NODE_TYPE_FIELD) ? "" : "()";
    return type_display + "." + method + suffix;
  }
  return type_display;
}

std::string
SCIPRustDecoder::extract_symbol_display(const std::string &unified,
                                        const std::string &symbol_type) {
  auto hash = unified.find('#');
  if (hash != std::string::npos) {
    std::string type_path = unified.substr(0, hash);
    std::string member = unified.substr(hash + 1);
    auto slash = type_path.rfind('/');
    std::string type_name =
        (slash == std::string::npos) ? type_path : type_path.substr(slash + 1);
    if (type_name == "impl")
      return extract_impl_display(member, symbol_type);

    member = strip_backticks(member);
    static const re2::RE2 lifetime_generic_all(R"re2(<['\\'a-z_,\s]+>)re2");
    re2::RE2::GlobalReplace(&member, lifetime_generic_all, "");
    while (!member.empty() && member.back() == '#')
      member.pop_back();
    if (!member.empty()) {
      std::string suffix = (symbol_type == NODE_TYPE_FIELD) ? "" : "()";
      return type_name + "." + member + suffix;
    }
    return type_name;
  }
  auto slash = unified.rfind('/');
  std::string func_name =
      (slash == std::string::npos) ? unified : unified.substr(slash + 1);
  func_name = strip_backticks(func_name);
  if (symbol_type == NODE_TYPE_FUNCTION || symbol_type == NODE_TYPE_METHOD) {
    return func_name + "()";
  }
  return func_name;
}

std::string
SCIPRustDecoder::unified_name(const std::string &unified,
                              const std::string &file_path,
                              const std::string &symbol_type) const {
  std::string display = extract_symbol_display(unified, symbol_type);
  if (!file_path.empty() && !display.empty())
    return file_path + ":" + display;
  if (!file_path.empty())
    return file_path;
  if (!display.empty())
    return display;
  return unified;
}

Subgraph
SCIPRustDecoder::process_document(const std::string &document_block) const {
  static const re2::RE2 relative_path_regex(
      R"re2(relative_path:\s*"([^"]+)")re2");
  std::string file_path;
  if (!re2::RE2::PartialMatch(document_block, relative_path_regex,
                              &file_path)) {
    return Subgraph{};
  }
  if (!ends_with(file_path, ".rs"))
    return Subgraph{};

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

void SCIPRustDecoder::process_occurrence(const std::string &occurrence_block,
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
  int col_start = ranges[1];
  int col_end = ranges[2];

  std::string symbol;
  if (!re2::RE2::PartialMatch(occurrence_block, symbol_re, &symbol))
    return;
  if (symbol.find("local ") != std::string::npos)
    return;

  // Skip module-only symbols (descriptor ends with "/")
  auto last_sp = symbol.find_last_of(' ');
  std::string last_tok =
      (last_sp == std::string::npos) ? symbol : symbol.substr(last_sp + 1);
  if (!last_tok.empty() && last_tok.back() == '/')
    return;

  // Filter by origin
  auto parts = split_ws_limit(symbol, 5);
  if (parts.size() >= 3) {
    if (parts[1] == "cargo") {
      if (!internal_crates_.empty() && internal_crates_.count(parts[2]) == 0)
        return;
    } else if (parts[1] != "crate") {
      return;
    }
  }

  int symbol_roles = 0;
  re2::RE2::PartialMatch(occurrence_block, symbol_roles_re, &symbol_roles);
  auto enclosing = extract_integers(occurrence_block, enclosing_re);
  process_symbol(symbol, file_path, line, col_start, col_end, symbol_roles,
                 enclosing, builder);
}

void SCIPRustDecoder::process_symbol(const std::string &symbol,
                                     const std::string &file_path, int line,
                                     int /*col_start*/, int /*col_end*/,
                                     int symbol_roles,
                                     const std::vector<int> &encl_ranges,
                                     SubgraphBuilder &builder) const {
  std::string unified = unify_symbol_name(symbol);
  if (unified.empty())
    return;
  std::string type = classify_symbol_type(unified, symbol);

  builder.exit_scopes_by_line(line);

  const bool is_definition = symbol_roles & 1;
  if (is_definition) {
    if (encl_ranges.size() >= 4) {
      int ss = encl_ranges[0];
      int se = encl_ranges[2];
      builder.add_symbol_node(unified, line, ss, se, type);
      builder.set_unified_name(unified, unified_name(unified, file_path, type));
      builder.add_containment_edge(unified);
      if (type == NODE_TYPE_CLASS || type == NODE_TYPE_FUNCTION ||
          type == NODE_TYPE_METHOD) {
        builder.update_current_scope(unified, ss, se);
      }
    } else if (encl_ranges.size() == 3) {
      int enc_line = encl_ranges[0];
      builder.add_symbol_node(unified, line, enc_line, enc_line, type);
      builder.set_unified_name(unified, unified_name(unified, file_path, type));
      builder.add_containment_edge(unified);
    } else {
      builder.add_symbol_node(unified, line, std::nullopt, std::nullopt, type);
      builder.set_unified_name(unified, unified_name(unified, file_path, type));
      builder.add_edge(builder.current_scope(), unified, EDGE_TYPE_CONTAIN);
    }
  } else {
    builder.add_symbol_reference(unified, file_path, type);
    builder.set_unified_name(unified, unified_name(unified, file_path, type),
                             /*only_if_missing=*/true);
  }
}

} // namespace codeminer::core
