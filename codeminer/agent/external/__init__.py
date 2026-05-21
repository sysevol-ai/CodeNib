# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Wrappers around third-party agent SDKs used as localization baselines.

These agents are external to CodeMiner's own agent stack (``rerank_agent``,
``runner``, ``tools``...): each is a thin, read-only wrapper over a vendor SDK
(``claude_agent_sdk``, ``openai_codex``) that emits chunker-canonical symbol
names through a shared ``LOC_OUTPUT_SCHEMA``. The vendor SDKs are intentionally
not declared in ``pyproject.toml`` — they are only needed for the paid baseline
experiments, not as library dependencies.
"""
