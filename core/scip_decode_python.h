/*
 * SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#pragma once

#include "scip_decode_base.h"

namespace codenib::core {

class SCIPPythonDecoder : public SCIPDecoderBase {
public:
  using SCIPDecoderBase::SCIPDecoderBase;

protected:
  Subgraph process_document(const std::string &document_block) const override;
  void postprocess() override;

private:
  void process_occurrence(const std::string &occurrence_block,
                          SubgraphBuilder &builder) const;
  void process_symbol(const std::string &symbol, int line, int symbol_roles,
                      const std::vector<int> &enclosing_ranges,
                      SubgraphBuilder &builder) const;

  std::string unify_symbol_name(const std::string &symbol) const;
  std::string classify_symbol_type(const std::string &unified_symbol,
                                   const std::string &original_symbol) const;
};

} // namespace codenib::core
