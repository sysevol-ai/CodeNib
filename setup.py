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
    """Stage the Wiki frontend inside the Python package."""

    def run(self) -> None:
        super().run()
        source = Path(__file__).resolve().parent / "web"
        target = Path(self.build_lib) / "codenib" / "web" / "frontend"
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        for filename in (
            "next.config.js",
            "package-lock.json",
            "package.json",
            "tsconfig.json",
        ):
            shutil.copy2(source / filename, target / filename)
        for dirname in ("app", "components", "lib", "public", "types"):
            shutil.copytree(source / dirname, target / dirname)


setup(cmdclass={"build_py": BuildPy})
