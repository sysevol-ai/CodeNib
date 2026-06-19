<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Contributing a Language

CodeMiner language support is deliberately split into layers. A language can
ship with chunking and retrieval first, then add graph indexing, incremental
patching, and optional C++ acceleration later. This guide keeps those steps
explicit so new language work does not become a scattered edit across chunkers,
routers, decoders, agent compile, and tests.

The central entry point is `codeminer/languages.py`. Add or update a
`LanguageSpec` there first, then wire only the layers the language actually
supports.

## Current Layers

| Layer | Purpose | Current integration point |
| --- | --- | --- |
| Language registry | Single declarative metadata record | `codeminer/languages.py::LanguageSpec` |
| Chunking | Tree-sitter chunks for retrieval, GT, and vector indexes | `codeminer/code_chunking/` and `codeminer/code_chunker.py` |
| Cold-start graph | Whole-repo symbol graph build | `codeminer/ls_router.py`, `codeminer/scip_interface/`, `codeminer/ls_index/` |
| Incremental graph | In-place graph patching after a diff | `codeminer/graph/incremental/` |
| Agent compile | Query-time language scenario normalization | `codeminer/agent/compile.py` |
| Dataset / GT | Extension-to-language mapping for patch analysis | `codeminer/dataset/gt_locate.py` |
| Core acceleration | Optional C++ mirror decoder | `core/`, `codeminer/scip_interface/scip_decode_core.py` |

## Backend Policy

Choose backends per language, not globally.

- `cold_start_backend`: the source for a fresh full-repo graph. Use SCIP where
  the indexer is mature, clangd for C/C++, and generic LSP only when it is the
  best available cold-start source.
- `incremental_backend`: the source for patching or filling gaps after a repo
  changes. LSP is often a better fit here because servers already maintain
  workspace state.
- `core_decoder`: optional acceleration. New languages may ship serial-only.
  If a language sets `core_decoder=True`, it must have serial/core parity tests.

Do not assume every LSP server exposes a clangd-style on-disk index. Most
generic LSP work means driving JSON-RPC methods such as
`textDocument/documentSymbol`, `textDocument/definition`,
`textDocument/references`, `callHierarchy/*`, and then normalizing the result
into the CodeGraph schema.

## Step 1: Register Metadata

Add a `LanguageSpec` in `codeminer/languages.py`.

```python
LanguageSpec(
    key="example",
    display_name="Example",
    aliases=("ex",),
    chunker_language="example",
    chunker_aliases=("example", "ex"),
    chunk_extensions=(".ex",),
    gt_language="example",
    gt_extensions=(".ex",),
    graph_language="example",
    graph_aliases=("example", "ex"),
    graph_extensions=(".ex",),
    agent_languages=("example",),
    agent_aliases=(("example", "example"), ("ex", "example")),
    cold_start_backend="lsp",
    incremental_backend="lsp",
    core_decoder=False,
)
```

Keep surface-specific differences explicit. Existing C/C++ is the model:
repository chunking does not accept raw `c`, graph routing maps `c` to `cpp`,
and agent compile keeps `c` as its own scenario key.

Update or add tests in `test/test_languages.py` whenever a new alias,
extension, backend, or parity status is added.

You can start from generated TODO stubs:

```bash
python scripts/scaffold_language.py java \
  --display-name Java \
  --extension .java \
  --alias jvm \
  --graph-backend lsp \
  --incremental-backend lsp
```

The scaffold is dry-run by default. Add `--write` after reviewing the
`LanguageSpec` snippet and planned files. Generated files are intentionally not
registered in routers yet; fill in the implementation and tests before wiring
the language into `create_chunker()`, `LSIndexer`, `GraphPatcher`, or core
bindings.

## Step 2: Add Tree-Sitter Chunking

Add a chunker when the language should support retrieval or GT extraction.

1. Create `codeminer/code_chunking/{lang}_chunker.py`.
2. Export the chunker from `codeminer/code_chunking/__init__.py`.
3. Extend `create_chunker()` to instantiate the chunker from the registry
   normalized language key.
4. Add repository-level tests under `test/chunker/`.

Chunking-only support is valid. In that state:

- `chunker_language` and `chunk_extensions` should be set.
- `graph_language`, graph backends, and patchers can remain unset.
- User-facing code should advertise retrieval support, not full graph support.

## Step 3: Add Cold-Start Graph Support

Pick one cold-start backend:

- SCIP backend: add `codeminer/scip_interface/scip_indexer_{lang}.py` and
  `codeminer/scip_interface/scip_decode_{lang}.py`.
- clangd-style backend: add an `ls_index/` indexer/decoder only if the server
  exposes a stable artifact like clangd `.idx`.
- Generic LSP backend: use a shared LSP driver once it exists, with
  per-language server command, root markers, capabilities, and normalization
  rules coming from the language registry.

Then update `LSIndexer` / `LSGraphDecoder` routing. The graph must conform to
the current CodeGraph contract:

- same vertex `type`, `file`, `start_line`, `end_line`, and `unified_name`
  conventions;
- same edge `type`, `anchor_file`, and `anchor_line` conventions;
- line bases handled deliberately (`CodeChunk` uses 0-based lines, graph query
  APIs use the CodeGraph conventions documented in `docs/graph_query.md`).

## Step 4: Add Incremental Graph Patching

Add `codeminer/graph/incremental/patcher_{lang}.py` only after graph support
exists. Register it in the graph patcher router and add tests under
`test/graph/incremental/`.

The patcher should use registry-derived graph extensions so file detection does
not drift from cold-start graph support.

## Step 5: Add Agent and Dataset Coverage

Most alias and extension maps should come from `LanguageSpec`, not local
hard-coded tables.

Verify these surfaces:

- `codeminer/agent/compile.py` recognizes the intended scenario key.
- `codeminer/dataset/gt_locate.py` maps target file extensions correctly.
- repository chunking discovers the intended extensions through
  `RepoChunkingConfig`.
- synthesis or benchmark-specific language groups are updated only when the
  dataset actually contains that language group.

## Step 6: Add Optional Core Acceleration

Core acceleration is not required for first support.

If you add it:

1. Add the C++ decoder implementation under `core/`.
2. Thread every schema field through both serial and core decoders in the same
   PR.
3. Add a `test_core_{lang}` parity case or extend `test/scip/test_scip_core.py`.
4. Keep parity bit-for-bit for:
   - vertex name set;
   - edge multiset over source, target, type, anchor file, and anchor line;
   - per-vertex `type`, `file`, `start_line`, `end_line`, `unified_name`.

If a language is serial-only, track that explicitly in the registry and tests
instead of skipping parity indefinitely.

## Required Tests

For a chunking-only PR:

```bash
python -m pytest -q test/test_languages.py test/chunker/test_{lang}_chunker.py
```

For repository discovery changes:

```bash
python -m pytest -q test/test_languages.py test/chunker/test_repo_chunking_config.py
```

For graph cold-start changes:

```bash
python -m pytest -q test/scip/test_scip_indexer.py
python -m pytest -q test/scip/test_scip_multilingual.py -k {lang}
```

For incremental graph changes:

```bash
python -m pytest -q test/graph/incremental -k {lang}
```

For core acceleration changes:

```bash
python -m pytest -q test/scip/test_scip_core.py -k {lang}
```

Local environments may skip integration tests when external tools or fixture
repositories are unavailable. CI must run the parity jobs for accelerated
languages.

## PR Checklist

- [ ] `LanguageSpec` added or updated first.
- [ ] Aliases and extensions are tested in `test/test_languages.py`.
- [ ] Chunking support and graph support are capability-gated separately.
- [ ] New local maps are avoided unless they are dataset-specific.
- [ ] Cold-start backend and incremental backend are named explicitly.
- [ ] Backend alignment tolerances are documented when both SCIP and LSP exist.
- [ ] Serial/core parity is green for every accelerated language.
- [ ] Docs mention whether the language is chunking-only, serial graph, or
      accelerated graph.
