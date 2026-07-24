# CLAUDE.md

CodeNib is a code analysis agent with graph-enhanced search. It parses
multi-language codebases with tree-sitter, builds semantic graphs (igraph),
and provides hybrid retrieval (BM25 + FAISS/Milvus embeddings + regex/trigram +
LLM re-ranking) via litellm. Retrieval is exposed both as composable agent
skills and over the Model Context Protocol (MCP). Python 3.10+.

> **Layered rules.** This is the project-wide file (always loaded). Each major
> subtree carries its own `CLAUDE.md` with deeper, domain-specific rules that
> load on demand when you touch files in that directory. See
> [Per-directory rules](#per-directory-rules) below.

## Package structure

```
codeminer/
  code_chunking/   # Tree-sitter chunkers: Python, Go, Rust, C++, JS/TS  (CLAUDE.md)
  graph/           # CodeGraph (igraph), ROI subgraph, range queries; graph/incremental/ LSP patchers  (CLAUDE.md)
  dataset/         # SWE-bench loading, ground-truth extraction, query synthesis  (CLAUDE.md)
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
test/              # Mirrors package structure; uses pytest markers  (CLAUDE.md)
scripts/           # CLI entry points: dataset collection, embedding, evaluation
                   #   scripts/agent_compile/ — agent-compile RFC tooling  (CLAUDE.md)
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

## Critical conventions

These bite across the whole codebase — full details live in the per-directory
rules, but keep these in mind everywhere:

- **Line numbering**: `CodeChunk.start_line`/`end_line` are **0-based**
  (tree-sitter); `CodeLocation.start_line`/`end_line` are **1-based**
  (output/HuggingFace). `_chunk_to_code_block()` in `dataset/gt_locate.py` does
  the +1 conversion. See [`codeminer/dataset/CLAUDE.md`](../codeminer/dataset/CLAUDE.md)
  and [`codeminer/code_chunking/CLAUDE.md`](../codeminer/code_chunking/CLAUDE.md).
- **`.c` files** use the `cpp` chunker (not a separate C chunker).

## Frontend / web demo

The DeepWiki-style web demo (FastAPI backend `codeminer/web/` + Next.js frontend
`web/` + `codeminer/wiki/`) is on `main` (merged via PR #166/#167). To run,
screenshot, or iterate the UI:

- **How to run, screenshot (Playwright), and self-critique** the demo:
  [`.claude/guides/frontend-loop.md`](guides/frontend-loop.md).
- **Where the graph UI is headed** (Cytoscape overhaul, the compiler-precise
  edge-provenance differentiator, wiki reframe):
  [`.claude/design/graph-frontend-direction.md`](design/graph-frontend-direction.md).

## Git & PR conventions

Rules for any AI/code agent (Claude Code, etc.) committing or opening PRs here.

- **No agent attribution.** Do **not** add `Co-Authored-By: Claude ...`,
  `🤖 Generated with Claude Code`, session links, or any "made by an AI"
  footer to commit messages, PR bodies, or review comments. This is enforced by
  `attribution: { commit: "", pr: "" }` in `.claude/settings.json`.
- **Commit messages**: Conventional Commits — `type(scope): summary`, where
  `type` is `feat`/`fix`/`docs`/`refactor`/`perf`/`test`/`chore`/`ci`.
  Imperative mood, ≤72-char subject; body explains *why* + how it was verified.
- **PRs must follow `.github/PULL_REQUEST_TEMPLATE.md` verbatim** — keep every
  section heading (`Summary`, `Changes`, `Type of Change`, `Testing`,
  `Checklist`) in order. Fill the bullets, tick the relevant `- [x]` boxes;
  do not substitute ad-hoc sections. Add extra context *below* the template,
  never in place of it.

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

Full pytest marker reference: [`test/CLAUDE.md`](../test/CLAUDE.md).

## Per-directory rules

Domain rules live next to the code they govern and load on demand:

| Path | Covers |
|------|--------|
| [`codeminer/code_chunking/CLAUDE.md`](../codeminer/code_chunking/CLAUDE.md) | Chunk depth (L0/L1/L2), per-language chunk types, line-number origin |
| [`codeminer/graph/CLAUDE.md`](../codeminer/graph/CLAUDE.md) | CodeGraph (igraph), node/edge types, pickle schema versioning, C++ decoder parity |
| [`codeminer/dataset/CLAUDE.md`](../codeminer/dataset/CLAUDE.md) | SWE-bench loading, ground-truth extraction, line conversion, test repos |
| [`test/CLAUDE.md`](../test/CLAUDE.md) | pytest marker tiers, fixture caches, package-shadow gotcha |
| [`scripts/agent_compile/CLAUDE.md`](../scripts/agent_compile/CLAUDE.md) | Agent-compile RFC tooling, phase lineage, RepoManifest/IndexCompiler |

When you edit code under one of these subtrees, follow its `CLAUDE.md` in
addition to this file. If a rule changes, update the file nearest the code.
