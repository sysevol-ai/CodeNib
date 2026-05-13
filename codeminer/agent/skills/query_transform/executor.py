# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import Any, Callable, Dict


def create_executor(context: Any) -> Callable[..., Dict[str, Any]]:
    """Create a query transform executor bound to the given TransformContext."""

    def execute(query: str, **kwargs: Any) -> Dict[str, Any]:
        extractor = context.ensure_keyword_extractor()
        result = extractor.extract_keywords(query)
        keywords = result.keywords

        joiner = kwargs.get("joiner", " ")
        rewritten = joiner.join(keywords) if keywords else query

        return {
            "query": rewritten,
            "keywords": keywords,
            "source_query": query,
        }

    return execute
