# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Vocabulary-overlap guard for synthesized retrieval queries.

Issue #130: synthesized behavioral queries are *too easy* because the
synthesizer prompt leaks ground-truth (GT) semantic vocabulary -- identifier,
function, class, and file/path names copied verbatim (or via case-split
sub-tokens) into the query. That makes retrieval trivially keyword-matchable
and saturates the benchmark.

This module provides a post-generation guard that, given a query and the GT
code's identifier/path vocabulary, tokenizes both, computes the fraction of GT
identifier tokens that surface in the query, and flags (and optionally rejects)
queries whose overlap exceeds a configurable threshold.

Tokenization deliberately mirrors ``scripts/diagnose_query_leak.py`` (the
diagnostic tooling from PR #138 for the same issue) so the in-pipeline guard
and the offline diagnostic agree on what counts as a "leaked" token: identifier
names are split on ``.``/``::``/``/``/``_`` and CamelCase boundaries, and
single-character / stopword-only tokens are dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, List, Optional, Set

# Default fraction of distinctive GT identifier tokens that may appear in the
# query before it is flagged. Calibrated to be lenient: a paraphrase that reuses
# one or two unavoidable domain nouns still passes, while a query that copies the
# bulk of the GT vocabulary is flagged.
DEFAULT_OVERLAP_THRESHOLD = 0.5

# Generic words that are not distinctive identifiers; they appear in GT symbol /
# path names but carry no lexical-leak signal on their own.
_STOPWORDS: Set[str] = {
    "the",
    "and",
    "for",
    "with",
    "from",
    "into",
    "this",
    "that",
    "get",
    "set",
    "new",
    "old",
    "src",
    "lib",
    "test",
    "tests",
    "util",
    "utils",
    "core",
    "common",
    "base",
    "main",
    "init",
    "impl",
    "type",
    "types",
    "data",
    "value",
    "values",
    "key",
    "keys",
    "name",
    "names",
    "file",
    "files",
    "func",
    "method",
    "class",
    "module",
    "package",
}

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*")


def _camel_split(token: str) -> List[str]:
    """Split CamelCase / snake_case / kebab-case into lowercase sub-tokens."""
    parts: List[str] = []
    for part in token.replace("-", "_").split("_"):
        if not part:
            continue
        sub = re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+", part)
        parts.extend(s.lower() for s in sub if s)
    return parts


def tokenize_query(text: str) -> Set[str]:
    """Lowercase word-token set for a free-text query."""
    return {m.group(0).lower() for m in _WORD_RE.finditer(text or "")}


def tokens_from_symbol(symbol: str) -> List[str]:
    """Distinctive sub-tokens for a symbol id (``file.py:Class.method``)."""
    raw = re.split(r"[./:\\#()]+", symbol or "")
    out: List[str] = []
    for chunk in raw:
        for tok in _camel_split(chunk):
            if len(tok) > 1:
                out.append(tok)
    return out


def tokens_from_path(path: str) -> List[str]:
    """Distinctive sub-tokens for a file path (extension dropped)."""
    parts = re.split(r"[\\/]+", path or "")
    if parts:
        parts[-1] = re.sub(r"\.[A-Za-z0-9]+$", "", parts[-1])
    out: List[str] = []
    for part in parts:
        for tok in _camel_split(part):
            if len(tok) > 1:
                out.append(tok)
    return out


def gt_identifier_tokens(
    *,
    target_files: Optional[Iterable[str]] = None,
    target_symbols: Optional[Iterable[str]] = None,
    drop_stopwords: bool = True,
) -> Set[str]:
    """Collect the distinctive identifier/path token set from GT metadata.

    Combines case-split sub-tokens drawn from GT file paths and GT symbol ids.
    Generic stopwords (``test``, ``utils``, ``get`` ...) are removed by default
    so they do not inflate the overlap denominator.
    """
    tokens: Set[str] = set()
    for path in target_files or []:
        tokens.update(tokens_from_path(path))
    for symbol in target_symbols or []:
        tokens.update(tokens_from_symbol(symbol))
    if drop_stopwords:
        tokens = {t for t in tokens if t not in _STOPWORDS}
    return tokens


@dataclass
class VocabOverlapResult:
    """Outcome of a single vocabulary-overlap check."""

    flagged: bool
    overlap_ratio: float
    overlapping_tokens: List[str]
    gt_token_count: int
    threshold: float
    reason: str = ""

    def __bool__(self) -> bool:  # truthy == flagged (leaky)
        return self.flagged


@dataclass
class VocabularyOverlapGuard:
    """Flag/reject queries that copy too much GT identifier vocabulary.

    Parameters
    ----------
    threshold:
        Maximum allowed fraction of distinctive GT identifier tokens that may
        surface in the query. Above this the query is flagged. Defaults to
        :data:`DEFAULT_OVERLAP_THRESHOLD`.
    drop_stopwords:
        Whether to drop generic stopwords from the GT vocabulary before scoring.
    """

    threshold: float = DEFAULT_OVERLAP_THRESHOLD
    drop_stopwords: bool = True

    def __post_init__(self) -> None:
        if not 0.0 <= self.threshold <= 1.0:
            raise ValueError(f"threshold must be in [0, 1]; got {self.threshold!r}")

    def gt_tokens_from_metadata(
        self,
        target_files: Optional[Iterable[str]] = None,
        target_symbols: Optional[Iterable[str]] = None,
    ) -> Set[str]:
        """Precompute the GT identifier token set for repeated evaluations."""
        return gt_identifier_tokens(
            target_files=target_files,
            target_symbols=target_symbols,
            drop_stopwords=self.drop_stopwords,
        )

    def evaluate(
        self,
        query: str,
        *,
        target_files: Optional[Iterable[str]] = None,
        target_symbols: Optional[Iterable[str]] = None,
        gt_tokens: Optional[Set[str]] = None,
    ) -> VocabOverlapResult:
        """Compute the overlap between *query* and GT identifier vocabulary.

        Either pass GT metadata (``target_files`` / ``target_symbols``) or a
        precomputed ``gt_tokens`` set. The overlap ratio is

            |gt_tokens & query_tokens| / |gt_tokens|

        i.e. the fraction of distinctive GT identifier tokens that leaked into
        the query. An empty GT vocabulary can never be flagged (ratio ``0``).
        """
        if gt_tokens is None:
            gt_tokens = gt_identifier_tokens(
                target_files=target_files,
                target_symbols=target_symbols,
                drop_stopwords=self.drop_stopwords,
            )

        gt_count = len(gt_tokens)
        if gt_count == 0:
            return VocabOverlapResult(
                flagged=False,
                overlap_ratio=0.0,
                overlapping_tokens=[],
                gt_token_count=0,
                threshold=self.threshold,
                reason="no GT identifier tokens to compare against",
            )

        query_tokens = tokenize_query(query)
        overlap = sorted(gt_tokens & query_tokens)
        ratio = len(overlap) / gt_count
        flagged = ratio > self.threshold
        reason = ""
        if flagged:
            reason = (
                f"vocabulary overlap {ratio:.2f} exceeds threshold "
                f"{self.threshold:.2f}; leaked GT tokens: {', '.join(overlap)}"
            )
        return VocabOverlapResult(
            flagged=flagged,
            overlap_ratio=ratio,
            overlapping_tokens=overlap,
            gt_token_count=gt_count,
            threshold=self.threshold,
            reason=reason,
        )

    def is_leaky(
        self,
        query: str,
        *,
        target_files: Optional[Iterable[str]] = None,
        target_symbols: Optional[Iterable[str]] = None,
        gt_tokens: Optional[Set[str]] = None,
    ) -> bool:
        """Convenience boolean wrapper around :meth:`evaluate`."""
        return self.evaluate(
            query,
            target_files=target_files,
            target_symbols=target_symbols,
            gt_tokens=gt_tokens,
        ).flagged
