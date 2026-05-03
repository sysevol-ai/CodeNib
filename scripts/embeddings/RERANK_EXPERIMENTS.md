# Rerank experiments runbook

How to run, name, and find output for the codeminer-base retrieval / rerank
experiments. For prompt-registry details (per-model query/doc prefixes,
when an index rebuild is needed) see
[EMBEDDING_PROMPTS.md](EMBEDDING_PROMPTS.md).

## Prerequisites

1. **Editable install pointed at this working tree** (CI uses a separate
   install in `/home/zhongming/SysEvol/actions-runner/`; if `pip show
   codeminer` doesn't show the right path, run `pip install -e .` from
   here).
2. **Pre-built FAISS indices for the small model(s) under
   `/mnt/data/codeminer/<instance>/{l0,l2}/index_<model>.faiss`.** Build
   via `build_codeminer_base_embeddings.sh`.
3. **Cross-encoder runs need no offline index.** The reranker scores top-K
   candidates online from the small model's L2 retrieval.

## What lives where

| Script | What it runs |
|---|---|
| `eval_codeminer_base_embeddings.sh` | Single-model baselines (5 models, no rerank). Hot-swap pattern, ~3 min/model. |
| `eval_codeminer_base_rerank_matrix.sh` | (small × large) rerank cascade matrix. Strategy-pluggable: `embedding` or `cross-encoder`. |
| `examples/retrieve_rerank.py` | LLM listwise rerank path (Claude, Gemini, SweRankLLM, etc.). Different harness — runs one (small, large) pair at a time. |

## Strategies

- **`embedding`** (default for the matrix script). Re-encode (query, doc)
  with the large *embedding* model, score by vector similarity. Mirrors
  the dual-encoder rerank pattern. **Demonstrated to regress on
  codeminer-base** vs single-model SweRank-Large at every k≥5.
- **`cross-encoder`**. Pairwise reranker that jointly attends to (query,
  doc). Auto-dispatched between two backends:
  - **ST** (sentence-transformers `CrossEncoder`) — for `mxbai-rerank-*`,
    `BAAI/bge-reranker-*`, `jinaai/jina-reranker-*`.
  - **Qwen** — for `Qwen/Qwen3-Reranker-*` (decoder-only, yes/no token
    logit trick with an instruction prompt).
- **LLM listwise** — separate script `examples/retrieve_rerank.py
  --rerank-strategy llm`, routes through litellm. Use for closed-source
  rerankers (Claude, Gemini) and Salesforce/SweRankLLM.

## Common invocations

### Single-model baselines (all 5 models)

```bash
bash scripts/embeddings/eval_codeminer_base_embeddings.sh
```

Outputs:
- results: `/mnt/data/codeminer/eval_results/eval_codeminer_base_test_<model>.json`
- profile: `/mnt/data/codeminer/profile_log/query_runtime/codeminer_base__embedding_baseline__<model>__<tag>.json`

### Embedding-rerank matrix (default)

```bash
bash scripts/embeddings/eval_codeminer_base_rerank_matrix.sh
```

Defaults: 2 smalls × 3 larges = 6 pairs, RERANK_TOP_K=30, ~1.5 h on H100.

### Cross-encoder rerank — single pair (Qwen3-Reranker)

```bash
SMALL_MODELS='Salesforce/SweRankEmbed-Small:768' \
LARGE_MODELS='Qwen/Qwen3-Reranker-0.6B:0' \
RERANK_STRATEGY='cross-encoder' \
PROFILE_TAG='ce_qwen3_0p6b' \
  bash scripts/embeddings/eval_codeminer_base_rerank_matrix.sh
```

`:0` is a placeholder for the dim — cross-encoders don't use it. Use
`PROFILE_TAG` to keep multiple runs in distinct output files.

### Cross-encoder rerank — mxbai / bge / jina

```bash
SMALL_MODELS='Salesforce/SweRankEmbed-Small:768' \
LARGE_MODELS='mixedbread-ai/mxbai-rerank-large-v2:0' \
RERANK_STRATEGY='cross-encoder' \
PROFILE_TAG='ce_mxbai_v2' \
  bash scripts/embeddings/eval_codeminer_base_rerank_matrix.sh
```

The backend auto-detects from the model name; force it with
`CROSS_ENCODER_BACKEND=st|qwen` if the heuristic guesses wrong.

### Window-size sweep (recall ceiling vs online cost)

```bash
for K in 50 70 100; do
  RERANK_TOP_K=$K \
  SMALL_MODELS='Salesforce/SweRankEmbed-Small:768' \
  LARGE_MODELS='Qwen/Qwen3-Reranker-0.6B:0' \
  RERANK_STRATEGY='cross-encoder' \
  PROFILE_TAG='ce_qwen3_0p6b' \
    bash scripts/embeddings/eval_codeminer_base_rerank_matrix.sh
done
```

The shell wrapper auto-appends `_k<K>` to `PROFILE_TAG` when
`RERANK_TOP_K` differs from the default 30, so the resulting result and
profile files end up distinct (e.g. `ce_qwen3_0p6b_k50.json`,
`ce_qwen3_0p6b_k100.json`) without you having to thread `${K}` into the
tag yourself. Existing K=30 runs keep their previous filenames untouched.

### Smoke test (one instance, one pair)

```bash
FILTER='^(redis__redis-10095)$' \
SMALL_MODELS='Salesforce/SweRankEmbed-Small:768' \
LARGE_MODELS='Qwen/Qwen3-Reranker-0.6B:0' \
RERANK_STRATEGY='cross-encoder' \
PROFILE_TAG='ce_smoke' \
  bash scripts/embeddings/eval_codeminer_base_rerank_matrix.sh
```

Verify the wrapper log line shows the right registry/backend choice
(`Loaded Qwen reranker: Qwen/Qwen3-Reranker-0.6B (...)` or `Loaded ST
CrossEncoder: ...`) before launching a long sweep.

### LLM listwise (Claude / Gemini / SweRankLLM)

Different harness — use `examples/retrieve_rerank.py`:

```bash
python examples/retrieve_rerank.py \
  --dataset codeminer_base --split test \
  --embedding-model Salesforce/SweRankEmbed-Small --embedding-dimension 768 \
  --rerank-strategy llm \
  --rerank-model anthropic/claude-sonnet-4-6 \
  --rerank-window-size 10 --rerank-top-k 30 \
  --index-cache-dir /mnt/data/codeminer
```

Provider strings follow litellm conventions:
- Anthropic: `anthropic/claude-sonnet-4-6`
- OpenAI: `openai/gpt-4o-mini`
- Google: `gemini/gemini-3-flash-preview` (verify exact ID via litellm docs)
- Salesforce SweRankLLM: needs a custom listwise wrapper (TODO).

## Env knobs (matrix sweep)

| Variable | Default | Notes |
|---|---|---|
| `SMALL_MODELS` | (in script) | Space-separated `MODEL:DIM`. |
| `LARGE_MODELS` | (in script) | `MODEL:DIM`; pass `:0` for cross-encoders. |
| `RERANK_STRATEGY` | `embedding` | `embedding` or `cross-encoder`. |
| `RERANK_TOP_K` | `30` | Number of candidates fed to the reranker. |
| `RERANK_BATCH_SIZE` | `8` | Embedding rerank doc-encoding batch. |
| `CROSS_ENCODER_BATCH_SIZE` | `8` | Cross-encoder pair-scoring batch. |
| `CROSS_ENCODER_BACKEND` | `auto` | `auto`, `st`, or `qwen`. |
| `CROSS_ENCODER_INSTRUCTION` | (code task) | Qwen3-Reranker prompt instruction. |
| `MAX_SEQ_LENGTH` | `8192` | Token cap (large model). |
| `SMALL_BATCH_SIZE` | `32` | Used only on rebuild-fallback paths. |
| `PROFILE_TAG` | (none) | Suffix for result/profile filenames. |
| `FILTER` | `.*` | Regex to subset instances. |
| `INDEX_CACHE_DIR` | `/mnt/data/codeminer` | Where small FAISS indices live. |
| `RESULTS_DIR` | `${INDEX_CACHE_DIR}/eval_results` | Per-pair result JSON. |
| `PROFILE_DIR` | `${INDEX_CACHE_DIR}/profile_log/query_runtime` | Per-pair profile JSON. |

## Output filename conventions

### `eval_results/`
- `eval_codeminer_base_<split>_<model>.json` — single-model baseline.
- `rerank_embedding_codeminer_base_<split>_<small>__x__<large>[__<tag>].json` — embedding cascade.
- `rerank_cross_encoder_codeminer_base_<split>_<small>__x__<large>[__<tag>].json` — cross-encoder cascade.

### `profile_log/query_runtime/`
- `codeminer_base__embedding_baseline__<model>__<tag>.json`
- `codeminer_base__dense_retrieve_plus_<strategy>_rerank__<small>__<small>__x__<large>[__<tag>].json`

The strategy is in the filename so embedding-rerank and cross-encoder
results never collide. Tag yourself per run via `PROFILE_TAG=` for
window-size sweeps or A/B comparisons.

## Adding a new reranker

### Standard CrossEncoder model (mxbai / bge / jina families)

Pass it in `LARGE_MODELS` — the factory auto-routes through ST. No code
change.

### Qwen3-Reranker variant (different size)

Same — auto-detected by `Qwen3-Reranker` substring. For different
instruction templates, set `CROSS_ENCODER_INSTRUCTION="..."`.

### A model with non-standard API

1. Add a wrapper class in
   [`codeminer/index/rerank/cross_encoder.py`](../../codeminer/index/rerank/cross_encoder.py)
   exposing `score(query, docs) -> List[float]` and `close()`.
2. Extend `build_reranker(...)` to dispatch to it.
3. Update this guide.

### Closed-source LLM (Claude, Gemini, ...)

Use `examples/retrieve_rerank.py --rerank-strategy llm` (litellm-backed).
The shell wrapper above does not cover this path.

## Debug checklist when results look wrong

1. **`pip show codeminer | grep -i editable` → check it points here**, not
   the actions-runner workspace. If wrong, `pip install -e .` from this
   directory.
2. **Wrapper init log**:
   - For embedding rerank, you should see two lines per pair —
     `Embedding wrapper for <small>: query_prompt_name='query' ...` and
     `Embedding wrapper for <large>: query_prompt_name='...' ...`.
   - For cross-encoder, you should see either `Loaded ST CrossEncoder:
     <model>` or `Loaded Qwen reranker: <model> (..., instruction=...)`.
   If the prompt fields are `None` for a model in the registry, something
   in the kwargs flow is overriding them.
3. **Result file was actually rewritten**: compare the file's mtime to
   when you started the run. Old files have model-specific filenames;
   they don't get overwritten until the same `(small, large, tag)`
   combination is rerun.
4. **`--force-rebuild` actually forced a rebuild**: check for
   `vector_store_add_documents_<level>` (or `embedding_encode_<level>`)
   sections in the build profile. If only `build_vector_store` and a
   ~0.04s `add_documents_l0` show up, the build hit the load-from-cache
   path silently — see fix in `build_embeddings.py` (already merged).
