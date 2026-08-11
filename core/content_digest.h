/*
 * SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <string>
#include <string_view>

namespace codenib::core {

// Small dependency-free streaming SHA-256 implementation used for content
// receipts. Only byte updates and hexadecimal output are public so receipt
// callers cannot depend on digest internals.
class Sha256 {
public:
  Sha256();

  void update(const void *data, std::size_t size);
  void update(std::string_view value) { update(value.data(), value.size()); }

  std::string hex_digest() const;

private:
  void transform(const std::uint8_t *block);

  std::array<std::uint32_t, 8> state_;
  std::array<std::uint8_t, 64> buffer_{};
  std::size_t buffer_size_{0};
  std::uint64_t total_bytes_{0};
};

std::string sha256_hex(std::string_view value);

} // namespace codenib::core
