# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Whether a symbol graph is connected the way its source is.

A graph can build without error and still be wrong in ways no count of nodes
shows. Two have happened:

- **Tests cut off from source.** A package the checkout imports from a
  directory the analyser was not told about leaves every such import
  unresolved. On one repository 15% of test functions had an edge into source;
  eleven repositories whose imports resolve sit between 48% and 87%.
- **Definitions repeated.** A symbolic link to the package made each definition
  exist three times, and callers were attached to only one of them.

:func:`graph_health` measures both so a build can say so. It reads only the
graph: no source, no language rules beyond the shared test-file convention.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List, Optional

from ..types import NODE_TYPE_FUNCTION, NODE_TYPE_METHOD
from ..utils import is_test_file

_DEFS = {NODE_TYPE_FUNCTION, NODE_TYPE_METHOD}
# Below these, with enough to judge by, a graph is reported as suspect.
MIN_TEST_FUNCTIONS = 50
LOW_TESTS_REACHING_SOURCE = 0.4
MIN_DEFINITIONS = 200
HIGH_REPEATED_DEFINITIONS = 0.2


def graph_health(graph) -> Dict[str, Any]:
    """Connectivity figures for ``graph`` (an igraph graph), with warnings.

    ``tests_reaching_source`` is the share of functions in test files that
    refer to a definition outside test files; ``None`` without test functions.
    ``repeated_definitions`` is the share of function and method definitions
    that share their file and line range with another one.
    """

    vs = graph.vs
    types: List[str] = vs["type"]
    files: List[Optional[str]] = vs["file"]
    starts: List[Optional[int]] = vs["start_line"]
    ends: List[Optional[int]] = vs["end_line"]
    defined = [bool(value) for value in vs["has_definition"]]

    in_test = [bool(path) and is_test_file(path) for path in files]
    tests = {
        vertex
        for vertex, kind in enumerate(types)
        if kind in _DEFS and defined[vertex] and in_test[vertex]
    }
    reaching = set()
    for (source, target), kind in zip(
        graph.get_edgelist(), graph.es["type"], strict=True
    ):
        if (
            kind == "reference"
            and source in tests
            and defined[target]
            and files[target]
            and not in_test[target]
        ):
            reaching.add(source)

    places = Counter(
        (files[vertex], starts[vertex], ends[vertex])
        for vertex, kind in enumerate(types)
        if kind in _DEFS and defined[vertex] and files[vertex]
    )
    definitions = sum(places.values())
    repeated = sum(count for count in places.values() if count > 1)

    health: Dict[str, Any] = {
        "test_functions": len(tests),
        "tests_reaching_source": (
            round(len(reaching) / len(tests), 3) if tests else None
        ),
        "definitions": definitions,
        "repeated_definitions": (
            round(repeated / definitions, 3) if definitions else 0.0
        ),
        "warnings": [],
    }
    share = health["tests_reaching_source"]
    if len(tests) >= MIN_TEST_FUNCTIONS and share < LOW_TESTS_REACHING_SOURCE:
        health["warnings"].append(
            f"Only {share:.0%} of {len(tests):,} test functions refer to source "
            "outside test files. Imports of the project's own packages may not "
            "resolve: check where the packages live."
        )
    if (
        definitions >= MIN_DEFINITIONS
        and health["repeated_definitions"] > HIGH_REPEATED_DEFINITIONS
    ):
        health["warnings"].append(
            f"{health['repeated_definitions']:.0%} of definitions appear more "
            "than once at the same place. A directory may be indexed twice, "
            "for example through a symbolic link."
        )
    return health
