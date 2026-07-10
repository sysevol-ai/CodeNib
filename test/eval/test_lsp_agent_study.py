# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from codeminer.agent.lsp_provider import StaticLSPProvider
from codeminer.compiler.snapshot_store import SourceSnapshot
from codeminer.eval.agent_runner.lsp_agent_study import (
    CODEMINER_LSP_ARM,
    FILESYSTEM_ARM,
    LIVE_LSP_ARM,
    LSPAgentStudySpec,
    LSPAgentStudyTask,
    run_lsp_agent_study_arm,
    study_arm_order,
)
from codeminer.eval.agent_runner.lsp_agent_study_analysis import (
    analyze_lsp_agent_noninferiority,
    collect_live_lsp_replay_bundles,
    summarize_lsp_agent_study,
)
from codeminer.eval.agent_runner.lsp_agent_study_artifacts import (
    find_source_repository,
    lsp_agent_artifact_profile,
    requires_lsp_occurrence_index,
    static_native_backend_for_language,
)
from codeminer.eval.agent_runner.lsp_agent_study_manifest import (
    build_base_lsp_agent_manifest,
    task_from_manifest_subject,
)
from codeminer.graph.code_graph import _SCHEMA_VERSION, CodeGraph
from codeminer.llm.litellm_chat import LiteLLMChat
from codeminer.scip_interface.lsp_occurrence_index import (
    LSP_OCCURRENCE_INDEX_SCHEMA_VERSION,
)
from codeminer.types import NODE_TYPE_FUNCTION


def _base_row(instance_id, repo, language_group="Go"):
    return {
        "instance_id": instance_id,
        "repo": repo,
        "base_commit": "a" * 40,
        "language_group": language_group,
        "problem_statement": "Locate the configuration loader.",
        "gt_target_files": ["callee.py"],
        "gt_symbols_modified": ["callee.py:load_config()"],
        "gt_symbols_deleted": [],
        "gt_code_blocks": [
            {
                "file_path": "callee.py",
                "start_line": 5,
                "end_line": 9,
                "symbol": "load_config()",
                "change_type": "modified",
            }
        ],
    }


def _graph(tmp_path):
    (tmp_path / "caller.py").write_text(
        "def run():\n    return load_config()\n", encoding="utf-8"
    )
    (tmp_path / "callee.py").write_text(
        "\n\n\n\ndef load_config():\n    return {}\n", encoding="utf-8"
    )
    graph = CodeGraph(project_root=str(tmp_path))
    graph.add_file_node("caller.py")
    graph.add_symbol_node(
        "caller.run",
        line=0,
        scope_start_line=0,
        scope_end_line=1,
        symbol_type=NODE_TYPE_FUNCTION,
    )
    graph.update_current_scope("caller.run", start_line=0, end_line=1)
    graph.add_symbol_reference(
        "callee.load_config",
        module_path="callee.py",
        symbol_type=NODE_TYPE_FUNCTION,
        anchor_file="caller.py",
        anchor_line=1,
    )
    graph.add_file_node("callee.py")
    graph.add_symbol_node(
        "callee.load_config",
        line=4,
        scope_start_line=4,
        scope_end_line=5,
        symbol_type=NODE_TYPE_FUNCTION,
    )
    graph.graph.vs[graph.name_to_vertex["callee.load_config"]][
        "unified_name"
    ] = "callee.py:load_config()"
    graph.build_range_indexes()
    return graph


def _response(*, content=None, tool_calls=None):
    message = SimpleNamespace(role="assistant", content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def test_study_artifact_profile_pins_graph_compatibility():
    profile = lsp_agent_artifact_profile("typescript")

    assert profile.languages == ("javascript", "typescript")
    assert profile.schema_versions == {
        "code_graph": str(_SCHEMA_VERSION),
        "lsp_occurrence_index": str(LSP_OCCURRENCE_INDEX_SCHEMA_VERSION),
    }
    assert profile.options == {"exclude_patterns": [], "graph_route": "active"}

    cpp_profile = lsp_agent_artifact_profile("cpp")
    assert cpp_profile.languages == ("cpp",)
    assert cpp_profile.schema_versions == {"code_graph": str(_SCHEMA_VERSION)}
    assert cpp_profile.options["static_native_backend"] == ("symbol_graph_position_v1")
    assert requires_lsp_occurrence_index("python") is True
    assert requires_lsp_occurrence_index("cpp") is False
    assert static_native_backend_for_language("cpp") == "symbol_graph_position_v1"


def test_find_source_repository_uses_clone_containing_commit(tmp_path):
    repo = tmp_path / "org__repo-1" / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "source.go").write_text("package demo\n", encoding="utf-8")
    subprocess.run(["git", "add", "source.go"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "fixture"], cwd=repo, check=True)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    found = find_source_repository(tmp_path, repo="org/repo", commit=commit)

    assert found == repo


def _definition_call():
    return SimpleNamespace(
        id="call_1",
        type="function",
        function=SimpleNamespace(
            name="lsp_definition",
            arguments=(
                '{"file_path":"caller.py","line":2,' '"character":15,"top_k":8}'
            ),
        ),
    )


def _study_task(tmp_path):
    return LSPAgentStudyTask.from_base_row(
        _base_row("org__repo-1", "org/repo"), repo_path=tmp_path
    )


def test_manifest_uses_all_supported_rows_and_repo_disjoint_roles(tmp_path):
    rows = [
        _base_row("dev-1", "org/dev"),
        _base_row("test-1", "org/test", language_group="Rust"),
        _base_row("python-1", "org/python", language_group="Python"),
    ]
    spec = LSPAgentStudySpec(development_repositories=("org/dev",), reps=2)

    first = build_base_lsp_agent_manifest(
        rows,
        spec=spec,
        dataset_fingerprint="dataset-fingerprint",
        prebuilt_root=tmp_path,
    )
    second = build_base_lsp_agent_manifest(
        reversed(rows),
        spec=spec,
        dataset_fingerprint="dataset-fingerprint",
        prebuilt_root=tmp_path,
    )

    assert first["manifest_sha256"] == second["manifest_sha256"]
    assert first["schema_version"] == 3
    assert first["summary"]["subject_count"] == 3
    assert first["summary"]["by_role"] == {
        "confirmatory": 1,
        "development": 1,
        "language_extension": 1,
    }
    dev_roles = {row["role"] for row in first["subjects"] if row["repo"] == "org/dev"}
    assert dev_roles == {"development"}
    assert first["summary"]["artifact_status"] == {"missing_repo": 3}


def test_arm_order_is_reproducible_and_contains_each_arm_once():
    first = study_arm_order("org__repo-1", 1)

    assert first == study_arm_order("org__repo-1", 1)
    assert set(first) == {FILESYSTEM_ARM, LIVE_LSP_ARM, CODEMINER_LSP_ARM}
    assert len(first) == 3


def test_manifest_subject_binding_rechecks_source_and_graph_identity(tmp_path):
    repo = tmp_path / "source"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "source.go").write_text("package demo\n", encoding="utf-8")
    subprocess.run(["git", "add", "source.go"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "fixture"], cwd=repo, check=True)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    row = _base_row("org__repo-1", "org/repo")
    row["base_commit"] = commit
    snapshot = SourceSnapshot(row["repo"], commit)
    profile = lsp_agent_artifact_profile("go")
    snapshot_dir = tmp_path / ".snapshots" / snapshot.snapshot_id
    profile_dir = snapshot_dir / "profiles" / profile.profile_id
    profile_dir.mkdir(parents=True)
    (snapshot_dir / "snapshot.json").write_text(
        json.dumps(snapshot.payload()), encoding="utf-8"
    )
    (profile_dir / "profile.json").write_text(
        json.dumps(profile.payload()), encoding="utf-8"
    )
    repo.rename(profile_dir / "repo")
    repo = profile_dir / "repo"
    (profile_dir / "graph.pkl").write_bytes(b"graph-v1")
    (profile_dir / "lsp_index.pkl").write_bytes(b"lsp-v1")
    instance = tmp_path / "org__repo-1"
    instance.symlink_to(profile_dir, target_is_directory=True)
    spec = LSPAgentStudySpec(development_repositories=("org/repo",))
    manifest = build_base_lsp_agent_manifest([row], spec=spec, prebuilt_root=tmp_path)
    subject = manifest["subjects"][0]

    task = task_from_manifest_subject(row, subject, repo_path=repo)

    assert task.role == "development"
    assert subject["artifact"]["status"] == "ready"
    (profile_dir / "graph.pkl").write_bytes(b"graph-v2")
    with pytest.raises(ValueError, match="graph artifact changed"):
        task_from_manifest_subject(row, subject, repo_path=repo)


def test_dynamic_lsp_arms_hold_prompt_and_tool_schema_constant(tmp_path):
    graph = _graph(tmp_path)
    task = _study_task(tmp_path)
    rows = []
    for arm in (CODEMINER_LSP_ARM, LIVE_LSP_ARM):
        llm = MagicMock(spec=LiteLLMChat)
        llm.model = "fake-haiku"
        llm.temperature = 0
        llm.max_tokens = 512
        llm._call_raw.side_effect = [
            _response(tool_calls=[_definition_call()]),
            _response(
                content=(
                    "Files: callee.py\n"
                    "Symbols: callee.py:load_config()\n"
                    "Locations: callee.py:5-6"
                )
            ),
        ]
        rows.append(
            run_lsp_agent_study_arm(
                task,
                arm=arm,
                graph=graph,
                llm=llm,
                provider=StaticLSPProvider(graph, snapshot_id=arm),
                max_turns=3,
            )
        )

    assert rows[0]["tool_schema_sha256"] == rows[1]["tool_schema_sha256"]
    assert rows[0]["system_prompt_sha256"] == rows[1]["system_prompt_sha256"]
    assert rows[0]["lsp_call_count"] == rows[1]["lsp_call_count"] == 1
    assert rows[0]["lsp_requests"][0]["arguments"]["line"] == 1
    assert "symbol" not in rows[0]["lsp_requests"][0]["arguments"]

    summary = summarize_lsp_agent_study(rows)
    assert summary["primary_pair_count"] == 1
    assert summary["primary_pair_schema_match_count"] == 1
    assert summary["frozen_live_trace_request_count"] == 1


def test_filesystem_arm_exposes_no_lsp_skill(tmp_path):
    graph = _graph(tmp_path)
    task = _study_task(tmp_path)
    llm = MagicMock(spec=LiteLLMChat)
    llm.model = "fake-haiku"
    llm.temperature = 0
    llm.max_tokens = 512
    llm._call_raw.return_value = _response(
        content=(
            "Files: callee.py\n"
            "Symbols: callee.py:load_config()\n"
            "Locations: callee.py:5-6"
        )
    )

    row = run_lsp_agent_study_arm(
        task,
        arm=FILESYSTEM_ARM,
        graph=graph,
        llm=llm,
        max_turns=1,
    )

    assert row["lsp_call_count"] == 0
    assert row["lsp_requests"] == []


def test_noninferiority_uses_complete_repository_clustered_pairs():
    cells = []
    for repo in ("org/one", "org/two"):
        for rep in (1, 2):
            for arm, recall in (
                (CODEMINER_LSP_ARM, 0.8),
                (LIVE_LSP_ARM, 0.75),
            ):
                cells.append(
                    {
                        "instance_id": f"{repo}-task",
                        "repo": repo,
                        "role": "confirmatory",
                        "rep": rep,
                        "arm": arm,
                        "tool_schema_sha256": "schema",
                        "system_prompt_sha256": "prompt",
                        "metrics": {
                            "answer_blocks": {5: {"recall": recall}},
                        },
                    }
                )

    result = analyze_lsp_agent_noninferiority(
        cells,
        expected_pair_count=4,
        bootstrap_samples=100,
        seed=7,
    )

    assert result["analysis_ready"] is True
    assert result["noninferior"] is True
    assert result["repository_count"] == 2
    assert result["point_delta"] == pytest.approx(0.05)

    incomplete = analyze_lsp_agent_noninferiority(
        cells,
        expected_pair_count=5,
        bootstrap_samples=10,
    )
    assert incomplete["analysis_ready"] is False
    assert incomplete["noninferior"] is False


def test_noninferiority_bounds_keep_missing_live_metric_in_denominator():
    cells = [
        {
            "instance_id": "org/repo-task",
            "repo": "org/repo",
            "role": "confirmatory",
            "rep": 1,
            "arm": CODEMINER_LSP_ARM,
            "metrics": {"answer_blocks": {5: {"recall": 0.25}}},
        },
        {
            "instance_id": "org/repo-task",
            "repo": "org/repo",
            "role": "confirmatory",
            "rep": 1,
            "arm": LIVE_LSP_ARM,
            "status": "error",
        },
    ]

    result = analyze_lsp_agent_noninferiority(
        cells,
        expected_pair_count=1,
        bootstrap_samples=10,
    )

    assert result["analysis_ready"] is False
    bounds = result["partial_identification"]
    assert bounds["bounds_ready"] is True
    assert bounds["missing_live_metric_pair_count"] == 1
    assert bounds["point_delta_bounds"] == {"lower": -0.75, "upper": 0.25}


def test_collect_live_lsp_replay_bundles_preserves_every_request():
    cells = [
        {
            "instance_id": "org__repo-1",
            "repo": "org/repo",
            "base_commit": "abc",
            "language": "go",
            "snapshot_id": "snapshot",
            "role": "development",
            "rep": 2,
            "arm": LIVE_LSP_ARM,
            "lsp_requests": [
                {
                    "capability": "definition",
                    "arguments": {
                        "file_path": "main.go",
                        "line": 3,
                        "character": 4,
                    },
                    "request_id": "request-1",
                },
                {
                    "capability": "references",
                    "arguments": {
                        "file_path": "main.go",
                        "line": 3,
                        "character": 4,
                    },
                    "request_id": "request-2",
                },
            ],
        },
        {
            "instance_id": "org__repo-1",
            "repo": "org/repo",
            "base_commit": "abc",
            "language": "go",
            "snapshot_id": "snapshot",
            "role": "development",
            "rep": 3,
            "arm": CODEMINER_LSP_ARM,
            "lsp_requests": [{"request_id": "excluded-static-request"}],
        },
    ]

    bundles = collect_live_lsp_replay_bundles(cells)

    assert len(bundles) == 1
    assert [row["request_id"] for row in bundles[0]["requests"]] == [
        "request-1",
        "request-2",
    ]
    assert bundles[0]["requests"][0]["source_cell"] == {
        "role": "development",
        "rep": 2,
        "arm": LIVE_LSP_ARM,
    }
