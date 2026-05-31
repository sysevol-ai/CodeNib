<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Agent Skills

CodeMiner's retrieval, reranking, and graph-expansion capabilities are packaged as
**skills** — self-contained units of metadata + execution logic that an LLM agent can
select and invoke. Skills live under `codeminer/agent/skills/`.

## How skills are defined

A skill is a package directory containing:

- `config.yaml` — metadata (`skill_id`, `skill_type`, inputs/outputs, `cost`,
  `index_requirements`, default params)
- `skill.md` — agent-readable description of when to use the skill
- `executor.py` — a `create_executor(context) -> Callable` factory returning the
  execution function

`SkillLoader.load_all(skills_dir, contexts)` scans the directory, parses each
`config.yaml`, reads `skill.md`, imports the executor, and registers a `SkillMetadata`
in the registry. Skills can also be declared in code with the `@skill` decorator
(`codeminer/agent/skills/registry.py`), which captures the same metadata and registers
the decorated function.

## Registry API

`SkillRegistry` is a singleton catalogue of `SkillMetadata`:

```python
from codeminer.agent.skills.registry import SkillRegistry

registry = SkillRegistry()
registry.list_skills()       # -> {skill_id: SkillMetadata}
registry.get("bm25_search")  # -> SkillMetadata | None
registry.has("llm_rerank")   # -> bool
```

## Exposing skills to an agent

`registry_to_tools(registry, allow=..., exclude=...)`
(`codeminer/agent/tool_schema.py`) converts registered skills into LLM tool schemas,
applying an optional `allow` set first, then an `exclude` set.

`AgentRunner` (`codeminer/agent/runner.py`) wraps this. It accepts `allow_skills` and
`exclude_skills`, and — when given a `manifest` — runs a `ResourceGuard` preflight that
automatically excludes skills whose required indexes are unavailable and surfaces
warnings in the system prompt:

```python
from codeminer.agent.runner import AgentRunner
from codeminer.agent.skills.registry import SkillRegistry

runner = AgentRunner(
    model="gpt-4o",
    registry=SkillRegistry(),
    allow_skills={"bm25_search", "graph_expand", "llm_rerank"},
)
result = runner.run("How does authentication work in this repo?")
```

## Available skills

| Skill | Type | Description |
|-------|------|-------------|
| `bm25_search` | retrieval | Fast keyword retrieval (TF-IDF/BM25); best for exact identifiers and tokens. |
| `embedding_search` | retrieval | Semantic vector retrieval that matches code by meaning, not literal tokens. |
| `regex_search` | retrieval | Pattern-based retrieval over the node index for structural queries. |
| `hybrid_search` | aggregate | Fuses multiple retrievers (e.g. BM25 + embedding) via weighted score normalization. |
| `graph_expand` | expand | Expands seed code blocks along the symbol graph to surface structurally related symbols. |
| `embedding_rerank` | rerank | Fast embedding-based reranking for large candidate sets. |
| `llm_rerank` | rerank | High-precision LLM-judged reranking to refine top results. |
| `query_transform` | transform | Expands/reformulates a query via keyword extraction to improve recall. |
| `code_to_query` | transform | Turns a code snippet into a search query for finding similar code. |

## Line-numbering at the agent boundary

Internally, every line number is **0-based** — BM25 docs, FAISS metadata,
symbol-graph anchors, and the tree-sitter `CodeChunk` all count from 0. The
*agent boundary* is **1-based outward**: line numbers shown to, and accepted
back from, the LLM are 1-based. This mirrors the `CodeLocation` convention at
the dataset/HuggingFace boundary (see `_chunk_to_code_block` in
`dataset/gt_locate.py`).

All conversion lives in one module, `codeminer/agent/boundary.py`, with one
site per direction:

- **Output** — `AgentRunner._serialize_result` runs every line-bearing result
  through `to_agent_repr` (`+1`) before it reaches the LLM, so a result's
  structured `start_line`/`end_line` agree with its rendered `content` gutter
  (which `wrap_code_snippet` already renders 1-based).
- **Input** — a skill input declared `is_line_number: true` in its
  `config.yaml` is passed through `from_agent_repr` (`-1`) by the runner before
  the executor sees it, so executors keep working in 0-based internals.

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
