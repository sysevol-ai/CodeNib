# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Installed CodeNib version.

``pyproject.toml`` is the only authored version source. Package metadata makes
that value available at runtime without importing optional CodeNib subsystems.
"""

from __future__ import annotations

from importlib import metadata


def package_version() -> str:
    """Return the installed distribution version."""
    try:
        return metadata.version("codenib")
    except metadata.PackageNotFoundError:
        return "0+unknown"


__version__ = package_version()

__all__ = ["__version__", "package_version"]
