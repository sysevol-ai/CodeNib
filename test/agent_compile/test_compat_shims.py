# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for removed agent-compile compatibility shims."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SHIM_DIR = PROJECT_ROOT / "scripts" / "agent_compile" / "lib"


def test_agent_compile_lib_namespace_is_removed():
    sys.modules.pop("scripts.agent_compile.lib", None)

    with pytest.raises(ModuleNotFoundError, match="scripts.agent_compile.lib"):
        importlib.import_module("scripts.agent_compile.lib")


def test_agent_compile_lib_directory_is_removed():
    """Experiment scripts should not expose a core-looking lib namespace."""
    assert not (SHIM_DIR / "__init__.py").exists()
    assert list(SHIM_DIR.glob("*.py")) == []
