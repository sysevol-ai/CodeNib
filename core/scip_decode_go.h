/*
 * SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#pragma once

#include "scip_decode_base.h"

namespace codeminer::core {

class SCIPGoDecoder : public SCIPDecoderBase {
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
                      int line, int symbol_roles,
                      const std::vector<int> &enclosing_ranges,
                      SubgraphBuilder &builder) const;

  // Key + classification helpers (pure).
  std::string make_symbol_key(const std::string &symbol) const;
  static std::string classify_symbol_type(const std::string &original_symbol);
  static std::string extract_symbol_display(const std::string &symbol_key,
                                            const std::string &symbol_type);
  std::string unified_name(const std::string &symbol_key,
                           const std::string &file_path,
                           const std::string &symbol_type) const;

  std::string internal_module_;
};

} // namespace codeminer::core
