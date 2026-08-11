#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Benchmark the opt-in native Python tree-sitter chunk extractor.

Both arms run the production PythonCodeChunker content assembly. The only
variable is whether parsing and definition-span traversal happen through the
existing Python tree-sitter bindings or the compact C++ proof of concept.
Measured arm order is balanced AB/BA and every iteration checks exact
CodeChunk parity.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import math
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Sequence

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_POC_BUILD_ENV = "CODENIB_PYTHON_CHUNK_POC_BUILD_DIR"
_configured_build = os.environ.get(_POC_BUILD_ENV)
_POC_BUILD = Path(
    _configured_build if _configured_build else _PROJECT_ROOT / "build/core-chunk-poc"
).expanduser()
if not _POC_BUILD.is_absolute():
    _POC_BUILD = (_PROJECT_ROOT / _POC_BUILD).resolve()
else:
    _POC_BUILD = _POC_BUILD.resolve()

# Keep the summarizer local: importing another profile script would prepend
# build/core and could silently benchmark the wrong extension. The POC build
# path itself is added lazily by _require_native_core so importing this module
# as a unit-test helper does not affect unrelated codenib_core imports.
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from codenib.code_chunker import CodeChunker, RepoChunkingConfig
from codenib.code_chunking.python_chunker import (
    PythonCodeChunker,
    _validate_native_contract,
)

DEFAULT_THRESHOLD = 0.20
DEFAULT_ITERATIONS = 6
DEFAULT_WARMUPS = 1
REPORT_SCHEMA_VERSION = 1


def summarize_samples(values: Sequence[float]) -> dict[str, float | int]:
    """Return stable distribution fields for a non-empty timing sample."""

    if not values:
        raise ValueError("at least one timing sample is required")
    ordered = sorted(float(value) for value in values)
    if any(not math.isfinite(value) or value < 0 for value in ordered):
        raise ValueError("timing samples must be finite and non-negative")
    p95_index = max(0, min(len(ordered) - 1, int(0.95 * len(ordered) - 1e-12)))
    return {
        "samples": len(ordered),
        "min_seconds": ordered[0],
        "median_seconds": statistics.median(ordered),
        "p95_seconds": ordered[p95_index],
        "max_seconds": ordered[-1],
    }


def promotion_decision(
    *,
    legacy_seconds: float,
    candidate_seconds: float,
    threshold: float = DEFAULT_THRESHOLD,
    parity: bool = True,
) -> dict[str, Any]:
    """Apply the exact repository chunk-pass promotion gate."""

    for name, value in (
        ("legacy_seconds", legacy_seconds),
        ("candidate_seconds", candidate_seconds),
    ):
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be finite and positive")
    if not math.isfinite(threshold) or not 0 < threshold < 1:
        raise ValueError("threshold must be finite and between 0 and 1")
    if type(parity) is not bool:
        raise ValueError("parity must be a bool")

    improvement = (legacy_seconds - candidate_seconds) / legacy_seconds
    promotion_cutoff = legacy_seconds * (1.0 - threshold)
    promoted = parity and candidate_seconds <= promotion_cutoff
    if promoted:
        reason = "semantic parity and end-to-end threshold passed"
    elif not parity:
        reason = "semantic parity failed"
    elif candidate_seconds > legacy_seconds:
        reason = "candidate is slower than the established chunker"
    else:
        reason = "end-to-end improvement is below threshold"
    return {
        "gate": "repository_chunk_pass_end_to_end",
        "threshold": threshold,
        "parity": parity,
        "legacy_seconds": legacy_seconds,
        "candidate_seconds": candidate_seconds,
        "promotion_cutoff_seconds": promotion_cutoff,
        "improvement_fraction": improvement,
        "promoted": promoted,
        "recommendation": (
            "promote-native-python-chunker" if promoted else "keep-experimental"
        ),
        "reason": reason,
    }


def _chunk_summary(chunk: Any) -> dict[str, Any]:
    return {
        "file": chunk.file,
        "start_line": chunk.start_line,
        "end_line": chunk.end_line,
        "chunk_type": chunk.chunk_type,
        "name": chunk.name,
        "node_id": chunk.node_id,
        "content_sha256": hashlib.sha256(chunk.content.encode("utf-8")).hexdigest(),
    }


def _assert_chunk_parity(reference: Sequence[Any], candidate: Sequence[Any]) -> None:
    if len(reference) != len(candidate):
        raise AssertionError(
            f"chunk count differs: {len(reference)} != {len(candidate)}"
        )
    for index, (left, right) in enumerate(zip(reference, candidate, strict=True)):
        if left != right:
            raise AssertionError(
                f"chunk {index} differs: {_chunk_summary(left)!r} != "
                f"{_chunk_summary(right)!r}"
            )


def _run_arm(
    *,
    chunker: PythonCodeChunker,
    files: Sequence[Path],
    repo_root: Path,
    mode: str,
) -> tuple[list[Any], float, dict[str, float]]:
    """Measure chunk calls while keeping validation/bookkeeping out of time."""

    if mode not in {"off", "required"}:
        raise ValueError("benchmark arm mode must be off or required")
    previous_mode = os.environ.get("CODENIB_NATIVE_PYTHON_CHUNKER")
    gc_was_enabled = gc.isenabled()
    gc.collect()
    gc.disable()
    os.environ["CODENIB_NATIVE_PYTHON_CHUNKER"] = mode

    stage_totals: dict[str, float] = {}
    chunks: list[Any] = []
    elapsed = 0.0
    try:
        for path in files:
            relative = path.relative_to(repo_root).as_posix()
            started = time.perf_counter()
            file_chunks = chunker.chunk_file(str(path), relative_path=relative)
            chunks.extend(file_chunks)
            elapsed += time.perf_counter() - started

            # These checks and the stage aggregation are deliberately after
            # the per-file stopwatch. They must not tax only the candidate.
            expected_backend = (
                "native-tree-sitter-poc" if mode == "required" else "python"
            )
            if chunker.native_chunk_backend != expected_backend:
                raise RuntimeError(
                    f"{mode} chunk arm selected {chunker.native_chunk_backend!r} "
                    f"for {relative}; expected {expected_backend!r}"
                )
            if mode == "required":
                for name, value in chunker.native_chunk_timings.items():
                    if not math.isfinite(value) or value < 0:
                        raise RuntimeError(
                            f"native chunk stage {name!r} is invalid: {value!r}"
                        )
                    stage_totals[name] = stage_totals.get(name, 0.0) + value
    finally:
        if previous_mode is None:
            os.environ.pop("CODENIB_NATIVE_PYTHON_CHUNKER", None)
        else:
            os.environ["CODENIB_NATIVE_PYTHON_CHUNKER"] = previous_mode
        if gc_was_enabled:
            gc.enable()
        else:
            gc.disable()
    return chunks, elapsed, stage_totals


def _arm_order(iteration: int) -> tuple[str, str]:
    """Return one half of a balanced AB/BA measurement sequence."""

    return ("off", "required") if iteration % 2 == 0 else ("required", "off")


def _discover_python_files(repo_root: Path, *, include_tests: bool) -> list[Path]:
    config = RepoChunkingConfig(
        languages=["python"],
        filter_tests=not include_tests,
    )
    driver = CodeChunker(language="python", repo_config=config, chunk_depth=2)
    return [
        path
        for path, language in driver._discover_files(repo_root, ["python"])
        if language == "python"
    ]


def _git_subject_identity(repo_root: Path) -> dict[str, str | bool | None]:
    """Return a non-mutating best-effort commit and dirty-state receipt."""

    git_environment = os.environ.copy()
    git_environment["GIT_OPTIONAL_LOCKS"] = "0"

    def run(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(repo_root), *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            env=git_environment,
        )

    try:
        revision = run("rev-parse", "--verify", "HEAD")
        if revision.returncode != 0 or not revision.stdout.strip():
            return {"git_commit": None, "git_dirty": None}
        status = run("status", "--porcelain=v1", "--untracked-files=normal")
    except (OSError, subprocess.TimeoutExpired):
        return {"git_commit": None, "git_dirty": None}
    return {
        "git_commit": revision.stdout.strip(),
        "git_dirty": None if status.returncode != 0 else bool(status.stdout),
    }


def _require_stable_subject_identity(
    before: dict[str, str | bool | None],
    after: dict[str, str | bool | None],
) -> dict[str, str | bool | None]:
    """Reject benchmark receipts if the subject changed during measurement."""

    if before != after:
        raise RuntimeError(
            "repository Git identity changed during the benchmark: "
            f"before={before!r}, after={after!r}"
        )
    return after


def _require_native_core() -> tuple[Any, dict[str, Any], Path]:
    poc_build = str(_POC_BUILD)
    while poc_build in sys.path:
        sys.path.remove(poc_build)
    sys.path.insert(0, poc_build)
    try:
        import codenib_core
    except ImportError as exc:
        raise RuntimeError(
            "native Python chunk POC is not built; run make core-chunk-poc-build"
        ) from exc

    origin_text = getattr(codenib_core, "__file__", None)
    if not origin_text:
        raise RuntimeError("native Python chunk POC module has no filesystem origin")
    origin = Path(origin_text).resolve()
    if not origin.is_relative_to(_POC_BUILD):
        raise RuntimeError(
            f"codenib_core resolved outside {_POC_BUILD}: {origin}; set "
            f"{_POC_BUILD_ENV} to the independent POC build directory"
        )
    if not callable(getattr(codenib_core, "extract_python_chunk_spans", None)):
        raise RuntimeError(
            "native Python chunk POC is not built; configure core with "
            "-DCODENIB_BUILD_TREE_SITTER_POC=ON"
        )
    return codenib_core, _validate_native_contract(codenib_core), origin


def profile_repository(
    *,
    repo_root: Path,
    iterations: int = DEFAULT_ITERATIONS,
    warmups: int = DEFAULT_WARMUPS,
    threshold: float = DEFAULT_THRESHOLD,
    chunk_depth: int = 2,
    l2_level_exclusive: bool = True,
    include_header_epilogue: bool = False,
    max_lines_per_chunk: int | None = None,
    include_tests: bool = False,
) -> dict[str, Any]:
    """Profile a filtered repository chunk pass and return a JSON-ready report."""

    if type(iterations) is not int or iterations < 2 or iterations % 2:
        raise ValueError("iterations must be a positive even integer of at least 2")
    if type(warmups) is not int or warmups < 0:
        raise ValueError("warmups must be a non-negative integer")
    if not math.isfinite(threshold) or not 0 < threshold < 1:
        raise ValueError("threshold must be finite and between 0 and 1")
    if chunk_depth not in {1, 2}:
        raise ValueError("native POC profiling supports chunk_depth 1 or 2")
    repo_root = repo_root.resolve()
    if not repo_root.is_dir():
        raise NotADirectoryError(repo_root)

    subject_identity_before = _git_subject_identity(repo_root)
    _, contract, native_module = _require_native_core()
    files = _discover_python_files(repo_root, include_tests=include_tests)
    if not files:
        raise ValueError(f"no Python source files found under {repo_root}")

    kwargs = {
        "chunk_depth": chunk_depth,
        "l2_level_exclusive": l2_level_exclusive,
        "include_header_epilogue": include_header_epilogue,
        "max_lines_per_chunk": max_lines_per_chunk,
    }
    legacy_chunker = PythonCodeChunker(**kwargs)
    candidate_chunker = PythonCodeChunker(**kwargs)
    chunkers = {"off": legacy_chunker, "required": candidate_chunker}

    for warmup in range(warmups):
        results = {
            mode: _run_arm(
                chunker=chunkers[mode],
                files=files,
                repo_root=repo_root,
                mode=mode,
            )
            for mode in _arm_order(warmup)
        }
        _assert_chunk_parity(results["off"][0], results["required"][0])

    legacy_samples: list[float] = []
    candidate_samples: list[float] = []
    stage_samples: dict[str, list[float]] = {}
    measured_rounds: list[dict[str, Any]] = []
    parity = True
    parity_error = None
    chunk_count = 0
    expected_stage_names: set[str] | None = None
    for iteration in range(iterations):
        modes = _arm_order(iteration)
        results = {
            mode: _run_arm(
                chunker=chunkers[mode],
                files=files,
                repo_root=repo_root,
                mode=mode,
            )
            for mode in modes
        }
        reference, legacy_elapsed, _ = results["off"]
        candidate, candidate_elapsed, stages = results["required"]
        try:
            _assert_chunk_parity(reference, candidate)
        except AssertionError as exc:
            if parity_error is None:
                parity_error = str(exc)
            parity = False
        chunk_count = len(reference)
        legacy_samples.append(legacy_elapsed)
        candidate_samples.append(candidate_elapsed)

        stage_names = set(stages)
        if expected_stage_names is None:
            expected_stage_names = stage_names
        elif stage_names != expected_stage_names:
            raise RuntimeError(
                "native stage names changed across benchmark iterations: "
                f"{sorted(expected_stage_names)} != {sorted(stage_names)}"
            )
        for name, value in stages.items():
            stage_samples.setdefault(name, []).append(value)
        measured_rounds.append(
            {
                "iteration": iteration + 1,
                "arm_order": [
                    "legacy" if mode == "off" else "candidate" for mode in modes
                ],
                "legacy_seconds": legacy_elapsed,
                "candidate_seconds": candidate_elapsed,
            }
        )

    legacy_summary = summarize_samples(legacy_samples)
    candidate_summary = summarize_samples(candidate_samples)
    decision = promotion_decision(
        legacy_seconds=float(legacy_summary["median_seconds"]),
        candidate_seconds=float(candidate_summary["median_seconds"]),
        threshold=threshold,
        parity=parity,
    )
    subject_identity = _require_stable_subject_identity(
        subject_identity_before,
        _git_subject_identity(repo_root),
    )
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "benchmark": "native_python_tree_sitter_chunk_poc",
        "subject": {
            "repo_root": str(repo_root),
            **subject_identity,
            "file_count": len(files),
            "source_bytes": sum(path.stat().st_size for path in files),
            "chunk_count": chunk_count,
        },
        "configuration": {
            "iterations": iterations,
            "warmups": warmups,
            "threshold": threshold,
            "arm_order_policy": "balanced-ab-ba",
            "chunk_depth": chunk_depth,
            "l2_level_exclusive": l2_level_exclusive,
            "include_header_epilogue": include_header_epilogue,
            "max_lines_per_chunk": max_lines_per_chunk,
            "include_tests": include_tests,
            "native_build_dir": str(_POC_BUILD),
            "native_module": str(native_module),
            "native_contract": contract,
        },
        "measurements": measured_rounds,
        "parity": {"passed": parity, "error": parity_error},
        "legacy": {
            "total": legacy_summary,
            "samples_seconds": legacy_samples,
        },
        "candidate": {
            "total": candidate_summary,
            "samples_seconds": candidate_samples,
            "native_stages": {
                name: {
                    **summarize_samples(values),
                    "samples_seconds": values,
                }
                for name, values in stage_samples.items()
            },
        },
        "decision": decision,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS)
    parser.add_argument("--warmups", type=int, default=DEFAULT_WARMUPS)
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--chunk-depth", type=int, choices=(1, 2), default=2)
    parser.add_argument("--include-l1-containers", action="store_true")
    parser.add_argument("--include-header-epilogue", action="store_true")
    parser.add_argument("--max-lines-per-chunk", type=int)
    parser.add_argument("--include-tests", action="store_true")
    parser.add_argument("--output-json", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = profile_repository(
        repo_root=args.repo,
        iterations=args.iterations,
        warmups=args.warmups,
        threshold=args.threshold,
        chunk_depth=args.chunk_depth,
        l2_level_exclusive=not args.include_l1_containers,
        include_header_epilogue=args.include_header_epilogue,
        max_lines_per_chunk=args.max_lines_per_chunk,
        include_tests=args.include_tests,
    )
    rendered = json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if args.output_json is not None:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)
    return 0 if report["parity"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
