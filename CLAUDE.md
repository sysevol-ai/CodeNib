<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# CLAUDE.md

CodeMiner is a code analysis agent with graph-enhanced search. It parses
multi-language codebases with tree-sitter, builds semantic graphs (igraph),
and provides hybrid retrieval (BM25 + FAISS/Milvus embeddings + regex/trigram +
LLM re-ranking) via litellm. Retrieval is exposed both as composable agent
skills and over the Model Context Protocol (MCP). Python 3.10+.

## Package structure

```
codeminer/
  code_chunking/   # Tree-sitter chunkers: Python, Go, Rust, C++, JS/TS
  graph/           # CodeGraph (igraph), ROI subgraph, range queries; graph/incremental/ LSP patchers
  dataset/         # SWE-bench loading, ground-truth extraction, query synthesis
  index/           # BM25 sparse, FAISS/embedding, regex node, trigram (Zoekt) indexes
  incremental/     # Incremental embedding pipeline (git-diff driven chunk/index updates)
  agent/           # Keyword extraction, re-ranking, resource guards, skills + runner
  compiler/        # Two-phase index compilation: IndexCompiler -> RepoManifest
  mcp/             # Model Context Protocol server (semantic/BM25/regex/Zoekt search)
  model/           # Retrieval pipelines (bm25/embedding/graph/rerank) + agentless prompts
  llm/             # litellm-based chat interface
  scip_interface/  # SCIP indexing bindings (proto, shell scripts)
  ls_index/        # Language server index (clangd, rust-analyzer, scip-typescript)
  ops/             # Graph operations (expand, traverse)
  eval/            # Evaluation utilities
core/              # C++ decoder backend (libigraph) mirroring CodeGraph/SCIPGraphDecoder
test/              # Mirrors package structure; uses pytest markers
scripts/           # CLI entry points: dataset collection, embedding, evaluation
third_party/       # Git submodules (scip-python)
```

## Dev commands

```bash
make dev          # pip install -e ".[dev,test]"
make test         # pytest
make scip         # Install SCIP toolchain (rust-analyzer, scip-clang, TS, Python)
make install      # pip install -e .
```

Pre-commit hooks: black (line-length 88), isort, flake8+bugbear, clang-format for C/C++.

## Testing

Three pytest marker tiers:

| Marker | Scope | Duration |
|--------|-------|----------|
| _(none)_ | Unit — pure logic, mocks only | ~1 min |
| `integration` | Repo cloning, SCIP indexing, chunkers | ~15 min |
| `slow` | LLM API calls, GPU embeddings | ~15 min |

```bash
pytest -m "not slow and not integration" -x   # unit only
pytest -m integration                          # integration only
pytest -m slow                                 # slow only
```

- Test fixtures cache repos to `/tmp/codeminer-gt-test/`
- HuggingFace dataset cache: `~/.codeminer/`

## Key conventions

- **Line numbering**: `CodeChunk.start_line`/`end_line` are **0-based** (tree-sitter).
  `CodeLocation.start_line`/`end_line` are **1-based** (output/HuggingFace).
  `_chunk_to_code_block()` in `dataset/gt_locate.py` does the +1 conversion.
- **`.c` files** use the `cpp` chunker (not a separate C chunker).
- **Chunking granularity**: L0 = file, L1 = top-level symbols, L2 = nested/methods.
- **Symbol chunk types**: `function`, `method`, `class`, `struct`, `type`,
  `interface`, `object`, `enum`, `trait`, `impl`, `var`, `const`, `static`,
  `declaration`, `macro`, `variable`.

## CI

Three parallel jobs on a self-hosted runner (see `.github/workflows/ci.yml`):

1. **unit** — fast, no external deps
2. **integration** — needs SCIP, clangd, rust-analyzer, bear
3. **slow** — needs LLM API keys, GPU

Skip mechanisms:
- `paths-ignore`: `**.md`, `docs/**`, `LICENSE`, `.gitignore`
- `[skip tests]` in commit message or PR title
- `skip-tests` label on PR
- `workflow_dispatch` with `skip_tests: true`
