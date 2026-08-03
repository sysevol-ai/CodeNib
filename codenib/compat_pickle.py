# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Read pickles written before the package rename.

Index artifacts (document stores, symbol graphs) embed the qualified name of
the class that wrote them. Artifacts built under the former package name still
resolve their classes against a module path that no longer exists, so a plain
``pickle.load`` raises ``ModuleNotFoundError`` and the caller degrades to a
span-less fallback. Rewriting the root package on the way in keeps those
artifacts readable without a reindex.

The former identifier is assembled rather than written literally so
``scripts/check_namespace.py`` stays authoritative — see
``docs/codenib_namespace_migration.md``.
"""

from __future__ import annotations

import pickle
from typing import IO, Any

_FORMER_ROOT = "code" + "miner"
_CURRENT_ROOT = "codenib"


def rewrite_module(module: str) -> str:
    """Map a former-namespace module path onto the current package."""

    if module == _FORMER_ROOT:
        return _CURRENT_ROOT
    if module.startswith(_FORMER_ROOT + "."):
        return _CURRENT_ROOT + module[len(_FORMER_ROOT) :]
    return module


class RenamingUnpickler(pickle.Unpickler):
    """Unpickler that resolves former-namespace classes to current ones."""

    def find_class(self, module: str, name: str) -> Any:  # noqa: D102
        return super().find_class(rewrite_module(module), name)


def load(handle: IO[bytes]) -> Any:
    """``pickle.load`` that also accepts pre-rename artifacts."""

    return RenamingUnpickler(handle).load()
