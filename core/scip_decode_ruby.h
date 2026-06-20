/*
 * SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#pragma once

#include "scip_decode_base.h"

#include <optional>
#include <string>
#include <unordered_map>
#include <vector>

namespace codeminer::core {

class SCIPRubyDecoder : public SCIPDecoderBase {
public:
  using SCIPDecoderBase::SCIPDecoderBase;

protected:
  Subgraph process_document(const std::string &document_block) const override;
  void prescan(const std::vector<std::string> &document_blocks) override;

private:
  struct SymbolInfo {
    std::string key;
    std::string file_path;
    std::string display_name;
    std::string symbol_type;
  };

  struct DefinitionInfo {
    std::string key;
    int line{0};
    std::string symbol_type;
  };

  struct MethodDefinition {
    int line{0};
    std::string key;
    std::string symbol_type;
  };

  struct DocumentState {
    std::string file_path;
    std::size_t document_order{0};
    std::unordered_map<std::string, std::string> file_definition_keys;
    std::unordered_map<std::string, std::vector<MethodDefinition>>
        method_definitions_by_member;
    std::vector<DefinitionInfo> definitions;
    std::vector<std::string> source_lines;
  };

  void collect_symbol_info(const std::string &document_block,
                           const std::string &file_path);
  std::vector<DefinitionInfo>
  collect_document_definitions(const std::string &document_block,
                               const std::string &file_path) const;
  void process_occurrence(const std::string &occurrence_block,
                          const std::string &file_path, DocumentState &state,
                          SubgraphBuilder &builder) const;
  void add_definition(const SymbolInfo &info, const std::string &file_path,
                      int line, const std::vector<int> &enclosing_ranges,
                      DocumentState &state, SubgraphBuilder &builder) const;
  void add_reference(const SymbolInfo &info, int anchor_line,
                     const DocumentState &state,
                     SubgraphBuilder &builder) const;
  void add_alias_definitions(const std::string &file_path, DocumentState &state,
                             SubgraphBuilder &builder) const;

  std::optional<SymbolInfo>
  symbol_info_for_key(const std::string &key, const std::string &file_path,
                      const std::string &original_symbol) const;

  std::string definition_display_name(const SymbolInfo &info,
                                      const std::string &file_path, int line,
                                      const DocumentState &state,
                                      const SubgraphBuilder &builder) const;
  std::optional<std::string>
  nested_method_display_name(const SymbolInfo &info,
                             const std::string &file_path,
                             const SubgraphBuilder &builder) const;
  std::optional<std::string>
  current_method_scope_display(const std::string &file_path,
                               const SubgraphBuilder &builder) const;
  std::string top_level_singleton_display_name(
      const SymbolInfo &info, const std::string &file_path, int line,
      std::string display_name, const DocumentState &state) const;
  std::string source_line(const DocumentState &state, int line) const;
  std::string containment_parent_in_file(const std::string &key,
                                         const std::string &file_path,
                                         const DocumentState &state,
                                         const SubgraphBuilder &builder) const;
  std::optional<std::string>
  current_method_scope_vertex(const std::string &file_path,
                              const SubgraphBuilder &builder) const;
  std::optional<std::string>
  reference_source(const std::string &file_path, int anchor_line,
                   const DocumentState &state,
                   const SubgraphBuilder &builder) const;

  std::vector<std::string>
  read_source_lines(const std::string &file_path) const;

  std::unordered_map<std::string, SymbolInfo> symbols_;
  std::unordered_map<std::string, std::vector<DefinitionInfo>>
      definition_lines_by_file_;
  std::unordered_map<std::string, std::string> definition_graph_keys_;
  std::unordered_map<std::string, std::size_t> document_order_by_file_;
};

} // namespace codeminer::core
