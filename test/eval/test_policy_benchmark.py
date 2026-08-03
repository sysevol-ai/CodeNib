# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the paired external-policy benchmark entry point."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

import codenib.eval.benchmarks.policy_benchmark as policy_benchmark
from codenib.eval.benchmarks.policy_benchmark import (
    _activate_orcaloca_checkout,
    _close_provider,
    _locagent_predictions,
    _run_locagent_loop,
    main,
)
from codenib.eval.benchmarks.policy_compat import (
    PolicyBenchmarkCase,
    policy_result_path,
    write_json_atomic,
)


def _write_cases(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "dataset": "fixture",
                "cases": [
                    {
                        "instance_id": "demo__repo-1",
                        "repo": "demo/repo",
                        "base_commit": "a" * 40,
                        "problem_statement": "Locate the parser",
                        "repo_path": "repo",
                        "manifest_path": "manifest.json",
                        "ground_truth": {
                            "gold_files": ["src/parser.py"],
                            "gold_functions": ["src/parser.py::parse"],
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def _write_result(results: Path, *, provider: str, model: str = "test-model") -> None:
    write_json_atomic(
        policy_result_path(
            results,
            instance_id="demo__repo-1",
            agent="orcaloca",
            provider=provider,
        ),
        {
            "agent": "orcaloca",
            "provider": provider,
            "instance_id": "demo__repo-1",
            "model": model,
            "usage": {"total_tokens_estimated": 100 if provider == "native" else 80},
            "locations": [
                {
                    "file_path": "src/parser.py",
                    "method_name": "parse",
                }
            ],
        },
    )


def _write_locagent_result(
    results: Path,
    *,
    provider: str,
    model: str = "test-model",
) -> None:
    write_json_atomic(
        policy_result_path(
            results,
            instance_id="demo__repo-1",
            agent="locagent",
            provider=provider,
        ),
        {
            "agent": "locagent",
            "provider": provider,
            "instance_id": "demo__repo-1",
            "model": model,
            "usage": {"total_tokens": 100 if provider == "native" else 80},
            "final_output": "src/parser.py\nFunctions: parse\n",
        },
    )


def test_score_locagent_uses_common_ranked_metrics_and_strict_denominator(
    tmp_path: Path,
) -> None:
    cases = tmp_path / "cases.json"
    results = tmp_path / "results"
    output = tmp_path / "summary.json"
    repo = tmp_path / "repo" / "src"
    repo.mkdir(parents=True)
    (repo / "parser.py").write_text("def parse():\n    pass\n", encoding="utf-8")
    _write_cases(cases)
    payload = json.loads(cases.read_text(encoding="utf-8"))
    payload["cases"][0]["repo_path"] = "repo"
    cases.write_text(json.dumps(payload), encoding="utf-8")
    _write_locagent_result(results, provider="native")
    base_args = [
        "score-locagent",
        "--cases",
        str(cases),
        "--results-dir",
        str(results),
        "--output",
        str(output),
    ]

    assert main(base_args) == 1
    assert main([*base_args, "--allow-incomplete"]) == 0
    partial = json.loads(output.read_text(encoding="utf-8"))
    assert partial["coverage"]["expected_cell_count"] == 2
    by_provider = {row["provider"]: row for row in partial["summary"]}
    assert by_provider["native"]["file_metrics"]["1"]["accuracy"] == 1.0
    assert by_provider["native"]["function_metrics"]["1"]["accuracy"] == 1.0
    assert by_provider["codenib"]["failed_count"] == 1
    assert by_provider["codenib"]["file_metrics"]["1"]["accuracy"] == 0.0

    _write_locagent_result(results, provider="codenib")
    assert main(base_args) == 0
    complete = json.loads(output.read_text(encoding="utf-8"))
    assert complete["coverage"]["paired_successful_count"] == 1
    assert complete["paired_summary"]["n"] == 1
    assert complete["paired_summary"][
        "native_to_codenib_total_tokens_ratio_median"
    ] == pytest.approx(1.25)


def test_locagent_predictions_score_repository_files_but_only_named_functions(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "Dockerfile").write_text("FROM python:3.12\n", encoding="utf-8")
    (repo / "setup.cfg").write_text("[metadata]\n", encoding="utf-8")
    (repo / "src" / "model.py").write_text(
        "class Widget:\n    def save(self):\n        pass\n",
        encoding="utf-8",
    )
    case = PolicyBenchmarkCase(
        instance_id="demo__repo-1",
        problem_statement="Locate the configuration and save method",
        repo_path=repo,
        base_commit="a" * 40,
        manifest_path=tmp_path / "manifest.json",
    )

    files, functions = _locagent_predictions(
        case,
        {
            "final_output": (
                "Dockerfile\n"
                "setup.cfg\n"
                "src/model.py\n"
                "Class: Widget\n"
                "src/model.py\n"
                "Class: Widget\n"
                "Functions: save\n"
            )
        },
    )

    assert files == ("Dockerfile", "setup.cfg", "src/model.py")
    assert functions == ("src/model.py:Widget.save",)


def test_score_command_enforces_the_requested_cell_denominator(tmp_path: Path) -> None:
    cases = tmp_path / "cases.json"
    results = tmp_path / "results"
    output = tmp_path / "summary.json"
    _write_cases(cases)
    _write_result(results, provider="native")
    base_args = [
        "score-orcaloca",
        "--cases",
        str(cases),
        "--results-dir",
        str(results),
        "--output",
        str(output),
    ]

    assert main(base_args) == 1
    assert main([*base_args, "--allow-incomplete"]) == 0
    partial = json.loads(output.read_text(encoding="utf-8"))
    assert partial["coverage"]["expected_cell_count"] == 2
    assert partial["coverage"]["observed_cell_count"] == 1
    assert partial["coverage"]["paired_successful_count"] == 0
    partial_summary = {row["provider"]: row for row in partial["summary"]}
    assert partial_summary["native"]["n"] == 1
    assert partial_summary["native"]["file_match_rate"] == 1.0
    assert partial_summary["codenib"]["n"] == 1
    assert partial_summary["codenib"]["file_match_rate"] == 0.0

    _write_result(results, provider="codenib")
    assert main(base_args) == 0
    complete = json.loads(output.read_text(encoding="utf-8"))
    assert complete["coverage"]["observed_cell_count"] == 2
    assert complete["coverage"]["paired_successful_count"] == 1
    assert complete["paired_summary"]["n"] == 1
    assert complete["paired_summary"][
        "native_to_codenib_total_tokens_ratio_median"
    ] == pytest.approx(1.25)
    assert complete["paired_summary"][
        "codenib_total_token_reduction_fraction_median"
    ] == pytest.approx(0.2)
    assert complete["comparison_scope"] == {
        "provider_compatibility": "golden-patch localization outcomes",
        "token_and_time_statistics": "single-trajectory descriptive statistics",
        "performance_inference_supported": False,
        "reason": (
            "The runner preserves the upstream sampling policy and does not "
            "collect repeated trials. Use deterministic tool-output replay for "
            "provider-semantic equivalence."
        ),
    }


def test_score_command_rejects_mixed_model_aggregation(tmp_path: Path) -> None:
    cases = tmp_path / "cases.json"
    results = tmp_path / "results"
    _write_cases(cases)
    _write_result(results, provider="native", model="model-a")
    _write_result(results, provider="codenib", model="model-b")

    with pytest.raises(ValueError, match="mixed models"):
        main(
            [
                "score-orcaloca",
                "--cases",
                str(cases),
                "--results-dir",
                str(results),
            ]
        )


def test_score_command_rejects_invalid_provenance_in_strict_mode(
    tmp_path: Path,
) -> None:
    cases = tmp_path / "cases.json"
    results = tmp_path / "results"
    output = tmp_path / "summary.json"
    _write_cases(cases)
    _write_result(results, provider="native")
    _write_result(results, provider="codenib")
    codenib_path = policy_result_path(
        results,
        instance_id="demo__repo-1",
        agent="orcaloca",
        provider="codenib",
    )
    payload = json.loads(codenib_path.read_text(encoding="utf-8"))
    payload["provenance"] = {
        "schema_version": 99,
        "policy": {"agent": "orcaloca", "provider": "codenib"},
    }
    write_json_atomic(codenib_path, payload)
    base_args = [
        "score-orcaloca",
        "--cases",
        str(cases),
        "--results-dir",
        str(results),
        "--output",
        str(output),
    ]

    assert main(base_args) == 1
    assert main([*base_args, "--allow-incomplete"]) == 0
    summary = json.loads(output.read_text(encoding="utf-8"))
    assert summary["provenance"]["invalid_cells"] == [
        {
            "instance_id": "demo__repo-1",
            "provider": "codenib",
            "reason": "unsupported provenance schema_version",
        }
    ]


def test_score_command_rejects_stale_case_provenance_in_strict_mode(
    tmp_path: Path,
) -> None:
    cases = tmp_path / "cases.json"
    results = tmp_path / "results"
    output = tmp_path / "summary.json"
    _write_cases(cases)
    _write_result(results, provider="native")
    _write_result(results, provider="codenib")
    codenib_path = policy_result_path(
        results,
        instance_id="demo__repo-1",
        agent="orcaloca",
        provider="codenib",
    )
    payload = json.loads(codenib_path.read_text(encoding="utf-8"))
    payload["provenance"] = {
        "schema_version": 1,
        "policy": {"agent": "orcaloca", "provider": "codenib"},
        "cases": {
            "source_sha256": "0" * 64,
            "selection_sha256": "1" * 64,
            "instance_ids": ["demo__repo-1"],
        },
    }
    write_json_atomic(codenib_path, payload)

    assert (
        main(
            [
                "score-orcaloca",
                "--cases",
                str(cases),
                "--results-dir",
                str(results),
                "--output",
                str(output),
            ]
        )
        == 1
    )
    summary = json.loads(output.read_text(encoding="utf-8"))
    assert summary["provenance"]["invalid_cells"][0]["reason"] == (
        "provenance case source digest does not match active cases"
    )


@pytest.mark.parametrize(
    "failure",
    (
        {"error": "TimeoutError: model deadline exceeded"},
        {"success": False},
    ),
)
def test_failed_orcaloca_cell_is_an_all_miss_with_empty_function_labels(
    failure: dict[str, object],
    tmp_path: Path,
) -> None:
    cases = tmp_path / "cases.json"
    results = tmp_path / "results"
    output = tmp_path / "summary.json"
    _write_cases(cases)
    payload = json.loads(cases.read_text(encoding="utf-8"))
    payload["cases"][0]["ground_truth"]["gold_functions"] = []
    cases.write_text(json.dumps(payload), encoding="utf-8")
    _write_result(results, provider="codenib")
    write_json_atomic(
        policy_result_path(
            results,
            instance_id="demo__repo-1",
            agent="orcaloca",
            provider="native",
        ),
        {
            "agent": "orcaloca",
            "provider": "native",
            "instance_id": "demo__repo-1",
            "model": "test-model",
            **failure,
        },
    )

    assert (
        main(
            [
                "score-orcaloca",
                "--cases",
                str(cases),
                "--results-dir",
                str(results),
                "--output",
                str(output),
                "--allow-incomplete",
            ]
        )
        == 0
    )
    summary = json.loads(output.read_text(encoding="utf-8"))
    native = next(row for row in summary["summary"] if row["provider"] == "native")
    assert native["n"] == 1
    assert native["file_match_count"] == 0
    assert native["function_match_count"] == 0
    assert native["function_precision_mean"] == 0.0
    assert summary["paired_summary"]["n"] == 0


def test_locagent_loop_uses_the_vendored_protocol(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    case = PolicyBenchmarkCase.from_mapping(
        {
            "instance_id": "demo__repo-1",
            "problem_statement": "Locate the parser",
            "repo_path": str(repo),
            "base_commit": "a" * 40,
            "manifest_path": str(tmp_path / "manifest.json"),
        }
    )
    captured = {}

    def build_protocol(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(policy_benchmark, "build_locagent_protocol", build_protocol)
    monkeypatch.setattr(
        policy_benchmark,
        "run_locagent_policy",
        lambda **_kwargs: SimpleNamespace(
            usage={},
            iterations=1,
            tool_calls=0,
            final_output="",
            tool_trace=(),
            trajectory=(),
        ),
    )
    openai = types.ModuleType("openai")
    openai.OpenAI = lambda **_kwargs: object()
    monkeypatch.setitem(sys.modules, "openai", openai)

    result = _run_locagent_loop(
        case=case,
        dispatch=lambda _name, _arguments: "",
        model="test-model",
        base_url=None,
        max_iterations=2,
    )

    assert captured == {
        "problem_statement": "Locate the parser",
        "package_name": "demo",
    }
    assert result["iterations"] == 1


def test_codenib_locagent_run_needs_no_upstream_checkout_or_python(
    monkeypatch, tmp_path: Path
) -> None:
    from codenib.integrations.locagent import LocAgentToolProvider

    cases = tmp_path / "cases.json"
    output_dir = tmp_path / "results"
    _write_cases(cases)

    class Provider:
        load_seconds = 0.01

        def dispatch(self, _name, _arguments):
            return ""

    monkeypatch.setattr(
        policy_benchmark,
        "inspect_policy_run_preflight",
        lambda *_args, **_kwargs: SimpleNamespace(require_eligible=lambda: None),
    )
    monkeypatch.setattr(
        policy_benchmark,
        "_policy_provenance_by_provider",
        lambda **_kwargs: {"codenib": {"fixture": True}},
    )
    monkeypatch.setattr(
        LocAgentToolProvider,
        "from_manifest",
        lambda _manifest: Provider(),
    )
    monkeypatch.setattr(
        policy_benchmark,
        "_run_locagent_loop",
        lambda **_kwargs: {
            "model": "test-model",
            "elapsed_seconds": 0.1,
            "regions": [],
            "usage": {},
        },
    )

    assert (
        main(
            [
                "locagent",
                "--cases",
                str(cases),
                "--output-dir",
                str(output_dir),
                "--provider",
                "codenib",
                "--model",
                "test-model",
            ]
        )
        == 0
    )
    result = json.loads(
        policy_result_path(
            output_dir,
            instance_id="demo__repo-1",
            agent="locagent",
            provider="codenib",
        ).read_text(encoding="utf-8")
    )
    assert result["provider"] == "codenib"
    assert "error" not in result


def test_provider_cleanup_failure_is_recordable() -> None:
    class FailingProvider:
        def close(self) -> None:
            raise TimeoutError("worker did not stop")

    assert _close_provider(FailingProvider()) == ("TimeoutError: worker did not stop")


def test_locagent_run_writes_the_cell_when_provider_cleanup_fails(
    monkeypatch, tmp_path: Path
) -> None:
    cases = tmp_path / "cases.json"
    output_dir = tmp_path / "results"
    _write_cases(cases)

    class FailingProvider:
        load_seconds = 0.0

        def dispatch(self, _name, _arguments):
            return ""

        def close(self) -> None:
            raise TimeoutError("worker did not stop")

    monkeypatch.setattr(
        policy_benchmark,
        "inspect_policy_run_preflight",
        lambda *_args, **_kwargs: SimpleNamespace(require_eligible=lambda: None),
    )
    monkeypatch.setattr(
        policy_benchmark,
        "_policy_provenance_by_provider",
        lambda **_kwargs: {"native": {"fixture": True}},
    )
    monkeypatch.setattr(
        policy_benchmark,
        "_NativeLocAgentClient",
        lambda **_kwargs: FailingProvider(),
    )
    monkeypatch.setattr(
        policy_benchmark,
        "_run_locagent_loop",
        lambda **_kwargs: {
            "model": "test-model",
            "elapsed_seconds": 0.1,
            "regions": [],
            "usage": {},
        },
    )

    assert (
        main(
            [
                "locagent",
                "--cases",
                str(cases),
                "--output-dir",
                str(output_dir),
                "--provider",
                "native",
                "--locagent-checkout",
                str(tmp_path / "LocAgent"),
                "--locagent-python",
                sys.executable,
                "--native-index-dir",
                str(tmp_path / "native-index"),
                "--model",
                "test-model",
            ]
        )
        == 1
    )
    result_path = policy_result_path(
        output_dir,
        instance_id="demo__repo-1",
        agent="locagent",
        provider="native",
    )
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["cleanup_error"] == "TimeoutError: worker did not stop"
    assert result["error"] == (
        "provider cleanup failed: TimeoutError: worker did not stop"
    )


def test_orcaloca_run_activates_the_validated_checkout(monkeypatch, tmp_path) -> None:
    checkout = tmp_path / "orcaloca"
    marker = checkout / "Orcar" / "search_agent.py"
    marker.parent.mkdir(parents=True)
    marker.write_text("class SearchAgent: pass\n", encoding="utf-8")
    monkeypatch.setattr("sys.path", ["/installed-policy", str(checkout)])

    _activate_orcaloca_checkout(checkout)

    assert __import__("sys").path[0] == str(checkout.resolve())
    assert __import__("sys").path.count(str(checkout.resolve())) == 1
