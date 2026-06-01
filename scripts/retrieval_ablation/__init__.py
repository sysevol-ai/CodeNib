# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Offline retrieval ablations (no agent, no LLM).

These probes answer "what is the call graph / a given index worth as a
*retriever*?" — orthogonal to the agent-compile cost study (which is about the
agent's tool harness). They reuse ``scripts.agent_compile.lib`` for dataset +
prebuilt-index loading but do not run an agent loop.
"""
