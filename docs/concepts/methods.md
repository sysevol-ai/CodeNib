<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Context methods and support matrix

Every CodeNib method answers the same question: given a repository snapshot
and a task description, which source regions should a model read? The methods
differ in three ways:

1. **Views**: which precomputed indexes they read, if any.
2. **Control**: who decides what to look at next. It can be a fixed pipeline,
   a fixed sequence of LLM calls, or an LLM that keeps choosing tools.
3. **Surface**: how the method is reached: the Python API, the `codenib` CLI,
   MCP, or the Web UI.

This page sorts every supported method along those axes. Each method keeps its
own page with commands and limits. For measured results, see
[Benchmarks](../evaluation/index.md).

```text
VIEWS       bm25 · vector · symbol_graph · regex · zoekt
  │         built once per snapshot; grep reads the live checkout
  ▼
OPERATORS   retrieve + fuse · filter · expand · rerank
  │
  ▼
METHODS     A  one-shot retrieval    a fixed pipeline ranks once
  │         B  fixed LLM workflow    an LLM fills fixed stages
  │         C  agentic tool loop     an LLM picks the next tool
  ▼
SURFACES    Python API · codenib CLI · MCP tools · Web / Wiki
```

## Support matrix

**Type** combines the paradigm (A, B, or C, defined below) with the method's
role:

| Role | Means |
| --- | --- |
| CodeNib | A CodeNib method: CodeNib owns the pipeline, workflow, or agent loop |
| Reproduced | A published method reimplemented or vendored on top of CodeNib's views, with its upstream contract pinned; see [Reproduced agents](../agent_integrations.md) |
| Client | An external agent that owns its loop and reaches CodeNib through MCP |
| Control | An agent run without any CodeNib tool, to show how far file tools alone get on the same cases |

The **Evidence** column shows the strongest claim the method has reached on
any benchmark or upstream contract, using the
[evidence levels](../evaluation/index.md#evidence-levels) from Benchmarks:
*Tested* (CodeNib's test suite covers it), *Runnable* (a harness scores it on a
benchmark), *Pinned* (checked against a fixed upstream revision), and
*Measured* (a complete result on a fixed, published case set). Only *Measured*
is a quality claim, and only at its benchmark's
[layer](../evaluation/index.md#three-layers). The
[method × benchmark matrix](../evaluation/index.md#method-benchmark-matrix)
breaks this down per benchmark.

<!-- BEGIN CODENIB_SUPPORT_MATRIX:methods -->
| Method | Type | Views read | Model at query time | Exposed through | Evidence |
| --- | --- | --- | --- | --- | --- |
| BM25 | A · CodeNib | `bm25` | none | Python, MCP | [Measured](../evaluation/agent_retrieval_bench.md#results) · ARB (L1) |
| Dense | A · CodeNib | `vector` | embedding model | Python, MCP | [Measured](../evaluation/agent_retrieval_bench.md#results) · ARB (L1) |
| Hybrid (RRF) | A · CodeNib | `bm25`, `vector` | embedding model | Python, MCP | [Measured](../evaluation/agent_retrieval_bench.md#results) · ARB (L1) |
| Graph expansion | A · CodeNib | `bm25` or `vector`, plus `symbol_graph` | embedding model for dense seeds; optional reranker | Python, MCP | [Measured](../evaluation/agent_retrieval_bench.md#results) · ARB (L1) |
| Retrieve → rerank | A · CodeNib | any first stage | a reranker | Python | [Measured](../evaluation/grep_jev.md#what-was-measured) · Base (L1) |
| Planner `auto` | A · CodeNib | whatever is available | none to plan; the chosen route may need embeddings or a reranker | Python, MCP | Runnable · 4 benchmarks |
| Model-planned grep → Jev | B · CodeNib | none; reads the checkout | planner LLM and Jev | CLI, MCP | [Measured](../evaluation/grep_jev.md) · Base (L1) |
| Agentless v1.5.0 | B · Reproduced | `symbol_graph` | chat LLM, three stages | Python | [Pinned](../agent_integrations.md#agentless) · upstream prompts |
| CodeNib agent | C · CodeNib | per enabled skill | chat LLM | Python | [Measured](../evaluation/contextbench-results.md#historical-post-run-24-task-results) · ContextBench (L2) |
| Claude Code or Codex over MCP | C · Client | `bm25`, `symbol_graph` | the client's own | MCP | Tested · test suite |
| Claude Agent SDK with CodeNib source tools | C · Client | none; CodeNib's confined `read`, `grep`, `glob` | the SDK's Claude model | research scripts only | [Measured](../evaluation/contextbench-results.md) · ContextBench Pass@1 (L3) |
| LocAgent | C · Reproduced | `bm25`, `symbol_graph` | chat LLM | Python | [Pinned](../agent_integrations.md#locagent-on-swe-bench-lite) · SWE-bench Lite |
| CoSIL | C · Reproduced | `symbol_graph` | chat LLM | Python | [Pinned](../agent_integrations.md#cosil) · upstream prompts and loop |
| OrcaLoca SearchAgent | C · Reproduced | `symbol_graph` | chat LLM | Python | [Pinned](../agent_integrations.md#paired-provider-compatibility) · SWE-bench Lite |
| RepoNavigator `jump` | C · Reproduced | `symbol_graph`, SCIP occurrences | the caller's | Python | [Pinned](../agent_integrations.md#reponavigator) · upstream tool contract |
| Claude / Codex without CodeNib | C · Control | none; reads files | Claude Agent SDK or Codex | Python | Runnable · 3 benchmarks |
<!-- END CODENIB_SUPPORT_MATRIX:methods -->

The Control wrappers give these agents no CodeNib tools. The Claude wrapper
also disables every MCP server; the Codex wrapper adds none, but Codex can
still load servers from its own user configuration.

The matrix lists methods, not experiment configurations. A **research arm**,
such as a post-search completion stage or a router between two search workers,
is listed with its outcome on its benchmark's results page and joins this
matrix only once it becomes a supported surface. Both tables are generated from
`scripts/support_matrix.yaml`; edit that file and run
`python scripts/support_matrix.py --write`.

## Views: what is precomputed

A view is one index built from one source snapshot. The
[view manifest](architecture.md) records which views exist and whether they
are current, and every method checks those capabilities before it runs.

| View | Built from | Good at | Build needs | `codenib index --preset` |
| --- | --- | --- | --- | --- |
| `bm25` | Tree-sitter chunks of the source | Exact identifiers and keywords | CPU only | `fast`, `semantic`, `graph`, `full` |
| `vector` | Embeddings of file-level (L0) and symbol-level (L2) chunks in FAISS | Behavior described in natural language | Embedding model; [CodeRankEmbed](https://huggingface.co/nomic-ai/CodeRankEmbed) by default, or an OpenAI-compatible endpoint | `semantic`, `full` |
| `symbol_graph` | SCIP, LSP, or clangd output stored as a typed `CodeGraph` | Definitions, references, callers, callees, containment | A [language provider](../language_capabilities.md) per language | `graph`, `full` |
| `regex_index` | Derived from `symbol_graph` when the runtime loads | Patterns over file and symbol names | Nothing beyond the graph | with `symbol_graph` |
| `zoekt` | Trigram shards over raw files | Fast substring and regex search | Zoekt binaries; Linux only | `full` |
| live checkout | The verified working tree | Exact source windows; grep without an index | Nothing | — |

Language coverage differs by view. Swift and Lua have chunking and retrieval but
no graph backend. Check the generated
[language capability matrix](../language_capabilities.md) before relying on
`symbol_graph` for a language.

## Operators

The views are combined by a small set of typed operators in `codenib/ops/`.
The operators do not choose a strategy; pipelines and the planner do.

| Operator | What it does |
| --- | --- |
| Retrieve and fuse | Normalize candidates from each view and merge them, either by weighted score or by reciprocal rank fusion (RRF, `k = 60`) |
| Filter | Keep or drop candidates by path, extension, span, symbol-versus-file, or test status |
| Expand | Add graph neighbors of the seed candidates: one hop, or a bounded k-hop BFS or personalized-PageRank region |
| Rerank | Rescore a fixed candidate set with embeddings, a cross-encoder, a listwise LLM, or Jev |

[RAG ops and planner](../rag_ops.md) documents each operator, the planner's
routes, and the embedding and rerank models with retained evidence.

## A. One-shot retrieval (RAG baselines)

**Principle.** A fixed pipeline computes one ranked list. Models may score
candidates, but they never decide what to search next. Cost and latency are
predictable, and the same input gives the same candidates.

| Variant | How it ranks | Example entry point |
| --- | --- | --- |
| Sparse | BM25 over chunks | `examples/bm25_retrieve_baseline.py` |
| Dense | Inner product between query and chunk embeddings | `examples/embedding_retrieve_baseline.py` |
| Hybrid | Dense and sparse lists merged by RRF or weighted score | `RetrieveRerankPipeline(retrieval_mode="hybrid")` |
| BM25 → dense cascade | BM25 top 128, rescored by embeddings to the top 50 | `HybridRetrievePipeline` |
| Sparse-seeded graph | Five BM25 seeds, then k-hop BFS or personalized PageRank over the symbol graph, then an optional rerank | `examples/graph_retrieve_baseline.py` |
| Dense-seeded graph | Dense top-K, then one hop of reference edges, then filter and rerank | `DenseGraphExpandRerankPipeline` |
| Retrieve → rerank | Any first stage, then `embedding`, `crossencoder`, `llm` (listwise), or `decisions` (Jev) | `examples/retrieve_rerank.py`, `examples/swerank_retrieve_rerank.py` |

**Choosing a route automatically.** `RetrievalPlanner` is a deterministic
classifier. It reads lexical, semantic, and structural signals in the query,
a budget (`fast`, `balanced`, `thorough`), and the available views, then picks
one of four routes: `fast_lexical`, `semantic`, `hybrid_fusion`, or
`structural_graph`. It makes no model call. The planner drives MCP
`search_context`, `RepositoryContextExplorer(policy="auto")`, and
`RetrieveRerankPipeline(retrieval_mode="auto")`.

Rerank behavior differs by surface:

- MCP `search_context` never reranks.
- `RepositoryContextExplorer` reranks with embeddings only, and only when a
  `vector` view is loaded.
- `RetrieveRerankPipeline` runs its configured `rerank_strategy` in the fixed
  `dense`, `sparse`, and `hybrid` modes. In `auto` mode the planner's choice
  replaces it. The planner picks only `llm` (on the `thorough` budget) or
  `embedding`, and never the cross-encoder. When the pipeline is configured
  with `decisions`, the planner's `llm` choice runs Jev.

For a fixed comparison arm instead of the planner, `RepositoryContextExplorer`
accepts `bm25`, `dense`, `hybrid`, `hybrid_rerank`, or `graph`.

## B. Fixed LLM workflows

**Principle.** An LLM is called a fixed number of times, in a fixed order.
Each stage receives context chosen by the workflow, and the model fills in the
answer for that stage. The number of model calls has a known upper bound.

| Workflow | Stages | Views | Entry point |
| --- | --- | --- | --- |
| [Agentless v1.5.0](../agent_integrations.md#agentless) | 1. Rank files from the filtered project tree. 2. Pick classes, functions, and variables from compressed file skeletons. 3. Choose edit lines from numbered source windows. | `symbol_graph` | `AgentlessAgent`, `examples/agentless_loc_agent.py` |
| [Model-planned grep → Jev](../guides/grep-jev.md) | 1. A planning model proposes one to six ripgrep searches from the issue and a directory overview. 2. Local `rg` runs them, and matches are mapped to their enclosing code blocks (at most 100). 3. Jev scores each candidate's relevance. | none | `codenib explore`, `codenib mcp --retrieval-route grep-jev`, `codenib init` |

The Agentless row reproduces localization only. Repair and patch validation
are out of scope. The older `codenib.model.AgentlessPipeline` uses
CodeNib-specific prompts. It remains for existing callers and is not the
pinned upstream policy.

Model-planned grep → Jev needs no index, embedding model, or GPU. It does send
the query and the selected source snippets to OpenRouter.

## C. Agentic tool loops

**Principle.** A model calls tools, reads the results, and decides what to do
next until it answers or runs out of turns. CodeNib supplies the tools; the
agent policy owns the control flow. Quality and cost depend on the model and
on the turn budget as much as on the tools.

| Agent | Tools CodeNib provides | Who owns the loop |
| --- | --- | --- |
| [CodeNib agent](../agent_skills.md) (`AgentRunner`, `query()`) | Four default tools (`read`, `grep`, `glob`, `bash`) plus any of 14 skills for retrieval, graph navigation, LSP lookups, reranking, and query rewriting | CodeNib |
| [Claude Code, Codex, or any MCP client](../mcp.md) | 12 MCP tools; `explore_context` bundles search, LSP route, dependencies, and verified source in one bounded call | The client |
| [LocAgent](../agent_integrations.md#locagent) | `search_code_snippets`, `get_entity_contents`, `explore_tree_structure` | Vendored upstream prompts and loop |
| [CoSIL](../agent_integrations.md#cosil) | Three class, function, and file lookup tools plus `exit` | Vendored upstream file reflection and function loop |
| [OrcaLoca](../agent_integrations.md#orcaloca) | Six search tools and the private hooks the upstream agent calls | Upstream `SearchAgent` |
| [RepoNavigator](../agent_integrations.md#reponavigator) | One `jump` tool that resolves definitions | The caller |

The external policies use the same manifest and views as every other method.
They do not build their own graph, BM25 index, or cache.

### How agents receive context

The agent decides what to read. CodeNib controls how much each call returns
and whether that content can be trusted.

| Mechanism | What happens | Status |
| --- | --- | --- |
| Pull through tools | The agent calls search, navigation, and read tools as needed | Default for `query()`, MCP clients, and the Web UI |
| Composite call | `explore_context` returns ranked anchors, the LSP route, dependency neighbors, and verified source windows under a 256 KiB result limit, and points back to ranges it already returned in the same connection | Recommended first call for MCP clients |
| Startup route context | `enable_lsp_route_context` adds LSP route anchors to the first prompt | Opt-in on `AgentRunner` and `CodeNibAgentOptions` |
| Pre-loaded hints | The evaluation harness can insert BM25, embedding, or graph results into the first prompt and vary how strongly the agent is told to trust them | Research arms only; not a product default |

Every route reports where its source came from. Source that CodeNib could not
verify against the checkout is marked `verified: false` and is never presented
as checkout source.

## Picking a method

| You have | Start with |
| --- | --- |
| Claude Code or Codex, no model budget for CodeNib | [`codenib codegraph init`](../codegraph.md): BM25 plus the symbol graph over MCP |
| An OpenRouter key and no index | [Model-planned grep → Jev](../guides/grep-jev.md) |
| Your own pipeline that needs ranked regions | `RepositoryContextExplorer` with `policy="auto"`, or a fixed policy for an ablation |
| A question about callers, impact, or dependencies | MCP `dependency_subgraph`, or the `graph` policy |
| A published localization agent to reproduce, or to run on CodeNib's index | [Reproduced agents](../agent_integrations.md) |
| Your own agent loop | [Agent skills](../agent_skills.md) and `AgentRunner` |
