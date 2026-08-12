// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

#include "scip_decode_rust.h"

#include "content_digest.h"

#include <algorithm>
#include <cctype>
#include <filesystem>
#include <fstream>
#include <re2/re2.h>
#include <sstream>
#include <system_error>

namespace codenib::core {

namespace {

// Very small TOML-ish parser: pulls "name" from [package] and "members"
// (array of strings) from [workspace]. Works on the subset of Cargo.toml
// files encountered in practice (simple key=value, no inline tables).
struct MinimalCargoToml {
  std::string package_name;
  std::vector<std::string> workspace_members;
  bool complete{true};
  bool saw_package_table{false};
  bool saw_workspace_table{false};
  bool saw_workspace_members{false};
};

bool valid_utf8(const std::string &value) {
  const auto *bytes = reinterpret_cast<const unsigned char *>(value.data());
  std::size_t index = 0;
  while (index < value.size()) {
    const auto lead = bytes[index];
    if (lead <= 0x7fU) {
      ++index;
      continue;
    }
    std::size_t width = 0;
    std::uint32_t code_point = 0;
    if (lead >= 0xc2U && lead <= 0xdfU) {
      width = 2;
      code_point = lead & 0x1fU;
    } else if (lead >= 0xe0U && lead <= 0xefU) {
      width = 3;
      code_point = lead & 0x0fU;
    } else if (lead >= 0xf0U && lead <= 0xf4U) {
      width = 4;
      code_point = lead & 0x07U;
    } else {
      return false;
    }
    if (index + width > value.size())
      return false;
    for (std::size_t offset = 1; offset < width; ++offset) {
      const auto continuation = bytes[index + offset];
      if ((continuation & 0xc0U) != 0x80U)
        return false;
      code_point = (code_point << 6U) | (continuation & 0x3fU);
    }
    const bool overlong = (width == 2 && code_point < 0x80U) ||
                          (width == 3 && code_point < 0x800U) ||
                          (width == 4 && code_point < 0x10000U);
    if (overlong || (code_point >= 0xd800U && code_point <= 0xdfffU) ||
        code_point > 0x10ffffU) {
      return false;
    }
    index += width;
  }
  return true;
}

bool valid_basic_string_contents(const std::string &value) {
  if (value.empty() || !valid_utf8(value) ||
      value.find('\\') != std::string::npos ||
      value.find('\0') != std::string::npos) {
    return false;
  }
  return std::none_of(value.begin(), value.end(), [](char character) {
    const auto byte = static_cast<unsigned char>(character);
    return byte <= 0x1fU || byte == 0x7fU;
  });
}

std::string trim(std::string value) {
  auto whitespace = [](unsigned char character) {
    return std::isspace(character) != 0;
  };
  value.erase(value.begin(),
              std::find_if_not(value.begin(), value.end(), whitespace));
  value.erase(std::find_if_not(value.rbegin(), value.rend(), whitespace).base(),
              value.end());
  return value;
}

std::string strip_comment(std::string value) {
  bool quoted = false;
  for (std::size_t index = 0; index < value.size(); ++index) {
    if (value[index] == '\\') {
      ++index;
      continue;
    }
    if (value[index] == '"')
      quoted = !quoted;
    if (value[index] == '#' && !quoted) {
      value.resize(index);
      break;
    }
  }
  return value;
}

bool proven_unrelated_array_table(const std::string &raw_section) {
  const auto section = trim(raw_section);
  if (section.empty())
    return false;

  std::size_t start = 0;
  while (start <= section.size()) {
    const auto dot = section.find('.', start);
    const auto end = dot == std::string::npos ? section.size() : dot;
    const auto component = section.substr(start, end - start);
    if (component.empty() || component == "package" ||
        component == "workspace" ||
        !std::all_of(component.begin(), component.end(), [](char character) {
          const auto byte = static_cast<unsigned char>(character);
          return std::isalnum(byte) != 0 || character == '_' ||
                 character == '-';
        })) {
      return false;
    }
    if (dot == std::string::npos)
      break;
    start = dot + 1;
  }
  return true;
}

bool parse_basic_string(const std::string &raw, std::string &output) {
  const auto value = trim(raw);
  if (value.size() < 2 || value.front() != '"')
    return false;
  const auto close = value.find('"', 1);
  if (close == std::string::npos || !trim(value.substr(close + 1)).empty())
    return false;
  output = value.substr(1, close - 1);
  return valid_basic_string_contents(output);
}

bool parse_basic_string_array(const std::string &raw,
                              std::vector<std::string> &output) {
  const auto value = trim(raw);
  if (value.size() < 2 || value.front() != '[')
    return false;
  std::size_t cursor = 1;
  while (true) {
    while (cursor < value.size() &&
           std::isspace(static_cast<unsigned char>(value[cursor])))
      ++cursor;
    if (cursor >= value.size())
      return false;
    if (value[cursor] == ']') {
      ++cursor;
      return trim(value.substr(cursor)).empty();
    }
    if (value[cursor] != '"')
      return false;
    const auto close = value.find('"', cursor + 1);
    if (close == std::string::npos)
      return false;
    std::string item = value.substr(cursor + 1, close - cursor - 1);
    if (!valid_basic_string_contents(item)) {
      return false;
    }
    output.push_back(std::move(item));
    cursor = close + 1;
    while (cursor < value.size() &&
           std::isspace(static_cast<unsigned char>(value[cursor])))
      ++cursor;
    if (cursor >= value.size())
      return false;
    if (value[cursor] == ']') {
      ++cursor;
      return trim(value.substr(cursor)).empty();
    }
    if (value[cursor] != ',')
      return false;
    ++cursor;
  }
}

MinimalCargoToml parse_cargo_toml(const std::string &content) {
  MinimalCargoToml result;
  result.complete = valid_utf8(content);
  std::istringstream in(content);
  std::string line;
  std::string section;
  while (std::getline(in, line)) {
    std::string trimmed = trim(strip_comment(line));
    if (trimmed.empty())
      continue;

    if (trimmed.rfind("[[", 0) == 0) {
      if (trimmed.size() < 5 ||
          trimmed.compare(trimmed.size() - 2, 2, "]]") != 0) {
        result.complete = false;
      } else {
        const auto array_section = trimmed.substr(2, trimmed.size() - 4);
        if (!proven_unrelated_array_table(array_section))
          result.complete = false;
      }
      // Keys in a proven-unrelated array-of-tables must not be interpreted as
      // package or workspace metadata.
      section = "__unrelated_array_table__";
      continue;
    }

    if (trimmed.front() == '[') {
      auto close = trimmed.find(']');
      if (close != std::string::npos &&
          trim(trimmed.substr(close + 1)).empty()) {
        section = trimmed.substr(1, close - 1);
        if (section == "package")
          result.saw_package_table = true;
        if (section == "workspace")
          result.saw_workspace_table = true;
        if ((section.find("package") != std::string::npos ||
             section.find("workspace") != std::string::npos) &&
            section.find_first_of("\"'") != std::string::npos) {
          result.complete = false;
        }
      } else {
        result.complete = false;
      }
      continue;
    }

    auto eq = trimmed.find('=');
    if (eq == std::string::npos)
      continue;
    std::string key = trim(trimmed.substr(0, eq));
    std::string val = trim(trimmed.substr(eq + 1));

    if (section == "package" && key == "name") {
      if (!result.package_name.empty() ||
          !parse_basic_string(val, result.package_name)) {
        result.complete = false;
      }
    } else if (section == "workspace" && key == "members") {
      if (result.saw_workspace_members)
        result.complete = false;
      result.saw_workspace_members = true;
      std::string collected = val;
      while (collected.find(']') == std::string::npos) {
        if (!std::getline(in, line)) {
          result.complete = false;
          break;
        }
        collected += " " + trim(strip_comment(line));
      }
      std::vector<std::string> members;
      if (!parse_basic_string_array(collected, members))
        result.complete = false;
      result.workspace_members.insert(result.workspace_members.end(),
                                      members.begin(), members.end());
    } else if (section == "workspace" && key == "exclude") {
      // Exclusions change the expansion of ``members``. The deliberately
      // small parser cannot prove that expansion yet, so reject the receipt.
      result.complete = false;
    } else if (key == "workspace.members" || key == "package.name" ||
               (key == "workspace" &&
                val.find("members") != std::string::npos) ||
               key.find("members.") != std::string::npos ||
               (section == "workspace" &&
                key.find("members") != std::string::npos) ||
               (section == "package" &&
                key.find("name") != std::string::npos)) {
      // Dotted and inline-table spellings are valid TOML, but not part of the
      // strictly proven parser subset.
      result.complete = false;
    }
  }
  return result;
}

bool canonical_relative_path(const std::string &path, bool allow_final_star) {
  if (path.empty() || path.find('\\') != std::string::npos ||
      path.find('\0') != std::string::npos || path.front() == '/' ||
      (path.size() >= 2 && std::isalpha(static_cast<unsigned char>(path[0])) &&
       path[1] == ':')) {
    return false;
  }
  std::size_t start = 0;
  while (start <= path.size()) {
    const auto slash = path.find('/', start);
    const auto end = slash == std::string::npos ? path.size() : slash;
    const auto component = path.substr(start, end - start);
    const bool final = end == path.size();
    if (component.empty() || component == "." || component == "..")
      return false;
    if (component.find_first_of("*?[") != std::string::npos &&
        !(allow_final_star && final && component == "*")) {
      return false;
    }
    if (final)
      break;
    start = end + 1;
  }
  return true;
}

bool safe_workspace_pattern(const std::string &path) {
  if (path.empty() || path.find('\\') != std::string::npos ||
      path.find('\0') != std::string::npos || path.front() == '/' ||
      (path.size() >= 2 && std::isalpha(static_cast<unsigned char>(path[0])) &&
       path[1] == ':')) {
    return false;
  }
  std::size_t start = 0;
  while (start <= path.size()) {
    const auto slash = path.find('/', start);
    const auto end = slash == std::string::npos ? path.size() : slash;
    const auto component = path.substr(start, end - start);
    if (component == "..")
      return false;
    if (end == path.size())
      break;
    start = end + 1;
  }
  return true;
}

bool path_is_within(const std::filesystem::path &path,
                    const std::filesystem::path &root) {
  auto path_part = path.begin();
  for (auto root_part = root.begin(); root_part != root.end();
       ++root_part, ++path_part) {
    if (path_part == path.end() || *path_part != *root_part)
      return false;
  }
  return true;
}

std::optional<std::string> read_binary_file(const std::filesystem::path &path) {
  std::ifstream input(path, std::ios::binary);
  if (!input.is_open())
    return std::nullopt;
  std::ostringstream buffer;
  buffer << input.rdbuf();
  if (input.bad())
    return std::nullopt;
  return buffer.str();
}

SCIPInputFileReceipt input_receipt(const std::string &relative_path,
                                   const std::string &content) {
  return SCIPInputFileReceipt{relative_path,
                              static_cast<std::uint64_t>(content.size()),
                              sha256_hex(content)};
}

bool resolve_workspace_dirs(const std::filesystem::path &root,
                            const std::string &pattern,
                            std::vector<std::filesystem::path> &output) {
  if (!safe_workspace_pattern(pattern))
    return false;
  const bool proven_pattern = canonical_relative_path(pattern, true);
  const auto slash = pattern.rfind('/');
  const std::string leaf =
      slash == std::string::npos ? pattern : pattern.substr(slash + 1);
  const auto base =
      slash == std::string::npos ? root : root / pattern.substr(0, slash);
  std::error_code error;
  if (leaf.find('*') == std::string::npos) {
    const auto candidate = base / leaf;
    if (!std::filesystem::is_directory(candidate, error) || error)
      return false;
    output.push_back(candidate);
    return proven_pattern;
  }
  if (!std::filesystem::is_directory(base, error) || error)
    return false;
  std::filesystem::directory_iterator iterator(base, error);
  const std::filesystem::directory_iterator end;
  if (error)
    return false;
  for (; iterator != end; iterator.increment(error)) {
    if (error)
      return false;
    if (iterator->is_directory(error) && !error)
      output.push_back(iterator->path());
    if (error)
      return false;
  }
  std::sort(output.begin(), output.end());
  return proven_pattern && !output.empty();
}

} // namespace

void SCIPRustDecoder::load_metadata() {
  internal_crates_.clear();
  std::vector<SCIPInputFileReceipt> inputs;
  bool complete = true;
  if (!project_root_.has_value()) {
    set_metadata_receipt("rust-cargo", false);
    return;
  }
  std::error_code error;
  const auto absolute_root =
      std::filesystem::absolute(*project_root_, error).lexically_normal();
  if (error) {
    set_metadata_receipt("rust-cargo", false);
    return;
  }
  const auto root = std::filesystem::canonical(*project_root_, error);
  if (error) {
    set_metadata_receipt("rust-cargo", false);
    return;
  }
  complete = root == absolute_root;
  const auto cargo_toml = root / "Cargo.toml";
  const auto resolved_root_cargo =
      std::filesystem::canonical(cargo_toml, error);
  if (error || resolved_root_cargo != cargo_toml.lexically_normal())
    complete = false;
  error.clear();
  const auto root_content = read_binary_file(cargo_toml);
  if (!root_content.has_value()) {
    set_metadata_receipt("rust-cargo", false);
    return;
  }
  inputs.push_back(input_receipt("Cargo.toml", *root_content));
  MinimalCargoToml cargo = parse_cargo_toml(*root_content);
  complete = complete && cargo.complete &&
             (!cargo.saw_package_table || !cargo.package_name.empty()) &&
             (!cargo.package_name.empty() || cargo.saw_workspace_table);
  if (!cargo.package_name.empty())
    internal_crates_.insert(cargo.package_name);

  std::unordered_set<std::string> observed_members;
  for (const auto &pattern : cargo.workspace_members) {
    std::vector<std::filesystem::path> directories;
    if (!resolve_workspace_dirs(root, pattern, directories))
      complete = false;
    if (directories.empty())
      continue;
    for (const auto &directory : directories) {
      const auto resolved_directory =
          std::filesystem::canonical(directory, error);
      if (error || !path_is_within(resolved_directory, root)) {
        error.clear();
        complete = false;
        continue;
      }
      const auto absolute_directory =
          std::filesystem::absolute(directory, error).lexically_normal();
      if (error || absolute_directory != resolved_directory) {
        error.clear();
        complete = false;
      }
      const auto relative_directory =
          std::filesystem::relative(resolved_directory, root, error)
              .generic_string();
      if (error || !canonical_relative_path(relative_directory, false) ||
          !observed_members.insert(relative_directory).second) {
        error.clear();
        complete = false;
        continue;
      }
      const auto relative_cargo = relative_directory + "/Cargo.toml";
      const auto member_cargo = resolved_directory / "Cargo.toml";
      const auto resolved_member_cargo =
          std::filesystem::canonical(member_cargo, error);
      if (error || resolved_member_cargo != member_cargo.lexically_normal())
        complete = false;
      error.clear();
      const auto member_content = read_binary_file(member_cargo);
      if (!member_content.has_value()) {
        complete = false;
        continue;
      }
      inputs.push_back(input_receipt(relative_cargo, *member_content));
      MinimalCargoToml m = parse_cargo_toml(*member_content);
      complete = complete && m.complete && !m.package_name.empty();
      if (!m.package_name.empty())
        internal_crates_.insert(m.package_name);
    }
  }
  std::vector<std::string> crates(internal_crates_.begin(),
                                  internal_crates_.end());
  set_metadata_receipt("rust-cargo", complete, std::move(inputs),
                       std::move(crates));
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

  SubgraphBuilder builder;
  builder.add_file_hierarchy(file_path);

  auto occurrences = extract_blocks(document_block, "occurrences");
  for (const auto &occ : occurrences)
    process_occurrence(occ, file_path, builder);
  return builder.build();
}

void SCIPRustDecoder::process_occurrence(const std::string &occurrence_block,
                                         const std::string &file_path,
                                         SubgraphBuilder &builder) const {
  static const re2::RE2 range_re(R"re2(range:\s*(\d+))re2");
  static const re2::RE2 symbol_roles_re(R"re2(symbol_roles:\s*(\d+))re2");
  static const re2::RE2 enclosing_re(R"re2(enclosing_range:\s*(\d+))re2");

  auto ranges = extract_integers(occurrence_block, range_re);
  if (ranges.size() < 3)
    return;
  int line = ranges[0];
  int col_start = ranges[1];
  int col_end = ranges[2];

  auto symbol_opt = extract_symbol(occurrence_block);
  if (!symbol_opt || symbol_opt->empty())
    return;
  const std::string &symbol = *symbol_opt;
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
    builder.add_symbol_reference(unified, file_path, type,
                                 /*anchor_file=*/std::nullopt,
                                 /*anchor_line=*/line);
    builder.set_unified_name(unified, unified_name(unified, file_path, type),
                             /*only_if_missing=*/true);
  }
}

} // namespace codenib::core
