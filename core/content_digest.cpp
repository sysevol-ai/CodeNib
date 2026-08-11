/*
 * SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#include "content_digest.h"

#include <algorithm>
#include <limits>
#include <stdexcept>

namespace codenib::core {
namespace {

constexpr std::array<std::uint32_t, 64> ROUND_CONSTANTS{
    0x428a2f98U, 0x71374491U, 0xb5c0fbcfU, 0xe9b5dba5U, 0x3956c25bU,
    0x59f111f1U, 0x923f82a4U, 0xab1c5ed5U, 0xd807aa98U, 0x12835b01U,
    0x243185beU, 0x550c7dc3U, 0x72be5d74U, 0x80deb1feU, 0x9bdc06a7U,
    0xc19bf174U, 0xe49b69c1U, 0xefbe4786U, 0x0fc19dc6U, 0x240ca1ccU,
    0x2de92c6fU, 0x4a7484aaU, 0x5cb0a9dcU, 0x76f988daU, 0x983e5152U,
    0xa831c66dU, 0xb00327c8U, 0xbf597fc7U, 0xc6e00bf3U, 0xd5a79147U,
    0x06ca6351U, 0x14292967U, 0x27b70a85U, 0x2e1b2138U, 0x4d2c6dfcU,
    0x53380d13U, 0x650a7354U, 0x766a0abbU, 0x81c2c92eU, 0x92722c85U,
    0xa2bfe8a1U, 0xa81a664bU, 0xc24b8b70U, 0xc76c51a3U, 0xd192e819U,
    0xd6990624U, 0xf40e3585U, 0x106aa070U, 0x19a4c116U, 0x1e376c08U,
    0x2748774cU, 0x34b0bcb5U, 0x391c0cb3U, 0x4ed8aa4aU, 0x5b9cca4fU,
    0x682e6ff3U, 0x748f82eeU, 0x78a5636fU, 0x84c87814U, 0x8cc70208U,
    0x90befffaU, 0xa4506cebU, 0xbef9a3f7U, 0xc67178f2U};

std::uint32_t rotate_right(std::uint32_t value, unsigned int bits) {
  return (value >> bits) | (value << (32U - bits));
}

} // namespace

Sha256::Sha256()
    : state_{{0x6a09e667U, 0xbb67ae85U, 0x3c6ef372U, 0xa54ff53aU, 0x510e527fU,
              0x9b05688cU, 0x1f83d9abU, 0x5be0cd19U}} {}

void Sha256::update(const void *data, std::size_t size) {
  if (size == 0)
    return;
  if (data == nullptr)
    throw std::invalid_argument("SHA-256 update data must not be null");
  if (size > std::numeric_limits<std::uint64_t>::max() - total_bytes_)
    throw std::overflow_error("SHA-256 input length exceeds uint64");

  const auto *bytes = static_cast<const std::uint8_t *>(data);
  total_bytes_ += static_cast<std::uint64_t>(size);
  if (buffer_size_ != 0) {
    const auto count = std::min(size, buffer_.size() - buffer_size_);
    std::copy_n(bytes, count, buffer_.begin() + buffer_size_);
    buffer_size_ += count;
    bytes += count;
    size -= count;
    if (buffer_size_ == buffer_.size()) {
      transform(buffer_.data());
      buffer_size_ = 0;
    }
  }
  while (size >= buffer_.size()) {
    transform(bytes);
    bytes += buffer_.size();
    size -= buffer_.size();
  }
  if (size != 0) {
    std::copy_n(bytes, size, buffer_.begin());
    buffer_size_ = size;
  }
}

void Sha256::transform(const std::uint8_t *block) {
  std::array<std::uint32_t, 64> words;
  for (std::size_t index = 0; index < 16; ++index) {
    const auto offset = index * 4;
    words[index] = (static_cast<std::uint32_t>(block[offset]) << 24U) |
                   (static_cast<std::uint32_t>(block[offset + 1]) << 16U) |
                   (static_cast<std::uint32_t>(block[offset + 2]) << 8U) |
                   static_cast<std::uint32_t>(block[offset + 3]);
  }
  for (std::size_t index = 16; index < words.size(); ++index) {
    const auto value15 = words[index - 15];
    const auto value2 = words[index - 2];
    const auto sigma0 =
        rotate_right(value15, 7) ^ rotate_right(value15, 18) ^ (value15 >> 3U);
    const auto sigma1 =
        rotate_right(value2, 17) ^ rotate_right(value2, 19) ^ (value2 >> 10U);
    words[index] = words[index - 16] + sigma0 + words[index - 7] + sigma1;
  }

  auto a = state_[0];
  auto b = state_[1];
  auto c = state_[2];
  auto d = state_[3];
  auto e = state_[4];
  auto f = state_[5];
  auto g = state_[6];
  auto h = state_[7];
  for (std::size_t index = 0; index < words.size(); ++index) {
    const auto sum1 =
        rotate_right(e, 6) ^ rotate_right(e, 11) ^ rotate_right(e, 25);
    const auto choose = (e & f) ^ ((~e) & g);
    const auto temporary1 =
        h + sum1 + choose + ROUND_CONSTANTS[index] + words[index];
    const auto sum0 =
        rotate_right(a, 2) ^ rotate_right(a, 13) ^ rotate_right(a, 22);
    const auto majority = (a & b) ^ (a & c) ^ (b & c);
    const auto temporary2 = sum0 + majority;
    h = g;
    g = f;
    f = e;
    e = d + temporary1;
    d = c;
    c = b;
    b = a;
    a = temporary1 + temporary2;
  }
  state_[0] += a;
  state_[1] += b;
  state_[2] += c;
  state_[3] += d;
  state_[4] += e;
  state_[5] += f;
  state_[6] += g;
  state_[7] += h;
}

std::string Sha256::hex_digest() const {
  Sha256 finalized = *this;
  const auto bit_count = finalized.total_bytes_ * 8U;
  std::array<std::uint8_t, 64> padding{};
  padding[0] = 0x80U;
  const auto padding_size = finalized.buffer_size_ < 56
                                ? 56 - finalized.buffer_size_
                                : 120 - finalized.buffer_size_;
  finalized.update(padding.data(), padding_size);

  std::array<std::uint8_t, 8> length{};
  for (std::size_t index = 0; index < length.size(); ++index) {
    length[length.size() - 1 - index] =
        static_cast<std::uint8_t>(bit_count >> (index * 8U));
  }
  finalized.update(length.data(), length.size());

  static constexpr std::array<char, 16> digits{{'0', '1', '2', '3', '4', '5',
                                                '6', '7', '8', '9', 'a', 'b',
                                                'c', 'd', 'e', 'f'}};
  std::string result;
  result.reserve(64);
  for (const auto word : finalized.state_) {
    for (int shift = 28; shift >= 0; shift -= 4)
      result.push_back(digits[(word >> shift) & 0x0fU]);
  }
  return result;
}

std::string sha256_hex(std::string_view value) {
  Sha256 digest;
  digest.update(value);
  return digest.hex_digest();
}

} // namespace codenib::core
