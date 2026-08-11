/*
 * SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#include "python_chunk_poc.h"

#include <tree_sitter/api.h>

#include <algorithm>
#include <chrono>
#include <limits>
#include <memory>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

extern "C" const TSLanguage *tree_sitter_python(void);

namespace codenib::core {
namespace {

enum class ChunkKind : std::uint8_t {
  Function = 1,
  Class = 2,
  Method = 3,
};

struct ChunkSpan {
  std::uint32_t start_line;
  std::uint32_t end_line;
  ChunkKind kind;
  std::string name;
};

using ParserPtr = std::unique_ptr<TSParser, decltype(&ts_parser_delete)>;
using TreePtr = std::unique_ptr<TSTree, decltype(&ts_tree_delete)>;

struct PythonParserState {
  PythonParserState() : parser(ts_parser_new(), &ts_parser_delete) {
    if (!parser)
      throw std::runtime_error("failed to allocate tree-sitter parser");
    if (!ts_parser_set_language(parser.get(), tree_sitter_python()))
      throw std::runtime_error(
          "bundled Python grammar is incompatible with tree-sitter runtime");
  }

  ParserPtr parser;
};

TSParser *thread_python_parser() {
  thread_local PythonParserState state;
  return state.parser.get();
}

bool node_has_type(TSNode node, std::string_view expected) {
  const char *type = ts_node_type(node);
  return type != nullptr && expected == type;
}

bool is_function(TSNode node) {
  return node_has_type(node, "function_definition") ||
         node_has_type(node, "async_function_definition");
}

std::optional<TSNode> direct_child_with_type(TSNode node,
                                             std::string_view expected) {
  const auto count = ts_node_child_count(node);
  for (std::uint32_t index = 0; index < count; ++index) {
    TSNode child = ts_node_child(node, index);
    if (node_has_type(child, expected))
      return child;
  }
  return std::nullopt;
}

std::optional<TSNode> decorated_definition(TSNode node) {
  const auto count = ts_node_child_count(node);
  for (std::uint32_t index = 0; index < count; ++index) {
    TSNode child = ts_node_child(node, index);
    if (is_function(child) || node_has_type(child, "class_definition"))
      return child;
  }
  return std::nullopt;
}

std::optional<std::string> definition_name(TSNode node,
                                           std::string_view source) {
  const auto identifier = direct_child_with_type(node, "identifier");
  if (!identifier.has_value())
    return std::nullopt;
  const auto start = ts_node_start_byte(*identifier);
  const auto end = ts_node_end_byte(*identifier);
  if (start > end || end > source.size())
    throw std::runtime_error("tree-sitter returned an invalid name byte range");
  return std::string(source.substr(start, end - start));
}

void append_span(std::vector<ChunkSpan> &spans, TSNode node, ChunkKind kind,
                 std::string name) {
  const auto start = ts_node_start_point(node).row;
  const auto end = ts_node_end_point(node).row;
  spans.push_back(ChunkSpan{start, end, kind, std::move(name)});
}

void append_class_methods(std::vector<ChunkSpan> &spans, TSNode class_node,
                          std::string_view class_name,
                          std::string_view source) {
  const auto block = direct_child_with_type(class_node, "block");
  if (!block.has_value())
    return;
  const auto count = ts_node_child_count(*block);
  for (std::uint32_t index = 0; index < count; ++index) {
    TSNode statement = ts_node_child(*block, index);
    TSNode method_node = statement;
    if (node_has_type(statement, "decorated_definition")) {
      const auto actual = decorated_definition(statement);
      if (!actual.has_value() || !is_function(*actual))
        continue;
      method_node = *actual;
    } else if (!is_function(statement)) {
      continue;
    }
    const auto method_name = definition_name(method_node, source);
    if (!method_name.has_value())
      continue;
    std::string full_name(class_name);
    full_name.push_back('.');
    full_name.append(*method_name);
    append_span(spans, statement, ChunkKind::Method, std::move(full_name));
  }
}

void append_u32(std::vector<std::uint8_t> &output, std::uint32_t value) {
  for (unsigned shift = 0; shift < 32; shift += 8)
    output.push_back(static_cast<std::uint8_t>((value >> shift) & 0xffU));
}

std::uint64_t elapsed_ns(std::chrono::steady_clock::time_point start,
                         std::chrono::steady_clock::time_point end) {
  return static_cast<std::uint64_t>(
      std::chrono::duration_cast<std::chrono::nanoseconds>(end - start)
          .count());
}

} // namespace

PythonChunkBuffer extract_python_chunk_buffer(std::string_view source,
                                              int chunk_depth,
                                              bool l2_level_exclusive) {
  if (chunk_depth != 1 && chunk_depth != 2)
    throw std::invalid_argument(
        "native Python chunk extraction supports chunk_depth 1 or 2");
  if (source.size() > std::numeric_limits<std::uint32_t>::max())
    throw std::overflow_error("Python source exceeds tree-sitter's u32 limit");

  using clock = std::chrono::steady_clock;
  const auto parse_started = clock::now();
  TSParser *parser = thread_python_parser();
  TreePtr tree(
      ts_parser_parse_string(parser, nullptr, source.data(),
                             static_cast<std::uint32_t>(source.size())),
      &ts_tree_delete);
  if (!tree)
    throw std::runtime_error("tree-sitter failed to parse Python source");
  const auto parse_finished = clock::now();

  std::vector<ChunkSpan> spans;
  TSNode root = ts_tree_root_node(tree.get());
  const auto count = ts_node_child_count(root);
  const bool include_classes = chunk_depth < 2 || !l2_level_exclusive;
  const bool include_methods = chunk_depth >= 2;
  for (std::uint32_t index = 0; index < count; ++index) {
    TSNode child = ts_node_child(root, index);
    TSNode definition = child;
    if (node_has_type(child, "decorated_definition")) {
      const auto actual = decorated_definition(child);
      if (!actual.has_value())
        continue;
      definition = *actual;
    }

    if (is_function(definition)) {
      const auto name = definition_name(definition, source);
      if (name.has_value())
        append_span(spans, child, ChunkKind::Function, *name);
      continue;
    }
    if (!node_has_type(definition, "class_definition"))
      continue;
    const auto name = definition_name(definition, source);
    if (!name.has_value())
      continue;
    if (include_classes)
      append_span(spans, child, ChunkKind::Class, *name);
    if (include_methods)
      append_class_methods(spans, definition, *name, source);
  }
  std::stable_sort(spans.begin(), spans.end(),
                   [](const ChunkSpan &left, const ChunkSpan &right) {
                     return left.start_line < right.start_line;
                   });

  PythonChunkBuffer result;
  if (spans.size() > std::numeric_limits<std::uint32_t>::max())
    throw std::overflow_error("native Python chunk rows exceed u32 count");
  if (spans.size() > result.rows.max_size() / PYTHON_CHUNK_BUFFER_ROW_SIZE)
    throw std::overflow_error("native Python chunk row table exceeds size_t");
  result.row_count = static_cast<std::uint32_t>(spans.size());
  result.rows.reserve(spans.size() * PYTHON_CHUNK_BUFFER_ROW_SIZE);
  for (const auto &span : spans) {
    if (span.name.size() > std::numeric_limits<std::uint32_t>::max() ||
        result.arena.size() >
            std::numeric_limits<std::uint32_t>::max() - span.name.size())
      throw std::overflow_error("native Python chunk name arena exceeds u32");
    const auto offset = static_cast<std::uint32_t>(result.arena.size());
    const auto length = static_cast<std::uint32_t>(span.name.size());
    append_u32(result.rows, span.start_line);
    append_u32(result.rows, span.end_line);
    result.rows.push_back(static_cast<std::uint8_t>(span.kind));
    result.rows.insert(result.rows.end(), 3, 0);
    append_u32(result.rows, offset);
    append_u32(result.rows, length);
    result.arena.insert(result.arena.end(), span.name.begin(), span.name.end());
  }
  const auto extract_finished = clock::now();
  result.parse_ns = elapsed_ns(parse_started, parse_finished);
  result.extract_ns = elapsed_ns(parse_finished, extract_finished);
  return result;
}

} // namespace codenib::core
