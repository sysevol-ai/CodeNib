/*
 * SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#include "python_chunk_batch_poc.h"

#include <cassert>
#include <cstdint>
#include <future>
#include <iostream>
#include <stdexcept>
#include <string>
#include <tuple>
#include <utility>
#include <vector>

namespace {

using codenib::core::PythonChunkBatchBuffer;

std::uint32_t read_u32(const std::vector<std::uint8_t> &buffer,
                       std::size_t offset) {
  assert(offset + 4 <= buffer.size());
  return static_cast<std::uint32_t>(buffer[offset]) |
         (static_cast<std::uint32_t>(buffer[offset + 1]) << 8U) |
         (static_cast<std::uint32_t>(buffer[offset + 2]) << 16U) |
         (static_cast<std::uint32_t>(buffer[offset + 3]) << 24U);
}

std::vector<std::pair<std::uint32_t, std::uint32_t>>
file_ranges(const PythonChunkBatchBuffer &buffer) {
  std::vector<std::pair<std::uint32_t, std::uint32_t>> result;
  for (std::size_t offset = 0; offset < buffer.file_rows.size(); offset += 8)
    result.emplace_back(read_u32(buffer.file_rows, offset),
                        read_u32(buffer.file_rows, offset + 4));
  return result;
}

std::vector<std::tuple<std::uint32_t, std::uint32_t, std::uint8_t, std::string>>
spans(const PythonChunkBatchBuffer &buffer) {
  std::vector<
      std::tuple<std::uint32_t, std::uint32_t, std::uint8_t, std::string>>
      result;
  for (std::size_t offset = 0; offset < buffer.rows.size(); offset += 20) {
    const auto name_offset = read_u32(buffer.rows, offset + 12);
    const auto name_length = read_u32(buffer.rows, offset + 16);
    assert(static_cast<std::size_t>(name_offset) + name_length <=
           buffer.arena.size());
    result.emplace_back(
        read_u32(buffer.rows, offset), read_u32(buffer.rows, offset + 4),
        buffer.rows[offset + 8],
        std::string(buffer.arena.begin() + name_offset,
                    buffer.arena.begin() + name_offset + name_length));
  }
  return result;
}

void assert_same_data(const PythonChunkBatchBuffer &left,
                      const PythonChunkBatchBuffer &right) {
  assert(left.file_rows == right.file_rows);
  assert(left.rows == right.rows);
  assert(left.arena == right.arena);
  assert(left.source_bytes == right.source_bytes);
  assert(left.file_count == right.file_count);
  assert(left.row_count == right.row_count);
}

template <typename Exception, typename Callable>
void expect_exception(Callable &&callable) {
  bool raised = false;
  try {
    callable();
  } catch (const Exception &) {
    raised = true;
  }
  assert(raised);
}

void test_empty_batch() {
  for (const auto workers : {1U, 2U, 4U}) {
    const auto buffer = codenib::core::extract_python_repository_chunk_buffer(
        {}, 2, true, workers);
    assert(buffer.file_count == 0);
    assert(buffer.source_bytes == 0);
    assert(buffer.row_count == 0);
    assert(buffer.worker_count == workers);
    assert(buffer.file_rows.empty());
    assert(buffer.rows.empty());
    assert(buffer.arena.empty());
  }
}

void test_ordered_repository_batch() {
  const std::vector<std::string> sources = {
      "def first():\n    return 1\n",
      "# deliberately has no definitions\nVALUE = 1\n",
      "@trace\nasync def caf\xC3\xA9():\n    return 2\n",
      "class Service:\n    def run(self):\n        return 3\n",
      "def first():\n    return 1\n",
  };
  const auto one = codenib::core::extract_python_repository_chunk_buffer(
      sources, 2, true, 1);
  const auto two = codenib::core::extract_python_repository_chunk_buffer(
      sources, 2, true, 2);
  const auto four = codenib::core::extract_python_repository_chunk_buffer(
      sources, 2, true, 4);
  assert_same_data(one, two);
  assert_same_data(one, four);
  assert(one.worker_count == 1);
  assert(two.worker_count == 2);
  assert(four.worker_count == 4);
  assert(one.source_bytes == sources[0].size() + sources[1].size() +
                                 sources[2].size() + sources[3].size() +
                                 sources[4].size());
  assert(file_ranges(one) ==
         (std::vector<std::pair<std::uint32_t, std::uint32_t>>{
             {0, 1}, {1, 0}, {1, 1}, {2, 1}, {3, 1}}));
  const auto decoded = spans(one);
  assert(decoded.size() == 4);
  assert(std::get<3>(decoded[0]) == "first");
  assert(std::get<3>(decoded[1]) == "caf\xC3\xA9");
  assert(std::get<3>(decoded[2]) == "Service.run");
  assert(std::get<3>(decoded[3]) == "first");
  assert(std::get<2>(decoded[0]) == 1);
  assert(std::get<2>(decoded[2]) == 3);
  assert(one.file_rows.size() == one.file_count * 8U);
  assert(one.rows.size() == one.row_count * 20U);
  assert(one.batch_wall_ns >= one.ordered_merge_ns);
}

void test_depth_and_recovery_semantics() {
  const std::vector<std::string> sources = {
      "def identity[T](value: T) -> T:\n    return value\n\n"
      "class Box[T]:\n    @classmethod\n    async def create(cls):\n"
      "        return cls()\n",
      "if ENABLED:\n    def conditional():\n        return 1\n\n"
      "class Visible:\n    if ENABLED:\n        def hidden(self):\n"
      "            return 2\n\n    def direct(self):\n        return 3\n",
      "def valid():\n    return 1\n\ndef broken(:\n    missing\n\n"
      "class Recovered:\n    def method(self):\n        return 2\n",
  };
  const auto l1 = codenib::core::extract_python_repository_chunk_buffer(
      sources, 1, true, 2);
  const auto l2_exclusive =
      codenib::core::extract_python_repository_chunk_buffer(sources, 2, true,
                                                            4);
  const auto l2_inclusive =
      codenib::core::extract_python_repository_chunk_buffer(sources, 2, false,
                                                            1);
  std::vector<std::string> l1_names;
  for (const auto &span : spans(l1))
    l1_names.push_back(std::get<3>(span));
  std::vector<std::string> exclusive_names;
  for (const auto &span : spans(l2_exclusive))
    exclusive_names.push_back(std::get<3>(span));
  std::vector<std::string> inclusive_names;
  for (const auto &span : spans(l2_inclusive))
    inclusive_names.push_back(std::get<3>(span));
  assert(
      (l1_names == std::vector<std::string>{"identity", "Box", "Visible",
                                            "valid", "broken", "Recovered"}));
  assert((exclusive_names ==
          std::vector<std::string>{"identity", "Box.create", "Visible.direct",
                                   "valid", "broken", "Recovered.method"}));
  assert((inclusive_names ==
          std::vector<std::string>{"identity", "Box", "Box.create", "Visible",
                                   "Visible.direct", "valid", "broken",
                                   "Recovered", "Recovered.method"}));
}

void test_independent_concurrent_calls() {
  const std::vector<std::string> sources = {
      "def alpha():\n    return 1\n",
      "class Beta:\n    def run(self):\n        return 2\n",
      "@decorator\ndef gamma():\n    return 3\n",
  };
  const auto expected = codenib::core::extract_python_repository_chunk_buffer(
      sources, 2, false, 1);
  for (unsigned repetition = 0; repetition < 100; ++repetition) {
    std::vector<std::future<PythonChunkBatchBuffer>> futures;
    for (unsigned call = 0; call < 6; ++call) {
      futures.emplace_back(std::async(std::launch::async, [sources, call]() {
        const auto workers = call % 2 == 0 ? 2U : 4U;
        return codenib::core::extract_python_repository_chunk_buffer(
            sources, 2, false, workers);
      }));
    }
    for (auto &future : futures)
      assert_same_data(expected, future.get());
  }
}

void test_validation() {
  expect_exception<std::invalid_argument>([] {
    codenib::core::extract_python_repository_chunk_buffer({}, 0, true, 1);
  });
  expect_exception<std::invalid_argument>([] {
    codenib::core::extract_python_repository_chunk_buffer({}, 3, true, 1);
  });
  for (const auto workers : {0U, 3U, 8U}) {
    expect_exception<std::invalid_argument>([workers] {
      codenib::core::extract_python_repository_chunk_buffer({}, 2, true,
                                                            workers);
    });
  }
  std::vector<std::string> too_many(
      codenib::core::PYTHON_CHUNK_BATCH_MAX_FILE_COUNT + 1U);
  expect_exception<std::overflow_error>([&too_many] {
    codenib::core::extract_python_repository_chunk_buffer(too_many, 2, true, 1);
  });
}

} // namespace

int main() {
  static_assert(codenib::core::PYTHON_CHUNK_BATCH_ABI_VERSION == 1);
  static_assert(codenib::core::PYTHON_CHUNK_BATCH_FILE_ROW_SIZE == 8);
  static_assert(codenib::core::PYTHON_CHUNK_BATCH_SPAN_ROW_SIZE == 20);
  test_empty_batch();
  test_ordered_repository_batch();
  test_depth_and_recovery_semantics();
  test_independent_concurrent_calls();
  test_validation();
  std::cout << "python_chunk_batch_poc_test: OK\n";
  return 0;
}
