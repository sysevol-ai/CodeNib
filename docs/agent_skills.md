<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Agent Skills

CodeNib's retrieval, reranking, and graph-expansion capabilities are packaged as
**skills** — self-contained units of metadata + execution logic that an LLM agent can
select and invoke. Skills live under `codenib/agent/skills/`.

## How skills are defined

A skill is a package directory containing:

- `config.yaml` — metadata (`skill_id`, `skill_type`, inputs/outputs, `cost`,
  `index_requirements`, default params)
- `skill.md` — agent-readable description of when to use the skill
- `executor.py` — a `create_executor(context) -> Callable` factory returning the
  execution function. `context` is the typed op-context dataclass for the skill's
  type (`RetrieveContext` / `RerankContext` / `TransformContext` / `ExpandContext`);
  `custom` composer skills (`codenib_context`, `bm25_search`,
  `crossencoder_rerank`) instead receive a typed `ComposerContexts` bundle
  (`codenib/agent/skills/context.py`).

`SkillLoader.load_all(skills_dir, contexts)` scans the directory, parses each
`config.yaml`, reads `skill.md`, imports the executor, and registers a `SkillMetadata`
in the registry. Skills can also be declared in code with the `@skill` decorator
(`codenib/agent/skills/registry.py`), which captures the same metadata and registers
the decorated function.

## Registry API

`SkillRegistry` is a singleton catalogue of `SkillMetadata`:

```python
from codenib.agent.skills.registry import SkillRegistry

registry = SkillRegistry()
registry.list_skills()       # -> {skill_id: SkillMetadata}
registry.get("bm25_search")  # -> SkillMetadata | None
registry.has("llm_rerank")   # -> bool
```

## Exposing skills to an agent

`registry_to_tools(registry, allow=..., exclude=...)`
(`codenib/agent/tool_schema.py`) converts registered skills into LLM tool schemas,
applying an optional `allow` set first, then an `exclude` set.

`AgentRunner` (`codenib/agent/runner.py`) wraps this. It accepts `allow_skills` and
`exclude_skills`, and — when given a `manifest` — runs a `ResourceGuard` preflight that
automatically excludes skills whose required indexes are unavailable and surfaces
warnings in the system prompt:

```python
from codenib.agent.runner import AgentRunner
from codenib.agent.skills.registry import SkillRegistry

runner = AgentRunner(
    model="gpt-4o",
    registry=SkillRegistry(),
    allow_skills={"bm25_search", "find_callers", "llm_rerank"},
)
result = runner.run("How does authentication work in this repo?")
```

## Default tools vs skills

The always-on filesystem primitives — **`read` / `grep` / `glob` / `bash`** — are a
separate type from skills. They are `ToolSpec` objects (`codenib/agent/tools/spec.py`)
in a `ToolRegistry` the runner holds alongside the `SkillRegistry`, registered via
`ensure_default_tools_registered`. They carry no `index_requirements`, no `operator`,
and no retrieval `skill_type`, and are **never** narrowed by `allow_skills` /
`exclude_skills` (use the runner's `default_tool_ids` to restrict which defaults are
exposed, or `include_default_tools=False` to withhold them).

## Available skills

| Skill | Type | Description |
|-------|------|-------------|
| `bm25_search` | custom | Fast keyword retrieval (TF-IDF/BM25) over symbol chunks; `names_only=true` returns compact NAME tags (no bodies), with `unified_name` relabeling when a graph is present. |
| `embedding_search` | retrieval | Semantic vector retrieval that matches code by meaning, not literal tokens. |
| `hybrid_search` | aggregate | Fuses multiple retrievers (e.g. BM25 + embedding) via weighted score normalization. |
| `codenib_context` | custom | One-call GraphRAG composer: search seeds + deterministic call-graph expansion into a compact set (graph step toggle: `CODENIB_COMPOSER_NO_GRAPH=1`). |
| `repository_search` | custom | Repository-wide source retrieval with lexical search and optional semantic reciprocal-rank fusion; excludes supporting material by default. |
| `find_callers` | expand | Incoming call-graph edges of a symbol (who calls X). |
| `find_callees` | expand | Outgoing call-graph edges of a symbol (what X calls). |
| `trace` | expand | Shortest call path between two symbols. |
| `lsp_route` | expand | Symbol-name semantic navigation over the static symbol graph: ranks endpoint/bridge/provider/type route anchors for a query, optionally with one-hop neighbors. |
| `lsp_definition` | expand | LSP-compatible go-to-definition for the identifier at an exact source position (repo-relative `file_path`, 1-based `line`, 0-based `character`), served by the runtime's selected semantic provider — static SCIP occurrences by default, live LSP when injected. |
| `lsp_references` | expand | LSP-compatible find-references at an exact source position, with optional `include_declaration`; same position contract and provider selection as `lsp_definition`. |
| `llm_rerank` | rerank | High-precision LLM-judged reranking to refine top results. |
| `crossencoder_rerank` | custom | Pairwise reranking that scores each query/candidate pair with a cross-encoder; requires a `CrossEncoderContext` in the runner's skill contexts. |
| `code_to_query` | transform | Packs retrieved code nodes into a reusable follow-up search query. |

## Line-numbering at the agent boundary

Retrieval and graph representations used by skills are **0-based** — BM25
documents, FAISS metadata, symbol-graph anchors, and tree-sitter `CodeChunk`
objects count from 0. The *agent boundary* is **1-based outward**: line numbers
shown to, and accepted back from, the LLM are 1-based. Dataset
`CodeLocation` values are also 1-based; `_chunk_to_code_block` in
`dataset/gt_locate.py` performs that separate output conversion.

All conversion lives in one module, `codenib/agent/boundary.py`, with one
site per direction:

- **Output** — `AgentRunner._serialize_result` runs every line-bearing result
  through `to_agent_repr` (`+1`) before it reaches the LLM, so a result's
  structured `start_line`/`end_line` agree with its rendered `content` gutter
  (which `wrap_code_snippet` already renders 1-based).
- **Input** — a skill input declared `is_line_number: true` in its
  `config.yaml` is passed through `from_agent_repr` (`-1`) by the runner before
  the executor sees it, so executors keep working in 0-based internals.

The native LSP skills follow the same contract: `lsp_definition` /
`lsp_references` declare their `line` input `is_line_number: true` (1-based
in, 0-based internally), while `character` stays **0-based** end to end,
matching the native LSP wire convention.

### Authoring a skill that accepts line numbers

Mark the input in `config.yaml`; the runner handles the conversion:

```yaml
inputs:
  - name: ranges
    type: List[List[int]]
    is_line_number: true
    description: 1-based [start, end] line ranges (converted to 0-based for you)
```

Do **not** add ad-hoc `+1`/`-1` in executors or rendering paths — route through
the boundary helpers so the offset lives in exactly one place.
