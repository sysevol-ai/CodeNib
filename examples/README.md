<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# CodeMiner examples

Runnable scripts that demonstrate CodeMiner's retrieval and agent stacks and
double as the evaluation harnesses used in the experiments under
[`docs/experiments/`](../docs/experiments/). They fall into four groups:

1. **Agent (end-to-end)** — the LLM agent loop over the skill/tool registry.
2. **Retrieval baselines** — pure retrieval pipelines (no agent loop).
3. **Third-party agent baselines** — Claude / Codex SDK localization agents.
4. **Sweeps & utilities** — matrix experiments and helpers.

## Prerequisites

| Need | When |
|------|------|
| `make dev` (editable install) | always |
| An embedding model (e.g. `nomic-ai/CodeRankEmbed`, GPU recommended) | embedding / rerank / agent retrieval |
| LLM credentials (`litellm` provider env, e.g. `GOOGLE_APPLICATION_CREDENTIALS` for Vertex) | anything with an LLM (agent loop, rerank, query synthesis) |
| A dataset — `fishmingyu/codeminer-base-dataset`, SWE-bench, or LocBench (cached under `~/.codeminer/`) | every eval script |

Most scripts accept `--filter-instance <regex>` to run a single instance and
`--result-path <file.json>` to write metrics. Generated output is **gitignored**
(`results/` and `*.json`); commit only curated evidence under
`docs/experiments/`.

---

## 1. Agent (end-to-end)

The agent loop lets an LLM pick CodeMiner skills/tools (`bm25_search`,
`embedding_search`, `graph_expand`, the always-on `file_read` / `file_search`
primitives, …) and iterate to a localization answer.

| Script | What it shows |
|--------|---------------|
| [`skill_agent_aot.py`](skill_agent_aot.py) | **Start here.** Two-phase AoT (ahead-of-time) flow via the public API: `compile_repo()` writes a `RepoManifest` + indexes, then `query()` runs the agent against it. Self-contained — defaults to indexing CodeMiner itself with BM25 only. |
| [`skill_agent.py`](skill_agent.py) | The same two-phase pattern wired **by hand** (`IndexCompiler` + `BM25CodeIndexer` + `AgentRunner`). Drop down to this for custom builder registries / partial rebuilds. |
| [`skill_agent_eval.py`](skill_agent_eval.py) | Evaluation driver: runs `AgentRunner` with an arbitrary skill subset (`--skills`) over SWE-bench / codeminer-base, reporting retrieval accuracy + token usage. Supports `--compile-table` for query-time skill selection (CAR). |

```bash
# Phase 1 only (no LLM credentials needed):
python examples/skill_agent_aot.py --no-llm

# Full agent run over one instance with the A2 (bm25 + embedding) subset:
python examples/skill_agent_eval.py \
    --skills bm25_search embedding_search \
    --filter-instance "^astropy__astropy-12907$"
```

---

## 2. Retrieval baselines (no agent)

Pure retrieval pipelines — fixed strategy, no LLM tool loop — for measuring the
retrieval floor the agent builds on.

| Script | Pipeline |
|--------|----------|
| [`bm25_retrieve_baseline.py`](bm25_retrieve_baseline.py) | Sparse BM25 only. |
| [`embedding_retrieve_baseline.py`](embedding_retrieve_baseline.py) | Dense embedding (FAISS) only. Supports **`--index-type flat\|ivf`** (see below). |
| [`graph_retrieve_baseline.py`](graph_retrieve_baseline.py) | Symbol-graph / GraphRAG retrieval. |
| [`retrieve_rerank.py`](retrieve_rerank.py) | Retrieve → rerank cascade (`RetrieveRerankPipeline`). |
| [`agentless.py`](agentless.py) | Agentless-style retrieval pipeline. |

```bash
python examples/embedding_retrieve_baseline.py \
    --dataset codeminer_base --topk 50 \
    --embedding-model nomic-ai/CodeRankEmbed \
    --result-path results/embedding_baseline.json
```

### FAISS index type: flat vs IVF

The embedding store defaults to a **flat** index (`IndexFlat{IP,L2}`) — exact
brute-force search, which is the right choice for per-repo corpora (hundreds to
a few thousand chunks). For larger corpora it also supports an **IVF**
inverted-file index (`IndexIVFFlat`) — approximate, faster at scale:

```bash
# IVF takes effect at BUILD time, so combine with --force-rebuild
# (or build the prebuilt indices with --index-type ivf):
python examples/embedding_retrieve_baseline.py \
    --dataset codeminer_base --force-rebuild \
    --index-type ivf --ivf-nlist 256 --ivf-nprobe 16
```

- `--ivf-nlist` — number of Voronoi cells. Clamped down to the corpus size on
  small repos (FAISS k-means needs ≥ `nlist` training points); the index trains
  lazily on the first batch of vectors.
- `--ivf-nprobe` — cells probed per query; the recall/latency knob (clamped to
  the effective `nlist`). `nprobe == nlist` recovers exact (flat-equivalent)
  results.

The same flags exist on the index builder
[`scripts/embeddings/build_embeddings.py`](../scripts/embeddings/build_embeddings.py),
which is where prebuilt indices get their type.

---

## 3. Third-party agent baselines (`codeminer/clients/`)

Read-only localization agents built on **external** vendor SDKs (not CodeMiner's
own agent stack), scored against the same ground truth as the retrieval
baselines via [`codeminer/eval/loc_agent_runner.py`](../codeminer/eval/loc_agent_runner.py).
The vendor SDKs are intentionally **not** declared in `pyproject.toml` — install
them yourself.

| Script | Agent | Extra install |
|--------|-------|---------------|
| [`claude_loc_agent.py`](claude_loc_agent.py) | `ClaudeLocAgent` over `claude_agent_sdk` | `pip install claude-agent-sdk` |
| [`codex_loc_agent.py`](codex_loc_agent.py) | `CodexLocAgent` over OpenAI's `openai_codex` | see the script's header (GitHub, not PyPI) |

Both lock down writes (read-only sandbox + approval-deny) and emit symbol names
in the chunker's canonical form, so per-instance scoring is exact `file:name`
match. Datasets: `codeminer_base`, `swebench_lite`, `locbench_v1`. Runs are
resumable (`--resume`).

```bash
python examples/claude_loc_agent.py \
    --dataset codeminer_base --model claude-sonnet-4-6 \
    --result-path results/claude_loc.jsonl --resume
```

---

## 4. Sweeps & utilities

| File | Purpose |
|------|---------|
| [`codeminer_base_rerank_matrix.py`](codeminer_base_rerank_matrix.py) | Cartesian retrieval × rerank matrix sweep over codeminer-base. |
| [`eval_synthesized_queries.py`](eval_synthesized_queries.py) | Evaluate synthesized behavioral queries against retrieval backends. |
| [`eval.sh`](eval.sh) | Convenience wrapper around the eval drivers. |
| [`selected_instance.csv`](selected_instance.csv) | Small fixed instance set used by some sweeps. |
| [`roadmap_semantic_search_mcp.md`](roadmap_semantic_search_mcp.md) | Design note: semantic search over MCP. |

For the agent-compile (CAR) sweep harness and its configs, see
[`scripts/agent_compile/`](../scripts/agent_compile/) and
[`docs/experiments/agent_compile.md`](../docs/experiments/agent_compile.md).
