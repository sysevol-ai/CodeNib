# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from codenib.web.config import load_config


def test_minimal_config_uses_concrete_dataclass_defaults(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("wiki_agent: false\n")

    config = load_config(str(config_path))

    assert config.model == "gpt-4o"
    assert config.mode == "sparse"
    assert config.embedding_dimension == 384
    assert config.max_turns == 8
    assert config.wiki_agent is False


def test_default_config_does_not_import_durable_storage() -> None:
    root = Path(__file__).resolve().parents[2]
    script = """
import sys

from codenib.web.config import QAConfig

QAConfig()
if "codenib.storage" in sys.modules:
    raise SystemExit("durable storage imported without explicit configuration")
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_config_rejects_removed_index_storage(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("index_storage: {}\n")

    with pytest.raises(ValueError, match="no longer supported"):
        load_config(str(config_path))


def test_config_rejects_duplicate_yaml_keys_within_one_profile(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("wiki_agent: false\n" "wiki_agent: true\n")

    with pytest.raises(ValueError, match="duplicate YAML mapping key"):
        load_config(str(config_path))


def test_wiki_media_config_enables_local_renderer_without_endpoint(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
wiki_media_model: local/svg
wiki_media_options:
  provider: local
  width: 1024
""".lstrip()
    )

    config = load_config(str(config_path))

    assert config.wiki_media_generation_enabled is True
    assert config.wiki_media_api_base is None
    assert config.wiki_media_options == {"provider": "local", "width": 1024}


def test_wiki_media_environment_overrides_file_config(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
wiki_media_model: local/svg
wiki_media_options:
  provider: local
""".lstrip()
    )
    monkeypatch.setenv("CODENIB_WIKI_MEDIA_MODEL", "openai/image-1")
    monkeypatch.setenv("CODENIB_WIKI_MEDIA_API_BASE", "https://images.example/v1")
    monkeypatch.setenv("CODENIB_WIKI_MEDIA_API_KEY", "secret")
    monkeypatch.setenv(
        "CODENIB_WIKI_MEDIA_OPTIONS",
        '{"provider":"openai","timeout":30}',
    )

    config = load_config(str(config_path))

    assert config.wiki_media_generation_enabled is True
    assert config.wiki_media_model == "openai/image-1"
    assert config.wiki_media_api_base == "https://images.example/v1"
    assert config.wiki_media_api_key == "secret"
    assert config.wiki_media_options == {
        "provider": "openai",
        "timeout": 30,
    }


def test_wiki_visual_facts_config_is_disabled_by_default(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("wiki_agent: false\n")

    config = load_config(str(config_path))

    assert config.wiki_visual_fact_extraction_enabled is False
    assert config.wiki_visual_facts_options == {}


def test_wiki_visual_facts_config_rejects_truthy_string(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text('wiki_visual_facts_enabled: "false"\n')

    with pytest.raises(ValueError, match="must be a boolean"):
        load_config(str(config_path))


def test_wiki_visual_facts_environment_rejects_invalid_boolean(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("wiki_agent: false\n")
    monkeypatch.setenv("CODENIB_WIKI_VISUAL_FACTS_ENABLED", "ture")

    with pytest.raises(ValueError, match="CODENIB_WIKI_VISUAL_FACTS_ENABLED"):
        load_config(str(config_path))


def test_wiki_visual_facts_environment_overrides_file_config(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
wiki_visual_facts_enabled: false
wiki_visual_facts_model: file-model
wiki_visual_facts_options:
  provider: file-provider
""".lstrip()
    )
    monkeypatch.setenv("CODENIB_WIKI_VISUAL_FACTS_ENABLED", "true")
    monkeypatch.setenv("CODENIB_WIKI_VISUAL_FACTS_MODEL", "qwen-vl")
    monkeypatch.setenv("CODENIB_WIKI_VISUAL_FACTS_API_BASE", "https://vlm.example/v1")
    monkeypatch.setenv("CODENIB_WIKI_VISUAL_FACTS_API_KEY", "secret")
    monkeypatch.setenv(
        "CODENIB_WIKI_VISUAL_FACTS_OPTIONS",
        '{"provider":"local-vlm","timeout":45}',
    )

    config = load_config(str(config_path))

    assert config.wiki_visual_fact_extraction_enabled is True
    assert config.wiki_visual_facts_model == "qwen-vl"
    assert config.wiki_visual_facts_api_base == "https://vlm.example/v1"
    assert config.wiki_visual_facts_api_key == "secret"
    assert config.wiki_visual_facts_options == {
        "provider": "local-vlm",
        "timeout": 45,
    }


def test_config_profile_extends_relative_base_and_merges_options(
    tmp_path: Path,
) -> None:
    base = tmp_path / "base.yaml"
    base.write_text(
        "model: openai/local\n"
        "model_api_base: http://127.0.0.1:8080/v1\n"
        "model_api_key: local-placeholder\n"
        "mode: hybrid\n"
        "model_options:\n"
        "  timeout: 90\n"
        "  extra_body:\n"
        "    reasoning:\n"
        "      enabled: true\n"
    )
    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    api = profile_dir / "api.yaml"
    api.write_text(
        "extends: ../base.yaml\n"
        "model: deepseek/deepseek-v4-flash\n"
        "model_api_base: https://api.deepseek.com\n"
        "model_api_key: null\n"
        "model_options:\n"
        "  timeout: 30\n"
        "  extra_body:\n"
        "    thinking:\n"
        "      type: disabled\n"
    )

    config = load_config(str(api))

    assert config.model == "deepseek/deepseek-v4-flash"
    assert config.model_api_base == "https://api.deepseek.com"
    assert config.model_api_key is None
    assert config.mode == "hybrid"
    assert config.model_options == {
        "timeout": 30,
        "extra_body": {
            "reasoning": {"enabled": True},
            "thinking": {"type": "disabled"},
        },
    }


def test_config_profile_supports_ordered_parent_layers(tmp_path: Path) -> None:
    base = tmp_path / "base.yaml"
    base.write_text("model: base\nmax_tokens: 1024\n")
    service = tmp_path / "service.yaml"
    service.write_text("max_tokens: 2048\nmode: hybrid\n")
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        "extends:\n" "  - base.yaml\n" "  - service.yaml\n" "model: api\n"
    )

    config = load_config(str(profile))

    assert config.model == "api"
    assert config.max_tokens == 2048
    assert config.mode == "hybrid"


def test_config_profile_environment_still_has_highest_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = tmp_path / "base.yaml"
    base.write_text("model: base\n")
    profile = tmp_path / "profile.yaml"
    profile.write_text("extends: base.yaml\nmodel: profile\n")
    monkeypatch.setenv("CODENIB_DEMO_MODEL", "environment")

    assert load_config(str(profile)).model == "environment"


def test_config_profile_rejects_extends_cycle(tmp_path: Path) -> None:
    first = tmp_path / "first.yaml"
    second = tmp_path / "second.yaml"
    first.write_text("extends: second.yaml\n")
    second.write_text("extends: first.yaml\n")

    with pytest.raises(ValueError, match="extends cycle"):
        load_config(str(first))


def test_config_profile_reports_missing_parent(tmp_path: Path) -> None:
    profile = tmp_path / "profile.yaml"
    profile.write_text("extends: missing.yaml\n")

    with pytest.raises(FileNotFoundError, match="parent does not exist"):
        load_config(str(profile))
