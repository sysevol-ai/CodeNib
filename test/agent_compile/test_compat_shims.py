# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for deprecated agent-compile compatibility shims."""

from __future__ import annotations

import importlib
import sys

import pytest


def test_agent_compile_lib_import_warns_deprecated():
    sys.modules.pop("scripts.agent_compile.lib", None)

    with pytest.warns(DeprecationWarning, match="codeminer.eval.agent_runner"):
        module = importlib.import_module("scripts.agent_compile.lib")

    assert "deprecated" in module._DEPRECATION_MESSAGE
