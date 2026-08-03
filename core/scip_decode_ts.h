/*
 * SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#pragma once

#include "scip_decode_base.h"

#include <unordered_map>
#include <unordered_set>

namespace codenib::core {

class SCIPTSDecoder : public SCIPDecoderBase {
public:
  using SCIPDecoderBase::SCIPDecoderBase;

protected:
  Subgraph process_document(const std::string &document_block) const override;
  void prescan(const std::vector<std::string> &document_blocks) override;

private:
  void process_occurrence(const std::string &occurrence_block,
                          const std::string &file_path,
                          SubgraphBuilder &builder) const;
  void process_symbol(const std::string &symbol, int line, int symbol_roles,
                      const std::vector<int> &enclosing_ranges,
                      const std::string &file_path,
                      SubgraphBuilder &builder) const;

  static std::string extract_symbol_display(const std::string &unified);
  static std::string unify_symbol_name(const std::string &cleaned_symbol,
                                       const std::string &original_symbol,
                                       const std::string &file_path);
  static std::string classify_symbol_type(const std::string &unified,
                                          const std::string &original);
  std::string unified_name(const std::string &unified,
                           const std::string &file_path,
                           const std::string &symbol_type) const;

  std::unordered_set<std::string> project_packages_;
  std::unordered_map<std::string, std::string> symbol_kinds_;
};

} // namespace codenib::core
