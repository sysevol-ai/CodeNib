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


def _package_text_files(root: Path):
    for path in root.rglob("*"):
        if "__pycache__" in path.parts or not path.is_file():
            continue
        if path.suffix in {".py", ".md", ".yaml", ".yml"}:
            yield path


def _agent_compile_imports_under(root: Path):
    offenders = []
    for path in _python_files(root):
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

    return offenders


def test_codeminer_does_not_import_agent_compile_scripts():
    """Reusable packages must not depend on experiment-only script modules."""
    offenders = _agent_compile_imports_under(PROJECT_ROOT / "codeminer")

    assert offenders == []


def test_eval_tests_do_not_import_agent_compile_scripts():
    """Reusable eval coverage should exercise package APIs, not script shims."""
    offenders = _agent_compile_imports_under(PROJECT_ROOT / "test" / "eval")

    assert offenders == []


def test_reusable_agent_packages_do_not_name_experiment_surfaces():
    """Core/eval packages should not encode experiment-specific model lore."""
    forbidden = (
        "qwen",
        "haiku",
        "caddy",
        "astropy",
        "files@",
        "rec@",
        "scores 0",
    )
    offenders = []
    for root in (
        PROJECT_ROOT / "codeminer" / "agent",
        PROJECT_ROOT / "codeminer" / "eval" / "agent_runner",
    ):
        for path in _package_text_files(root):
            text = path.read_text(encoding="utf-8").lower()
            for term in forbidden:
                if term in text:
                    offenders.append(
                        f"{path.relative_to(PROJECT_ROOT)} contains {term!r}"
                    )

    assert offenders == []
