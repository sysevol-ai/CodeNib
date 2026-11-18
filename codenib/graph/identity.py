# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""One reading of a graph vertex name.

A vertex ``name`` is the builder's key. For Python it can carry the position
of a definition, so that same-named definitions in one scope stay apart:
``a.py:C.total(_codenib_l12_c8)`` for one overload of ``total``, and
``a.py:Form@codenib-l30-c4`` for one of two ``Form`` classes. That marker is
bookkeeping. Anything shown to a person or an agent, and anything that joins
two graphs, wants the name without it.

Every consumer used to strip the marker with its own pattern, and one of them
removed only the function form. These helpers are the single place that knows
what a marker looks like.
"""

from __future__ import annotations

import re

# Some names already carry ``()`` after the marker; one pair is kept.
_FUNCTION_MARKER = re.compile(r"\(_codenib_l\d+_c\d+\)(?:\(\))?")
_NAME_MARKER = re.compile(r"@codenib-l\d+-c\d+")


def strip_position_markers(text: str) -> str:
    """``text`` without position markers; a marked function keeps its ``()``."""

    return _NAME_MARKER.sub("", _FUNCTION_MARKER.sub("()", text))


def symbol_id(name: str) -> str:
    """Stable ``path:Qualified.name`` for a vertex name, without ``()``."""

    path, _, symbol = name.partition(":")
    if not symbol:
        return path
    return f"{path}:{strip_position_markers(symbol).replace('()', '')}"


def short_name(name: str) -> str:
    """Last identifier of a vertex name: ``a.py:C.m()`` -> ``m``."""

    symbol = strip_position_markers(name.split(":", 1)[-1])
    return symbol.replace("()", "").split(".")[-1]
