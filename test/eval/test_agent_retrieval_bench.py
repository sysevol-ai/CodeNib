# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""ARB boundaries: query isolation, corpus identity, scoring, and recovery."""

import copy
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from filelock import FileLock, Timeout

from codenib.dataset import agent_retrieval_bench as dataset
from codenib.dataset.agent_retrieval_bench import (
    ARB_RELEASES,
    ARBCase,
    corpus_manifest,
    load_cases,
    load_corpus,
)
from codenib.eval.benchmarks import agent_retrieval_bench as arb
from codenib.eval.benchmarks import agent_retrieval_bench_runner as runner

COMMIT = "a" * 40
REPO = "example/project"


def write_rows(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


@pytest.fixture
def released_data(tmp_path, monkeypatch):
    # Synthetic releases contain one case each; real releases require full counts.
    monkeypatch.setattr(dataset, "ARB_RELEASE_COUNTS", dict.fromkeys(ARB_RELEASES, 1))
    root = tmp_path / "data"
    gold_by_task = {
        "code2test": {"related_tests": ["tests/check.py"]},
        "comment2context": {
            "must_context_files": [{"path": "src/target.py"}],
            "given_files": ["src/anchor.py"],
        },
        "trace2code": {"root_cause_files": ["src/target.py"]},
        "edit2ripple": {"files": ["src/target.py"], "given_files": ["src/anchor.py"]},
    }
    paths = ["LICENSE", "tests/check.py", "src/target.py", "src/anchor.py"] + [
        f"src/other{i}.py" for i in range(22)
    ]
    corpus = [
        {
            "repo": REPO,
            "base_commit": COMMIT,
            "chunk_id": str(index),
            "path": path,
            "kind": "file",
            "symbol": "",
            "start_line": 1,
            "end_line": 1,
            "text": (
                "needle important regression" if path == "tests/check.py" else "other"
            ),
        }
        for index, path in enumerate(paths)
    ]
    for release, task in ARB_RELEASES.items():
        sample = {
            "id": task,
            "repo": REPO,
            "base_commit": COMMIT,
            "task_type": task,
            "query": {"text": "needle important regression"},
            "candidate_corpus": {"type": "repo_at_base_commit", "base_commit": COMMIT},
            "gold": gold_by_task[task],
            "metadata": {"fix_commit": "b" * 40},
            "gold_blocks": [{"path": "PRIVATE_GOLD", "start_line": 1, "end_line": 2}],
        }
        if task == "edit2ripple":
            sample["query"] = {
                "anchor_file": "src/anchor.py",
                "intent": "adjust behavior",
            }
        relative = f"corpus/{release}/example__project/{COMMIT}.chunks.jsonl"
        write_rows(root / f"benchmark/{release}/samples.jsonl", [sample])
        write_rows(root / relative, corpus)
        write_rows(
            root / f"corpus/{release}/corpus_manifest.jsonl",
            [
                {
                    "repo": REPO,
                    "base_commit": COMMIT,
                    "chunks_path": f"data/{relative}",
                    "status": "ok",
                }
            ],
        )
    return root


def prepared_file(root, tmp_path):
    prepared = runner.prepare(root)
    path = tmp_path / "prepared.json"
    path.write_text(json.dumps(prepared))
    return path, prepared


def fake_index(rank):
    """BM25CorpusIndex stand-in that delegates each query to ``rank``."""

    class Index:
        build_seconds = 0.5
        tool_calls = 1
        size = {"documents": 2, "tokens": 6, "vocabulary": 3, "indexed_text_bytes": 9}

        def __init__(self, corpus):
            self.corpus = corpus

        def rank(self, query):
            return rank(query, self.corpus)

    return Index


def predictions(prepared):
    return [
        {
            "sample_id": record["sample"]["id"],
            "repo": REPO,
            "base_commit": COMMIT,
            "status": "ok",
            "ranked_files": ["LICENSE", "tests/check.py", "src/target.py"],
        }
        for record in prepared["cases"]
    ]


def test_all_tasks_query_isolation_and_cwd_independence(
    released_data, monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    cases = load_cases(released_data)
    assert len(cases) == 4
    for case in cases:
        assert json.loads(case.query_text) == case.sample["query"]
        assert "PRIVATE_GOLD" not in case.query_text
        assert "b" * 40 not in case.query_text
        assert case.sample["gold_blocks"][0]["path"] == "PRIVATE_GOLD"
        manifest = corpus_manifest(released_data, case.release)
        corpus = load_corpus(manifest[(REPO, COMMIT)], REPO, COMMIT)
        assert {"LICENSE", "tests/check.py"} <= {row["path"] for row in corpus}


@pytest.mark.parametrize(
    "field,value",
    [
        ("base_commit", "a" * 7),
        ("task_type", "abstention"),
        ("repo", "../escape"),
        ("candidate_corpus", {"type": "repo_at_base_commit", "base_commit": "b" * 40}),
        ("gold", {"no_gold": True}),
    ],
)
def test_sample_identity_rejected(released_data, field, value):
    sample = copy.deepcopy(load_cases(released_data)[0].sample)
    sample[field] = value
    with pytest.raises(ValueError):
        ARBCase.from_record("v2_code2test", sample)


def test_selection_and_duplicate_samples(released_data):
    assert [
        c.sample_id for c in load_cases(released_data, sample_ids=["trace2code"])
    ] == ["trace2code"]
    with pytest.raises(ValueError, match="unknown sample"):
        load_cases(released_data, sample_ids=["unknown"])
    with pytest.raises(ValueError, match="positive"):
        load_cases(released_data, limit_per_task=0)
    path = released_data / "benchmark/v2_code2test/samples.jsonl"
    path.write_text(path.read_text() * 2)
    with pytest.raises(ValueError, match="duplicate sample"):
        load_cases(released_data)


@pytest.mark.parametrize(
    "selection", [{}, {"limit_per_task": 1}, {"sample_ids": ["code2test"]}]
)
def test_complete_release_required_before_selection(
    released_data, monkeypatch, selection
):
    monkeypatch.setitem(dataset.ARB_RELEASE_COUNTS, "v2_code2test", 2)
    with pytest.raises(ValueError, match="expected 2 samples, got 1"):
        load_cases(released_data, **selection)


@pytest.mark.parametrize(
    "reference",
    ["../../outside", "/tmp/outside", "data/corpus/v2_code2test/../../outside"],
)
def test_manifest_escape_rejected(released_data, reference):
    path = released_data / "corpus/v2_code2test/corpus_manifest.jsonl"
    row = json.loads(path.read_text())
    row["chunks_path"] = reference
    write_rows(path, [row])
    with pytest.raises(ValueError):
        corpus_manifest(released_data, "v2_code2test")


def test_manifest_symlink_escape_rejected(released_data, tmp_path):
    path = next((released_data / "corpus/v2_code2test").rglob("*.chunks.jsonl"))
    path.unlink()
    path.symlink_to(tmp_path / "outside")
    with pytest.raises(ValueError, match="escapes"):
        corpus_manifest(released_data, "v2_code2test")


@pytest.mark.parametrize("corruption", ["commit", "canonical", "duplicate", "lines"])
def test_corrupt_corpus_retained_as_preparation_failure(released_data, corruption):
    path = next((released_data / "corpus/v2_code2test").rglob("*.chunks.jsonl"))
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    if corruption == "commit":
        rows[0]["base_commit"] = "b" * 40
    elif corruption == "canonical":
        rows[0]["kind"] = "symbol"
    elif corruption == "duplicate":
        rows.append({**rows[0], "chunk_id": "duplicate-file"})
    else:
        rows[0]["start_line"] = 0
    write_rows(path, rows)
    prepared = runner.prepare(released_data)
    assert len(prepared["cases"]) == 4
    assert "error" in prepared["cases"][0]["corpus"]
    assert all("error" not in row["corpus"] for row in prepared["cases"][1:])


def test_bm25_keeps_tests_and_extensionless_paths(released_data):
    path = corpus_manifest(released_data, "v2_code2test")[(REPO, COMMIT)]
    corpus = load_corpus(path, REPO, COMMIT)
    ranked = arb.rank_bm25("needle important regression", corpus)
    assert ranked[0] == "tests/check.py"
    assert set(ranked) == {row["path"] for row in corpus}
    assert "LICENSE" in ranked


def test_chunk_dedup_before_file_cutoff_and_line_conversion(released_data, monkeypatch):
    import codenib.index.sparse_idx.bm25_index as bm25

    path = corpus_manifest(released_data, "v2_code2test")[(REPO, COMMIT)]
    corpus = load_corpus(path, REPO, COMMIT)
    nodes = [SimpleNamespace(file="LICENSE")] * 30 + [
        SimpleNamespace(file=row["path"]) for row in corpus[1:]
    ]

    class Index:
        retriever = SimpleNamespace(
            size=lambda: {"documents": 0, "tokens": 0, "vocabulary": 0},
            documents=[],
        )

        def __init__(self, *, chunks, max_k):
            assert max_k == len(corpus)
            assert chunks[0].start_line == chunks[0].end_line == 0
            assert chunks[0].node_id.startswith("LICENSE:")

        def search(self, query, **kwargs):
            assert query == "query only"
            assert not kwargs["filter_test"]
            assert not kwargs["return_code_content"]
            return nodes

    monkeypatch.setattr(bm25, "BM25CodeIndexer", Index)
    ranked = arb.rank_bm25("query only", corpus)
    assert ranked[:3] == ["LICENSE", "tests/check.py", "src/target.py"]
    assert len(ranked) == len(corpus)


def test_corpus_index_reports_size_and_matches_one_shot_ranking(released_data):
    path = corpus_manifest(released_data, "v2_code2test")[(REPO, COMMIT)]
    corpus = load_corpus(path, REPO, COMMIT)
    index = arb.BM25CorpusIndex(corpus)
    assert index.size["documents"] == len(corpus)
    assert index.size["tokens"] >= index.size["vocabulary"] > 0
    assert index.size["indexed_text_bytes"] > 0
    assert index.build_seconds >= 0
    query = "needle important regression"
    assert index.rank(query) == arb.rank_bm25(query, corpus)


def test_sample_split_uses_first_digest_byte():
    for sample_id in ("0a4b3f17ec58d3405b1c9a47", "code2test", "trace2code"):
        even = hashlib.sha256(sample_id.encode()).digest()[0] % 2 == 0
        assert arb.sample_split(sample_id) == ("dev" if even else "heldout")


def test_cases_grouped_by_corpus_in_first_appearance_order():
    cases = [
        {"sample": {"id": "a"}, "corpus": {"sha256": "x"}},
        {"sample": {"id": "b"}, "corpus": {"sha256": "y"}},
        {"sample": {"id": "c"}, "corpus": {"error": "missing"}},
        {"sample": {"id": "d"}, "corpus": {"sha256": "x"}},
        {"sample": {"id": "e"}, "corpus": {"error": "missing"}},
    ]
    order = [record["sample"]["id"] for record in runner.by_corpus(cases)]
    assert order == ["a", "d", "b", "c", "e"]


def test_input_drift_and_prediction_identity(released_data):
    prepared = runner.prepare(released_data)
    pred = predictions(prepared)
    with pytest.raises(ValueError, match="duplicate"):
        runner.prediction_map(pred + [pred[0]], prepared)
    with pytest.raises(ValueError, match="unknown"):
        runner.prediction_map([{**pred[0], "sample_id": "unknown"}], prepared)
    record = prepared["cases"][0]
    path = released_data / record["corpus"]["path"]
    path.write_text(path.read_text() + "\n")
    with pytest.raises(ValueError, match="SHA-256"):
        runner.checked_corpus(prepared, record)


def test_adapter_changes_invalidate_receipt_despite_same_dataset_basename(monkeypatch):
    monkeypatch.setattr(arb, "sha256_file", lambda path: "original")
    before = arb.implementation_receipt()
    adapter = Path(arb.__file__)
    monkeypatch.setattr(
        arb, "sha256_file", lambda path: "changed" if path == adapter else "original"
    )
    assert arb.implementation_receipt() != before


@pytest.fixture
def fake_evaluator(monkeypatch):
    monkeypatch.setattr(runner, "validate_upstream", lambda path: path)
    monkeypatch.setattr(runner, "implementation_receipt", lambda: {"implementation": 1})

    def score(jobs, *args):
        return [
            {
                "sample_id": job["sample"]["id"],
                "task_type": job["sample"]["task_type"],
                "status": (
                    "ok"
                    if not job.get("error")
                    and (job.get("prediction") or {}).get("status") == "ok"
                    else "error"
                ),
                "metrics": dict.fromkeys(arb.METRICS, 1.0),
            }
            for job in jobs
        ]

    monkeypatch.setattr(runner, "evaluate_official", score)


def test_missing_predictions_count_in_denominator(
    released_data, tmp_path, fake_evaluator
):
    _path, prepared = prepared_file(released_data, tmp_path)
    result = runner.evaluate(
        prepared, predictions(prepared)[:1], tmp_path, sys.executable
    )
    summary = result["summary"]["overall"]
    assert (summary["selected"], summary["valid"], summary["failed"]) == (4, 1, 3)
    assert summary["metrics_all_selected"]["MRR"] == 0.25
    assert summary["metrics_valid_only"]["MRR"] == 1


def test_interrupted_run_resume_and_drift(
    released_data, tmp_path, fake_evaluator, monkeypatch
):
    path, _prepared = prepared_file(released_data, tmp_path)
    calls = []

    def rank(query, corpus):
        calls.append(query)
        if len(calls) == 2:
            raise KeyboardInterrupt
        return [row["path"] for row in corpus]

    monkeypatch.setattr(runner, "BM25CorpusIndex", fake_index(rank))
    output = tmp_path / "run"
    with pytest.raises(KeyboardInterrupt):
        runner.execute(path, output, tmp_path)
    assert len(runner.load_predictions(output / "predictions.json")) == 1
    assert not (output / "results.json").exists()
    result = runner.execute(path, output, tmp_path, resume=True)
    assert result["summary"]["overall"]["valid"] == 4
    assert len(calls) == 5  # first success is not rerun
    monkeypatch.setattr(runner, "implementation_receipt", lambda: {"implementation": 2})
    with pytest.raises(ValueError, match="changed"):
        runner.execute(path, output, tmp_path, resume=True)


def test_one_writer_owns_output(released_data, tmp_path, fake_evaluator):
    path, _prepared = prepared_file(released_data, tmp_path)
    output = tmp_path / "run"
    output.mkdir()
    with FileLock(str(output / ".run.lock")):
        with pytest.raises(Timeout):
            runner.execute(path, output, tmp_path)
        assert not (output / "config.json").exists()


@pytest.mark.parametrize(
    "field,value",
    [("ranked_files", ["injected.py"]), ("status", "error"), ("repo", "other/repo")],
)
def test_resume_rebuilds_edited_export_from_native_checkpoint(
    released_data, tmp_path, fake_evaluator, monkeypatch, field, value
):
    path, _prepared = prepared_file(released_data, tmp_path)
    output = tmp_path / "run"
    runner.execute(path, output, tmp_path)
    original = runner.load_predictions(output / "predictions.json")
    modified = copy.deepcopy(original)
    modified[0][field] = value
    (output / "predictions.json").write_text(json.dumps(modified))
    monkeypatch.setattr(
        runner,
        "BM25CorpusIndex",
        lambda *args: pytest.fail("completed retrieval rerun"),
    )
    result = runner.execute(path, output, tmp_path, resume=True)
    assert result["summary"]["overall"]["valid"] == 4
    assert runner.load_predictions(output / "predictions.json") == original


def test_resume_rejects_modified_native_checkpoint(
    released_data, tmp_path, fake_evaluator
):
    path, _prepared = prepared_file(released_data, tmp_path)
    output = tmp_path / "run"
    runner.execute(path, output, tmp_path)
    checkpoint = json.loads((output / "checkpoint.json").read_text())
    checkpoint["predictions"][0]["ranked_files"] = ["injected.py"]
    (output / "checkpoint.json").write_text(json.dumps(checkpoint))
    with pytest.raises(ValueError, match="integrity receipt"):
        runner.execute(path, output, tmp_path, resume=True)
    assert not (output / "results.json").exists()


def test_checkpoint_survives_interrupted_public_export(
    released_data, tmp_path, fake_evaluator, monkeypatch
):
    path, _prepared = prepared_file(released_data, tmp_path)
    output = tmp_path / "run"
    write = runner.write_json_atomic
    calls = []

    def interrupt_export(destination, payload):
        if destination.name == "predictions.json":
            raise KeyboardInterrupt
        write(destination, payload)

    def rank(query, corpus):
        calls.append(query)
        return [row["path"] for row in corpus]

    monkeypatch.setattr(runner, "BM25CorpusIndex", fake_index(rank))
    monkeypatch.setattr(runner, "write_json_atomic", interrupt_export)
    with pytest.raises(KeyboardInterrupt):
        runner.execute(path, output, tmp_path)
    assert not (output / "predictions.json").exists()
    monkeypatch.setattr(runner, "write_json_atomic", write)
    result = runner.execute(path, output, tmp_path, resume=True)
    assert result["summary"]["overall"]["valid"] == 4
    assert len(calls) == 4


@pytest.mark.parametrize("exists", [False, True])
def test_cli_invalid_upstream_exits_without_traceback(
    released_data, tmp_path, capsys, exists
):
    path, _prepared = prepared_file(released_data, tmp_path)
    checkout = tmp_path / "upstream"
    if exists:
        checkout.mkdir()
    with pytest.raises(SystemExit) as failure:
        runner.main(
            [
                "run",
                "--prepared",
                str(path),
                "--output",
                str(tmp_path / "run"),
                "--upstream",
                str(checkout),
            ]
        )
    assert failure.value.code == 2
    error = capsys.readouterr().err
    assert "CalledProcessError" in error
    assert "Traceback" not in error


def test_shared_snapshot_built_once_with_separate_costs(
    released_data, tmp_path, fake_evaluator, monkeypatch
):
    path, _prepared = prepared_file(released_data, tmp_path)
    builds = []

    class Index(fake_index(lambda query, corpus: [row["path"] for row in corpus])):
        def __init__(self, corpus):
            builds.append(len(corpus))
            super().__init__(corpus)

    monkeypatch.setattr(runner, "BM25CorpusIndex", Index)
    result = runner.execute(path, tmp_path / "run", tmp_path)
    rows = runner.load_predictions(tmp_path / "run" / "predictions.json")
    # The four synthetic releases carry byte-identical corpora.
    assert len(builds) == 1
    assert [row["index_built"] for row in rows] == [True, False, False, False]
    assert rows[0]["index_build_seconds"] == 0.5
    assert rows[0]["index_size"]["documents"] == 2
    assert all("index_size" not in row for row in rows[1:])
    assert all(row["tool_calls"] == 1 and row["query_seconds"] >= 0 for row in rows)
    usage = result["retrieval_usage"]
    assert usage["index_builds"] == 1
    assert usage["index_build_seconds"]["total"] == 0.5
    assert usage["index_size"]["tokens"]["max"] == 6
    assert usage["query_seconds"]["count"] == 4
    assert usage["model_calls"] == 0
    assert result["environment"]["cpu_count"] >= 1
    splits = result["summary"]["by_split"]
    assert sum(group["selected"] for group in splits.values()) == 4
    assert {row["split"] for row in result["cases"]} == set(splits)


def test_failed_retrieval_checkpoint_is_not_silently_retried(
    released_data, tmp_path, fake_evaluator, monkeypatch
):
    path, _prepared = prepared_file(released_data, tmp_path)
    calls = []

    def fail(query, corpus):
        calls.append(query)
        raise RuntimeError("retrieval unavailable")

    monkeypatch.setattr(runner, "BM25CorpusIndex", fake_index(fail))
    output = tmp_path / "run"
    result = runner.execute(path, output, tmp_path)
    assert result["summary"]["overall"]["failed"] == 4
    runner.execute(path, output, tmp_path, resume=True)
    assert len(calls) == 4
    rows = runner.load_predictions(output / "predictions.json")
    assert all("retrieval unavailable" in row["error"] for row in rows)
    # Failed searches still count as attempted calls with measured latency.
    assert all(row["tool_calls"] == 1 and row["query_seconds"] >= 0 for row in rows)
    assert result["retrieval_usage"]["tool_calls"]["total"] == 4
    assert result["retrieval_usage"]["query_seconds"]["count"] == 4


@pytest.mark.parametrize(
    "revision,dirty",
    [
        ("b" * 40, ""),
        (arb.ARB_UPSTREAM_REVISION, " M src/agent_retrieval_bench/baseline.py"),
    ],
)
def test_unpinned_or_modified_evaluator_rejected(
    tmp_path, monkeypatch, revision, dirty
):
    def git(command, **kwargs):
        return revision if "rev-parse" in command else dirty

    monkeypatch.setattr(arb.subprocess, "check_output", git)
    with pytest.raises(ValueError):
        arb.validate_upstream(tmp_path)


@pytest.mark.parametrize("flag", ["--skip-worktree", "--assume-unchanged"])
def test_hidden_evaluator_edits_rejected(tmp_path, monkeypatch, flag):
    checkout = tmp_path / "upstream"
    source = checkout / "src/agent_retrieval_bench/bcy_curve.py"
    source.parent.mkdir(parents=True)
    source.write_text("original = True\n")
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    subprocess.run(["git", "-C", str(checkout), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    revision = subprocess.check_output(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True
    ).strip()
    monkeypatch.setattr(arb, "ARB_UPSTREAM_REVISION", revision)
    assert arb.validate_upstream(checkout) == checkout
    subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "update-index",
            flag,
            str(source.relative_to(checkout)),
        ],
        check=True,
    )
    source.write_text("original = False\n")
    with pytest.raises(ValueError, match="hidden Git index flags"):
        arb.validate_upstream(checkout)


def test_offline_evaluation_no_retrieval_and_no_stale_report(
    released_data, tmp_path, fake_evaluator, monkeypatch
):
    path, prepared = prepared_file(released_data, tmp_path)
    pred = tmp_path / "external.jsonl"
    write_rows(pred, predictions(prepared))
    monkeypatch.setattr(
        runner,
        "BM25CorpusIndex",
        lambda *args: pytest.fail("offline rescore retrieved"),
    )
    output = tmp_path / "rescore"
    runner.execute(path, output, tmp_path, pred_path=pred)
    assert (output / "results.json").exists()

    def fail(*args):
        raise RuntimeError("evaluator unavailable")

    monkeypatch.setattr(runner, "evaluate_official", fail)
    with pytest.raises(RuntimeError):
        runner.execute(path, output, tmp_path, pred_path=pred, resume=True)
    assert not (output / "results.json").exists()
    assert (output / "predictions.json").exists()


@pytest.fixture
def upstream():
    checkout = os.environ.get("ARB_CHECKOUT")
    if not checkout:
        pytest.skip("set ARB_CHECKOUT to the pinned upstream checkout")
    return arb.validate_upstream(Path(checkout))


@pytest.mark.integration
def test_official_scorer_four_tasks_and_full_mrr(released_data, upstream, tmp_path):
    prepared = runner.prepare(released_data)
    pred = predictions(prepared)
    result = runner.evaluate(prepared, pred, upstream, sys.executable)
    assert result["summary"]["overall"]["valid"] == 4
    by_task = {row["task_type"]: row for row in result["cases"]}
    assert by_task["code2test"]["metrics"]["MRR"] == 0.5
    assert by_task["comment2context"]["metrics"]["MRR"] == 1 / 3
    assert all(row["metrics"]["BCY@8000"] == 1 for row in result["cases"])
    pred[-1]["ranked_files"] = [f"src/other{i}.py" for i in range(22)] + [
        "src/target.py"
    ]
    result = runner.evaluate(prepared, pred, upstream, sys.executable)
    ripple = result["cases"][-1]["metrics"]
    assert ripple["MRR"] == 1 / 23
    assert ripple["Recall@20"] == ripple["BCY@32000"] == 0


@pytest.mark.integration
@pytest.mark.parametrize(
    "failure", ["missing", "revision", "unknown-file", "leakage", "retrieval"]
)
def test_official_failures_never_disappear(released_data, upstream, failure):
    prepared = runner.prepare(released_data)
    pred = predictions(prepared)
    if failure == "missing":
        pred.pop(0)
    elif failure == "revision":
        pred[0]["base_commit"] = "b" * 40
    elif failure == "unknown-file":
        pred[0]["ranked_files"] = ["unknown.py"]
    elif failure == "leakage":
        sample = prepared["cases"][0]["sample"]
        sample["query"] = {"text": "b" * 40}
        sample["gold"]["fix_commit"] = "b" * 40
    else:
        pred[0].update(status="error", error="retrieval failed")
    result = runner.evaluate(prepared, pred, upstream, sys.executable)
    assert result["summary"]["overall"]["failed"] == 1
    assert len(result["cases"]) == 4
    assert result["cases"][0]["status"] == "error"


@pytest.mark.integration
def test_canonical_bcy_content_boundary_and_file_dedup(released_data, upstream):
    corpus_path = corpus_manifest(released_data, "v2_code2test")[(REPO, COMMIT)]
    corpus = load_corpus(corpus_path, REPO, COMMIT)
    # LICENSE header = 4 tokens; 3990 content + 1 separator leaves 5 tokens.
    # The test path header costs 8, so no test content fits at 4000.
    corpus[0]["text"] = "longword " * 3990
    write_rows(corpus_path, corpus)
    prepared = runner.prepare(released_data, releases=["v2_code2test"])
    pred = predictions(prepared)
    pred[0]["ranked_files"] = ["LICENSE"] * 30 + ["tests/check.py"]
    result = runner.evaluate(prepared, pred, upstream, sys.executable)
    row = result["cases"][0]
    assert row["status"] == "ok"
    assert row["ranking_depth"] == 2
    assert row["metrics"]["Recall@5"] == 1
    assert row["metrics"]["BCY@4000"] == 0
    assert row["metrics"]["BCY@8000"] == 1
    assert row["packed"]["4000"]["used_tokens"] == 4000


def test_full_source_access_reaches_scorer_and_report(
    released_data, tmp_path, monkeypatch
):
    monkeypatch.setattr(runner, "validate_upstream", lambda path: path)
    seen = []

    def score(jobs, *args):
        seen.extend(job["restrict_to_candidates"] for job in jobs)
        return [
            {
                "sample_id": job["sample"]["id"],
                "task_type": job["sample"]["task_type"],
                "status": "ok",
                "metrics": dict.fromkeys(arb.METRICS, 1.0),
            }
            for job in jobs
        ]

    monkeypatch.setattr(runner, "evaluate_official", score)
    prepared = runner.prepare(released_data)
    result = runner.evaluate(
        prepared, predictions(prepared), tmp_path, sys.executable, "full-source"
    )
    assert seen == [True] * 4
    assert result["protocol"]["source_access"] == "full-source"
    with pytest.raises(ValueError, match="source access"):
        runner.evaluate(prepared, [], tmp_path, sys.executable, "checkout")


def test_native_method_fixes_its_source_access(released_data, tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "validate_upstream", lambda path: path)
    path, _prepared = prepared_file(released_data, tmp_path)
    with pytest.raises(ValueError, match="released-corpus"):
        runner.execute(path, tmp_path / "run", tmp_path, access="full-source")
    with pytest.raises(ValueError, match="--workspace"):
        runner.method_indexer("graph_expansion", {})


@pytest.mark.integration
def test_official_full_source_scores_only_released_files(released_data, upstream):
    prepared = runner.prepare(released_data)
    pred = predictions(prepared)
    pred[0]["ranked_files"] = ["vendor/full_source_only.py"] + pred[0]["ranked_files"]
    strict = runner.evaluate(prepared, pred, upstream, sys.executable)
    assert strict["cases"][0]["status"] == "error"
    full = runner.evaluate(prepared, pred, upstream, sys.executable, "full-source")
    reference = runner.evaluate(
        prepared, predictions(prepared), upstream, sys.executable
    )
    assert full["cases"][0]["status"] == "ok"
    assert full["cases"][0]["removed_non_candidate_paths"] == 1
    # The dropped file consumes no rank before cutoffs.
    assert full["cases"][0]["metrics"] == reference["cases"][0]["metrics"]
