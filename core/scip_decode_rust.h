/*
 * SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#pragma once

#include "scip_decode_base.h"

#include <unordered_set>

namespace codeminer::core {

class SCIPRustDecoder : public SCIPDecoderBase {
public:
  using SCIPDecoderBase::SCIPDecoderBase;

protected:
  Subgraph process_document(const std::string &document_block) const override;
  void load_metadata() override;

private:
  void process_occurrence(const std::string &occurrence_block,
                          const std::string &file_path,
                          SubgraphBuilder &builder) const;
  void process_symbol(const std::string &symbol, const std::string &file_path,
                      int line, int col_start, int col_end, int symbol_roles,
                      const std::vector<int> &enclosing_ranges,
                      SubgraphBuilder &builder) const;

  static std::string unify_symbol_name(const std::string &symbol);
  static std::string classify_symbol_type(const std::string &unified,
                                          const std::string &original);
  static std::string extract_impl_display(const std::string &impl_member,
                                          const std::string &symbol_type);
  static std::string extract_symbol_display(const std::string &unified,
                                            const std::string &symbol_type);
  std::string unified_name(const std::string &unified,
                           const std::string &file_path,
                           const std::string &symbol_type) const;

  std::unordered_set<std::string> internal_crates_;
};

} // namespace codeminer::core
