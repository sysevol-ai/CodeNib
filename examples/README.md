<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# CodeNib examples

Runnable scripts that demonstrate CodeNib's retrieval and agent stacks and
double as the evaluation harnesses used in the experiments under
[`docs/experiments/`](../docs/experiments/). They fall into five groups:

1. **Agent (end-to-end)** — the LLM agent loop over the skill/tool registry.
2. **Retrieval baselines** — pure retrieval pipelines (no agent loop).
3. **Third-party policy baselines** — Claude, Codex, LocAgent, and OrcaLoca
   localization.
4. **Agent integrations** — dependency-free providers for external runtimes.
5. **Sweeps & utilities** — matrix experiments and helpers.

## Prerequisites

| Need | When |
|------|------|
| `make dev` (editable install) | always |
| An embedding model (e.g. `nomic-ai/CodeRankEmbed`, GPU recommended) | embedding / rerank / agent retrieval |
| LLM credentials (`litellm` provider env, e.g. `GOOGLE_APPLICATION_CREDENTIALS` for Vertex) | anything with an LLM (agent loop, rerank, query synthesis) |
| A dataset — `fishmingyu/codeminer-base-dataset`, SWE-bench, or LocBench (cached under `~/.codenib/`) | every eval script |

Most scripts accept `--filter-instance <regex>` to run a single instance and
`--result-path <file.json>` to write metrics. Generated output is **gitignored**
(`results/` and `*.json`); commit only curated evidence under
`docs/experiments/`.

---

## 1. Agent (end-to-end)

The agent loop lets an LLM pick CodeNib skills/tools (`bm25_search`,
`embedding_search`, `codenib_context`, the always-on `read` / `grep` /
`glob` / `bash` tools, …) and iterate to a localization answer.

| Script | What it shows |
|--------|---------------|
| [`skill_agent_aot.py`](skill_agent_aot.py) | **Start here.** Two-phase AoT (ahead-of-time) flow via the public API: `compile_repo()` writes a `RepoManifest` + indexes, then `query()` runs the agent against it. Self-contained — defaults to indexing CodeNib itself with BM25 only. |
| [`skill_agent.py`](skill_agent.py) | The same two-phase pattern wired **by hand** (`IndexCompiler` + `BM25CodeIndexer` + `AgentRunner`). Drop down to this for custom builder registries / partial rebuilds. |
| [`skill_agent_eval.py`](skill_agent_eval.py) | Evaluation driver: runs `AgentRunner` with an arbitrary skill subset (`--skills`) over SWE-bench / codenib-base, reporting retrieval accuracy + token usage. Supports `--compile-table` for query-time skill selection (CAR). |

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
| [`swerank_retrieve_rerank.py`](swerank_retrieve_rerank.py) | Runnable SweRank recipe over one local repository. |
| [`agentless.py`](agentless.py) | Agentless-style retrieval pipeline. |

```bash
python examples/embedding_retrieve_baseline.py \
    --dataset codenib_base --topk 50 \
    --embedding-model nomic-ai/CodeRankEmbed \
    --result-path results/embedding_baseline.json
```

### SweRank retrieve → rerank

The recipe makes the candidate funnel explicit: SweRankEmbed-Small retrieves
100 L2 code chunks, the selected reranker scores the first 30, and CodeNib
returns the final 10 by default. Install the semantic stack first:

```bash
pip install "codenib[semantic,agent]"
```

For the SweRankLLM listwise route, serve SweRankLLM-Small through an
OpenAI-compatible endpoint and run the recipe in a second terminal:

```bash
pip install vllm
vllm serve Salesforce/SweRankLLM-Small \
  --served-model-name swerank-llm-small --port 9000

python examples/swerank_retrieve_rerank.py /path/to/repo \
  --query "Issue or change request to localize"
```

The one-process alternative uses SweRankEmbed-Large to rescore the candidate
contents and does not need an LLM server. The model card requires a CUDA stack
with FlashAttention for this 7B route:

```bash
python examples/swerank_retrieve_rerank.py /path/to/repo \
  --query "Issue or change request to localize" \
  --reranker embed-large
```

The default dense-index cache is outside the checkout and keyed by the clean
Git commit or, for dirty and non-Git repositories, a source-content
fingerprint. A versioned build-profile key also separates language and chunking
configurations. Pass `--index-dir` only when the caller manages those identity
boundaries itself.

Both model routes apply the prompt names published by the model cards through
CodeNib's embedding prompt registry and L2-normalize vectors before inner-product
scoring. SweRank weights use CC-BY-NC-4.0 and were trained for issue localization
on Python repositories; other languages are a supported execution path, not a
retained quality claim. CodeNib implements the published RankGPT prompt and
permutation parser but retains its own overlapping window score aggregation, so
this is an integration recipe rather than a claim of bit-for-bit reproduction
of the upstream evaluation harness.

### FAISS index type: flat vs IVF

The embedding store defaults to a **flat** index (`IndexFlat{IP,L2}`) — exact
brute-force search, which is the right choice for per-repo corpora (hundreds to
a few thousand chunks). For larger corpora it also supports an **IVF**
inverted-file index (`IndexIVFFlat`) — approximate, faster at scale:

```bash
# IVF takes effect at BUILD time, so combine with --force-rebuild
# (or build the prebuilt indices with --index-type ivf):
python examples/embedding_retrieve_baseline.py \
    --dataset codenib_base --force-rebuild \
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

## 3. Third-party policy baselines (`codenib/clients/`)

Read-only localization agents built on **external policies** (not CodeNib's own
agent stack), scored against the same ground truth as the retrieval baselines
via [`codenib/eval/agent_runner/loc_baseline.py`](../codenib/eval/agent_runner/loc_baseline.py).
Their upstream runtimes remain optional; install only the policy being
evaluated.

| Script | Agent | Extra install |
|--------|-------|---------------|
| [`claude_loc_agent.py`](claude_loc_agent.py) | `ClaudeLocAgent` over `claude_agent_sdk` | `pip install claude-agent-sdk` |
| [`codex_loc_agent.py`](codex_loc_agent.py) | `CodexLocAgent` over OpenAI's `openai_codex` | see the script's header (GitHub, not PyPI) |
| [`locagent_loc_agent.py`](locagent_loc_agent.py) | Pinned LocAgent policy over CodeNib's manifest-backed tools | `pip install "codenib[agent,graph]"` |
| [`orcaloca_loc_agent.py`](orcaloca_loc_agent.py) | OrcaLoca policy over CodeNib's manifest-backed graph | pinned OrcaLoca checkout and its LlamaIndex dependencies |

The Claude and Codex wrappers lock down writes (read-only sandbox +
approval-deny). All four adapters emit symbol names in the chunker's canonical
form, so per-instance scoring is exact `file:name` match. Datasets:
`codenib_base`, `swebench_lite`, `locbench_v1`. Runs are resumable
(`--resume`).

```bash
python examples/claude_loc_agent.py \
    --dataset codenib_base --model claude-sonnet-4-6 \
    --result-path results/claude_loc.jsonl --resume
```

LocAgent and OrcaLoca use the same dataset loop and JSONL schema. Pre-index
each checkout with the graph preset, then run either policy:

```bash
python examples/locagent_loc_agent.py \
    --dataset codenib_base --model "$LOCAGENT_MODEL" \
    --result-path results/locagent_loc.jsonl --resume

python examples/orcaloca_loc_agent.py \
    --dataset codenib_base --model "$ORCALOCA_MODEL" \
    --orcaloca-checkout /path/to/OrcaLoca \
    --result-path results/orcaloca_loc.jsonl --resume
```

---

## 4. Agent integrations

These examples exercise CodeNib's external-agent providers without installing
the upstream agent. The production adapters remain under
[`codenib/integrations/`](../codenib/integrations/); examples contain only
manifest loading and provider calls.

| Script | What it shows |
|--------|---------------|
| [`integrations/locagent.py`](integrations/locagent.py) | Load a graph-enabled manifest and call LocAgent-compatible search, entity, or tree tools. |
| [`integrations/orcaloca.py`](integrations/orcaloca.py) | Load a graph-enabled manifest and call OrcaLoca-compatible tree or class search. |

```bash
codenib index /path/to/repository --preset graph
python examples/integrations/locagent.py \
  --manifest /path/to/repo_manifest.json \
  --search "configuration loader"

python examples/integrations/orcaloca.py \
    --manifest /path/to/repo_manifest.json
```

See [Agent Integrations](../docs/agent_integrations.md) for upstream injection,
revision pins, and compatibility boundaries.

---

## 5. Sweeps & utilities

| File | Purpose |
|------|---------|
| [`codenib_base_rerank_matrix.py`](codenib_base_rerank_matrix.py) | Cartesian retrieval × rerank matrix sweep over codenib-base. |
| [`eval_synthesized_queries.py`](eval_synthesized_queries.py) | Evaluate synthesized behavioral queries against retrieval backends. |
| [`eval.sh`](eval.sh) | Convenience wrapper around the eval drivers. |
| [`selected_instance.csv`](selected_instance.csv) | Small fixed instance set used by some sweeps. |
| [`roadmap_semantic_search_mcp.md`](roadmap_semantic_search_mcp.md) | Design note: semantic search over MCP. |

For the agent-compile (CAR) sweep harness and its configs, see
[`scripts/agent_compile/`](../scripts/agent_compile/) and
[`docs/experiments/agent_compile.md`](../docs/experiments/agent_compile.md).
