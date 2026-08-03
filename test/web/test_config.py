# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

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
