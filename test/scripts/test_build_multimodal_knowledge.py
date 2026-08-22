# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import subprocess
import sys

from scripts.build_multimodal_knowledge import build_parser


def test_build_multimodal_knowledge_script_writes_bundle(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "docs").mkdir()
    (repo / "src").mkdir()
    (repo / "docs" / "architecture.svg").write_text(
        "<svg>WikiService</svg>",
        encoding="utf-8",
    )
    (repo / "README.md").write_text(
        "![WikiService architecture](docs/architecture.svg)",
        encoding="utf-8",
    )
    (repo / "src" / "wiki.py").write_text(
        "class WikiService: pass",
        encoding="utf-8",
    )
    generated = repo / "generated"
    generated.mkdir()
    (generated / "ignored.png").write_bytes(b"png")
    output = tmp_path / "bundle.json"

    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "scripts/build_multimodal_knowledge.py",
            str(repo),
            "--output",
            str(output),
            "--commit",
            "abc123",
            "--exclude-root",
            str(generated),
        ],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )

    counts = json.loads(completed.stdout)
    bundle = json.loads(output.read_text(encoding="utf-8"))
    assert counts["media_artifacts"] == 1
    assert counts["knowledge_entries"] == 1
    assert bundle["media_manifest"]["commit"] == "abc123"
    assert bundle["knowledge_view"]["entry_count"] == 1


def test_build_multimodal_knowledge_script_rejects_missing_repository(tmp_path):
    output = tmp_path / "bundle.json"

    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "scripts/build_multimodal_knowledge.py",
            str(tmp_path / "missing"),
            "--output",
            str(output),
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    assert completed.returncode != 0
    assert "repository root does not exist" in completed.stderr
    assert not output.exists()


def test_build_multimodal_knowledge_parser_accepts_vlm_options(tmp_path):
    output = tmp_path / "bundle.json"

    args = build_parser().parse_args(
        [
            str(tmp_path),
            "--output",
            str(output),
            "--visual-facts-model",
            "qwen-vl",
            "--visual-facts-api-base",
            "https://vlm.example/v1",
            "--visual-facts-api-key-env",
            "TEST_VLM_KEY",
            "--visual-facts-provider",
            "qwen",
            "--visual-facts-timeout",
            "15",
        ]
    )

    assert args.visual_facts_model == "qwen-vl"
    assert args.visual_facts_api_base == "https://vlm.example/v1"
    assert args.visual_facts_api_key_env == "TEST_VLM_KEY"
    assert args.visual_facts_provider == "qwen"
    assert args.visual_facts_timeout == 15
