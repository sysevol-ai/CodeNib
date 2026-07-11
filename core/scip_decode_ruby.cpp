// SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
//
// SPDX-License-Identifier: Apache-2.0

#include "scip_decode_ruby.h"

#include <algorithm>
#include <cctype>
#include <filesystem>
#include <fstream>
#include <re2/re2.h>
#include <utility>

namespace codeminer::core {

namespace {

bool starts_with(const std::string &s, const std::string &prefix) {
  return s.size() >= prefix.size() && s.compare(0, prefix.size(), prefix) == 0;
}

std::string trim(std::string s) {
  std::size_t start = 0;
  while (start < s.size() &&
         std::isspace(static_cast<unsigned char>(s[start]))) {
    ++start;
  }
  std::size_t end = s.size();
  while (end > start && std::isspace(static_cast<unsigned char>(s[end - 1]))) {
    --end;
  }
  return s.substr(start, end - start);
}

std::string remove_char(std::string s, char target) {
  s.erase(std::remove(s.begin(), s.end(), target), s.end());
  return s;
}

std::string rstrip_char(std::string s, char target) {
  while (!s.empty() && s.back() == target)
    s.pop_back();
  return s;
}

std::string replace_char(std::string s, char from, char to) {
  std::replace(s.begin(), s.end(), from, to);
  return s;
}

bool is_symbol_type_with_scope(const std::string &type) {
  return type == NODE_TYPE_CLASS || type == NODE_TYPE_FUNCTION ||
         type == NODE_TYPE_METHOD;
}

std::string file_definition_lookup_key(const std::string &file_path,
                                       const std::string &symbol_key) {
  return file_path + "\n" + symbol_key;
}

std::optional<std::string>
document_file_path(const std::string &document_block);
bool is_source_file(const std::string &file_path);
std::optional<std::string> make_symbol_key(const std::string &symbol);
std::string normalize_singleton_class_descriptor(std::string descriptor);
std::string symbol_type_from_block(const std::string &block,
                                   const std::string &original_symbol);
std::string symbol_type_from_descriptor(const std::string &symbol);
std::string display_name_from_block(const std::string &block);
std::string symbol_display(const std::string &key,
                           const std::string &symbol_type,
                           const std::string &display_name = "");
std::pair<std::string, std::string> split_owner_member(const std::string &key);
std::string qualified_name(std::string key);
std::string definition_graph_key(const std::string &key,
                                 const std::string &symbol_type,
                                 const std::string &file_path);
std::pair<std::optional<int>, std::optional<int>>
scope_lines(int line, const std::vector<int> &enclosing_ranges);
std::optional<std::pair<std::string, std::string>>
parse_ruby_alias(const std::string &line);
std::string clean_alias_token(std::string token);

} // namespace

void SCIPRubyDecoder::prescan(const std::vector<std::string> &document_blocks) {
  symbols_.clear();
  definition_lines_by_file_.clear();
  definition_graph_keys_.clear();
  document_order_by_file_.clear();

  for (std::size_t i = 0; i < document_blocks.size(); ++i) {
    const auto &document = document_blocks[i];
    auto file_path = document_file_path(document);
    if (!file_path || !is_source_file(*file_path))
      continue;
    document_order_by_file_[*file_path] = i;
    collect_symbol_info(document, *file_path);
  }

  for (const auto &document : document_blocks) {
    auto file_path = document_file_path(document);
    if (!file_path || !is_source_file(*file_path))
      continue;
    auto definitions = collect_document_definitions(document, *file_path);
    for (const auto &definition : definitions) {
      definition_graph_keys_[file_definition_lookup_key(*file_path,
                                                        definition.key)] =
          definition_graph_key(definition.key, definition.symbol_type,
                               *file_path);
    }
    definition_lines_by_file_[*file_path] = std::move(definitions);
  }
}

void SCIPRubyDecoder::collect_symbol_info(const std::string &document_block,
                                          const std::string &file_path) {
  auto symbol_blocks = extract_blocks(document_block, "symbols");
  for (const auto &block : symbol_blocks) {
    auto symbol = extract_symbol(block);
    if (!symbol)
      continue;
    auto key = make_symbol_key(*symbol);
    if (!key)
      continue;
    std::string symbol_type = symbol_type_from_block(block, *symbol);
    std::string display_name =
        symbol_display(*key, symbol_type, display_name_from_block(block));
    symbols_[*key] = SymbolInfo{*key, file_path, display_name, symbol_type};
  }
}

Subgraph
SCIPRubyDecoder::process_document(const std::string &document_block) const {
  auto file_path = document_file_path(document_block);
  if (!file_path || !is_source_file(*file_path))
    return Subgraph{};

  SubgraphBuilder builder;
  builder.add_file_hierarchy(*file_path);

  DocumentState state;
  state.file_path = *file_path;
  auto order_it = document_order_by_file_.find(*file_path);
  if (order_it != document_order_by_file_.end())
    state.document_order = order_it->second;
  auto definitions_it = definition_lines_by_file_.find(*file_path);
  if (definitions_it != definition_lines_by_file_.end())
    state.definitions = definitions_it->second;
  state.source_lines = read_source_lines(*file_path);

  auto occurrences = extract_blocks(document_block, "occurrences");
  for (const auto &occurrence : occurrences) {
    process_occurrence(occurrence, *file_path, state, builder);
  }
  add_alias_definitions(*file_path, state, builder);
  return builder.build();
}

std::vector<SCIPRubyDecoder::DefinitionInfo>
SCIPRubyDecoder::collect_document_definitions(
    const std::string &document_block, const std::string &file_path) const {
  static const re2::RE2 range_re(R"re2(range:\s*(\d+))re2");
  static const re2::RE2 symbol_roles_re(R"re2(symbol_roles:\s*(\d+))re2");

  std::vector<DefinitionInfo> definitions;
  auto occurrences = extract_blocks(document_block, "occurrences");
  for (const auto &occurrence : occurrences) {
    int roles = 0;
    re2::RE2::PartialMatch(occurrence, symbol_roles_re, &roles);
    if (!(roles & 1))
      continue;

    auto ranges = extract_integers(occurrence, range_re);
    if (ranges.size() < 3)
      continue;

    auto symbol = extract_symbol(occurrence);
    if (!symbol)
      continue;
    auto key = make_symbol_key(*symbol);
    if (!key)
      continue;

    auto info = symbol_info_for_key(*key, file_path, *symbol);
    if (!info)
      continue;
    definitions.push_back(DefinitionInfo{*key, ranges[0], info->symbol_type});
  }
  std::stable_sort(definitions.begin(), definitions.end(),
                   [](const DefinitionInfo &a, const DefinitionInfo &b) {
                     return a.line < b.line;
                   });
  return definitions;
}

void SCIPRubyDecoder::process_occurrence(const std::string &occurrence_block,
                                         const std::string &file_path,
                                         DocumentState &state,
                                         SubgraphBuilder &builder) const {
  static const re2::RE2 range_re(R"re2(range:\s*(\d+))re2");
  static const re2::RE2 symbol_roles_re(R"re2(symbol_roles:\s*(\d+))re2");
  static const re2::RE2 enclosing_re(R"re2(enclosing_range:\s*(\d+))re2");

  auto ranges = extract_integers(occurrence_block, range_re);
  if (ranges.size() < 3)
    return;
  int line = ranges[0];

  auto symbol = extract_symbol(occurrence_block);
  if (!symbol)
    return;
  auto key = make_symbol_key(*symbol);
  if (!key)
    return;

  int roles = 0;
  re2::RE2::PartialMatch(occurrence_block, symbol_roles_re, &roles);
  auto enclosing = extract_integers(occurrence_block, enclosing_re);

  builder.exit_scopes_by_line(line);
  auto info = symbol_info_for_key(*key, file_path, *symbol);
  if (!info)
    return;

  if (roles & 1) {
    add_definition(*info, file_path, line, enclosing, state, builder);
  } else {
    add_reference(*info, line, state, builder);
  }
}

void SCIPRubyDecoder::add_definition(const SymbolInfo &info,
                                     const std::string &file_path, int line,
                                     const std::vector<int> &enclosing_ranges,
                                     DocumentState &state,
                                     SubgraphBuilder &builder) const {
  std::string graph_key =
      definition_graph_key(info.key, info.symbol_type, file_path);
  state.file_definition_keys[file_definition_lookup_key(file_path, info.key)] =
      graph_key;

  auto [start_line, end_line] = scope_lines(line, enclosing_ranges);
  builder.add_symbol_node(graph_key, line, start_line, end_line,
                          info.symbol_type);
  std::string display_name =
      definition_display_name(info, file_path, line, state, builder);
  builder.set_unified_name(
      graph_key,
      file_path + ":" + (display_name.empty() ? info.key : display_name));

  std::string parent =
      containment_parent_in_file(info.key, file_path, state, builder);
  builder.add_edge(parent, graph_key, EDGE_TYPE_CONTAIN);

  if (is_symbol_type_with_scope(info.symbol_type) && start_line.has_value() &&
      end_line.has_value()) {
    builder.update_current_scope(graph_key, start_line, end_line);
  }

  if (info.symbol_type == NODE_TYPE_FUNCTION ||
      info.symbol_type == NODE_TYPE_METHOD) {
    auto [owner, member] = split_owner_member(info.key);
    if (!owner.empty() && !member.empty()) {
      state.method_definitions_by_member[member].push_back(
          MethodDefinition{line, graph_key, info.symbol_type});
    }
  }
}

void SCIPRubyDecoder::add_reference(const SymbolInfo &info, int anchor_line,
                                    const DocumentState &state,
                                    SubgraphBuilder &builder) const {
  std::string source =
      reference_source(info.file_path, anchor_line, state, builder)
          .value_or(builder.current_scope());
  builder.add_symbol_reference_from(source, info.key, info.file_path,
                                    info.symbol_type,
                                    /*anchor_file=*/std::nullopt,
                                    /*anchor_line=*/anchor_line);
  const Subgraph::Node *node = builder.node(info.key);
  if (node != nullptr && node->is_definition &&
      node->data.unified_name.has_value()) {
    return;
  }
  builder.set_unified_name(
      info.key, info.file_path + ":" +
                    (info.display_name.empty() ? info.key : info.display_name));
}

void SCIPRubyDecoder::add_alias_definitions(const std::string &file_path,
                                            DocumentState &state,
                                            SubgraphBuilder &builder) const {
  if (state.source_lines.empty())
    return;

  for (std::size_t i = 0; i < state.source_lines.size(); ++i) {
    auto alias_pair = parse_ruby_alias(state.source_lines[i]);
    if (!alias_pair)
      continue;

    const std::string &alias_member = alias_pair->first;
    const std::string &original_member = alias_pair->second;
    auto it = state.method_definitions_by_member.find(original_member);
    if (it == state.method_definitions_by_member.end())
      continue;

    const MethodDefinition *best = nullptr;
    for (const auto &definition : it->second) {
      if (definition.line >= static_cast<int>(i))
        break;
      best = &definition;
    }
    if (best == nullptr)
      continue;

    auto owner_member = split_owner_member(best->key);
    const std::string &owner = owner_member.first;
    if (owner.empty())
      continue;

    auto parent_it = state.file_definition_keys.find(
        file_definition_lookup_key(file_path, owner));
    if (parent_it == state.file_definition_keys.end())
      continue;
    const std::string &parent = parent_it->second;
    if (!builder.has_node(parent))
      continue;

    std::string alias_key = owner + "#" + alias_member;
    if (builder.has_node(alias_key))
      continue;

    std::string display_name = symbol_display(alias_key, best->symbol_type);
    int line = static_cast<int>(i);
    builder.add_symbol_node(alias_key, line, line, line, best->symbol_type);
    builder.set_unified_name(alias_key, file_path + ":" + display_name);
    builder.add_edge(parent, alias_key, EDGE_TYPE_CONTAIN);
  }
}

std::optional<SCIPRubyDecoder::SymbolInfo>
SCIPRubyDecoder::symbol_info_for_key(const std::string &key,
                                     const std::string &file_path,
                                     const std::string &original_symbol) const {
  auto it = symbols_.find(key);
  if (it != symbols_.end())
    return it->second;

  std::string type = symbol_type_from_descriptor(original_symbol);
  return SymbolInfo{key, file_path, symbol_display(key, type), type};
}

namespace {

std::optional<std::string>
document_file_path(const std::string &document_block) {
  static const re2::RE2 relative_path_regex(
      R"re2(relative_path:\s*"([^"]+)")re2");
  std::string file_path;
  if (!re2::RE2::PartialMatch(document_block, relative_path_regex,
                              &file_path)) {
    return std::nullopt;
  }
  return file_path;
}

bool is_source_file(const std::string &file_path) {
  return ends_with(file_path, ".rb");
}

std::optional<std::string> make_symbol_key(const std::string &symbol) {
  if (symbol.empty() || starts_with(symbol, "local "))
    return std::nullopt;
  auto parts = split_ws(symbol);
  if (parts.size() < 5 || parts[0] != "scip-ruby")
    return std::nullopt;

  std::string descriptor = remove_char(parts.back(), '`');
  descriptor = rstrip_char(descriptor, '.');
  descriptor = normalize_singleton_class_descriptor(descriptor);
  if (ends_with(descriptor, "()"))
    descriptor.resize(descriptor.size() - 2);
  if (ends_with(descriptor, "#"))
    descriptor.resize(descriptor.size() - 1);
  if (descriptor.empty())
    return std::nullopt;
  return descriptor;
}

std::string normalize_singleton_class_descriptor(std::string descriptor) {
  static const std::string prefix = "<Class:";
  if (starts_with(descriptor, prefix)) {
    auto close = descriptor.find(">#");
    if (close != std::string::npos) {
      std::string owner =
          descriptor.substr(prefix.size(), close - prefix.size());
      descriptor = owner + "#" + descriptor.substr(close + 2);
    }
  }

  while (true) {
    auto pos = descriptor.find("#<Class:");
    if (pos == std::string::npos)
      break;
    auto close = descriptor.find(">#", pos);
    if (close == std::string::npos)
      break;
    std::string prefix_part = descriptor.substr(0, pos);
    std::string owner = descriptor.substr(pos + 8, close - (pos + 8));
    std::string suffix = descriptor.substr(close + 2);
    descriptor = prefix_part + "#" + owner + "#" + suffix;
  }
  return descriptor;
}

std::string symbol_type_from_block(const std::string &block,
                                   const std::string &original_symbol) {
  static const re2::RE2 kind_re(R"re2(kind:\s*(\w+))re2");
  std::string kind;
  if (re2::RE2::PartialMatch(block, kind_re, &kind)) {
    if (kind == "Class" || kind == "Interface" || kind == "Enum" ||
        kind == "Object" || kind == "Trait") {
      return NODE_TYPE_CLASS;
    }
    if (kind == "Method" || kind == "Constructor")
      return NODE_TYPE_METHOD;
    if (kind == "Field" || kind == "Property" || kind == "EnumMember" ||
        kind == "Constant" || kind == "Variable") {
      return NODE_TYPE_FIELD;
    }
  }
  return symbol_type_from_descriptor(original_symbol);
}

std::string symbol_type_from_descriptor(const std::string &symbol) {
  auto parts = split_ws(symbol);
  std::string descriptor = parts.empty() ? std::string{} : parts.back();
  if (ends_with(descriptor, "/"))
    return NODE_TYPE_CLASS;
  if (ends_with(descriptor, "#"))
    return NODE_TYPE_CLASS;
  if (descriptor.find('#') != std::string::npos &&
      ends_with(descriptor, "().")) {
    return NODE_TYPE_METHOD;
  }
  if (descriptor.find('#') != std::string::npos && ends_with(descriptor, "."))
    return NODE_TYPE_FIELD;
  if (ends_with(descriptor, "()."))
    return NODE_TYPE_FUNCTION;
  return NODE_TYPE_SYMBOL;
}

std::string display_name_from_block(const std::string &block) {
  static const re2::RE2 display_re(R"re2(display_name:\s*"([^"]*)")re2");
  std::string display;
  if (re2::RE2::PartialMatch(block, display_re, &display))
    return display;
  return {};
}

std::string symbol_display(const std::string &key,
                           const std::string &symbol_type,
                           const std::string &display_name) {
  auto [owner, member] = split_owner_member(key);
  std::string owner_name = qualified_name(owner);

  if (symbol_type == NODE_TYPE_CLASS) {
    if (!owner_name.empty() && !member.empty())
      return owner_name + "." + member;
    return !member.empty() ? member : key;
  }
  if (symbol_type == NODE_TYPE_METHOD || symbol_type == NODE_TYPE_FUNCTION) {
    if (!owner_name.empty() && !member.empty())
      return owner_name + "." + member + "()";
    return (!member.empty() ? member
                            : (!display_name.empty() ? display_name : key)) +
           "()";
  }
  if (symbol_type == NODE_TYPE_FIELD) {
    if (!owner_name.empty() && !member.empty())
      return owner_name + "." + member;
    return !member.empty() ? member
                           : (!display_name.empty() ? display_name : key);
  }
  return !display_name.empty() ? display_name
                               : (!member.empty() ? member : key);
}

std::pair<std::string, std::string> split_owner_member(const std::string &key) {
  auto pos = key.rfind('#');
  if (pos == std::string::npos)
    return {"", key};
  return {key.substr(0, pos), key.substr(pos + 1)};
}

std::string qualified_name(std::string key) {
  key = replace_char(std::move(key), '#', '.');
  while (!key.empty() && key.front() == '.')
    key.erase(key.begin());
  while (!key.empty() && key.back() == '.')
    key.pop_back();
  return key;
}

std::string definition_graph_key(const std::string &key,
                                 const std::string &symbol_type,
                                 const std::string &file_path) {
  if (symbol_type == NODE_TYPE_CLASS && key.find('#') == std::string::npos)
    return file_path + ":" + key;
  return key;
}

std::pair<std::optional<int>, std::optional<int>>
scope_lines(int line, const std::vector<int> &enclosing_ranges) {
  if (enclosing_ranges.size() >= 4)
    return {enclosing_ranges[0], enclosing_ranges[2]};
  if (enclosing_ranges.size() == 3)
    return {enclosing_ranges[0], enclosing_ranges[0]};
  return {std::nullopt, std::nullopt};
}

} // namespace

std::string SCIPRubyDecoder::definition_display_name(
    const SymbolInfo &info, const std::string &file_path, int line,
    const DocumentState &state, const SubgraphBuilder &builder) const {
  auto nested = nested_method_display_name(info, file_path, builder);
  if (nested)
    return *nested;

  std::string display = top_level_singleton_display_name(
      info, file_path, line, info.display_name, state);
  if (!ends_with(display, "=()"))
    return display;
  if (info.symbol_type != NODE_TYPE_FUNCTION &&
      info.symbol_type != NODE_TYPE_METHOD) {
    return display;
  }
  std::string source = source_line(state, line);
  if (source.find("attr_accessor") == std::string::npos &&
      source.find("attr_writer") == std::string::npos) {
    return display;
  }
  display.resize(display.size() - 3);
  return display + "()";
}

std::optional<std::string> SCIPRubyDecoder::nested_method_display_name(
    const SymbolInfo &info, const std::string &file_path,
    const SubgraphBuilder &builder) const {
  if (info.symbol_type != NODE_TYPE_FUNCTION &&
      info.symbol_type != NODE_TYPE_METHOD) {
    return std::nullopt;
  }
  auto parent_display = current_method_scope_display(file_path, builder);
  if (!parent_display)
    return std::nullopt;
  auto owner_member = split_owner_member(info.key);
  const std::string &member = owner_member.second;
  if (member.empty())
    return std::nullopt;
  return *parent_display + "." + member + "()";
}

std::optional<std::string> SCIPRubyDecoder::current_method_scope_display(
    const std::string &file_path, const SubgraphBuilder &builder) const {
  const std::string &scope = builder.current_scope();
  if (scope.empty() || scope == builder.current_file())
    return std::nullopt;
  const Subgraph::Node *node = builder.node(scope);
  if (node == nullptr)
    return std::nullopt;
  if (node->data.type != NODE_TYPE_FUNCTION &&
      node->data.type != NODE_TYPE_METHOD) {
    return std::nullopt;
  }
  if (!node->data.unified_name.has_value())
    return std::nullopt;
  std::string prefix = file_path + ":";
  if (!starts_with(*node->data.unified_name, prefix))
    return std::nullopt;
  return node->data.unified_name->substr(prefix.size());
}

std::string SCIPRubyDecoder::top_level_singleton_display_name(
    const SymbolInfo &info, const std::string &file_path, int line,
    std::string display_name, const DocumentState &state) const {
  auto [owner, member] = split_owner_member(info.key);
  if (owner != "Object" || member.empty() ||
      (info.symbol_type != NODE_TYPE_FUNCTION &&
       info.symbol_type != NODE_TYPE_METHOD)) {
    return display_name;
  }
  re2::RE2 def_re("\\bdef\\s+[A-Za-z_]\\w*\\." + re2::RE2::QuoteMeta(member) +
                  "\\b");
  if (re2::RE2::PartialMatch(source_line(state, line), def_re))
    return member + "()";
  return display_name;
}

std::string SCIPRubyDecoder::source_line(const DocumentState &state,
                                         int line) const {
  if (line < 0 || static_cast<std::size_t>(line) >= state.source_lines.size())
    return {};
  return state.source_lines[static_cast<std::size_t>(line)];
}

std::string SCIPRubyDecoder::containment_parent_in_file(
    const std::string &key, const std::string &file_path,
    const DocumentState &state, const SubgraphBuilder &builder) const {
  auto method_scope = current_method_scope_vertex(file_path, builder);
  if (method_scope)
    return *method_scope;

  auto [owner, member] = split_owner_member(key);
  if (!member.empty()) {
    auto it = state.file_definition_keys.find(
        file_definition_lookup_key(file_path, owner));
    if (it != state.file_definition_keys.end() && builder.has_node(it->second))
      return it->second;
  }
  return builder.current_scope();
}

std::optional<std::string> SCIPRubyDecoder::current_method_scope_vertex(
    const std::string &file_path, const SubgraphBuilder &builder) const {
  if (current_method_scope_display(file_path, builder))
    return builder.current_scope();
  return std::nullopt;
}

std::optional<std::string>
SCIPRubyDecoder::reference_source(const std::string &file_path, int anchor_line,
                                  const DocumentState &state,
                                  const SubgraphBuilder &builder) const {
  auto definitions_it = definition_lines_by_file_.find(file_path);
  if (definitions_it == definition_lines_by_file_.end())
    return std::nullopt;

  auto order_it = document_order_by_file_.find(file_path);
  if (order_it == document_order_by_file_.end())
    return std::nullopt;
  const bool same_document = file_path == state.file_path;
  if (order_it->second > state.document_order)
    return std::nullopt;

  const auto &definitions = definitions_it->second;
  for (auto it = definitions.rbegin(); it != definitions.rend(); ++it) {
    if (it->line > anchor_line)
      continue;

    auto key_it = definition_graph_keys_.find(
        file_definition_lookup_key(file_path, it->key));
    std::string graph_key =
        key_it == definition_graph_keys_.end() ? it->key : key_it->second;
    if (same_document && !builder.has_node(graph_key))
      continue;
    if (it->symbol_type == NODE_TYPE_METHOD ||
        it->symbol_type == NODE_TYPE_FUNCTION ||
        it->symbol_type == NODE_TYPE_CLASS) {
      return graph_key;
    }
  }
  return std::nullopt;
}

namespace {

std::optional<std::pair<std::string, std::string>>
parse_ruby_alias(const std::string &line) {
  std::string text = line;
  auto comment = text.find('#');
  if (comment != std::string::npos)
    text = text.substr(0, comment);
  text = trim(text);
  if (!starts_with(text, "alias "))
    return std::nullopt;
  auto parts = split_ws(text);
  if (parts.size() != 3)
    return std::nullopt;
  std::string alias_member = clean_alias_token(parts[1]);
  std::string original_member = clean_alias_token(parts[2]);
  if (alias_member.empty() || original_member.empty())
    return std::nullopt;
  return std::make_pair(alias_member, original_member);
}

std::string clean_alias_token(std::string token) {
  token = trim(std::move(token));
  if (!token.empty() && token.front() == ':')
    token.erase(token.begin());
  if (token.size() >= 2 && token.front() == token.back() &&
      (token.front() == '\'' || token.front() == '"')) {
    token = token.substr(1, token.size() - 2);
  }
  return token;
}

} // namespace

std::vector<std::string>
SCIPRubyDecoder::read_source_lines(const std::string &file_path) const {
  if (!project_root_.has_value())
    return {};
  std::filesystem::path path =
      std::filesystem::path(*project_root_) / file_path;
  std::ifstream input(path);
  if (!input.is_open())
    return {};

  std::vector<std::string> lines;
  std::string line;
  while (std::getline(input, line)) {
    if (!line.empty() && line.back() == '\r')
      line.pop_back();
    lines.push_back(line);
  }
  return lines;
}

} // namespace codeminer::core
