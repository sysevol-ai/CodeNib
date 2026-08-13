<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Code Chunking

Tree-sitter based code chunkers that split source files into semantic chunks
at configurable granularity levels.

## Chunk Depth (L0 / L1 / L2)

Controlled by the `chunk_depth` parameter in `BaseCodeChunker`:

| Depth | Name | Description |
|-------|------|-------------|
| 0 | **L0 — File** | Entire file as a single chunk. With `skeleton_mode=True`, emits a signature-only skeleton and falls back to full source when no signature can be extracted. |
| 1 | **L1 — Top-level** | Top-level definitions only: classes, standalone functions, and language-specific constructs (vars, consts, macros, etc.). Methods/members inside containers are **not** emitted. |
| 2 | **L2 — Member** | Method/member level (default). Emits methods and members inside classes/structs/impls. When `l2_level_exclusive=True` (default), the L1 container chunks are excluded, keeping only their L2 members. |

## Chunk Types by Language

### Python (`python_chunker.py`)

| chunk_type | Level | AST node | Example |
|------------|-------|----------|---------|
| `function` | L1 | `function_definition`, `async_function_definition` | `def foo():` |
| `class` | L1 | `class_definition` | `class Bar:` |
| `method` | L2 | method inside class body | `def baz(self):` |

### Go (`go_chunker.py`)

| chunk_type | Level | AST node | Example |
|------------|-------|----------|---------|
| `function` | L1 | `function_declaration` | `func Foo() {}` |
| `struct` | L1 | `type_spec` (struct) | `type Bar struct {}` |
| `type` | L1 | `type_spec` (non-struct) | `type Handler func()` |
| `interface` | L1 | `type_spec` (interface) | `type Reader interface {}` |
| `var` | L1 | `var_declaration` | `var ErrNotFound = ...` |
| `const` | L1 | `const_declaration` | `const MaxRetries = 3` |
| `method` | L2 | `method_declaration` | `func (s *Server) Start() {}` |

### Rust (`rust_chunker.py`)

| chunk_type | Level | AST node | Example |
|------------|-------|----------|---------|
| `function` | L1 | `function_item` | `fn foo() {}` |
| `struct` | L1 | `struct_item` | `struct Config {}` |
| `enum` | L1 | `enum_item` | `enum Status {}` |
| `trait` | L1 | `trait_item` | `trait Handler {}` |
| `impl` | L1 | `impl_item` | `impl Config {}` |
| `const` | L1 | `const_item` | `const MAX: u32 = 10;` |
| `static` | L1 | `static_item` | `static GLOBAL: ...` |
| `type` | L1 | `type_item` | `type Result<T> = ...` |
| `method` | L2 | function inside `impl`/`trait` | `fn get(&self) {}` |

### C/C++ (`cpp_chunker.py`)

| chunk_type | Level | AST node | Example |
|------------|-------|----------|---------|
| `function` | L1 | `function_definition` | `void foo() {}` |
| `class` | L1 | `class_specifier` | `class Config {};` |
| `declaration` | L1 | `declaration` (non-prototype) | `static int counter = 0;` |
| `macro` | L1 | `preproc_def`, `preproc_function_def` | `#define MAX 100` |
| `method` | L2 | method inside class body | `void start() {}` |

### C# (`csharp_chunker.py`)

| chunk_type | Level | AST node | Example |
|------------|-------|----------|---------|
| `class` | L1 | `class_declaration` | `class Invoice {}` |
| `interface` | L1 | `interface_declaration` | `interface IInvoice {}` |
| `enum` | L1 | `enum_declaration` | `enum Status { Draft }` |
| `record` | L1 | `record_declaration` | `record Money(decimal Amount);` |
| `struct` | L1 | `struct_declaration` | `struct Point {}` |
| `function` | L1 | top-level `local_function_statement` | `static void Main() {}` |
| `method` | L2 | `method_declaration`, `constructor_declaration` | `void AddLine() {}`, `Invoice() {}` |
| `property` | L2 | `property_declaration` | `decimal Total => total;` |

### Java (`java_chunker.py`)

| chunk_type | Level | AST node | Example |
|------------|-------|----------|---------|
| `class` | L1 | `class_declaration` | `class App {}` |
| `interface` | L1 | `interface_declaration` | `interface Runner {}` |
| `enum` | L1 | `enum_declaration` | `enum Mode { FAST }` |
| `record` | L1 | `record_declaration` | `record Point(int x, int y) {}` |
| `method` | L2 | `method_declaration`, `constructor_declaration` | `void run() {}`, `App() {}` |

### Ruby (`ruby_chunker.py`)

| chunk_type | Level | AST node | Example |
|------------|-------|----------|---------|
| `module` | L1 | `module` | `module Billing` |
| `class` | L1 | `class` | `class Invoice` |
| `function` | L1 | top-level `method` / `singleton_method` | `def top_level` |
| `method` | L2 | nested `method` / `singleton_method` | `def total`, `def self.build` |

### PHP (`php_chunker.py`)

| chunk_type | Level | AST node | Example |
|------------|-------|----------|---------|
| `class` | L1 | `class_declaration` | `class Invoice {}` |
| `interface` | L1 | `interface_declaration` | `interface Printable {}` |
| `trait` | L1 | `trait_declaration` | `trait Timestamped {}` |
| `enum` | L1 | `enum_declaration` | `enum Status {}` |
| `function` | L1 | `function_definition` | `function normalize() {}` |
| `method` | L2 | `method_declaration` | `public function total() {}` |
| `property` | L2 | `property_declaration` | `public int $total = 0;` |

### Kotlin (`kotlin_chunker.py`)

| chunk_type | Level | AST node | Example |
|------------|-------|----------|---------|
| `class` | L1 | `class_declaration` | `class Invoice {}` |
| `interface` | L1 | `class_declaration` | `interface Runner {}` |
| `enum` | L1 | `class_declaration` | `enum class Status { Active }` |
| `object` | L1 | `object_declaration` | `object Config {}` |
| `function` | L1 | `function_declaration` | `fun normalize() {}` |
| `property` | L1/L2 | `property_declaration` | `val total: Int = 0` |
| `method` | L2 | `function_declaration`, `secondary_constructor` | `fun total() {}`, `constructor(...)` |

### Swift (`swift_chunker.py`)

| chunk_type | Level | AST node | Example |
|------------|-------|----------|---------|
| `class` | L1 | `class_declaration` | `class Invoice {}` |
| `struct` | L1 | `class_declaration` (with `struct` keyword) | `struct Point {}` |
| `actor` | L1 | `class_declaration` (with `actor` keyword) | `actor Counter {}` |
| `protocol` | L1 | `protocol_declaration` | `protocol Runner {}` |
| `enum` | L1 | `enum_declaration` | `enum Status {}` |
| `extension` | L1 | `extension_declaration` | `extension Invoice {}` |
| `function` | L1 | top-level `function_declaration` | `func normalize() {}` |
| `method` | L2 | `function_declaration`, `protocol_function_declaration` in a body | `func total() {}` |
| `property` | L2 | `property_declaration` | `var total: Int = 0` |

### Scala (`scala_chunker.py`)

| chunk_type | Level | AST node | Example |
|------------|-------|----------|---------|
| `class` | L1 | `class_definition` | `class Invoice {}` |
| `object` | L1 | `object_definition` | `object Config {}` |
| `trait` | L1 | `trait_definition` | `trait Runner {}` |
| `function` | L1 | top-level `function_definition` | `def normalize() = ...` |
| `method` | L2 | `function_definition`, `function_declaration` in `template_body` | `def total = ...` |
| `property` | L2 | `val_definition`, `var_definition`, `val`/`var` class parameters | `val total: Int = 0` |

### Lua (`lua_chunker.py`)

| chunk_type | Level | AST node | Example |
|------------|-------|----------|---------|
| `function` | L1 | `function_declaration`, or `function_definition` assigned via `variable_declaration` / `assignment_statement` | `function M.foo() end`, `M.foo = function() end` |

### JavaScript / TypeScript (`js_chunker.py`)

| chunk_type | Level | AST node | Example |
|------------|-------|----------|---------|
| `function` | L1 | `function_declaration`, or arrow/function assigned to variable | `function foo() {}`, `const foo = () => {}` |
| `class` | L1 | `class_declaration` | `class App {}` |
| `object` | L1 | object literal assigned to variable | `const config = { ... }` |
| `variable` | L1 | other variable with initializer | `const MAX = 100` |
| `method` | L2 | method inside class body | `render() {}` |

## Special Chunk Types

These are emitted regardless of language when `include_header_epilogue=True`:

| chunk_type | Description |
|------------|-------------|
| `file` | Whole-file chunk (depth 0) |
| `header` | Imports, module docstrings, leading code before first definition |
| `epilogue` | Trailing code after the last definition |

## Configuration

```python
from codenib.code_chunking import create_chunker

# L2 (default) — methods and members
chunker = create_chunker("python", chunk_depth=2)

# L1 — top-level only
chunker = create_chunker("go", chunk_depth=1)

# L0 skeleton — file-level signature overview
chunker = create_chunker("rust", chunk_depth=0, skeleton_mode=True)

# L2 with L1 containers included
chunker = create_chunker("cpp", chunk_depth=2, l2_level_exclusive=False)
```

## Experimental Native Python Span Extraction

[#558](https://github.com/sysevol-ai/CodeNib/issues/558) retains an optional
C++ proof of concept for Python parsing and L1/L2 definition-span traversal.
Python decodes its compact 20-byte rows and name arena, then performs the
normal line splitting, node-ID generation, and final `CodeChunk` assembly.
The POC supports non-skeleton Python depths 1 and 2.

Both build and runtime selection are off by default. The POC builds separately
under `build/core-chunk-poc`, and `auto` falls back to the established Python
chunker per file. `required` fails closed for parity and benchmark runs.

The final gate passed exact `CodeChunk` parity for decorators, async
definitions, Unicode and PEP 695 syntax, L1/L2 selection, optional containers,
headers, error recovery, and line splitting. Balanced clean-checkout profiles
on CodeNib and HTTPie both missed the requirement for at least 20% end-to-end
acceleration; the candidate was slower in both final reports. Exact subject
revisions and medians are retained in the
[multi-language roadmap](../../docs/scip_multilanguage_roadmap.md).

```bash
make core-chunk-poc-test
make core-chunk-poc-profile \
  PYTHON_CHUNK_PROFILE_REPO=/path/to/python/repository

# Explicit experiment with compatible per-file fallback
export CODENIB_NATIVE_PYTHON_CHUNKER=auto

# Parity/profile mode: surface unsupported cases and native failures
export CODENIB_NATIVE_PYTHON_CHUNKER=required
```

This target remains local/manual only and is not part of required CI. The next
candidate should test incremental parse-tree reuse or repository-level batching
instead of another per-file language-boundary crossing.

## Repository-Batch Successor Gate

[#599](https://github.com/sysevol-ai/CodeNib/issues/599) defines the rejection
gate for one repository-level successor without adding that successor. It does
not reuse or retune the #558 per-file candidate, add C++ code, or change a
production chunking route. The candidate is the fixed private #600 adapter
`codenib.code_chunking.python_repository_batch_poc.run_python_repository_batch`
with `CODENIB_NATIVE_PYTHON_CHUNK_BATCH=required`. Until that adapter exists,
the controller validates the complete benchmark contract, writes its report
atomically, and exits nonzero instead of comparing the established arm with
itself.

The versioned manifest fixes clean detached checkouts of CodeNib
`a33bb13118e3a04f8d3d76eabcfb2602f785477a` and HTTPie
`2105caa49bae87c5809c274e407619a0de2639d1`. It also fixes two Python-only
repository configurations. Both exclude tests, use filter policy v3,
`strict=True`, depth 2, non-skeleton output, and L2-exclusive chunks:

- `continuity_l2_exclusive_unsplit` has no header/epilogue and no line cap.
- `bm25_v8_l2_exclusive_headers_300` enables the header/epilogue and a
  300-line cap for the current BM25 builder-schema-v8 consumer shape.

The established arm calls the current `CodeChunker.chunk_repository()` path
with `CODENIB_NATIVE_PYTHON_CHUNKER=off` and asserts the Python backend. The
candidate worker counts are exactly 1, 2, and 4. One worker count must pass all
four subject/configuration cells; if several pass, the smallest is selected.
Per-cell worker tuning and threshold averaging are forbidden.

Each arm receives four warmups and 20 measured samples in fresh, unique
processes. Each pair contains one established and one candidate sample; pair
order alternates AB then BA, GC treatment is symmetric, and filesystem page
cache is uncontrolled. The stopwatch starts
before cold adapter or chunker construction and includes discovery and
filtering, minified-source inspection, reads, parser/worker setup, the native
binding and ordered merge, buffer decode, complete `CodeChunk` construction,
and node materialization. Controller-side identity, artifact, contract, and
receipt checks happen before timing; the candidate's runtime contract safety
check remains inside its stopwatch. Parity, backend checks, stage aggregation,
and report serialization happen after timing.

Every warmup and measured pair must preserve the complete ordered seven-field
`CodeChunk` sequence, including content and node IDs. Because
`CodeChunker.nodes` is accumulated through set iteration, node parity uses the
sorted unique symbolic IDs; the raw list is diagnostic and chunk order is
never normalized. The hard performance gates require candidate median p50 and
nearest-rank p95 to be at most 80% of the established values, and candidate
nearest-rank p95 absolute peak RSS to be at most 125%. Exact chunk/node parity,
one native batch call, zero fallbacks, the expected backend, fresh PIDs, the
canonical protocol, and unchanged clean subject/source and benchmark/candidate
receipts must also pass in every cell.

Run the local/manual controller with both pinned checkouts:

```bash
make python-repository-chunk-gate \
  CODENIB_CHUNK_GATE_CODENIB_ROOT=/path/to/CodeNib \
  CODENIB_CHUNK_GATE_HTTPIE_ROOT=/path/to/httpie
```

The default report is
`/tmp/codenib-python-repository-chunk-gate.json`. Before #600 supplies the
adapter, the expected result is a complete fail-closed report and a nonzero
exit. After #600 connects it, the unchanged command becomes the formal
four-cell performance gate. Raw machine reports remain local; only their hash
and durable decision belong in the issue and roadmap.

## File Extension Mapping

Used by `gt_locate.py` to select the correct chunker:

| Extensions | Chunker |
|------------|---------|
| `.py` | `python` |
| `.go` | `go` |
| `.rs` | `rust` |
| `.c`, `.cpp`, `.cc`, `.cxx`, `.h`, `.hpp` | `cpp` |
| `.cs` | `csharp` |
| `.java` | `java` |
| `.rb` | `ruby` |
| `.php`, `.phtml` | `php` |
| `.kt`, `.kts` | `kotlin` |
| `.swift` | `swift` |
| `.scala`, `.sc` | `scala` |
| `.lua`, `.luau` | `lua` |
| `.js`, `.jsx` | `javascript` |
| `.ts`, `.tsx` | `typescript` |
