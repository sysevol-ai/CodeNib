/*
 * SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#include "content_digest.h"

#include <cassert>
#include <iostream>
#include <string>

int main() {
  using codenib::core::Sha256;
  using codenib::core::sha256_hex;

  assert(sha256_hex("") == "e3b0c44298fc1c149afbf4c8996fb924"
                           "27ae41e4649b934ca495991b7852b855");
  assert(sha256_hex("abc") == "ba7816bf8f01cfea414140de5dae2223"
                              "b00361a396177a9cb410ff61f20015ad");
  assert(sha256_hex("abcdbcdecdefdefgefghfghighijhijk"
                    "ijkljklmklmnlmnomnopnopq") ==
         "248d6a61d20638b8e5c026930c3e6039"
         "a33ce45964ff2167f6ecedd419db06c1");
  assert(sha256_hex(std::string(1'000'000, 'a')) ==
         "cdc76e5c9914fb9281a1c7e284d73e67"
         "f1809a48a497200e046d39ccc7112cd0");

  Sha256 incremental;
  incremental.update("a");
  incremental.update("b");
  incremental.update("c");
  assert(incremental.hex_digest() == sha256_hex("abc"));
  assert(incremental.hex_digest() == sha256_hex("abc"));

  const std::string block_boundary(65, 'x');
  Sha256 split_boundary;
  split_boundary.update(block_boundary.data(), 63);
  split_boundary.update(block_boundary.data() + 63, 2);
  assert(split_boundary.hex_digest() == sha256_hex(block_boundary));

  std::cout << "content_digest_test: OK\n";
  return 0;
}
