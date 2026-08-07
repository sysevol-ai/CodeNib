<!--
SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors

SPDX-License-Identifier: Apache-2.0
-->

# Embedding query/document prompt cheat sheet

Sentence-transformer code retrievers are trained with **asymmetric**
encoding — queries take a task-instruction prefix, documents either take
nothing or a different prefix. Calling `model.encode(text)` with no prompt
produces off-distribution vectors and noticeably hurts retrieval quality
(and especially cross-encoder/embedding rerank). This file documents the
prompts we use and how to verify them.

The runtime registry lives in
[`codenib/index/embedding/prompt_registry.py`](../../codenib/index/embedding/prompt_registry.py)
and is consumed automatically by `_HuggingFaceEmbeddingWrapper` —
build/eval/rerank scripts do not need to pass prompts explicitly.

## Per-model prompts

| Model | Query prompt | Document prompt | Doc rebuild needed when fixing? |
|---|---|---|---|
| `Salesforce/SweRankEmbed-Small` | `prompt_name="query"` → `Represent this query for searching relevant code: ` | none | No |
| `Salesforce/SweRankEmbed-Large` | `prompt_name="query"` → `Instruct: Given a github issue, identify the code that needs to be changed to fix the issue.\nQuery: ` | none | No |
| `fishmingyu/SweRankEmbed-Large` | same as Salesforce/SweRankEmbed-Large | none | No |
| `Qwen/Qwen3-Embedding-0.6B` | **overridden** to `Instruct: Given a github issue, identify the code that needs to be changed to fix the issue.\nQuery: ` (stock prompt is for *web search*, not code) | `""` (empty) | No |
| `Qwen/Qwen3-Embedding-4B` | same override as 0.6B | `""` | No |
| `nomic-ai/CodeRankEmbed` | `prompt_name="query"` → `Represent this query for searching relevant code: ` | none | No |
| `jinaai/jina-code-embeddings-1.5b` | `prompt_name="nl2code_query"` → `Find the most relevant code snippet given the following query:\n` | `prompt_name="nl2code_document"` → `Candidate code snippet:\n` | **Yes** ⚠️ |
| `jinaai/jina-code-embeddings-0.5b` | same as 1.5b | same as 1.5b | **Yes** ⚠️ |

**Why jina needs a doc rebuild:** unlike the other models, jina has a
non-empty document-side prompt. Existing FAISS indices were built with
plain `model.encode(text)` (no prefix), so the stored vectors are
off-distribution from how jina expects to score documents. To use jina
with the registry entry, run `build_codenib_base_embeddings.sh` again
for jina with `--force-rebuild`. The other 4 models have either no
doc-side prompt or an empty doc-side prompt — their existing indices stay
valid; only `embed_query` changes.

## Why we override Qwen3's stock prompt

Qwen3-Embedding's `config_sentence_transformers.json` ships
`"query": "Instruct: Given a web search query, retrieve relevant
passages that answer the query\nQuery:"`. That instruction is for
generic web search, not code retrieval. The Qwen3 model card explicitly
recommends customizing the `Instruct: <task>` line per task. We use the
SweRankEmbed-Large wording so all 5 models converge on a comparable
code-retrieval instruction:

```
Instruct: Given a github issue, identify the code that needs to be changed to fix the issue.
Query: ...
```

## Verifying prompts on a new model

Source of truth, in order of reliability:

1. **`config_sentence_transformers.json` in the model snapshot.** This is
   the file `SentenceTransformer` reads at load time:

   ```bash
   d=$(ls -d "${HF_HOME:-$HOME/.cache/huggingface}"/hub/models--<NAMESPACE>--<NAME>/snapshots/*/ | head -1)
   cat "${d}/config_sentence_transformers.json"
   ```

   Look for the `prompts` dict. If a `"query"` key exists, that's the
   conventional default. Multi-task models (jina) instead expose
   task-specific keys like `nl2code_query` / `nl2code_document` — pick
   the pair whose semantics match natural-language → code retrieval.

2. **Model card README** — usage examples typically show
   `model.encode(queries, prompt_name="...")` with the right key.

3. **Runtime introspection:**
   ```python
   from sentence_transformers import SentenceTransformer
   m = SentenceTransformer(
       "<repo-id>",
       revision="<full-commit-sha>",
       trust_remote_code=True,
   )
   print(m.prompts)
   ```

If the model has no registered prompts at all, fall back to the model
card's "task description" line and pass it as a raw `query_prompt`
override (and `document_prompt=""` if doc-side is meant to be empty).

## How the registry is plumbed

The wrapper picks up registry defaults automatically:

- `_HuggingFaceEmbeddingWrapper.__init__` calls
  `prompt_registry.resolve_prompts(model_name)` and uses the result
  as defaults for any `query_prompt(_name)` / `document_prompt(_name)`
  kwargs the caller didn't pass explicitly.
- `embed_query` / `embed_documents` apply the resolved prompts via
  `SentenceTransformer.encode(prompt=...)` or `encode(prompt_name=...)`
  on each call.
- A raw `query_prompt="..."` (or `document_prompt="..."`) passed via
  `embedding_kwargs` overrides the registry. An explicit empty string
  disables prefixing for that side.

So adding a new model is one entry in `_REGISTRY`; nothing else changes.

## When to rebuild indices

| Change | Rebuild? |
|---|---|
| Add a query-only prompt for a model whose existing doc-side prompt is empty/none | **No** — only `embed_query` changes. |
| Add or change a non-empty doc-side prompt for a model whose existing index was built with empty/no doc-side prompt | **Yes** — the stored vectors are off-distribution. |
| Change the query-side prompt | **No** — index is doc-side only. |
| Switch the model | **Yes** (separate index per model). |

In our current state, only **jina** falls into the rebuild bucket. The
other four models are usable immediately after the wrapper change.
