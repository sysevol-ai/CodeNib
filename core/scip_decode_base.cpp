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
#include <string_view>
#include <thread>
#include <unordered_map>
#include <unordered_set>

namespace codenib::core {

namespace {

bool debug_logging_enabled() {
  static const bool enabled = std::getenv("CODENIB_SCIP_DEBUG") != nullptr;
  return enabled;
}

bool profile_logging_enabled() {
  const char *value = std::getenv("CODENIB_CORE_PROFILE");
  return value != nullptr && std::string_view(value) == "1";
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

SCIPDecodedRecords SCIPDecoderBase::decode_records_impl() {
  using clock = std::chrono::steady_clock;
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

  SCIPDecodedRecords records = merge_subgraphs(subgraphs);
  postprocess_records(records);
  auto t_merge = clock::now();

  auto as_ns = [](auto a, auto b) {
    return static_cast<std::uint64_t>(
        std::chrono::duration_cast<std::chrono::nanoseconds>(b - a).count());
  };
  last_profile_ = SCIPDecodeProfile{};
  last_profile_.load_metadata_ns = as_ns(t0, t_meta);
  last_profile_.file_read_ns = as_ns(t_meta, t_read);
  last_profile_.extract_blocks_ns = as_ns(t_read, t_extract);
  last_profile_.prescan_ns = as_ns(t_extract, t_prescan);
  last_profile_.process_documents_ns = as_ns(t_prescan, t_procs);
  last_profile_.merge_subgraphs_ns = as_ns(t_procs, t_merge);
  last_profile_.total_ns = as_ns(t0, t_merge);
  last_profile_.worker_count = max_threads;
  last_profile_.document_count = num_blocks;
  return records;
}

SCIPDecodedRecords SCIPDecoderBase::decode_records() {
  SCIPDecodedRecords records = decode_records_impl();
  log_profile();
  return records;
}

CodeGraph SCIPDecoderBase::decode() {
  using clock = std::chrono::steady_clock;
  SCIPDecodedRecords records = decode_records_impl();
  const auto materialize_started = clock::now();
  code_graph_.batch_upsert_nodes(records.vertices);
  code_graph_.batch_add_indexed_edges(records.edges);
  const auto materialize_finished = clock::now();
  const auto materialize_ns = static_cast<std::uint64_t>(
      std::chrono::duration_cast<std::chrono::nanoseconds>(
          materialize_finished - materialize_started)
          .count());
  last_profile_.materialize_graph_ns = materialize_ns;
  last_profile_.total_ns += materialize_ns;
  log_profile();
  return std::move(code_graph_);
}

void SCIPDecoderBase::log_profile() const {
  if (profile_logging_enabled()) {
    auto ms = [](std::uint64_t ns) {
      return static_cast<double>(ns) / 1'000'000.0;
    };
    std::cout << "\n=== C++ decode profile ===\n"
              << "load_metadata:   " << ms(last_profile_.load_metadata_ns)
              << " ms\n"
              << "file_read:       " << ms(last_profile_.file_read_ns)
              << " ms\n"
              << "extract_blocks:  " << ms(last_profile_.extract_blocks_ns)
              << " ms\n"
              << "prescan:         " << ms(last_profile_.prescan_ns) << " ms\n"
              << "process_docs:    " << ms(last_profile_.process_documents_ns)
              << " ms  (threads=" << last_profile_.worker_count
              << ", docs=" << last_profile_.document_count << ")\n"
              << "merge_subgraphs: " << ms(last_profile_.merge_subgraphs_ns)
              << " ms\n"
              << "materialize_graph: " << ms(last_profile_.materialize_graph_ns)
              << " ms\n"
              << "total_wall:      " << ms(last_profile_.total_ns) << " ms\n"
              << "==========================\n";
  }
}

SCIPDecodedRecords
SCIPDecoderBase::merge_subgraphs(const std::vector<Subgraph> &subgraphs) const {
  // Reproduce serial semantics:
  //   - First occurrence of a name takes its attrs (including unified_name).
  //   - Later DEFINITION overwrites all attrs (matches serial
  //     add_symbol_node's unconditional _add_vertex overwrite).
  //   - Later FILE/DIRECTORY insertion overwrites only type (matches serial
  //     structural-node upserts when a path collides with a symbol name).
  //   - Later REFERENCE leaves existing attrs alone (matches serial
  //     add_symbol_reference's "only create if missing").
  std::unordered_map<std::string, Subgraph::Node> merged_nodes;
  std::vector<Subgraph::Edge> all_edges;

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
      } else {
        if (node.is_definition) {
          Subgraph::Node &existing = it->second;
          existing.is_definition = true;
          existing.data.has_definition = true;
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
          if (node.data.symbol_kind.has_value())
            existing.data.symbol_kind = node.data.symbol_kind;
        } else {
          if (node.updates_structural_type && !node.data.type.empty()) {
            it->second.data.type = node.data.type;
            it->second.updates_structural_type = true;
          }
          if (!it->second.is_definition && node.updates_unified_name &&
              node.data.unified_name.has_value()) {
            it->second.data.unified_name = node.data.unified_name;
            it->second.updates_unified_name = true;
          }
        }
        if (!it->second.data.symbol_kind.has_value() &&
            node.data.symbol_kind.has_value()) {
          it->second.data.symbol_kind = node.data.symbol_kind;
        }
      }
      // else: subsequent ref on existing node — leave attrs alone.
    }
    for (const auto &e : sg.edges) {
      all_edges.push_back(e);
    }
  }

  SCIPDecodedRecords records;
  records.project_root = project_root_.value_or(std::string{});
  records.vertices.reserve(merged_nodes.size() + 1);
  CodeGraph::VertexData root;
  root.name = ROOT_NODE;
  root.type = "root";
  records.vertices.push_back(std::move(root));
  for (auto &[name, node] : merged_nodes) {
    if (name != ROOT_NODE)
      records.vertices.emplace_back(std::move(node.data));
  }

  std::unordered_map<std::string, CodeGraph::VertexId> vertex_ids;
  vertex_ids.reserve(records.vertices.size());
  for (std::size_t index = 0; index < records.vertices.size(); ++index) {
    vertex_ids.emplace(records.vertices[index].name,
                       static_cast<CodeGraph::VertexId>(index));
  }

  struct StructuralKey {
    CodeGraph::VertexId source;
    CodeGraph::VertexId target;
    bool operator==(const StructuralKey &other) const {
      return source == other.source && target == other.target;
    }
  };
  struct StructuralHash {
    std::size_t operator()(const StructuralKey &key) const {
      return std::hash<CodeGraph::VertexId>{}(key.source) ^
             (std::hash<CodeGraph::VertexId>{}(key.target) << 1U);
    }
  };
  struct AnchoredKey {
    CodeGraph::VertexId source;
    CodeGraph::VertexId target;
    std::string type;
    std::optional<std::string> anchor_file;
    std::optional<int> anchor_line;
    bool operator==(const AnchoredKey &other) const {
      return source == other.source && target == other.target &&
             type == other.type && anchor_file == other.anchor_file &&
             anchor_line == other.anchor_line;
    }
  };
  struct AnchoredHash {
    std::size_t operator()(const AnchoredKey &key) const {
      std::size_t hash = std::hash<CodeGraph::VertexId>{}(key.source);
      hash ^= std::hash<CodeGraph::VertexId>{}(key.target) << 1U;
      hash ^= std::hash<std::string>{}(key.type) << 2U;
      if (key.anchor_file.has_value())
        hash ^= std::hash<std::string>{}(*key.anchor_file) << 3U;
      if (key.anchor_line.has_value())
        hash ^= std::hash<int>{}(*key.anchor_line) << 4U;
      return hash;
    }
  };

  std::unordered_set<StructuralKey, StructuralHash> structural_edges;
  std::unordered_set<AnchoredKey, AnchoredHash> anchored_edges;
  structural_edges.reserve(all_edges.size());
  anchored_edges.reserve(all_edges.size());
  records.edges.reserve(all_edges.size());
  for (const auto &edge : all_edges) {
    const auto source = vertex_ids.find(edge.source);
    const auto target = vertex_ids.find(edge.target);
    if (source == vertex_ids.end() || target == vertex_ids.end())
      continue;
    const bool structural =
        !edge.anchor_file.has_value() && !edge.anchor_line.has_value();
    if (structural) {
      if (!structural_edges
               .insert(StructuralKey{source->second, target->second})
               .second)
        continue;
    } else if (!anchored_edges
                    .insert(AnchoredKey{source->second, target->second,
                                        edge.type, edge.anchor_file,
                                        edge.anchor_line})
                    .second) {
      continue;
    }
    records.edges.push_back(CodeGraph::EdgeData{source->second, target->second,
                                                edge.type, edge.anchor_file,
                                                edge.anchor_line});
  }
  return records;
}

} // namespace codenib::core
