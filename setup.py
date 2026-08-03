# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Setuptools hooks for release-only non-Python assets."""

from __future__ import annotations

import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py as _build_py


class BuildPy(_build_py):
    """Stage the prebuilt Wiki frontend inside the Python package."""

    def run(self) -> None:
        super().run()
        source = Path(__file__).resolve().parent / "web" / "dist"
        if not (source / "index.html").is_file():
            raise RuntimeError(
                "the prebuilt Wiki frontend is missing; run "
                "`cd web && npm ci && npm run build` before building the wheel"
            )
        target = Path(self.build_lib) / "codenib" / "web" / "frontend"
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target)


setup(cmdclass={"build_py": BuildPy})
