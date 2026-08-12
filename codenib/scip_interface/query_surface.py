# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Canonical digest of the ordered symbol-query graph surface.

The framing is shared with ``core/`` but this module depends only on Python's
standard library.  Callers supply a CodeGraph-compatible duck type; importing
this module never imports igraph or the persisted graph implementation.
"""

from __future__ import annotations

import hashlib
import struct
from collections.abc import Mapping
from typing import Any

QUERY_SURFACE_SCHEMA_VERSION = 1
_MAGIC = b"CodeNib-FactQuery-Surface\0"


def _attributes(item: object, *, label: str) -> Mapping[str, Any]:
    method = getattr(item, "attributes", None)
    value = method() if callable(method) else item
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} attributes must be a mapping")
    return value


def _required_string(value: object, *, label: str) -> bytes:
    if type(value) is not str:
        raise ValueError(f"{label} must be a string")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} must be strict UTF-8") from exc
    return b"\x01" + struct.pack(">Q", len(encoded)) + encoded


def _optional_string(value: object, *, label: str) -> bytes:
    if value is None:
        return b"\x00"
    return _required_string(value, label=label)


def _required_int(value: object, *, label: str) -> bytes:
    if type(value) is not int:
        raise ValueError(f"{label} must be an integer")
    try:
        return b"\x01" + struct.pack(">q", value)
    except struct.error as exc:
        raise ValueError(f"{label} is outside signed 64-bit range") from exc


def _optional_int(value: object, *, label: str) -> bytes:
    if value is None:
        return b"\x00"
    return _required_int(value, label=label)


def _optional_bool(value: object, *, label: str) -> bytes:
    if value is None:
        return b"\x00"
    if type(value) is not bool:
        raise ValueError(f"{label} must be a boolean")
    return b"\x02" if value else b"\x01"


def query_surface_sha256(graph: object) -> str:
    """Hash vertices and edges in their exact graph index order."""

    storage = getattr(graph, "graph", graph)
    vertices = list(storage.vs)
    edges = list(storage.es)
    digest = hashlib.sha256()
    digest.update(_MAGIC)
    digest.update(struct.pack(">I", QUERY_SURFACE_SCHEMA_VERSION))
    digest.update(struct.pack(">Q", len(vertices)))

    for position, vertex in enumerate(vertices):
        attrs = _attributes(vertex, label=f"vertex[{position}]")
        name = attrs.get("name")
        digest.update(b"V")
        digest.update(_required_string(name, label=f"vertex[{position}].name"))
        digest.update(
            _required_string(attrs.get("type"), label=f"vertex[{position}].type")
        )
        digest.update(
            _optional_string(attrs.get("file"), label=f"vertex[{position}].file")
        )
        for field in ("start_line", "end_line", "selection_line"):
            digest.update(
                _optional_int(attrs.get(field), label=f"vertex[{position}].{field}")
            )
        for field in ("unified_name", "symbol_kind"):
            digest.update(
                _optional_string(attrs.get(field), label=f"vertex[{position}].{field}")
            )
        digest.update(
            _optional_bool(
                attrs.get("has_definition"),
                label=f"vertex[{position}].has_definition",
            )
        )

    digest.update(struct.pack(">Q", len(edges)))
    for position, edge in enumerate(edges):
        attrs = _attributes(edge, label=f"edge[{position}]")
        source = getattr(edge, "source", None)
        target = getattr(edge, "target", None)
        if (
            type(source) is not int
            or type(target) is not int
            or source < 0
            or target < 0
            or source >= len(vertices)
            or target >= len(vertices)
        ):
            raise ValueError(f"edge[{position}] has an invalid endpoint")
        digest.update(b"E")
        digest.update(_required_int(source, label=f"edge[{position}].source"))
        digest.update(_required_int(target, label=f"edge[{position}].target"))
        digest.update(
            _required_string(attrs.get("type"), label=f"edge[{position}].type")
        )
        digest.update(
            _optional_string(
                attrs.get("anchor_file"), label=f"edge[{position}].anchor_file"
            )
        )
        digest.update(
            _optional_int(
                attrs.get("anchor_line"), label=f"edge[{position}].anchor_line"
            )
        )
    return digest.hexdigest()


__all__ = ["QUERY_SURFACE_SCHEMA_VERSION", "query_surface_sha256"]
