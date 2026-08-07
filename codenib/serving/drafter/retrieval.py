# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""Retrieval-augmented speculation (RASD).

Use a retrieval index over the codebase to fetch candidate continuations and
build a draft tree from them. On an unfamiliar repo this is where a small draft
model is weakest and retrieval is strongest, because the tokens the agent needs
usually already exist in the corpus even when they are not in the prompt.

The retrieval source is abstracted behind :class:`RetrievalBackend` so CodeNib serving
does not hard-depend on any one index. A CodeNib adapter (symbol graph + BM25 +
embeddings + trigram) is the intended production backend; see
``CodeNibBackend`` below for the integration seam.
"""

from __future__ import annotations

import warnings
from abc import ABC, abstractmethod
from pathlib import Path
from typing import FrozenSet, List, Protocol, Sequence, Tuple

from codenib.serving.drafter.base import Drafter
from codenib.serving.types import DraftTree, TokenId

#: Candidate continuations requested per retrieval step. Shared by the server
#: and the acceptance harness for the same reason as the copy defaults above.
DEFAULT_RETRIEVAL_K = 6


class SnippetTokenizer(Protocol):
    """The text↔token surface a retrieval backend needs.

    Narrower than the serving ``Tokenizer`` on purpose: retrieval only decodes a
    query and encodes snippets, so anything with ``encode``/``decode`` — the
    serving wrapper, an experiment proxy, a test fake — can be injected.
    """

    def encode(self, text: str) -> Sequence[int]: ...

    def decode(self, ids: List[int]) -> str: ...


class RetrievalBackend(ABC):
    """Returns candidate token continuations for the current context.

    Implementations own detokenization/tokenization and ranking; CodeNib serving only
    consumes ranked token-id continuations. ``retrieve`` must be latency-bounded:
    it runs on (or just ahead of) the decode critical path, so a slow backend
    erases the speculation win.
    """

    @abstractmethod
    def retrieve(
        self, context: Sequence[TokenId], k: int, max_tokens: int
    ) -> List[List[TokenId]]:
        """Return up to ``k`` candidate continuations, each <= ``max_tokens``."""
        raise NotImplementedError


class RetrievalDrafter(Drafter):
    """Builds a draft tree from a :class:`RetrievalBackend`'s candidates.

    Each retrieved continuation is added as a branch; shared prefixes collapse
    automatically via :meth:`DraftTree.add_sequence`. Pruning to the global
    speculation budget is left to :func:`codenib.serving.drafter.fusion.fuse`, which
    sees all sources together.
    """

    source = "retrieval"

    def __init__(self, backend: RetrievalBackend, k: int = 4, max_draft: int = 16):
        self.backend = backend
        self.k = k
        self.max_draft = max_draft

    def draft(self, context: Sequence[TokenId], max_tokens: int) -> DraftTree:
        tree = DraftTree()
        budget = min(max_tokens, self.max_draft)
        if budget <= 0:
            return tree

        for cand in self.backend.retrieve(context, k=self.k, max_tokens=budget):
            tree.add_sequence(cand[:budget], source=self.source)
        return tree


def _path_parts(path: object) -> Tuple[str, ...]:
    return Path(str(path)).parts


def _same_file(a: Tuple[str, ...], b: Tuple[str, ...]) -> bool:
    """Whether two path part-tuples denote the same file.

    Compares from the right so an absolute index path and a repo-relative one
    still match (``/repo/pkg/x.py`` vs ``pkg/x.py``). A bare filename therefore
    matches any file with that name — deliberately over-, never under-matching,
    because the caller is using this to *hold data out*.
    """
    n = min(len(a), len(b))
    return n > 0 and a[-n:] == b[-n:]


class CodeNibBackend(RetrievalBackend):
    """Adapter onto a CodeNib index (in-process).

    CodeNib deals in *text* — its searchers take a query string and return code
    snippets, with no notion of token ids. So this backend bridges the gap in four
    steps:

    1. **Query.** Decode the tail of the context to text (what the model is
       currently writing) and use it as the retrieval query.
    2. **Retrieve.** Ask CodeNib in-process for the most relevant snippets
       (``ctx.bm25.search`` by default; ``vector`` for dense). Retrieval runs on
       the decode critical path, so only the fast in-process indexes are used.
    3. **Tokenize.** Encode each returned snippet back to token ids.
    4. **Align.** Reuse :class:`NgramRetrievalBackend`'s suffix-match over those
       snippets as a transient corpus — it finds where the current context suffix
       recurs inside a snippet and returns the tokens that follow. That is exactly
       the RASD "align retrieved code to the cursor" step, already unit-tested.

    The CodeNib ``ctx`` is injected (like the model in
    :class:`~codenib.serving.server.hf_engine.HFTreeEngine`) so the adapter is testable
    with a fake context and no ``codenib`` install; use :meth:`from_manifest` to
    load a prebuilt ``.codenib_cache`` index. ``tokenizer`` is any
    :class:`SnippetTokenizer` (e.g. ``codenib.serving.server.tokenization.Tokenizer``).
    """

    def __init__(
        self,
        ctx: object,
        tokenizer: SnippetTokenizer,
        *,
        index: str = "bm25",
        top_k: int = 20,
        key_len: int = 4,
        query_suffix_tokens: int = 64,
        exclude_files: Sequence[object] = (),
    ) -> None:
        self.ctx = ctx
        self.tokenizer = tokenizer
        self.index = index
        self.top_k = top_k
        self.key_len = key_len
        self.query_suffix_tokens = query_suffix_tokens
        self.exclude_files: FrozenSet[Tuple[str, ...]] = frozenset(
            _path_parts(p) for p in exclude_files
        )
        self._warned_empty_content = False
        self._warned_unattributed = False

    def excluding(self, *paths: object) -> "CodeNibBackend":
        """Return a view of this backend that never returns hits from ``paths``.

        The index (``ctx``) is shared, so this is cheap enough to build per
        target file. Use it whenever the caller already knows the answer for
        some file — an oracle replay retrieving the very file it is predicting
        would draft the ground-truth continuation and report acceptance that no
        live run could reproduce.
        """
        view = CodeNibBackend(
            self.ctx,
            self.tokenizer,
            index=self.index,
            top_k=self.top_k,
            key_len=self.key_len,
            query_suffix_tokens=self.query_suffix_tokens,
            exclude_files=paths,
        )
        # Union rather than replace: views compose, and `self` is untouched.
        view.exclude_files = self.exclude_files | view.exclude_files
        return view

    def _is_excluded(self, hit: object) -> bool:
        """Whether ``hit`` must be dropped because it comes from a held-out file."""
        if not self.exclude_files:
            return False
        raw = getattr(hit, "file", None) or getattr(hit, "file_path", None)
        if not raw:
            # No provenance: the hit cannot be *shown* to come from another
            # file, so drop it rather than risk leaking the held-out one. Say so
            # once — silently dropping every hit would look like "retrieval
            # found nothing" instead of "the index has no file metadata".
            if not self._warned_unattributed:
                self._warned_unattributed = True
                warnings.warn(
                    "CodeNib hit has no file/file_path attribute, so it "
                    "cannot be checked against the held-out files; dropping it "
                    "to stay conservative.",
                    RuntimeWarning,
                    stacklevel=3,
                )
            return True
        parts = _path_parts(raw)
        return any(_same_file(parts, ex) for ex in self.exclude_files)

    @classmethod
    def from_manifest(
        cls,
        manifest_path: str,
        tokenizer: SnippetTokenizer,
        *,
        index: str = "bm25",
        top_k: int = 20,
        key_len: int = 4,
        query_suffix_tokens: int = 64,
    ) -> "CodeNibBackend":
        """Load a prebuilt CodeNib index (``.codenib_cache/repo_manifest.json``).

        Only the selected runtime view is loaded; loading every manifest view
        would needlessly initialize vector models and Zoekt for a BM25 serving
        process. An explicit manifest is an opt-in to retrieval, so an absent,
        stale, or failed selected view aborts startup instead of silently
        degrading the process to copy-only drafting.
        """
        from codenib.mcp.context import ServerContext

        if index not in {"bm25", "vector"}:
            raise ValueError(f"unknown CodeNib index: {index!r}")

        ctx = ServerContext.load(manifest_path, views={index})
        if getattr(ctx, index, None) is None:
            errors = getattr(ctx, "errors", {})
            manifest = getattr(ctx, "manifest", None)
            entry = getattr(manifest, "indexes", {}).get(index)
            if index in errors:
                detail = f"failed to load: {errors[index]}"
            elif entry is None:
                detail = "the manifest has no entry for that index"
            elif not manifest.index_is_current(index):
                detail = "the manifest entry is stale for the current source"
            else:
                detail = "the manifest entry did not produce a runtime view"
            raise RuntimeError(
                f"requested CodeNib retrieval index {index!r} is unavailable "
                f"({detail}); rebuild the index before starting codenib-serve"
            )

        return cls(
            ctx,
            tokenizer,
            index=index,
            top_k=top_k,
            key_len=key_len,
            query_suffix_tokens=query_suffix_tokens,
        )

    def _search(self, query: str) -> list:
        """Return CodeNib hits (objects with a ``.content`` snippet) for ``query``."""
        if self.index == "bm25":
            bm25 = getattr(self.ctx, "bm25", None)
            if bm25 is None:
                return []
            # wrap_with_ln=False: keep snippets as clean code so drafted tokens
            # can match the model's output (line-number prefixes like "6 | "
            # never would).
            return bm25.search(
                query,
                top_k=self.top_k,
                return_code_content=True,
                wrap_with_ln=False,
            )
        if self.index == "vector":
            vector = getattr(self.ctx, "vector", None)
            if vector is None:
                return []
            return vector.search_with_content(query, top_k=self.top_k)
        raise ValueError(f"unknown CodeNib index: {self.index!r}")

    def retrieve(
        self, context: Sequence[TokenId], k: int, max_tokens: int
    ) -> List[List[TokenId]]:
        ctx_ids = list(context)
        if len(ctx_ids) < self.key_len:
            return []
        query = self.tokenizer.decode(ctx_ids[-self.query_suffix_tokens :])
        if not query.strip():
            return []

        corpus: List[List[TokenId]] = []
        hits = [h for h in self._search(query) if not self._is_excluded(h)]
        for hit in hits:
            content = getattr(hit, "content", None)
            if not content:
                continue
            ids = list(self.tokenizer.encode(content))
            if ids:
                corpus.append(ids)
        if not corpus:
            if hits and not self._warned_empty_content:
                # The index matched but carries no snippet text — e.g. a
                # legacy index saved without bm25_metadata.json, so
                # project_root is None and content lookup silently fails.
                # Say so once rather than reporting zero acceptance as a
                # measurement.
                self._warned_empty_content = True
                warnings.warn(
                    f"CodeNib returned {len(hits)} hit(s) with no snippet "
                    "content; retrieval will draft nothing. The index is "
                    "likely loaded without its symbol graph.",
                    RuntimeWarning,
                    stacklevel=2,
                )
            return []

        # Suffix-align the current context against the retrieved snippets.
        aligner = NgramRetrievalBackend(corpus, key_len=self.key_len)
        return aligner.retrieve(ctx_ids, k=k, max_tokens=max_tokens)


class NgramRetrievalBackend(RetrievalBackend):
    """Corpus n-gram lookup backend — a real, dependency-free retrieval source.

    Indexes every ``key_len``-gram in a token-id corpus (e.g. the other files in
    a repo). At query time it looks up the context suffix, extends each hit
    leftward to score by match length, and returns the continuations of the
    best-matching occurrences. This is "prompt-lookup over the whole corpus":
    it gives CodeNib serving retrieval *reach beyond the context window* without a
    model or a vector store, and is the default backend for offline acceptance
    measurement. CodeNib's symbol-graph/BM25/embedding retrieval is the
    production upgrade (see :class:`CodeNibBackend`).
    """

    def __init__(
        self,
        corpus: Sequence[Sequence[TokenId]],
        key_len: int = 4,
        max_candidates: int = 16,
    ) -> None:
        if key_len < 1:
            raise ValueError("key_len must be >= 1")
        self.key_len = key_len
        self.max_candidates = max_candidates
        self._flat: List[TokenId] = []
        # For each flat position, the exclusive end of the document it belongs to
        # so continuations never bleed across document boundaries.
        self._doc_end: List[int] = []
        self._index: dict = {}
        for doc in corpus:
            base = len(self._flat)
            self._flat.extend(doc)
            end = len(self._flat)
            self._doc_end.extend([end] * (end - base))
            for i in range(base, end - key_len + 1):
                key = tuple(self._flat[i : i + key_len])
                self._index.setdefault(key, []).append(i)

    def retrieve(
        self, context: Sequence[TokenId], k: int, max_tokens: int
    ) -> List[List[TokenId]]:
        if len(context) < self.key_len:
            return []
        ctx = list(context)
        key = tuple(ctx[-self.key_len :])
        positions = self._index.get(key)
        if not positions:
            return []

        scored = []
        for pos in positions[-self.max_candidates :]:
            # Extend the match leftward to measure how much context agrees.
            match = self.key_len
            a, b = len(ctx) - self.key_len - 1, pos - 1
            while a >= 0 and b >= 0 and ctx[a] == self._flat[b]:
                match += 1
                a -= 1
                b -= 1
            cont_start = pos + self.key_len
            cont_end = min(cont_start + max_tokens, self._doc_end[pos])
            cont = self._flat[cont_start:cont_end]
            if cont:
                scored.append((match, cont))

        scored.sort(key=lambda mc: mc[0], reverse=True)
        return [cont for _, cont in scored[:k]]
