#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Report-only parity and cost gate for retained storage routes.

The benchmark deliberately cannot authorize default-route promotion.  Its
checked-in policy is unratified, so a completely successful canonical run
still reports ``promotion_eligible=false``.  The report is the evidence needed
to ratify explicit per-cell budgets in a later change.

Every measured route runs in a fresh inner process.  A short-lived outer sample
worker provisions an isolated storage root and performs any required warm-cache
preparation before starting that process, keeping preparation out of Linux
``VmHWM`` and ``/proc/self/io`` observations.  The controller alternates AB/BA
pairs and validates exact BM25 artifact, authority, and public-query parity.

The manifest-backed ``runtime-cold`` cell is intentionally retained as a
compatibility sentinel: its live-source legacy arm and source-disabled retained
arm are not authority-equivalent.  ``runtime-cold-query-only`` is the comparable
runtime cost cell; it measures a direct portable artifact without ``--repo``
against the retained portable-artifact route.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import math
import os
import re
import secrets
import shutil
import signal
import stat
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_MANIFEST = Path(__file__).with_name("retained_storage_subjects.json")
_DEFAULT_OUTPUT = Path(tempfile.gettempdir()) / "codenib-retained-storage-gate.json"

BENCHMARK_ID = "retained_storage_explicit_route_gate_v2"
MANIFEST_SCHEMA_VERSION = 2
REPORT_SCHEMA_VERSION = 2
DEFAULT_ITERATIONS = 20
DEFAULT_WARMUPS = 4
DEFAULT_WORKER_TIMEOUT_SECONDS = 1800.0
CANONICAL_PEAK_RSS_SOURCE = "proc-self-status-vmhwm-kib-v1"
CANONICAL_IO_SOURCE = "proc-self-io-v1"
_CANONICAL_MANIFEST_SHA256 = (
    "sha256:2d7f95489567ef17993bd1f87b46864931645719e172731a68d15d7e7e6913cb"
)
_CANONICAL_MANIFEST_SIZE = 3164

ARMS = ("legacy", "candidate")
CELLS = (
    "compiler-cold",
    "compiler-current",
    "runtime-cold",
    "runtime-cold-query-only",
)
TRACKS = {
    "compiler": ("compiler-cold", "compiler-current"),
    "query-only-runtime": ("runtime-cold-query-only",),
    "manifest-runtime-compatibility": ("runtime-cold",),
}
CELL_AUTHORITY_CONTRACTS = {
    "compiler-cold": {
        "legacy": "compiler-cache-index",
        "candidate": "compiler-cache-index-and-retained-publication",
    },
    "compiler-current": {
        "legacy": "compiler-cache-index",
        "candidate": "compiler-cache-index-and-retained-publication",
    },
    "runtime-cold": {
        "legacy": "manifest-live-source",
        "candidate": "retained-portable-artifact-query-only",
    },
    "runtime-cold-query-only": {
        "legacy": "direct-portable-artifact-query-only-no-repo",
        "candidate": "retained-portable-artifact-query-only",
    },
}
CANONICAL_SAMPLE_COUNT = 1152
PHASES = ("warmup", "measured")
PAYLOAD_CLASSES = ("small", "medium", "large")
VIEW_SET_ID = "bm25-fast"
_VIEW_FILES = ("documents.json", "bm25_metadata.json")
_TEMP_PREFIX = ".codenib-retained-gate-"
_SHA1_RE = re.compile(r"[0-9a-f]{40}\Z")
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SOURCE_V2_RE = re.compile(r"sha256-v2:[0-9a-f]{64}\Z")
_SNAPSHOT_RE = re.compile(r"snapshot_[0-9a-f]{64}\Z")
_ID_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}\Z")
_GITHUB_REPOSITORY_RE = re.compile(
    r"https://github\.com/"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/"
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,98}[A-Za-z0-9])?\.git\Z",
    re.ASCII,
)
_REPOSITORY_KEY_RE = re.compile(
    r"[a-z0-9_.-]+(?:/[a-z0-9_.-]+)*\Z",
    re.ASCII,
)
_MAX_MANIFEST_BYTES = 1024 * 1024
_MAX_CONTEXT_MANIFEST_BYTES = 16 * 1024 * 1024
_MAX_VIEW_FILE_BYTES = 512 * 1024 * 1024
_MAX_REPORT_TEXT = 16 * 1024
_MAX_GIT_OUTPUT = 16 * 1024 * 1024
_MAX_SUBJECTS = 16
_MAX_QUERIES = 64
_MAX_QUERY_CHARS = 4096
_MAX_EXCLUSIONS = 4096
_MAX_EXCLUSION_CHARS = 4096
_MAX_WORKER_REQUEST_BYTES = 4 * 1024 * 1024
_MAX_WORKER_STDOUT_BYTES = 16 * 1024 * 1024
_MAX_WORKER_STDERR_BYTES = 1024 * 1024
_INNER_SAMPLE_FIELDS = frozenset(
    {
        "schema_version",
        "operation",
        "run_id",
        "arm",
        "phase",
        "round_index",
        "cell",
        "subject_id",
        "media_id",
        "view_set_id",
        "process_id",
        "metrics",
        "parity_identity",
        "result",
        "safety",
    }
)
_SAMPLE_REQUEST_FIELDS = frozenset(
    {
        "operation",
        "arm",
        "phase",
        "round_index",
        "cell",
        "subject",
        "subject_root",
        "media_id",
        "media_root",
        "media_identity",
        "view_set",
        "run_id",
    }
)
_METRIC_FIELDS = frozenset(
    {
        "route_wall_seconds",
        "process_wall_seconds",
        "cpu_seconds",
        "peak_rss_bytes",
        "io_read_bytes",
        "io_write_bytes",
        "payload_bytes",
        "payload_files",
    }
)
_SAFETY_FIELDS = frozenset(
    {
        "subject_unchanged",
        "sample_root_fresh",
        "cleanup_complete",
        "storage_closed",
        "context_closed",
        "ref_stable",
        "retained_matches_raw",
    }
)
_RESULT_FIELDS = frozenset(
    {"manifest", "view", "retained_view", "queries", "snapshot", "authority"}
)
_MANIFEST_IDENTITY_FIELDS = frozenset(
    {
        "commit",
        "source_fingerprint",
        "source_selection_digest",
        "languages",
        "file_count",
        "semantic_sha256",
    }
)
_VIEW_IDENTITY_FIELDS = frozenset(
    {
        "documents_sha256",
        "metadata_sha256",
        "payload_bytes",
        "payload_files",
    }
)
_QUERY_IDENTITY_FIELDS = frozenset({"sha256", "count", "nonempty"})
_SNAPSHOT_FIELDS = frozenset({"snapshot_id", "ref_name", "generation", "changed"})
_PARITY_FIELDS = frozenset({"manifest", "view", "queries", "authority"})
_AUTHORITY_IDENTITY_FIELDS = frozenset(
    {
        "context_kind",
        "artifact",
        "source_verified",
        "source_verification_scope",
    }
)
_SOURCE_SELECTION_FIELDS = frozenset(
    {"schema", "repository_filter_policy", "exclude_subtrees"}
)
_QUERY_FIELDS = frozenset({"text", "top_k", "filter_test"})
_SUBJECT_FIELDS = frozenset(
    {
        "id",
        "repository",
        "revision",
        "tree",
        "payload_class",
        "languages",
        "repository_key",
        "source_selection",
        "queries",
    }
)
_VIEW_SET_FIELDS = frozenset({"id", "views", "index_args"})
_POLICY_FIELDS = frozenset(
    {
        "status",
        "canonical_iterations",
        "canonical_warmups",
        "min_payload_classes",
        "min_media_classes",
    }
)
_MANIFEST_FIELDS = frozenset(
    {"schema_version", "benchmark", "policy", "cells", "view_sets", "subjects"}
)
_MEDIA_IDENTITY_FIELDS = frozenset(
    {"path", "device", "filesystem", "mount_source", "block_size"}
)

SampleRunner = Callable[[Mapping[str, Any]], Mapping[str, Any]]


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def _json_digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _json_snapshot(value: Any) -> Any:
    """Return an independent strict-JSON snapshot of a protocol value."""

    return json.loads(_canonical_json_bytes(value))


def _json_safe(value: Any) -> Any:
    if value is None or type(value) in {str, bool, int}:
        return value
    if type(value) is float:
        return value if math.isfinite(value) else repr(value)
    if isinstance(value, Path):
        return os.fspath(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_json_safe(item) for item in value]
    return repr(value)


def _require_exact_dict(
    value: object,
    fields: frozenset[str],
    *,
    label: str,
) -> dict[str, Any]:
    if type(value) is not dict:
        raise ValueError(f"{label} must be an exact JSON object")
    observed = set(value)
    if observed != fields:
        missing = sorted(fields - observed)
        unknown = sorted(observed - fields)
        details: list[str] = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unknown:
            details.append("unknown " + ", ".join(unknown))
        raise ValueError(f"{label} has invalid fields ({'; '.join(details)})")
    return value


def _required_text(
    value: object,
    *,
    label: str,
    maximum: int = _MAX_REPORT_TEXT,
) -> str:
    if type(value) is not str or not value or "\x00" in value:
        raise ValueError(f"{label} must be non-empty NUL-free text")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ValueError(f"{label} must be valid UTF-8 text") from exc
    if len(encoded) > maximum:
        raise ValueError(f"{label} is too large")
    return value


def _nonnegative_int(value: object, *, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _positive_int(value: object, *, label: str) -> int:
    number = _nonnegative_int(value, label=label)
    if number == 0:
        raise ValueError(f"{label} must be positive")
    return number


def _finite_nonnegative(value: object, *, label: str) -> float:
    if type(value) not in {int, float}:
        raise ValueError(f"{label} must be a number")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{label} must be finite and non-negative")
    return number


def paired_arm_order(round_index: int) -> tuple[str, str]:
    """Return one complete half of the alternating AB/BA protocol."""

    _nonnegative_int(round_index, label="round index")
    return ARMS if round_index % 2 == 0 else tuple(reversed(ARMS))


def _nearest_rank_p95(ordered: Sequence[float]) -> float:
    if not ordered:
        raise ValueError("at least one sample is required")
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


def summarize_samples(values: Sequence[float | int]) -> dict[str, float | int]:
    """Summarize finite non-negative samples with median and nearest-rank p95."""

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


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON contains duplicate key {key!r}")
        result[key] = value
    return result


def _reject_nonfinite_constant(value: str) -> None:
    raise ValueError(f"JSON contains non-finite number {value}")


def _read_stable_file(path: Path, *, maximum: int) -> tuple[bytes, dict[str, Any]]:
    lexical = Path(os.path.abspath(os.fspath(path.expanduser())))
    before = lexical.lstat()
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ValueError(f"file must be a non-symbolic regular file: {lexical}")
    if before.st_size < 0 or before.st_size > maximum:
        raise ValueError(f"file exceeds its size limit: {lexical}")
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lexical, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or opened.st_size != before.st_size
        ):
            raise RuntimeError(f"file changed while opening: {lexical}")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > maximum or len(payload) != opened.st_size:
            raise RuntimeError(f"file changed while reading: {lexical}")
        after_fd = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    after = lexical.lstat()
    identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    if identity != (
        after_fd.st_dev,
        after_fd.st_ino,
        after_fd.st_size,
        after_fd.st_mtime_ns,
    ) or identity != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise RuntimeError(f"file changed while reading: {lexical}")
    return payload, {
        "path": os.fspath(lexical),
        "device": before.st_dev,
        "inode": before.st_ino,
        "size": len(payload),
        "sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
    }


def _validate_source_selection(value: object, *, label: str) -> dict[str, Any]:
    selection = _require_exact_dict(value, _SOURCE_SELECTION_FIELDS, label=label)
    if selection["schema"] != "codenib.repository-source-selection.v1":
        raise ValueError(f"{label} has an unsupported schema")
    if selection["repository_filter_policy"] != 4:
        raise ValueError(f"{label} has an unsupported policy")
    exclusions = selection["exclude_subtrees"]
    if type(exclusions) is not list or len(exclusions) > _MAX_EXCLUSIONS:
        raise ValueError(f"{label} exclusions must be a bounded JSON array")
    normalized: list[str] = []
    for index, raw in enumerate(exclusions):
        path = _required_text(
            raw,
            label=f"{label} exclusion[{index}]",
            maximum=_MAX_EXCLUSION_CHARS,
        )
        pure = PurePosixPath(path)
        if (
            pure.is_absolute()
            or path in {".", ".."}
            or ".." in pure.parts
            or "\\" in path
            or pure.as_posix() != path
        ):
            raise ValueError(f"{label} exclusions must be canonical POSIX paths")
        normalized.append(path)
    if normalized != sorted(set(normalized)):
        raise ValueError(f"{label} exclusions must be sorted and unique")
    selected: list[str] = []
    for path in normalized:
        if any(path == parent or path.startswith(parent + "/") for parent in selected):
            raise ValueError(f"{label} exclusions must not contain redundant subtrees")
        selected.append(path)
    return {
        "schema": selection["schema"],
        "repository_filter_policy": selection["repository_filter_policy"],
        "exclude_subtrees": list(normalized),
    }


def _validate_query(value: object, *, label: str) -> dict[str, Any]:
    query = _require_exact_dict(value, _QUERY_FIELDS, label=label)
    text = _required_text(
        query["text"], label=f"{label} text", maximum=_MAX_QUERY_CHARS
    )
    top_k = _positive_int(query["top_k"], label=f"{label} top_k")
    if top_k > 50:
        raise ValueError(f"{label} top_k exceeds the benchmark limit")
    if type(query["filter_test"]) is not bool:
        raise ValueError(f"{label} filter_test must be a boolean")
    return {"text": text, "top_k": top_k, "filter_test": query["filter_test"]}


def _validate_subject(value: object, *, label: str) -> dict[str, Any]:
    subject = _require_exact_dict(value, _SUBJECT_FIELDS, label=label)
    subject_id = _required_text(subject["id"], label=f"{label} id", maximum=64)
    if not _ID_RE.fullmatch(subject_id):
        raise ValueError(f"{label} id is not canonical")
    repository = _required_text(
        subject["repository"], label=f"{label} repository", maximum=2048
    )
    if not _GITHUB_REPOSITORY_RE.fullmatch(repository):
        raise ValueError(
            f"{label} repository must be a canonical credential-free GitHub URL"
        )
    revision = _required_text(
        subject["revision"], label=f"{label} revision", maximum=40
    )
    tree = _required_text(subject["tree"], label=f"{label} tree", maximum=40)
    if not _SHA1_RE.fullmatch(revision) or not _SHA1_RE.fullmatch(tree):
        raise ValueError(f"{label} revision and tree must be lowercase Git SHAs")
    payload_class = _required_text(
        subject["payload_class"], label=f"{label} payload_class", maximum=16
    )
    if payload_class not in PAYLOAD_CLASSES:
        raise ValueError(f"{label} payload_class is unsupported")
    languages = subject["languages"]
    if (
        type(languages) is not list
        or not languages
        or len(languages) > 32
        or any(
            type(language) is not str or not _ID_RE.fullmatch(language)
            for language in languages
        )
        or languages != list(dict.fromkeys(languages))
    ):
        raise ValueError(f"{label} languages must be canonical and unique")
    repository_key = _required_text(
        subject["repository_key"], label=f"{label} repository_key", maximum=512
    )
    if not _REPOSITORY_KEY_RE.fullmatch(repository_key):
        raise ValueError(f"{label} repository_key is not canonical")
    selection = _validate_source_selection(
        subject["source_selection"], label=f"{label} source_selection"
    )
    queries = subject["queries"]
    if type(queries) is not list or not queries or len(queries) > _MAX_QUERIES:
        raise ValueError(f"{label} queries must be a bounded non-empty array")
    normalized_queries = [
        _validate_query(query, label=f"{label} query[{index}]")
        for index, query in enumerate(queries)
    ]
    if len({query["text"] for query in normalized_queries}) != len(normalized_queries):
        raise ValueError(f"{label} query texts must be unique")
    return {
        "id": subject_id,
        "repository": repository,
        "revision": revision,
        "tree": tree,
        "payload_class": payload_class,
        "languages": list(languages),
        "repository_key": repository_key,
        "source_selection": selection,
        "queries": normalized_queries,
    }


def _validate_view_set(value: object, *, label: str) -> dict[str, Any]:
    view_set = _require_exact_dict(value, _VIEW_SET_FIELDS, label=label)
    if view_set != {
        "id": VIEW_SET_ID,
        "views": ["bm25"],
        "index_args": ["--preset", "fast"],
    }:
        raise ValueError(f"{label} must be the canonical BM25 fast view set")
    return {
        "id": VIEW_SET_ID,
        "views": ["bm25"],
        "index_args": ["--preset", "fast"],
    }


def _validate_policy(value: object) -> dict[str, Any]:
    policy = _require_exact_dict(value, _POLICY_FIELDS, label="benchmark policy")
    expected = {
        "status": "unratified",
        "canonical_iterations": DEFAULT_ITERATIONS,
        "canonical_warmups": DEFAULT_WARMUPS,
        "min_payload_classes": list(PAYLOAD_CLASSES),
        "min_media_classes": 2,
    }
    if policy != expected:
        raise ValueError("benchmark policy must remain the canonical unratified policy")
    return dict(expected)


def load_subject_manifest(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load and strictly validate the fixed retained-storage subject manifest."""

    payload_bytes, receipt = _read_stable_file(path, maximum=_MAX_MANIFEST_BYTES)
    selected = Path(receipt["path"])
    default = Path(os.path.abspath(os.fspath(_DEFAULT_MANIFEST.expanduser())))
    if selected == default and (
        receipt["sha256"] != _CANONICAL_MANIFEST_SHA256
        or receipt["size"] != _CANONICAL_MANIFEST_SIZE
    ):
        raise ValueError(
            "checked-in subject manifest differs from the v2 canonical anchor"
        )
    if payload_bytes.startswith(b"\xef\xbb\xbf"):
        raise ValueError("subject manifest must not use a UTF-8 BOM")
    try:
        text = payload_bytes.decode("utf-8", errors="strict")
        raw = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite_constant,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("subject manifest is not strict UTF-8 JSON") from exc
    manifest = _require_exact_dict(raw, _MANIFEST_FIELDS, label="subject manifest")
    if manifest["schema_version"] != MANIFEST_SCHEMA_VERSION:
        raise ValueError("subject manifest schema version is unsupported")
    if manifest["benchmark"] != BENCHMARK_ID:
        raise ValueError("subject manifest benchmark identity changed")
    policy = _validate_policy(manifest["policy"])
    if manifest["cells"] != list(CELLS):
        raise ValueError("subject manifest cell order changed")
    view_sets = manifest["view_sets"]
    if type(view_sets) is not list or len(view_sets) != 1:
        raise ValueError("subject manifest must contain one view set")
    normalized_view_sets = [
        _validate_view_set(view_sets[0], label="subject manifest view_set[0]")
    ]
    subjects = manifest["subjects"]
    if type(subjects) is not list or not 1 <= len(subjects) <= _MAX_SUBJECTS:
        raise ValueError("subject manifest must contain a bounded subject array")
    normalized_subjects = [
        _validate_subject(subject, label=f"subject manifest subject[{index}]")
        for index, subject in enumerate(subjects)
    ]
    subject_ids = [subject["id"] for subject in normalized_subjects]
    if len(set(subject_ids)) != len(subject_ids):
        raise ValueError("subject manifest IDs must be unique")
    payload_classes = [subject["payload_class"] for subject in normalized_subjects]
    if sorted(payload_classes, key=PAYLOAD_CLASSES.index) != list(PAYLOAD_CLASSES):
        raise ValueError("subject manifest must contain small, medium, and large once")
    normalized = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "benchmark": BENCHMARK_ID,
        "policy": policy,
        "cells": list(CELLS),
        "view_sets": normalized_view_sets,
        "subjects": normalized_subjects,
    }
    return normalized, receipt


def write_report_atomic(path: Path, report: Mapping[str, Any]) -> None:
    """Atomically write canonical report JSON without a trailing newline."""

    if not isinstance(report, Mapping):
        raise TypeError("report must be a mapping")
    payload = _canonical_json_bytes(dict(report))
    destination = Path(os.path.abspath(os.fspath(path.expanduser())))
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    replaced = False
    try:
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("report write made no progress")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, destination)
        replaced = True
        parent_flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            parent_flags |= os.O_DIRECTORY
        if hasattr(os, "O_CLOEXEC"):
            parent_flags |= os.O_CLOEXEC
        parent_descriptor = os.open(destination.parent, parent_flags)
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if not replaced:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _run_git(
    root: Path,
    arguments: Sequence[str],
    *,
    binary: bool = False,
) -> bytes | str:
    environment = dict(os.environ)
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    completed = subprocess.run(
        ["git", "-C", os.fspath(root), *arguments],
        check=False,
        capture_output=True,
        text=not binary,
        timeout=30,
        env=environment,
    )
    output = completed.stdout
    error = completed.stderr
    output_size = len(output) if binary else len(output.encode("utf-8"))
    error_size = len(error) if binary else len(error.encode("utf-8"))
    if output_size > _MAX_GIT_OUTPUT or error_size > _MAX_GIT_OUTPUT:
        raise RuntimeError("Git command output exceeds the benchmark limit")
    if completed.returncode != 0:
        rendered = (
            error.decode("utf-8", errors="replace") if binary else str(error)
        ).strip()
        raise RuntimeError(
            f"Git {' '.join(arguments)} failed: {rendered[:_MAX_REPORT_TEXT]}"
        )
    return output


def _git_identity(root: Path) -> dict[str, Any]:
    commit = str(_run_git(root, ("rev-parse", "--verify", "HEAD"))).strip()
    tree = str(_run_git(root, ("rev-parse", "--verify", "HEAD^{tree}"))).strip()
    remote = str(_run_git(root, ("config", "--get", "remote.origin.url"))).strip()
    status_output = str(
        _run_git(
            root,
            (
                "status",
                "--porcelain=v1",
                "--untracked-files=normal",
                "--ignore-submodules=none",
            ),
        )
    )
    if not _SHA1_RE.fullmatch(commit) or not _SHA1_RE.fullmatch(tree):
        raise RuntimeError("Git returned a non-canonical commit or tree identity")
    return {
        "repository": remote,
        "commit": commit,
        "tree": tree,
        "dirty": bool(status_output),
    }


def _selection_digest(selection: Mapping[str, Any]) -> str:
    return _json_digest(dict(selection))


def _selection_allows(path: str, selection: Mapping[str, Any]) -> bool:
    return not any(
        path == excluded or path.startswith(excluded + "/")
        for excluded in selection["exclude_subtrees"]
    )


def _source_receipt(root: Path, selection: Mapping[str, Any]) -> dict[str, Any]:
    raw = _run_git(root, ("ls-files", "--stage", "-z"), binary=True)
    assert isinstance(raw, bytes)
    hasher = hashlib.sha256()
    hasher.update(_canonical_json_bytes(dict(selection)))
    selected: list[str] = []
    total_bytes = 0
    for index, record in enumerate(raw.split(b"\0")):
        if not record:
            continue
        try:
            header, raw_path = record.split(b"\t", 1)
            mode, blob, stage_text = header.split(b" ", 2)
            path = raw_path.decode("utf-8", errors="strict")
        except ValueError as exc:
            raise RuntimeError(f"Git source record {index} is malformed") from exc
        if stage_text != b"0":
            raise RuntimeError("benchmark subject has an unresolved Git index stage")
        if not _selection_allows(path, selection):
            continue
        pure = PurePosixPath(path)
        if pure.is_absolute() or ".." in pure.parts or pure.as_posix() != path:
            raise RuntimeError("Git returned a non-canonical source path")
        selected_path = root.joinpath(*pure.parts)
        metadata = selected_path.lstat()
        total_bytes += metadata.st_size
        for payload in (mode, blob, raw_path):
            hasher.update(len(payload).to_bytes(8, "little", signed=False))
            hasher.update(payload)
        selected.append(path)
    return {
        "selection_digest": _selection_digest(selection),
        "index_sha256": "sha256:" + hasher.hexdigest(),
        "file_count": len(selected),
        "total_bytes": total_bytes,
        "first_path": selected[0] if selected else None,
        "last_path": selected[-1] if selected else None,
    }


def _observe_subject(subject: Mapping[str, Any], root: Path) -> dict[str, Any]:
    return {
        "git": _git_identity(root),
        "source": _source_receipt(root, subject["source_selection"]),
    }


def _require_expected_subject(
    subject: Mapping[str, Any],
    observation: Mapping[str, Any],
) -> None:
    git = observation.get("git")
    source = observation.get("source")
    if type(git) is not dict or type(source) is not dict:
        raise RuntimeError(f"subject {subject['id']!r} observation is malformed")
    expected_git = {
        "repository": subject["repository"],
        "commit": subject["revision"],
        "tree": subject["tree"],
        "dirty": False,
    }
    if git != expected_git:
        raise RuntimeError(
            f"subject {subject['id']!r} Git identity differs from its fixed receipt"
        )
    if source.get("selection_digest") != _selection_digest(subject["source_selection"]):
        raise RuntimeError(
            f"subject {subject['id']!r} source-selection receipt changed"
        )
    if (
        type(source.get("file_count")) is not int
        or source["file_count"] <= 0
        or type(source.get("total_bytes")) is not int
        or source["total_bytes"] <= 0
        or not _SHA256_RE.fullmatch(str(source.get("index_sha256", "")))
    ):
        raise RuntimeError(f"subject {subject['id']!r} source receipt is invalid")


def _benchmark_identity(manifest_path: Path) -> dict[str, Any]:
    _, harness_receipt = _read_stable_file(Path(__file__), maximum=4 * 1024 * 1024)
    _, manifest_receipt = _read_stable_file(manifest_path, maximum=_MAX_MANIFEST_BYTES)
    return {
        "git": _git_identity(_PROJECT_ROOT),
        "harness": harness_receipt,
        "manifest": manifest_receipt,
    }


def _decode_mount_path(value: str) -> str:
    return re.sub(
        r"\\([0-7]{3})",
        lambda match: chr(int(match.group(1), 8)),
        value,
    )


def _mount_identity(path: Path) -> tuple[str, str]:
    try:
        lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return "unknown", "unknown"
    if len(lines) > 65_536:
        raise RuntimeError("Linux mount table exceeds the benchmark limit")
    selected: tuple[int, str, str] | None = None
    rendered_path = os.fspath(path)
    for line in lines:
        before, separator, after = line.partition(" - ")
        if not separator:
            continue
        left = before.split()
        right = after.split()
        if len(left) < 5 or len(right) < 2:
            continue
        mount_point = _decode_mount_path(left[4])
        try:
            contained = os.path.commonpath((rendered_path, mount_point)) == mount_point
        except ValueError:
            contained = False
        if not contained:
            continue
        candidate = (len(mount_point), right[0], _decode_mount_path(right[1]))
        if selected is None or candidate[0] > selected[0]:
            selected = candidate
    if selected is None:
        return "unknown", "unknown"
    return selected[1], selected[2]


def _media_identity(path: Path) -> dict[str, Any]:
    lexical = Path(os.path.abspath(os.fspath(path.expanduser())))
    resolved = lexical.resolve(strict=True)
    if resolved != lexical:
        raise ValueError(f"media root must not resolve through aliases: {lexical}")
    metadata = resolved.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"media root must be a non-symbolic directory: {resolved}")
    filesystem, mount_source = _mount_identity(resolved)
    filesystem_stats = os.statvfs(resolved)
    block_size = filesystem_stats.f_frsize or filesystem_stats.f_bsize
    return {
        "path": os.fspath(resolved),
        "device": metadata.st_dev,
        "filesystem": filesystem,
        "mount_source": mount_source,
        "block_size": block_size,
    }


def _validate_media_identity(value: object, *, label: str) -> dict[str, Any]:
    identity = _require_exact_dict(value, _MEDIA_IDENTITY_FIELDS, label=label)
    path = _required_text(identity["path"], label=f"{label} path")
    device = _nonnegative_int(identity["device"], label=f"{label} device")
    filesystem = _required_text(
        identity["filesystem"], label=f"{label} filesystem", maximum=256
    )
    mount_source = _required_text(
        identity["mount_source"], label=f"{label} mount_source", maximum=4096
    )
    block_size = _positive_int(identity["block_size"], label=f"{label} block_size")
    return {
        "path": path,
        "device": device,
        "filesystem": filesystem,
        "mount_source": mount_source,
        "block_size": block_size,
    }


def _paths_overlap(first: Path, second: Path) -> bool:
    try:
        common = Path(os.path.commonpath((first, second)))
    except ValueError:
        return False
    return common == first or common == second


def _real_directory(path: Path, *, label: str) -> Path:
    lexical = Path(os.path.abspath(os.fspath(path.expanduser())))
    resolved = lexical.resolve(strict=True)
    metadata = lexical.lstat()
    if (
        resolved != lexical
        or stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
    ):
        raise ValueError(f"{label} must be a real directory without aliases")
    return resolved


def _reject_overlapping_roots(
    roots: Mapping[str, Path],
    *,
    label: str,
) -> None:
    ordered = sorted(roots.items())
    for index, (first_id, first) in enumerate(ordered):
        for second_id, second in ordered[index + 1 :]:
            if _paths_overlap(first, second):
                raise ValueError(f"{label} {first_id!r} overlaps {label} {second_id!r}")


def _peak_rss_source() -> str:
    return (
        CANONICAL_PEAK_RSS_SOURCE if sys.platform.startswith("linux") else "unsupported"
    )


def _io_source() -> str:
    return CANONICAL_IO_SOURCE if sys.platform.startswith("linux") else "unsupported"


def _empty_tracks() -> dict[str, dict[str, Any]]:
    return {
        track: {
            "cells": list(cells),
            "measurement_complete": False,
            "parity_passed": False,
            "safety_passed": False,
            "passed": False,
            "policy_status": "unratified",
            "promotion_eligible": False,
        }
        for track, cells in TRACKS.items()
    }


def _base_report(
    *,
    manifest_path: Path,
    iterations: object,
    warmups: object,
    worker_timeout_seconds: object,
    subject_roots: object,
    media_roots: object,
) -> dict[str, Any]:
    rendered_subjects = (
        {
            str(key): _json_safe(value)
            for key, value in subject_roots.items()  # type: ignore[union-attr]
        }
        if isinstance(subject_roots, Mapping)
        else _json_safe(subject_roots)
    )
    rendered_media = (
        {
            str(key): _json_safe(value)
            for key, value in media_roots.items()  # type: ignore[union-attr]
        }
        if isinstance(media_roots, Mapping)
        else _json_safe(media_roots)
    )
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "benchmark": BENCHMARK_ID,
        "status": "initializing",
        "passed": False,
        "promotion_eligible": False,
        "failure": None,
        "policy": {
            "status": "unratified",
            "performance_budgets": None,
            "promotion_eligible": False,
        },
        "protocol": None,
        "configuration": {
            "manifest": _json_safe(manifest_path),
            "iterations_per_arm": _json_safe(iterations),
            "warmups_per_arm": _json_safe(warmups),
            "worker_timeout_seconds": _json_safe(worker_timeout_seconds),
            "subject_roots": rendered_subjects,
            "media_roots": rendered_media,
            "p50_method": "median",
            "p95_method": "nearest-rank",
            "stopwatch_boundaries": {
                "compiler-cold": (
                    "fresh-inner-codenib-cli-import-parser-index-handler-through-return"
                ),
                "compiler-current": (
                    "fresh-inner-codenib-cli-import-parser-index-handler-through-return"
                ),
                "runtime-cold": (
                    "fresh-inner-import-parser-handler-through-ready-callback-"
                    "fixed-queries-and-normal-cleanup-return"
                ),
                "runtime-cold-query-only": (
                    "fresh-inner-import-parser-direct-or-retained-portable-"
                    "artifact-handler-through-ready-callback-fixed-queries-and-"
                    "normal-cleanup-return"
                ),
            },
            "process_wall_boundary": (
                "full-inner-subprocess-lifecycle-including-post-timing-parity"
            ),
            "filesystem_page_cache": "uncontrolled",
            "cold_definitions": {
                "compiler-cold": "empty-codenib-cache",
                "runtime-cold": "fresh-process-and-context",
                "runtime-cold-query-only": "fresh-process-and-context",
            },
            "cell_authority_contracts": _json_snapshot(CELL_AUTHORITY_CONTRACTS),
        },
        "benchmark_receipts": {
            "before": None,
            "after": None,
            "unchanged": False,
        },
        "subjects": {},
        "media": {},
        "cells": {},
        "tracks": _empty_tracks(),
        "process_isolation": {
            "expected_samples": 0,
            "observed_samples": 0,
            "inner_process_ids": [],
            "duplicate_process_ids": [],
            "passed": False,
        },
        "decision": {
            "policy_status": "unratified",
            "report_only": True,
            "promotion_eligible": False,
            "recommendation": "retain-explicit-routes",
            "reason": "performance budgets are unratified",
        },
    }


def _failure(report: dict[str, Any], stage: str, exc: BaseException) -> dict[str, Any]:
    report["status"] = "failed"
    report["passed"] = False
    report["promotion_eligible"] = False
    report["failure"] = {
        "stage": stage,
        "error_type": type(exc).__name__,
        "message": str(exc)[:_MAX_REPORT_TEXT],
    }
    return report


def _normalize_root_mapping(
    value: object,
    *,
    label: str,
) -> dict[str, Path]:
    if not isinstance(value, Mapping) or not value:
        raise ValueError(f"{label} must be a non-empty mapping")
    normalized: dict[str, Path] = {}
    for raw_key, raw_path in value.items():
        key = _required_text(raw_key, label=f"{label} ID", maximum=64)
        if not _ID_RE.fullmatch(key) or key in normalized:
            raise ValueError(f"{label} IDs must be canonical and unique")
        if not isinstance(raw_path, Path):
            raise TypeError(f"{label} paths must be pathlib.Path instances")
        lexical = Path(os.path.abspath(os.fspath(raw_path.expanduser())))
        resolved = lexical.resolve(strict=True)
        if resolved != lexical:
            raise ValueError(
                f"{label} path must not resolve through aliases: {lexical}"
            )
        metadata = resolved.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f"{label} path must be a real directory: {resolved}")
        normalized[key] = resolved
    return normalized


def _protocol(
    manifest: Mapping[str, Any],
    *,
    manifest_receipt: Mapping[str, Any],
    iterations: int,
    warmups: int,
    media: Mapping[str, Mapping[str, Any]],
    built_in_runner: bool,
) -> dict[str, Any]:
    subject_receipts = [
        {
            "id": subject["id"],
            "revision": subject["revision"],
            "tree": subject["tree"],
        }
        for subject in manifest["subjects"]
    ]
    fixed_manifest, _ignored_receipt = load_subject_manifest(_DEFAULT_MANIFEST)
    fixed_subject_receipts = [
        {
            "id": subject["id"],
            "revision": subject["revision"],
            "tree": subject["tree"],
        }
        for subject in fixed_manifest["subjects"]
    ]
    expected = {
        "iterations_per_arm": DEFAULT_ITERATIONS,
        "warmups_per_arm": DEFAULT_WARMUPS,
        "cells": list(CELLS),
        "tracks": {track: list(cells) for track, cells in TRACKS.items()},
        "cell_authority_contracts": _json_snapshot(CELL_AUTHORITY_CONTRACTS),
        "canonical_sample_count": CANONICAL_SAMPLE_COUNT,
        "view_set_ids": [VIEW_SET_ID],
        "payload_classes": list(PAYLOAD_CLASSES),
        "minimum_media_classes": 2,
        "fresh_inner_process_per_sample": True,
        "runner": "built-in-process-isolated",
        "paired_arm_order": "alternating-ab-ba",
        "peak_rss_source": CANONICAL_PEAK_RSS_SOURCE,
        "io_source": CANONICAL_IO_SOURCE,
        "fixed_manifest_sha256": _CANONICAL_MANIFEST_SHA256,
        "fixed_manifest_size": _CANONICAL_MANIFEST_SIZE,
        "subject_receipts": fixed_subject_receipts,
    }
    distinct_media = {
        (
            identity["device"],
            identity["filesystem"],
            identity["mount_source"],
        )
        for identity in media.values()
    }
    observed = {
        "iterations_per_arm": iterations,
        "warmups_per_arm": warmups,
        "cells": list(manifest["cells"]),
        "tracks": {track: list(cells) for track, cells in TRACKS.items()},
        "cell_authority_contracts": _json_snapshot(CELL_AUTHORITY_CONTRACTS),
        "canonical_sample_count": (
            len(manifest["subjects"])
            * len(media)
            * len(manifest["view_sets"])
            * len(manifest["cells"])
            * (iterations + warmups)
            * len(ARMS)
        ),
        "view_set_ids": [item["id"] for item in manifest["view_sets"]],
        "payload_classes": [item["payload_class"] for item in manifest["subjects"]],
        "minimum_media_classes": min(len(distinct_media), 2),
        "fresh_inner_process_per_sample": built_in_runner,
        "runner": (
            "built-in-process-isolated" if built_in_runner else "injected-sample-runner"
        ),
        "paired_arm_order": "alternating-ab-ba",
        "peak_rss_source": _peak_rss_source(),
        "io_source": _io_source(),
        "fixed_manifest_sha256": manifest_receipt["sha256"],
        "fixed_manifest_size": manifest_receipt["size"],
        "subject_receipts": subject_receipts,
    }
    return {
        "expected": expected,
        "observed": observed,
        "canonical": expected == observed,
    }


def _validate_manifest_identity(
    value: object,
    *,
    subject: Mapping[str, Any],
    label: str,
) -> dict[str, Any]:
    identity = _require_exact_dict(value, _MANIFEST_IDENTITY_FIELDS, label=label)
    commit = _required_text(identity["commit"], label=f"{label} commit", maximum=40)
    if commit != subject["revision"]:
        raise RuntimeError(f"{label} commit differs from the fixed subject")
    fingerprint = _required_text(
        identity["source_fingerprint"], label=f"{label} source fingerprint", maximum=80
    )
    if not _SOURCE_V2_RE.fullmatch(fingerprint):
        raise RuntimeError(f"{label} source fingerprint is not secure v2")
    selection_digest = _required_text(
        identity["source_selection_digest"],
        label=f"{label} source selection digest",
        maximum=80,
    )
    if selection_digest != _selection_digest(subject["source_selection"]):
        raise RuntimeError(f"{label} source selection differs from the subject")
    if identity["languages"] != subject["languages"]:
        raise RuntimeError(f"{label} languages differ from the fixed subject")
    file_count = _positive_int(identity["file_count"], label=f"{label} file count")
    semantic = _required_text(
        identity["semantic_sha256"], label=f"{label} semantic digest", maximum=80
    )
    if not _SHA256_RE.fullmatch(semantic):
        raise RuntimeError(f"{label} semantic digest is malformed")
    return {
        "commit": commit,
        "source_fingerprint": fingerprint,
        "source_selection_digest": selection_digest,
        "languages": list(identity["languages"]),
        "file_count": file_count,
        "semantic_sha256": semantic,
    }


def _validate_view_identity(
    value: object,
    *,
    label: str,
) -> dict[str, Any]:
    identity = _require_exact_dict(value, _VIEW_IDENTITY_FIELDS, label=label)
    documents = _required_text(
        identity["documents_sha256"], label=f"{label} documents digest", maximum=80
    )
    metadata = _required_text(
        identity["metadata_sha256"], label=f"{label} metadata digest", maximum=80
    )
    if not _SHA256_RE.fullmatch(documents) or not _SHA256_RE.fullmatch(metadata):
        raise RuntimeError(f"{label} contains a malformed SHA-256 digest")
    payload_bytes = _positive_int(
        identity["payload_bytes"], label=f"{label} payload bytes"
    )
    payload_files = _positive_int(
        identity["payload_files"], label=f"{label} payload files"
    )
    if payload_files != len(_VIEW_FILES):
        raise RuntimeError(f"{label} BM25 inventory changed")
    return {
        "documents_sha256": documents,
        "metadata_sha256": metadata,
        "payload_bytes": payload_bytes,
        "payload_files": payload_files,
    }


def _validate_query_identity(
    value: object,
    *,
    subject: Mapping[str, Any],
    label: str,
) -> dict[str, Any]:
    identity = _require_exact_dict(value, _QUERY_IDENTITY_FIELDS, label=label)
    digest = _required_text(identity["sha256"], label=f"{label} digest", maximum=80)
    if not _SHA256_RE.fullmatch(digest):
        raise RuntimeError(f"{label} digest is malformed")
    count = _positive_int(identity["count"], label=f"{label} count")
    if count != len(subject["queries"]):
        raise RuntimeError(f"{label} count differs from the fixed workload")
    if identity["nonempty"] is not True:
        raise RuntimeError(f"{label} must report a result for every fixed query")
    return {"sha256": digest, "count": count, "nonempty": True}


def _portable_artifact_identity(subject: Mapping[str, Any]) -> dict[str, Any]:
    from codenib.artifacts.context import CONTEXT_ARTIFACT_SCHEMA

    return {
        "verified": True,
        "schema": CONTEXT_ARTIFACT_SCHEMA,
        "repository": subject["repository_key"],
        "commit": subject["revision"],
        "views": ["bm25"],
    }


def _expected_authority_identity(request: Mapping[str, Any]) -> dict[str, Any]:
    cell = request["cell"]
    arm = request["arm"]
    if cell in {"compiler-cold", "compiler-current"}:
        return {
            "context_kind": "compiler-portable-plan",
            "artifact": None,
            "source_verified": None,
            "source_verification_scope": None,
        }
    if cell == "runtime-cold" and arm == "legacy":
        return {
            "context_kind": "manifest-live-source",
            "artifact": None,
            "source_verified": True,
            "source_verification_scope": "content-bytes",
        }
    return {
        "context_kind": "portable-artifact-query-only",
        "artifact": _portable_artifact_identity(request["subject"]),
        "source_verified": False,
        "source_verification_scope": None,
    }


def _validate_authority_identity(
    value: object,
    *,
    request: Mapping[str, Any],
    label: str,
) -> dict[str, Any]:
    identity = _require_exact_dict(value, _AUTHORITY_IDENTITY_FIELDS, label=label)
    expected = _expected_authority_identity(request)
    if identity != expected:
        raise RuntimeError(f"{label} differs from its exact per-cell contract")
    return _json_snapshot(expected)


def _validate_snapshot(
    value: object,
    *,
    arm: str,
    cell: str,
) -> dict[str, Any]:
    snapshot = _require_exact_dict(value, _SNAPSHOT_FIELDS, label="sample snapshot")
    if arm == "legacy":
        if any(snapshot[field] is not None for field in _SNAPSHOT_FIELDS):
            raise RuntimeError("legacy sample must not claim a retained snapshot")
        return {field: None for field in _SNAPSHOT_FIELDS}
    snapshot_id = _required_text(
        snapshot["snapshot_id"], label="sample snapshot ID", maximum=256
    )
    if not _SNAPSHOT_RE.fullmatch(snapshot_id):
        raise RuntimeError("candidate sample snapshot ID is not canonical")
    if snapshot["ref_name"] != "main":
        raise RuntimeError("candidate sample must bind the main ref")
    generation = _positive_int(snapshot["generation"], label="sample generation")
    if generation != 1:
        raise RuntimeError("candidate report-only samples must publish generation 1")
    expected_changed: bool | None = {
        "compiler-cold": True,
        "compiler-current": False,
        "runtime-cold": None,
        "runtime-cold-query-only": None,
    }[cell]
    if snapshot["changed"] is not expected_changed:
        raise RuntimeError("candidate sample changed flag violates its cell contract")
    return {
        "snapshot_id": snapshot_id,
        "ref_name": "main",
        "generation": generation,
        "changed": expected_changed,
    }


def _validate_sample_receipt(
    value: object,
    *,
    request: Mapping[str, Any],
) -> dict[str, Any]:
    receipt = _require_exact_dict(value, _INNER_SAMPLE_FIELDS, label="sample receipt")
    for field in ("run_id", "arm", "phase", "round_index", "cell"):
        if receipt[field] != request[field]:
            raise RuntimeError(f"sample receipt changed {field}")
    if (
        receipt["schema_version"] != REPORT_SCHEMA_VERSION
        or receipt["operation"] != "sample"
        or receipt["subject_id"] != request["subject"]["id"]
        or receipt["media_id"] != request["media_id"]
        or receipt["view_set_id"] != request["view_set"]["id"]
    ):
        raise RuntimeError("sample receipt identity differs from its request")
    process_id = _positive_int(receipt["process_id"], label="sample process ID")
    metrics = _require_exact_dict(
        receipt["metrics"], _METRIC_FIELDS, label="sample metrics"
    )
    normalized_metrics: dict[str, float | int] = {}
    for field in ("route_wall_seconds", "process_wall_seconds", "cpu_seconds"):
        normalized_metrics[field] = _finite_nonnegative(
            metrics[field], label=f"sample {field}"
        )
    for field in (
        "peak_rss_bytes",
        "io_read_bytes",
        "io_write_bytes",
        "payload_bytes",
        "payload_files",
    ):
        normalized_metrics[field] = _nonnegative_int(
            metrics[field], label=f"sample {field}"
        )
    if normalized_metrics["peak_rss_bytes"] <= 0:
        raise RuntimeError("sample peak RSS must be positive")
    subject = request["subject"]
    result = _require_exact_dict(
        receipt["result"], _RESULT_FIELDS, label="sample result"
    )
    manifest = _validate_manifest_identity(
        result["manifest"], subject=subject, label="sample manifest identity"
    )
    view = _validate_view_identity(result["view"], label="sample BM25 identity")
    queries = _validate_query_identity(
        result["queries"], subject=subject, label="sample query identity"
    )
    authority = _validate_authority_identity(
        result["authority"],
        request=request,
        label="sample authority identity",
    )
    retained_view = result["retained_view"]
    if retained_view is not None:
        retained_view = _validate_view_identity(
            retained_view, label="sample retained BM25 identity"
        )
    if request["arm"] == "legacy":
        if retained_view is not None:
            raise RuntimeError("legacy sample must not claim a retained BM25 view")
    elif retained_view is None or retained_view != view:
        raise RuntimeError(
            "candidate retained BM25 identity must exactly equal its raw view"
        )
    snapshot = _validate_snapshot(
        result["snapshot"], arm=request["arm"], cell=request["cell"]
    )
    if normalized_metrics["payload_bytes"] != view["payload_bytes"]:
        raise RuntimeError("sample payload byte metric differs from its BM25 identity")
    if normalized_metrics["payload_files"] != view["payload_files"]:
        raise RuntimeError("sample payload file metric differs from its BM25 identity")
    parity = _require_exact_dict(
        receipt["parity_identity"], _PARITY_FIELDS, label="sample parity identity"
    )
    expected_parity = {
        "manifest": manifest,
        "view": {
            "documents_sha256": view["documents_sha256"],
            "metadata_sha256": view["metadata_sha256"],
        },
        "queries": queries,
        "authority": authority,
    }
    if parity != expected_parity:
        raise RuntimeError("sample parity projection differs from its result")
    safety = _require_exact_dict(
        receipt["safety"], _SAFETY_FIELDS, label="sample safety"
    )
    if any(type(safety[field]) is not bool for field in _SAFETY_FIELDS):
        raise RuntimeError("sample safety fields must be exact booleans")
    if not all(safety.values()):
        failed = sorted(field for field, passed in safety.items() if not passed)
        raise RuntimeError("sample safety failed: " + ", ".join(failed))
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "operation": "sample",
        "run_id": receipt["run_id"],
        "arm": receipt["arm"],
        "phase": receipt["phase"],
        "round_index": receipt["round_index"],
        "cell": receipt["cell"],
        "subject_id": receipt["subject_id"],
        "media_id": receipt["media_id"],
        "view_set_id": receipt["view_set_id"],
        "process_id": process_id,
        "metrics": normalized_metrics,
        "parity_identity": expected_parity,
        "result": {
            "manifest": manifest,
            "view": view,
            "retained_view": retained_view,
            "queries": queries,
            "snapshot": snapshot,
            "authority": authority,
        },
        "safety": dict(safety),
    }


def _summarize_arm(samples: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not samples:
        raise RuntimeError("measured arm has no samples")
    return {
        field: summarize_samples([sample["metrics"][field] for sample in samples])
        for field in (
            "route_wall_seconds",
            "process_wall_seconds",
            "cpu_seconds",
            "peak_rss_bytes",
            "io_read_bytes",
            "io_write_bytes",
            "payload_bytes",
            "payload_files",
        )
    }


def _metric_ratio(
    legacy: Mapping[str, Any],
    candidate: Mapping[str, Any],
    metric: str,
    percentile: str,
) -> float | None:
    baseline = _finite_nonnegative(
        legacy[metric][percentile], label=f"legacy {metric} {percentile}"
    )
    observed = _finite_nonnegative(
        candidate[metric][percentile], label=f"candidate {metric} {percentile}"
    )
    return None if baseline == 0 else observed / baseline


def _run_cell(
    *,
    subject: Mapping[str, Any],
    subject_root: Path,
    media_id: str,
    media_root: Path,
    media_identity: Mapping[str, Any],
    view_set: Mapping[str, Any],
    cell: str,
    iterations: int,
    warmups: int,
    sample_runner: SampleRunner,
    process_ids: list[int],
) -> dict[str, Any]:
    frozen_subject = _json_snapshot(dict(subject))
    frozen_media_identity = _json_snapshot(dict(media_identity))
    frozen_view_set = _json_snapshot(dict(view_set))
    runs: dict[str, list[dict[str, Any]]] = {"warmups": [], "measured": []}
    measured_by_arm: dict[str, list[dict[str, Any]]] = {
        "legacy": [],
        "candidate": [],
    }
    all_samples: list[dict[str, Any]] = []
    every_pair = True
    for phase, rounds, report_phase in (
        ("warmup", warmups, "warmups"),
        ("measured", iterations, "measured"),
    ):
        for round_index in range(rounds):
            order = paired_arm_order(round_index)
            pair: list[dict[str, Any]] = []
            for arm in order:
                request = {
                    "operation": "sample",
                    "arm": arm,
                    "phase": phase,
                    "round_index": round_index,
                    "cell": cell,
                    "subject": _json_snapshot(frozen_subject),
                    "subject_root": os.fspath(subject_root),
                    "media_id": media_id,
                    "media_root": os.fspath(media_root),
                    "media_identity": _json_snapshot(frozen_media_identity),
                    "view_set": _json_snapshot(frozen_view_set),
                    "run_id": secrets.token_hex(16),
                }
                expected_request = _json_snapshot(request)
                raw_receipt = sample_runner(request)
                if request != expected_request:
                    raise RuntimeError("sample runner mutated its request")
                receipt = _validate_sample_receipt(
                    raw_receipt,
                    request=expected_request,
                )
                pair.append(receipt)
                all_samples.append(receipt)
                process_ids.append(receipt["process_id"])
                if phase == "measured":
                    measured_by_arm[arm].append(receipt)
            pair_equal = pair[0]["parity_identity"] == pair[1]["parity_identity"]
            every_pair = every_pair and pair_equal
            runs[report_phase].append(
                {
                    "round_index": round_index,
                    "arm_order": list(order),
                    "parity": pair_equal,
                    "samples": pair,
                }
            )
    identities = {_json_digest(sample["parity_identity"]) for sample in all_samples}
    stable = len(identities) == 1
    summary = {arm: _summarize_arm(measured_by_arm[arm]) for arm in ARMS}
    safety_checks = {
        field: all(sample["safety"][field] for sample in all_samples)
        for field in sorted(_SAFETY_FIELDS)
    }
    passed = every_pair and stable and all(safety_checks.values())
    return {
        "subject_id": frozen_subject["id"],
        "media_id": media_id,
        "view_set_id": frozen_view_set["id"],
        "cell": cell,
        "passed": passed,
        "runs": runs,
        "summary": summary,
        "parity": {
            "every_pair": every_pair,
            "stable_across_runs": stable,
            "identity_sha256": sorted(identities),
            "passed": every_pair and stable,
        },
        "safety": {
            "checks": safety_checks,
            "passed": all(safety_checks.values()),
        },
        "performance": {
            "policy_status": "unratified",
            "budgets": None,
            "ratios": {
                metric: {
                    percentile: _metric_ratio(
                        summary["legacy"],
                        summary["candidate"],
                        metric,
                        percentile,
                    )
                    for percentile in ("p50", "p95")
                }
                for metric in (
                    "route_wall_seconds",
                    "process_wall_seconds",
                    "cpu_seconds",
                    "peak_rss_bytes",
                    "io_read_bytes",
                    "io_write_bytes",
                )
            },
            "gates": None,
            "promotion_eligible": False,
        },
    }


def _cell_measurement_complete(
    cell: object,
    *,
    iterations: int,
    warmups: int,
) -> bool:
    if type(cell) is not dict:
        return False
    runs = cell.get("runs")
    if type(runs) is not dict or set(runs) != {"warmups", "measured"}:
        return False
    for phase_name, expected_rounds in (
        ("warmups", warmups),
        ("measured", iterations),
    ):
        rounds = runs[phase_name]
        if type(rounds) is not list or len(rounds) != expected_rounds:
            return False
        for round_index, pair in enumerate(rounds):
            if type(pair) is not dict:
                return False
            order = list(paired_arm_order(round_index))
            samples = pair.get("samples")
            if (
                pair.get("round_index") != round_index
                or pair.get("arm_order") != order
                or type(pair.get("parity")) is not bool
                or type(samples) is not list
                or len(samples) != len(ARMS)
                or [sample.get("arm") for sample in samples] != order
            ):
                return False
    summary = cell.get("summary")
    if type(summary) is not dict or set(summary) != set(ARMS):
        return False
    for arm in ARMS:
        metrics = summary[arm]
        if type(metrics) is not dict or set(metrics) != _METRIC_FIELDS:
            return False
        for metric in _METRIC_FIELDS:
            aggregate = metrics[metric]
            if type(aggregate) is not dict or aggregate.get("samples") != iterations:
                return False
    return True


def _aggregate_tracks(
    cells: Mapping[str, Mapping[str, Any]],
    *,
    expected_instances_per_cell: int,
    iterations: int,
    warmups: int,
) -> dict[str, dict[str, Any]]:
    """Reduce exact cell instances into independent report-only tracks."""

    expected_instances = _positive_int(
        expected_instances_per_cell,
        label="expected instances per cell",
    )
    measured_iterations = _positive_int(iterations, label="iterations")
    measured_warmups = _nonnegative_int(warmups, label="warmups")
    if not isinstance(cells, Mapping):
        raise TypeError("cells must be a mapping")
    by_cell: dict[str, list[Mapping[str, Any]]] = {cell: [] for cell in CELLS}
    unknown = False
    for value in cells.values():
        if not isinstance(value, Mapping) or value.get("cell") not in by_cell:
            unknown = True
            continue
        by_cell[str(value["cell"])].append(value)

    tracks: dict[str, dict[str, Any]] = {}
    for track, track_cells in TRACKS.items():
        instances = [item for cell in track_cells for item in by_cell[cell]]
        expected_count = expected_instances * len(track_cells)
        identities = {
            (
                item.get("subject_id"),
                item.get("media_id"),
                item.get("view_set_id"),
                item.get("cell"),
            )
            for item in instances
        }
        measurement_complete = (
            not unknown
            and len(instances) == expected_count
            and len(identities) == expected_count
            and all(
                _cell_measurement_complete(
                    item,
                    iterations=measured_iterations,
                    warmups=measured_warmups,
                )
                for item in instances
            )
        )
        parity_passed = measurement_complete and all(
            type(item.get("parity")) is dict and item["parity"].get("passed") is True
            for item in instances
        )
        safety_passed = measurement_complete and all(
            type(item.get("safety")) is dict and item["safety"].get("passed") is True
            for item in instances
        )
        tracks[track] = {
            "cells": list(track_cells),
            "measurement_complete": measurement_complete,
            "parity_passed": parity_passed,
            "safety_passed": safety_passed,
            "passed": measurement_complete and parity_passed and safety_passed,
            "policy_status": "unratified",
            "promotion_eligible": False,
        }
    return tracks


def _duplicate_process_ids(process_ids: Sequence[int]) -> list[int]:
    counts: dict[int, int] = {}
    for process_id in process_ids:
        counts[process_id] = counts.get(process_id, 0) + 1
    return sorted(process_id for process_id, count in counts.items() if count > 1)


def profile_retained_storage_gate(
    *,
    subject_roots: Mapping[str, Path],
    media_roots: Mapping[str, Path],
    manifest_path: Path = _DEFAULT_MANIFEST,
    iterations: int = DEFAULT_ITERATIONS,
    warmups: int = DEFAULT_WARMUPS,
    worker_timeout_seconds: float = DEFAULT_WORKER_TIMEOUT_SECONDS,
    sample_runner: SampleRunner | None = None,
) -> dict[str, Any]:
    """Run the fixed report-only retained route gate.

    ``sample_runner`` is the sole public injection point.  Its contract is the
    same exact outer ``operation='sample'`` request and receipt used by the
    built-in process-isolated runner.  Invalid, partial, or non-finite receipts
    produce a negative report and can never authorize promotion.
    """

    report = _base_report(
        manifest_path=manifest_path,
        iterations=iterations,
        warmups=warmups,
        worker_timeout_seconds=worker_timeout_seconds,
        subject_roots=subject_roots,
        media_roots=media_roots,
    )
    try:
        if not isinstance(manifest_path, Path):
            raise TypeError("manifest_path must be a pathlib.Path")
        iterations = _positive_int(iterations, label="iterations")
        warmups = _nonnegative_int(warmups, label="warmups")
        timeout = _finite_nonnegative(worker_timeout_seconds, label="worker timeout")
        if timeout <= 0:
            raise ValueError("worker timeout must be positive")
        if sample_runner is not None and not callable(sample_runner):
            raise TypeError("sample_runner must be callable")
        manifest, manifest_receipt = load_subject_manifest(manifest_path)
        normalized_subject_roots = _normalize_root_mapping(
            subject_roots, label="subject roots"
        )
        normalized_media_roots = _normalize_root_mapping(
            media_roots, label="media roots"
        )
        benchmark_root = _real_directory(_PROJECT_ROOT, label="benchmark checkout")
        expected_subject_ids = {subject["id"] for subject in manifest["subjects"]}
        if set(normalized_subject_roots) != expected_subject_ids:
            raise ValueError("subject root IDs must match the fixed manifest exactly")
        _reject_overlapping_roots(normalized_subject_roots, label="subject root")
        _reject_overlapping_roots(normalized_media_roots, label="media root")
        media: dict[str, dict[str, Any]] = {
            media_id: _media_identity(root)
            for media_id, root in sorted(normalized_media_roots.items())
        }
        for subject_id, subject_root in normalized_subject_roots.items():
            if _paths_overlap(subject_root, benchmark_root):
                raise ValueError(
                    f"subject {subject_id!r} overlaps the benchmark checkout"
                )
        for subject_id, subject_root in normalized_subject_roots.items():
            for media_id, media_root in normalized_media_roots.items():
                if _paths_overlap(subject_root, media_root):
                    raise ValueError(
                        f"subject {subject_id!r} overlaps media root {media_id!r}"
                    )
        for media_id, media_root in normalized_media_roots.items():
            if _paths_overlap(media_root, benchmark_root):
                raise ValueError(
                    f"media root {media_id!r} overlaps the benchmark checkout"
                )
        report["media"] = media
        report["protocol"] = _protocol(
            manifest,
            manifest_receipt=manifest_receipt,
            iterations=iterations,
            warmups=warmups,
            media=media,
            built_in_runner=sample_runner is None,
        )
    except Exception as exc:
        return _failure(report, "controller-validation", exc)

    try:
        benchmark_before = _benchmark_identity(manifest_path)
        if benchmark_before.get("git", {}).get("dirty") is not False:
            raise RuntimeError("benchmark checkout must be clean")
        subjects_report: dict[str, Any] = {}
        subject_observations: dict[str, dict[str, Any]] = {}
        for subject in manifest["subjects"]:
            subject_id = subject["id"]
            root = normalized_subject_roots[subject_id]
            observation = _observe_subject(subject, root)
            _require_expected_subject(subject, observation)
            subject_observations[subject_id] = observation
            subjects_report[subject_id] = {
                "manifest": dict(subject),
                "root": os.fspath(root),
                "before": observation,
                "after": None,
                "unchanged": False,
            }
        report["subjects"] = subjects_report
        report["benchmark_receipts"] = {
            "before": benchmark_before,
            "after": None,
            "unchanged": False,
        }
        if benchmark_before["manifest"] != manifest_receipt:
            raise RuntimeError(
                "manifest receipt changed between validation and preflight"
            )
    except Exception as exc:
        return _failure(report, "preflight", exc)

    process_ids: list[int] = []
    runner: SampleRunner
    if sample_runner is None:
        runner = lambda request: _run_isolated_sample(  # noqa: E731
            request,
            timeout_seconds=timeout,
        )
    else:
        runner = sample_runner
    try:
        for subject in manifest["subjects"]:
            subject_id = subject["id"]
            for media_id, media_root in sorted(normalized_media_roots.items()):
                for view_set in manifest["view_sets"]:
                    for cell in manifest["cells"]:
                        key = "/".join((subject_id, media_id, view_set["id"], cell))
                        report["cells"][key] = _run_cell(
                            subject=subject,
                            subject_root=normalized_subject_roots[subject_id],
                            media_id=media_id,
                            media_root=media_root,
                            media_identity=media[media_id],
                            view_set=view_set,
                            cell=cell,
                            iterations=iterations,
                            warmups=warmups,
                            sample_runner=runner,
                            process_ids=process_ids,
                        )
    except Exception as exc:
        measurement_failure = _failure(report, "measurement", exc)
    else:
        measurement_failure = None

    postflight_error: Exception | None = None
    try:
        for subject in manifest["subjects"]:
            subject_id = subject["id"]
            after = _observe_subject(subject, normalized_subject_roots[subject_id])
            _require_expected_subject(subject, after)
            unchanged = after == subject_observations[subject_id]
            report["subjects"][subject_id]["after"] = after
            report["subjects"][subject_id]["unchanged"] = unchanged
            if not unchanged:
                raise RuntimeError(f"subject {subject_id!r} changed during measurement")
        benchmark_after = _benchmark_identity(manifest_path)
        benchmark_unchanged = benchmark_after == benchmark_before
        report["benchmark_receipts"]["after"] = benchmark_after
        report["benchmark_receipts"]["unchanged"] = benchmark_unchanged
        if (
            not benchmark_unchanged
            or benchmark_after.get("git", {}).get("dirty") is not False
        ):
            raise RuntimeError("benchmark checkout changed during measurement")
    except Exception as exc:
        postflight_error = exc

    expected_samples = (
        len(manifest["subjects"])
        * len(normalized_media_roots)
        * len(manifest["view_sets"])
        * len(manifest["cells"])
        * (iterations + warmups)
        * len(ARMS)
    )
    duplicates = _duplicate_process_ids(process_ids)
    isolation_passed = len(process_ids) == expected_samples and not duplicates
    report["process_isolation"] = {
        "expected_samples": expected_samples,
        "observed_samples": len(process_ids),
        "inner_process_ids": list(process_ids),
        "duplicate_process_ids": duplicates,
        "passed": isolation_passed,
    }
    report["tracks"] = _aggregate_tracks(
        report["cells"],
        expected_instances_per_cell=(
            len(manifest["subjects"])
            * len(normalized_media_roots)
            * len(manifest["view_sets"])
        ),
        iterations=iterations,
        warmups=warmups,
    )
    if measurement_failure is not None:
        if postflight_error is not None:
            measurement_failure["failure"][
                "message"
            ] += f"; postflight also failed: {postflight_error}"
        return measurement_failure
    if postflight_error is not None:
        return _failure(report, "postflight", postflight_error)
    if not isolation_passed:
        return _failure(
            report,
            "isolation",
            RuntimeError("global inner-process isolation failed"),
        )
    incomplete_tracks = [
        track
        for track, aggregate in report["tracks"].items()
        if aggregate["measurement_complete"] is not True
    ]
    if incomplete_tracks:
        return _failure(
            report,
            "measurement",
            RuntimeError(
                "track measurement incomplete: " + ", ".join(incomplete_tracks)
            ),
        )
    unsafe_tracks = [
        track
        for track, aggregate in report["tracks"].items()
        if aggregate["safety_passed"] is not True
    ]
    if unsafe_tracks:
        return _failure(
            report,
            "safety",
            RuntimeError("track safety failed: " + ", ".join(unsafe_tracks)),
        )
    if report["protocol"]["canonical"] is not True:
        return _failure(
            report,
            "protocol",
            RuntimeError("measurement protocol is not canonical v2"),
        )

    tracks_passed = all(
        aggregate["passed"] is True for aggregate in report["tracks"].values()
    )
    report["status"] = "complete"
    report["passed"] = tracks_passed
    report["promotion_eligible"] = False
    report["failure"] = None
    if not tracks_passed:
        report["decision"] = {
            "policy_status": "unratified",
            "report_only": True,
            "promotion_eligible": False,
            "recommendation": "retain-explicit-routes",
            "reason": "one or more compatibility tracks are parity red",
        }
    return report


def _worker_environment() -> dict[str, str]:
    environment = dict(os.environ)
    paths = [os.fspath(_PROJECT_ROOT / "build/core"), os.fspath(_PROJECT_ROOT)]
    if environment.get("PYTHONPATH"):
        paths.append(environment["PYTHONPATH"])
    environment["PYTHONPATH"] = os.pathsep.join(paths)
    environment["PYTHONHASHSEED"] = "0"
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    return environment


def _terminate_process(
    process: subprocess.Popen[str],
    *,
    process_group: bool,
) -> tuple[str, str]:
    if process_group and os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        graceful: tuple[str, str] | None = None
        try:
            graceful = process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        finally:
            # The group leader can exit on TERM while a descendant ignores it.
            # Always revoke the original process group before returning.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        final = process.communicate()
        return graceful if graceful is not None else final
    try:
        process.terminate()
    except ProcessLookupError:
        pass
    try:
        return process.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        return process.communicate()


def _run_worker_process(
    request: Mapping[str, Any],
    *,
    timeout_seconds: float,
    process_group: bool,
) -> tuple[dict[str, Any], float]:
    encoded_request = _canonical_json_bytes(dict(request)).decode("ascii")
    started = time.perf_counter()
    process: subprocess.Popen[str] | None = None
    try:
        process = subprocess.Popen(
            [sys.executable, os.fspath(Path(__file__).resolve()), "--worker"],
            cwd=os.fspath(_PROJECT_ROOT),
            env=_worker_environment(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=process_group,
        )
        stdout, stderr = process.communicate(
            input=encoded_request,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        if process is None:  # pragma: no cover - Popen cannot raise this type
            raise
        stdout, stderr = _terminate_process(process, process_group=process_group)
        raise RuntimeError(
            f"worker timed out after {timeout_seconds:g}s; "
            f"stderr={stderr[:_MAX_REPORT_TEXT]!r}"
        ) from exc
    except BaseException as exc:  # noqa: B036 - reap before preserving interruption
        if process is not None:
            try:
                _terminate_process(process, process_group=process_group)
            except BaseException as cleanup_error:  # noqa: B036 - retain primary
                if hasattr(exc, "add_note"):
                    exc.add_note(f"worker cleanup also failed: {cleanup_error}")
        raise
    elapsed = time.perf_counter() - started
    if len(stdout.encode("utf-8")) > _MAX_WORKER_STDOUT_BYTES:
        raise RuntimeError("worker stdout exceeds its size limit")
    if len(stderr.encode("utf-8")) > _MAX_WORKER_STDERR_BYTES:
        raise RuntimeError("worker stderr exceeds its size limit")
    if process.returncode != 0:
        raise RuntimeError(
            f"worker exited {process.returncode}; "
            f"stderr={stderr[:_MAX_REPORT_TEXT]!r}; "
            f"stdout={stdout[:_MAX_REPORT_TEXT]!r}"
        )
    if stderr and request.get("operation") != "route":
        raise RuntimeError("successful worker wrote unexpected stderr output")
    try:
        result = json.loads(
            stdout,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite_constant,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError("worker did not emit one strict JSON result") from exc
    if type(result) is not dict:
        raise RuntimeError("worker result must be an exact JSON object")
    if request.get("operation") == "route" and result.get("process_id") != process.pid:
        raise RuntimeError("inner route receipt PID differs from its subprocess")
    return result, elapsed


def _run_isolated_sample(
    request: Mapping[str, Any],
    *,
    timeout_seconds: float,
) -> Mapping[str, Any]:
    validated = _validate_sample_request(dict(request))
    sample_root = _owned_sample_root(validated)
    if os.path.lexists(sample_root):
        raise RuntimeError("fresh sample root already exists before worker launch")
    try:
        result, _elapsed = _run_worker_process(
            validated,
            timeout_seconds=timeout_seconds,
            process_group=True,
        )
        if os.path.lexists(sample_root):
            raise RuntimeError("outer worker left its owned sample root behind")
        return result
    except BaseException as exc:  # noqa: B036 - reclaim exact abandoned root
        if os.path.lexists(sample_root):
            try:
                _cleanup_sample_root(
                    sample_root,
                    Path(validated["media_root"]),
                    validated["run_id"],
                )
            except BaseException as cleanup_error:  # noqa: B036
                if hasattr(exc, "add_note"):
                    exc.add_note(
                        f"abandoned sample cleanup also failed: {cleanup_error}"
                    )
        raise


def _validate_sample_request(value: object) -> dict[str, Any]:
    request = _require_exact_dict(value, _SAMPLE_REQUEST_FIELDS, label="sample request")
    if request["operation"] != "sample":
        raise ValueError("outer worker requires operation='sample'")
    arm = _required_text(request["arm"], label="sample arm", maximum=16)
    phase = _required_text(request["phase"], label="sample phase", maximum=16)
    cell = _required_text(request["cell"], label="sample cell", maximum=32)
    if arm not in ARMS or phase not in PHASES or cell not in CELLS:
        raise ValueError("sample arm, phase, or cell is unsupported")
    round_index = _nonnegative_int(request["round_index"], label="sample round index")
    subject = _validate_subject(request["subject"], label="sample subject")
    view_set = _validate_view_set(request["view_set"], label="sample view set")
    subject_root = Path(
        _required_text(request["subject_root"], label="sample subject root")
    )
    media_root = Path(_required_text(request["media_root"], label="sample media root"))
    if not subject_root.is_absolute() or not media_root.is_absolute():
        raise ValueError("sample roots must be absolute")
    subject_root = subject_root.resolve(strict=True)
    media_root = media_root.resolve(strict=True)
    media_id = _required_text(request["media_id"], label="sample media ID", maximum=64)
    if not _ID_RE.fullmatch(media_id):
        raise ValueError("sample media ID is not canonical")
    media_identity = _validate_media_identity(
        request["media_identity"], label="sample media identity"
    )
    if media_identity != _media_identity(media_root):
        raise RuntimeError("sample media identity changed before provisioning")
    run_id = _required_text(request["run_id"], label="sample run ID", maximum=32)
    if not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise ValueError("sample run ID is not canonical")
    if _paths_overlap(subject_root, media_root):
        raise ValueError("sample subject and media roots overlap")
    return {
        "operation": "sample",
        "arm": arm,
        "phase": phase,
        "round_index": round_index,
        "cell": cell,
        "subject": subject,
        "subject_root": os.fspath(subject_root),
        "media_id": media_id,
        "media_root": os.fspath(media_root),
        "media_identity": media_identity,
        "view_set": view_set,
        "run_id": run_id,
    }


def _sample_paths(sample_root: Path, run_id: str) -> dict[str, str]:
    return {
        "sample_root": os.fspath(sample_root),
        "codenib_home": os.fspath(sample_root / "codenib-home"),
        "temp_root": os.fspath(sample_root / "temp"),
        "results_root": os.fspath(sample_root / "results"),
        "prebuilt_root": os.fspath(sample_root / "prebuilt"),
        "catalog": os.fspath(sample_root / "catalog.sqlite"),
        "cas_root": os.fspath(sample_root / "cas"),
        "workspace_root": os.fspath(sample_root / "workspace"),
        "direct_artifact": os.fspath(sample_root / f"direct-artifact-{run_id}"),
        "runtime_output": os.fspath(sample_root / "workspace" / f"runtime-{run_id}"),
        "materialized_output": os.fspath(
            sample_root / "workspace" / f"materialized-{run_id}"
        ),
    }


def _owned_sample_root(request: Mapping[str, Any]) -> Path:
    return Path(request["media_root"]) / f"{_TEMP_PREFIX}{request['run_id']}"


@contextlib.contextmanager
def _sample_environment(paths: Mapping[str, str]):
    updates = {
        "CODENIB_HOME": paths["codenib_home"],
        "CODENIB_TEMP_DIR": paths["temp_root"],
        "CODENIB_RESULTS_DIR": paths["results_root"],
        "CODENIB_PREBUILT_DIR": paths["prebuilt_root"],
        "TMPDIR": paths["temp_root"],
        "TMP": paths["temp_root"],
        "TEMP": paths["temp_root"],
    }
    previous = {name: os.environ.get(name) for name in updates}
    previous_tempdir = tempfile.tempdir
    os.environ.update(updates)
    tempfile.tempdir = paths["temp_root"]
    try:
        yield
    finally:
        tempfile.tempdir = previous_tempdir
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _prepare_sample_directories(paths: Mapping[str, str]) -> None:
    for name in ("codenib_home", "temp_root", "results_root", "prebuilt_root"):
        directory = Path(paths[name])
        directory.mkdir(mode=0o700)
        directory.chmod(0o700)


def _provision_storage(paths: Mapping[str, str]) -> None:
    from codenib.storage import LocalCAS, SQLiteCatalog

    workspace = Path(paths["workspace_root"])
    workspace.mkdir(mode=0o700)
    workspace.chmod(0o700)
    with SQLiteCatalog(Path(paths["catalog"])):
        pass
    with LocalCAS.provision(Path(paths["cas_root"])):
        pass


def _index_arguments(
    request: Mapping[str, Any],
    paths: Mapping[str, str],
    *,
    candidate: bool,
) -> list[str]:
    subject = request["subject"]
    arguments = ["index", request["subject_root"], *request["view_set"]["index_args"]]
    for language in subject["languages"]:
        arguments.extend(("--language", language))
    exclusions = subject["source_selection"]["exclude_subtrees"]
    for exclusion in exclusions:
        arguments.extend(("--exclude-dir", exclusion))
    if candidate:
        arguments.extend(
            (
                "--publish-retained",
                "--catalog",
                paths["catalog"],
                "--cas-root",
                paths["cas_root"],
                "--workspace-root",
                paths["workspace_root"],
                "--repository",
                subject["repository_key"],
                "--ref",
                "main",
                "--expected-generation",
                "0",
            )
        )
    return arguments


def _invoke_cli(arguments: Sequence[str]) -> tuple[int, str, str]:
    stdout_buffer = io.StringIO()
    stderr_buffer = io.StringIO()
    with (
        contextlib.redirect_stdout(stdout_buffer),
        contextlib.redirect_stderr(stderr_buffer),
    ):
        from codenib.cli import build_parser

        parser = build_parser()
        parsed = parser.parse_args(list(arguments))
        result = parsed.handler(parsed)
    if type(result) is not int:
        raise RuntimeError("CLI handler returned a non-integer status")
    stdout = stdout_buffer.getvalue()
    stderr = stderr_buffer.getvalue()
    if (
        len(stdout.encode("utf-8")) > 1024 * 1024
        or len(stderr.encode("utf-8")) > 1024 * 1024
    ):
        raise RuntimeError("CLI output exceeds the benchmark limit")
    if result != 0:
        raise RuntimeError(
            f"CLI handler failed with status {result}: {stderr[:_MAX_REPORT_TEXT]}"
        )
    return result, stdout, stderr


def _cache_manifest_path(subject_root: Path) -> Path:
    from codenib.compiler.manifest import MANIFEST_FILENAME
    from codenib.paths import repo_index_dir

    return repo_index_dir(subject_root) / MANIFEST_FILENAME


def _repository_id(repository_key: str) -> str:
    from codenib.storage.models import DEFAULT_NAMESPACE_ID, RepositoryIdentity

    return RepositoryIdentity(
        namespace_id=DEFAULT_NAMESPACE_ID,
        repository_key=repository_key,
    ).repository_id


def _read_ref(paths: Mapping[str, str], repository_key: str) -> dict[str, Any] | None:
    from codenib.storage import CatalogNotFoundError, SQLiteCatalog

    with SQLiteCatalog(Path(paths["catalog"]), create=False) as catalog:
        try:
            raw = catalog.resolve_ref(_repository_id(repository_key), "main")
        except CatalogNotFoundError:
            return None
    required = {"repository_id", "ref_name", "snapshot_id", "generation"}
    if type(raw) is not dict or not required <= set(raw):
        raise RuntimeError("retained ref response is malformed")
    if (
        raw["repository_id"] != _repository_id(repository_key)
        or raw["ref_name"] != "main"
        or type(raw["snapshot_id"]) is not str
        or not _SNAPSHOT_RE.fullmatch(raw["snapshot_id"])
        or type(raw["generation"]) is not int
        or raw["generation"] <= 0
    ):
        raise RuntimeError("retained ref response has an invalid identity")
    return {
        "snapshot_id": raw["snapshot_id"],
        "ref_name": "main",
        "generation": raw["generation"],
    }


def _prepare_sample(request: Mapping[str, Any], paths: Mapping[str, str]) -> None:
    arm = request["arm"]
    cell = request["cell"]
    needs_storage = arm == "candidate"
    if needs_storage:
        _provision_storage(paths)
    if cell == "compiler-current":
        _invoke_cli(_index_arguments(request, paths, candidate=arm == "candidate"))
    elif cell in {"runtime-cold", "runtime-cold-query-only"}:
        _invoke_cli(_index_arguments(request, paths, candidate=arm == "candidate"))
        if cell == "runtime-cold-query-only" and arm == "legacy":
            _invoke_cli(
                (
                    "artifact",
                    "pack",
                    request["subject_root"],
                    "--output",
                    paths["direct_artifact"],
                    "--repository",
                    request["subject"]["repository_key"],
                    "--view",
                    "bm25",
                )
            )


def _cleanup_sample_root(sample_root: Path, media_root: Path, run_id: str) -> None:
    if (
        sample_root.parent != media_root
        or sample_root.name != f"{_TEMP_PREFIX}{run_id}"
        or sample_root == media_root
    ):
        raise RuntimeError("refusing to clean a non-owned sample root")
    metadata = sample_root.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError("owned sample root changed before cleanup")
    shutil.rmtree(sample_root)
    if sample_root.exists():
        raise RuntimeError("owned sample root remains after cleanup")


def _stable_json_file(
    path: Path, *, maximum: int
) -> tuple[dict[str, Any], dict[str, Any]]:
    payload, receipt = _read_stable_file(path, maximum=maximum)
    if payload.startswith(b"\xef\xbb\xbf"):
        raise RuntimeError(f"retained route JSON must not use a BOM: {path}")
    try:
        value = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite_constant,
        )
    except ValueError as exc:
        raise RuntimeError(f"retained route JSON is malformed: {path}") from exc
    if type(value) is not dict:
        raise RuntimeError(f"retained route JSON must be an object: {path}")
    return value, receipt


def _normalize_manifest_measurement_metadata(
    normalized: dict[str, Any],
) -> dict[str, Any]:
    if type(normalized) is not dict or set(normalized.get("indexes", {})) != {"bm25"}:
        raise RuntimeError("BM25 gate manifest has an unexpected view set")
    normalized["compiled_at"] = "<measurement-metadata>"
    normalized["compiled_at_epoch"] = 0
    entry = normalized["indexes"]["bm25"]
    if type(entry) is not dict:
        raise RuntimeError("BM25 gate manifest entry is malformed")
    raw_entry_path = entry.get("path")
    if type(raw_entry_path) is not str:
        raise RuntimeError("BM25 gate manifest path is malformed")
    entry["path"] = "<isolated-bm25-view>"
    entry["built_at"] = "<measurement-metadata>"
    entry["built_at_epoch"] = 0
    metadata = entry.get("metadata")
    if type(metadata) is not dict:
        raise RuntimeError("BM25 gate manifest metadata is malformed")
    if "build_duration_seconds" in metadata:
        if type(metadata["build_duration_seconds"]) not in {int, float}:
            raise RuntimeError("BM25 build duration metadata is malformed")
        metadata["build_duration_seconds"] = "<measurement-metadata>"
    return normalized


def _normalized_manifest_semantics(
    raw: Mapping[str, Any],
    *,
    manifest_root: Path,
    view_root: Path,
    subject_root: Path,
) -> dict[str, Any]:
    normalized = json.loads(_canonical_json_bytes(dict(raw)))
    if type(normalized) is not dict or set(normalized.get("indexes", {})) != {"bm25"}:
        raise RuntimeError("BM25 gate manifest has an unexpected view set")
    repo = normalized.get("repo")
    if type(repo) is not dict or type(repo.get("path")) is not str:
        raise RuntimeError("BM25 gate manifest repository path is malformed")
    raw_repo_path = repo["path"]
    if raw_repo_path != "source":
        candidate_repo = Path(raw_repo_path)
        if (
            not candidate_repo.is_absolute()
            or Path(os.path.abspath(os.fspath(candidate_repo))) != subject_root
        ):
            raise RuntimeError(
                "BM25 gate manifest repository path differs from its subject"
            )
    repo["path"] = "<isolated-source>"
    entry = normalized["indexes"]["bm25"]
    if type(entry) is not dict or type(entry.get("path")) is not str:
        raise RuntimeError("BM25 gate manifest entry path is malformed")
    raw_entry_path = entry["path"]
    if raw_repo_path == "source" and raw_entry_path != "views/bm25":
        raise RuntimeError("portable BM25 manifest path is not canonical")
    candidate = Path(raw_entry_path)
    if not candidate.is_absolute():
        candidate = manifest_root / candidate
    try:
        same_view = candidate.resolve(strict=True) == view_root.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("BM25 gate manifest view is unavailable") from exc
    if not same_view:
        raise RuntimeError("BM25 gate manifest path differs from the loaded view")
    return _normalize_manifest_measurement_metadata(normalized)


def _normalized_portable_manifest_semantics(manifest: Any) -> dict[str, Any]:
    normalized = json.loads(_canonical_json_bytes(manifest.to_dict()))
    if type(normalized) is not dict or set(normalized.get("indexes", {})) != {"bm25"}:
        raise RuntimeError("portable BM25 plan has an unexpected view set")
    repo = normalized.get("repo")
    entry = normalized["indexes"].get("bm25")
    if (
        type(repo) is not dict
        or repo.get("path") != "source"
        or type(entry) is not dict
        or entry.get("path") != "views/bm25"
    ):
        raise RuntimeError("portable BM25 plan paths are not canonical")
    repo["path"] = "<isolated-source>"
    return _normalize_manifest_measurement_metadata(normalized)


def _view_inventory_receipts(view_root: Path) -> dict[str, dict[str, Any]]:
    before = view_root.lstat()
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
        raise RuntimeError("BM25 view root must be a non-symbolic directory")
    with os.scandir(view_root) as iterator:
        entries = {entry.name: entry for entry in iterator}
    if set(entries) != set(_VIEW_FILES):
        raise RuntimeError("BM25 view inventory differs from its exact contract")
    entry_receipts: dict[str, os.stat_result] = {}
    for filename in _VIEW_FILES:
        entry = entries[filename]
        metadata = entry.stat(follow_symlinks=False)
        if (
            entry.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
        ):
            raise RuntimeError("BM25 view members must be single-link regular files")
        entry_receipts[filename] = metadata

    receipts: dict[str, dict[str, Any]] = {}
    for filename in _VIEW_FILES:
        _payload, receipt = _read_stable_file(
            view_root / filename,
            maximum=_MAX_VIEW_FILE_BYTES,
        )
        expected = entry_receipts[filename]
        after_entry = (view_root / filename).lstat()
        if (
            (receipt["device"], receipt["inode"]) != (expected.st_dev, expected.st_ino)
            or (after_entry.st_dev, after_entry.st_ino)
            != (expected.st_dev, expected.st_ino)
            or after_entry.st_nlink != 1
        ):
            raise RuntimeError("BM25 view member identity changed during hashing")
        receipts[filename] = receipt
    after = view_root.lstat()
    if (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise RuntimeError("BM25 view inventory changed during hashing")
    return receipts


def _manifest_view_identity(
    manifest_path: Path,
    *,
    subject: Mapping[str, Any],
    subject_root: Path,
) -> tuple[dict[str, Any], dict[str, Any], Any]:
    from codenib.compiler.manifest import RepoManifest

    manifest = RepoManifest.load(manifest_path)
    if not manifest.index_is_current("bm25") or set(manifest.indexes) != {"bm25"}:
        raise RuntimeError("retained gate requires one current BM25 view")
    if (
        manifest.commit != subject["revision"]
        or manifest.languages != subject["languages"]
        or manifest.source_selection is None
        or manifest.source_selection.to_dict() != subject["source_selection"]
    ):
        raise RuntimeError("route manifest differs from the fixed subject")
    entry = manifest.indexes["bm25"]
    view_root = Path(entry.path)
    if not view_root.is_absolute():
        view_root = manifest_path.parent / view_root
    view_root = view_root.resolve(strict=True)
    receipts = _view_inventory_receipts(view_root)
    payload_bytes = sum(receipt["size"] for receipt in receipts.values())
    raw_manifest, _manifest_receipt = _stable_json_file(
        manifest_path,
        maximum=_MAX_CONTEXT_MANIFEST_BYTES,
    )
    semantics = _normalized_manifest_semantics(
        raw_manifest,
        manifest_root=manifest_path.parent,
        view_root=view_root,
        subject_root=subject_root,
    )
    manifest_identity = {
        "commit": manifest.commit,
        "source_fingerprint": manifest.source_fingerprint,
        "source_selection_digest": manifest.source_selection_digest,
        "languages": list(manifest.languages),
        "file_count": manifest.file_count,
        "semantic_sha256": _json_digest(semantics),
    }
    view_identity = {
        "documents_sha256": receipts["documents.json"]["sha256"],
        "metadata_sha256": receipts["bm25_metadata.json"]["sha256"],
        "payload_bytes": payload_bytes,
        "payload_files": len(receipts),
    }
    return manifest_identity, view_identity, manifest


def _canonical_cache_identity(
    request: Mapping[str, Any],
    paths: Mapping[str, str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Plan the production portable BM25 projection without publishing it."""

    from codenib.artifacts.strict_bm25 import _plan_recaptured_bm25_view
    from codenib.compiler.cache_lock import compiler_cache_lock
    from codenib.compiler.manifest_storage import DEFAULT_MAX_MANIFEST_BYTES
    from codenib.source_fingerprint import capture_repository_source

    from codenib.compiler.cache_import import (  # isort: skip
        _parse_exact_manifest,
        _portable_manifest,
        _read_manifest,
        _require_current_view,
        _require_manifest_source,
        _require_source_fingerprints,
        _view_source,
        compiler_cache_source_selection,
    )

    subject = request["subject"]
    subject_root = Path(request["subject_root"])
    cache = _cache_manifest_path(subject_root).parent
    selection = compiler_cache_source_selection(cache)
    if selection.to_dict() != subject["source_selection"]:
        raise RuntimeError("compiler cache source selection differs from the subject")
    plan_destination = (
        Path(paths["sample_root"]) / f"canonical-plan-{request['run_id']}"
    )
    if os.path.lexists(plan_destination):
        raise RuntimeError("canonical BM25 plan destination unexpectedly exists")
    forbidden_paths = tuple(
        dict.fromkeys((cache, *(Path(value) for value in paths.values())))
    )
    with capture_repository_source(
        subject_root,
        exclude_roots=forbidden_paths,
        selection=selection,
    ) as repository_source:
        with compiler_cache_lock(cache, create=False):
            manifest = _parse_exact_manifest(
                _read_manifest(
                    cache,
                    max_manifest_bytes=DEFAULT_MAX_MANIFEST_BYTES,
                ),
                max_manifest_bytes=DEFAULT_MAX_MANIFEST_BYTES,
            )
            identity = repository_source.authenticated_identity_snapshot()
            _require_manifest_source(manifest, identity)
            entry = _require_current_view(manifest, view="bm25")
            source_view = _view_source(cache, entry, view="bm25")
            planned = _plan_recaptured_bm25_view(
                source_view,
                plan_destination,
                repository_source=repository_source,
                view_config=entry.config,
                forbidden_paths=forbidden_paths,
                environ=dict(os.environ),
            )
            _require_source_fingerprints(entry, planned)
            portable, _canonical_manifest_bytes = _portable_manifest(
                manifest,
                views=("bm25",),
                planned_views={"bm25": planned},
            )
    if (
        portable.commit != subject["revision"]
        or portable.languages != subject["languages"]
        or portable.source_selection is None
        or portable.source_selection.to_dict() != subject["source_selection"]
    ):
        raise RuntimeError("canonical BM25 plan differs from the fixed subject")
    output_records = {record.path: record for record in planned.output_records}
    if set(output_records) != set(_VIEW_FILES):
        raise RuntimeError("canonical BM25 plan has an unexpected inventory")
    documents = output_records["documents.json"]
    metadata = output_records["bm25_metadata.json"]
    manifest_identity = {
        "commit": portable.commit,
        "source_fingerprint": portable.source_fingerprint,
        "source_selection_digest": portable.source_selection_digest,
        "languages": list(portable.languages),
        "file_count": portable.file_count,
        "semantic_sha256": _json_digest(
            _normalized_portable_manifest_semantics(portable)
        ),
    }
    view_identity = {
        "documents_sha256": "sha256:" + documents.sha256,
        "metadata_sha256": "sha256:" + metadata.sha256,
        "payload_bytes": documents.size + metadata.size,
        "payload_files": len(output_records),
    }
    return manifest_identity, view_identity


def _query_payload(context: Any, subject: Mapping[str, Any]) -> list[dict[str, Any]]:
    from codenib.mcp.tools.search import search_bm25_impl

    payload: list[dict[str, Any]] = []
    for query in subject["queries"]:
        results = search_bm25_impl(
            context,
            query["text"],
            top_k=query["top_k"],
            filter_test=query["filter_test"],
        )
        if not results:
            raise RuntimeError("fixed BM25 query returned no results")
        payload.append({"query": dict(query), "results": results})
    return payload


def _query_identity_from_payload(
    payload: Sequence[Mapping[str, Any]],
    *,
    subject: Mapping[str, Any],
) -> dict[str, Any]:
    if len(payload) != len(subject["queries"]):
        raise RuntimeError("captured BM25 query count changed")
    return {
        "sha256": _json_digest(payload),
        "count": len(payload),
        "nonempty": True,
    }


def _query_identity(context: Any, subject: Mapping[str, Any]) -> dict[str, Any]:
    return _query_identity_from_payload(
        _query_payload(context, subject),
        subject=subject,
    )


def _route_result_from_manifest(
    manifest_path: Path,
    *,
    request: Mapping[str, Any],
    subject: Mapping[str, Any],
    manifest_identity: Mapping[str, Any],
    view_identity: Mapping[str, Any],
) -> tuple[dict[str, Any], bool]:
    from codenib.mcp.context import ServerContext

    active_context = ServerContext.load(manifest_path, views={"bm25"})
    closed = False
    try:
        manifest = active_context.manifest
        if (
            manifest.commit != subject["revision"]
            or manifest.languages != subject["languages"]
            or manifest.source_selection is None
            or manifest.source_selection.to_dict() != subject["source_selection"]
            or active_context.loaded_views != frozenset({"bm25"})
            or active_context.errors
        ):
            raise RuntimeError("compiler query context differs from its fixed subject")
        queries = _query_identity(active_context, subject)
    finally:
        if active_context is not None:
            active_context.close()
            closed = True
    result = {
        "manifest": dict(manifest_identity),
        "view": dict(view_identity),
        "retained_view": None,
        "queries": queries,
        "snapshot": {field: None for field in _SNAPSHOT_FIELDS},
        "authority": _expected_authority_identity(request),
    }
    return result, closed


def _runtime_context_state(context: Any) -> dict[str, Any]:
    return {
        "loaded_views": sorted(context.loaded_views),
        "errors": dict(context.errors),
        "artifact": None if context.artifact is None else dict(context.artifact),
        "source_verified": context.source_verified,
        "source_verification_scope": context.source_verification_scope,
        "vector_loaded": context.vector is not None,
    }


def _validate_runtime_context_state(
    value: object,
    *,
    request: Mapping[str, Any],
) -> dict[str, Any]:
    fields = frozenset(
        {
            "loaded_views",
            "errors",
            "artifact",
            "source_verified",
            "source_verification_scope",
            "vector_loaded",
        }
    )
    state = _require_exact_dict(value, fields, label="runtime context state")
    if (
        state["loaded_views"] != ["bm25"]
        or state["errors"] != {}
        or state["vector_loaded"] is not False
    ):
        raise RuntimeError("MCP route is not an exact clean BM25 context")
    observed = {
        "context_kind": (
            "manifest-live-source"
            if state["artifact"] is None
            else "portable-artifact-query-only"
        ),
        "artifact": state["artifact"],
        "source_verified": state["source_verified"],
        "source_verification_scope": state["source_verification_scope"],
    }
    return _validate_authority_identity(
        observed,
        request=request,
        label="runtime context authority",
    )


def _read_proc_io() -> tuple[int, int]:
    if not sys.platform.startswith("linux"):
        raise RuntimeError("canonical retained storage gate requires Linux /proc I/O")
    try:
        lines = Path("/proc/self/io").read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError) as exc:
        raise RuntimeError("Linux /proc/self/io is unavailable") from exc
    values: dict[str, int] = {}
    for line in lines:
        key, separator, raw = line.partition(":")
        if not separator:
            continue
        try:
            values[key] = int(raw.strip(), 10)
        except ValueError as exc:
            raise RuntimeError("Linux /proc/self/io is malformed") from exc
    if "read_bytes" not in values or "write_bytes" not in values:
        raise RuntimeError("Linux /proc/self/io lacks physical I/O counters")
    if values["read_bytes"] < 0 or values["write_bytes"] < 0:
        raise RuntimeError("Linux /proc/self/io contains negative counters")
    return values["read_bytes"], values["write_bytes"]


def _linux_peak_rss_bytes() -> int:
    try:
        lines = Path("/proc/self/status").read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError) as exc:
        raise RuntimeError("Linux /proc/self/status is unavailable") from exc
    for line in lines:
        if not line.startswith("VmHWM:"):
            continue
        fields = line.split()
        if len(fields) != 3 or fields[2] != "kB":
            break
        try:
            value = int(fields[1], 10)
        except ValueError:
            break
        if value > 0:
            return value * 1024
    raise RuntimeError("Linux VmHWM is unavailable or malformed")


def _freeze_route_measurement(
    *,
    wall_started: float,
    cpu_started: float,
    io_before: tuple[int, int],
) -> dict[str, float | int]:
    route_wall_seconds = time.perf_counter() - wall_started
    cpu_seconds = time.process_time() - cpu_started
    read_after, write_after = _read_proc_io()
    read_before, write_before = io_before
    if read_after < read_before or write_after < write_before:
        raise RuntimeError("Linux process I/O counters moved backwards")
    return {
        "route_wall_seconds": route_wall_seconds,
        "process_wall_seconds": 0.0,
        "cpu_seconds": cpu_seconds,
        "peak_rss_bytes": _linux_peak_rss_bytes(),
        "io_read_bytes": read_after - read_before,
        "io_write_bytes": write_after - write_before,
    }


def _runtime_arguments(
    request: Mapping[str, Any],
    paths: Mapping[str, str],
    *,
    generation: int | None,
) -> list[str]:
    if request["arm"] == "legacy":
        if generation is not None:
            raise RuntimeError("legacy runtime route must not receive a retained ref")
        if request["cell"] == "runtime-cold-query-only":
            return [
                "mcp",
                "--artifact",
                paths["direct_artifact"],
                "--repository",
                request["subject"]["repository_key"],
            ]
        return ["mcp", os.fspath(_cache_manifest_path(Path(request["subject_root"])))]
    if generation is None:
        raise RuntimeError("candidate runtime route requires a retained ref")
    return [
        "mcp",
        "--catalog",
        paths["catalog"],
        "--cas-root",
        paths["cas_root"],
        "--workspace-root",
        paths["workspace_root"],
        "--repository",
        request["subject"]["repository_key"],
        "--ref",
        "main",
        "--expected-generation",
        str(generation),
        "--output",
        paths["runtime_output"],
    ]


def _validate_route_config(value: object) -> tuple[dict[str, Any], dict[str, str]]:
    config = _require_exact_dict(
        value,
        frozenset({"operation", "request", "paths"}),
        label="route worker request",
    )
    if config["operation"] != "route":
        raise ValueError("inner worker requires operation='route'")
    request = _validate_sample_request(config["request"])
    expected_path_fields = frozenset(
        {
            "sample_root",
            "codenib_home",
            "temp_root",
            "results_root",
            "prebuilt_root",
            "catalog",
            "cas_root",
            "workspace_root",
            "direct_artifact",
            "runtime_output",
            "materialized_output",
        }
    )
    raw_paths = _require_exact_dict(
        config["paths"], expected_path_fields, label="route worker paths"
    )
    paths: dict[str, str] = {}
    sample_root = Path(
        _required_text(raw_paths["sample_root"], label="route sample root")
    )
    if not sample_root.is_absolute():
        raise ValueError("route sample root must be absolute")
    sample_root = sample_root.resolve(strict=True)
    media_root = Path(request["media_root"])
    expected_root = _owned_sample_root(request)
    if sample_root != expected_root or sample_root.parent != media_root:
        raise ValueError("route sample root differs from its exact request binding")
    expected_paths = _sample_paths(expected_root, request["run_id"])
    if raw_paths != expected_paths:
        raise ValueError("route authority paths differ from their exact binding")
    for name, raw_path in raw_paths.items():
        path = Path(_required_text(raw_path, label=f"route path {name}"))
        if not path.is_absolute():
            raise ValueError("route paths must be absolute")
        lexical = Path(os.path.abspath(os.fspath(path)))
        if name != "sample_root" and sample_root not in lexical.parents:
            raise ValueError(f"route path {name} escapes the owned sample root")
        paths[name] = os.fspath(lexical)
    return request, paths


def _route_worker(value: object) -> dict[str, Any]:
    request, paths = _validate_route_config(value)
    io_before = _read_proc_io()
    cpu_started = time.process_time()
    wall_started = time.perf_counter()
    context_closed = False
    route_result: dict[str, Any] | None = None
    metrics: dict[str, float | int] | None = None
    with _sample_environment(paths):
        if request["cell"] in {"compiler-cold", "compiler-current"}:
            _invoke_cli(
                _index_arguments(
                    request,
                    paths,
                    candidate=request["arm"] == "candidate",
                )
            )
            metrics = _freeze_route_measurement(
                wall_started=wall_started,
                cpu_started=cpu_started,
                io_before=io_before,
            )
            canonical_manifest, canonical_view = _canonical_cache_identity(
                request,
                paths,
            )
            route_result, context_closed = _route_result_from_manifest(
                _cache_manifest_path(Path(request["subject_root"])),
                request=request,
                subject=request["subject"],
                manifest_identity=canonical_manifest,
                view_identity=canonical_view,
            )
        else:
            from codenib.compiler.manifest import MANIFEST_FILENAME
            from codenib.mcp import server as server_module

            if request["arm"] == "candidate":
                runtime_manifest = Path(paths["runtime_output"]) / MANIFEST_FILENAME
                expected_generation = 1
            elif request["cell"] == "runtime-cold-query-only":
                runtime_manifest = Path(paths["direct_artifact"]) / MANIFEST_FILENAME
                expected_generation = None
            else:
                runtime_manifest = _cache_manifest_path(Path(request["subject_root"]))
                expected_generation = None
            captured: dict[str, Any] = {}

            def run_stdio(*, transport: str) -> None:
                if transport != "stdio":
                    raise RuntimeError("MCP route selected a non-stdio transport")
                context = server_module.get_context()
                captured["context_manifest"] = context.manifest.to_dict()
                captured["context_state"] = _runtime_context_state(context)
                captured["query_payload"] = _query_payload(
                    context,
                    request["subject"],
                )

            server_module.mcp.run = run_stdio
            try:
                _invoke_cli(
                    _runtime_arguments(
                        request,
                        paths,
                        generation=expected_generation,
                    )
                )
            finally:
                remaining = server_module._ctx  # noqa: SLF001 - benchmark cleanup
                if remaining is not None:
                    remaining.close()
                    server_module._ctx = None  # noqa: SLF001 - benchmark cleanup
            context_closed = server_module._ctx is None  # noqa: SLF001
            metrics = _freeze_route_measurement(
                wall_started=wall_started,
                cpu_started=cpu_started,
                io_before=io_before,
            )
            context_manifest = captured.get("context_manifest")
            context_state = captured.get("context_state")
            query_payload = captured.get("query_payload")
            if (
                type(context_manifest) is not dict
                or type(context_state) is not dict
                or type(query_payload) is not list
            ):
                raise RuntimeError("MCP benchmark callback did not observe a context")
            authority = _validate_runtime_context_state(
                context_state,
                request=request,
            )
            canonical_manifest, canonical_view = _canonical_cache_identity(
                request,
                paths,
            )
            if (
                request["arm"] == "candidate"
                or request["cell"] == "runtime-cold-query-only"
            ):
                actual_manifest, actual_view, manifest = _manifest_view_identity(
                    runtime_manifest,
                    subject=request["subject"],
                    subject_root=Path(request["subject_root"]),
                )
                if (
                    actual_manifest != canonical_manifest
                    or actual_view != canonical_view
                ):
                    raise RuntimeError(
                        "retained runtime materialization differs from its "
                        "canonical BM25 plan"
                    )
            else:
                from codenib.compiler.manifest import RepoManifest

                manifest = RepoManifest.load(runtime_manifest)
                actual_view = None
            if context_manifest != manifest.to_dict():
                raise RuntimeError(
                    "runtime context manifest differs from its persisted manifest"
                )
            route_result = {
                "manifest": canonical_manifest,
                "view": canonical_view,
                "retained_view": None,
                "queries": _query_identity_from_payload(
                    query_payload,
                    subject=request["subject"],
                ),
                "snapshot": {field: None for field in _SNAPSHOT_FIELDS},
                "authority": authority,
            }
            if request["arm"] == "candidate":
                route_result["retained_view"] = actual_view
    if route_result is None or metrics is None:
        raise RuntimeError("route worker produced no result")
    view = route_result["view"]
    metrics.update(
        {
            "payload_bytes": view["payload_bytes"],
            "payload_files": view["payload_files"],
        }
    )
    return {
        "operation": "route",
        "process_id": os.getpid(),
        "metrics": metrics,
        "result": route_result,
        "context_closed": context_closed,
    }


def _invoke_materialize(
    request: Mapping[str, Any],
    paths: Mapping[str, str],
    *,
    snapshot_id: str,
) -> Path:
    _invoke_cli(
        (
            "artifact",
            "materialize",
            "--catalog",
            paths["catalog"],
            "--cas-root",
            paths["cas_root"],
            "--workspace-root",
            paths["workspace_root"],
            "--repository",
            request["subject"]["repository_key"],
            "--snapshot",
            snapshot_id,
            "--output",
            paths["materialized_output"],
        )
    )
    return Path(paths["materialized_output"])


def _validate_route_result(value: object) -> dict[str, Any]:
    route = _require_exact_dict(
        value,
        frozenset({"operation", "process_id", "metrics", "result", "context_closed"}),
        label="inner route result",
    )
    if route["operation"] != "route":
        raise RuntimeError("inner route changed its operation identity")
    _positive_int(route["process_id"], label="inner route process ID")
    if type(route["metrics"]) is not dict or set(route["metrics"]) != _METRIC_FIELDS:
        raise RuntimeError("inner route metrics shape changed")
    if type(route["result"]) is not dict or set(route["result"]) != _RESULT_FIELDS:
        raise RuntimeError("inner route result shape changed")
    if type(route["context_closed"]) is not bool:
        raise RuntimeError("inner route context cleanup flag is malformed")
    return route


def _expected_ref_transition(
    request: Mapping[str, Any],
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
) -> tuple[bool, bool | None]:
    arm = request["arm"]
    cell = request["cell"]
    if arm == "legacy":
        return before is None and after is None, None
    if cell == "compiler-cold" and arm == "candidate":
        valid = before is None and after is not None and after["generation"] == 1
        return valid, True
    if before is None or after is None:
        return False, None
    stable = before == after and after["generation"] == 1
    if cell == "compiler-current" and arm == "candidate":
        return stable, False
    return stable, None


def _sample_worker(value: object) -> dict[str, Any]:
    request = _validate_sample_request(value)
    subject = request["subject"]
    subject_root = Path(request["subject_root"])
    media_root = Path(request["media_root"])
    before = _observe_subject(subject, subject_root)
    _require_expected_subject(subject, before)
    sample_root = _owned_sample_root(request)
    sample_root_fresh = False
    paths: dict[str, str] | None = None
    created = False
    receipt: dict[str, Any] | None = None
    primary: BaseException | None = None
    try:
        sample_root.mkdir(mode=0o700)
        created = True
        sample_root.chmod(0o700)
        sample_root_fresh = not any(sample_root.iterdir())
        paths = _sample_paths(sample_root, request["run_id"])
        _prepare_sample_directories(paths)
        with _sample_environment(paths):
            _prepare_sample(request, paths)
            storage_present = request["arm"] == "candidate"
            ref_before = (
                _read_ref(paths, subject["repository_key"]) if storage_present else None
            )
            route_request = {
                "operation": "route",
                "request": request,
                "paths": paths,
            }
            raw_route, process_wall = _run_worker_process(
                route_request,
                timeout_seconds=DEFAULT_WORKER_TIMEOUT_SECONDS,
                process_group=False,
            )
            route = _validate_route_result(raw_route)
            route["metrics"]["process_wall_seconds"] = process_wall
            ref_after = (
                _read_ref(paths, subject["repository_key"]) if storage_present else None
            )
            ref_stable, changed = _expected_ref_transition(
                request, ref_before, ref_after
            )
            result = route["result"]
            retained_matches_raw = True
            if request["arm"] == "candidate":
                if ref_after is None:
                    raise RuntimeError("candidate route did not retain a snapshot")
                result["snapshot"] = {
                    "snapshot_id": ref_after["snapshot_id"],
                    "ref_name": "main",
                    "generation": ref_after["generation"],
                    "changed": changed,
                }
                if request["cell"] in {"compiler-cold", "compiler-current"}:
                    from codenib.compiler.manifest import MANIFEST_FILENAME

                    materialized = _invoke_materialize(
                        request,
                        paths,
                        snapshot_id=ref_after["snapshot_id"],
                    )
                    retained_manifest, retained_view, _manifest = (
                        _manifest_view_identity(
                            materialized / MANIFEST_FILENAME,
                            subject=subject,
                            subject_root=subject_root,
                        )
                    )
                    retained_matches_raw = (
                        retained_manifest == result["manifest"]
                        and retained_view == result["view"]
                    )
                    result["retained_view"] = retained_view
                else:
                    raw_manifest, raw_view = _canonical_cache_identity(
                        request,
                        paths,
                    )
                    retained_matches_raw = (
                        raw_manifest == result["manifest"]
                        and raw_view == result["view"]
                        and result["retained_view"] == result["view"]
                    )
            else:
                result["snapshot"] = {field: None for field in _SNAPSHOT_FIELDS}
                result["retained_view"] = None
            if not retained_matches_raw:
                raise RuntimeError("retained materialization differs from raw BM25")
            after = _observe_subject(subject, subject_root)
            _require_expected_subject(subject, after)
            subject_unchanged = before == after
            if not subject_unchanged:
                raise RuntimeError("benchmark subject changed during a sample")
            view = result["view"]
            parity = {
                "manifest": dict(result["manifest"]),
                "view": {
                    "documents_sha256": view["documents_sha256"],
                    "metadata_sha256": view["metadata_sha256"],
                },
                "queries": dict(result["queries"]),
                "authority": _json_snapshot(result["authority"]),
            }
            receipt = {
                "schema_version": REPORT_SCHEMA_VERSION,
                "operation": "sample",
                "run_id": request["run_id"],
                "arm": request["arm"],
                "phase": request["phase"],
                "round_index": request["round_index"],
                "cell": request["cell"],
                "subject_id": subject["id"],
                "media_id": request["media_id"],
                "view_set_id": request["view_set"]["id"],
                "process_id": route["process_id"],
                "metrics": dict(route["metrics"]),
                "parity_identity": parity,
                "result": result,
                "safety": {
                    "subject_unchanged": subject_unchanged,
                    "sample_root_fresh": sample_root_fresh,
                    "cleanup_complete": False,
                    "storage_closed": True,
                    "context_closed": route["context_closed"],
                    "ref_stable": ref_stable,
                    "retained_matches_raw": retained_matches_raw,
                },
            }
    except BaseException as exc:  # noqa: B036 - cleanup before worker failure
        primary = exc
    if created:
        try:
            _cleanup_sample_root(sample_root, media_root, request["run_id"])
        except BaseException as cleanup_error:  # noqa: B036 - preserve primary
            if primary is None:
                primary = cleanup_error
            elif hasattr(primary, "add_note"):
                primary.add_note(f"sample cleanup also failed: {cleanup_error}")
    if primary is not None:
        raise primary
    if receipt is None:
        raise RuntimeError("outer sample worker produced no receipt")
    receipt["safety"]["cleanup_complete"] = True
    return receipt


def _parse_root_arguments(values: Sequence[str], *, option: str) -> dict[str, Path]:
    roots: dict[str, Path] = {}
    for value in values:
        identifier, separator, raw_path = value.partition("=")
        if not separator or not identifier or not raw_path:
            raise ValueError(f"{option} must use ID=/absolute/path")
        if identifier in roots:
            raise ValueError(f"duplicate {option} ID: {identifier}")
        roots[identifier] = Path(raw_path)
    if not roots:
        raise ValueError(f"at least one {option} is required")
    return roots


def _parseable_root_paths(values: Sequence[str]) -> dict[str, Path]:
    """Collect paths conservatively for output-topology checks after CLI errors."""

    paths: dict[str, Path] = {}
    for index, value in enumerate(values):
        _identifier, separator, raw_path = value.partition("=")
        if separator and raw_path:
            paths[f"argument-{index}"] = Path(raw_path)
    return paths


def _file_identity(metadata: os.stat_result) -> dict[str, int]:
    return {
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "mode": metadata.st_mode,
        "size": metadata.st_size,
        "mtime_ns": metadata.st_mtime_ns,
    }


def _validate_output_destination(
    output: Path,
    *,
    manifest_path: Path,
    subject_roots: Mapping[str, Path],
) -> dict[str, Any]:
    if not isinstance(output, Path):
        raise TypeError("output must be a pathlib.Path")
    destination = Path(os.path.abspath(os.fspath(output.expanduser())))
    if destination.name in {"", ".", ".."}:
        raise ValueError("output must name a report file")
    parent = _real_directory(destination.parent, label="output parent")
    if parent != destination.parent:
        raise ValueError("output parent must not resolve through aliases")

    manifest = Path(os.path.abspath(os.fspath(manifest_path.expanduser())))
    if destination == manifest:
        raise ValueError("output must not replace the selected subject manifest")
    benchmark_root = _real_directory(_PROJECT_ROOT, label="benchmark checkout")
    if _paths_overlap(destination, benchmark_root):
        raise ValueError("output must be outside the benchmark checkout")
    for subject_id, raw_root in subject_roots.items():
        lexical = Path(os.path.abspath(os.fspath(raw_root.expanduser())))
        resolved = lexical.resolve(strict=False)
        if _paths_overlap(destination, lexical) or _paths_overlap(
            destination, resolved
        ):
            raise ValueError(f"output must be outside subject checkout {subject_id!r}")

    parent_metadata = parent.lstat()
    leaf: dict[str, int] | None
    try:
        output_metadata = destination.lstat()
    except FileNotFoundError:
        leaf = None
    else:
        if stat.S_ISLNK(output_metadata.st_mode) or not stat.S_ISREG(
            output_metadata.st_mode
        ):
            raise ValueError("output must be absent or a non-symbolic regular file")
        leaf = _file_identity(output_metadata)

    try:
        manifest_metadata = manifest.lstat()
    except FileNotFoundError:
        manifest_metadata = None
    if (
        leaf is not None
        and manifest_metadata is not None
        and (leaf["device"], leaf["inode"])
        == (manifest_metadata.st_dev, manifest_metadata.st_ino)
    ):
        raise ValueError("output must not alias the selected subject manifest")
    return {
        "path": os.fspath(destination),
        "parent": {
            "path": os.fspath(parent),
            "device": parent_metadata.st_dev,
            "inode": parent_metadata.st_ino,
            "mode": parent_metadata.st_mode,
        },
        "leaf": leaf,
    }


def _require_output_destination_unchanged(
    expected: Mapping[str, Any],
    *,
    manifest_path: Path,
    subject_roots: Mapping[str, Path],
) -> Path:
    destination = Path(str(expected["path"]))
    observed = _validate_output_destination(
        destination,
        manifest_path=manifest_path,
        subject_roots=subject_roots,
    )
    if observed != expected:
        raise RuntimeError("output destination changed during measurement")
    return destination


def _read_worker_request() -> dict[str, Any]:
    payload = sys.stdin.buffer.read(_MAX_WORKER_REQUEST_BYTES + 1)
    if not payload:
        raise ValueError("worker request is empty")
    if len(payload) > _MAX_WORKER_REQUEST_BYTES:
        raise ValueError("worker request exceeds its size limit")
    try:
        text = payload.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite_constant,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("worker request is not strict UTF-8 JSON") from exc
    if type(value) is not dict:
        raise ValueError("worker request must be an exact JSON object")
    return value


def _worker_main() -> int:
    try:
        request = _read_worker_request()
        operation = request.get("operation")
        if operation == "sample":
            result = _sample_worker(request)
        elif operation == "route":
            result = _route_worker(request)
        else:
            raise ValueError(f"unsupported worker operation: {operation!r}")
        encoded = _canonical_json_bytes(result).decode("ascii")
    except BaseException as exc:  # noqa: B036 - worker must fail as one receipt
        error = {
            "error_type": type(exc).__name__,
            "message": str(exc)[:_MAX_REPORT_TEXT],
        }
        print(_canonical_json_bytes(error).decode("ascii"), file=sys.stderr)
        return 2
    print(encoded)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--subject-root",
        action="append",
        default=[],
        metavar="ID=PATH",
        help="fixed detached subject checkout; repeat for every subject",
    )
    parser.add_argument(
        "--media-root",
        action="append",
        default=[],
        metavar="ID=PATH",
        help="existing target-media directory; repeat for every media class",
    )
    parser.add_argument("--subject-manifest", type=Path, default=_DEFAULT_MANIFEST)
    parser.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS)
    parser.add_argument("--warmups", type=int, default=DEFAULT_WARMUPS)
    parser.add_argument(
        "--worker-timeout-seconds",
        type=float,
        default=DEFAULT_WORKER_TIMEOUT_SECONDS,
    )
    parser.add_argument("--output", type=Path, default=_DEFAULT_OUTPUT)
    return parser


def _write_cli_report(
    output_receipt: Mapping[str, Any],
    *,
    manifest_path: Path,
    subject_roots: Mapping[str, Path],
    report: Mapping[str, Any],
) -> None:
    destination = _require_output_destination_unchanged(
        output_receipt,
        manifest_path=manifest_path,
        subject_roots=subject_roots,
    )
    write_report_atomic(destination, report)


def _emit_cli_error(exc: BaseException) -> None:
    error = {
        "error_type": type(exc).__name__,
        "message": str(exc)[:_MAX_REPORT_TEXT],
    }
    print(_canonical_json_bytes(error).decode("ascii"), file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.worker:
        return _worker_main()

    subject_roots: dict[str, Path] = {}
    media_roots: dict[str, Path] = {}
    conservative_subject_roots = _parseable_root_paths(args.subject_root)
    output_receipt: dict[str, Any] | None = None
    try:
        subject_roots = _parse_root_arguments(
            args.subject_root,
            option="--subject-root",
        )
        media_roots = _parse_root_arguments(
            args.media_root,
            option="--media-root",
        )
        output_receipt = _validate_output_destination(
            args.output,
            manifest_path=args.subject_manifest,
            subject_roots=subject_roots,
        )
        report = profile_retained_storage_gate(
            subject_roots=subject_roots,
            media_roots=media_roots,
            manifest_path=args.subject_manifest,
            iterations=args.iterations,
            warmups=args.warmups,
            worker_timeout_seconds=args.worker_timeout_seconds,
        )
    except Exception as exc:
        report = _failure(
            _base_report(
                manifest_path=args.subject_manifest,
                iterations=args.iterations,
                warmups=args.warmups,
                worker_timeout_seconds=args.worker_timeout_seconds,
                subject_roots=subject_roots or conservative_subject_roots,
                media_roots=media_roots,
            ),
            "controller-validation",
            exc,
        )

    try:
        if output_receipt is None:
            output_receipt = _validate_output_destination(
                args.output,
                manifest_path=args.subject_manifest,
                subject_roots=subject_roots or conservative_subject_roots,
            )
        _write_cli_report(
            output_receipt,
            manifest_path=args.subject_manifest,
            subject_roots=subject_roots or conservative_subject_roots,
            report=report,
        )
    except Exception as exc:
        _emit_cli_error(exc)
        return 2
    return 0 if report["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
