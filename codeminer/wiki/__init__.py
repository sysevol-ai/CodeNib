# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Index-derived wiki generation for the DeepWiki-style demo.

Produces a per-repo page tree and source-grounded page content from the
already-loaded BM25 / vector indexes — no LLM required. When an LLM is
configured it can later refine the prose, but every code anchor here resolves
to a real symbol span pulled from the indexes (no fabricated lines).
"""

from .builder import WikiBuilder

__all__ = ["WikiBuilder"]
