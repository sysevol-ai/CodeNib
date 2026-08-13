/*
 * SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#include "python_chunk_batch_poc.h"

#include "python_chunk_poc.h"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <exception>
#include <limits>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <string_view>
#include <thread>
#include <utility>
#include <vector>

namespace codenib::core {
namespace {

using Clock = std::chrono::steady_clock;

std::uint64_t elapsed_ns(Clock::time_point start, Clock::time_point end) {
  return static_cast<std::uint64_t>(
      std::chrono::duration_cast<std::chrono::nanoseconds>(end - start)
          .count());
}

std::uint64_t checked_add(std::uint64_t left, std::uint64_t right,
                          const char *label) {
  if (right > std::numeric_limits<std::uint64_t>::max() - left)
    throw std::overflow_error(label);
  return left + right;
}

std::size_t checked_table_size(std::size_t count, std::size_t row_size,
                               const char *label) {
  if (count > std::numeric_limits<std::size_t>::max() / row_size)
    throw std::overflow_error(label);
  return count * row_size;
}

void append_u32(std::vector<std::uint8_t> &output, std::uint32_t value) {
  for (unsigned shift = 0; shift < 32; shift += 8)
    output.push_back(static_cast<std::uint8_t>((value >> shift) & 0xffU));
}

std::uint32_t read_u32(const std::vector<std::uint8_t> &input,
                       std::size_t offset) {
  if (offset > input.size() || input.size() - offset < sizeof(std::uint32_t))
    throw std::runtime_error("native Python chunk row is truncated");
  std::uint32_t value = 0;
  for (unsigned shift = 0; shift < 32; shift += 8)
    value |= static_cast<std::uint32_t>(input[offset + shift / 8]) << shift;
  return value;
}

void write_u32(std::vector<std::uint8_t> &output, std::size_t offset,
               std::uint32_t value) {
  if (offset > output.size() || output.size() - offset < sizeof(std::uint32_t))
    throw std::runtime_error("native Python chunk row is truncated");
  for (unsigned shift = 0; shift < 32; shift += 8)
    output[offset + shift / 8] =
        static_cast<std::uint8_t>((value >> shift) & 0xffU);
}

std::uint64_t source_line_count(std::string_view source) {
  return 1U + static_cast<std::uint64_t>(
                  std::count(source.begin(), source.end(), '\n'));
}

void validate_local_buffer(const PythonChunkBuffer &buffer,
                           std::string_view source) {
  const auto expected_size =
      checked_table_size(buffer.row_count, PYTHON_CHUNK_BATCH_SPAN_ROW_SIZE,
                         "native Python chunk row table exceeds size_t");
  if (buffer.rows.size() != expected_size)
    throw std::runtime_error("native Python chunk row table size mismatch");

  const auto line_count = source_line_count(source);
  std::optional<std::uint32_t> previous_start;
  for (std::size_t offset = 0; offset < buffer.rows.size();
       offset += PYTHON_CHUNK_BATCH_SPAN_ROW_SIZE) {
    const auto start_line = read_u32(buffer.rows, offset);
    const auto end_line = read_u32(buffer.rows, offset + 4);
    const auto kind = buffer.rows[offset + 8];
    const auto name_offset = read_u32(buffer.rows, offset + 12);
    const auto name_length = read_u32(buffer.rows, offset + 16);
    if (start_line > end_line || end_line >= line_count)
      throw std::runtime_error(
          "native Python chunk span exceeds source bounds");
    if (previous_start.has_value() && start_line < *previous_start)
      throw std::runtime_error("native Python chunk spans are not ordered");
    previous_start = start_line;
    if (kind < 1 || kind > 3)
      throw std::runtime_error("native Python chunk kind is invalid");
    if (buffer.rows[offset + 9] != 0 || buffer.rows[offset + 10] != 0 ||
        buffer.rows[offset + 11] != 0)
      throw std::runtime_error("native Python chunk span padding is invalid");
    if (name_length == 0 || name_offset > buffer.arena.size() ||
        name_length > buffer.arena.size() - name_offset)
      throw std::runtime_error("native Python chunk name exceeds arena bounds");
  }
}

void join_workers(std::vector<std::thread> &workers) noexcept {
  for (auto &worker : workers) {
    if (worker.joinable())
      worker.join();
  }
}

} // namespace

PythonChunkBatchBuffer
extract_python_repository_chunk_buffer(const std::vector<std::string> &sources,
                                       int chunk_depth, bool l2_level_exclusive,
                                       std::size_t worker_count) {
  const auto batch_started = Clock::now();
  if (chunk_depth != 1 && chunk_depth != 2)
    throw std::invalid_argument(
        "native Python repository extraction supports chunk_depth 1 or 2");
  if (worker_count != 1 && worker_count != 2 && worker_count != 4)
    throw std::invalid_argument(
        "native Python repository extraction supports 1, 2, or 4 workers");
  if (sources.size() > PYTHON_CHUNK_BATCH_MAX_FILE_COUNT)
    throw std::overflow_error("native Python repository file cap exceeded");

  std::uint64_t total_source_bytes = 0;
  for (const auto &source : sources) {
    if (source.size() > std::numeric_limits<std::uint32_t>::max())
      throw std::overflow_error(
          "Python source exceeds tree-sitter's u32 limit");
    total_source_bytes = checked_add(
        total_source_bytes, static_cast<std::uint64_t>(source.size()),
        "native Python repository source byte count overflowed");
    if (total_source_bytes > PYTHON_CHUNK_BATCH_MAX_SOURCE_BYTES)
      throw std::overflow_error("native Python repository source cap exceeded");
  }

  std::vector<std::optional<PythonChunkBuffer>> local_buffers(sources.size());
  std::atomic<std::size_t> next_ordinal{0};
  std::atomic<bool> cancelled{false};
  std::mutex failure_mutex;
  std::exception_ptr failure;
  auto run_worker = [&]() {
    try {
      while (!cancelled.load(std::memory_order_relaxed)) {
        const auto ordinal =
            next_ordinal.fetch_add(1, std::memory_order_relaxed);
        if (ordinal >= sources.size())
          return;
        local_buffers[ordinal].emplace(extract_python_chunk_buffer(
            sources[ordinal], chunk_depth, l2_level_exclusive));
      }
    } catch (...) {
      {
        std::lock_guard<std::mutex> lock(failure_mutex);
        if (!failure)
          failure = std::current_exception();
      }
      cancelled.store(true, std::memory_order_relaxed);
    }
  };

  std::vector<std::thread> workers;
  workers.reserve(worker_count);
  try {
    for (std::size_t index = 0; index < worker_count; ++index)
      workers.emplace_back(run_worker);
  } catch (...) {
    cancelled.store(true, std::memory_order_relaxed);
    join_workers(workers);
    throw;
  }
  join_workers(workers);
  if (failure)
    std::rethrow_exception(failure);

  std::uint64_t total_rows = 0;
  std::uint64_t total_arena_bytes = 0;
  std::uint64_t parse_work_ns = 0;
  std::uint64_t extract_encode_work_ns = 0;
  for (std::size_t ordinal = 0; ordinal < sources.size(); ++ordinal) {
    if (!local_buffers[ordinal].has_value())
      throw std::runtime_error(
          "native Python repository worker omitted a file");
    const auto &buffer = *local_buffers[ordinal];
    validate_local_buffer(buffer, sources[ordinal]);
    total_rows = checked_add(total_rows, buffer.row_count,
                             "native Python repository row count overflowed");
    total_arena_bytes = checked_add(
        total_arena_bytes, static_cast<std::uint64_t>(buffer.arena.size()),
        "native Python repository arena size overflowed");
    parse_work_ns = checked_add(parse_work_ns, buffer.parse_ns,
                                "native Python parse work overflowed");
    extract_encode_work_ns =
        checked_add(extract_encode_work_ns, buffer.extract_ns,
                    "native Python extract/encode work overflowed");
    if (total_rows > PYTHON_CHUNK_BATCH_MAX_ROW_COUNT)
      throw std::overflow_error("native Python repository row cap exceeded");
    if (total_arena_bytes > PYTHON_CHUNK_BATCH_MAX_ARENA_BYTES)
      throw std::overflow_error("native Python repository arena cap exceeded");
  }

  PythonChunkBatchBuffer result;
  result.source_bytes = total_source_bytes;
  result.file_count = static_cast<std::uint32_t>(sources.size());
  result.row_count = static_cast<std::uint32_t>(total_rows);
  result.worker_count = static_cast<std::uint32_t>(worker_count);
  result.parse_work_ns = parse_work_ns;
  result.extract_encode_work_ns = extract_encode_work_ns;
  result.file_rows.reserve(
      checked_table_size(sources.size(), PYTHON_CHUNK_BATCH_FILE_ROW_SIZE,
                         "native Python repository file table exceeds size_t"));
  result.rows.reserve(checked_table_size(
      static_cast<std::size_t>(total_rows), PYTHON_CHUNK_BATCH_SPAN_ROW_SIZE,
      "native Python repository span table exceeds size_t"));
  result.arena.reserve(static_cast<std::size_t>(total_arena_bytes));

  const auto merge_started = Clock::now();
  std::uint32_t row_offset = 0;
  for (const auto &local : local_buffers) {
    const auto &buffer = *local;
    append_u32(result.file_rows, row_offset);
    append_u32(result.file_rows, buffer.row_count);
    row_offset += buffer.row_count;

    const auto arena_offset = static_cast<std::uint32_t>(result.arena.size());
    const auto rows_begin = result.rows.size();
    result.rows.insert(result.rows.end(), buffer.rows.begin(),
                       buffer.rows.end());
    for (std::size_t offset = rows_begin; offset < result.rows.size();
         offset += PYTHON_CHUNK_BATCH_SPAN_ROW_SIZE) {
      const auto local_name_offset = read_u32(result.rows, offset + 12);
      if (local_name_offset >
          std::numeric_limits<std::uint32_t>::max() - arena_offset)
        throw std::overflow_error(
            "native Python repository name offset overflowed");
      write_u32(result.rows, offset + 12, arena_offset + local_name_offset);
    }
    result.arena.insert(result.arena.end(), buffer.arena.begin(),
                        buffer.arena.end());
  }
  if (row_offset != result.row_count ||
      result.file_rows.size() !=
          checked_table_size(
              result.file_count, PYTHON_CHUNK_BATCH_FILE_ROW_SIZE,
              "native Python repository file table overflowed") ||
      result.rows.size() !=
          checked_table_size(result.row_count, PYTHON_CHUNK_BATCH_SPAN_ROW_SIZE,
                             "native Python repository span table overflowed"))
    throw std::runtime_error("native Python repository merge is inconsistent");
  const auto merge_finished = Clock::now();
  result.ordered_merge_ns = elapsed_ns(merge_started, merge_finished);
  result.batch_wall_ns = elapsed_ns(batch_started, merge_finished);
  return result;
}

} // namespace codenib::core
