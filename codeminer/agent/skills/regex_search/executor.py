# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import Any, Callable, List, Optional


def create_executor(context: Any) -> Callable[..., List[Any]]:
    """Factory: returns a callable that performs regex-based search.

    Parameters
    ----------
    context:
        A ``RetrieveContext`` instance (from ``codeminer.ops.retrieve``)
        that carries the backing ``RegexNodeIndex``.
    """

    def execute(pattern: str, top_k: int = 20, **kwargs: Any) -> List[Any]:
        index = context.regex_index
        if index is None:
            # DEPRECATED: regex_search needs a RegexNodeIndex that is not built
            # in the current pipeline (skill_context wires regex_index=None).
            # Plain regex over file content needs no index — use the always-on
            # default tool instead.
            raise RuntimeError(
                "regex_search is unavailable (no regex node index is built). "
                "Use grep(pattern=...) for grep-style "
                "regex over file contents — it is always available."
            )

        file_glob: Optional[str] = kwargs.get("file_glob")
        node_type: Optional[str] = kwargs.get("node_type")
        case_sensitive: bool = kwargs.get("case_sensitive", False)
        use_regex: bool = kwargs.get("use_regex", True)

        results = index.search(
            pattern=pattern,
            file_glob=file_glob,
            node_type=node_type,
            case_sensitive=case_sensitive,
            use_regex=use_regex,
        )

        if top_k:
            results = results[:top_k]

        return results

    return execute
