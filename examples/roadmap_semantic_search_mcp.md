<!--
SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Roadmap: `search_semantic` MCP tool

This only covers the embedding/vector part. Graph tools and manifest schema changes can be tracked separately with hengjia if needed.

---

## Phase 1 — core tool (this RFC)

### `search_semantic`

**`codeminer/mcp/tools/search.py`**

```python
async def search_semantic(
    query: str,
    top_k: int = 10,
    level: str | None = None,
    score_threshold: float | None = None,
    transform: str | None = None,   # reserved for HyDE/expand, no-op for now
) -> list[dict]:
    ...
```

Wraps `CodeVectorStore.search_with_content()`, returns `list[NodeInfo.model_dump()]`. `transform` is in the signature now so future query-side augmentation doesn't need a breaking change. Underlying call is sync, needs `asyncio.to_thread()`.

```python
results = await asyncio.to_thread(
    ctx.vector.search_with_content,
    query=query,
    top_k=top_k,
    level=level,
    score_threshold=score_threshold,
)
return [r.model_dump() for r in results]
```

If `ctx.vector is None`, return early:

```python
if ctx.vector is None:
    return ToolError("Vector index not loaded. Re-run indexing with embedding enabled.")
```

---

### `ServerContext` — FAISS load

**`codeminer/mcp/context.py`** (overall structure from RFC, I own the `vector` load path)

**Key constraint:** `CodeVectorStore` requires embedding model info at `__init__` time, not during `.load()`.

- FAISS index files only store numerical vectors, not the model name
- `__init__()` creates `self.embedding` (OpenAI/HuggingFace wrapper), needed for both `.load()` and `.search()`
- During search, `self.embedding.embed_query(query)` converts the query to a vector before FAISS lookup

Embedding model metadata comes from `manifest.indexes["vector"].config`:

```python
@dataclass
class ServerContext:
    manifest: RepoManifest
    symbol_graph: CodeGraph | None = None
    bm25: BM25CodeIndexer | None = None
    vector: CodeVectorStore | None = None

    @classmethod
    def load(cls, manifest_path: str) -> "ServerContext":
        manifest = RepoManifest.load(manifest_path)
        ctx = cls(manifest=manifest)

        if "vector" in manifest.indexes:
            try:
                vector_entry = manifest.indexes["vector"]
                cfg = vector_entry.config

                # __init__ creates self.embedding wrapper
                ctx.vector = CodeVectorStore(
                    embedding_model=cfg["embedding_model"],
                    embedding_provider=cfg["embedding_provider"],
                    dimension=cfg.get("dimension"),
                    index_metric=cfg.get("index_metric", "ip"),
                    store_path=vector_entry.path
                )
                # .load() restores FAISS (requires self.embedding)
                ctx.vector.load()

                logger.info(f"Loaded: {cfg['embedding_model']}")
            except Exception as e:
                logger.warning("Failed to load vector index: %s", e)
                ctx.vector = None

        return ctx
```

Search flow: `query` → `embed_query()` → `query_vector` → `FAISS.search()` → `results`

Cold start: HuggingFace ~10-30s (model load in `__init__`), OpenAI negligible.

---

### `RepoManifest` — embedding config dependency

> Dependency on manifest schema, needs alignment with indexing stage

`RepoManifest.indexes["vector"].config` must include:

```python
{
    "embedding_model": str,      # e.g. "text-embedding-3-small"
    "embedding_provider": str,   # "openai" or "huggingface"
    "dimension": int,            # optional, auto-inferred if missing
    "index_metric": str          # "ip" or "l2"
}
```

Populated by indexing stage. `CodeVectorStore.save()` already writes this to `config_{model_suffix}.json`, just needs mirroring to manifest.

---

### Tests

**`tests/mcp/test_search_semantic.py`**

```
✓ Unit tests (mocked)
  - mock search_with_content() → list[NodeInfo]
  - assert output is list of dicts with expected keys
  - assert top_k and level forwarding
  - ctx.vector = None → error returned, no exception

✓ Integration test (real index from /mnt/data/codeminer)
  - Use Turquoise-T's pre-built indexes (8 embedding models available)
  - Test with jinaai/jina-code-embeddings-1.5b (1536-dim, IP metric)
  - Sample repo: astropy__astropy-12907 (6870 l2 documents)
  - Verify search quality and performance
```

---

## Phase 2 — robustness

### Model mismatch validation

After FAISS loads, check the model name matches what's in the manifest. The failure mode: someone rebuilds the index with a different model, server loads it silently, scores are wrong with no warning.

```python
loaded_model = ctx.vector.embedding_model  # public attribute, line 65 in vector_store.py
if loaded_model != cfg["embedding_model"]:
    raise RuntimeError(
        f"Embedding model mismatch: index built with '{cfg['embedding_model']}', "
        f"loaded store has '{loaded_model}'. Re-run indexing."
    )
```
