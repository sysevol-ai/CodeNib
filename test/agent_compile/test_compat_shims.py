# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for deprecated agent-compile compatibility shims."""

from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SHIM_DIR = PROJECT_ROOT / "scripts" / "agent_compile" / "lib"


def test_agent_compile_lib_import_warns_deprecated():
    sys.modules.pop("scripts.agent_compile.lib", None)

    with pytest.warns(DeprecationWarning, match="codeminer.eval.agent_runner"):
        module = importlib.import_module("scripts.agent_compile.lib")

    assert "deprecated" in module._DEPRECATION_MESSAGE


def test_agent_compile_lib_files_are_import_only_shims():
    """Legacy lib modules must not regain reusable implementation ownership."""
    offenders = []
    for path in sorted(SHIM_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}")
                continue
            if isinstance(node, ast.Import):
                if any(alias.name != "warnings" for alias in node.names):
                    offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}")
                continue
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if (
                    module != "__future__"
                    and module != "warnings"
                    and not module.startswith("codeminer.eval.agent_runner")
                ):
                    offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}")

    assert offenders == []
