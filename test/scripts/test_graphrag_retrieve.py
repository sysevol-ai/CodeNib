# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the GraphRAG retrieval ablation runner."""

from __future__ import annotations

from types import SimpleNamespace

from scripts.retrieval_ablation import graphrag_retrieve


def test_actionable_nodes_require_line_spans():
    nodes = [
        SimpleNamespace(node_id="a.py:a()", start_line=1, end_line=2),
        SimpleNamespace(node_id="b.py:b()", start_line=None, end_line=3),
        SimpleNamespace(node_id="c.py:c()", start_line=4, end_line=None),
    ]

    assert graphrag_retrieve._actionable_nodes(nodes) == [nodes[0]]


def test_retrieve_loads_contexts_from_prebuilt_repo_not_staged_cache(
    monkeypatch, tmp_path
):
    iid = "repo__proj-1"
    prebuilt = tmp_path / "prebuilt"
    cache_root = tmp_path / "cache"
    repo = prebuilt / iid / "repo"
    repo.mkdir(parents=True)

    calls = {}

    def fake_load_dataset_rows(cfg):
        return (
            {
                iid: {
                    "instance_id": iid,
                    "problem_statement": "where is target?",
                    "language_group": "Python",
                }
            },
            {iid: {"gt_files": ["target.py"]}},
        )

    def fake_stage_prebuilt_indexes(prebuilt_dir, instance_id, cache_dir):
        calls["stage"] = (prebuilt_dir, instance_id, cache_dir)
        return cache_dir

    def fake_load_full_contexts(cfg, repo_path, cache_dir):
        calls["load_full_contexts"] = (repo_path, cache_dir)

    class FakeRegistry:
        def get(self, skill_id):
            assert skill_id == "codeminer_context"
            return SimpleNamespace(
                executor_fn=lambda query, seeds, max_results: [
                    SimpleNamespace(
                        file="target.py",
                        node_id="target.py:target()",
                        node_name="target.py:target()",
                        start_line=9,
                        end_line=19,
                        score=1.0,
                    )
                ]
            )

    monkeypatch.setattr(
        "scripts.agent_compile.lib.harness.load_dataset_rows",
        fake_load_dataset_rows,
    )
    monkeypatch.setattr(
        "scripts.agent_compile.lib.prebuilt.stage_prebuilt_indexes",
        fake_stage_prebuilt_indexes,
    )
    monkeypatch.setattr(
        "scripts.agent_compile.lib.harness.load_full_contexts",
        fake_load_full_contexts,
    )
    monkeypatch.setattr(
        "codeminer.eval.retrieval_eval.collect_targets",
        lambda meta, simplified_symbols=True: (["target.py"], []),
    )
    monkeypatch.setattr(
        "codeminer.eval.retrieval_eval.collect_target_blocks",
        lambda meta: [{"file": "target.py", "start": 10, "end": 20}],
    )
    monkeypatch.setattr("codeminer.agent.skills.registry.SkillRegistry", FakeRegistry)

    (
        nodes,
        ranked,
        targets,
        target_symbols,
        target_blocks,
        lang,
    ) = graphrag_retrieve._retrieve(
        iid, SimpleNamespace(), str(prebuilt), str(cache_root)
    )

    assert calls["stage"] == (str(prebuilt), iid, str(cache_root / iid))
    assert calls["load_full_contexts"] == (str(repo), str(cache_root / iid))
    assert len(nodes) == 1
    assert ranked == ["target.py"]
    assert targets == ["target.py"]
    assert target_symbols == []
    assert target_blocks == [{"file": "target.py", "start": 10, "end": 20}]
    assert lang == "Python"
