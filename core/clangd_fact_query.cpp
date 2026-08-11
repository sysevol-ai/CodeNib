/*
 * SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#include "clangd_fact_query.h"

#include <zlib.h>

#include <algorithm>
#include <array>
#include <chrono>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <limits>
#include <new>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

namespace codenib::core {
namespace {

using Clock = std::chrono::steady_clock;

constexpr std::uint8_t REF_KIND_DEFINITION = 0x2;
constexpr std::uint8_t REF_KIND_REFERENCE = 0x4;
constexpr std::uint8_t REF_KIND_SPELLED = 0x8;

constexpr std::uint8_t KIND_MACRO = 4;
constexpr std::uint8_t KIND_STRUCT = 6;
constexpr std::uint8_t KIND_CLASS = 7;
constexpr std::uint8_t KIND_FUNCTION = 12;
constexpr std::uint8_t KIND_FIELD = 14;
constexpr std::uint8_t KIND_INSTANCE_METHOD = 16;
constexpr std::uint8_t KIND_CLASS_METHOD = 17;
constexpr std::uint8_t KIND_STATIC_METHOD = 18;
constexpr std::uint8_t KIND_CONSTRUCTOR = 22;
constexpr std::uint8_t KIND_DESTRUCTOR = 23;

constexpr std::uint8_t RELATION_BASE_OF = 0;
constexpr std::uint8_t RELATION_OVERRIDDEN_BY = 1;

bool is_supported_version(std::uint32_t version) {
  return std::find(CLANGD_SUPPORTED_RIFF_VERSIONS.begin(),
                   CLANGD_SUPPORTED_RIFF_VERSIONS.end(),
                   version) != CLANGD_SUPPORTED_RIFF_VERSIONS.end();
}

bool is_known_chunk(std::string_view id) {
  return id == "meta" || id == "srcs" || id == "stri" || id == "symb" ||
         id == "refs" || id == "rela" || id == "cmdl";
}

template <typename Value>
void consume_budget(Value amount, Value &used, Value limit,
                    std::string_view label) {
  if (used > limit || amount > limit - used) {
    throw std::runtime_error("clangd " + std::string(label) +
                             " exceeds safety limit " + std::to_string(limit));
  }
  used += amount;
}

struct DecodeBudget {
  explicit DecodeBudget(const ClangdFactDecodeLimits &limits)
      : limits(limits) {}

  const ClangdFactDecodeLimits &limits;
  std::uint64_t string_table_bytes{0};
  std::uint64_t materialized_string_bytes{0};
  std::size_t string_entries{0};
  std::size_t records{0};
};

struct FileBudget {
  explicit FileBudget(DecodeBudget &aggregate) : aggregate(aggregate) {}

  void consume_string_bytes(std::uint64_t amount) {
    if (amount > aggregate.limits.max_string_table_bytes) {
      throw std::runtime_error(
          "clangd string table exceeds per-file safety limit " +
          std::to_string(aggregate.limits.max_string_table_bytes));
    }
    consume_budget(amount, aggregate.string_table_bytes,
                   aggregate.limits.max_aggregate_string_table_bytes,
                   "aggregate decompressed string bytes");
  }

  void consume_string_entries(std::size_t amount) {
    if (amount > aggregate.limits.max_string_entries_per_file) {
      throw std::runtime_error(
          "clangd string table exceeds per-file entry limit " +
          std::to_string(aggregate.limits.max_string_entries_per_file));
    }
    consume_budget(amount, aggregate.string_entries,
                   aggregate.limits.max_aggregate_string_entries,
                   "aggregate string entries");
  }

  void consume_materialized_string_bytes(std::uint64_t amount) {
    if (file_materialized_string_bytes >
            aggregate.limits.max_materialized_string_bytes_per_file ||
        amount > aggregate.limits.max_materialized_string_bytes_per_file -
                     file_materialized_string_bytes) {
      throw std::runtime_error(
          "clangd copied strings exceed per-file safety limit " +
          std::to_string(
              aggregate.limits.max_materialized_string_bytes_per_file));
    }
    file_materialized_string_bytes += amount;
    consume_budget(amount, aggregate.materialized_string_bytes,
                   aggregate.limits.max_aggregate_materialized_string_bytes,
                   "aggregate copied string bytes");
  }

  void consume_records(std::size_t amount, std::string_view kind) {
    if (file_records > aggregate.limits.max_records_per_file ||
        amount > aggregate.limits.max_records_per_file - file_records) {
      throw std::runtime_error(
          "clangd " + std::string(kind) + " exceeds per-file record limit " +
          std::to_string(aggregate.limits.max_records_per_file));
    }
    file_records += amount;
    consume_budget(amount, aggregate.records,
                   aggregate.limits.max_aggregate_records,
                   "aggregate decoded records");
  }

  DecodeBudget &aggregate;
  std::size_t file_records{0};
  std::uint64_t file_materialized_string_bytes{0};
};

std::uint64_t elapsed_ns(Clock::time_point start, Clock::time_point end) {
  return static_cast<std::uint64_t>(
      std::chrono::duration_cast<std::chrono::nanoseconds>(end - start)
          .count());
}

struct ByteView {
  const std::uint8_t *data{nullptr};
  std::size_t size{0};
};

class Reader {
public:
  explicit Reader(ByteView view) : view_(view) {}

  bool empty() const { return offset_ >= view_.size; }
  std::size_t remaining() const { return view_.size - offset_; }

  std::uint8_t read_u8() {
    require(1, "uint8");
    return view_.data[offset_++];
  }

  std::uint64_t read_varint() {
    std::uint64_t value = 0;
    unsigned int shift = 0;
    for (unsigned int count = 0; count < 10; ++count) {
      const auto byte = read_u8();
      if (shift == 63 && (byte & 0x7eU) != 0)
        throw std::runtime_error("clangd varint overflows uint64");
      value |= static_cast<std::uint64_t>(byte & 0x7fU) << shift;
      if ((byte & 0x80U) == 0)
        return value;
      shift += 7;
    }
    throw std::runtime_error("clangd varint exceeds 10 bytes");
  }

  std::string read_symbol_id() {
    require(8, "SymbolID");
    std::string result(reinterpret_cast<const char *>(view_.data + offset_), 8);
    offset_ += 8;
    return result;
  }

private:
  void require(std::size_t count, const char *what) const {
    if (count > remaining()) {
      throw std::runtime_error(std::string("truncated clangd ") + what);
    }
  }

  ByteView view_;
  std::size_t offset_{0};
};

std::uint32_t read_u32_le(const std::uint8_t *data) {
  return static_cast<std::uint32_t>(data[0]) |
         (static_cast<std::uint32_t>(data[1]) << 8U) |
         (static_cast<std::uint32_t>(data[2]) << 16U) |
         (static_cast<std::uint32_t>(data[3]) << 24U);
}

std::vector<std::uint8_t> read_file(const std::filesystem::path &path,
                                    const ClangdFactDecodeLimits &limits) {
  std::ifstream input(path, std::ios::binary | std::ios::ate);
  if (!input)
    throw std::runtime_error("cannot open clangd index: " + path.string());
  const auto end = input.tellg();
  if (end < 0)
    throw std::runtime_error("cannot size clangd index: " + path.string());
  const auto size = static_cast<std::uint64_t>(end);
  if (size > limits.max_index_file_bytes) {
    throw std::runtime_error("clangd index exceeds per-file safety limit " +
                             std::to_string(limits.max_index_file_bytes) +
                             ": " + path.string());
  }
  if (size > std::numeric_limits<std::size_t>::max() ||
      size > static_cast<std::uint64_t>(
                 std::numeric_limits<std::streamsize>::max())) {
    throw std::runtime_error("clangd index is too large: " + path.string());
  }
  std::vector<std::uint8_t> data(static_cast<std::size_t>(size));
  input.seekg(0, std::ios::beg);
  if (!data.empty() && !input.read(reinterpret_cast<char *>(data.data()),
                                   static_cast<std::streamsize>(data.size()))) {
    throw std::runtime_error("cannot read clangd index: " + path.string());
  }
  return data;
}

std::unordered_map<std::string, ByteView>
read_riff(const std::vector<std::uint8_t> &data,
          const ClangdFactDecodeLimits &limits) {
  if (data.size() < 12 ||
      std::string(reinterpret_cast<const char *>(data.data()), 4) != "RIFF") {
    throw std::runtime_error("not a RIFF file");
  }
  const auto total_len = static_cast<std::size_t>(read_u32_le(data.data() + 4));
  if (total_len < 4)
    throw std::runtime_error("invalid RIFF length");
  if (total_len != data.size() - 8)
    throw std::runtime_error("RIFF length does not match file size");
  if (std::string(reinterpret_cast<const char *>(data.data() + 8), 4) !=
      "CdIx") {
    throw std::runtime_error("not a clangd CdIx index");
  }

  const std::size_t end = 8 + total_len;
  std::size_t chunk_count = 0;
  std::unordered_map<std::string, ByteView> chunks;
  std::size_t offset = 12;
  while (offset < end) {
    if (chunk_count >= limits.max_chunks_per_file) {
      throw std::runtime_error("clangd RIFF chunk count exceeds safety limit " +
                               std::to_string(limits.max_chunks_per_file));
    }
    ++chunk_count;
    if (end - offset < 8)
      throw std::runtime_error("truncated RIFF chunk header");
    const std::string id(reinterpret_cast<const char *>(data.data() + offset),
                         4);
    const auto length =
        static_cast<std::size_t>(read_u32_le(data.data() + offset + 4));
    offset += 8;
    if (length > end - offset)
      throw std::runtime_error("truncated RIFF chunk payload");
    if (is_known_chunk(id)) {
      const auto inserted =
          chunks.emplace(id, ByteView{data.data() + offset, length}).second;
      if (!inserted)
        throw std::runtime_error("duplicate clangd RIFF chunk: " + id);
    }
    offset += length;
    if ((length & 1U) != 0) {
      if (offset >= end)
        throw std::runtime_error("missing RIFF padding byte");
      ++offset;
    }
  }
  if (offset != end)
    throw std::runtime_error("RIFF chunk alignment exceeds declared length");
  return chunks;
}

std::vector<std::string> decode_string_table(ByteView view,
                                             FileBudget &budget) {
  if (view.size < 4)
    throw std::runtime_error("truncated clangd string table");
  const auto expected = static_cast<std::size_t>(read_u32_le(view.data));
  const auto *payload = view.data + 4;
  const auto payload_size = view.size - 4;
  const auto decoded_size = expected == 0 ? payload_size : expected;
  budget.consume_string_bytes(static_cast<std::uint64_t>(decoded_size));

  std::vector<std::uint8_t> raw;
  if (expected == 0) {
    raw.assign(payload, payload + payload_size);
  } else {
    raw.resize(expected);
    if (payload_size > std::numeric_limits<uInt>::max() ||
        raw.size() > std::numeric_limits<uInt>::max()) {
      throw std::runtime_error("clangd zlib input exceeds platform limit");
    }
    z_stream stream{};
    stream.next_in =
        const_cast<Bytef *>(reinterpret_cast<const Bytef *>(payload));
    stream.avail_in = static_cast<uInt>(payload_size);
    stream.next_out = reinterpret_cast<Bytef *>(raw.data());
    stream.avail_out = static_cast<uInt>(raw.size());
    const int init_status = inflateInit(&stream);
    if (init_status != Z_OK)
      throw std::runtime_error("cannot initialize clangd string decompressor");
    const int status = inflate(&stream, Z_FINISH);
    const bool complete = status == Z_STREAM_END && stream.avail_in == 0 &&
                          stream.total_out == raw.size();
    (void)inflateEnd(&stream);
    if (!complete)
      throw std::runtime_error("cannot decompress clangd string table");
  }

  std::size_t string_count = static_cast<std::size_t>(
      std::count(raw.begin(), raw.end(), static_cast<std::uint8_t>(0)));
  if (!raw.empty() && raw.back() != 0)
    ++string_count;
  budget.consume_string_entries(string_count);

  std::vector<std::string> strings;
  strings.reserve(string_count);
  std::size_t start = 0;
  for (std::size_t index = 0; index < raw.size(); ++index) {
    if (raw[index] != 0)
      continue;
    strings.emplace_back(reinterpret_cast<const char *>(raw.data() + start),
                         index - start);
    start = index + 1;
  }
  if (start < raw.size()) {
    strings.emplace_back(reinterpret_cast<const char *>(raw.data() + start),
                         raw.size() - start);
  }
  return strings;
}

const std::string &string_at(const std::vector<std::string> &strings,
                             std::uint64_t index) {
  if (index >= strings.size())
    throw std::runtime_error("clangd string index is out of range");
  return strings[static_cast<std::size_t>(index)];
}

std::string copy_string_at(const std::vector<std::string> &strings,
                           std::uint64_t index, FileBudget &budget) {
  const auto &value = string_at(strings, index);
  budget.consume_materialized_string_bytes(
      static_cast<std::uint64_t>(value.size()));
  return value;
}

int checked_line(std::uint64_t value) {
  if (value > static_cast<std::uint64_t>(std::numeric_limits<int>::max()))
    throw std::runtime_error("clangd source line exceeds int range");
  return static_cast<int>(value);
}

struct Location {
  std::string file;
  int start_line{0};
  int start_col{0};
  int end_line{0};
  int end_col{0};
};

Location decode_location(Reader &reader,
                         const std::vector<std::string> &strings,
                         FileBudget &budget) {
  const auto file_index = reader.read_varint();
  Location location;
  location.file = copy_string_at(strings, file_index, budget);
  location.start_line = checked_line(reader.read_varint());
  location.start_col = checked_line(reader.read_varint());
  location.end_line = checked_line(reader.read_varint());
  location.end_col = checked_line(reader.read_varint());
  return location;
}

struct SymbolRecord {
  std::string id;
  std::uint8_t kind{0};
  std::string name;
  std::string scope;
  std::optional<Location> definition;
  std::optional<Location> canonical_declaration;
};

struct ReferenceRecord {
  std::uint8_t kind{0};
  Location location;
  std::string container;
};

struct ReferenceGroup {
  std::string symbol_id;
  std::vector<ReferenceRecord> refs;
};

struct RelationRecord {
  std::string subject;
  std::uint8_t predicate{0};
  std::string object;
};

struct ParsedFile {
  std::vector<SymbolRecord> symbols;
  std::vector<ReferenceGroup> refs;
  std::vector<RelationRecord> relations;
};

std::vector<SymbolRecord>
decode_symbols(ByteView view, const std::vector<std::string> &strings,
               FileBudget &budget) {
  Reader reader(view);
  std::vector<SymbolRecord> symbols;
  while (!reader.empty()) {
    if (reader.remaining() < 8)
      throw std::runtime_error("truncated clangd symbol record");
    budget.consume_records(1, "symbol records");
    SymbolRecord symbol;
    symbol.id = reader.read_symbol_id();
    symbol.kind = reader.read_u8();
    (void)reader.read_u8(); // SymbolLanguage
    symbol.name = copy_string_at(strings, reader.read_varint(), budget);
    symbol.scope = copy_string_at(strings, reader.read_varint(), budget);
    (void)string_at(strings,
                    reader.read_varint()); // TemplateSpecializationArgs
    symbol.definition = decode_location(reader, strings, budget);
    symbol.canonical_declaration =
        decode_location(reader, strings, budget);   // CanonicalDeclaration
    (void)reader.read_varint();                     // References count
    (void)reader.read_u8();                         // Flags
    (void)string_at(strings, reader.read_varint()); // Signature
    (void)string_at(strings,
                    reader.read_varint());          // CompletionSnippetSuffix
    (void)string_at(strings, reader.read_varint()); // Documentation
    (void)string_at(strings, reader.read_varint()); // ReturnType
    (void)string_at(strings, reader.read_varint()); // Type
    const auto headers = reader.read_varint();
    if (headers > reader.remaining() / 2)
      throw std::runtime_error("invalid clangd include-header count");
    if (headers > std::numeric_limits<std::size_t>::max())
      throw std::runtime_error("clangd include-header count exceeds size_t");
    budget.consume_records(static_cast<std::size_t>(headers),
                           "include-header records");
    for (std::uint64_t index = 0; index < headers; ++index) {
      (void)string_at(strings, reader.read_varint());
      (void)reader.read_varint();
    }
    symbols.push_back(std::move(symbol));
  }
  return symbols;
}

std::vector<ReferenceGroup> decode_refs(ByteView view,
                                        const std::vector<std::string> &strings,
                                        FileBudget &budget) {
  Reader reader(view);
  std::vector<ReferenceGroup> groups;
  while (!reader.empty()) {
    if (reader.remaining() < 8)
      throw std::runtime_error("truncated clangd reference group");
    budget.consume_records(1, "reference groups");
    ReferenceGroup group;
    group.symbol_id = reader.read_symbol_id();
    const auto count = reader.read_varint();
    if (count > reader.remaining() / 14)
      throw std::runtime_error("invalid clangd reference count");
    if (count > std::numeric_limits<std::size_t>::max())
      throw std::runtime_error("clangd reference count exceeds size_t");
    budget.consume_records(static_cast<std::size_t>(count),
                           "reference records");
    group.refs.reserve(static_cast<std::size_t>(count));
    for (std::uint64_t index = 0; index < count; ++index) {
      ReferenceRecord reference;
      reference.kind = reader.read_u8();
      reference.location = decode_location(reader, strings, budget);
      reference.container = reader.read_symbol_id();
      group.refs.push_back(std::move(reference));
    }
    groups.push_back(std::move(group));
  }
  return groups;
}

std::vector<RelationRecord> decode_relations(ByteView view,
                                             FileBudget &budget) {
  Reader reader(view);
  std::vector<RelationRecord> relations;
  while (!reader.empty()) {
    if (reader.remaining() < 17)
      throw std::runtime_error("truncated clangd relation record");
    budget.consume_records(1, "relation records");
    RelationRecord relation;
    relation.subject = reader.read_symbol_id();
    relation.predicate = reader.read_u8();
    relation.object = reader.read_symbol_id();
    relations.push_back(std::move(relation));
  }
  return relations;
}

ParsedFile parse_idx(const std::vector<std::uint8_t> &data,
                     DecodeBudget &aggregate_budget) {
  const auto chunks = read_riff(data, aggregate_budget.limits);
  const auto metadata = chunks.find("meta");
  if (metadata == chunks.end())
    throw std::runtime_error("missing required clangd RIFF chunk: meta");
  if (metadata->second.size != sizeof(std::uint32_t))
    throw std::runtime_error("invalid clangd meta chunk length");
  const auto version = read_u32_le(metadata->second.data);
  if (!is_supported_version(version)) {
    throw std::runtime_error("unsupported clangd RIFF version " +
                             std::to_string(version) +
                             "; supported versions are 18, 19, 20");
  }

  const auto string_chunk = chunks.find("stri");
  if (string_chunk == chunks.end())
    throw std::runtime_error("missing required clangd RIFF chunk: stri");
  FileBudget file_budget(aggregate_budget);
  const auto strings = decode_string_table(string_chunk->second, file_budget);

  ParsedFile parsed;
  if (const auto symbols = chunks.find("symb"); symbols != chunks.end())
    parsed.symbols = decode_symbols(symbols->second, strings, file_budget);
  if (const auto refs = chunks.find("refs"); refs != chunks.end())
    parsed.refs = decode_refs(refs->second, strings, file_budget);
  if (const auto relations = chunks.find("rela"); relations != chunks.end())
    parsed.relations = decode_relations(relations->second, file_budget);
  return parsed;
}

bool has_file(const std::optional<Location> &location) {
  return location.has_value() && !location->file.empty();
}

struct CollectedRecords {
  std::vector<SymbolRecord> symbols;
  std::unordered_map<std::string, std::size_t> symbol_indexes;
  std::unordered_map<std::string, std::vector<ReferenceRecord>> refs;
  std::vector<std::string> ref_order;
  std::vector<RelationRecord> relations;
};

void merge_file(CollectedRecords &collected, ParsedFile parsed) {
  for (auto &symbol : parsed.symbols) {
    const auto found = collected.symbol_indexes.find(symbol.id);
    if (found == collected.symbol_indexes.end()) {
      const auto index = collected.symbols.size();
      collected.symbol_indexes.emplace(symbol.id, index);
      collected.symbols.push_back(std::move(symbol));
      continue;
    }
    auto &existing = collected.symbols[found->second];
    if (!has_file(existing.definition) && has_file(symbol.definition))
      existing = std::move(symbol);
  }

  for (auto &group : parsed.refs) {
    auto found = collected.refs.find(group.symbol_id);
    if (found == collected.refs.end()) {
      collected.ref_order.push_back(group.symbol_id);
      found = collected.refs
                  .emplace(group.symbol_id, std::vector<ReferenceRecord>{})
                  .first;
    }
    auto &destination = found->second;
    destination.insert(destination.end(),
                       std::make_move_iterator(group.refs.begin()),
                       std::make_move_iterator(group.refs.end()));
  }
  collected.relations.insert(collected.relations.end(),
                             std::make_move_iterator(parsed.relations.begin()),
                             std::make_move_iterator(parsed.relations.end()));
}

const char *node_type(std::uint8_t kind) {
  if (kind == KIND_STRUCT || kind == KIND_CLASS)
    return NODE_TYPE_CLASS;
  if (kind == KIND_FUNCTION)
    return NODE_TYPE_FUNCTION;
  if (kind == KIND_INSTANCE_METHOD || kind == KIND_CLASS_METHOD ||
      kind == KIND_STATIC_METHOD || kind == KIND_CONSTRUCTOR ||
      kind == KIND_DESTRUCTOR) {
    return NODE_TYPE_METHOD;
  }
  if (kind == KIND_FIELD)
    return NODE_TYPE_FIELD;
  return NODE_TYPE_SYMBOL;
}

std::string symbol_hex(const std::string &id) {
  static constexpr std::array<char, 16> digits = {'0', '1', '2', '3', '4', '5',
                                                  '6', '7', '8', '9', 'a', 'b',
                                                  'c', 'd', 'e', 'f'};
  std::string result(id.size() * 2, '0');
  for (std::size_t index = 0; index < id.size(); ++index) {
    const auto byte = static_cast<unsigned char>(id[index]);
    result[index * 2] = digits[byte >> 4U];
    result[index * 2 + 1] = digits[byte & 0x0fU];
  }
  return result;
}

std::filesystem::path normalized_absolute(const std::string &value) {
  std::error_code error;
  auto path = std::filesystem::absolute(std::filesystem::path(value), error);
  if (error)
    path = std::filesystem::path(value);
  return path.lexically_normal();
}

struct IndexFile {
  std::filesystem::path path;
  std::string name;
  std::uint64_t bytes{0};
};

std::vector<IndexFile>
discover_index_files(const std::filesystem::path &directory,
                     const ClangdFactDecodeLimits &limits) {
  if (!std::filesystem::is_directory(directory))
    throw std::runtime_error("clangd index directory does not exist: " +
                             directory.string());
  std::vector<IndexFile> files;
  std::uint64_t aggregate_bytes = 0;
  for (const auto &entry : std::filesystem::directory_iterator(directory)) {
    if (!entry.is_regular_file() || entry.path().extension() != ".idx")
      continue;
    if (files.size() >= limits.max_index_files) {
      throw std::runtime_error("clangd index file count exceeds safety limit " +
                               std::to_string(limits.max_index_files));
    }
    std::error_code size_error;
    const auto bytes = entry.file_size(size_error);
    if (size_error)
      throw std::runtime_error("cannot size clangd index: " +
                               entry.path().string());
    if (bytes > limits.max_index_file_bytes) {
      throw std::runtime_error("clangd index exceeds per-file safety limit " +
                               std::to_string(limits.max_index_file_bytes) +
                               ": " + entry.path().string());
    }
    consume_budget(static_cast<std::uint64_t>(bytes), aggregate_bytes,
                   limits.max_aggregate_index_bytes, "aggregate index bytes");
    files.push_back(IndexFile{entry.path(),
                              entry.path().filename().generic_string(),
                              static_cast<std::uint64_t>(bytes)});
  }
  std::sort(files.begin(), files.end(),
            [](const IndexFile &left, const IndexFile &right) {
              return left.name < right.name;
            });
  if (files.empty())
    throw std::runtime_error("clangd index directory has no .idx files: " +
                             directory.string());
  return files;
}

bool path_has_prefix(const std::filesystem::path &path,
                     const std::filesystem::path &prefix) {
  auto path_part = path.begin();
  for (auto prefix_part = prefix.begin(); prefix_part != prefix.end();
       ++prefix_part, ++path_part) {
    if (path_part == path.end() || *path_part != *prefix_part)
      return false;
  }
  return true;
}

std::string relative_file(std::string uri,
                          const std::filesystem::path &project_root) {
  if (uri.rfind("file://", 0) == 0)
    uri.erase(0, 7);
  auto path = std::filesystem::path(uri).lexically_normal();
  if (path_has_prefix(path, project_root)) {
    const auto relative = path.lexically_relative(project_root);
    if (!relative.empty())
      return relative.generic_string();
  }
  return path.generic_string();
}

std::optional<Location>
resolve_definition(const SymbolRecord &symbol,
                   const std::vector<ReferenceRecord> *refs) {
  if (refs != nullptr) {
    for (const auto &reference : *refs) {
      if ((reference.kind & REF_KIND_DEFINITION) != 0 &&
          (reference.kind & REF_KIND_SPELLED) != 0) {
        return reference.location;
      }
    }
    for (const auto &reference : *refs) {
      if ((reference.kind & REF_KIND_DEFINITION) != 0)
        return reference.location;
    }
  }
  if (has_file(symbol.definition))
    return symbol.definition;
  return std::nullopt;
}

void replace_scope_separators(std::string &value) {
  std::size_t position = 0;
  while ((position = value.find("::", position)) != std::string::npos) {
    value.replace(position, 2, ".");
    ++position;
  }
}

std::shared_ptr<DecodedRecords>
build_query_records(const CollectedRecords &collected,
                    const std::filesystem::path &project_root) {
  auto records = std::make_shared<DecodedRecords>();
  records->project_root = project_root.generic_string();
  records->vertices.reserve(collected.symbols.size());

  std::unordered_map<std::string, CodeGraph::VertexId> record_indexes;
  record_indexes.reserve(collected.symbols.size());
  auto upsert_structural = [&](const std::string &name, const char *type) {
    const auto found = record_indexes.find(name);
    if (found != record_indexes.end()) {
      records->vertices[static_cast<std::size_t>(found->second)].type = type;
      return;
    }
    const auto id = static_cast<CodeGraph::VertexId>(records->vertices.size());
    CodeGraph::VertexData vertex;
    vertex.name = name;
    vertex.type = type;
    records->vertices.push_back(std::move(vertex));
    record_indexes.emplace(name, id);
  };
  auto ensure_placeholder = [&](const std::string &name) {
    if (record_indexes.find(name) != record_indexes.end())
      return;
    const auto id = static_cast<CodeGraph::VertexId>(records->vertices.size());
    CodeGraph::VertexData vertex;
    vertex.name = name;
    records->vertices.push_back(std::move(vertex));
    record_indexes.emplace(name, id);
  };

  upsert_structural(ROOT_NODE, "root");
  std::unordered_set<std::string> observed_files;
  std::unordered_set<std::string> observed_directories;
  std::vector<std::pair<std::string, std::string>> structural_edge_names;
  std::unordered_map<std::string, CodeGraph::VertexId> vertex_by_symbol;
  vertex_by_symbol.reserve(collected.symbols.size());
  for (const auto &symbol : collected.symbols) {
    if (symbol.kind == KIND_MACRO)
      continue;
    std::string qualified = symbol.scope + symbol.name;
    if (qualified.empty())
      continue;
    const auto ref_it = collected.refs.find(symbol.id);
    const auto definition = resolve_definition(
        symbol, ref_it == collected.refs.end() ? nullptr : &ref_it->second);
    if (!definition.has_value() || definition->file.empty())
      continue;
    const std::string file = relative_file(definition->file, project_root);
    if (std::filesystem::path(file).is_absolute() ||
        file.find("third_party/") != std::string::npos) {
      continue;
    }

    if (observed_files.insert(file).second) {
      auto directory = std::filesystem::path(file).parent_path();
      while (!directory.empty() && directory != directory.parent_path() &&
             directory != std::filesystem::path(".")) {
        const auto name = directory.generic_string();
        if (observed_directories.insert(name).second) {
          upsert_structural(name, NODE_TYPE_DIRECTORY);
          auto parent = directory.parent_path().generic_string();
          if (parent.empty() || parent == ".")
            parent = ROOT_NODE;
          ensure_placeholder(parent);
          structural_edge_names.emplace_back(parent, name);
        }
        directory = directory.parent_path();
      }
      upsert_structural(file, NODE_TYPE_FILE);
      auto parent = std::filesystem::path(file).parent_path().generic_string();
      if (parent.empty() || parent == ".")
        parent = ROOT_NODE;
      ensure_placeholder(parent);
      structural_edge_names.emplace_back(parent, file);
    }

    CodeGraph::VertexData vertex;
    vertex.name = symbol_hex(symbol.id);
    vertex.type = node_type(symbol.kind);
    vertex.file = file;
    vertex.start_line = definition->start_line;
    vertex.end_line = definition->start_line;
    vertex.selection_line = definition->start_line;
    replace_scope_separators(qualified);
    if (vertex.type == NODE_TYPE_FUNCTION || vertex.type == NODE_TYPE_METHOD)
      qualified += "()";
    vertex.unified_name = file + ":" + qualified;
    vertex.has_definition = true;

    const auto existing = record_indexes.find(vertex.name);
    CodeGraph::VertexId id;
    if (existing == record_indexes.end()) {
      id = static_cast<CodeGraph::VertexId>(records->vertices.size());
      record_indexes.emplace(vertex.name, id);
      records->vertices.push_back(std::move(vertex));
    } else {
      id = existing->second;
      records->vertices[static_cast<std::size_t>(id)] = std::move(vertex);
    }
    vertex_by_symbol.emplace(symbol.id, id);
  }

  std::unordered_map<std::string, std::vector<CodeGraph::VertexId>>
      resolution_groups;
  std::vector<std::string> resolution_display_order;
  for (std::size_t index = 0; index < records->vertices.size(); ++index) {
    const auto &vertex = records->vertices[index];
    if (!vertex.unified_name.has_value() || vertex.unified_name->empty())
      continue;
    const auto id = static_cast<CodeGraph::VertexId>(index);
    auto [group, inserted] =
        resolution_groups.try_emplace(*vertex.unified_name);
    if (inserted)
      resolution_display_order.push_back(*vertex.unified_name);
    group->second.push_back(id);
  }
  records->query_resolution_order.reserve(records->vertices.size());
  std::vector<bool> resolution_seen(records->vertices.size(), false);
  for (const auto &display : resolution_display_order) {
    for (const auto id : resolution_groups.at(display)) {
      records->query_resolution_order.push_back(id);
      resolution_seen[static_cast<std::size_t>(id)] = true;
    }
  }
  for (std::size_t index = 0; index < records->vertices.size(); ++index) {
    if (!resolution_seen[index]) {
      records->query_resolution_order.push_back(
          static_cast<CodeGraph::VertexId>(index));
    }
  }

  for (const auto &[source_name, target_name] : structural_edge_names) {
    const auto source = record_indexes.find(source_name);
    const auto target = record_indexes.find(target_name);
    if (source == record_indexes.end() || target == record_indexes.end())
      continue;
    records->edges.push_back(CodeGraph::EdgeData{source->second, target->second,
                                                 EDGE_TYPE_CONTAIN,
                                                 std::nullopt, std::nullopt});
  }

  std::unordered_set<std::string> contained_symbols;
  contained_symbols.reserve(vertex_by_symbol.size());
  for (const auto &target_symbol : collected.ref_order) {
    const auto target = vertex_by_symbol.find(target_symbol);
    if (target == vertex_by_symbol.end())
      continue;
    const auto ref_group = collected.refs.find(target_symbol);
    if (ref_group == collected.refs.end())
      continue;
    for (const auto &reference : ref_group->second) {
      if ((reference.kind & REF_KIND_DEFINITION) == 0 ||
          reference.container == std::string(8, '\0')) {
        continue;
      }
      const auto source = vertex_by_symbol.find(reference.container);
      if (source == vertex_by_symbol.end())
        continue;
      records->edges.push_back(
          CodeGraph::EdgeData{source->second, target->second, EDGE_TYPE_CONTAIN,
                              std::nullopt, std::nullopt});
      contained_symbols.insert(target_symbol);
      break;
    }
  }

  for (const auto &symbol : collected.symbols) {
    if (symbol.kind == KIND_MACRO ||
        contained_symbols.find(symbol.id) != contained_symbols.end()) {
      continue;
    }
    const auto target = vertex_by_symbol.find(symbol.id);
    if (target == vertex_by_symbol.end())
      continue;
    const auto &vertex =
        records->vertices[static_cast<std::size_t>(target->second)];
    if (!vertex.file.has_value())
      continue;
    const auto source = record_indexes.find(*vertex.file);
    if (source == record_indexes.end())
      continue;
    records->edges.push_back(CodeGraph::EdgeData{source->second, target->second,
                                                 EDGE_TYPE_CONTAIN,
                                                 std::nullopt, std::nullopt});
  }

  for (const auto &target_symbol : collected.ref_order) {
    const auto target = vertex_by_symbol.find(target_symbol);
    if (target == vertex_by_symbol.end())
      continue;
    const auto ref_group = collected.refs.find(target_symbol);
    if (ref_group == collected.refs.end())
      continue;
    for (const auto &reference : ref_group->second) {
      if ((reference.kind & REF_KIND_REFERENCE) == 0 ||
          reference.container == std::string(8, '\0')) {
        continue;
      }
      const auto source = vertex_by_symbol.find(reference.container);
      if (source == vertex_by_symbol.end())
        continue;
      CodeGraph::EdgeData edge;
      edge.source = source->second;
      edge.target = target->second;
      edge.type = EDGE_TYPE_REFERENCE;
      if (!reference.location.file.empty())
        edge.anchor_file = relative_file(reference.location.file, project_root);
      edge.anchor_line = reference.location.start_line;
      records->edges.push_back(std::move(edge));
    }
  }

  for (const auto &relation : collected.relations) {
    const auto subject = vertex_by_symbol.find(relation.subject);
    const auto object = vertex_by_symbol.find(relation.object);
    if (subject == vertex_by_symbol.end() || object == vertex_by_symbol.end())
      continue;
    if (relation.predicate != RELATION_BASE_OF &&
        relation.predicate != RELATION_OVERRIDDEN_BY) {
      continue;
    }
    records->edges.push_back(
        CodeGraph::EdgeData{object->second, subject->second,
                            EDGE_TYPE_REFERENCE, std::nullopt, std::nullopt});
  }
  return records;
}

} // namespace

ClangdQueryRecords
decode_clangd_query_records(const std::string &idx_directory,
                            const std::string &project_root,
                            const ClangdFactDecodeLimits &limits) {
  const auto total_started = Clock::now();
  ClangdFactDecodeProfile profile;

  const auto discover_started = Clock::now();
  const auto files =
      discover_index_files(normalized_absolute(idx_directory), limits);
  const auto root = normalized_absolute(project_root);
  profile.file_count = files.size();
  for (const auto &file : files)
    profile.index_bytes += file.bytes;
  profile.discover_files_ns = elapsed_ns(discover_started, Clock::now());

  CollectedRecords collected;
  DecodeBudget budget(limits);
  std::uint64_t read_bytes = 0;
  for (const auto &file : files) {
    const auto read_started = Clock::now();
    auto data = read_file(file.path, limits);
    consume_budget(static_cast<std::uint64_t>(data.size()), read_bytes,
                   limits.max_aggregate_index_bytes, "aggregate index bytes");
    profile.read_files_ns += elapsed_ns(read_started, Clock::now());

    const auto parse_started = Clock::now();
    ParsedFile parsed;
    try {
      parsed = parse_idx(data, budget);
    } catch (const std::bad_alloc &) {
      throw;
    } catch (const std::exception &error) {
      throw std::runtime_error("cannot parse clangd index " +
                               file.path.string() + ": " + error.what());
    }
    profile.parse_files_ns += elapsed_ns(parse_started, Clock::now());
    profile.raw_symbol_count += parsed.symbols.size();
    for (const auto &group : parsed.refs)
      profile.raw_reference_count += group.refs.size();

    const auto merge_started = Clock::now();
    merge_file(collected, std::move(parsed));
    profile.merge_records_ns += elapsed_ns(merge_started, Clock::now());
  }

  const auto build_started = Clock::now();
  auto records = build_query_records(collected, root);
  profile.build_query_records_ns = elapsed_ns(build_started, Clock::now());
  profile.decoded_record_count = records->vertices.size();
  for (const auto &vertex : records->vertices) {
    if (vertex.has_definition.value_or(false))
      ++profile.definition_count;
  }
  for (const auto &edge : records->edges) {
    if (edge.type == EDGE_TYPE_REFERENCE)
      ++profile.reference_count;
  }
  profile.index_bytes = read_bytes;
  profile.decompressed_string_bytes = budget.string_table_bytes;
  profile.materialized_string_bytes = budget.materialized_string_bytes;
  profile.string_entry_count = budget.string_entries;
  profile.total_ns = elapsed_ns(total_started, Clock::now());
  return ClangdQueryRecords{std::move(records), profile};
}

} // namespace codenib::core
