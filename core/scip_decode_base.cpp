// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

#include "scip_decode_base.h"

#include <algorithm>
#include <cctype>
#include <chrono>
#include <cstdlib>
#include <fstream>
#include <future>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <thread>
#include <unordered_map>
#include <unordered_set>

namespace codenib::core {

namespace {

bool debug_logging_enabled() {
  static const bool enabled = std::getenv("CODENIB_SCIP_DEBUG") != nullptr;
  return enabled;
}

void log_debug(const std::string &message) {
  if (debug_logging_enabled()) {
    std::cerr << "[SCIPDecode] " << message << '\n';
  }
}

} // namespace

std::vector<std::string> extract_blocks(const std::string &text,
                                        const std::string &keyword) {
  std::vector<std::string> blocks;
  std::size_t search_pos = 0;
  while (true) {
    std::size_t key_pos = text.find(keyword, search_pos);
    if (key_pos == std::string::npos)
      break;

    if (key_pos > 0) {
      unsigned char prev = static_cast<unsigned char>(text[key_pos - 1]);
      if (std::isalnum(prev) || prev == '_' || prev == '/') {
        search_pos = key_pos + keyword.size();
        continue;
      }
    }

    std::size_t brace_pos = key_pos + keyword.size();
    while (brace_pos < text.size() &&
           std::isspace(static_cast<unsigned char>(text[brace_pos]))) {
      ++brace_pos;
    }
    if (brace_pos >= text.size() || text[brace_pos] != '{') {
      search_pos = key_pos + keyword.size();
      continue;
    }

    std::size_t start = brace_pos + 1;
    int depth = 1;
    bool in_string = false;
    bool escape = false;
    bool done = false;
    for (std::size_t i = start; i < text.size(); ++i) {
      char ch = text[i];
      if (in_string) {
        if (escape) {
          escape = false;
        } else if (ch == '\\') {
          escape = true;
        } else if (ch == '"') {
          in_string = false;
        }
        continue;
      }
      if (ch == '"') {
        in_string = true;
        continue;
      }
      if (ch == '{') {
        ++depth;
        continue;
      }
      if (ch == '}') {
        --depth;
        if (depth == 0) {
          blocks.emplace_back(text.substr(start, i - start));
          search_pos = i + 1;
          done = true;
          break;
        }
        continue;
      }
    }

    if (!done) {
      log_debug("Unbalanced braces while parsing blocks for keyword '" +
                keyword + "'");
      break;
    }
  }
  return blocks;
}

std::optional<std::string> extract_symbol(const std::string &text) {
  static const std::string kw = "symbol:";
  std::size_t pos = 0;
  while (pos < text.size()) {
    std::size_t k = text.find(kw, pos);
    if (k == std::string::npos)
      return std::nullopt;
    if (k > 0) {
      unsigned char prev = static_cast<unsigned char>(text[k - 1]);
      if (std::isalnum(prev) || prev == '_') {
        pos = k + kw.size();
        continue;
      }
    }
    std::size_t i = k + kw.size();
    while (i < text.size() && (text[i] == ' ' || text[i] == '\t'))
      ++i;
    if (i >= text.size() || text[i] != '"') {
      pos = k + kw.size();
      continue;
    }
    ++i;
    std::string buf;
    while (i < text.size()) {
      char ch = text[i];
      if (ch == '"')
        return buf;
      if (ch == '\\' && i + 1 < text.size()) {
        char nxt = text[i + 1];
        switch (nxt) {
        case 'n':
          buf.push_back('\n');
          break;
        case 't':
          buf.push_back('\t');
          break;
        case 'r':
          buf.push_back('\r');
          break;
        case '\\':
          buf.push_back('\\');
          break;
        case '"':
          buf.push_back('"');
          break;
        case '\'':
          buf.push_back('\'');
          break;
        default:
          buf.push_back(nxt);
          break;
        }
        i += 2;
        continue;
      }
      buf.push_back(ch);
      ++i;
    }
    return std::nullopt;
  }
  return std::nullopt;
}

SCIPDecoderBase::SCIPDecoderBase(std::string index_file_path,
                                 std::optional<std::string> project_root)
    : index_file_path_(std::move(index_file_path)),
      project_root_(std::move(project_root)),
      code_graph_(project_root_ ? *project_root_ : std::string{}) {}

CodeGraph SCIPDecoderBase::decode() {
  using clock = std::chrono::high_resolution_clock;
  using ms = std::chrono::milliseconds;
  auto t0 = clock::now();

  load_metadata();
  auto t_meta = clock::now();

  std::ifstream input(index_file_path_);
  if (!input.is_open()) {
    throw std::runtime_error("Failed to open SCIP index file at " +
                             index_file_path_);
  }
  std::ostringstream buffer;
  buffer << input.rdbuf();
  std::string content = buffer.str();
  auto t_read = clock::now();

  std::vector<std::string> document_blocks =
      extract_blocks(content, "documents");
  auto t_extract = clock::now();

  prescan(document_blocks);
  auto t_prescan = clock::now();

  const std::size_t max_threads =
      std::max<std::size_t>(1, std::thread::hardware_concurrency());
  const std::size_t num_blocks = document_blocks.size();
  std::vector<Subgraph> subgraphs;
  subgraphs.resize(num_blocks);

  for (std::size_t batch_start = 0; batch_start < num_blocks;
       batch_start += max_threads) {
    const std::size_t batch_end =
        std::min(batch_start + max_threads, num_blocks);
    std::vector<std::future<Subgraph>> futures;
    futures.reserve(batch_end - batch_start);
    for (std::size_t i = batch_start; i < batch_end; ++i) {
      futures.emplace_back(
          std::async(std::launch::async, [this, &document_blocks, i]() {
            return process_document(document_blocks[i]);
          }));
    }
    for (std::size_t i = 0; i < futures.size(); ++i) {
      subgraphs[batch_start + i] = futures[i].get();
    }
  }
  auto t_procs = clock::now();

  code_graph_.add_root_node(ROOT_NODE);
  merge_subgraphs(subgraphs);
  postprocess();
  auto t_merge = clock::now();

  auto as_ms = [](auto a, auto b) {
    return std::chrono::duration_cast<ms>(b - a).count();
  };
  std::cout << "\n=== C++ decode profile ===\n"
            << "load_metadata:   " << as_ms(t0, t_meta) << " ms\n"
            << "file_read:       " << as_ms(t_meta, t_read) << " ms\n"
            << "extract_blocks:  " << as_ms(t_read, t_extract) << " ms\n"
            << "prescan:         " << as_ms(t_extract, t_prescan) << " ms\n"
            << "process_docs:    " << as_ms(t_prescan, t_procs)
            << " ms  (threads=" << max_threads << ", docs=" << num_blocks
            << ")\n"
            << "merge_subgraphs: " << as_ms(t_procs, t_merge) << " ms\n"
            << "total_wall:      " << as_ms(t0, t_merge) << " ms\n"
            << "==========================\n";
  return std::move(code_graph_);
}

void SCIPDecoderBase::merge_subgraphs(const std::vector<Subgraph> &subgraphs) {
  // Reproduce serial semantics:
  //   - First occurrence of a name takes its attrs (including unified_name).
  //   - Later DEFINITION overwrites all attrs (matches serial
  //     add_symbol_node's unconditional _add_vertex overwrite).
  //   - Later REFERENCE leaves existing attrs alone (matches serial
  //     add_symbol_reference's "only create if missing").
  std::unordered_map<std::string, Subgraph::Node> merged_nodes;
  std::vector<std::tuple<std::string, std::string, std::string,
                         std::optional<std::string>, std::optional<int>>>
      all_edges;

  std::size_t estimated = 0;
  for (const auto &sg : subgraphs)
    estimated += sg.nodes.size();
  merged_nodes.reserve(estimated);
  all_edges.reserve(estimated * 3);

  for (const auto &sg : subgraphs) {
    for (const auto &[name, node] : sg.nodes) {
      auto it = merged_nodes.find(name);
      if (it == merged_nodes.end()) {
        merged_nodes.emplace(name, node);
      } else if (node.is_definition) {
        Subgraph::Node &existing = it->second;
        existing.is_definition = true;
        if (!node.data.type.empty())
          existing.data.type = node.data.type;
        if (node.data.file.has_value())
          existing.data.file = node.data.file;
        if (node.data.start_line.has_value())
          existing.data.start_line = node.data.start_line;
        if (node.data.end_line.has_value())
          existing.data.end_line = node.data.end_line;
        if (node.data.selection_line.has_value())
          existing.data.selection_line = node.data.selection_line;
        if (node.data.unified_name.has_value())
          existing.data.unified_name = node.data.unified_name;
      } else if (!it->second.is_definition && node.updates_unified_name &&
                 node.data.unified_name.has_value()) {
        it->second.data.unified_name = node.data.unified_name;
        it->second.updates_unified_name = true;
      }
      // else: subsequent ref on existing node — leave attrs alone.
    }
    for (const auto &e : sg.edges) {
      all_edges.emplace_back(e.source, e.target, e.type, e.anchor_file,
                             e.anchor_line);
    }
  }

  std::vector<CodeGraph::VertexData> flat_nodes;
  flat_nodes.reserve(merged_nodes.size());
  for (auto &[name, node] : merged_nodes) {
    flat_nodes.emplace_back(std::move(node.data));
  }

  code_graph_.batch_upsert_nodes(flat_nodes);
  code_graph_.batch_add_edges(all_edges);
}

} // namespace codenib::core
