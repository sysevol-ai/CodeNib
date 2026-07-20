# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for agent-compile experiment configuration."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from codeminer.eval.agent_runner.sweep_config import SweepConfig

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_sweep_config_loads_gate_extra_body_from_yaml(tmp_path):
    config = tmp_path / "cfg.yaml"
    config.write_text(
        """
sweep_id: gate
model: test/model
embedding_model: test/embedding
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
    assert cfg.force_localization_contract


def test_sweep_config_can_disable_localization_contract_for_ablation(tmp_path):
    config = tmp_path / "cfg.yaml"
    config.write_text(
        """
sweep_id: no_contract
model: test/model
embedding_model: test/embedding
subsets:
  defaults_only: [read, grep]
force_localization_contract: false
""",
        encoding="utf-8",
    )

    cfg = SweepConfig.from_yaml(config)

    assert not cfg.force_localization_contract


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


@pytest.mark.parametrize(
    ("config_name", "model"),
    [
        ("gemma4_12b_synth_runtime.yaml", "openai/gemma4-12b"),
        ("gemini25_flash_synth_runtime.yaml", "vertex_ai/gemini-2.5-flash"),
    ],
)
def test_agent_runtime_extensions_match_frozen_protocol(config_name, model):
    config = PROJECT_ROOT / "scripts" / "agent_compile" / "configs" / config_name

    cfg = SweepConfig.from_yaml(config)

    assert cfg.model == model
    assert cfg.reps == 1
    assert cfg.max_turns == 16
    assert cfg.max_context_tokens == 48_000
    assert cfg.max_tokens == 4_096
    assert cfg.first_turn_tool_choice is None
    assert cfg.instances == []
    assert cfg.dataset == "sysevol-ai/codeminer-synthesis"
    assert cfg.dataset_revision == "5ac36c39ef69bbfe2e14dac58b6067b8c350c53e"
    assert set(cfg.subsets) == {
        "grep_only",
        "preinj_eager",
        "preinj_eager_compact",
    }
    assert cfg.preload["preinj_eager"]["top_k"] == 10
    assert cfg.preload["preinj_eager"]["level"] == "l2"
    compact = cfg.preload["preinj_eager_compact"]
    assert compact["mode"] == "eager_compact"
    assert compact["compact_keep_reads"] == 0
    assert cfg.run_metadata


def test_gemma_runtime_config_records_immutable_serving_provenance():
    config = (
        PROJECT_ROOT
        / "scripts"
        / "agent_compile"
        / "configs"
        / "gemma4_12b_synth_runtime.yaml"
    )

    cfg = SweepConfig.from_yaml(config)

    assert cfg.model_revision == "12ace6d648d72bd41519e140f1185f34d38c7e3d"
    assert cfg.run_metadata["vllm_version"] == "0.25.1"
    assert cfg.run_metadata["tool_call_parser"] == "gemma4"
    assert cfg.run_metadata["enable_thinking"] is False
    assert len(cfg.run_metadata["chat_template_sha256"]) == 64


def test_gemini_runtime_config_records_json_safe_provider_provenance():
    config = (
        PROJECT_ROOT
        / "scripts"
        / "agent_compile"
        / "configs"
        / "gemini25_flash_synth_runtime.yaml"
    )

    cfg = SweepConfig.from_yaml(config)

    assert cfg.run_metadata["provider"] == "vertex-ai"
    assert cfg.run_metadata["access_date"] == "2026-07-20"
    assert cfg.run_metadata["thinking_budget"] == 0
