# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Import-boundary tests for agent architecture modules."""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _python_files(root: Path):
    for path in root.rglob("*.py"):
        if "__pycache__" not in path.parts:
            yield path


def test_codeminer_does_not_import_agent_compile_scripts():
    """Reusable packages must not depend on experiment-only script modules."""
    offenders = []
    for path in _python_files(PROJECT_ROOT / "codeminer"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            module = None
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("scripts.agent_compile"):
                        module = alias.name
                        break
            elif isinstance(node, ast.ImportFrom):
                module = node.module
            if module and module.startswith("scripts.agent_compile"):
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}")

    assert offenders == []
