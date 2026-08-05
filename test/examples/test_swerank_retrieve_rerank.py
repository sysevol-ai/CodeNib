# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the runnable SweRank recipe."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

_RECIPE_PATH = (
    Path(__file__).resolve().parents[2] / "examples" / "swerank_retrieve_rerank.py"
)


def _load_recipe():
    spec = importlib.util.spec_from_file_location(
        "swerank_retrieve_rerank_under_test", _RECIPE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def recipe():
    return _load_recipe()


def _args(recipe, *extra):
    return recipe.parse_args([".", "--query", "fix nested configuration", *extra])


def test_llm_recipe_uses_rankgpt_contract(recipe, tmp_path):
    args = _args(recipe)
    kwargs = recipe.build_pipeline_kwargs(
        args,
        repo=tmp_path,
        index_dir=tmp_path / "index",
        languages=["python"],
    )

    assert kwargs["embedding_model"] == "Salesforce/SweRankEmbed-Small"
    assert kwargs["retrieval_plan"][0].top_k == 100
    assert kwargs["rerank_candidate_top_k"] == 30
    assert kwargs["rerank_strategy"] == "llm"
    assert kwargs["rerank_model"] == "openai/swerank-llm-small"
    assert kwargs["rerank_listwise_format"] == "rankgpt"
    assert kwargs["rerank_window_size"] == 10
    assert kwargs["rerank_window_step"] == 10


def test_embedding_recipe_uses_independent_large_encoder(recipe, tmp_path):
    args = _args(
        recipe,
        "--reranker",
        "embed-large",
        "--device",
        "cuda:1",
        "--rerank-batch-size",
        "4",
    )
    kwargs = recipe.build_pipeline_kwargs(
        args,
        repo=tmp_path,
        index_dir=tmp_path / "index",
        languages=["python", "go"],
    )

    assert kwargs["rerank_strategy"] == "embedding"
    assert kwargs["rerank_embedding_model"] == "Salesforce/SweRankEmbed-Large"
    assert kwargs["rerank_embedding_dimension"] == 3584
    assert kwargs["rerank_embedding_model_kwargs"] == {
        "trust_remote_code": True,
        "model_kwargs": {"device": "cuda:1"},
        "encode_kwargs": {"batch_size": 4, "normalize_embeddings": True},
    }
    assert kwargs["embedding_model_kwargs"]["encode_kwargs"] == {
        "batch_size": 32,
        "normalize_embeddings": True,
    }
    assert kwargs["languages"] == ["python", "go"]


def test_default_index_cache_is_keyed_by_clean_commit(recipe, monkeypatch, tmp_path):
    from codenib import paths, source_fingerprint
    from codenib.compiler import checkout_identity

    state = tmp_path / "state"
    monkeypatch.setattr(paths, "repo_state_dir", lambda repo: state)
    monkeypatch.setattr(checkout_identity, "checkout_commit", lambda repo: "A" * 40)
    monkeypatch.setattr(
        source_fingerprint, "repository_source_is_dirty", lambda repo: False
    )
    monkeypatch.setattr(
        source_fingerprint,
        "fingerprint_repository",
        lambda repo: pytest.fail("clean commits should not require content hashing"),
    )

    index_dir = recipe.resolve_index_dir(_args(recipe), tmp_path)

    assert index_dir == (state / "recipes" / "swerank-embed-small" / f"git-{'a' * 40}")


def test_default_index_cache_fingerprints_dirty_checkout(recipe, monkeypatch, tmp_path):
    from codenib import paths, source_fingerprint
    from codenib.compiler import checkout_identity

    state = tmp_path / "state"
    monkeypatch.setattr(paths, "repo_state_dir", lambda repo: state)
    monkeypatch.setattr(checkout_identity, "checkout_commit", lambda repo: "a" * 40)
    monkeypatch.setattr(
        source_fingerprint, "repository_source_is_dirty", lambda repo: True
    )
    monkeypatch.setattr(
        source_fingerprint,
        "fingerprint_repository",
        lambda repo: source_fingerprint.SourceFingerprint(
            value=f"sha256:{'b' * 64}", file_count=3
        ),
    )

    index_dir = recipe.resolve_index_dir(_args(recipe), tmp_path)

    assert index_dir == (
        state / "recipes" / "swerank-embed-small" / f"source-{'b' * 64}"
    )


@pytest.mark.parametrize(
    "extra",
    [
        ("--top-k", "0"),
        ("--top-k", "20", "--candidate-top-k", "10"),
        ("--candidate-top-k", "101"),
    ],
)
def test_candidate_funnel_is_validated_before_model_loading(recipe, extra):
    with pytest.raises(SystemExit) as exc_info:
        _args(recipe, *extra)
    assert exc_info.value.code == 2


def test_render_results_reports_one_based_source_location(recipe, capsys):
    result = SimpleNamespace(
        file="src/config.py",
        start_line=40,
        end_line=44,
        node_name="Config.reload",
        type="method",
        score=0.875,
        content="def reload(self):\n    return self._read()",
    )

    recipe.render_results([result], snippet_lines=1, as_json=False)

    output = capsys.readouterr().out
    assert "[1] src/config.py:41  Config.reload score=0.8750" in output
    assert "    def reload(self):" in output


def test_render_results_makes_checkout_paths_repository_relative(
    recipe, capsys, tmp_path
):
    result = SimpleNamespace(
        file=str(tmp_path / "src" / "config.py"),
        start_line=0,
        end_line=1,
        node_name="load",
        type="function",
        score=0.5,
        content=None,
    )

    recipe.render_results([result], snippet_lines=0, as_json=False, repo=tmp_path)

    assert "[1] src/config.py:1  load score=0.5000" in capsys.readouterr().out


def test_run_wires_repository_query_and_closes_pipeline(
    recipe, monkeypatch, tmp_path, capsys
):
    import codenib.model

    observed = {}

    class FakePipeline:
        def __init__(self, **kwargs):
            observed["kwargs"] = kwargs

        def query(self, query, top_k):
            observed["query"] = query
            observed["top_k"] = top_k
            return [
                SimpleNamespace(
                    file="src/config.py",
                    start_line=2,
                    end_line=5,
                    node_name="reload",
                    type="function",
                    score=0.9,
                    content="def reload(): pass",
                )
            ]

        def close(self):
            observed["closed"] = True

    monkeypatch.setattr(codenib.model, "RetrieveRerankPipeline", FakePipeline)
    monkeypatch.setattr(recipe, "resolve_languages", lambda repo, requested: ["python"])
    monkeypatch.setattr(
        recipe, "resolve_index_dir", lambda args, repo: tmp_path / "index"
    )

    args = recipe.parse_args(
        [str(tmp_path), "--query", "  fix nested configuration  ", "--top-k", "1"]
    )
    assert recipe.run(args) == 0

    assert observed["query"] == "fix nested configuration"
    assert observed["top_k"] == 1
    assert observed["kwargs"]["repo_path"] == str(tmp_path)
    assert observed["closed"] is True
    assert "[1] src/config.py:3  reload score=0.9000" in capsys.readouterr().out
