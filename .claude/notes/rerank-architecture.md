# CodeMiner Rerank + Retrieval Architecture Notes

> Personal notes — Yash, 2026-06-27
> Updated after full cross-encoder wiring (session 2).

---

## How all the pieces connect (current state)

```
User types a question in the web UI (http://localhost:3001)
  │
  ▼
Next.js frontend  →  POST /api/ask  →  FastAPI (codeminer.web.app)
                                              │
                                              ▼
                                       repo_registry.py
                                       builds AgentRunner with skill contexts:
                                         contexts["retrieve"]      ← RetrieveContext
                                         contexts["rerank"]        ← RerankContext (dot-product)
                                         contexts["cross_encoder"] ← CrossEncoderContext  ← NEW
                                              │
                                              ▼
                                         AgentRunner  (agent/runner.py:130)
                                         LLM tool-call loop (max_turns=8)
                                              │
                       ┌──────────────────────┼──────────────────────────┐
                       │                      │                          │
                  bm25_search          embedding_search          crossencoder_rerank  ← NEW
                  (sparse BM25)        (FAISS dot-product)       (neural pair scorer)
                       │                      │                          │
                       └──────────────────────┘                          │
                                   │                                     │
                             candidates                                  │
                             (QueriedNode list)   ─────────────────────►─┘
                                                                         │
                                                            CrossEncoderContext.score()
                                                                         │
                                                             build_reranker(model_name)
                                                                    │
                                          ┌─────────────────────────┴──────────────────────┐
                                          │                                                 │
                               STCrossEncoderWrapper                           QwenRerankerWrapper
                               (mxbai-rerank-*, bge-reranker-*)                (Qwen3-Reranker-*)
                               BERT-style: [CLS] q [SEP] doc [SEP]             yes/no logit at last token
                               one score per pair, no generation                one score per pair, no generation
```

---

## The three rerank strategies — how each one works

### Strategy 1: `embedding` (default, fast)

```
candidates (QueriedNode list)
    │
    ▼
embed_query(query)       ← one forward pass, 110M–0.6B params
    +
embed_documents(docs)    ← N forward passes (or pre-indexed in FAISS)
    │
    ▼
np.dot(D, q)             ← matrix multiply, trivial
    │
    ▼
sorted by score → top_k
```

**In production (pre-indexed FAISS):** only `embed_query` runs at query time (~16ms).
**As a reranker (re-encode docs):** ~83ms for N=25 candidates.
**Weakness:** query and doc encoded separately → misses cross-attention between them.

---

### Strategy 2: `crossencoder` — NEW, the one we wired

Two sub-backends, selected automatically from the model name:

#### 2a. `STCrossEncoderWrapper` (BERT-style cross-encoder)

```python
# index/rerank/cross_encoder.py

def score(self, query: str, docs: List[str]) -> List[float]:
    pairs = [(query, doc) for doc in docs]      # N tuples
    scores = self._model.predict(               # sentence-transformers CrossEncoder
        pairs, batch_size=self.batch_size       # one batched forward pass per batch
    )
    return [float(s) for s in scores]
```

**Architecture:** `[CLS] query [SEP] document [SEP]` → BERT encoder → `[CLS]` hidden state → linear → float.
Every (query, doc) pair sees each other's tokens — cross-attention is the key advantage.
**Models:** `mixedbread-ai/mxbai-rerank-large-v2` (570M), `BAAI/bge-reranker-v2-m3`.
**Latency:** ~800ms for N=25 (H100) — too slow because 570M params hit batching ceiling.

#### 2b. `QwenRerankerWrapper` (generative reranker, yes/no logit)

```python
# index/rerank/cross_encoder.py

def score(self, query: str, docs: List[str]) -> List[float]:
    for doc in batch:
        prompt = format_prompt(query, doc)      # "Is this relevant? Answer yes or no."
        logits = model(tokenize(prompt)).logits # one forward pass (NO generation)
        score = softmax([logits[yes_id], logits[no_id]])[0]  # P(yes)
    return scores
```

**Architecture:** Qwen3 decoder LLM, but used without generation — only read the logit
for "yes" vs "no" at the final token. Functionally cross-encoder (pairs processed jointly),
architecturally generative.
**Models:** `Qwen/Qwen3-Reranker-0.6B` (recommended), `Qwen3-Reranker-4B`, `Qwen3-Reranker-8B`.
**Latency:** ~108ms for N=25 (H100) — **recommended choice**.

**Why Qwen3-Reranker-0.6B is faster than mxbai (570M despite similar param count):**
- Yes/no trick = 1 token decoded per pair → very short sequence, no KV cache growth
- mxbai processes longer padded sequences through the full encoder stack
- H100 saturates the mxbai kernel at ~800ms regardless of N (batching ceiling effect —
  N=25 and N=50 both take ~800ms because padding fills the batch the same way)

---

### Strategy 3: `llm` (listwise, slowest, most expressive)

```
candidates (all of them, windowed)
    │
    ▼
RerankAgent  (agent/rerank_agent.py)
    │
    ├─ "structured" mode:
    │    prompt: "Rank all N nodes. Return JSON: {ranked_indices, scores}"
    │    parsed: Pydantic RerankResult via with_structured_output()
    │
    └─ "rankgpt" mode (SweRankLLM, RankZephyr):
         prompt: "[1] code... [2] code... Rank: [3] > [1] > [2]"
         parsed: regex strip-digits → 1-based to 0-based
    │
    ▼
Sliding window: window_size=20, window_step=10 → ~10 LLM calls for N=100
Score per position: (num - position) / num, averaged across windows
```

**Latency:** 5,000–15,000ms (10 LLM chat completions).
**Advantage:** LLM understands natural language intent, handles multi-hop reasoning.
**Note:** `repo_registry.py` currently builds `RerankContext(embedding_store=vector_store)`
with **no `llm=` arg** → the `llm_rerank` skill falls back to dot-product even though
it's registered. Real LLM reranking needs an `llm=LiteLLMChat(...)` passed in.

---

## Latency numbers (measured on this H100, CodeMiner index)

| Strategy | Model | N=25 | N=50 | N=100 | VRAM delta |
|----------|-------|------|------|-------|------------|
| embedding (production FAISS path) | bge-base-en-v1.5 | **16ms** | **18ms** | ~20ms | 0 MB (pre-indexed) |
| embedding (re-encode docs, reranker mode) | bge-base-en-v1.5 | 83ms | 145ms | 259ms | ~150 MB |
| crossencoder Qwen3 | Qwen3-Reranker-0.6B | **108ms** | **361ms** | ~700ms | ~550 MB |
| crossencoder ST | mxbai-rerank-large-v2 | 804ms | 807ms | ~810ms | ~530 MB |
| llm listwise | qwen3:8b via Ollama | ~6,000ms | ~10,000ms | ~15,000ms | (Ollama server) |

**Takeaway:** Qwen3-Reranker-0.6B at N=25 → 108ms is the sweet spot.
50× faster than LLM listwise, more accurate than dot-product embedding.

---

## Code path — query to reranked results

### Web demo (Ask button)

```
1. POST /api/ask  (app.py)
       │
2. repo_registry.py → AgentRunner constructed with:
       contexts["retrieve"]      = RetrieveContext(bm25, vector_store)
       contexts["rerank"]        = RerankContext(embedding_store=vector_store)
       contexts["cross_encoder"] = CrossEncoderContext(        ← only if rerank_strategy="crossencoder"
                                       model_name=cfg.crossencoder_model,
                                       batch_size=cfg.crossencoder_batch_size)
       │
3. AgentRunner.run_turn(query)    (runner.py:130)
       LLM decides: call bm25_search(query, top_k=50)
       │
4. bm25_search/executor.py  →  BM25CodeIndexer.search()  →  List[QueriedNode]
       │
5. LLM decides: call crossencoder_rerank(query, candidates, top_k=10)
       │
6. crossencoder_rerank/executor.py
       ctx = bundle.cross_encoder           ← CrossEncoderContext
       scores = ctx.score(query, docs)      ← lazy-loads model on first call
       return sorted(zip(scores, candidates))[:top_k]
       │
7. LLM uses top-10 reranked chunks to write answer + citations
```

### Eval / benchmark script

```python
pipeline = RetrieveRerankPipeline(
    repo_path=...,
    index_path=...,
    rerank_strategy="crossencoder",
    crossencoder_model="Qwen/Qwen3-Reranker-0.6B",
    crossencoder_batch_size=8,
)
results = pipeline.query("how does BM25 search work", top_k=10)
# internally: _run_retrieval_stage() → _run_rerank() → _rerank_crossencoder()
```

---

## How to choose the model — all the knobs

### Option A: Config file (web demo) — `qa_config.local.yaml`

```yaml
# ── Rerank strategy ────────────────────────────────────────────────────────────
# "embedding"    → dot-product, fast, no extra VRAM (~16ms production path)
# "crossencoder" → neural pair scoring (~108ms @ N=25, +550MB VRAM)
rerank_strategy: embedding          # change to "crossencoder" to enable

crossencoder_model: Qwen/Qwen3-Reranker-0.6B    # fastest, recommended
# crossencoder_model: Qwen/Qwen3-Reranker-4B    # slower, higher quality
# crossencoder_model: mixedbread-ai/mxbai-rerank-large-v2  # ~800ms, not recommended
crossencoder_batch_size: 8
```

Then restart backend:
```bash
pkill -f codeminer.web.app && bash scripts/start_local_yash.sh
```

### Option B: CLI flag (eval script)

```bash
# Embedding rerank (default)
python examples/retrieve_rerank.py --rerank-strategy embedding

# Cross-encoder, recommended model
python examples/retrieve_rerank.py \
    --rerank-strategy crossencoder \
    --crossencoder-model Qwen/Qwen3-Reranker-0.6B \
    --crossencoder-batch-size 8

# Cross-encoder, heavier model (better quality, slower)
python examples/retrieve_rerank.py \
    --rerank-strategy crossencoder \
    --crossencoder-model Qwen/Qwen3-Reranker-4B \
    --crossencoder-batch-size 4

# LLM listwise (slow, needs Ollama)
python examples/retrieve_rerank.py --rerank-strategy llm
```

### Option C: Python API (scripts / notebooks)

```python
from codeminer.model import RetrieveRerankPipeline

# Fast embedding baseline
pipeline = RetrieveRerankPipeline(repo_path=..., index_path=..., rerank_strategy="embedding")

# Cross-encoder (auto-selects QwenRerankerWrapper because of model name)
pipeline = RetrieveRerankPipeline(
    repo_path=..., index_path=...,
    rerank_strategy="crossencoder",
    crossencoder_model="Qwen/Qwen3-Reranker-0.6B",  # → QwenRerankerWrapper
    crossencoder_batch_size=8,
)

# Cross-encoder (ST BERT-style)
pipeline = RetrieveRerankPipeline(
    repo_path=..., index_path=...,
    rerank_strategy="crossencoder",
    crossencoder_model="mixedbread-ai/mxbai-rerank-large-v2",  # → STCrossEncoderWrapper
    crossencoder_batch_size=16,
)
```

### Option D: Benchmark script

```bash
make bench-rerank                                  # default: embedding + embedding_cached + st_cross + qwen_cross
make bench-rerank BENCH_BACKENDS="qwen_cross"      # just Qwen3-Reranker-0.6B
make bench-rerank BENCH_N="10 25 50" BENCH_REPS=5  # custom candidate sizes + reps
```

---

## Model dispatch — how `build_reranker()` picks the backend

```python
# index/rerank/cross_encoder.py

def build_reranker(model_name: str, backend: str | None = None, batch_size: int = 16):
    if backend == "qwen" or (backend is None and "Qwen3-Reranker" in model_name):
        return QwenRerankerWrapper(model_name, batch_size=batch_size)
    else:
        return STCrossEncoderWrapper(model_name, batch_size=batch_size)
```

| Model name contains | → Backend | Notes |
|---------------------|-----------|-------|
| `Qwen3-Reranker` | `QwenRerankerWrapper` | yes/no logit trick |
| anything else | `STCrossEncoderWrapper` | sentence-transformers CrossEncoder |
| — | either | force with `backend="st"` or `backend="qwen"` |

---

## Files changed / created this session

| File | Status | What |
|------|--------|------|
| `codeminer/ops/rerank.py` | modified | Added `CrossEncoderContext` dataclass — lazy-loads wrapper, exposes `.score()` / `.close()` |
| `codeminer/model/retrieve_rerank_pipeline.py` | modified | `rerank_strategy="crossencoder"` branch, `_rerank_crossencoder()` method, `crossencoder_model` param |
| `codeminer/agent/skills/context.py` | modified | `cross_encoder: Optional[CrossEncoderContext]` field in `ComposerContexts` |
| `codeminer/web/config.py` | modified | `rerank_strategy`, `crossencoder_model`, `crossencoder_batch_size` fields in `QAConfig` |
| `codeminer/web/repo_registry.py` | modified (skip-worktree) | Reads config, builds `CrossEncoderContext` when `rerank_strategy="crossencoder"` |
| `codeminer/agent/skills/crossencoder_rerank/` | **new** | Full skill package — `config.yaml`, `executor.py`, `skill.md` |
| `examples/retrieve_rerank.py` | modified | `--rerank-strategy crossencoder`, `--crossencoder-model`, `--crossencoder-batch-size` CLI flags |
| `scripts/bench_rerank_latency.py` | **new** | Latency benchmark — `embedding`, `embedding_cached`, `st_cross`, `qwen_cross`, `llm_listwise` |
| `Makefile` | modified | `make bench-rerank` target |
| `qa_config.local.yaml` | modified | `rerank_strategy`, `crossencoder_model`, `crossencoder_batch_size` keys |

---

## Models on `/mnt` ready to use

| Role | Model | Backend | Latency N=25 | Notes |
|------|-------|---------|--------------|-------|
| Embedding (current) | `BAAI/bge-base-en-v1.5` | bi-encoder | 16ms (FAISS) | What `.codeminer_cache/` is built with |
| Embedding (upgrade) | `Qwen/Qwen3-Embedding-0.6B` | bi-encoder | ~20ms | Better code retrieval, needs reindex |
| **Cross-encoder (recommended)** | `Qwen/Qwen3-Reranker-0.6B` | QwenRerankerWrapper | **108ms** | Best latency/quality balance |
| Cross-encoder (heavier) | `Qwen/Qwen3-Reranker-4B` | QwenRerankerWrapper | ~400ms | Higher quality, more VRAM |
| Cross-encoder (ST) | `mixedbread-ai/mxbai-rerank-large-v2` | STCrossEncoderWrapper | 804ms | Not recommended — batching ceiling |
| SWE listwise | `Salesforce/SweRankLLM-Small` | RerankAgent (rankgpt) | ~5000ms | Use `listwise_format="rankgpt"` |

---

## What changed vs what was there before

| Component | Before this session | After |
|-----------|--------------------|----|
| `STCrossEncoderWrapper` | ✓ exists, **zero callers** | ✓ callable via `rerank_strategy="crossencoder"` |
| `QwenRerankerWrapper` | ✓ exists, **zero callers** | ✓ callable via `rerank_strategy="crossencoder"` |
| `rerank_strategy="crossencoder"` in pipeline | ✗ | ✓ |
| `crossencoder_rerank` skill | ✗ | ✓ |
| `CrossEncoderContext` | ✗ | ✓ in `ops/rerank.py` |
| Config file toggle | ✗ (needed code edit) | ✓ `qa_config.local.yaml` |
| Latency benchmark | ✗ | ✓ `make bench-rerank` |

---

## Q&A

### Q: Is Qwen3-Reranker-0.6B a cross-encoder?

Functionally **yes** — processes (query, doc) pairs jointly, cannot pre-encode documents.
Architecturally **no** — it is a Qwen3 generative decoder (0.6B), not a BERT encoder.
The yes/no logit trick makes it behave like a cross-encoder: feed the pair as a prompt,
read `softmax([logit_yes, logit_no])[0]` as the relevance score. No tokens generated.

### Q: Why does mxbai show flat latency at N=25 and N=50?

H100 batching ceiling. With `batch_size=16`:
- N=25 → 2 batches (2nd is half-empty but padded to 16)
- N=50 → 4 batches

Each batch dispatches the same CUDA kernel regardless of actual content.
The model (570M params) saturates the kernel at ~400ms per two batches regardless
of how full they are. Both N=25 and N=50 ≈ 800ms because the dominating cost
is kernel setup + model activation, not data volume.

### Q: The wiki documentation builder — does cross-encoder help it?

**Not yet.** `wiki/agent_wiki.py:_rerank_for_page()` uses pure string matching:

```python
score += 5.0   # file path matches outline hint
score += 0.5   # keyword found in content (capped 3.0)
score += 2.0   # exact phrase match
```

Replacing this with `CrossEncoderContext.score()` would give semantically better
chunk selection for each wiki page → better grounded prose. That's a future improvement.

### Q: Why does `llm_rerank` skill use dot-product even though it's named "llm"?

`repo_registry.py` builds `RerankContext(embedding_store=vector_store)` with **no `llm=`
argument**. `RerankContext.ensure_agent()` then has no LLM to call and falls back to
dot-product scoring. To enable true LLM listwise reranking, pass:

```python
RerankContext(
    embedding_store=vector_store,
    llm=LiteLLMChat(model="openai/qwen3:8b", api_base="http://localhost:11434/v1"),
)
```

### Q: How does the `embedding_cached` benchmark differ from the regular `embedding` one?

`embedding` re-encodes all N documents every call (simulates the reranker path).
`embedding_cached` pre-embeds docs once before the timed loop, then only times
`embed_query(q)` + `np.dot(D, q)` — this simulates the **production FAISS path** where
document vectors are stored and only the query is encoded at serve time.
That's why `embedding_cached` is constant at ~16-18ms regardless of N.
