# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Small helper for preserving package APIs without eager imports."""

from __future__ import annotations

from importlib import import_module
from typing import Any, Mapping, MutableMapping

LazyExports = Mapping[str, tuple[str, str]]


def load_export(
    namespace: MutableMapping[str, Any],
    exports: LazyExports,
    name: str,
) -> Any:
    """Import, cache, and return one declared package export."""
    target = exports.get(name)
    if target is None:
        module_name = namespace.get("__name__", "module")
        raise AttributeError(f"module {module_name!r} has no attribute {name!r}")
    module_name, attribute = target
    value = getattr(import_module(module_name), attribute)
    namespace[name] = value
    return value


def exported_dir(namespace: Mapping[str, Any], exports: LazyExports) -> list[str]:
    """Include lazy exports in ``dir(package)``."""
    return sorted(set(namespace) | set(exports))


__all__ = ["LazyExports", "exported_dir", "load_export"]
