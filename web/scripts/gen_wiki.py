#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Generate the DeepWiki-style site data from CodeMiner source.

Walks ``codeminer/`` and uses the stdlib ``ast`` module to extract module
docstrings and top-level classes/functions (with methods, line numbers, and
docstrings). Emits ``web/data/wiki.json`` consumed by the Next.js app. Pure
stdlib + git; no network or LLM required, fully deterministic.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PKG_DIR = REPO_ROOT / "codeminer"
OUT = REPO_ROOT / "web" / "data" / "wiki.json"

GITHUB_BASE = "https://github.com/sysevol-ai/CodeMiner/blob/main"

# Curated, human-readable module blurbs (sourced from CLAUDE.md).
MODULE_INFO = {
    "code_chunking": ("Code Chunking", "Tree-sitter chunkers for Python, Go, Rust, C/C++, and JS/TS. Splits source into L0 (file), L1 (top-level), and L2 (nested/method) chunks."),
    "graph": ("Code Graph", "CodeGraph over igraph: ROI subgraphs, range queries, and an incremental LSP-driven patcher that keeps the graph in sync with edits."),
    "dataset": ("Dataset", "SWE-bench loading, ground-truth extraction, and query synthesis used to build and evaluate retrieval datasets."),
    "index": ("Indexes", "BM25 sparse retrieval, FAISS/embedding dense search, a regex node index, and a trigram (Zoekt) index over raw file contents."),
    "incremental": ("Incremental", "Git-diff driven incremental embedding pipeline that re-chunks and re-indexes only what changed."),
    "agent": ("Agent", "Keyword extraction, LLM re-ranking, resource guards, and the skill registry + runner that drive tool-calling retrieval."),
    "compiler": ("Compiler", "Two-phase index compilation: IndexCompiler builds artifacts and writes a RepoManifest that the agent consumes at query time."),
    "mcp": ("MCP Server", "Model Context Protocol server exposing semantic, BM25, regex, and Zoekt search over a pre-built index via stdio."),
    "model": ("Retrieval Models", "Composable retrieval pipelines (BM25 / embedding / graph / rerank) plus agentless prompting strategies."),
    "llm": ("LLM Interface", "litellm-based chat interface with structured-output and token-usage tracking, provider-agnostic."),
    "scip_interface": ("SCIP Interface", "SCIP indexing bindings: protobuf schema and shell scripts wrapping the SCIP toolchain."),
    "ls_index": ("Language Server Index", "Language-server-backed indexing via clangd, rust-analyzer, and scip-typescript."),
    "ops": ("Graph Ops", "Graph operations — expand and traverse — plus retrieve/rerank context objects used by skills."),
    "eval": ("Evaluation", "Evaluation utilities for measuring retrieval and locator accuracy."),
    "web": ("Web Demo", "DeepWiki-style demo: a FastAPI backend wrapping the agent and a Next.js documentation frontend."),
}

# Ordering for the architecture flow (left-to-right pipeline).
MODULE_ORDER = [
    "code_chunking", "graph", "index", "incremental", "compiler",
    "ops", "model", "agent", "llm", "mcp", "scip_interface", "ls_index",
    "dataset", "eval", "web", "core",
]


def run_git(*args: str) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(REPO_ROOT), *args], text=True
        ).strip()
    except Exception:
        return ""


def short_doc(node: ast.AST, limit: int = 320) -> str:
    doc = ast.get_docstring(node) or ""
    doc = doc.strip()
    if not doc:
        return ""
    # First paragraph only.
    para = doc.split("\n\n")[0].replace("\n", " ").strip()
    if len(para) > limit:
        para = para[: limit - 1].rstrip() + "…"
    return para


def fmt_args(fn: ast.AST) -> str:
    try:
        a = fn.args
        parts = [arg.arg for arg in a.posonlyargs] + [arg.arg for arg in a.args]
        if a.vararg:
            parts.append("*" + a.vararg.arg)
        if a.kwonlyargs:
            if not a.vararg:
                parts.append("*")
            parts += [arg.arg for arg in a.kwonlyargs]
        if a.kwarg:
            parts.append("**" + a.kwarg.arg)
        return "(" + ", ".join(parts) + ")"
    except Exception:
        return "()"


def parse_file(path: Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
    loc = text.count("\n") + 1
    rel = path.relative_to(REPO_ROOT).as_posix()
    entry = {
        "path": rel,
        "name": path.name,
        "language": "python",
        "lines": loc,
        "docstring": "",
        "symbols": [],
        "source_url": f"{GITHUB_BASE}/{rel}",
    }
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        entry["parse_error"] = str(exc)
        return entry

    entry["docstring"] = short_doc(tree, limit=500)

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            methods = []
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if sub.name.startswith("_") and sub.name != "__init__":
                        continue
                    methods.append({
                        "name": sub.name,
                        "signature": sub.name + fmt_args(sub),
                        "lineno": sub.lineno,
                        "docstring": short_doc(sub, limit=200),
                    })
            entry["symbols"].append({
                "kind": "class",
                "name": node.name,
                "signature": "class " + node.name,
                "lineno": node.lineno,
                "end_lineno": getattr(node, "end_lineno", node.lineno),
                "docstring": short_doc(node),
                "methods": methods,
            })
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("_"):
                continue
            prefix = "async def " if isinstance(node, ast.AsyncFunctionDef) else "def "
            entry["symbols"].append({
                "kind": "function",
                "name": node.name,
                "signature": prefix + node.name + fmt_args(node),
                "lineno": node.lineno,
                "end_lineno": getattr(node, "end_lineno", node.lineno),
                "docstring": short_doc(node),
                "methods": [],
            })
    return entry


def build_modules() -> list[dict]:
    buckets: dict[str, list[dict]] = {}
    for py in sorted(PKG_DIR.rglob("*.py")):
        if "__pycache__" in py.parts:
            continue
        rel_parts = py.relative_to(PKG_DIR).parts
        mod = rel_parts[0] if len(rel_parts) > 1 else "core"
        buckets.setdefault(mod, []).append(parse_file(py))

    modules = []
    for mod_id in sorted(buckets, key=lambda m: (MODULE_ORDER.index(m) if m in MODULE_ORDER else 99, m)):
        files = sorted(buckets[mod_id], key=lambda f: f["path"])
        title, desc = MODULE_INFO.get(mod_id, (mod_id.replace("_", " ").title(), ""))
        if mod_id == "core":
            title, desc = ("Core", "Top-level package modules: shared types, logging, and the public API surface.")
        sym_count = sum(len(f["symbols"]) for f in files)
        loc = sum(f["lines"] for f in files)
        modules.append({
            "id": mod_id,
            "name": mod_id,
            "title": title,
            "description": desc,
            "path": f"codeminer/{mod_id}" if mod_id != "core" else "codeminer",
            "file_count": len(files),
            "symbol_count": sym_count,
            "loc": loc,
            "files": files,
        })
    return modules


def architecture_mermaid() -> str:
    return (
        "flowchart LR\n"
        "  subgraph Ingest[Ingestion]\n"
        "    CC[code_chunking]\n"
        "    SCIP[scip_interface]\n"
        "    LS[ls_index]\n"
        "  end\n"
        "  subgraph Build[Index build]\n"
        "    G[graph]\n"
        "    IDX[index]\n"
        "    INC[incremental]\n"
        "    COMP[compiler]\n"
        "  end\n"
        "  subgraph Query[Query time]\n"
        "    MODEL[model]\n"
        "    OPS[ops]\n"
        "    AGENT[agent]\n"
        "    LLM[llm]\n"
        "  end\n"
        "  CC --> G\n"
        "  SCIP --> G\n"
        "  LS --> G\n"
        "  CC --> IDX\n"
        "  G --> COMP\n"
        "  IDX --> COMP\n"
        "  INC --> IDX\n"
        "  COMP -->|RepoManifest| AGENT\n"
        "  AGENT --> OPS\n"
        "  AGENT --> MODEL\n"
        "  AGENT --> LLM\n"
        "  AGENT --> MCP[mcp server]\n"
        "  MCP --> WEB[web demo]\n"
        "  AGENT --> WEB\n"
    )


def curated_pages(repo: dict) -> list[dict]:
    overview = f"""# Overview

**{repo['name']}** is {repo['description']}

It parses multi-language codebases with tree-sitter, builds semantic graphs
(igraph), and provides hybrid retrieval — BM25 + FAISS/Milvus embeddings +
regex/trigram + LLM re-ranking — over [litellm](https://github.com/BerriAI/litellm).
Retrieval is exposed both as composable agent **skills** and over the
**Model Context Protocol (MCP)**.

## At a glance

| | |
|---|---|
| Language | {", ".join(repo['languages'])} |
| Supports parsing | {", ".join(repo['supports'])} |
| License | {repo['license']} |
| Python files | {repo['file_count']} |
| Lines of code | {repo['loc']:,} |

## Where to start

- **Architecture** — how data flows from source to answer.
- **Retrieval Pipeline** — the BM25 / embedding / graph / rerank stages.
- **Testing & CI** — the three pytest tiers and the CI jobs.
- Browse any **module** in the sidebar to drill into its files and symbols.
"""

    architecture = """# Architecture

CodeMiner follows a **two-phase** design that cleanly separates expensive
index building from fast query-time retrieval.

```mermaid
flowchart TB
  subgraph P1[Phase 1 - repo time]
    A[Source tree] --> B[code_chunking]
    B --> C[graph: CodeGraph]
    B --> D[index: BM25 / vector / regex / zoekt]
    C --> E[compiler: IndexCompiler]
    D --> E
    E --> M[(RepoManifest)]
  end
  subgraph P2[Phase 2 - query time]
    M --> R[agent: AgentRunner]
    R --> S[skills: bm25 / embedding / hybrid / rerank]
    S --> O[ops: retrieve + expand]
    R --> L[llm: litellm]
    R --> ANS[Answer + citations]
  end
```

## Phase 1 — index compilation

`IndexCompiler.compile_repo()` runs the registered builders (BM25, vector,
symbol graph) and writes a `RepoManifest` recording **what** was built,
**where**, and its **freshness**. The manifest is the linking protocol
between build time and query time.

## Phase 2 — query-time retrieval

`AgentRunner` loads the manifest, filters skills whose indexes are unavailable
via a `ResourceGuard`, and runs a think → act → observe loop: the LLM picks
skills (BM25 for exact names, embeddings for intent, hybrid for coverage),
executes them, and produces a grounded answer.

## Surfaces

The same retrieval core is exposed two ways: as an **MCP server** (stdio) for
AI tools, and through the **web demo** in this site.
"""

    pipeline = """# Retrieval Pipeline

CodeMiner is a **hybrid** retriever. Each stage is a skill the agent can call.

## Stages

1. **BM25 (sparse)** — `index/sparse_idx`. Best for exact symbol names and
   keyword lookups.
2. **Embeddings (dense)** — `index/embedding` over FAISS/Milvus. Best for
   natural-language / intent queries.
3. **Regex & Trigram** — `index/regex_idx` and the Zoekt trigram index for
   structural pattern matching and fast substring search across raw files.
4. **Graph expansion** — `graph` + `ops` expand an initial hit to structurally
   related code (callers, containers, references).
5. **LLM re-ranking** — `agent/rerank_agent` reorders candidates by relevance
   using listwise prompting.

## Fusion

`ops/retrieve.merge_hybrid()` fuses multiple ranked branches with weighted
scoring and de-duplicates by `(file, symbol, line range)`.
"""

    testing = """# Testing & CI

## Three pytest tiers

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

## CI

Three parallel jobs on a self-hosted runner: **unit**, **integration**
(needs SCIP, clangd, rust-analyzer, bear), and **slow** (LLM keys, GPU).
Skips: `paths-ignore` for docs, `[skip tests]` in the commit/PR title, or a
`skip-tests` label.
"""

    return [
        {"id": "overview", "title": "Overview", "markdown": overview},
        {"id": "architecture", "title": "Architecture", "markdown": architecture},
        {"id": "pipeline", "title": "Retrieval Pipeline", "markdown": pipeline},
        {"id": "testing", "title": "Testing & CI", "markdown": testing},
    ]


def main() -> None:
    modules = build_modules()
    file_count = sum(m["file_count"] for m in modules)
    loc = sum(m["loc"] for m in modules)

    commit = run_git("log", "-1", "--format=%H")
    commit_date = run_git("log", "-1", "--format=%cI")
    commit_subject = run_git("log", "-1", "--format=%s")

    repo = {
        "name": "CodeMiner",
        "description": (
            "a code analysis agent with graph-enhanced search — an LLM-adapted "
            "code indexing and retrieval system for multi-language codebases."
        ),
        "languages": ["Python"],
        "supports": ["Python", "Go", "Rust", "C/C++", "JavaScript", "TypeScript"],
        "license": "Apache-2.0",
        "commit": commit,
        "commit_short": commit[:8],
        "commit_date": commit_date,
        "commit_subject": commit_subject,
        "file_count": file_count,
        "loc": loc,
        "github": GITHUB_BASE.rsplit("/blob/", 1)[0],
    }

    data = {
        "repo": repo,
        "architecture_mermaid": architecture_mermaid(),
        "pages": curated_pages(repo),
        "modules": modules,
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(
        f"Wrote {OUT.relative_to(REPO_ROOT)}: {len(modules)} modules, "
        f"{file_count} files, {loc:,} LOC, "
        f"{sum(len(f['symbols']) for m in modules for f in m['files'])} symbols"
    )


if __name__ == "__main__":
    main()
