# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Pinned SWE-Explore data, prediction, and metric contracts.

SWE-Explore's released benchmark rows contain trajectory-derived regions but
not the issue text or base commit.  Reproducible evaluation therefore joins
each row to its source SWE-bench dataset before preparing a checkout.  This
module makes that join and the benchmark's two independent cutoffs explicit:
``top_k`` counts ranked regions, while Recall/nDCG budgets count source lines.

Metric behavior is compatible with SWE-Explore revision
``3c12dc5a551937038afcbdb6eb6bbf19f3ddd8c1``.
"""

from __future__ import annotations

import json
import math
import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

from codenib.integrations.swe_explore import SWEExploreContextRegion, SWEExploreResult
from codenib.languages import extension_to_language_map
from codenib.repository_filters import repository_path_is_visible

SWE_EXPLORE_UPSTREAM_REVISION = "3c12dc5a551937038afcbdb6eb6bbf19f3ddd8c1"
SWE_EXPLORE_DATASET_REVISION = "bdb0ae45d7c337d9e1dc3ebfe2a0af6bc7c1fbd9"
SWE_EXPLORE_BENCHMARK_SHA256 = (
    "dc4f114ececd0bfb987361c26ae5e2440456e2cccb36adfccb09ea5385aec202"
)
SWE_EXPLORE_SOURCE_DATASETS: Mapping[str, tuple[str, str]] = {
    "verified": (
        "princeton-nlp/SWE-bench_Verified",
        "c104f840cc67f8b6eec6f759ebc8b2693d585d4a",
    ),
    "multilingual": (
        "SWE-bench/SWE-bench_Multilingual",
        "2b7aced941b4873e9cad3e76abbae93f481d1beb",
    ),
    "pro": (
        "ScaleAI/SWE-bench_Pro",
        "2dd05cab1572ce1d59fdc699b386692ff8e0bd29",
    ),
}
SWE_EXPLORE_METRICS = (
    "precision",
    "recall",
    "f1_score",
    "hit_file_rate",
    "noise_file_rate",
    "hit_region_rate",
    "noise_region_rate",
    "weighted_core_coverage",
    "context_efficiency",
    "optional_coverage",
    "ndcg_at_100",
    "ndcg_at_300",
    "ndcg_at_500",
    "recall_at_100",
    "recall_at_300",
    "recall_at_500",
    "first_useful_hit",
)


@dataclass(frozen=True, slots=True)
class SWEExploreGroundTruth:
    """Trajectory-derived labels for one official benchmark instance."""

    read_core_files: tuple[str, ...]
    read_core_regions: tuple[SWEExploreContextRegion, ...]
    read_optional_files_map: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    read_optional_regions_map: Mapping[str, tuple[SWEExploreContextRegion, ...]] = (
        field(default_factory=dict)
    )
    modified_core_files: tuple[str, ...] = ()
    main_files: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "SWEExploreGroundTruth":
        """Validate and normalize a released ``ground_truth`` object."""

        core_files = _path_sequence(value.get("read_core_files"), "read_core_files")
        core_regions = _region_sequence(
            value.get("read_core_regions"), "read_core_regions"
        )
        if not core_files or not core_regions:
            raise ValueError("SWE-Explore ground truth must contain core labels")

        optional_files = _path_map(
            value.get("read_optional_files_map"), "read_optional_files_map"
        )
        optional_regions = _region_map(
            value.get("read_optional_regions_map"), "read_optional_regions_map"
        )
        return cls(
            read_core_files=core_files,
            read_core_regions=core_regions,
            read_optional_files_map=optional_files,
            read_optional_regions_map=optional_regions,
            modified_core_files=_path_sequence(
                value.get("modified_core_files", ()), "modified_core_files"
            ),
            main_files=_path_sequence(value.get("main_files", ()), "main_files"),
        )


@dataclass(frozen=True, slots=True)
class SWEExploreSourceRecord:
    """Issue and snapshot identity joined from a source SWE-bench dataset."""

    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str
    dataset: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(
        cls, value: Mapping[str, Any], *, dataset: str
    ) -> "SWEExploreSourceRecord":
        normalized_dataset = str(dataset).strip().lower()
        if normalized_dataset not in SWE_EXPLORE_SOURCE_DATASETS:
            raise ValueError(f"unsupported SWE-Explore source dataset: {dataset}")
        instance_id = normalize_swe_explore_instance_id(value.get("instance_id"))
        repo = str(value.get("repo") or "").strip()
        base_commit = str(value.get("base_commit") or "").strip().lower()
        problem_statement = str(value.get("problem_statement") or "").strip()
        if not instance_id or not repo or not base_commit or not problem_statement:
            raise ValueError(
                "SWE-Explore source record requires instance_id, repo, "
                "base_commit, and problem_statement"
            )
        return cls(
            instance_id=instance_id,
            repo=repo,
            base_commit=base_commit,
            problem_statement=problem_statement,
            dataset=normalized_dataset,
            metadata=dict(value),
        )


@dataclass(frozen=True, slots=True)
class SWEExploreCase:
    """One fully joined and reproducible SWE-Explore case."""

    instance_id: str
    dataset: str
    repo_dir: str
    ground_truth: SWEExploreGroundTruth
    source: SWEExploreSourceRecord | None = None
    read_step_info: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def query(self) -> str:
        return self.source.problem_statement if self.source is not None else ""

    @property
    def base_commit(self) -> str:
        return self.source.base_commit if self.source is not None else ""

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        source: SWEExploreSourceRecord | None = None,
    ) -> "SWEExploreCase":
        instance_id = normalize_swe_explore_instance_id(value.get("instance_id"))
        dataset = str(value.get("dataset") or "").strip().lower()
        if not instance_id or dataset not in SWE_EXPLORE_SOURCE_DATASETS:
            raise ValueError("invalid SWE-Explore instance_id or dataset")
        if source is not None and (
            source.instance_id != instance_id or source.dataset != dataset
        ):
            raise ValueError("SWE-Explore source record does not match benchmark row")
        repo_dir = _safe_relative_path(value.get("repo_dir"), field_name="repo_dir")
        expected_repo_dir = f"repos/{instance_id}"
        if repo_dir != expected_repo_dir:
            raise ValueError(
                f"non-canonical SWE-Explore repo_dir: {repo_dir!r}; "
                f"expected {expected_repo_dir!r}"
            )
        ground_truth = value.get("ground_truth")
        if not isinstance(ground_truth, Mapping):
            raise ValueError("SWE-Explore ground_truth must be a mapping")
        return cls(
            instance_id=instance_id,
            dataset=dataset,
            repo_dir=repo_dir,
            ground_truth=SWEExploreGroundTruth.from_mapping(ground_truth),
            source=source,
            read_step_info=(
                dict(value.get("read_step_info") or {})
                if isinstance(value.get("read_step_info") or {}, Mapping)
                else {}
            ),
            metadata=(
                dict(value.get("meta") or {})
                if isinstance(value.get("meta") or {}, Mapping)
                else {}
            ),
        )


@dataclass(frozen=True, slots=True)
class SWEExploreSnapshotAudit:
    """Observed checkout identity for one joined benchmark case."""

    repo_path: str
    expected_commit: str
    observed_commit: str | None
    revision_matches: bool | None
    is_clean: bool | None
    unexpected_source_files: tuple[str, ...] | None

    @property
    def valid(self) -> bool:
        return self.revision_matches is True and self.is_clean is True

    def to_record(self) -> dict[str, Any]:
        return {
            "repo_path": self.repo_path,
            "expected_commit": self.expected_commit,
            "observed_commit": self.observed_commit,
            "revision_matches": self.revision_matches,
            "is_clean": self.is_clean,
            "unexpected_source_files": (
                list(self.unexpected_source_files)
                if self.unexpected_source_files is not None
                else None
            ),
            "valid": self.valid,
        }


def normalize_swe_explore_instance_id(value: Any) -> str:
    """Normalize SWE-bench Pro's source-only ``instance_`` prefix."""

    instance_id = str(value or "").strip()
    return instance_id.removeprefix("instance_")


def load_swe_explore_sources(
    records_by_dataset: Mapping[str, Iterable[Mapping[str, Any]]],
) -> dict[str, SWEExploreSourceRecord]:
    """Index source rows from the three pinned SWE-bench datasets."""

    sources: dict[str, SWEExploreSourceRecord] = {}
    for dataset, records in records_by_dataset.items():
        for value in records:
            source = SWEExploreSourceRecord.from_mapping(value, dataset=dataset)
            existing = sources.get(source.instance_id)
            if existing is not None and existing != source:
                raise ValueError(
                    f"conflicting SWE-Explore source record: {source.instance_id}"
                )
            sources[source.instance_id] = source
    return sources


def load_swe_explore_cases(
    path: str | Path,
    *,
    sources: Mapping[str, SWEExploreSourceRecord] | None = None,
    require_source: bool = True,
) -> tuple[SWEExploreCase, ...]:
    """Load official JSONL rows and join their issue/snapshot identity."""

    rows: list[SWEExploreCase] = []
    seen: set[str] = set()
    with Path(path).expanduser().open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"invalid SWE-Explore JSONL at line {line_number}"
                ) from exc
            if not isinstance(value, Mapping):
                raise ValueError(f"SWE-Explore row {line_number} must be a JSON object")
            instance_id = normalize_swe_explore_instance_id(value.get("instance_id"))
            if instance_id in seen:
                raise ValueError(f"duplicate SWE-Explore instance_id: {instance_id}")
            source = sources.get(instance_id) if sources is not None else None
            if require_source and source is None:
                raise ValueError(f"missing SWE-Explore source record: {instance_id}")
            rows.append(SWEExploreCase.from_mapping(value, source=source))
            seen.add(instance_id)
    return tuple(rows)


def resolve_swe_explore_repo(case: SWEExploreCase, repos_root: str | Path) -> Path:
    """Resolve a case checkout without allowing ``repo_dir`` to escape root."""

    root = Path(repos_root).expanduser().resolve()
    relative = PurePosixPath(case.repo_dir)
    parts = relative.parts[1:] if relative.parts[:1] == ("repos",) else relative.parts
    repo = root.joinpath(*parts).resolve()
    if not repo.is_relative_to(root):
        raise ValueError(f"SWE-Explore repo_dir escapes repositories root: {repo}")
    if not repo.is_dir():
        raise FileNotFoundError(f"SWE-Explore repository snapshot is missing: {repo}")
    return repo


def audit_swe_explore_snapshot(
    case: SWEExploreCase,
    repos_root: str | Path,
) -> SWEExploreSnapshotAudit:
    """Compare a local Git checkout with the source dataset's base commit."""

    if case.source is None:
        raise ValueError("snapshot audit requires a joined SWE-Explore source record")
    repo = resolve_swe_explore_repo(case, repos_root)
    observed = _git_output(repo, "rev-parse", "HEAD")
    status = _git_output(repo, "status", "--porcelain", "--untracked-files=no")
    unexpected_source_files = _unexpected_source_files(repo)
    is_clean = (
        status == "" and not unexpected_source_files
        if status is not None and unexpected_source_files is not None
        else None
    )
    return SWEExploreSnapshotAudit(
        repo_path=str(repo),
        expected_commit=case.base_commit,
        observed_commit=observed.lower() if observed else None,
        revision_matches=(observed.lower() == case.base_commit if observed else None),
        is_clean=is_clean,
        unexpected_source_files=unexpected_source_files,
    )


def flatten_swe_explore_results(
    results: Sequence[SWEExploreResult],
    *,
    top_k: int | None = None,
) -> tuple[SWEExploreContextRegion, ...]:
    """Flatten explorer results exactly as the official runner does."""

    regions = tuple(region for result in results for region in result.regions)
    return regions if top_k is None else regions[: max(0, int(top_k))]


def swe_explore_prediction_record(
    *,
    instance_id: str,
    regions: Sequence[SWEExploreContextRegion],
    explorer: str = "codenib",
    metrics: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    """Return the JSONL row consumed and emitted by the official runner."""

    row: dict[str, Any] = {
        "instance_id": normalize_swe_explore_instance_id(instance_id),
        "explorer": str(explorer),
        "regions": [
            {"path": region.path, "start": region.start, "end": region.end}
            for region in regions
        ],
        "num_regions": len(regions),
    }
    if metrics is not None:
        row["metrics"] = {name: float(metrics[name]) for name in SWE_EXPLORE_METRICS}
    return row


def score_swe_explore_regions(
    regions: Sequence[SWEExploreContextRegion],
    ground_truth: SWEExploreGroundTruth,
    *,
    repo_path: str | Path,
) -> dict[str, float]:
    """Compute the metrics registered by the pinned official evaluator."""

    root = Path(repo_path).expanduser().resolve()
    optional_regions = tuple(
        region
        for values in ground_truth.read_optional_regions_map.values()
        for region in values
    )
    all_regions = tuple(regions) + ground_truth.read_core_regions + optional_regions
    path_to_lines = _source_line_counts(root, all_regions)
    pred_lines = _regions_to_lines(regions, path_to_lines)
    core_lines = _regions_to_lines(ground_truth.read_core_regions, path_to_lines)
    optional_lines = _regions_to_lines(optional_regions, path_to_lines)

    precision = _ratio(len(pred_lines & core_lines), len(pred_lines))
    recall = _ratio(len(pred_lines & core_lines), len(core_lines))
    f1_score = _ratio(2.0 * precision * recall, precision + recall)

    visited_files = {region.path for region in regions}
    core_files = set(ground_truth.read_core_files)
    optional_files = {
        path
        for values in ground_truth.read_optional_files_map.values()
        for path in values
    }
    hit_file_rate = _ratio(len(visited_files & core_files), len(core_files))
    noise_file_rate = _ratio(
        len(visited_files - core_files - optional_files), len(visited_files)
    )
    hit_region_rate = _ratio(
        sum(
            any(_regions_overlap(core, pred, path_to_lines) for pred in regions)
            for core in ground_truth.read_core_regions
        ),
        len(ground_truth.read_core_regions),
    )
    noise_region_rate = _ratio(
        sum(
            not any(
                _regions_overlap(pred, target, path_to_lines)
                for target in ground_truth.read_core_regions + optional_regions
            )
            for pred in regions
        ),
        len(regions),
    )

    main_files = set(ground_truth.main_files)
    weighted_sum = 0.0
    weight_total = 0.0
    for target in ground_truth.read_core_regions:
        resolved = _resolve_interval(target, path_to_lines)
        if resolved is None:
            continue
        start, end = resolved
        target_lines = {(target.path, line) for line in range(start, end + 1)}
        weight = 3.0 if target.path in main_files else 2.0
        weighted_sum += weight * _ratio(
            len(pred_lines & target_lines), len(target_lines)
        )
        weight_total += weight

    metrics = {
        "precision": precision,
        "recall": recall,
        "f1_score": f1_score,
        "hit_file_rate": hit_file_rate,
        "noise_file_rate": noise_file_rate,
        "hit_region_rate": hit_region_rate,
        "noise_region_rate": noise_region_rate,
        "weighted_core_coverage": _ratio(weighted_sum, weight_total),
        "context_efficiency": _ratio(
            len(pred_lines & (core_lines | optional_lines)), len(pred_lines)
        ),
        "optional_coverage": _ratio(
            len(pred_lines & optional_lines), len(optional_lines)
        ),
        "ndcg_at_100": _ndcg_at_line_budget(
            regions, core_lines, main_files, path_to_lines, 100
        ),
        "ndcg_at_300": _ndcg_at_line_budget(
            regions, core_lines, main_files, path_to_lines, 300
        ),
        "ndcg_at_500": _ndcg_at_line_budget(
            regions, core_lines, main_files, path_to_lines, 500
        ),
        "recall_at_100": _recall_at_line_budget(
            regions, core_lines, path_to_lines, 100
        ),
        "recall_at_300": _recall_at_line_budget(
            regions, core_lines, path_to_lines, 300
        ),
        "recall_at_500": _recall_at_line_budget(
            regions, core_lines, path_to_lines, 500
        ),
        "first_useful_hit": _first_useful_hit(regions, core_lines, path_to_lines),
    }
    return {name: metrics[name] for name in SWE_EXPLORE_METRICS}


def _safe_relative_path(value: Any, *, field_name: str) -> str:
    path = str(value or "").strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    pure = PurePosixPath(path)
    if not path or path.startswith("/") or ".." in pure.parts:
        raise ValueError(f"unsafe SWE-Explore {field_name}: {value!r}")
    return pure.as_posix()


def _path_sequence(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"SWE-Explore {field_name} must be a sequence")
    return tuple(_safe_relative_path(path, field_name=field_name) for path in value)


def _region(value: Any, field_name: str) -> SWEExploreContextRegion:
    if not isinstance(value, Mapping):
        raise ValueError(f"SWE-Explore {field_name} entries must be mappings")
    path = _safe_relative_path(value.get("path"), field_name=field_name)
    start = value.get("start")
    end = value.get("end")
    if (
        not isinstance(start, int)
        or isinstance(start, bool)
        or not isinstance(end, int)
        or isinstance(end, bool)
    ):
        raise ValueError(f"invalid SWE-Explore region: {value!r}")
    return SWEExploreContextRegion(path=path, start=start, end=end)


def _region_sequence(
    value: Any, field_name: str
) -> tuple[SWEExploreContextRegion, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"SWE-Explore {field_name} must be a sequence")
    return tuple(_region(item, field_name) for item in value)


def _path_map(value: Any, field_name: str) -> dict[str, tuple[str, ...]]:
    if not isinstance(value, Mapping):
        raise ValueError(f"SWE-Explore {field_name} must be a mapping")
    return {
        str(name): _path_sequence(paths, f"{field_name}.{name}")
        for name, paths in value.items()
    }


def _region_map(
    value: Any, field_name: str
) -> dict[str, tuple[SWEExploreContextRegion, ...]]:
    if not isinstance(value, Mapping):
        raise ValueError(f"SWE-Explore {field_name} must be a mapping")
    return {
        str(name): _region_sequence(regions, f"{field_name}.{name}")
        for name, regions in value.items()
    }


def _git_output(repo: Path, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            [
                "git",
                "-c",
                f"safe.directory={repo}",
                "-C",
                str(repo),
                *args,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.strip()


def _unexpected_source_files(repo: Path) -> tuple[str, ...] | None:
    """Return untracked or ignored files that the source chunker would index."""

    untracked = _git_paths(repo, "ls-files", "--others", "--exclude-standard", "-z")
    ignored = _git_paths(
        repo,
        "ls-files",
        "--others",
        "--ignored",
        "--exclude-standard",
        "-z",
    )
    if untracked is None or ignored is None:
        return None
    source_extensions = extension_to_language_map("chunker")
    return tuple(
        sorted(
            path
            for path in set(untracked).union(ignored)
            if repository_path_is_visible(path)
            and PurePosixPath(path).suffix.lower() in source_extensions
        )
    )


def _git_paths(repo: Path, *args: str) -> tuple[str, ...] | None:
    output = _git_output(repo, *args)
    if output is None:
        return None
    return tuple(path for path in output.split("\0") if path)


def _source_line_counts(
    repo: Path, regions: Sequence[SWEExploreContextRegion]
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in {region.path for region in regions}:
        safe_path = _safe_relative_path(path, field_name="region.path")
        source = (repo / safe_path).resolve()
        if not source.is_relative_to(repo) or not source.is_file():
            continue
        try:
            counts[safe_path] = len(
                source.read_text(encoding="utf-8", errors="replace").splitlines()
            )
        except OSError:
            continue
    return counts


def _resolve_interval(
    region: SWEExploreContextRegion,
    path_to_lines: Mapping[str, int],
) -> tuple[int, int] | None:
    total = path_to_lines.get(region.path)
    needs_total = region.end == -1 or region.start < 0
    if needs_total and (total is None or total < 1):
        return None
    if needs_total and total is not None:
        start = total + region.start + 1 if region.start < 0 else region.start
        end = total if region.end == -1 else region.end
        return max(1, min(start, total)), max(1, min(end, total))
    if region.end == -1 or region.start < 0:
        return None
    start = max(1, region.start)
    return (start, region.end) if region.end >= 1 else None


def _regions_to_lines(
    regions: Sequence[SWEExploreContextRegion],
    path_to_lines: Mapping[str, int],
) -> set[tuple[str, int]]:
    lines: set[tuple[str, int]] = set()
    for region in regions:
        resolved = _resolve_interval(region, path_to_lines)
        if resolved is None:
            continue
        start, end = resolved
        lines.update((region.path, line) for line in range(start, end + 1))
    return lines


def _regions_overlap(
    left: SWEExploreContextRegion,
    right: SWEExploreContextRegion,
    path_to_lines: Mapping[str, int],
) -> bool:
    if left.path != right.path:
        return False
    left_range = _resolve_interval(left, path_to_lines)
    right_range = _resolve_interval(right, path_to_lines)
    if left_range is None or right_range is None:
        return False
    return left_range[0] <= right_range[1] and right_range[0] <= left_range[1]


def _region_gains(
    regions: Sequence[SWEExploreContextRegion],
    core_lines: set[tuple[str, int]],
    main_files: set[str],
    path_to_lines: Mapping[str, int],
) -> tuple[list[float], list[int]]:
    gains: list[float] = []
    line_counts: list[int] = []
    for region in regions:
        resolved = _resolve_interval(region, path_to_lines)
        if resolved is None:
            gains.append(0.0)
            line_counts.append(0)
            continue
        start, end = resolved
        weight = 1.5 if region.path in main_files else 1.0
        gains.append(
            sum(
                weight
                for line in range(start, end + 1)
                if (region.path, line) in core_lines
            )
        )
        line_counts.append(end - start + 1)
    return gains, line_counts


def _dcg_with_line_budget(
    gains: Sequence[float], line_counts: Sequence[int], budget: int
) -> float:
    value = 0.0
    cumulative = 0
    for index, (gain, line_count) in enumerate(zip(gains, line_counts, strict=True)):
        cumulative += line_count
        if cumulative > budget and index > 0:
            break
        value += gain / math.log2(index + 2)
    return value


def _ndcg_at_line_budget(
    regions: Sequence[SWEExploreContextRegion],
    core_lines: set[tuple[str, int]],
    main_files: set[str],
    path_to_lines: Mapping[str, int],
    budget: int,
) -> float:
    if not regions or not core_lines:
        return 0.0
    gains, line_counts = _region_gains(regions, core_lines, main_files, path_to_lines)
    dcg = _dcg_with_line_budget(gains, line_counts, budget)
    ideal = sorted(
        zip(gains, line_counts, strict=True),
        key=lambda item: item[0] / max(item[1], 1),
        reverse=True,
    )
    ideal_dcg = _dcg_with_line_budget(
        [item[0] for item in ideal], [item[1] for item in ideal], budget
    )
    return min(dcg / ideal_dcg, 1.0) if ideal_dcg > 0 else 0.0


def _recall_at_line_budget(
    regions: Sequence[SWEExploreContextRegion],
    core_lines: set[tuple[str, int]],
    path_to_lines: Mapping[str, int],
    budget: int,
) -> float:
    if not core_lines:
        return 0.0
    covered: set[tuple[str, int]] = set()
    cumulative = 0
    for region in regions:
        resolved = _resolve_interval(region, path_to_lines)
        if resolved is None:
            continue
        start, end = resolved
        for line in range(start, end + 1):
            cumulative += 1
            location = (region.path, line)
            if location in core_lines:
                covered.add(location)
            if cumulative >= budget:
                break
        if cumulative >= budget:
            break
    return len(covered) / len(core_lines)


def _first_useful_hit(
    regions: Sequence[SWEExploreContextRegion],
    core_lines: set[tuple[str, int]],
    path_to_lines: Mapping[str, int],
) -> float:
    if not regions or not core_lines:
        return 0.0
    for index, region in enumerate(regions):
        resolved = _resolve_interval(region, path_to_lines)
        if resolved is None:
            continue
        start, end = resolved
        if any((region.path, line) in core_lines for line in range(start, end + 1)):
            return 1.0 - index / len(regions)
    return 0.0


def _ratio(numerator: float | int, denominator: float | int) -> float:
    return float(numerator) / float(denominator) if denominator else 0.0


__all__ = [
    "SWE_EXPLORE_BENCHMARK_SHA256",
    "SWE_EXPLORE_DATASET_REVISION",
    "SWE_EXPLORE_METRICS",
    "SWE_EXPLORE_SOURCE_DATASETS",
    "SWE_EXPLORE_UPSTREAM_REVISION",
    "SWEExploreCase",
    "SWEExploreGroundTruth",
    "SWEExploreSnapshotAudit",
    "SWEExploreSourceRecord",
    "audit_swe_explore_snapshot",
    "flatten_swe_explore_results",
    "load_swe_explore_cases",
    "load_swe_explore_sources",
    "normalize_swe_explore_instance_id",
    "resolve_swe_explore_repo",
    "score_swe_explore_regions",
    "swe_explore_prediction_record",
]
