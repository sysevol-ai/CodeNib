# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for agent-compile experiment configuration."""

from __future__ import annotations

from pathlib import Path

import yaml

from scripts.agent_compile.lib.config import SweepConfig

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_sweep_config_loads_gate_extra_body_from_yaml(tmp_path):
    config = tmp_path / "cfg.yaml"
    config.write_text(
        """
sweep_id: gate
subsets:
  preinj_eager_gated: [read, grep]
gate_llm_extra_body:
  chat_template_kwargs:
    enable_thinking: false
""",
        encoding="utf-8",
    )

    cfg = SweepConfig.from_yaml(config)

    assert cfg.gate_llm_extra_body == {
        "chat_template_kwargs": {"enable_thinking": False}
    }


def test_eager_compact_recipes_pin_compact_keep_reads():
    offenders = []
    for path in sorted(
        (PROJECT_ROOT / "scripts" / "agent_compile" / "configs").glob("*.yaml")
    ):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for arm, recipe in (data.get("preload") or {}).items():
            if isinstance(recipe, dict) and recipe.get("mode") == "eager_compact":
                if "compact_keep_reads" not in recipe:
                    offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{arm}")

    assert offenders == []
