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
