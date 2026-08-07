# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""SGLang speculative-decoding integration.

This module is the seam between CodeNib serving's drafters and the target engine. The
draft-assembly logic (``build_draft``) is engine-agnostic; the speculation loop
(``SpeculativeServer.run``) drives it against any object implementing the
``Verifier`` protocol. The production verifier lives in
:mod:`codenib.serving.server.sglang` (``SGLangVerifier`` over a ``TargetEngine``); its
only stubbed part is the GPU model forward. The loop itself is real and is
exercised offline by ``OracleVerifier`` (and in the experiments harness).

Per-step loop::

    context = prompt
    while not done:
        tree   = build_draft(drafters, context, budget)   # this module
        result = verifier.verify(context, tree)           # SGLang forward / oracle
        context += result.emitted                         # >= 1 token / step
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Iterator, List, Optional, Sequence

from codenib.serving.drafter.base import Drafter
from codenib.serving.drafter.fusion import fuse
from codenib.serving.types import DraftTree, TokenId


@dataclass
class SpeculativeConfig:
    """Tuning knobs for the speculation loop."""

    #: Max candidate positions per verification tree (the speculation budget).
    max_draft_tokens: int = 16
    #: Skip verification overhead when fewer than this many tokens were drafted.
    min_draft_tokens: int = 1


def build_draft(
    drafters: Sequence[Drafter],
    context: Sequence[TokenId],
    config: SpeculativeConfig,
    *,
    max_tokens: Optional[int] = None,
) -> DraftTree:
    """Run every drafter and fuse the results into one budgeted tree.

    Drafters are consulted in order, so earlier ones (typically the near-free
    copy drafter) own shared prefixes; retrieval and model drafts graft on. The
    fused tree is pruned to ``config.max_draft_tokens``.
    """
    budget = config.max_draft_tokens
    if max_tokens is not None:
        budget = min(budget, max_tokens)
    trees = [d.draft(context, budget) for d in drafters]
    trees = [t for t in trees if not t.is_empty()]
    return fuse(trees, max_tokens=budget)


@dataclass
class VerifyResult:
    """Outcome of verifying one draft tree against the target.

    ``accepted`` are drafted tokens the target confirmed along its chosen path
    (>= 0). ``bonus`` is the one free token the target emits for the next
    position, or ``None`` when generation should stop (EOS / budget reached).
    """

    accepted: List[TokenId]
    bonus: Optional[TokenId]

    @property
    def emitted(self) -> List[TokenId]:
        """All tokens to append to the context this step (accepted + bonus)."""
        if self.bonus is None:
            return list(self.accepted)
        return [*self.accepted, self.bonus]


class Verifier(ABC):
    """Confirms a draft tree against the target model.

    A verifier consumes the current ``context`` and a ``DraftTree`` and returns
    the prefix of drafted tokens the target accepts plus one bonus token. The
    real engine (``SGLangVerifier``) verifies the whole tree in a single forward
    pass; ``OracleVerifier`` stands in for offline evaluation and tests.
    """

    @abstractmethod
    def verify(self, context: Sequence[TokenId], tree: DraftTree) -> VerifyResult: ...


@dataclass
class StepResult:
    """One decoding step's outcome, yielded by ``SpeculativeServer.run_iter``.

    ``emitted`` are the tokens appended to the context this step (accepted draft
    tokens plus the bonus, possibly empty on a terminal no-emit step). ``accepted``
    counts how many of those were drafted tokens the target confirmed. ``stop`` is
    True on the last step (EOS reached, the token budget is exhausted, or
    nothing is left to emit).
    """

    emitted: List[TokenId]
    accepted: int
    stop: bool


@dataclass
class RunResult:
    """Aggregate stats from a full ``SpeculativeServer.run``."""

    tokens: List[TokenId]
    forward_passes: int
    accepted_tokens: int

    @property
    def speedup(self) -> float:
        """Tokens emitted per forward pass (1.0 == no speculation benefit)."""
        return len(self.tokens) / self.forward_passes if self.forward_passes else 1.0


@dataclass
class OracleVerifier(Verifier):
    """Reference verifier whose "target" is a known true continuation.

    Accepts the longest draft-tree path that matches ``truth`` from the current
    context length, then emits one bonus truth token. This makes the speculation
    loop runnable and testable without a GPU; it is the same acceptance math used
    by ``experiments/acceptance.py``. ``context`` must be a prefix of ``truth``
    (the loop maintains this when started from ``truth[:k]``).
    """

    truth: Sequence[TokenId]

    def verify(self, context: Sequence[TokenId], tree: DraftTree) -> VerifyResult:
        pos = len(context)
        n = len(self.truth)
        accepted: List[TokenId] = []
        node = tree.root
        while node.children and pos + len(accepted) < n:
            want = self.truth[pos + len(accepted)]
            child = next((c for c in node.children if c.token == want), None)
            if child is None:
                break
            accepted.append(child.token)
            node = child
        nxt = pos + len(accepted)
        bonus = self.truth[nxt] if nxt < n else None
        return VerifyResult(accepted=accepted, bonus=bonus)


# The production verifier backed by a live SGLang engine lives in
# ``codenib.serving.server.sglang`` (``SGLangVerifier`` + ``TargetEngine``). It is kept
# out of this module so the speculation loop here stays engine-agnostic and
# importable without sglang or a GPU.


@dataclass
class SpeculativeServer:
    """Drives CodeNib serving drafting through a speculative-decoding loop.

    ``drafters`` are the ordered draft sources (e.g. ``[CopyDrafter(),
    RetrievalDrafter(backend)]``). ``step`` builds one fused draft tree; ``run``
    closes the loop against any :class:`Verifier`.
    """

    drafters: List[Drafter] = field(default_factory=list)
    config: SpeculativeConfig = field(default_factory=SpeculativeConfig)

    def step(
        self,
        context: Sequence[TokenId],
        *,
        max_tokens: Optional[int] = None,
    ) -> DraftTree:
        """Produce the draft tree for one decoding step (engine-agnostic)."""
        return build_draft(
            self.drafters,
            context,
            self.config,
            max_tokens=max_tokens,
        )

    def run_iter(
        self,
        prompt: Sequence[TokenId],
        verifier: Verifier,
        *,
        max_new_tokens: int = 256,
    ) -> Iterator[StepResult]:
        """Speculatively decode, yielding one :class:`StepResult` per forward pass.

        Each iteration drafts a fused tree, has ``verifier`` confirm a prefix plus
        a bonus token, appends the result to the context (>= 1 token/pass), and
        yields it — so a streaming caller can emit tokens as they are produced.
        Generation ends at EOS, at ``max_new_tokens``, or when the verifier emits
        nothing. This is the single decoding loop; :meth:`run` aggregates it.
        """
        if isinstance(max_new_tokens, bool) or not isinstance(max_new_tokens, int):
            raise ValueError("max_new_tokens must be a positive integer")
        if max_new_tokens <= 0:
            raise ValueError("max_new_tokens must be a positive integer")

        for drafter in self.drafters:
            begin_run = getattr(drafter, "begin_run", None)
            if callable(begin_run):
                begin_run()

        context: List[TokenId] = list(prompt)
        produced = 0

        while produced < max_new_tokens:
            remaining = max_new_tokens - produced
            tree = self.step(context, max_tokens=remaining)
            if tree.size < self.config.min_draft_tokens:
                # Still run the target once for its next-token prediction, but
                # do not feed a too-small draft whose extra verification work
                # is unlikely to amortize its overhead.
                tree = DraftTree()
            result = verifier.verify(context, tree)

            emitted = result.emitted[: max_new_tokens - produced]
            accepted = min(len(result.accepted), len(emitted))
            stop = (
                not emitted
                or result.bonus is None
                or produced + len(emitted) >= max_new_tokens
            )
            if emitted:
                context.extend(emitted)
                produced += len(emitted)

            yield StepResult(emitted=emitted, accepted=accepted, stop=stop)
            if stop:
                return

    def run(
        self,
        prompt: Sequence[TokenId],
        verifier: Verifier,
        *,
        max_new_tokens: int = 256,
    ) -> RunResult:
        """Speculatively decode up to ``max_new_tokens`` tokens after ``prompt``.

        Drives :meth:`run_iter` to completion and aggregates the generated tokens
        and the forward-pass / acceptance counts that determine the realized
        speedup. Output is identical to consuming ``run_iter`` directly.
        """
        generated: List[TokenId] = []
        passes = 0
        accepted_total = 0

        for step in self.run_iter(prompt, verifier, max_new_tokens=max_new_tokens):
            passes += 1
            generated.extend(step.emitted)
            accepted_total += step.accepted

        return RunResult(
            tokens=generated, forward_passes=passes, accepted_tokens=accepted_total
        )
