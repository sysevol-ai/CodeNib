# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the synthesizer vocabulary-overlap guard (issue #130).

These verify that a query which copies GT identifiers verbatim is flagged,
while a behavioral paraphrase that avoids domain vocabulary passes. They also
exercise the ``Verifier`` integration: a flagged query triggers regeneration
from an alternate consensus run.
"""

import pytest

from codeminer.dataset.synthesize._types import BehavioralContext, SampledCodeBlock
from codeminer.dataset.synthesize.verifier import Verifier
from codeminer.dataset.synthesize.vocab_guard import (
    DEFAULT_OVERLAP_THRESHOLD,
    VocabularyOverlapGuard,
    gt_identifier_tokens,
    tokens_from_path,
    tokens_from_symbol,
)

# A representative GT target drawn from issue #130 (astropy votable validate()).
GT_FILES = ["astropy/io/votable/table.py"]
GT_SYMBOLS = ["astropy/io/votable/table.py:VOTable.validate()"]

# Copies identifiers verbatim: votable / validate / table all leak.
LEAKY_QUERY = (
    "How does the VOTable validate routine in table.py perform votable "
    "validation when given a path object?"
)

# Behavioral paraphrase: describes the symptom, no GT identifiers reused.
CLEAN_QUERY = (
    "When I run the XML report routine on an astronomical metadata document "
    "and pass in a filesystem path object instead of a string, why does it "
    "raise an error rather than emitting the expected warnings?"
)


# ---------------------------------------------------------------------------
# Tokenization
# ---------------------------------------------------------------------------


def test_tokens_from_symbol_camel_and_snake_split():
    toks = set(tokens_from_symbol("table.py:VOTable.validate()"))
    assert "validate" in toks
    assert "table" in toks
    # CamelCase boundary: 'VOTable' -> 'vo' + 'table'
    assert "vo" in toks
    # empty / single-char fragments are dropped
    assert "" not in toks


def test_tokens_from_path_drops_extension():
    toks = set(tokens_from_path("astropy/io/votable/table.py"))
    assert "votable" in toks
    assert "astropy" in toks
    assert "py" not in toks  # extension stripped


def test_gt_identifier_tokens_drops_stopwords():
    toks = gt_identifier_tokens(
        target_files=["src/utils/cache_manager.py"],
        target_symbols=["cache_manager.py:CacheManager.invalidate()"],
    )
    # distinctive tokens survive
    assert "cache" in toks
    assert "manager" in toks
    assert "invalidate" in toks
    # generic path/word stopwords are removed
    assert "src" not in toks
    assert "utils" not in toks


# ---------------------------------------------------------------------------
# Guard scoring
# ---------------------------------------------------------------------------


def test_leaky_query_is_flagged():
    guard = VocabularyOverlapGuard()
    res = guard.evaluate(LEAKY_QUERY, target_files=GT_FILES, target_symbols=GT_SYMBOLS)
    assert res.flagged is True
    assert res.overlap_ratio > DEFAULT_OVERLAP_THRESHOLD
    assert "votable" in res.overlapping_tokens
    assert "validate" in res.overlapping_tokens
    assert bool(res) is True  # truthy == leaky


def test_paraphrased_query_passes():
    guard = VocabularyOverlapGuard()
    res = guard.evaluate(CLEAN_QUERY, target_files=GT_FILES, target_symbols=GT_SYMBOLS)
    assert res.flagged is False
    assert res.overlap_ratio <= DEFAULT_OVERLAP_THRESHOLD
    assert bool(res) is False


def test_is_leaky_convenience_wrapper():
    guard = VocabularyOverlapGuard()
    assert guard.is_leaky(LEAKY_QUERY, target_files=GT_FILES, target_symbols=GT_SYMBOLS)
    assert not guard.is_leaky(
        CLEAN_QUERY, target_files=GT_FILES, target_symbols=GT_SYMBOLS
    )


def test_threshold_is_configurable():
    # A strict threshold flags even a lightly overlapping query.
    light = "Why does validation of the document fail?"
    strict = VocabularyOverlapGuard(threshold=0.0)
    lenient = VocabularyOverlapGuard(threshold=0.99)
    strict_res = strict.evaluate(
        light, target_files=GT_FILES, target_symbols=GT_SYMBOLS
    )
    lenient_res = lenient.evaluate(
        light, target_files=GT_FILES, target_symbols=GT_SYMBOLS
    )
    # 'validation' shares no exact token with GT vocab (validate != validation),
    # so neither flags here; assert the threshold plumbs through instead.
    assert strict_res.threshold == 0.0
    assert lenient_res.threshold == 0.99


def test_empty_gt_vocab_never_flags():
    guard = VocabularyOverlapGuard()
    res = guard.evaluate(LEAKY_QUERY, target_files=[], target_symbols=[])
    assert res.flagged is False
    assert res.gt_token_count == 0
    assert res.overlap_ratio == 0.0


def test_invalid_threshold_rejected():
    with pytest.raises(ValueError):
        VocabularyOverlapGuard(threshold=1.5)
    with pytest.raises(ValueError):
        VocabularyOverlapGuard(threshold=-0.1)


# ---------------------------------------------------------------------------
# Verifier integration
# ---------------------------------------------------------------------------


def _make_block(block_id: str, file_path: str, node_name: str) -> SampledCodeBlock:
    return SampledCodeBlock(
        block_id=block_id,
        node_id=1,
        node_name=node_name,
        file_path=file_path,
        node_type="method",
        start_line=10,
        end_line=40,
        content="def validate(...): ...",
        char_count=24,
        line_count=2,
    )


def _make_context() -> BehavioralContext:
    core = _make_block("blk_1", GT_FILES[0], GT_SYMBOLS[0])
    return BehavioralContext(
        core_block=core, candidate_blocks=[core], neighborhood_blocks=[]
    )


def test_verifier_records_overlap_and_flags(monkeypatch):
    """Mode='none' so no agent call: just exercise quality + vocab guard."""
    ctx = _make_context()
    verifier = Verifier(agent=None, verification_mode="none")
    result = {
        "question": LEAKY_QUERY,
        "focus": None,
        "selected_blocks": [ctx.core_block],
    }
    out = verifier.verify(result, runs=[], behavioral_context=ctx, cwd="/tmp")
    assert out["vocab_overlap_flagged"] is True
    assert out["vocab_overlap_ratio"] > DEFAULT_OVERLAP_THRESHOLD
    # No clean alternate available -> flagged question is kept.
    assert out["question"] == LEAKY_QUERY


def test_verifier_regenerates_from_clean_alternate():
    """A flagged primary question is replaced by a clean alternate run."""
    ctx = _make_context()
    verifier = Verifier(agent=None, verification_mode="none")
    result = {
        "question": LEAKY_QUERY,
        "focus": "leaky",
        "selected_blocks": [ctx.core_block],
    }
    runs = [
        {"question": LEAKY_QUERY, "focus": "leaky"},
        {"question": CLEAN_QUERY, "focus": "clean"},
    ]
    out = verifier.verify(result, runs=runs, behavioral_context=ctx, cwd="/tmp")
    assert out["question"] == CLEAN_QUERY
    assert out["focus"] == "clean"
    assert out["vocab_overlap_flagged"] is False


def test_verifier_guard_can_be_disabled():
    """With enforcement off, a flagged query is reported but not replaced."""
    ctx = _make_context()
    verifier = Verifier(agent=None, verification_mode="none", enforce_vocab_guard=False)
    result = {
        "question": LEAKY_QUERY,
        "focus": "leaky",
        "selected_blocks": [ctx.core_block],
    }
    runs = [{"question": CLEAN_QUERY, "focus": "clean"}]
    out = verifier.verify(result, runs=runs, behavioral_context=ctx, cwd="/tmp")
    # Not regenerated despite a clean alternate being available.
    assert out["question"] == LEAKY_QUERY
    assert out["vocab_overlap_flagged"] is True


def test_strict_swap_restamps_overlap_metrics(monkeypatch):
    """Strict-mode alignment fallback must re-stamp vocab metrics for the
    swapped-in question (#130 regression): the recorded ``vocab_overlap_*``
    must describe the *final* query, not the pre-swap one.
    """
    ctx = _make_context()
    verifier = Verifier(agent=None, verification_mode="strict")

    # Alignment fails for the clean primary, passes only for the leaky
    # alternate -> forces a strict-mode question swap *after* the vocab
    # metrics were first stamped for the clean primary.
    def fake_align(question, behavioral_context, *, cwd):
        passed = question == LEAKY_QUERY
        return {
            "passed": passed,
            "block_id": "blk_1" if passed else "",
            "confidence": "high",
        }

    monkeypatch.setattr(verifier, "_verify_alignment", fake_align)

    result = {
        "question": CLEAN_QUERY,  # clean -> vocab guard keeps it, stamps low overlap
        "focus": "clean",
        "selected_blocks": [ctx.core_block],
    }
    runs = [
        {"question": CLEAN_QUERY, "focus": "clean"},
        {"question": LEAKY_QUERY, "focus": "leaky"},
    ]
    out = verifier.verify(result, runs=runs, behavioral_context=ctx, cwd="/tmp")

    # The leaky alternate was swapped in by the alignment fallback...
    assert out["question"] == LEAKY_QUERY
    assert out["verification_passed"] is True
    # ...and the stamped vocab metrics now describe THAT question, not CLEAN.
    assert out["vocab_overlap_flagged"] is True
    assert out["vocab_overlap_ratio"] > DEFAULT_OVERLAP_THRESHOLD
    assert "votable" in out["vocab_overlap_tokens"]
