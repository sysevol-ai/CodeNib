/*
 * SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#pragma once

#include "scip_decode_base.h"

#include <memory>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

namespace codenib::core {

std::vector<std::string> canonical_scip_decoder_languages();
std::vector<std::string> accepted_scip_decoder_languages();
std::string accepted_scip_decoder_languages_help();

std::optional<std::string>
canonical_scip_decoder_language(std::string_view language);
bool is_scip_decoder_language_supported(std::string_view language);

std::unique_ptr<SCIPDecoderBase>
make_scip_decoder(std::string_view language, const std::string &index_file,
                  std::optional<std::string> project_root = std::nullopt);

} // namespace codenib::core
