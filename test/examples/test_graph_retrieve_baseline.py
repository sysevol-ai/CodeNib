# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the pure helpers in ``examples/graph_retrieve_baseline.py``.

Phase 1 review called out that the runner's helpers had no tests, so as
the harness grows (Phase 2 added percentile / memory rollups) regressions
would land silently. The helpers covered here are pure functions:

- ``_resolve_rerank_strategy``: legacy ``--embedding`` ↔ new
  ``--rerank-strategy`` precedence and the cross-encoder NotImplementedError
  fence (until PR #128 lands).
- ``_aggregate_section_stats``: sums per-instance section payloads, with
  optional sample / memory roll-ups added in Phase 2.
- ``_filter_index_sections`` / ``_filter_query_sections``: replace the
  prior ``_split_sections_by_phase`` helper that silently dropped
  unexpected labels into the wrong bucket — these now log a warning so
  future leaks are visible.

The runner script is not part of the ``codeminer`` package (see
``pyproject.toml``: ``include = ["codeminer*"]``), so we load it via
``importlib.util`` rather than polluting ``sys.path``.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

_RUNNER_PATH = (
    Path(__file__).resolve().parents[2] / "examples" / "graph_retrieve_baseline.py"
)


def _load_runner():
    spec = importlib.util.spec_from_file_location(
        "graph_retrieve_baseline_under_test", _RUNNER_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def runner():
    return _load_runner()


@pytest.fixture
def runner_caplog(runner, caplog):
    """``caplog`` that actually sees warnings emitted via ``runner.logger``.

    The project's ``get_logger`` sets ``propagate=False`` and installs its
    own handler. caplog attaches to the root logger, so without temporarily
    re-enabling propagation it never sees these records.
    """
    original = runner.logger.propagate
    runner.logger.propagate = True
    try:
        yield caplog
    finally:
        runner.logger.propagate = original


# ---------------------------------------------------------------------------
# _resolve_rerank_strategy
# ---------------------------------------------------------------------------


def _args(rerank_strategy=None, embedding=False):
    return SimpleNamespace(rerank_strategy=rerank_strategy, embedding=embedding)


def test_resolve_strategy_legacy_embedding_off(runner):
    """Neither flag set → 'none' (default behaviour)."""
    assert runner._resolve_rerank_strategy(_args()) == "none"


def test_resolve_strategy_legacy_embedding_on(runner):
    """``--embedding`` alone resolves to 'embedding' (legacy path)."""
    assert runner._resolve_rerank_strategy(_args(embedding=True)) == "embedding"


def test_resolve_strategy_explicit_overrides_legacy(runner):
    """``--rerank-strategy`` wins over legacy ``--embedding``.

    A user who passes ``--embedding --rerank-strategy=none`` clearly meant
    'none', not 'embedding'. Without this precedence, the legacy default
    would silently re-enable rerank.
    """
    assert (
        runner._resolve_rerank_strategy(_args(rerank_strategy="none", embedding=True))
        == "none"
    )


def test_resolve_strategy_explicit_embedding(runner):
    assert (
        runner._resolve_rerank_strategy(_args(rerank_strategy="embedding"))
        == "embedding"
    )


def test_resolve_strategy_cross_encoder_raises(runner):
    """Cross-encoder is wired but not implemented until PR #128 lands."""
    with pytest.raises(NotImplementedError, match="PR #128"):
        runner._resolve_rerank_strategy(_args(rerank_strategy="cross-encoder"))


def test_parse_args_rejects_cross_encoder_at_parse_time(runner, monkeypatch, capsys):
    """The cross-encoder NotImplementedError should be surfaced by parse_args
    via parser.error (exit code 2) before any dataset-load side effects, not
    deep inside run_graph_pipeline."""
    monkeypatch.setattr(
        "sys.argv",
        [
            "graph_retrieve_baseline.py",
            "--dataset",
            "swebench_lite",
            "--rerank-strategy",
            "cross-encoder",
        ],
    )
    with pytest.raises(SystemExit) as exc_info:
        runner.parse_args()
    assert exc_info.value.code == 2, (
        "expected argparse exit code 2, got " f"{exc_info.value.code}"
    )
    err = capsys.readouterr().err
    assert "PR #128" in err, f"expected error mentioning PR #128, got: {err!r}"


def test_parse_args_accepts_graph_route(runner, monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        [
            "graph_retrieve_baseline.py",
            "--dataset",
            "swebench_lite",
            "--graph-route",
            "lsp",
        ],
    )

    args = runner.parse_args()

    assert args.graph_route == "lsp"


# ---------------------------------------------------------------------------
# _aggregate_section_stats
# ---------------------------------------------------------------------------


def _section(
    label,
    total,
    count=1,
    *,
    section_min=None,
    section_max=None,
    errors=0,
    samples=None,
    rss_deltas=None,
    gpu_peaks=None,
):
    if section_min is None:
        section_min = total / count if count else 0.0
    if section_max is None:
        section_max = total / count if count else 0.0
    entry = {
        "label": label,
        "total": total,
        "count": count,
        "average": (total / count) if count else 0.0,
        "min": section_min,
        "max": section_max,
        "errors": errors,
    }
    if samples is not None:
        entry["samples"] = samples
    if rss_deltas is not None:
        entry["rss_deltas"] = rss_deltas
    if gpu_peaks is not None:
        entry["gpu_peaks"] = gpu_peaks
    return entry


def test_aggregate_empty(runner):
    assert runner._aggregate_section_stats([]) == []


def test_aggregate_sums_across_instances(runner):
    """Two instances, same label: totals/counts add, min/max collapse correctly."""
    instances = [
        {
            "instance_id": "a",
            "sections": [
                _section("query.bm25_seed", total=2.0, count=1),
                _section("query.graph_expand", total=10.0, count=1),
            ],
        },
        {
            "instance_id": "b",
            "sections": [
                _section("query.bm25_seed", total=4.0, count=1),
                _section("query.graph_expand", total=8.0, count=1),
            ],
        },
    ]
    out = runner._aggregate_section_stats(instances)
    by_label = {s["label"]: s for s in out}

    assert by_label["query.bm25_seed"]["total"] == pytest.approx(6.0)
    assert by_label["query.bm25_seed"]["count"] == 2
    assert by_label["query.bm25_seed"]["average"] == pytest.approx(3.0)
    assert by_label["query.bm25_seed"]["min"] == pytest.approx(2.0)
    assert by_label["query.bm25_seed"]["max"] == pytest.approx(4.0)

    assert by_label["query.graph_expand"]["total"] == pytest.approx(18.0)
    assert by_label["query.graph_expand"]["count"] == 2
    assert by_label["query.graph_expand"]["min"] == pytest.approx(8.0)
    assert by_label["query.graph_expand"]["max"] == pytest.approx(10.0)


def test_aggregate_sorted_by_total_desc(runner):
    instances = [
        {
            "instance_id": "a",
            "sections": [
                _section("cheap", total=1.0),
                _section("expensive", total=100.0),
                _section("medium", total=10.0),
            ],
        }
    ]
    out = runner._aggregate_section_stats(instances)
    assert [s["label"] for s in out] == ["expensive", "medium", "cheap"]


def test_aggregate_concatenates_samples_and_recomputes_percentiles(runner):
    """Phase-2 sample roll-up: per-instance samples concatenate, p50/p95/p99
    are recomputed globally so the aggregate matches what a single global
    profiler would have reported."""
    instances = [
        {
            "instance_id": "a",
            "sections": [
                _section(
                    "query.bm25_seed",
                    total=10.0,
                    count=10,
                    samples=[float(x) for x in range(1, 11)],  # 1..10
                )
            ],
        },
        {
            "instance_id": "b",
            "sections": [
                _section(
                    "query.bm25_seed",
                    total=10.0,
                    count=10,
                    samples=[float(x) for x in range(11, 21)],  # 11..20
                )
            ],
        },
    ]
    out = runner._aggregate_section_stats(instances)
    [agg] = out
    assert agg["sample_count"] == 20
    # Linear-interpolation percentiles over 1..20:
    #   p50 ≈ 10.5, p95 ≈ 19.05, p99 ≈ 19.81
    assert agg["p50"] == pytest.approx(10.5, rel=1e-3)
    assert agg["p95"] == pytest.approx(19.05, rel=1e-3)
    assert agg["p99"] == pytest.approx(19.81, rel=1e-3)


def test_aggregate_rolls_up_memory_when_present(runner):
    instances = [
        {
            "instance_id": "a",
            "sections": [
                _section(
                    "index.graph_build",
                    total=1.0,
                    rss_deltas=[10.0, 20.0],
                    gpu_peaks=[100.0],
                )
            ],
        },
        {
            "instance_id": "b",
            "sections": [
                _section(
                    "index.graph_build",
                    total=1.0,
                    rss_deltas=[30.0],
                    gpu_peaks=[200.0, 50.0],
                )
            ],
        },
    ]
    [agg] = runner._aggregate_section_stats(instances)
    assert agg["rss_delta_avg"] == pytest.approx(20.0)  # (10+20+30)/3
    assert agg["rss_delta_max"] == pytest.approx(30.0)
    assert agg["gpu_peak_avg"] == pytest.approx((100 + 200 + 50) / 3)
    assert agg["gpu_peak_max"] == pytest.approx(200.0)


def test_aggregate_skips_memory_rollup_when_absent(runner):
    """Sections without samples/memory don't grow the schema with empty fields."""
    instances = [
        {
            "instance_id": "a",
            "sections": [_section("query.bm25_seed", total=1.0)],
        }
    ]
    [agg] = runner._aggregate_section_stats(instances)
    for absent in (
        "p50",
        "p95",
        "p99",
        "sample_count",
        "rss_delta_avg",
        "rss_delta_max",
        "gpu_peak_avg",
        "gpu_peak_max",
    ):
        assert absent not in agg, f"unexpected key {absent!r} in {agg}"


# ---------------------------------------------------------------------------
# _filter_index_sections / _filter_query_sections
# ---------------------------------------------------------------------------


def test_filter_index_keeps_index_and_inner_labels(runner):
    sections = [
        {"label": "index.graph_build"},
        {"label": "chunking_python"},
        {"label": "embedding_encode_batch"},
        {"label": "faiss_index_add_l2"},
    ]
    out = runner._filter_index_sections(sections)
    assert [s["label"] for s in out] == [s["label"] for s in sections]


def test_filter_index_warns_on_query_leak(runner, runner_caplog):
    sections = [
        {"label": "index.graph_build"},
        {"label": "query.bm25_seed"},  # leaked
    ]
    with runner_caplog.at_level("WARNING"):
        out = runner._filter_index_sections(sections)
    assert [s["label"] for s in out] == ["index.graph_build"]
    assert any(
        "query.bm25_seed" in rec.getMessage() for rec in runner_caplog.records
    ), (
        "expected leak warning; got "
        f"{[r.getMessage() for r in runner_caplog.records]}"
    )


def test_filter_query_keeps_query_only(runner):
    sections = [
        {"label": "query.bm25_seed"},
        {"label": "query.graph_expand"},
        {"label": "query.embedding_rerank"},
    ]
    out = runner._filter_query_sections(sections)
    assert [s["label"] for s in out] == [s["label"] for s in sections]


def test_filter_query_warns_on_index_leak(runner, runner_caplog):
    sections = [
        {"label": "query.bm25_seed"},
        {"label": "index.graph_build"},  # leaked
        {"label": "chunking_python"},  # leaked
    ]
    with runner_caplog.at_level("WARNING"):
        out = runner._filter_query_sections(sections)
    assert [s["label"] for s in out] == ["query.bm25_seed"]
    assert runner_caplog.records, "expected at least one warning record"
    msg = runner_caplog.records[-1].getMessage()
    assert "index.graph_build" in msg and "chunking_python" in msg


def test_filter_query_drops_unrecognised_labels_with_warning(runner, runner_caplog):
    """Future label additions that don't follow the query.* convention surface
    as warnings rather than disappearing into the wrong bucket."""
    sections = [
        {"label": "query.bm25_seed"},
        {"label": "fancy_new_phase_3_label"},
    ]
    with runner_caplog.at_level("WARNING"):
        out = runner._filter_query_sections(sections)
    assert [s["label"] for s in out] == ["query.bm25_seed"]
    assert any(
        "fancy_new_phase_3_label" in rec.getMessage() for rec in runner_caplog.records
    )


# ---------------------------------------------------------------------------
# _compute_bm25_seed_recall
# ---------------------------------------------------------------------------


def _seed(node_id):
    """Minimal duck-typed stand-in for the NodeInfo / QueriedNode seeds the
    pipeline hands to ``_compute_bm25_seed_recall`` — only ``node_id`` is read
    by ``extract_predictions``."""
    return SimpleNamespace(node_id=node_id)


def test_seed_recall_empty_targets_returns_empty(runner):
    """Instance has no GT files → no recall to report."""
    seeds = [_seed("a.py:foo()"), _seed("b.py:bar()")]
    assert runner._compute_bm25_seed_recall(seeds, [], [1, 5, 10]) == {}


def test_seed_recall_partial_then_full_hit(runner):
    """First seed misses the GT file, the second hits it; recall climbs
    from 0/1 at k=1 to 1/1 at k=2 and stays at 1.0 for larger k."""
    seeds = [_seed("other.py:x()"), _seed("target.py:hit()")]
    target_files = ["target.py"]
    out = runner._compute_bm25_seed_recall(seeds, target_files, [1, 2, 5])
    assert out == {1: 0.0, 2: 1.0, 5: 1.0}


def test_seed_recall_multi_target_partial(runner):
    """Two GT files, three seeds — only one GT file in top-1, both by top-2."""
    seeds = [
        _seed("hit_a.py:foo()"),
        _seed("hit_b.py:bar()"),
        _seed("miss.py:baz()"),
    ]
    out = runner._compute_bm25_seed_recall(seeds, ["hit_a.py", "hit_b.py"], [1, 2, 3])
    assert out == {1: 0.5, 2: 1.0, 3: 1.0}


def test_seed_recall_returns_int_keys(runner):
    """Keys must be ``int`` even when ``ks`` is supplied as floats / numpy
    scalars — the aggregator downstream keys into the dict via ``int(k)`` so a
    str/float key would silently produce zeros."""
    seeds = [_seed("a.py:foo()")]
    out = runner._compute_bm25_seed_recall(seeds, ["a.py"], [1.0, 2, 3.0])
    assert list(out.keys()) == [1, 2, 3]
    assert all(isinstance(k, int) for k in out.keys())


def test_seed_recall_normalizes_file_paths(runner):
    """Seeds emitted as ``./pkg/mod.py:sym()`` should match a GT entry
    ``pkg/mod.py`` because ``extract_predictions`` strips the leading ``./``
    via ``normalize_file_path``."""
    seeds = [_seed("./pkg/mod.py:foo()")]
    out = runner._compute_bm25_seed_recall(seeds, ["pkg/mod.py"], [1])
    assert out == {1: 1.0}


def test_seed_recall_dedupes_seeds_before_k_slice(runner):
    """``extract_predictions`` collapses duplicate files; the top-1 slice
    should therefore see the second distinct file at k=2, not a re-count of
    the first."""
    seeds = [
        _seed("dup.py:a()"),
        _seed("dup.py:b()"),
        _seed("target.py:c()"),
    ]
    # k=2 covers the two unique files {dup.py, target.py}, hitting target.py
    out = runner._compute_bm25_seed_recall(seeds, ["target.py"], [1, 2])
    assert out == {1: 0.0, 2: 1.0}


# ---------------------------------------------------------------------------
# _load_instance_ids_from_csv / _filter_regex_from_ids  (#131 corpus filter)
# ---------------------------------------------------------------------------


def test_load_instance_ids_reads_column_in_order(runner, tmp_path):
    csv_path = tmp_path / "ids.csv"
    csv_path.write_text("instance_id\nrepo__a-1\nrepo__b-2\nrepo__c-3\n")
    assert runner._load_instance_ids_from_csv(str(csv_path)) == [
        "repo__a-1",
        "repo__b-2",
        "repo__c-3",
    ]


def test_load_instance_ids_dedupes_and_strips(runner, tmp_path):
    """Duplicate / blank / whitespace-padded rows collapse to ordered uniques."""
    csv_path = tmp_path / "ids.csv"
    csv_path.write_text("instance_id\nrepo__a-1\n  repo__a-1  \n\nrepo__b-2\n")
    assert runner._load_instance_ids_from_csv(str(csv_path)) == [
        "repo__a-1",
        "repo__b-2",
    ]


def test_load_instance_ids_extra_columns_ok(runner, tmp_path):
    """A CSV with extra columns still works as long as instance_id is present."""
    csv_path = tmp_path / "ids.csv"
    csv_path.write_text("instance_id,language_group\nrepo__a-1,python\nrepo__b-2,go\n")
    assert runner._load_instance_ids_from_csv(str(csv_path)) == [
        "repo__a-1",
        "repo__b-2",
    ]


def test_load_instance_ids_missing_column_raises(runner, tmp_path):
    csv_path = tmp_path / "bad.csv"
    csv_path.write_text("id,foo\nrepo__a-1,x\n")
    with pytest.raises(ValueError, match="instance_id"):
        runner._load_instance_ids_from_csv(str(csv_path))


def test_load_instance_ids_empty_raises(runner, tmp_path):
    csv_path = tmp_path / "empty.csv"
    csv_path.write_text("instance_id\n")
    with pytest.raises(ValueError, match="No instance_id"):
        runner._load_instance_ids_from_csv(str(csv_path))


def test_filter_regex_matches_exactly_listed_ids(runner):
    """The regex anchors with ^...$, so it matches the listed ids and nothing
    that merely shares a prefix/suffix — the dataset loaders apply it via
    re.match, which only anchors the start."""
    rx = runner._filter_regex_from_ids(["astropy__astropy-14096", "django__django-1"])
    assert re.match(rx, "astropy__astropy-14096")
    assert re.match(rx, "django__django-1")
    # Prefix of a listed id must NOT match (the $ anchor guards the tail).
    assert not re.match(rx, "astropy__astropy-140960")
    assert not re.match(rx, "astropy__astropy-1409")
    assert not re.match(rx, "unrelated__repo-9")


def test_filter_regex_escapes_metacharacters(runner):
    """Instance ids are treated as literals — any regex metacharacter in an id
    must be escaped so it can't widen the match."""
    rx = runner._filter_regex_from_ids(["a.b+c"])
    assert re.match(rx, "a.b+c")
    assert not re.match(rx, "axbxc")  # '.' and '+' must be literal


# ---------------------------------------------------------------------------
# _count_query_tokens / _query_length_bucket / _aggregate_query_length_buckets
# ---------------------------------------------------------------------------


def test_count_query_tokens_whitespace_fallback(runner, monkeypatch):
    """When tiktoken is unavailable (sentinel False), fall back to a
    whitespace token count rather than crashing."""
    monkeypatch.setattr(runner, "_TIKTOKEN_ENCODING", False)
    assert runner._count_query_tokens("") == 0
    assert runner._count_query_tokens("one two three") == 3


@pytest.mark.parametrize(
    "n, expected",
    [
        (0, "<128"),
        (127, "<128"),
        (128, "128-512"),
        (511, "128-512"),
        (512, "512-2k"),
        (2047, "512-2k"),
        (2048, ">2k"),
        (10000, ">2k"),
    ],
)
def test_query_length_bucket_boundaries(runner, monkeypatch, n, expected):
    """Bucket edges are half-open [lo, hi): 128 lands in 128-512, 512 in
    512-2k, 2048 in >2k. Drive the count directly so the test is independent
    of whichever tokenizer is installed."""
    monkeypatch.setattr(runner, "_count_query_tokens", lambda _text: n)
    assert runner._query_length_bucket("ignored") == expected


def test_aggregate_query_length_buckets_histograms(runner):
    profiles = [
        {"query_length_bucket": "<128"},
        {"query_length_bucket": "512-2k"},
        {"query_length_bucket": "<128"},
        {"query_length_bucket": ">2k"},
    ]
    assert runner._aggregate_query_length_buckets(profiles) == {
        "<128": 2,
        "128-512": 0,
        "512-2k": 1,
        ">2k": 1,
    }


def test_aggregate_query_length_buckets_ignores_unknown_and_missing(runner):
    """All canonical buckets are present even when no instance hit them, and a
    stray/missing label is not counted (defends against silent schema drift)."""
    profiles = [
        {"query_length_bucket": "<128"},
        {"query_length_bucket": "nonsense"},
        {"no_bucket_key": True},
    ]
    out = runner._aggregate_query_length_buckets(profiles)
    assert out == {"<128": 1, "128-512": 0, "512-2k": 0, ">2k": 0}


# ---------------------------------------------------------------------------
# _map_language_group / _resolve_instance_languages  (#119 multi-language)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label, expected",
    [
        ("python", ["python"]),
        ("Python", ["python"]),
        ("rust", ["rust"]),
        ("go", ["go"]),
        ("golang", ["go"]),
        ("cpp", ["cpp"]),
        ("C++", ["cpp"]),
        ("typescript", ["ts"]),
        ("javascript", ["js"]),
        ("JavaScript/TypeScript", ["ts", "js"]),
    ],
)
def test_map_language_group_known(runner, label, expected):
    assert runner._map_language_group(label) == expected


def test_map_language_group_unknown_uses_fallback(runner):
    assert runner._map_language_group("klingon", fallback="go") == ["go"]
    assert runner._map_language_group(None) == ["python"]
    assert runner._map_language_group("") == ["python"]


def test_resolve_instance_languages_prefers_language_group(runner):
    """codeminer_base instances carry language_group; it wins over the CLI."""
    inst = {"language_group": "rust"}
    assert runner._resolve_instance_languages(inst, ["python"]) == ["rust"]


def test_resolve_instance_languages_falls_back_to_cli(runner):
    """SWE-bench instances have no language_group → use --languages verbatim."""
    assert runner._resolve_instance_languages({}, ["go", "rust"]) == ["go", "rust"]


# ---------------------------------------------------------------------------
# _build_dataset  (#119 dataset factory dispatch)
# ---------------------------------------------------------------------------


def _ds_args(dataset):
    return SimpleNamespace(
        dataset=dataset,
        split="test",
        filter_instance="^(x)$",
        repo_cache_dir="/tmp/codeminer-repos",
    )


def test_build_dataset_swebench_lite(runner, monkeypatch):
    captured = {}

    def _stub(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(**kwargs)

    monkeypatch.setattr(runner, "SwebenchDataset", _stub)
    out = runner._build_dataset(_ds_args("swebench_lite"))
    assert captured["dataset"] == "princeton-nlp/SWE-bench_Lite"
    assert captured["filter_instance"] == "^(x)$"
    assert captured["repo_root"] == "/tmp/codeminer-repos"
    assert out.split == "test"


def test_build_dataset_locbench(runner, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        runner, "LocbenchDataset", lambda **kw: captured.update(kw) or SimpleNamespace()
    )
    runner._build_dataset(_ds_args("locbench_v1"))
    assert captured["dataset"] == "czlll/Loc-Bench_V1"


def test_build_dataset_codeminer_base(runner, monkeypatch):
    """codeminer_base is imported lazily inside the factory; patch the source
    module so dispatch is verified without importing/loading the HF dataset."""
    captured = {}
    monkeypatch.setattr(
        "codeminer.dataset.codeminer_base.CodeMinerBaseDataset",
        lambda **kw: captured.update(kw) or SimpleNamespace(),
    )
    runner._build_dataset(_ds_args("codeminer_base"))
    assert captured["dataset"] == "fishmingyu/codeminer-base-dataset"
    assert captured["filter_instance"] == "^(x)$"


def test_build_dataset_unsupported_raises(runner):
    with pytest.raises(ValueError, match="Unsupported dataset"):
        runner._build_dataset(_ds_args("nope"))
