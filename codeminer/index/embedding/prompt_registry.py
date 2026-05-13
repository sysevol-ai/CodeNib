# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Per-model prompt registry for sentence-transformer embedders.

These embedders are trained with **asymmetric** query/document encoding —
queries take a task-instruction prefix, documents either take none or take a
different prefix. Calling ``model.encode(text)`` with no prompt produces
off-distribution vectors and noticeably hurts retrieval (and especially
rerank) quality.

Authoritative source for each model is its
``config_sentence_transformers.json`` ``prompts`` dict (see
``scripts/embeddings/EMBEDDING_PROMPTS.md`` for inspection commands and
prompt provenance).
"""

from typing import Dict, Optional

# Code-task instruction we use for general code-retrieval models that ship
# with a *generic* default prompt (e.g. Qwen3-Embedding's stock prompt is for
# web search, not code retrieval). Same wording as Salesforce's
# SweRankEmbed-Large card so all models converge on a comparable instruction.
_CODE_RETRIEVAL_INSTRUCT = (
    "Instruct: Given a github issue, identify the code that needs to be "
    "changed to fix the issue.\nQuery: "
)


# Each entry is one of:
#   {"query_prompt_name": "<key in model.prompts>"}
#   {"query_prompt": "<raw string>"}
#   {"document_prompt_name": "<key>"} or {"document_prompt": "<raw>"}
#
# ``*_prompt`` (raw string) takes precedence over ``*_prompt_name`` if both are
# set. An explicit empty string disables prefixing for that side. ``None``
# means "let SentenceTransformer.encode default to no prompt".
_REGISTRY: Dict[str, Dict[str, Optional[str]]] = {
    # --- SweRank family: query prompt is registered as "query"; no doc prompt.
    "Salesforce/SweRankEmbed-Small": {
        "query_prompt_name": "query",
    },
    "Salesforce/SweRankEmbed-Large": {
        "query_prompt_name": "query",
    },
    "fishmingyu/SweRankEmbed-Large": {
        "query_prompt_name": "query",
    },
    # --- Qwen3-Embedding: stock "query" prompt is for web search. Override
    # with a code-retrieval task description so the instruction matches our
    # use case (problem-statement → code).
    "Qwen/Qwen3-Embedding-0.6B": {
        "query_prompt": _CODE_RETRIEVAL_INSTRUCT,
        "document_prompt": "",
    },
    "Qwen/Qwen3-Embedding-4B": {
        "query_prompt": _CODE_RETRIEVAL_INSTRUCT,
        "document_prompt": "",
    },
    # --- nomic-ai/CodeRankEmbed: shipped query prompt prefix. No doc prompt.
    "nomic-ai/CodeRankEmbed": {
        "query_prompt_name": "query",
    },
    # --- jina-code-embeddings: multi-task. No plain "query" key; we pick the
    # natural-language → code retrieval pair. NOTE: doc-side has a non-empty
    # prompt, so existing FAISS indices built without it are **off-distribution
    # and must be rebuilt** to use this registry entry.
    "jinaai/jina-code-embeddings-1.5b": {
        "query_prompt_name": "nl2code_query",
        "document_prompt_name": "nl2code_document",
    },
    "jinaai/jina-code-embeddings-0.5b": {
        "query_prompt_name": "nl2code_query",
        "document_prompt_name": "nl2code_document",
    },
}


def resolve_prompts(model_name: str) -> Dict[str, Optional[str]]:
    """Look up registered query/document prompts for ``model_name``.

    Returns an empty dict when the model is not registered, in which case the
    wrapper falls back to plain ``model.encode(text)`` (no prefix) for both
    query and document sides.
    """
    return dict(_REGISTRY.get(model_name, {}))
