# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""Retrieval-augmented speculative serving for code agents.

CodeNib indexes a repository; this package spends that index on *decode*. A
speculative-decoding loop drafts continuations — from the prompt itself
(:mod:`~codenib.serving.drafter.copy`) and from the retrieval index
(:mod:`~codenib.serving.drafter.retrieval`) — and a target model verifies them
in one forward pass. Acceptance is greedy-exact, so the emitted text is
identical to plain autoregressive decoding; only the tokens-per-forward-pass
changes.

The HTTP surface is OpenAI-compatible (``codenib-serve``), so an agent or a
LiteLLM gateway calls it exactly as it would call OpenAI. See ``deploy/`` for a
gateway config and ``examples/serving_agent_demo.py`` for an end-to-end agent.

Heavy dependencies (torch, transformers, sse-starlette) live behind the
``serving`` extra and are imported lazily, so importing this package on a
machine without a GPU stack is free.

Originally developed as the RetroSpec research project and vendored here when
serving became product surface.
"""

from codenib.serving.types import DraftNode, DraftTree

__all__ = ["DraftNode", "DraftTree"]
