# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""Tests for engine-agnostic draft assembly in the serving worker."""

from typing import List, Sequence

import pytest

from codenib.serving.drafter.copy import CopyDrafter
from codenib.serving.drafter.retrieval import RetrievalBackend, RetrievalDrafter
from codenib.serving.server.worker import (
    OracleVerifier,
    SpeculativeConfig,
    SpeculativeServer,
    build_draft,
)
from codenib.serving.types import DraftTree, TokenId


class _FakeBackend(RetrievalBackend):
    def __init__(self, candidates: List[List[TokenId]]):
        self._candidates = candidates

    def retrieve(
        self, context: Sequence[TokenId], k: int, max_tokens: int
    ) -> List[List[TokenId]]:
        return [c[:max_tokens] for c in self._candidates[:k]]


def test_build_draft_fuses_copy_and_retrieval():
    context = [1, 2, 3, 1, 2]  # copy predicts -> 3
    copy = CopyDrafter(min_match=2, max_match=8, max_draft=4)
    retr = RetrievalDrafter(_FakeBackend([[3, 7, 8]]), k=1, max_draft=4)
    config = SpeculativeConfig(max_draft_tokens=8)

    tree = build_draft([copy, retr], context, config)
    # Both sources start with token 3; the shared prefix must collapse.
    roots = [c.token for c in tree.root.children]
    assert roots.count(3) == 1
    assert not tree.is_empty()


def test_build_draft_respects_budget():
    context = [5, 6, 7, 5, 6]
    copy = CopyDrafter(min_match=2, max_match=8, max_draft=16)
    config = SpeculativeConfig(max_draft_tokens=2)
    assert build_draft([copy], context, config).size <= 2


def test_server_step_delegates_to_build_draft():
    server = SpeculativeServer(
        drafters=[CopyDrafter(min_match=2, max_match=8, max_draft=4)],
        config=SpeculativeConfig(max_draft_tokens=4),
    )
    assert server.step([1, 2, 3, 1, 2]).size >= 1


def _copy_server(budget: int = 8) -> SpeculativeServer:
    return SpeculativeServer(
        drafters=[CopyDrafter(min_match=2, max_match=16, max_draft=budget)],
        config=SpeculativeConfig(max_draft_tokens=budget),
    )


def test_run_reconstructs_truth_against_oracle():
    # A repetitive sequence the copy drafter can speculate through.
    truth = [1, 2, 3, 4, 1, 2, 3, 4, 1, 2, 3, 4, 5]
    server = _copy_server()
    result = server.run(truth[:4], OracleVerifier(truth), max_new_tokens=64)
    # The loop must regenerate the true continuation exactly.
    assert truth[:4] + result.tokens == truth


def test_run_makes_forward_progress_and_reports_speedup():
    truth = [1, 2, 3, 4, 1, 2, 3, 4, 1, 2, 3, 4, 5]
    server = _copy_server()
    result = server.run(truth[:4], OracleVerifier(truth), max_new_tokens=64)
    generated = len(result.tokens)
    # Each pass emits its accepted drafts plus one bonus token; the only pass
    # that can emit no bonus is a trailing one (no EOS in `truth`).
    bonuses = generated - result.accepted_tokens
    assert bonuses in (result.forward_passes, result.forward_passes - 1)
    # Speculation actually helped on this repetitive stream.
    assert result.speedup > 1.0


def test_run_respects_max_new_tokens():
    truth = list(range(100)) + list(range(100))
    server = _copy_server()
    result = server.run(truth[:100], OracleVerifier(truth), max_new_tokens=5)
    assert len(result.tokens) == 5


def test_final_draft_is_bounded_by_remaining_generation_budget():
    class RecordingDrafter:
        seen = []

        def draft(self, context, max_tokens):
            self.seen.append(max_tokens)
            tree = DraftTree()
            tree.add_sequence([2, 3, 4], source="test")
            return tree

    drafter = RecordingDrafter()
    server = SpeculativeServer(
        drafters=[drafter],
        config=SpeculativeConfig(max_draft_tokens=16),
    )

    result = server.run([1], OracleVerifier([1, 2, 3]), max_new_tokens=1)

    assert result.tokens == [2]
    assert drafter.seen == [1]


def test_run_iter_marks_exact_budget_step_as_stopped() -> None:
    class MultiTokenDrafter:
        def draft(self, context, max_tokens):
            tree = DraftTree()
            tree.add_sequence([2, 3, 4], source="test")
            return tree

    server = SpeculativeServer(
        drafters=[MultiTokenDrafter()],
        config=SpeculativeConfig(max_draft_tokens=8),
    )

    steps = list(server.run_iter([1], OracleVerifier([1, 2, 3, 4]), max_new_tokens=2))

    assert len(steps) == 1
    assert steps[0].emitted == [2, 3]
    assert steps[0].accepted == 2
    assert steps[0].stop is True


def test_run_iter_skips_draft_below_minimum_threshold() -> None:
    class OneTokenDrafter:
        def draft(self, context, max_tokens):
            tree = DraftTree()
            tree.add_sequence([2], source="test")
            return tree

    class RecordingOracle(OracleVerifier):
        seen_sizes: List[int] = []

        def verify(self, context, tree):
            self.seen_sizes.append(tree.size)
            return super().verify(context, tree)

    verifier = RecordingOracle([1, 2])
    server = SpeculativeServer(
        drafters=[OneTokenDrafter()],
        config=SpeculativeConfig(max_draft_tokens=8, min_draft_tokens=2),
    )

    result = server.run([1], verifier, max_new_tokens=1)

    assert result.tokens == [2]
    assert result.accepted_tokens == 0
    assert verifier.seen_sizes == [0]


def test_run_iter_resets_request_scoped_drafter_state() -> None:
    class StatefulDrafter:
        begins = 0

        def begin_run(self):
            self.begins += 1

        def draft(self, context, max_tokens):
            return DraftTree()

    drafter = StatefulDrafter()
    server = SpeculativeServer(drafters=[drafter])

    first = server.run([1], OracleVerifier([1, 2, 3]), max_new_tokens=2)
    assert first.forward_passes == 2
    assert drafter.begins == 1

    server.run([1], OracleVerifier([1, 2]), max_new_tokens=1)

    assert drafter.begins == 2


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_run_rejects_invalid_generation_budget(value):
    server = _copy_server()

    with pytest.raises(ValueError, match="positive integer"):
        server.run([1], OracleVerifier([1, 2]), max_new_tokens=value)


def test_run_stops_at_end_of_truth():
    truth = [7, 8, 9]
    server = _copy_server()
    result = server.run(truth[:1], OracleVerifier(truth), max_new_tokens=64)
    assert truth[:1] + result.tokens == truth
