#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Process-isolated gate for the Python repository-chunk successor.

The candidate adapter intentionally lands in issue #600.  Until it is present,
this controller still validates the complete fixed corpus and writes a negative
receipt; it never substitutes the established implementation for the missing
candidate.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import importlib
import json
import math
import os
import re
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_MANIFEST = Path(__file__).with_name("python_repository_chunk_subjects.json")
_DEFAULT_BUILD_DIR = _PROJECT_ROOT / "build/core-chunk-batch-poc"
_ADAPTER_MODULE = "codenib.code_chunking.python_repository_batch_poc"
_ADAPTER_FUNCTION = "run_python_repository_batch"
_BATCH_MODE_ENV = "CODENIB_NATIVE_PYTHON_CHUNK_BATCH"
_PER_FILE_MODE_ENV = "CODENIB_NATIVE_PYTHON_CHUNKER"
_EXPECTED_BACKEND = "native-repository-batch-poc"
_ARMS = ("legacy", "candidate")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_MEMORY_ERROR_EXIT = 70

DEFAULT_ITERATIONS = 20
DEFAULT_WARMUPS = 4
DEFAULT_PROMOTION_THRESHOLD = 0.20
DEFAULT_RSS_RATIO_LIMIT = 1.25
DEFAULT_WORKER_COUNTS = (1, 2, 4)
DEFAULT_WORKER_TIMEOUT_SECONDS = 900.0
CANONICAL_PEAK_RSS_SOURCE = "proc-self-status-vmhwm-kib-v1"
REPOSITORY_FILTER_POLICY = 3
REPORT_SCHEMA_VERSION = 1
_DEFAULT_OUTPUT = (
    Path(tempfile.gettempdir()) / "codenib-python-repository-chunk-gate.json"
)

CONFIGURATIONS: tuple[dict[str, Any], ...] = (
    {
        "id": "continuity_l2_exclusive_unsplit",
        "language": "python",
        "chunk_depth": 2,
        "l2_level_exclusive": True,
        "skeleton_mode": False,
        "include_header_epilogue": False,
        "max_lines_per_chunk": None,
        "filter_tests": True,
        "strict": True,
        "repository_filter_policy": REPOSITORY_FILTER_POLICY,
        "consumer_contract": "continuity",
    },
    {
        "id": "bm25_v8_l2_exclusive_headers_300",
        "language": "python",
        "chunk_depth": 2,
        "l2_level_exclusive": True,
        "skeleton_mode": False,
        "include_header_epilogue": True,
        "max_lines_per_chunk": 300,
        "filter_tests": True,
        "strict": True,
        "repository_filter_policy": REPOSITORY_FILTER_POLICY,
        "consumer_contract": "bm25-builder-schema-v8",
        "builder_schema": 8,
    },
)

_CHUNK_FIELDS = (
    "content",
    "start_line",
    "end_line",
    "chunk_type",
    "name",
    "file",
    "node_id",
)
_CANDIDATE_STAGE_FIELDS = (
    "discovery_read_wall_seconds",
    "binding_wall_seconds",
    "native_batch_wall_seconds",
    "native_parse_work_seconds",
    "native_extract_encode_work_seconds",
    "native_ordered_merge_seconds",
    "buffer_decode_wall_seconds",
    "chunk_materialization_wall_seconds",
    "node_materialization_wall_seconds",
)
_CANDIDATE_CONTRACT_SCHEMA = {
    "abi_version": 1,
    "file_row_size": 8,
    "file_row_format": "<II",
    "span_row_size": 20,
    "span_row_format": "<IIB3xII",
    "grammar": "tree-sitter-python@v0.25.0",
    "runtime": "tree-sitter@v0.25.2",
    "semantic_profile": "python-definition-spans-v1",
    "ordering": "input-ordinal-then-span-order-v1",
    "supported_worker_counts": [1, 2, 4],
    "overflow_policy": "fail-closed-v1",
}
_CANDIDATE_CONTRACT_CAPS = (
    "max_file_count",
    "max_source_bytes",
    "max_row_count",
    "max_arena_bytes",
)


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _json_digest(value: Any) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json_bytes(value)).hexdigest()}"


def _update_framed(hasher: Any, payload: bytes) -> None:
    hasher.update(len(payload).to_bytes(8, "little", signed=False))
    hasher.update(payload)


def _finite_nonnegative(value: object, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{label} must be finite and non-negative")
    return number


def _positive_integer(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _nearest_rank_p95(ordered: Sequence[float]) -> float:
    if not ordered:
        raise ValueError("at least one sample is required")
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


def summarize_samples(values: Sequence[float | int]) -> dict[str, float | int]:
    """Summarize finite non-negative values using median and nearest-rank p95."""

    if not values:
        raise ValueError("at least one sample is required")
    ordered = sorted(
        _finite_nonnegative(value, label=f"sample[{index}]")
        for index, value in enumerate(values)
    )
    return {
        "samples": len(ordered),
        "min": ordered[0],
        "p50": statistics.median(ordered),
        "p95": _nearest_rank_p95(ordered),
        "max": ordered[-1],
    }


def paired_arm_order(round_index: int) -> tuple[str, str]:
    """Alternate complete AB and BA pairs without changing sample counts."""

    if isinstance(round_index, bool) or not isinstance(round_index, int):
        raise ValueError("round_index must be an integer")
    if round_index < 0:
        raise ValueError("round_index must be non-negative")
    return _ARMS if round_index % 2 == 0 else tuple(reversed(_ARMS))


def _file_receipt(path: Path) -> dict[str, Any]:
    _, receipt = _read_stable_file(path)
    return receipt


def _read_stable_file(path: Path) -> tuple[bytes, dict[str, Any]]:
    raw_path = Path(path)
    if raw_path.is_symlink():
        raise ValueError(f"benchmark artifact must not be a symlink: {raw_path}")
    resolved = raw_path.resolve(strict=True)
    before = resolved.stat()
    content = resolved.read_bytes()
    after = resolved.stat()
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise RuntimeError(f"benchmark artifact changed while hashing: {resolved}")
    return (
        content,
        {
            "path": str(resolved),
            "size_bytes": before.st_size,
            "sha256": f"sha256:{hashlib.sha256(content).hexdigest()}",
        },
    )


def _git_output(root: Path, *arguments: str) -> str | None:
    environment = dict(os.environ)
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed.stdout.rstrip("\n") if completed.returncode == 0 else None


def _canonical_repository(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    repository = value.strip()
    ssh_match = re.fullmatch(r"git@github\.com:(.+)", repository)
    if ssh_match:
        repository = f"https://github.com/{ssh_match.group(1)}"
    if repository.endswith("/"):
        repository = repository[:-1]
    if not repository.endswith(".git"):
        repository += ".git"
    return repository


def _absolute_unresolved(path: Path) -> Path:
    expanded = path.expanduser()
    return expanded if expanded.is_absolute() else Path.cwd() / expanded


def _subject_identity(root: Path) -> dict[str, Any]:
    raw_root = _absolute_unresolved(Path(root))
    if raw_root.is_symlink():
        raise ValueError(f"subject root must not be a symlink: {raw_root}")
    resolved = raw_root.resolve(strict=True)
    if not resolved.is_dir():
        raise NotADirectoryError(resolved)
    stat_result = resolved.stat()
    toplevel = _git_output(resolved, "rev-parse", "--show-toplevel")
    commit = _git_output(resolved, "rev-parse", "--verify", "HEAD")
    status = _git_output(
        resolved,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--ignore-submodules=none",
    )
    submodules = _git_output(resolved, "submodule", "status", "--recursive")
    remote = _git_output(resolved, "remote", "get-url", "origin")
    symbolic_head = _git_output(resolved, "symbolic-ref", "-q", "HEAD")
    if toplevel is None or Path(toplevel).resolve() != resolved:
        raise ValueError(f"subject root is not the Git toplevel: {resolved}")
    return {
        "raw_root": str(raw_root),
        "resolved_root": str(resolved),
        "git_toplevel": str(Path(toplevel).resolve()),
        "commit": commit if commit and _COMMIT_RE.fullmatch(commit) else None,
        "remote": remote,
        "canonical_remote": _canonical_repository(remote),
        "status_porcelain": status,
        "submodule_status": submodules,
        "dirty": None if status is None or submodules is None else bool(status),
        "symbolic_head": symbolic_head,
        "detached": symbolic_head is None,
        "root_identity": {
            "device": stat_result.st_dev,
            "inode": stat_result.st_ino,
            "mode": stat_result.st_mode,
        },
    }


def _configuration_by_id(configuration_id: str) -> dict[str, Any]:
    matches = [row for row in CONFIGURATIONS if row["id"] == configuration_id]
    if len(matches) != 1:
        raise ValueError(f"unknown repository chunk configuration: {configuration_id}")
    return dict(matches[0])


def _new_chunker(configuration: Mapping[str, Any]) -> Any:
    from codenib.code_chunker import CodeChunker, RepoChunkingConfig

    return CodeChunker(
        language="python",
        repo_config=RepoChunkingConfig(
            languages=["python"],
            filter_tests=bool(configuration["filter_tests"]),
        ),
        max_lines_per_chunk=configuration["max_lines_per_chunk"],
        chunk_depth=int(configuration["chunk_depth"]),
        include_header_epilogue=bool(configuration["include_header_epilogue"]),
        l2_level_exclusive=bool(configuration["l2_level_exclusive"]),
        skeleton_mode=bool(configuration["skeleton_mode"]),
    )


def _source_selection_contract(configuration: Mapping[str, Any]) -> dict[str, Any]:
    from codenib.languages import extensions_for_language
    from codenib.repository_filters import DEFAULT_IGNORED_DIRS

    return {
        "receipt_schema": 1,
        "configuration": dict(configuration),
        "ordered_paths": "sorted-posix-relative-path-v1",
        "file_framing": "u64le-path-length,path,u64le-content-length,content",
        "source_bytes": "raw",
        "repo_chunking_defaults": {
            "python_extensions": sorted(extensions_for_language("python", "chunker")),
            "ignore_dirs": sorted(DEFAULT_IGNORED_DIRS),
            "include_dirs": None,
            "ignore_patterns": sorted(
                {
                    ".*",
                    "*.pyc",
                    "*.pyo",
                    "*.pyd",
                    "*.so",
                    "*.dll",
                    "*.exe",
                    "*.o",
                    "*.obj",
                    "*~",
                    "*.bak",
                    "*.tmp",
                    "*.min.js",
                    "*.min.css",
                    "*.min.mjs",
                    "*.bundle.js",
                    "*.bundle.mjs",
                }
            ),
            "max_file_size_mb": 10.0,
            "filter_tests": bool(configuration["filter_tests"]),
            "skip_minified": True,
            "minified_line_threshold": 10000,
        },
    }


def _discover_selected_python_sources(
    root: Path, configuration: Mapping[str, Any]
) -> list[Path]:
    """Mirror repository filter policy v3 without constructing a parser.

    This routine is deliberately independent of ``CodeChunker``.  Workers use
    it before the stopwatch to verify bytes without warming tree-sitter language
    or parser caches that belong inside the cold consumer boundary.
    """

    from codenib.repository_filters import repository_path_is_visible
    from codenib.utils import is_test_file

    contract = _source_selection_contract(configuration)["repo_chunking_defaults"]
    extensions = set(contract["python_extensions"])
    ignore_patterns = set(contract["ignore_patterns"])
    selected: list[Path] = []
    for raw_directory, directories, files in os.walk(root):
        directory = Path(raw_directory)
        directories[:] = [
            name
            for name in directories
            if repository_path_is_visible((directory / name).relative_to(root))
            and name not in set(contract["ignore_dirs"])
        ]
        for name in files:
            path = directory / name
            if not repository_path_is_visible(path.relative_to(root)):
                continue
            if path.suffix not in extensions:
                continue
            if any(
                (pattern.startswith("*") and name.endswith(pattern[1:]))
                or (pattern.endswith("*") and name.startswith(pattern[:-1]))
                or pattern == name
                for pattern in ignore_patterns
            ):
                continue
            if configuration["filter_tests"] and is_test_file(str(path)):
                continue
            try:
                if path.stat().st_size > contract["max_file_size_mb"] * 1024 * 1024:
                    continue
            except OSError:
                continue
            minified = False
            try:
                with path.open("r", errors="replace") as handle:
                    minified = any(
                        len(line) > contract["minified_line_threshold"]
                        for line in handle
                    )
            except OSError:
                # This matches CodeChunker: unreadable minified inspection does
                # not itself drop the path; strict source reading later fails.
                pass
            if not minified:
                selected.append(path)
    return sorted(selected, key=lambda path: path.relative_to(root).as_posix())


def _selected_source_receipt(
    root: Path, configuration: Mapping[str, Any]
) -> dict[str, Any]:
    """Bind exactly the ordered bytes selected by current repository filtering."""

    resolved_root = Path(root).resolve(strict=True)
    discovered = _discover_selected_python_sources(resolved_root, configuration)
    hasher = hashlib.sha256()
    contract = _source_selection_contract(configuration)
    _update_framed(hasher, _canonical_json_bytes(contract))
    total_bytes = 0
    paths: list[str] = []
    materialization_hasher = hashlib.sha256()
    for path in discovered:
        if path.is_symlink():
            raise ValueError(f"selected source must not be a symlink: {path}")
        relative = path.relative_to(resolved_root).as_posix()
        before = path.stat()
        content = path.read_bytes()
        after = path.stat()
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise RuntimeError(f"selected source changed while hashing: {relative}")
        encoded_path = relative.encode("utf-8", errors="surrogateescape")
        _update_framed(hasher, encoded_path)
        _update_framed(hasher, content)
        _update_framed(materialization_hasher, encoded_path)
        _update_framed(
            materialization_hasher,
            _canonical_json_bytes(
                {
                    "device": before.st_dev,
                    "inode": before.st_ino,
                    "mode": before.st_mode,
                    "size": before.st_size,
                    "mtime_ns": before.st_mtime_ns,
                }
            ),
        )
        total_bytes += len(content)
        paths.append(relative)
    if not paths:
        raise ValueError(f"configuration selected no Python sources: {resolved_root}")
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise RuntimeError("selected Python source order is not canonical")
    return {
        "schema_version": 1,
        "configuration_id": configuration["id"],
        "contract_sha256": _json_digest(contract),
        "selected_source_sha256": f"sha256:{hasher.hexdigest()}",
        "materialization_identity_sha256": (
            f"sha256:{materialization_hasher.hexdigest()}"
        ),
        "file_count": len(paths),
        "total_bytes": total_bytes,
        "first_path": paths[0],
        "last_path": paths[-1],
    }


def _assert_selector_alignment(root: Path, configuration: Mapping[str, Any]) -> None:
    """Prove the parser-free worker selector matches the production selector."""

    resolved = Path(root).resolve(strict=True)
    mirror_paths = [
        path.relative_to(resolved).as_posix()
        for path in _discover_selected_python_sources(resolved, configuration)
    ]
    production = _new_chunker(configuration)._discover_files(resolved, ["python"])
    production_paths = [
        path.relative_to(resolved).as_posix()
        for path, language in production
        if language == "python"
    ]
    if mirror_paths != production_paths:
        mismatch_index = next(
            (
                index
                for index, (left, right) in enumerate(
                    zip(mirror_paths, production_paths, strict=False)
                )
                if left != right
            ),
            min(len(mirror_paths), len(production_paths)),
        )
        raise RuntimeError(
            "parser-free selected-source filter drifted from CodeChunker "
            f"at index {mismatch_index}: mirror_count={len(mirror_paths)}, "
            f"production_count={len(production_paths)}"
        )


def _load_manifest(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    if path.is_symlink():
        raise ValueError("subject manifest must not be a symlink")
    manifest_bytes, receipt = _read_stable_file(path)
    payload = json.loads(manifest_bytes)
    if not isinstance(payload, Mapping):
        raise ValueError("subject manifest must be an object")
    if set(payload) != {
        "schema_version",
        "repository_filter_policy",
        "configurations",
        "subjects",
    }:
        raise ValueError("subject manifest has unexpected fields")
    if payload.get("schema_version") != 1:
        raise ValueError("subject manifest must use schema_version 1")
    if payload.get("repository_filter_policy") != REPOSITORY_FILTER_POLICY:
        raise ValueError("subject manifest repository filter policy changed")
    if payload.get("configurations") != [row["id"] for row in CONFIGURATIONS]:
        raise ValueError("subject manifest configuration order changed")
    subjects = payload.get("subjects")
    if not isinstance(subjects, list) or len(subjects) != 2:
        raise ValueError("subject manifest must contain exactly two subjects")
    seen: set[str] = set()
    for subject in subjects:
        if not isinstance(subject, Mapping) or set(subject) != {
            "id",
            "repository",
            "revision",
            "selected_sources",
        }:
            raise ValueError("subject manifest entry has malformed fields")
        identifier = subject.get("id")
        if not isinstance(identifier, str) or not identifier or identifier in seen:
            raise ValueError("subject manifest ids must be non-empty and unique")
        seen.add(identifier)
        repository = subject.get("repository")
        if _canonical_repository(repository) != repository:
            raise ValueError(f"subject {identifier!r} repository is not canonical")
        revision = subject.get("revision")
        if not isinstance(revision, str) or not _COMMIT_RE.fullmatch(revision):
            raise ValueError(f"subject {identifier!r} revision is malformed")
        expected = subject.get("selected_sources")
        if not isinstance(expected, Mapping) or set(expected) != set(
            payload["configurations"]
        ):
            raise ValueError(f"subject {identifier!r} selected receipts are incomplete")
        for config_id, selected in expected.items():
            if not isinstance(selected, Mapping) or set(selected) != {
                "selected_source_sha256",
                "contract_sha256",
                "file_count",
                "total_bytes",
                "first_path",
                "last_path",
            }:
                raise ValueError(
                    f"subject {identifier!r}/{config_id!r} receipt is malformed"
                )
            if not _SHA256_RE.fullmatch(str(selected["selected_source_sha256"])):
                raise ValueError("selected source digest is malformed")
            if not _SHA256_RE.fullmatch(str(selected["contract_sha256"])):
                raise ValueError("selected source contract digest is malformed")
            _positive_integer(selected["file_count"], label="selected file_count")
            _positive_integer(selected["total_bytes"], label="selected total_bytes")
    return dict(payload), receipt


def _preflight_subjects(
    manifest_path: Path, subject_roots: Mapping[str, Path]
) -> dict[str, Any]:
    manifest, manifest_receipt = _load_manifest(manifest_path)
    subject_ids = [str(subject["id"]) for subject in manifest["subjects"]]
    if set(subject_roots) != set(subject_ids):
        raise ValueError(
            "subject roots must match the manifest exactly: "
            f"expected={subject_ids}, observed={sorted(subject_roots)}"
        )
    prepared: list[dict[str, Any]] = []
    for subject in manifest["subjects"]:
        identifier = str(subject["id"])
        root = Path(subject_roots[identifier])
        identity = _subject_identity(root)
        if identity["commit"] != subject["revision"]:
            raise ValueError(
                f"subject {identifier!r} requires {subject['revision']}; "
                f"observed {identity['commit']}"
            )
        if identity["dirty"] is not False:
            raise ValueError(f"subject {identifier!r} checkout must be clean")
        if identity["detached"] is not True:
            raise ValueError(f"subject {identifier!r} checkout must be detached")
        if identity["canonical_remote"] != subject["repository"]:
            raise ValueError(f"subject {identifier!r} remote does not match")
        receipts: dict[str, Any] = {}
        for configuration in CONFIGURATIONS:
            _assert_selector_alignment(Path(identity["resolved_root"]), configuration)
            observed = _selected_source_receipt(
                Path(identity["resolved_root"]), configuration
            )
            expected = dict(subject["selected_sources"][configuration["id"]])
            comparable = {key: observed[key] for key in expected}
            if comparable != expected:
                raise ValueError(
                    f"subject {identifier!r}/{configuration['id']!r} "
                    f"selected-source receipt changed: expected={expected!r}, "
                    f"observed={comparable!r}"
                )
            receipts[str(configuration["id"])] = observed
        prepared.append(
            {
                "id": identifier,
                "manifest_entry": dict(subject),
                "input_root": str(_absolute_unresolved(root)),
                "root": identity["resolved_root"],
                "identity_before": identity,
                "selected_sources_before": receipts,
            }
        )
    return {
        "manifest": manifest,
        "manifest_receipt": manifest_receipt,
        "subjects": prepared,
    }


def _observe_subjects_after(prepared: Mapping[str, Any]) -> dict[str, Any]:
    observations: dict[str, Any] = {}
    for subject in prepared["subjects"]:
        root = Path(subject["root"])
        identity = _subject_identity(Path(subject["input_root"]))
        receipts = {
            configuration["id"]: _selected_source_receipt(root, configuration)
            for configuration in CONFIGURATIONS
        }
        observations[str(subject["id"])] = {
            "identity": identity,
            "selected_sources": receipts,
            "unchanged": (
                identity == subject["identity_before"]
                and receipts == subject["selected_sources_before"]
                and identity.get("dirty") is False
            ),
        }
    return observations


def _benchmark_identity(manifest_path: Path) -> dict[str, Any]:
    return {
        "git": _subject_identity(_PROJECT_ROOT),
        "harness": _file_receipt(Path(__file__)),
        "manifest": _file_receipt(manifest_path),
    }


def _linux_peak_rss_bytes(
    status_path: Path | None = None,
) -> int | None:
    if status_path is None:
        status_path = Path("/proc/self/status")
    try:
        lines = status_path.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError):
        return None
    for line in lines:
        if not line.startswith("VmHWM:"):
            continue
        fields = line.split()
        if len(fields) != 3 or fields[0] != "VmHWM:" or fields[2] != "kB":
            return None
        try:
            kibibytes = int(fields[1], 10)
        except ValueError:
            return None
        if kibibytes <= 0:
            return None
        return kibibytes * 1024
    return None


def _peak_rss_source() -> str:
    if sys.platform.startswith("linux"):
        return "proc-self-status-vmhwm-kib-v1"
    if sys.platform == "darwin":
        return "getrusage-self-ru-maxrss-bytes-v1"
    return "getrusage-self-ru-maxrss-kib-v1"


def _peak_rss_bytes() -> int | None:
    # Linux carries ru_maxrss across fork+exec, so it can report the
    # controller's historical high-water mark instead of this fresh worker's
    # peak. /proc/self/status VmHWM is scoped to the current executable image.
    if sys.platform.startswith("linux"):
        return _linux_peak_rss_bytes()
    try:
        import resource

        value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    except (ImportError, OSError, ValueError):
        return None
    return value if sys.platform == "darwin" else value * 1024


def _chunk_identity(chunks: Sequence[Any]) -> tuple[dict[str, Any], list[Any]]:
    hasher = hashlib.sha256()
    hasher.update(len(chunks).to_bytes(8, "little", signed=False))
    records: list[Any] = []
    for chunk in chunks:
        record: dict[str, Any] = {}
        for field in _CHUNK_FIELDS:
            value = getattr(chunk, field)
            if field in {"start_line", "end_line"}:
                if type(value) is not int or value < 0:
                    raise ValueError(f"CodeChunk {field} is malformed")
            elif type(value) is not str:
                raise ValueError(f"CodeChunk {field} must be a string")
            encoded = _canonical_json_bytes(value)
            _update_framed(hasher, encoded)
            if field == "content":
                raw = value.encode("utf-8")
                record[field] = {
                    "utf8_bytes": len(raw),
                    "sha256": f"sha256:{hashlib.sha256(raw).hexdigest()}",
                }
            else:
                record[field] = value
        records.append(record)
    return (
        {
            "schema": "ordered-seven-field-codechunk-v1",
            "count": len(chunks),
            "sha256": f"sha256:{hasher.hexdigest()}",
        },
        records,
    )


def _node_identity(nodes: Sequence[Any]) -> tuple[dict[str, Any], list[str], list[str]]:
    raw: list[str] = []
    for index, node in enumerate(nodes):
        if type(node) is not str or not node:
            raise ValueError(f"node[{index}] must be a non-empty string")
        raw.append(node)
    canonical = sorted(set(raw))

    def digest(values: Sequence[str]) -> str:
        hasher = hashlib.sha256()
        hasher.update(len(values).to_bytes(8, "little", signed=False))
        for value in values:
            _update_framed(hasher, value.encode("utf-8"))
        return f"sha256:{hasher.hexdigest()}"

    return (
        {
            "comparison": "sorted-unique-symbolic-node-id-v1",
            "raw_count": len(raw),
            "raw_sha256": digest(raw),
            "canonical_count": len(canonical),
            "canonical_sha256": digest(canonical),
        },
        raw,
        canonical,
    )


def _result_field(result: object, name: str) -> Any:
    if isinstance(result, Mapping):
        return result.get(name)
    return getattr(result, name, None)


def _worker(config: Mapping[str, Any]) -> dict[str, Any]:
    arm = config.get("arm")
    if arm not in _ARMS:
        raise ValueError(f"unknown benchmark arm: {arm!r}")
    configuration = _configuration_by_id(str(config.get("configuration_id")))
    worker_count = _positive_integer(config.get("worker_count"), label="worker_count")
    if worker_count not in DEFAULT_WORKER_COUNTS:
        raise ValueError(f"unsupported worker count: {worker_count}")
    root = Path(str(config["root"])).resolve(strict=True)

    source_before = _selected_source_receipt(root, configuration)
    expected_source = config.get("selected_source_receipt")
    if source_before != expected_source:
        raise RuntimeError("selected sources changed before worker timing")

    previous_batch_mode = os.environ.get(_BATCH_MODE_ENV)
    previous_per_file_mode = os.environ.get(_PER_FILE_MODE_ENV)
    gc_was_enabled = gc.isenabled()
    gc.collect()
    gc.disable()
    os.environ[_PER_FILE_MODE_ENV] = "off"
    os.environ[_BATCH_MODE_ENV] = "off" if arm == "legacy" else "required"
    peak_before = _peak_rss_bytes()
    cpu_started = time.process_time()
    wall_started = time.perf_counter()
    try:
        if arm == "legacy":
            chunker = _new_chunker(configuration)
            chunks = chunker.chunk_repository(str(root), strict=True)
            nodes = list(chunker.nodes)
        else:
            module = importlib.import_module(_ADAPTER_MODULE)
            adapter = getattr(module, _ADAPTER_FUNCTION, None)
            if not callable(adapter):
                raise RuntimeError(
                    f"candidate adapter {_ADAPTER_MODULE}.{_ADAPTER_FUNCTION} "
                    "is unavailable"
                )
            result = adapter(
                str(root),
                config=dict(configuration),
                mode="required",
                worker_count=worker_count,
            )
            # Result attribute access is part of the complete
            # repository result boundary.  Exact-list validation stays after
            # the stopwatch, so lazy/custom sequences fail closed rather than
            # deferring materialization outside the adapter contract.
            chunks = _result_field(result, "chunks")
            nodes = _result_field(result, "nodes")
        wall_seconds = time.perf_counter() - wall_started
        cpu_seconds = time.process_time() - cpu_started
    finally:
        if previous_batch_mode is None:
            os.environ.pop(_BATCH_MODE_ENV, None)
        else:
            os.environ[_BATCH_MODE_ENV] = previous_batch_mode
        if previous_per_file_mode is None:
            os.environ.pop(_PER_FILE_MODE_ENV, None)
        else:
            os.environ[_PER_FILE_MODE_ENV] = previous_per_file_mode
        if gc_was_enabled:
            gc.enable()
        else:
            gc.disable()

    peak_rss = _peak_rss_bytes()
    # Stop immediately after both arms have completely materialized their
    # repository result.  Contract and parity bookkeeping must remain outside
    # this symmetric consumer-boundary stopwatch.
    if arm == "legacy":
        backend = getattr(chunker._chunker, "native_chunk_backend", None)
        if backend != "python":
            raise RuntimeError(
                f"established arm selected {backend!r}; expected 'python'"
            )
        diagnostics: dict[str, Any] = {
            "backend": "python",
            "fallback_count": 0,
            "batch_call_count": 0,
            "worker_count": None,
            "contract": None,
            "stages": {},
        }
    else:
        from codenib.code_chunking.base import CodeChunk

        diagnostics = _result_field(result, "diagnostics")
        if type(chunks) is not list:
            raise TypeError("candidate chunks must materialize as a list")
        if type(nodes) is not list:
            raise TypeError("candidate nodes must materialize as a list")
        if any(type(chunk) is not CodeChunk for chunk in chunks):
            raise TypeError("candidate chunks must contain exact CodeChunk values")
        if not isinstance(diagnostics, Mapping):
            raise TypeError("candidate diagnostics must be a mapping")
        diagnostics = dict(diagnostics)

    source_after = _selected_source_receipt(root, configuration)
    chunk_identity, chunk_records = _chunk_identity(chunks)
    node_identity, raw_nodes, canonical_nodes = _node_identity(nodes)
    return {
        "arm": arm,
        "subject_id": config["subject_id"],
        "configuration_id": configuration["id"],
        "worker_count": worker_count,
        "process_id": os.getpid(),
        "wall_seconds": wall_seconds,
        "cpu_seconds": cpu_seconds,
        "peak_rss_start_bytes": peak_before,
        "peak_rss_bytes": peak_rss,
        "file_count": source_before["file_count"],
        "source_bytes": source_before["total_bytes"],
        "chunk_count": chunk_identity["count"],
        "node_count": node_identity["canonical_count"],
        "chunk_identity": chunk_identity,
        "node_identity": node_identity,
        "diagnostics": diagnostics,
        "source_receipt_before": source_before,
        "source_receipt_after": source_after,
        "gc_policy": "collect-then-disabled-during-stopwatch-v1",
        "_chunk_records": chunk_records,
        "_raw_nodes": raw_nodes,
        "_canonical_nodes": canonical_nodes,
    }


def _validate_candidate_contract(contract: object) -> dict[str, Any]:
    if not isinstance(contract, Mapping):
        raise TypeError("candidate native contract must be a mapping")
    payload = dict(contract)
    expected_keys = set(_CANDIDATE_CONTRACT_SCHEMA) | set(_CANDIDATE_CONTRACT_CAPS)
    if set(payload) != expected_keys:
        raise RuntimeError(
            "candidate native contract schema changed: "
            f"expected={sorted(expected_keys)}, observed={sorted(payload)}"
        )
    for field, expected in _CANDIDATE_CONTRACT_SCHEMA.items():
        observed = payload.get(field)
        if type(observed) is not type(expected) or observed != expected:
            raise RuntimeError(
                f"candidate native contract {field} mismatch: "
                f"expected={expected!r}, observed={observed!r}"
            )
    for field in _CANDIDATE_CONTRACT_CAPS:
        _positive_integer(payload.get(field), label=f"contract {field}")
    _canonical_json_bytes(payload)
    return payload


def _validate_file_receipt_shape(
    receipt: object, *, label: str, expected_path: Path | None = None
) -> dict[str, Any]:
    if not isinstance(receipt, Mapping) or set(receipt) != {
        "path",
        "size_bytes",
        "sha256",
    }:
        raise RuntimeError(f"candidate {label} receipt is malformed")
    payload = dict(receipt)
    path_value = payload.get("path")
    if not isinstance(path_value, str) or not Path(path_value).is_absolute():
        raise RuntimeError(f"candidate {label} receipt path is malformed")
    if expected_path is not None and Path(path_value) != expected_path:
        raise RuntimeError(f"candidate {label} receipt path changed")
    size = payload.get("size_bytes")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise RuntimeError(f"candidate {label} receipt size is malformed")
    if not _SHA256_RE.fullmatch(str(payload.get("sha256"))):
        raise RuntimeError(f"candidate {label} receipt digest is malformed")
    return payload


def _validate_candidate_receipt(
    receipt: object, *, expected_build_dir: Path
) -> dict[str, Any]:
    expected_keys = {
        "available",
        "adapter",
        "native_module_origin",
        "native_binary",
        "contract",
        "contract_sha256",
        "build_dir",
    }
    if not isinstance(receipt, Mapping) or set(receipt) != expected_keys:
        raise RuntimeError("candidate artifact receipt schema changed")
    payload = dict(receipt)
    if payload.get("available") is not True:
        raise RuntimeError("candidate receipt did not prove availability")
    resolved_build_dir = Path(expected_build_dir).resolve()
    if payload.get("build_dir") != str(resolved_build_dir):
        raise RuntimeError("candidate receipt build directory changed")
    expected_adapter = (
        _PROJECT_ROOT / "codenib/code_chunking/python_repository_batch_poc.py"
    ).resolve()
    payload["adapter"] = _validate_file_receipt_shape(
        payload["adapter"], label="adapter", expected_path=expected_adapter
    )
    native_origin = payload.get("native_module_origin")
    if not isinstance(native_origin, str) or not Path(native_origin).is_absolute():
        raise RuntimeError("candidate native module origin is malformed")
    native_path = Path(native_origin)
    if not native_path.is_relative_to(resolved_build_dir):
        raise RuntimeError("candidate native module origin escaped the build directory")
    payload["native_binary"] = _validate_file_receipt_shape(
        payload["native_binary"], label="native binary", expected_path=native_path
    )
    payload["contract"] = _validate_candidate_contract(payload["contract"])
    if payload.get("contract_sha256") != _json_digest(payload["contract"]):
        raise RuntimeError("candidate native contract digest changed")
    return payload


def _candidate_artifact_receipt(build_dir: Path) -> dict[str, Any]:
    build_dir = Path(build_dir).resolve()
    module = importlib.import_module(_ADAPTER_MODULE)
    adapter = getattr(module, _ADAPTER_FUNCTION, None)
    if not callable(adapter):
        raise RuntimeError(
            f"candidate adapter {_ADAPTER_MODULE}.{_ADAPTER_FUNCTION} is unavailable"
        )
    adapter_origin = getattr(module, "__file__", None)
    if not adapter_origin:
        raise RuntimeError("candidate adapter has no filesystem origin")
    expected_adapter = (
        _PROJECT_ROOT / "codenib/code_chunking/python_repository_batch_poc.py"
    ).resolve(strict=True)
    if Path(adapter_origin).resolve(strict=True) != expected_adapter:
        raise RuntimeError(
            f"candidate adapter resolved outside the fixed path: {adapter_origin}"
        )
    core = importlib.import_module("codenib_core")
    core_origin_value = getattr(core, "__file__", None)
    if not core_origin_value:
        raise RuntimeError("candidate native module has no filesystem origin")
    core_origin = Path(core_origin_value).resolve(strict=True)
    if not core_origin.is_relative_to(build_dir):
        raise RuntimeError(
            f"candidate native module resolved outside {build_dir}: {core_origin}"
        )
    contract_function = getattr(core, "python_chunk_batch_poc_contract", None)
    extraction_function = getattr(core, "extract_python_repository_chunk_spans", None)
    if not callable(contract_function) or not callable(extraction_function):
        raise RuntimeError("candidate native module omits repository batch APIs")
    contract_payload = _validate_candidate_contract(contract_function())
    receipt = {
        "available": True,
        "adapter": _file_receipt(expected_adapter),
        "native_module_origin": str(core_origin),
        "native_binary": _file_receipt(core_origin),
        "contract": contract_payload,
        "contract_sha256": _json_digest(contract_payload),
        "build_dir": str(build_dir),
    }
    return _validate_candidate_receipt(receipt, expected_build_dir=build_dir)


def _worker_environment(build_dir: Path) -> dict[str, str]:
    environment = dict(os.environ)
    paths = [str(Path(build_dir).resolve()), str(_PROJECT_ROOT)]
    if environment.get("PYTHONPATH"):
        paths.append(environment["PYTHONPATH"])
    environment["PYTHONPATH"] = os.pathsep.join(paths)
    environment.pop(_BATCH_MODE_ENV, None)
    environment.pop(_PER_FILE_MODE_ENV, None)
    return environment


def _run_isolated_operation(
    config: Mapping[str, Any],
    *,
    build_dir: Path,
    timeout_seconds: float,
) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--worker"],
        cwd=str(_PROJECT_ROOT),
        env=_worker_environment(build_dir),
        input=json.dumps(config, sort_keys=True, allow_nan=False),
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
    )
    if completed.returncode == _MEMORY_ERROR_EXIT:
        try:
            failure = json.loads(completed.stderr)
        except json.JSONDecodeError:
            failure = {}
        raise MemoryError(str(failure.get("message") or "worker exhausted memory"))
    if completed.returncode != 0:
        raise RuntimeError(
            f"isolated worker failed ({completed.returncode}): "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "isolated worker returned malformed JSON: "
            f"stdout={completed.stdout[-1000:]!r}; "
            f"stderr={completed.stderr[-1000:]!r}"
        ) from exc
    if not isinstance(result, dict):
        raise RuntimeError("isolated worker returned a non-object")
    return result


def _observe_candidate_isolated(
    build_dir: Path, *, timeout_seconds: float
) -> dict[str, Any]:
    return _run_isolated_operation(
        {"operation": "candidate_receipt", "build_dir": str(build_dir)},
        build_dir=build_dir,
        timeout_seconds=timeout_seconds,
    )


def _run_sample_isolated(
    config: Mapping[str, Any], *, build_dir: Path, timeout_seconds: float
) -> dict[str, Any]:
    return _run_isolated_operation(
        {"operation": "sample", **dict(config)},
        build_dir=build_dir,
        timeout_seconds=timeout_seconds,
    )


def _validate_sample(
    sample: Mapping[str, Any],
    *,
    arm: str,
    subject_id: str,
    configuration_id: str,
    worker_count: int,
    expected_source: Mapping[str, Any],
    expected_candidate_contract: Mapping[str, Any],
) -> None:
    expected_header = {
        "arm": arm,
        "subject_id": subject_id,
        "configuration_id": configuration_id,
        "worker_count": worker_count,
    }
    if any(sample.get(key) != value for key, value in expected_header.items()):
        raise RuntimeError("isolated worker returned the wrong sample identity")
    _positive_integer(sample.get("process_id"), label="process_id")
    for metric in ("wall_seconds", "cpu_seconds"):
        _finite_nonnegative(sample.get(metric), label=metric)
    _positive_integer(sample.get("peak_rss_bytes"), label="peak_rss_bytes")
    if sample.get("peak_rss_start_bytes") is not None:
        _positive_integer(
            sample.get("peak_rss_start_bytes"), label="peak_rss_start_bytes"
        )
    for metric in ("file_count", "source_bytes", "chunk_count"):
        _positive_integer(sample.get(metric), label=metric)
    node_count = sample.get("node_count")
    if (
        isinstance(node_count, bool)
        or not isinstance(node_count, int)
        or node_count < 0
    ):
        raise ValueError("node_count must be a non-negative integer")
    if sample.get("source_receipt_before") != dict(expected_source):
        raise RuntimeError("selected sources changed before an isolated sample")
    if sample.get("source_receipt_after") != dict(expected_source):
        raise RuntimeError("selected sources changed during an isolated sample")
    if sample.get("gc_policy") != "collect-then-disabled-during-stopwatch-v1":
        raise RuntimeError("isolated sample changed the GC policy")
    chunk_identity = sample.get("chunk_identity")
    node_identity = sample.get("node_identity")
    if not isinstance(chunk_identity, Mapping) or not _SHA256_RE.fullmatch(
        str(chunk_identity.get("sha256"))
    ):
        raise RuntimeError("isolated sample omitted chunk identity")
    if not isinstance(node_identity, Mapping) or not _SHA256_RE.fullmatch(
        str(node_identity.get("canonical_sha256"))
    ):
        raise RuntimeError("isolated sample omitted node identity")
    if not isinstance(sample.get("_chunk_records"), list):
        raise RuntimeError("isolated sample omitted chunk comparison records")
    if not isinstance(sample.get("_raw_nodes"), list) or not isinstance(
        sample.get("_canonical_nodes"), list
    ):
        raise RuntimeError("isolated sample omitted node comparison records")
    diagnostics = sample.get("diagnostics")
    if not isinstance(diagnostics, Mapping):
        raise RuntimeError("isolated sample omitted diagnostics")
    if arm == "legacy":
        if diagnostics != {
            "backend": "python",
            "fallback_count": 0,
            "batch_call_count": 0,
            "worker_count": None,
            "contract": None,
            "stages": {},
        }:
            raise RuntimeError("established worker diagnostics changed")
    else:
        expected_diagnostic_keys = {
            "backend",
            "fallback_count",
            "batch_call_count",
            "worker_count",
            "contract",
            "counters",
            "stages",
        }
        if set(diagnostics) != expected_diagnostic_keys or (
            diagnostics.get("backend") != _EXPECTED_BACKEND
            or diagnostics.get("fallback_count") != 0
            or diagnostics.get("batch_call_count") != 1
            or diagnostics.get("worker_count") != worker_count
            or diagnostics.get("contract") != dict(expected_candidate_contract)
        ):
            raise RuntimeError("candidate backend/fallback/batch diagnostics failed")
        stages = diagnostics.get("stages")
        if not isinstance(stages, Mapping):
            raise RuntimeError("candidate omitted stage telemetry")
        if set(stages) != set(_CANDIDATE_STAGE_FIELDS):
            raise RuntimeError("candidate stage telemetry schema changed")
        for field in _CANDIDATE_STAGE_FIELDS:
            _finite_nonnegative(stages[field], label=f"candidate stage {field}")
        counters = diagnostics.get("counters")
        expected_counter_fields = {
            "file_count",
            "source_bytes",
            "span_count",
            "chunk_count",
            "node_count",
        }
        if (
            not isinstance(counters, Mapping)
            or set(counters) != expected_counter_fields
        ):
            raise RuntimeError("candidate deterministic counter schema changed")
        for field in expected_counter_fields:
            value = counters[field]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise RuntimeError(f"candidate counter {field} is malformed")
        if (
            counters["file_count"] != sample["file_count"]
            or counters["source_bytes"] != sample["source_bytes"]
            or counters["chunk_count"] != sample["chunk_count"]
            or counters["node_count"] != sample["node_count"]
        ):
            raise RuntimeError("candidate deterministic counters differ from output")


def _first_sequence_mismatch(
    legacy: Sequence[Any], candidate: Sequence[Any], *, kind: str
) -> dict[str, Any] | None:
    if len(legacy) != len(candidate):
        return {
            "kind": kind,
            "index": min(len(legacy), len(candidate)),
            "field": "length",
            "legacy": len(legacy),
            "candidate": len(candidate),
        }
    for index, (left, right) in enumerate(zip(legacy, candidate, strict=True)):
        if left == right:
            continue
        if isinstance(left, Mapping) and isinstance(right, Mapping):
            for field in _CHUNK_FIELDS:
                if left.get(field) != right.get(field):
                    return {
                        "kind": kind,
                        "index": index,
                        "field": field,
                        "legacy": left.get(field),
                        "candidate": right.get(field),
                    }
        return {
            "kind": kind,
            "index": index,
            "field": "value",
            "legacy": left,
            "candidate": right,
        }
    return None


def _pair_parity(
    legacy: Mapping[str, Any], candidate: Mapping[str, Any]
) -> dict[str, Any]:
    chunk_mismatch = _first_sequence_mismatch(
        legacy["_chunk_records"], candidate["_chunk_records"], kind="chunk"
    )
    node_mismatch = _first_sequence_mismatch(
        legacy["_canonical_nodes"],
        candidate["_canonical_nodes"],
        kind="canonical-node",
    )
    return {
        "chunks_equal": chunk_mismatch is None
        and legacy["chunk_identity"] == candidate["chunk_identity"],
        "canonical_nodes_equal": node_mismatch is None
        and legacy["node_identity"]["canonical_sha256"]
        == candidate["node_identity"]["canonical_sha256"],
        "first_mismatch": chunk_mismatch or node_mismatch,
    }


def _public_sample(sample: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in sample.items()
        if key not in {"_chunk_records", "_raw_nodes", "_canonical_nodes"}
    }


def cell_decision(
    *,
    legacy_wall: Mapping[str, Any],
    candidate_wall: Mapping[str, Any],
    legacy_rss: Mapping[str, Any],
    candidate_rss: Mapping[str, Any],
    threshold: float,
    rss_ratio_limit: float,
    chunks_equal: bool,
    nodes_equal: bool,
    candidate_contract: bool,
    identity_unchanged: bool,
    process_ids_unique: bool,
    canonical_protocol: bool,
) -> dict[str, Any]:
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        raise ValueError("threshold must be a number")
    if not math.isfinite(threshold) or not 0 < threshold < 1:
        raise ValueError("threshold must be finite and between zero and one")
    if isinstance(rss_ratio_limit, bool) or not isinstance(
        rss_ratio_limit, (int, float)
    ):
        raise ValueError("RSS ratio limit must be a number")
    if not math.isfinite(rss_ratio_limit) or rss_ratio_limit <= 0:
        raise ValueError("RSS ratio limit must be finite and positive")
    ratio = 1.0 - threshold
    legacy_p50 = _finite_nonnegative(legacy_wall.get("p50"), label="legacy p50")
    legacy_p95 = _finite_nonnegative(legacy_wall.get("p95"), label="legacy p95")
    candidate_p50 = _finite_nonnegative(
        candidate_wall.get("p50"), label="candidate p50"
    )
    candidate_p95 = _finite_nonnegative(
        candidate_wall.get("p95"), label="candidate p95"
    )
    legacy_rss_p95 = _finite_nonnegative(legacy_rss.get("p95"), label="legacy RSS p95")
    candidate_rss_p95 = _finite_nonnegative(
        candidate_rss.get("p95"), label="candidate RSS p95"
    )
    if legacy_p50 <= 0 or legacy_p95 <= 0 or legacy_rss_p95 <= 0:
        raise ValueError("legacy p50, p95, and RSS p95 must be positive")
    gates = {
        "p50": candidate_p50 <= legacy_p50 * ratio,
        "p95": candidate_p95 <= legacy_p95 * ratio,
        "peak_rss_p95": candidate_rss_p95 <= legacy_rss_p95 * rss_ratio_limit,
        "exact_chunk_parity_every_pair": bool(chunks_equal),
        "canonical_node_parity_every_pair": bool(nodes_equal),
        "candidate_backend_contract": bool(candidate_contract),
        "identity_unchanged": bool(identity_unchanged),
        "process_ids_unique": bool(process_ids_unique),
        "canonical_protocol": bool(canonical_protocol),
    }
    return {
        "threshold": threshold,
        "required_candidate_ratio": ratio,
        "rss_ratio_limit": rss_ratio_limit,
        "p50_improvement_fraction": (legacy_p50 - candidate_p50) / legacy_p50,
        "p95_improvement_fraction": (legacy_p95 - candidate_p95) / legacy_p95,
        "peak_rss_p95_ratio": candidate_rss_p95 / legacy_rss_p95,
        "gates": gates,
        "passed": all(gates.values()),
    }


def _summarize_arm(
    samples: Sequence[Mapping[str, Any]], *, candidate: bool
) -> dict[str, Any]:
    if not samples:
        raise ValueError("at least one measured sample is required")
    summary = {
        "wall_seconds": summarize_samples(
            [sample["wall_seconds"] for sample in samples]
        ),
        "cpu_seconds": summarize_samples([sample["cpu_seconds"] for sample in samples]),
        "peak_rss_bytes": summarize_samples(
            [sample["peak_rss_bytes"] for sample in samples]
        ),
        "process_ids": [sample["process_id"] for sample in samples],
        "chunk_identity_sha256": sorted(
            {sample["chunk_identity"]["sha256"] for sample in samples}
        ),
        "canonical_node_sha256": sorted(
            {sample["node_identity"]["canonical_sha256"] for sample in samples}
        ),
    }
    if candidate:
        summary["stage_telemetry"] = {
            field: summarize_samples(
                [sample["diagnostics"]["stages"][field] for sample in samples]
            )
            for field in _CANDIDATE_STAGE_FIELDS
        }
        summary["stage_semantics"] = {
            "wall_fields": [
                field for field in _CANDIDATE_STAGE_FIELDS if "wall_seconds" in field
            ],
            "parallel_work_sum_fields": [
                "native_parse_work_seconds",
                "native_extract_encode_work_seconds",
            ],
            "work_sums_are_not_elapsed_or_subtracted_from_wall": True,
        }
    return summary


def _sample_config(
    subject: Mapping[str, Any],
    configuration: Mapping[str, Any],
    *,
    arm: str,
    worker_count: int,
    candidate_contract: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "arm": arm,
        "subject_id": subject["id"],
        "root": subject["root"],
        "configuration_id": configuration["id"],
        "worker_count": worker_count,
        "selected_source_receipt": subject["selected_sources_before"][
            configuration["id"]
        ],
        "candidate_contract": dict(candidate_contract),
    }


def _run_cell(
    subject: Mapping[str, Any],
    configuration: Mapping[str, Any],
    *,
    worker_count: int,
    iterations: int,
    warmups: int,
    threshold: float,
    rss_ratio_limit: float,
    canonical_protocol: bool,
    candidate_contract: Mapping[str, Any],
    run_sample: Callable[[Mapping[str, Any]], dict[str, Any]],
    seen_process_ids: set[int],
    all_process_ids: list[int],
) -> dict[str, Any]:
    raw_rounds: dict[str, list[dict[str, Any]]] = {
        "warmups": [],
        "measured": [],
    }
    measured_by_arm: dict[str, list[dict[str, Any]]] = {
        "legacy": [],
        "candidate": [],
    }
    chunks_equal = True
    nodes_equal = True
    candidate_contract_valid = True
    process_ids_unique = True
    first_mismatch: dict[str, Any] | None = None
    node_diagnostics: dict[str, Any] | None = None

    for phase, rounds in (("warmups", warmups), ("measured", iterations)):
        for round_index in range(rounds):
            order = paired_arm_order(round_index)
            pair: dict[str, dict[str, Any]] = {}
            for arm in order:
                config = _sample_config(
                    subject,
                    configuration,
                    arm=arm,
                    worker_count=worker_count,
                    candidate_contract=candidate_contract,
                )
                sample = run_sample(config)
                _validate_sample(
                    sample,
                    arm=arm,
                    subject_id=str(subject["id"]),
                    configuration_id=str(configuration["id"]),
                    worker_count=worker_count,
                    expected_source=config["selected_source_receipt"],
                    expected_candidate_contract=candidate_contract,
                )
                process_id = int(sample["process_id"])
                if process_id in seen_process_ids:
                    process_ids_unique = False
                seen_process_ids.add(process_id)
                all_process_ids.append(process_id)
                if arm == "candidate":
                    diagnostics = sample["diagnostics"]
                    candidate_contract_valid = candidate_contract_valid and bool(
                        diagnostics.get("backend") == _EXPECTED_BACKEND
                        and diagnostics.get("fallback_count") == 0
                        and diagnostics.get("batch_call_count") == 1
                        and diagnostics.get("worker_count") == worker_count
                    )
                pair[arm] = sample
                if phase == "measured":
                    measured_by_arm[arm].append(sample)
            parity = _pair_parity(pair["legacy"], pair["candidate"])
            chunks_equal = chunks_equal and parity["chunks_equal"]
            nodes_equal = nodes_equal and parity["canonical_nodes_equal"]
            if first_mismatch is None:
                first_mismatch = parity["first_mismatch"]
            if node_diagnostics is None:
                node_diagnostics = {
                    arm: {
                        "raw_nodes": pair[arm]["_raw_nodes"],
                        "identity": pair[arm]["node_identity"],
                    }
                    for arm in _ARMS
                }
            raw_rounds[phase].append(
                {
                    "round": round_index,
                    "arm_order": list(order),
                    "parity": parity,
                    "samples": {arm: _public_sample(pair[arm]) for arm in _ARMS},
                }
            )

    legacy_summary = _summarize_arm(measured_by_arm["legacy"], candidate=False)
    candidate_summary = _summarize_arm(measured_by_arm["candidate"], candidate=True)
    decision = cell_decision(
        legacy_wall=legacy_summary["wall_seconds"],
        candidate_wall=candidate_summary["wall_seconds"],
        legacy_rss=legacy_summary["peak_rss_bytes"],
        candidate_rss=candidate_summary["peak_rss_bytes"],
        threshold=threshold,
        rss_ratio_limit=rss_ratio_limit,
        chunks_equal=chunks_equal,
        nodes_equal=nodes_equal,
        candidate_contract=candidate_contract_valid,
        identity_unchanged=True,
        process_ids_unique=process_ids_unique,
        canonical_protocol=canonical_protocol,
    )
    return {
        "subject_id": subject["id"],
        "configuration_id": configuration["id"],
        "worker_count": worker_count,
        "legacy": legacy_summary,
        "candidate": candidate_summary,
        "parity": {
            "ordered_seven_field_chunks_every_pair": chunks_equal,
            "canonical_sorted_unique_nodes_every_pair": nodes_equal,
            "first_mismatch": first_mismatch,
            "node_diagnostics": node_diagnostics,
        },
        "candidate_contract_every_sample": candidate_contract_valid,
        "process_ids_unique": process_ids_unique,
        "decision": decision,
        "raw": raw_rounds,
    }


def _canonical_protocol(
    prepared: Mapping[str, Any],
    *,
    iterations: int,
    warmups: int,
    threshold: float,
    rss_ratio_limit: float,
    worker_counts: Sequence[int],
) -> dict[str, Any]:
    expected = {
        "iterations_per_arm": DEFAULT_ITERATIONS,
        "warmups_per_arm": DEFAULT_WARMUPS,
        "promotion_threshold": DEFAULT_PROMOTION_THRESHOLD,
        "rss_ratio_limit": DEFAULT_RSS_RATIO_LIMIT,
        "worker_counts": list(DEFAULT_WORKER_COUNTS),
        "configuration_ids": [row["id"] for row in CONFIGURATIONS],
        "subject_ids": ["codenib", "httpie"],
        "repository_filter_policy": REPOSITORY_FILTER_POLICY,
        "fresh_process_per_sample": True,
        "paired_arm_order": "alternating-ab-ba",
        "filesystem_page_cache": "uncontrolled",
        "peak_rss_source": CANONICAL_PEAK_RSS_SOURCE,
    }
    observed = {
        "iterations_per_arm": iterations,
        "warmups_per_arm": warmups,
        "promotion_threshold": threshold,
        "rss_ratio_limit": rss_ratio_limit,
        "worker_counts": list(worker_counts),
        "configuration_ids": [row["id"] for row in CONFIGURATIONS],
        "subject_ids": [row["id"] for row in prepared["subjects"]],
        "repository_filter_policy": prepared["manifest"]["repository_filter_policy"],
        "fresh_process_per_sample": True,
        "paired_arm_order": "alternating-ab-ba",
        "filesystem_page_cache": "uncontrolled",
        "peak_rss_source": _peak_rss_source(),
    }
    return {"expected": expected, "observed": observed, "passed": observed == expected}


def _json_safe_observed(value: Any) -> Any:
    """Preserve invalid CLI observations without emitting non-standard JSON."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else repr(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe_observed(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_json_safe_observed(item) for item in value]
    return repr(value)


def _base_report(
    *,
    manifest_path: Path,
    iterations: int,
    warmups: int,
    threshold: float,
    rss_ratio_limit: float,
    worker_counts: Sequence[int],
    build_dir: Path,
) -> dict[str, Any]:
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "benchmark": "python_repository_chunk_successor_gate_v1",
        "passed": False,
        "promotion_eligible": False,
        "status": "initializing",
        "failure": None,
        "configuration": {
            "iterations_per_arm": _json_safe_observed(iterations),
            "warmups_per_arm": _json_safe_observed(warmups),
            "promotion_threshold": _json_safe_observed(threshold),
            "rss_ratio_limit": _json_safe_observed(rss_ratio_limit),
            "worker_counts": _json_safe_observed(list(worker_counts)),
            "configurations": [dict(row) for row in CONFIGURATIONS],
            "percentile_method": "nearest-rank",
            "p50_method": "median",
            "arm_order": "alternating paired AB/BA rounds",
            "fresh_process_per_sample": True,
            "gc_policy": "collect-then-disabled-during-stopwatch-v1",
            "peak_rss_source": _peak_rss_source(),
            "filesystem_page_cache": "uncontrolled",
            "stopwatch_boundary": (
                "cold construction through repository discovery/filtering, "
                "minified inspection, reads, parser/worker setup, native binding, "
                "ordered merge, decode, CodeChunk and node materialization"
            ),
            "verification_boundary": {
                "controller_artifact_contract_and_subject_receipts": (
                    "outside-stopwatch"
                ),
                "candidate_runtime_contract_safety_check": (
                    "inside-candidate-stopwatch"
                ),
                "parity_backend_stage_and_report_checks": "outside-stopwatch",
            },
            "manifest": _json_safe_observed(manifest_path),
            "candidate_build_dir": _json_safe_observed(build_dir),
            "candidate_adapter": f"{_ADAPTER_MODULE}.{_ADAPTER_FUNCTION}",
        },
        "preflight": None,
        "candidate_receipts": {"before": None, "after": None, "unchanged": False},
        "canonical_protocol": None,
        "process_isolation": {
            "sample_count": 0,
            "expected_sample_count": None,
            "sample_set_complete": False,
            "unique_process_count": 0,
            "duplicate_process_ids": [],
            "passed": False,
        },
        "workers": {},
        "decision": {
            "qualifying_worker_counts": [],
            "selected_worker_count": None,
            "passed": False,
        },
    }


def _failure(report: dict[str, Any], stage: str, exc: BaseException) -> dict[str, Any]:
    report["status"] = "failed"
    report["passed"] = False
    report["promotion_eligible"] = False
    report["failure"] = {
        "stage": stage,
        "error_type": type(exc).__name__,
        "message": str(exc),
    }
    return report


def profile_python_repository_chunk_gate(
    *,
    subject_roots: Mapping[str, Path],
    manifest_path: Path = _DEFAULT_MANIFEST,
    iterations: int = DEFAULT_ITERATIONS,
    warmups: int = DEFAULT_WARMUPS,
    threshold: float = DEFAULT_PROMOTION_THRESHOLD,
    rss_ratio_limit: float = DEFAULT_RSS_RATIO_LIMIT,
    worker_counts: Sequence[int] = DEFAULT_WORKER_COUNTS,
    build_dir: Path = _DEFAULT_BUILD_DIR,
    worker_timeout_seconds: float = DEFAULT_WORKER_TIMEOUT_SECONDS,
    _sample_runner: Callable[[Mapping[str, Any]], dict[str, Any]] | None = None,
    _candidate_observer: Callable[[], dict[str, Any]] | None = None,
    _benchmark_observer: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run the fixed four-cell gate, or emit a fail-closed preflight receipt."""

    if (
        isinstance(iterations, bool)
        or not isinstance(iterations, int)
        or iterations <= 0
    ):
        raise ValueError("iterations must be a positive integer")
    if isinstance(warmups, bool) or not isinstance(warmups, int) or warmups < 0:
        raise ValueError("warmups must be a non-negative integer")
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        raise ValueError("threshold must be a number")
    if not math.isfinite(threshold) or not 0 < threshold < 1:
        raise ValueError("threshold must be finite and between zero and one")
    if isinstance(rss_ratio_limit, bool) or not isinstance(
        rss_ratio_limit, (int, float)
    ):
        raise ValueError("RSS ratio limit must be a number")
    if not math.isfinite(rss_ratio_limit) or rss_ratio_limit <= 0:
        raise ValueError("RSS ratio limit must be finite and positive")
    if not isinstance(worker_counts, Sequence) or isinstance(
        worker_counts, (str, bytes)
    ):
        raise ValueError("worker counts must be a sequence")
    if any(
        isinstance(count, bool) or not isinstance(count, int) for count in worker_counts
    ):
        raise ValueError("worker counts must be integers")
    if tuple(worker_counts) != tuple(sorted(set(worker_counts))):
        raise ValueError("worker counts must be unique and ascending")
    if any(count not in DEFAULT_WORKER_COUNTS for count in worker_counts):
        raise ValueError("worker counts must be selected from 1, 2, and 4")
    if isinstance(worker_timeout_seconds, bool) or not isinstance(
        worker_timeout_seconds, (int, float)
    ):
        raise ValueError("worker timeout must be a number")
    if not math.isfinite(worker_timeout_seconds) or worker_timeout_seconds <= 0:
        raise ValueError("worker timeout must be positive")

    report = _base_report(
        manifest_path=manifest_path,
        iterations=iterations,
        warmups=warmups,
        threshold=threshold,
        rss_ratio_limit=rss_ratio_limit,
        worker_counts=worker_counts,
        build_dir=build_dir,
    )
    benchmark_observer = _benchmark_observer or (
        lambda: _benchmark_identity(manifest_path)
    )
    try:
        benchmark_before = benchmark_observer()
        prepared = _preflight_subjects(manifest_path, subject_roots)
    except Exception as exc:
        return _failure(report, "preflight", exc)

    report["preflight"] = {
        "manifest": prepared["manifest"],
        "manifest_receipt": prepared["manifest_receipt"],
        "subjects": prepared["subjects"],
        "benchmark_before": benchmark_before,
        "subjects_after": None,
        "benchmark_after": None,
        "unchanged": False,
    }
    protocol = _canonical_protocol(
        prepared,
        iterations=iterations,
        warmups=warmups,
        threshold=threshold,
        rss_ratio_limit=rss_ratio_limit,
        worker_counts=worker_counts,
    )
    report["canonical_protocol"] = protocol

    candidate_observer = _candidate_observer or (
        lambda: _observe_candidate_isolated(
            build_dir, timeout_seconds=worker_timeout_seconds
        )
    )
    try:
        candidate_before = _validate_candidate_receipt(
            candidate_observer(), expected_build_dir=build_dir
        )
        report["candidate_receipts"]["before"] = candidate_before
    except Exception as exc:
        try:
            subjects_after = _observe_subjects_after(prepared)
            benchmark_after = benchmark_observer()
            report["preflight"]["subjects_after"] = subjects_after
            report["preflight"]["benchmark_after"] = benchmark_after
            report["preflight"]["unchanged"] = bool(
                all(row["unchanged"] for row in subjects_after.values())
                and benchmark_before == benchmark_after
                and benchmark_after.get("git", {}).get("dirty") is False
            )
        except Exception as identity_exc:
            report["preflight"]["identity_failure"] = {
                "error_type": type(identity_exc).__name__,
                "message": str(identity_exc),
            }
        return _failure(report, "candidate-preflight", exc)

    sample_runner = _sample_runner or (
        lambda config: _run_sample_isolated(
            config,
            build_dir=build_dir,
            timeout_seconds=worker_timeout_seconds,
        )
    )
    seen_process_ids: set[int] = set()
    all_process_ids: list[int] = []
    try:
        for worker_count in worker_counts:
            cells: dict[str, Any] = {}
            for subject in prepared["subjects"]:
                for configuration in CONFIGURATIONS:
                    cell = _run_cell(
                        subject,
                        configuration,
                        worker_count=worker_count,
                        iterations=iterations,
                        warmups=warmups,
                        threshold=threshold,
                        rss_ratio_limit=rss_ratio_limit,
                        canonical_protocol=bool(protocol["passed"]),
                        candidate_contract=candidate_before["contract"],
                        run_sample=sample_runner,
                        seen_process_ids=seen_process_ids,
                        all_process_ids=all_process_ids,
                    )
                    cells[f"{subject['id']}::{configuration['id']}"] = cell
            passed = len(cells) == 4 and all(
                cell["decision"]["passed"] for cell in cells.values()
            )
            report["workers"][str(worker_count)] = {
                "worker_count": worker_count,
                "cells": cells,
                "passed_all_four_cells": passed,
            }
    except Exception as exc:
        _failure(report, "measurement", exc)

    try:
        candidate_after = _validate_candidate_receipt(
            candidate_observer(), expected_build_dir=build_dir
        )
        subjects_after = _observe_subjects_after(prepared)
        benchmark_after = benchmark_observer()
        candidate_unchanged = candidate_after == candidate_before
        identity_unchanged = bool(
            all(row["unchanged"] for row in subjects_after.values())
            and benchmark_before == benchmark_after
            and benchmark_after.get("git", {}).get("dirty") is False
            and candidate_unchanged
        )
        report["candidate_receipts"].update(
            {"after": candidate_after, "unchanged": candidate_unchanged}
        )
        report["preflight"].update(
            {
                "subjects_after": subjects_after,
                "benchmark_after": benchmark_after,
                "unchanged": identity_unchanged,
            }
        )
    except Exception as exc:
        return _failure(report, "final-identity", exc)

    pid_counts: dict[int, int] = {}
    for process_id in all_process_ids:
        pid_counts[process_id] = pid_counts.get(process_id, 0) + 1
    duplicate_process_ids = sorted(
        process_id for process_id, count in pid_counts.items() if count > 1
    )
    expected_sample_count = (
        len(worker_counts)
        * len(prepared["subjects"])
        * len(CONFIGURATIONS)
        * (iterations + warmups)
        * len(_ARMS)
    )
    sample_set_complete = len(all_process_ids) == expected_sample_count
    global_process_isolation = bool(
        expected_sample_count > 0 and sample_set_complete and not duplicate_process_ids
    )
    report["process_isolation"] = {
        "sample_count": len(all_process_ids),
        "expected_sample_count": expected_sample_count,
        "sample_set_complete": sample_set_complete,
        "unique_process_count": len(pid_counts),
        "duplicate_process_ids": duplicate_process_ids,
        "passed": global_process_isolation,
    }

    # Identity and cross-cell PID uniqueness are known only after the complete
    # matrix, so fold them back into every cell before global worker selection.
    for worker in report["workers"].values():
        for cell in worker["cells"].values():
            gates = cell["decision"]["gates"]
            gates["identity_unchanged"] = identity_unchanged
            gates["process_ids_unique"] = global_process_isolation
            cell["process_ids_unique"] = global_process_isolation
            cell["decision"]["passed"] = all(gates.values())
        worker["passed_all_four_cells"] = len(worker["cells"]) == 4 and all(
            cell["decision"]["passed"] for cell in worker["cells"].values()
        )

    qualifying = []
    if report["failure"] is None:
        qualifying = [
            int(worker_count)
            for worker_count, result in report["workers"].items()
            if result["passed_all_four_cells"]
        ]
    qualifying.sort()
    selected = qualifying[0] if qualifying else None
    passed = bool(
        report["failure"] is None
        and protocol["passed"]
        and identity_unchanged
        and selected is not None
    )
    report["decision"] = {
        "selection_policy": "one-global-worker-count-smallest-qualifier-v1",
        "qualifying_worker_counts": qualifying,
        "selected_worker_count": selected,
        "passed": passed,
    }
    # ``canonical_protocol`` records whether the run is eligible to make a
    # decision.  Promotion itself additionally requires every correctness,
    # performance, RSS, identity, and isolation gate to pass.
    report["promotion_eligible"] = passed
    report["passed"] = passed
    if report["failure"] is None:
        report["status"] = "passed" if passed else "rejected"
    return report


def _atomic_write_json(path: Path, report: Mapping[str, Any]) -> None:
    encoded = (
        json.dumps(
            report,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.is_symlink():
        raise ValueError(f"output path must not be a symlink: {output}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=str(output.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
        directory_descriptor = os.open(output.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if temporary.exists():
            temporary.unlink()


def _parse_subject_roots(values: Sequence[str]) -> dict[str, Path]:
    roots: dict[str, Path] = {}
    for value in values:
        identifier, separator, raw_path = value.partition("=")
        if not separator or not identifier or not raw_path:
            raise ValueError("--subject-root must use ID=/path/to/checkout")
        if identifier in roots:
            raise ValueError(f"duplicate subject root: {identifier}")
        roots[identifier] = Path(raw_path)
    return roots


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--subject-root",
        action="append",
        default=[],
        metavar="ID=PATH",
        help="fixed subject checkout; pass once for codenib and once for httpie",
    )
    parser.add_argument("--subject-manifest", type=Path, default=_DEFAULT_MANIFEST)
    parser.add_argument("--candidate-build-dir", type=Path, default=_DEFAULT_BUILD_DIR)
    parser.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS)
    parser.add_argument("--warmups", type=int, default=DEFAULT_WARMUPS)
    parser.add_argument(
        "--promotion-threshold", type=float, default=DEFAULT_PROMOTION_THRESHOLD
    )
    parser.add_argument(
        "--rss-ratio-limit", type=float, default=DEFAULT_RSS_RATIO_LIMIT
    )
    parser.add_argument(
        "--worker-count",
        action="append",
        type=int,
        dest="worker_counts",
        help="global worker candidate; defaults to 1, 2, and 4",
    )
    parser.add_argument(
        "--worker-timeout-seconds",
        type=float,
        default=DEFAULT_WORKER_TIMEOUT_SECONDS,
    )
    parser.add_argument("--output", type=Path, default=_DEFAULT_OUTPUT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.worker:
        try:
            config = json.load(sys.stdin)
            if not isinstance(config, Mapping):
                raise ValueError("worker input must be a JSON object")
            operation = config.get("operation")
            if operation == "sample":
                result = _worker(config)
            elif operation == "candidate_receipt":
                result = _candidate_artifact_receipt(Path(str(config["build_dir"])))
            else:
                raise ValueError(f"unknown worker operation: {operation!r}")
        except MemoryError as exc:
            print(
                json.dumps(
                    {"error_type": "MemoryError", "message": str(exc)},
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
            return _MEMORY_ERROR_EXIT
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return 0

    try:
        roots = _parse_subject_roots(args.subject_root)
        worker_counts = tuple(args.worker_counts or DEFAULT_WORKER_COUNTS)
        report = profile_python_repository_chunk_gate(
            subject_roots=roots,
            manifest_path=args.subject_manifest,
            iterations=args.iterations,
            warmups=args.warmups,
            threshold=args.promotion_threshold,
            rss_ratio_limit=args.rss_ratio_limit,
            worker_counts=worker_counts,
            build_dir=args.candidate_build_dir,
            worker_timeout_seconds=args.worker_timeout_seconds,
        )
    except Exception as exc:
        report = _failure(
            _base_report(
                manifest_path=args.subject_manifest,
                iterations=args.iterations,
                warmups=args.warmups,
                threshold=args.promotion_threshold,
                rss_ratio_limit=args.rss_ratio_limit,
                worker_counts=tuple(args.worker_counts or DEFAULT_WORKER_COUNTS),
                build_dir=args.candidate_build_dir,
            ),
            "controller",
            exc,
        )
    _atomic_write_json(args.output, report)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
