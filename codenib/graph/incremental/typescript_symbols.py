# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Recover compiler name spans omitted from TypeScript LSP function symbols."""

from __future__ import annotations

import copy
from collections import defaultdict


def _walk(symbols):
    for symbol in symbols if isinstance(symbols, list) else ():
        if isinstance(symbol, dict):
            yield symbol
            yield from _walk(symbol.get("children"))


def _span(value, *, compiler=False):
    if not isinstance(value, dict):
        return None
    coordinates = []
    for side in ("start", "end"):
        point = value.get(side)
        if not isinstance(point, dict):
            return None
        for key in ("line", "offset" if compiler else "character"):
            number = point.get(key)
            if type(number) is not int or number < int(compiler):
                return None
            coordinates.append(number - int(compiler))
    span = tuple(coordinates)
    return span if span[:2] < span[2:] else None


def _range(span):
    return {
        "start": {"line": span[0], "character": span[1]},
        "end": {"line": span[2], "character": span[3]},
    }


def _needs_name_span(symbol):
    return (
        symbol.get("kind") in (6, 12)
        and _span(symbol.get("range")) is not None
        and symbol.get("selectionRange") == symbol.get("range")
    )


def needs_typescript_name_spans(symbols):
    """Whether a callable's selection is only the provider's whole-body fallback."""
    return any(_needs_name_span(symbol) for symbol in _walk(symbols))


def restore_typescript_name_spans(symbols, navigation_tree):
    """Match exact compiler names/body spans; never guess a source position.

    TypeScript's navtree puts an assigned function's name before its initializer.
    typescript-language-server can discard that non-overlapping nameSpan and
    select the initializer instead. Preserve the compiler's name and widen the
    LSP range to contain it. Conflicting compiler entries or stale/missing body
    matches leave the original symbol intact. Both protocols use UTF-16 offsets.
    """
    names = defaultdict(set)

    def collect(node):
        if not isinstance(node, dict):
            return
        name_span = _span(node.get("nameSpan"), compiler=True)
        if (
            node.get("kind") in ("function", "method")
            and isinstance(node.get("text"), str)
            and name_span is not None
        ):
            for span in node.get("spans") or []:
                body = _span(span, compiler=True)
                if body is not None:
                    names[node["text"], body].add(name_span)
        for child in node.get("childItems") or []:
            collect(child)

    collect(navigation_tree)
    result = copy.deepcopy(symbols)
    for symbol in _walk(result):
        if not _needs_name_span(symbol):
            continue
        body = _span(symbol.get("range"))
        if body is None or not isinstance(symbol.get("name"), str):
            continue
        matches = names.get((symbol["name"], body), ())
        if len(matches) != 1:
            continue
        name_span = next(iter(matches))
        symbol["selectionRange"] = _range(name_span)
        symbol["range"] = _range(
            min(body[:2], name_span[:2]) + max(body[2:], name_span[2:])
        )
    return result
