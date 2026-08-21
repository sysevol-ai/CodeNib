# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Manifest-bound, graph-free access to the native SCIP FactQueryIndex.

This module deliberately has no dependency on ``CodeGraph``, ``igraph``, the
SCIP indexer facade, or ``LSIndexer``.  It publishes a native candidate only
when the persisted compiler inputs and the native fact surface are proven to
match the repository and filter identity recorded by the manifest.
"""

from __future__ import annotations

import copy
import hashlib
import importlib
import json
import math
import re
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib

from ..compiler.artifact_fingerprints import regular_file_fingerprint
from ..compiler.manifest import (
    LEGACY_MANIFEST_VERSION,
    MANIFEST_VERSION,
    SUPPORTED_MANIFEST_VERSIONS,
    IndexEntry,
    RepoManifest,
)
from ..compiler.manifest_source import (
    fingerprint_repository_for_manifest,
    repository_filter_policy_for_manifest,
    repository_source_selection_for_manifest,
)
from ..languages import graph_indexer_path, normalize_language
from ..repository_filters import walk_repository_files
from ..repository_source_selection import DEFAULT_REPOSITORY_SOURCE_SELECTION
from ..source_fingerprint import (
    SourceFingerprint,
    fingerprint_repository,
    is_secure_source_fingerprint_v2,
)
from .scip_filter import scip_path_is_allowed

FACT_QUERY_ABI_VERSION = 1
FACT_QUERY_FORMAT = "fact-query-index-v1"
FACT_QUERY_CAPABILITIES = {
    "definition_by_symbol": True,
    "references_by_symbol": True,
    "position_queries": False,
    "route_queries": False,
}
FACT_QUERY_PROMOTED_LANGUAGES = frozenset({"python", "rust"})
_ACTIVE_SCIP_INDEXERS = {
    "python": "codenib.scip_interface.scip_indexer_python:SCIPPythonIndexer",
    "rust": "codenib.scip_interface.scip_indexer_rust:SCIPRustIndexer",
}
FACT_QUERY_RECEIPT_SCHEMA_VERSION = 1
SCIP_INPUT_RECEIPT_SCHEMA_VERSION = 1
FACT_QUERY_FILTER_PROOF_SCHEMA_VERSION = 1
LEGACY_SYMBOL_GRAPH_BUILDER_SCHEMA_VERSION = 4
SYMBOL_GRAPH_BUILDER_SCHEMA_VERSION = 6

_HEX_DIGEST = re.compile(r"[0-9a-f]{64}")
_SOURCE_FINGERPRINT = re.compile(r"(?:sha256|sha256-v2):[0-9a-f]{64}")
_GIT_COMMIT = re.compile(r"[0-9a-f]{40}")
_INPUT_RECEIPT_KEYS = frozenset({"schema_version", "index", "metadata"})
_INPUT_INDEX_KEYS = frozenset({"path", "size_bytes", "sha256"})
_INPUT_METADATA_KEYS = frozenset({"kind", "complete", "inputs", "internal_crates"})
_INPUT_FILE_KEYS = frozenset({"path", "size_bytes", "sha256"})
_INDEX_ENTRY_V11_KEYS = frozenset(
    {
        "index_type",
        "path",
        "built_at",
        "built_at_epoch",
        "status",
        "config",
        "metadata",
        "commit",
        "source_fingerprint",
    }
)
_INDEX_ENTRY_V12_KEYS = _INDEX_ENTRY_V11_KEYS | frozenset({"source_selection_digest"})
_GRAPH_ARTIFACT_KEYS = frozenset({"relative_path", "size", "sha256"})
_FILTER_PROOF_KEYS = frozenset(
    {
        "schema_version",
        "identity",
        "allowed_file_count",
        "allowed_files_sha256",
        "record_count",
        "directory_count",
        "file_count",
        "definition_count",
        "reference_only_count",
        "edge_count",
        "reference_count",
        "occurrence_count",
        "query_surface_sha256",
    }
)
_NATIVE_PAYLOAD_KEYS = frozenset(
    {
        "format",
        "index",
        "native_decode_ns",
        "native_index_ns",
        "decode_profile_ns",
        "graph_materialized",
        "input_receipt",
    }
)
_LEGACY_SYMBOL_GRAPH_CONFIG_KEYS = frozenset(
    {
        "builder_schema",
        "languages",
        "graph_route",
        "exclude_patterns",
        "allow_partial_languages",
        "allow_partial_index",
        "source_coverage_fallback",
        "target_dir",
        "repository_filter_policy",
        "node_count",
        "edge_count",
        "language",
        "available_languages",
        "compiler_available_languages",
        "index_generation_reports",
        "scip_decoded_artifacts",
        "graph_artifact",
        "query_surface_sha256",
        "update_mode",
        "partial_index",
        "failed_languages",
        "partial",
    }
)
_SYMBOL_GRAPH_CONFIG_KEYS = _LEGACY_SYMBOL_GRAPH_CONFIG_KEYS | {
    "allow_project_preparation",
    "lsp_occurrence_artifact",
    "source_selection_digest",
    "source_selection_report",
}
_SOURCE_SELECTION_REPORT_KEYS = frozenset(
    {
        "source_selection_digest",
        "nodes_before",
        "nodes_after",
        "edges_before",
        "edges_after",
        "removed_excluded_path_count",
        "excluded_path_count_after",
        "invalid_path_count_before",
        "invalid_path_count_after",
    }
)


class FactQueryCandidateRejected(ValueError):
    """Raised when a graph-free candidate cannot prove exact equivalence."""


@dataclass(frozen=True, slots=True)
class NativeFactQueryIndex:
    """Validated raw native index plus the inputs the decoder actually read."""

    index: Any
    input_receipt: dict[str, Any]
    timings: dict[str, float | int]


@dataclass(frozen=True, slots=True)
class FactQueryCandidate:
    """A manifest-bound native index and its canonical consumer receipt."""

    index: Any
    receipt: dict[str, Any]
    timings: dict[str, float | int]

    def __getattr__(self, name: str) -> Any:
        """Preserve the FactQueryIndex query protocol for existing consumers."""

        return getattr(self.index, name)


_FACT_QUERY_RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "language",
        "project_root",
        "manifest",
        "index",
        "graph",
        "fact_query",
        "source",
        "filters",
        "native_input",
        "filter_identity",
        "receipt_sha256",
    }
)


def _reject(message: str) -> FactQueryCandidateRejected:
    return FactQueryCandidateRejected(message)


def _mapping(value: object, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _reject(f"{label} must be a mapping")
    if not all(type(key) is str for key in value):
        raise _reject(f"{label} keys must be strings")
    return value


def _require_exact_keys(
    value: Mapping[str, Any], expected: frozenset[str], *, label: str
) -> None:
    observed = frozenset(value)
    if observed != expected:
        missing = sorted(expected - observed)
        extra = sorted(observed - expected)
        raise _reject(f"{label} keys mismatch: missing={missing}, extra={extra}")


def _nonnegative_int(value: object, *, label: str) -> int:
    if type(value) is not int or value < 0:
        raise _reject(f"{label} must be a non-negative integer")
    return value


def _sha256(value: object, *, label: str) -> str:
    if type(value) is not str or _HEX_DIGEST.fullmatch(value) is None:
        raise _reject(f"{label} must be a lowercase SHA-256 digest")
    return value


def _canonical_relative_path(value: object, *, label: str) -> str:
    if type(value) is not str or not value or "\x00" in value or "\\" in value:
        raise _reject(f"{label} must be a canonical relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise _reject(f"{label} must be a canonical relative POSIX path")
    if path.as_posix() != value:
        raise _reject(f"{label} must be a canonical relative POSIX path")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise _reject(f"{label} must be UTF-8 encodable") from exc
    return value


def _canonical_json_digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_stable_file(path: Path, *, label: str) -> tuple[bytes, tuple[int, ...]]:
    """Read one file generation and return an identity for a final recheck."""

    try:
        before = path.stat()
        payload = path.read_bytes()
        after = path.stat()
    except (OSError, RuntimeError) as exc:
        raise _reject(f"{label} is unavailable") from exc
    before_identity = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    after_identity = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    if before_identity != after_identity or len(payload) != after.st_size:
        raise _reject(f"{label} changed while it was being read")
    return payload, after_identity


def _allowed_files_digest(paths: Sequence[str]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _checkout_head(project_root: Path) -> str:
    """Return the live Git HEAD, or ``""`` for a non-Git/unborn checkout."""

    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "--verify", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _reject("live repository commit could not be observed") from exc
    if result.returncode != 0:
        return ""
    head = result.stdout.strip().lower()
    if _GIT_COMMIT.fullmatch(head) is None:
        raise _reject("live repository returned a malformed Git commit")
    return head


def validate_fact_query_contract(contract: Mapping[str, Any]) -> None:
    """Validate the exact compiled FactQueryIndex v1 capability contract."""

    contract = _mapping(contract, label="FactQueryIndex contract")
    _require_exact_keys(
        contract,
        frozenset(
            {
                "abi_version",
                "format",
                "requires_anchored_references",
                "filter_identity_proof_schema_version",
                "input_receipt_schema_version",
                "capabilities",
            }
        ),
        label="FactQueryIndex contract",
    )
    if type(contract.get("abi_version")) is not int or (
        contract["abi_version"] != FACT_QUERY_ABI_VERSION
    ):
        raise RuntimeError(
            "compiled FactQueryIndex contract mismatch: expected ABI "
            f"{FACT_QUERY_ABI_VERSION}, got {contract.get('abi_version')!r}"
        )
    if contract.get("format") != FACT_QUERY_FORMAT:
        raise RuntimeError(
            "compiled FactQueryIndex contract mismatch: expected format "
            f"{FACT_QUERY_FORMAT!r}"
        )
    if contract.get("requires_anchored_references") is not True:
        raise RuntimeError(
            "compiled FactQueryIndex must fail closed on unanchored references"
        )
    if type(contract.get("filter_identity_proof_schema_version")) is not int or (
        contract["filter_identity_proof_schema_version"]
        != FACT_QUERY_FILTER_PROOF_SCHEMA_VERSION
    ):
        raise RuntimeError("compiled FactQueryIndex filter proof schema is not v1")
    if type(contract.get("input_receipt_schema_version")) is not int or (
        contract["input_receipt_schema_version"] != SCIP_INPUT_RECEIPT_SCHEMA_VERSION
    ):
        raise RuntimeError("compiled FactQueryIndex input receipt schema is not v1")
    capabilities = _mapping(
        contract.get("capabilities"), label="FactQueryIndex capabilities"
    )
    if dict(capabilities) != FACT_QUERY_CAPABILITIES:
        raise RuntimeError("compiled FactQueryIndex capabilities do not match v1")


def _validate_file_receipt(
    value: object,
    *,
    label: str,
    root: Path | None = None,
) -> dict[str, Any]:
    receipt = _mapping(value, label=label)
    _require_exact_keys(receipt, _INPUT_FILE_KEYS, label=label)
    relative = _canonical_relative_path(receipt["path"], label=f"{label}.path")
    size = _nonnegative_int(receipt["size_bytes"], label=f"{label}.size_bytes")
    digest = _sha256(receipt["sha256"], label=f"{label}.sha256")
    if root is not None:
        path = root / relative
        try:
            observed = regular_file_fingerprint(path)
        except (OSError, ValueError) as exc:
            raise _reject(f"{label} no longer names a regular input file") from exc
        if observed != {"size": size, "sha256": digest}:
            raise _reject(f"{label} changed after native decode")
    return {"path": relative, "size_bytes": size, "sha256": digest}


def _legacy_rust_cargo_identity(project_root: Path) -> tuple[list[str], list[str]]:
    """Recompute the serial Rust decoder's Cargo input and crate identity."""

    root_cargo = project_root / "Cargo.toml"
    if root_cargo.is_symlink() or not root_cargo.is_file():
        raise _reject("Rust project root has no regular Cargo.toml")
    try:
        with root_cargo.open("rb") as handle:
            cargo_data = tomllib.load(handle)
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        raise _reject("Rust root Cargo.toml cannot be parsed exactly") from exc
    if not isinstance(cargo_data, Mapping):
        raise _reject("Rust root Cargo.toml is not a table")
    workspace = cargo_data.get("workspace", {})
    package = cargo_data.get("package", {})
    if not isinstance(workspace, Mapping) or not isinstance(package, Mapping):
        raise _reject("Rust Cargo workspace/package metadata is malformed")
    member_patterns = workspace.get("members", [])
    if type(member_patterns) is not list:
        raise _reject("Rust workspace.members must be an array")

    root_name = package.get("name")
    if root_name is not None and (type(root_name) is not str or not root_name):
        raise _reject("Rust root package name is malformed")
    crates: set[str] = {root_name} if root_name else set()
    inputs = {"Cargo.toml"}
    for raw_pattern in member_patterns:
        pattern = str(raw_pattern).strip().rstrip("/")
        if not pattern or pattern.startswith("/"):
            continue
        try:
            members = list(project_root.glob(pattern))
        except (ValueError, IndexError, OSError) as exc:
            raise _reject("Rust workspace member glob cannot be proven") from exc
        for member_dir in members:
            if not member_dir.is_dir():
                continue
            member_cargo = member_dir / "Cargo.toml"
            if not member_cargo.exists():
                continue
            try:
                relative = member_cargo.relative_to(project_root).as_posix()
                relative = _canonical_relative_path(
                    relative, label="Rust workspace Cargo.toml path"
                )
                if member_cargo.is_symlink() or not member_cargo.is_file():
                    raise _reject("Rust workspace Cargo.toml is not a regular file")
                with member_cargo.open("rb") as handle:
                    member_data = tomllib.load(handle)
            except FactQueryCandidateRejected:
                raise
            except (OSError, ValueError, tomllib.TOMLDecodeError):
                # This exactly mirrors the serial decoder: an unreadable member
                # contributes neither an input nor an internal crate.
                continue
            if not isinstance(member_data, Mapping):
                continue
            member_package = member_data.get("package", {})
            if not isinstance(member_package, Mapping):
                continue
            crate_name = member_package.get("name")
            if crate_name is not None and (
                type(crate_name) is not str or not crate_name
            ):
                raise _reject("Rust workspace package name is malformed")
            inputs.add(relative)
            if crate_name:
                crates.add(crate_name)
    return (
        sorted(inputs, key=lambda item: item.encode("utf-8")),
        sorted(crates),
    )


def _validate_input_receipt(
    value: object,
    *,
    index_path: Path,
    project_root: Path | None,
    language: str,
) -> dict[str, Any]:
    receipt = _mapping(value, label="native input receipt")
    _require_exact_keys(receipt, _INPUT_RECEIPT_KEYS, label="native input receipt")
    if (
        receipt.get("schema_version") != SCIP_INPUT_RECEIPT_SCHEMA_VERSION
        or type(receipt.get("schema_version")) is not int
    ):
        raise _reject("native input receipt schema is not v1")

    index = _mapping(receipt.get("index"), label="native input receipt.index")
    _require_exact_keys(index, _INPUT_INDEX_KEYS, label="native input receipt.index")
    recorded_path = index.get("path")
    if type(recorded_path) is not str or recorded_path != str(index_path):
        raise _reject("native input receipt does not bind the resolved index path")
    index_size = _nonnegative_int(
        index.get("size_bytes"), label="native input receipt.index.size_bytes"
    )
    index_digest = _sha256(
        index.get("sha256"), label="native input receipt.index.sha256"
    )
    try:
        observed_index = regular_file_fingerprint(index_path)
    except (OSError, ValueError) as exc:
        raise _reject("native input index changed after decode") from exc
    if observed_index != {"size": index_size, "sha256": index_digest}:
        raise _reject("native input receipt does not match the decoded index")

    metadata = _mapping(receipt.get("metadata"), label="native input receipt.metadata")
    _require_exact_keys(
        metadata, _INPUT_METADATA_KEYS, label="native input receipt.metadata"
    )
    expected_kind = "rust-cargo" if language == "rust" else "none"
    if metadata.get("kind") != expected_kind:
        raise _reject("native input receipt metadata kind does not match the language")
    if metadata.get("complete") is not True:
        raise _reject("native decoder metadata inputs are incomplete")

    raw_inputs = metadata.get("inputs")
    if type(raw_inputs) is not list:
        raise _reject("native input receipt.metadata.inputs must be a list")
    inputs = [
        _validate_file_receipt(
            item,
            label=f"native input receipt.metadata.inputs[{position}]",
            root=project_root,
        )
        for position, item in enumerate(raw_inputs)
    ]
    input_paths = [item["path"] for item in inputs]
    if input_paths != sorted(input_paths, key=lambda item: item.encode("utf-8")) or len(
        input_paths
    ) != len(set(input_paths)):
        raise _reject("native metadata input paths must be unique and sorted")

    raw_crates = metadata.get("internal_crates")
    if type(raw_crates) is not list or not all(
        type(crate) is str and crate for crate in raw_crates
    ):
        raise _reject("native internal crate names must be non-empty strings")
    crates = list(raw_crates)
    if crates != sorted(crates) or len(crates) != len(set(crates)):
        raise _reject("native internal crate names must be unique and sorted")
    if language == "rust":
        if project_root is None or not inputs:
            raise _reject("Rust native decode did not prove its Cargo inputs")
        expected_inputs, expected_crates = _legacy_rust_cargo_identity(project_root)
        if input_paths != expected_inputs or crates != expected_crates:
            raise _reject(
                "native Cargo identity does not match the serial Rust decoder"
            )
    elif inputs or crates:
        raise _reject("non-Rust native decode unexpectedly read Cargo metadata")

    return {
        "schema_version": SCIP_INPUT_RECEIPT_SCHEMA_VERSION,
        "index": {
            "path": str(index_path),
            "size_bytes": index_size,
            "sha256": index_digest,
        },
        "metadata": {
            "kind": expected_kind,
            "complete": True,
            "inputs": inputs,
            "internal_crates": crates,
        },
    }


def decode_native_fact_query_index(
    *,
    index_file: str | Path,
    project_root: str | Path | None,
    language: str,
    core_module: Any | None = None,
) -> NativeFactQueryIndex:
    """Decode and validate a raw graph-free index without importing graph code.

    This is the compatibility primitive used by ``SCIPDecoderCore``.  It proves
    what the native decoder read, but only :func:`load_fact_query_candidate`
    additionally binds the index to compiler source and filter identity.
    """

    canonical_language = normalize_language(language)
    if canonical_language is None:
        raise ValueError(f"unsupported FactQueryIndex language: {language!r}")
    raw_index_path = Path(index_file).expanduser()
    if raw_index_path.is_symlink():
        raise _reject("FactQueryIndex input must be a regular, non-symlink file")
    try:
        index_path = raw_index_path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _reject("FactQueryIndex input file is unavailable") from exc
    if not index_path.is_file():
        raise _reject("FactQueryIndex input must be a regular, non-symlink file")
    try:
        root = (
            Path(project_root).expanduser().resolve(strict=True)
            if project_root is not None
            else None
        )
    except (OSError, RuntimeError) as exc:
        raise _reject("FactQueryIndex project root is unavailable") from exc
    if root is not None and not root.is_dir():
        raise _reject("FactQueryIndex project root must be a directory")

    core = core_module or importlib.import_module("codenib_core")
    if not hasattr(core, "decode_scip_fact_query_index") or not hasattr(
        core, "fact_query_index_contract"
    ):
        raise RuntimeError("compiled core does not expose FactQueryIndex v1")
    validate_fact_query_contract(core.fact_query_index_contract())

    started = time.perf_counter()
    payload = core.decode_scip_fact_query_index(
        index_file=str(index_path),
        project_root=str(root) if root is not None else None,
        language=canonical_language,
    )
    binding_seconds = time.perf_counter() - started
    payload = _mapping(payload, label="native FactQueryIndex payload")
    _require_exact_keys(payload, _NATIVE_PAYLOAD_KEYS, label="native payload")
    if payload.get("format") != FACT_QUERY_FORMAT:
        raise _reject("native FactQueryIndex returned an unknown format")
    if payload.get("graph_materialized") is not False:
        raise _reject("native FactQueryIndex unexpectedly materialized a graph")

    index = payload.get("index")
    if index is None or getattr(index, "fact_query_index", None) is not True:
        raise _reject("native FactQueryIndex payload is missing its index")
    if getattr(index, "materializes_graph", None) is not False:
        raise _reject("native FactQueryIndex does not guarantee graph-free use")
    if getattr(index, "capabilities", None) != FACT_QUERY_CAPABILITIES:
        raise _reject("native FactQueryIndex capabilities do not match v1")

    input_receipt = _validate_input_receipt(
        payload.get("input_receipt"),
        index_path=index_path,
        project_root=root,
        language=canonical_language,
    )
    native_decode_ns = _nonnegative_int(
        payload.get("native_decode_ns"), label="native_decode_ns"
    )
    native_index_ns = _nonnegative_int(
        payload.get("native_index_ns"), label="native_index_ns"
    )
    profile = _mapping(payload.get("decode_profile_ns"), label="decode_profile_ns")
    profile_values: dict[str, int] = {}
    for name, value in profile.items():
        profile_values[name] = _nonnegative_int(
            value, label=f"decode_profile_ns[{name!r}]"
        )

    native_decode = native_decode_ns / 1_000_000_000
    native_index = native_index_ns / 1_000_000_000
    timings: dict[str, float | int] = {
        "core_decode": binding_seconds,
        "native_decode": native_decode,
        "native_fact_query_index": native_index,
        "boundary_transport_residual": max(
            0.0, binding_seconds - native_decode - native_index
        ),
        "node_count": _nonnegative_int(
            getattr(index, "record_count", None), label="index.record_count"
        ),
        "edge_count": _nonnegative_int(
            getattr(index, "edge_count", None), label="index.edge_count"
        ),
        "definition_symbol_count": _nonnegative_int(
            getattr(index, "symbol_count", None), label="index.symbol_count"
        ),
        "reference_count": _nonnegative_int(
            getattr(index, "reference_count", None), label="index.reference_count"
        ),
        "occurrence_count": _nonnegative_int(
            getattr(index, "occurrence_count", None), label="index.occurrence_count"
        ),
    }
    for name, nanoseconds in profile_values.items():
        timings[f"native_{name}"] = (
            nanoseconds
            if name in {"worker_count", "document_count"}
            else nanoseconds / 1_000_000_000
        )
    return NativeFactQueryIndex(
        index=index,
        input_receipt=input_receipt,
        timings=timings,
    )


def _manifest_language(
    manifest: RepoManifest, entry: IndexEntry, requested: str | None
) -> tuple[str, list[str]]:
    languages = entry.config.get("languages")
    if type(languages) is not list or not all(type(item) is str for item in languages):
        raise _reject("symbol graph manifest languages are missing or malformed")
    canonical = [normalize_language(item) for item in languages]
    if len(canonical) != 1 or canonical[0] is None:
        raise _reject("FactQueryIndex v1 requires one canonical graph language")
    language = canonical[0]
    if language not in FACT_QUERY_PROMOTED_LANGUAGES:
        raise _reject(f"FactQueryIndex is not promoted for {language!r}")
    if graph_indexer_path(language) != _ACTIVE_SCIP_INDEXERS[language]:
        raise _reject(f"FactQueryIndex requires the active SCIP route for {language!r}")
    if requested is not None and normalize_language(requested) != language:
        raise _reject("requested language does not match the symbol graph manifest")
    if manifest.languages != [language]:
        raise _reject("FactQueryIndex v1 requires a single-language manifest")
    if languages != [language]:
        raise _reject("symbol graph language must use its canonical registry name")
    return language, list(languages)


def _validate_builder_entry(
    manifest: RepoManifest,
    entry: IndexEntry,
    *,
    requested_language: str | None,
) -> tuple[str, list[str], int, int, int, str]:
    if entry.index_type != "symbol_graph":
        raise _reject("symbol graph manifest entry has the wrong index type")
    if entry.status != "fresh" or not manifest.index_is_current("symbol_graph"):
        raise _reject("symbol graph manifest entry is not current")
    language, languages = _manifest_language(manifest, entry, requested_language)
    config = _mapping(entry.config, label="symbol graph config")
    metadata = _mapping(entry.metadata, label="symbol graph metadata")
    is_legacy = manifest.version == LEGACY_MANIFEST_VERSION
    expected_config_keys = (
        _LEGACY_SYMBOL_GRAPH_CONFIG_KEYS if is_legacy else _SYMBOL_GRAPH_CONFIG_KEYS
    )
    if set(config) != expected_config_keys:
        raise _reject("symbol graph config fields do not match its builder schema")
    if set(metadata) != set(config) | {"build_duration_seconds"}:
        raise _reject(
            "symbol graph config/metadata fields are not an exact build record"
        )
    duration = metadata.get("build_duration_seconds")
    if (
        type(duration) not in {int, float}
        or not math.isfinite(duration)
        or duration < 0
    ):
        raise _reject("symbol graph build duration is malformed")
    for key, value in config.items():
        if (
            key not in metadata
            or metadata[key] != value
            or type(metadata[key]) is not type(value)
        ):
            raise _reject(f"symbol graph config/metadata disagree for field {key!r}")

    required_config = {
        "builder_schema": (
            LEGACY_SYMBOL_GRAPH_BUILDER_SCHEMA_VERSION
            if is_legacy
            else SYMBOL_GRAPH_BUILDER_SCHEMA_VERSION
        ),
        "languages": languages,
        "graph_route": "active",
        "allow_partial_languages": False,
        "allow_partial_index": False,
        "source_coverage_fallback": False,
        "target_dir": None,
        "repository_filter_policy": repository_filter_policy_for_manifest(manifest),
    }
    for key, expected in required_config.items():
        if (
            key not in config
            or config[key] != expected
            or type(config[key]) is not type(expected)
        ):
            raise _reject(f"symbol graph config {key!r} is not candidate-safe")

    if not is_legacy:
        if type(config.get("allow_project_preparation")) is not bool:
            raise _reject(
                "symbol graph config 'allow_project_preparation' is malformed"
            )
        occurrence_artifact = config.get("lsp_occurrence_artifact")
        if occurrence_artifact is not None:
            occurrence_record = _mapping(
                occurrence_artifact,
                label="symbol graph LSP occurrence artifact",
            )
            _require_exact_keys(
                occurrence_record,
                _GRAPH_ARTIFACT_KEYS,
                label="symbol graph LSP occurrence artifact",
            )
            relative = _canonical_relative_path(
                occurrence_record.get("relative_path"),
                label="symbol graph LSP occurrence artifact relative_path",
            )
            if relative != "lsp_index.pkl":
                raise _reject(
                    "symbol graph LSP occurrence artifact must be lsp_index.pkl"
                )
            _nonnegative_int(
                occurrence_record.get("size"),
                label="symbol graph LSP occurrence artifact size",
            )
            _sha256(
                occurrence_record.get("sha256"),
                label="symbol graph LSP occurrence artifact sha256",
            )
        source_selection = repository_source_selection_for_manifest(manifest)
        if source_selection is None:
            raise _reject("current symbol graph manifest has no source selection")
        if config.get("source_selection_digest") != source_selection.digest:
            raise _reject("symbol graph source-selection identity is inconsistent")
        selection_report = _mapping(
            config.get("source_selection_report"),
            label="symbol graph source-selection report",
        )
        if set(selection_report) != _SOURCE_SELECTION_REPORT_KEYS:
            raise _reject("symbol graph source-selection report fields are malformed")
        if selection_report.get("source_selection_digest") != source_selection.digest:
            raise _reject(
                "symbol graph source-selection report identity is inconsistent"
            )
        report_counts = {
            key: _nonnegative_int(
                selection_report.get(key),
                label=f"symbol graph source-selection report {key}",
            )
            for key in _SOURCE_SELECTION_REPORT_KEYS
            if key != "source_selection_digest"
        }
        if (
            report_counts["nodes_after"] != config.get("node_count")
            or report_counts["edges_after"] != config.get("edge_count")
            or report_counts["nodes_before"] < report_counts["nodes_after"]
            or report_counts["edges_before"] < report_counts["edges_after"]
            or report_counts["excluded_path_count_after"] != 0
            or report_counts["invalid_path_count_after"] != 0
        ):
            raise _reject(
                "symbol graph manifest source-selection report is inconsistent"
            )

    patterns = config.get("exclude_patterns")
    if type(patterns) is not list or not all(type(item) is str for item in patterns):
        raise _reject("symbol graph exclude_patterns are malformed")
    if patterns != sorted(patterns):
        raise _reject("symbol graph exclude_patterns must be sorted")
    node_count = _nonnegative_int(config.get("node_count"), label="graph node_count")
    edge_count = _nonnegative_int(config.get("edge_count"), label="graph edge_count")
    query_surface_digest = _sha256(
        config.get("query_surface_sha256"), label="graph query_surface_sha256"
    )
    if node_count == 0:
        raise _reject("symbol graph manifest records an empty graph")

    required_metadata = {
        **required_config,
        "exclude_patterns": patterns,
        "language": language,
        "available_languages": [language],
        "compiler_available_languages": [language],
        "failed_languages": {},
        "partial": False,
        "partial_index": False,
        "update_mode": "full_rebuild",
    }
    for key, expected in required_metadata.items():
        if (
            key not in metadata
            or metadata[key] != expected
            or type(metadata[key]) is not type(expected)
        ):
            raise _reject(f"symbol graph metadata {key!r} is not candidate-safe")
    if "source_coverage_report" in metadata:
        raise _reject("source coverage fallback artifacts cannot publish a candidate")

    reports = metadata.get("index_generation_reports")
    if not isinstance(reports, Mapping):
        raise _reject("symbol graph index generation reports are missing")
    if set(reports) != {language}:
        raise _reject(f"{language} graph build must have one generation report")
    report = _mapping(
        reports[language], label=f"index generation report for {language}"
    )
    if set(report) != {
        "backend",
        "status",
        "complete",
        "partial",
        "document_count",
    }:
        raise _reject(f"{language} graph generation report fields are malformed")
    expected_backend = "scip-python" if language == "python" else "rust-analyzer-scip"
    if (
        report.get("backend") != expected_backend
        or report.get("partial") is not False
        or report.get("complete") is not True
        or report.get("status") != "complete"
    ):
        raise _reject("symbol graph compiler index is not complete")
    document_count = _nonnegative_int(
        report.get("document_count"), label=f"{language} graph document_count"
    )
    if document_count == 0:
        raise _reject("symbol graph generation report has no source documents")
    return (
        language,
        patterns,
        node_count,
        edge_count,
        document_count,
        query_surface_digest,
    )


def _resolve_artifact(
    entry: IndexEntry,
    *,
    language: str,
    expected_root: Path,
) -> tuple[Path, str, dict[str, Any]]:
    entry_root_raw = Path(entry.path).expanduser()
    if not entry_root_raw.is_absolute() or entry_root_raw.is_symlink():
        raise _reject("symbol graph entry path must be an absolute artifact directory")
    if entry_root_raw != expected_root:
        raise _reject("symbol graph entry path is not the manifest symbol_graph view")
    try:
        entry_root = entry_root_raw.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _reject("symbol graph artifact directory is unavailable") from exc
    if not entry_root.is_dir():
        raise _reject("symbol graph artifact path is not a directory")

    config_artifacts = _mapping(
        entry.config.get("scip_decoded_artifacts"),
        label="symbol graph SCIP artifact records",
    )
    metadata_artifacts = _mapping(
        entry.metadata.get("scip_decoded_artifacts"),
        label="symbol graph SCIP artifact metadata",
    )
    if dict(config_artifacts) != dict(metadata_artifacts):
        raise _reject("symbol graph SCIP artifact records disagree")
    if set(config_artifacts) != {language}:
        raise _reject("manifest does not bind one decoded SCIP artifact")
    record = _mapping(
        config_artifacts[language], label=f"decoded SCIP artifact for {language}"
    )
    _require_exact_keys(
        record,
        frozenset({"relative_path", "size", "sha256"}),
        label=f"decoded SCIP artifact for {language}",
    )
    relative = _canonical_relative_path(
        record.get("relative_path"), label="decoded SCIP artifact relative_path"
    )
    if relative != "index.decoded":
        raise _reject("single-language SCIP artifact must be index.decoded")
    size = _nonnegative_int(record.get("size"), label="decoded SCIP artifact size")
    digest = _sha256(record.get("sha256"), label="decoded SCIP artifact sha256")
    artifact = entry_root / relative
    try:
        resolved = artifact.resolve(strict=True)
        resolved.relative_to(entry_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _reject("decoded SCIP artifact escapes its manifest directory") from exc
    if artifact.is_symlink() or not resolved.is_file():
        raise _reject("decoded SCIP artifact must be a regular, non-symlink file")
    expected = {"size": size, "sha256": digest}
    try:
        observed = regular_file_fingerprint(resolved)
    except (OSError, ValueError) as exc:
        raise _reject("decoded SCIP artifact could not be fingerprinted") from exc
    if observed != expected:
        raise _reject("decoded SCIP artifact does not match its manifest fingerprint")
    return resolved, relative, expected


def _resolve_graph_artifact(
    entry: IndexEntry,
    *,
    expected_root: Path,
) -> tuple[Path, dict[str, Any]]:
    record = _mapping(
        entry.config.get("graph_artifact"), label="symbol graph artifact record"
    )
    _require_exact_keys(record, _GRAPH_ARTIFACT_KEYS, label="symbol graph artifact")
    relative = _canonical_relative_path(
        record.get("relative_path"), label="symbol graph artifact relative_path"
    )
    if relative != "graph.pkl":
        raise _reject("symbol graph artifact must be graph.pkl")
    size = _nonnegative_int(record.get("size"), label="symbol graph artifact size")
    digest = _sha256(record.get("sha256"), label="symbol graph artifact sha256")
    artifact = expected_root / relative
    try:
        resolved = artifact.resolve(strict=True)
        resolved.relative_to(expected_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _reject("symbol graph artifact escapes its manifest directory") from exc
    if artifact.is_symlink() or not resolved.is_file():
        raise _reject("symbol graph artifact must be a regular, non-symlink file")
    expected = {"size": size, "sha256": digest}
    try:
        observed = regular_file_fingerprint(resolved)
    except (OSError, ValueError) as exc:
        raise _reject("symbol graph artifact could not be fingerprinted") from exc
    if observed != expected:
        raise _reject("symbol graph artifact does not match its manifest fingerprint")
    return resolved, expected


def _source_and_allowed_files(
    project_root: Path,
    *,
    cache_root: Path,
    exclude_patterns: Iterable[str],
    target_dir: str | None,
    manifest: RepoManifest | None = None,
) -> tuple[SourceFingerprint, list[str]]:
    if manifest is None:
        selection = DEFAULT_REPOSITORY_SOURCE_SELECTION
        source = fingerprint_repository(project_root, exclude_roots=(cache_root,))
    else:
        persisted_selection = repository_source_selection_for_manifest(manifest)
        selection = persisted_selection or DEFAULT_REPOSITORY_SOURCE_SELECTION
        source = fingerprint_repository_for_manifest(
            project_root,
            manifest,
            exclude_roots=(cache_root,),
        )
    allowed: list[str] = []
    seen: set[str] = set()
    for path in walk_repository_files(
        project_root,
        exclude_roots=(cache_root,),
        selection=selection,
    ):
        try:
            mode = path.lstat().st_mode
        except OSError as exc:
            raise _reject("repository file disappeared during traversal") from exc
        if not (stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
            continue
        relative = _canonical_relative_path(
            path.relative_to(project_root).as_posix(), label="repository file path"
        )
        if not scip_path_is_allowed(
            relative,
            target_dir=target_dir,
            exclude_patterns=exclude_patterns,
            enforce_repository_policy=True,
        ):
            continue
        if relative in seen:
            raise _reject("repository traversal returned a duplicate path")
        seen.add(relative)
        allowed.append(relative)
    allowed.sort(key=lambda item: item.encode("utf-8"))
    return source, allowed


def _validate_filter_proof(
    value: object,
    *,
    allowed_files: Sequence[str],
    native: NativeFactQueryIndex,
    expected_record_count: int,
    expected_edge_count: int,
    expected_query_surface_sha256: str,
    repository_filter_policy: int,
    source_selection_digest: str | None,
) -> dict[str, Any]:
    proof = _mapping(value, label="native filter identity proof")
    _require_exact_keys(proof, _FILTER_PROOF_KEYS, label="native filter proof")
    if (
        proof.get("schema_version") != FACT_QUERY_FILTER_PROOF_SCHEMA_VERSION
        or type(proof.get("schema_version")) is not int
    ):
        raise _reject("native filter identity proof schema is not v1")
    if proof.get("identity") is not True:
        raise _reject("native fact surface did not prove filter identity")

    counts: dict[str, int] = {}
    for key in _FILTER_PROOF_KEYS - {
        "schema_version",
        "identity",
        "allowed_files_sha256",
        "query_surface_sha256",
    }:
        counts[key] = _nonnegative_int(proof.get(key), label=f"filter proof {key}")
    expected_digest = _allowed_files_digest(allowed_files)
    if (
        counts["allowed_file_count"] != len(allowed_files)
        or _sha256(
            proof.get("allowed_files_sha256"), label="filter proof allowed_files_sha256"
        )
        != expected_digest
    ):
        raise _reject("native filter proof does not bind the allowed file set")
    proof_query_surface = _sha256(
        proof.get("query_surface_sha256"), label="filter proof query_surface_sha256"
    )
    if proof_query_surface != expected_query_surface_sha256:
        raise _reject("native query surface disagrees with the compiled symbol graph")
    if counts["record_count"] != native.timings["node_count"]:
        raise _reject("native filter proof record count disagrees with the index")
    if counts["record_count"] != expected_record_count:
        raise _reject("native filter proof record count disagrees with the manifest")
    if counts["edge_count"] != native.timings["edge_count"]:
        raise _reject("native filter proof edge count disagrees with the index")
    if counts["edge_count"] != expected_edge_count:
        raise _reject("native filter proof edge count disagrees with the manifest")
    if counts["definition_count"] != native.timings["definition_symbol_count"]:
        raise _reject("native filter proof definition count disagrees with the index")
    if counts["reference_count"] != native.timings["reference_count"]:
        raise _reject("native filter proof reference count disagrees with the index")
    if counts["file_count"] > counts["allowed_file_count"]:
        raise _reject("native filter proof contains more files than were allowed")
    if counts["file_count"] == 0 or counts["definition_count"] == 0:
        raise _reject("native filter proof has no queryable source definitions")
    if (
        1
        + counts["directory_count"]
        + counts["file_count"]
        + counts["definition_count"]
        + counts["reference_only_count"]
        != counts["record_count"]
    ):
        raise _reject("native filter proof contains an unknown record class")
    if counts["occurrence_count"] != 0:
        raise _reject("native filter proof occurrence count must be zero")
    if counts["occurrence_count"] != native.timings["occurrence_count"]:
        raise _reject("native filter proof occurrence count disagrees with the index")
    return {
        "schema_version": FACT_QUERY_FILTER_PROOF_SCHEMA_VERSION,
        "identity": True,
        "repository_filter_policy": repository_filter_policy,
        "source_selection_digest": source_selection_digest,
        "allowed_files_sha256": expected_digest,
        "query_surface_sha256": proof_query_surface,
        **counts,
    }


def load_fact_query_candidate(
    manifest_path: str | Path,
    *,
    language: str | None = None,
    project_root: str | Path | None = None,
    core_module: Any | None = None,
) -> FactQueryCandidate:
    """Load one consumer-safe native candidate or reject the whole attempt.

    No row-level filtering or fallback occurs here.  Callers that want a full
    graph fallback must perform it outside this graph-free module after a
    rejection.
    """

    total_started = time.perf_counter()
    raw_manifest_path = Path(manifest_path).expanduser()
    if raw_manifest_path.is_symlink():
        raise _reject("RepoManifest must be a regular, non-symlink file")
    try:
        resolved_manifest = raw_manifest_path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _reject("RepoManifest is unavailable") from exc
    if not resolved_manifest.is_file():
        raise _reject("RepoManifest must be a regular, non-symlink file")
    manifest_bytes, manifest_stat_identity = _read_stable_file(
        resolved_manifest, label="RepoManifest"
    )
    manifest_file_fingerprint = {
        "size": len(manifest_bytes),
        "sha256": hashlib.sha256(manifest_bytes).hexdigest(),
    }
    try:
        raw_manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise _reject("RepoManifest is not readable canonical JSON") from exc
    raw_manifest = _mapping(raw_manifest, label="RepoManifest")
    raw_version = raw_manifest.get("version")
    if type(raw_version) is not str or raw_version not in SUPPORTED_MANIFEST_VERSIONS:
        raise _reject("RepoManifest must explicitly record a supported version")
    raw_repo = _mapping(raw_manifest.get("repo"), label="RepoManifest.repo")
    for required in ("path", "commit", "source_fingerprint", "file_count"):
        if required not in raw_repo:
            raise _reject(f"RepoManifest.repo is missing {required!r}")
    if type(raw_repo["path"]) is not str or not raw_repo["path"]:
        raise _reject("RepoManifest repository path is malformed")
    if type(raw_repo["commit"]) is not str or (
        raw_repo["commit"] and _GIT_COMMIT.fullmatch(raw_repo["commit"]) is None
    ):
        raise _reject("RepoManifest commit is malformed")
    if (
        type(raw_repo["source_fingerprint"]) is not str
        or _SOURCE_FINGERPRINT.fullmatch(raw_repo["source_fingerprint"]) is None
    ):
        raise _reject("RepoManifest source fingerprint is malformed")
    if type(raw_repo["file_count"]) is not int or raw_repo["file_count"] < 0:
        raise _reject("RepoManifest file_count is malformed")
    raw_indexes = _mapping(raw_manifest.get("indexes"), label="RepoManifest.indexes")
    raw_entry = _mapping(
        raw_indexes.get("symbol_graph"), label="RepoManifest symbol_graph entry"
    )
    _require_exact_keys(
        raw_entry,
        (
            _INDEX_ENTRY_V12_KEYS
            if raw_version == MANIFEST_VERSION
            else _INDEX_ENTRY_V11_KEYS
        ),
        label="RepoManifest symbol_graph entry",
    )
    if raw_entry.get("index_type") != "symbol_graph":
        raise _reject("RepoManifest symbol_graph index_type is malformed")
    if type(raw_entry.get("path")) is not str or not raw_entry["path"]:
        raise _reject("RepoManifest symbol_graph path is malformed")
    if type(raw_entry.get("built_at")) is not str or not raw_entry["built_at"]:
        raise _reject("RepoManifest symbol_graph built_at is malformed")
    built_at_epoch = raw_entry.get("built_at_epoch")
    if (
        type(built_at_epoch) not in {int, float}
        or not math.isfinite(built_at_epoch)
        or built_at_epoch < 0
    ):
        raise _reject("RepoManifest symbol_graph built_at_epoch is malformed")
    if raw_entry.get("status") != "fresh":
        raise _reject("RepoManifest symbol_graph status is not fresh")
    _mapping(raw_entry.get("config"), label="RepoManifest symbol_graph config")
    _mapping(raw_entry.get("metadata"), label="RepoManifest symbol_graph metadata")
    raw_entry_commit = raw_entry.get("commit")
    if type(raw_entry_commit) is not str or (
        raw_entry_commit and _GIT_COMMIT.fullmatch(raw_entry_commit) is None
    ):
        raise _reject("RepoManifest symbol_graph commit is malformed")
    if raw_entry_commit != raw_repo["commit"]:
        raise _reject("RepoManifest symbol_graph commit provenance is incomplete")
    raw_entry_source = raw_entry.get("source_fingerprint")
    if (
        type(raw_entry_source) is not str
        or _SOURCE_FINGERPRINT.fullmatch(raw_entry_source) is None
    ):
        raise _reject("RepoManifest symbol_graph source fingerprint is malformed")
    if raw_entry_source != raw_repo["source_fingerprint"]:
        raise _reject(
            "RepoManifest symbol_graph source fingerprint provenance is incomplete"
        )
    try:
        manifest = RepoManifest.from_dict(dict(raw_manifest))
    except (KeyError, TypeError, ValueError) as exc:
        raise _reject("RepoManifest fields are malformed") from exc
    if type(manifest.file_count) is not int or manifest.file_count < 0:
        raise _reject("RepoManifest file_count is malformed")
    if type(manifest.source_fingerprint) is not str or not manifest.source_fingerprint:
        raise _reject("RepoManifest source fingerprint is missing")
    if manifest.version == MANIFEST_VERSION and not is_secure_source_fingerprint_v2(
        manifest.source_fingerprint
    ):
        raise _reject("RepoManifest v1.2 requires source fingerprint v2")

    try:
        manifest_root = Path(manifest.repo_path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _reject("RepoManifest repository path is unavailable") from exc
    if not manifest_root.is_dir():
        raise _reject("RepoManifest repository path is not a directory")
    if project_root is not None:
        try:
            supplied_root = Path(project_root).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise _reject("supplied repository root is unavailable") from exc
        if supplied_root != manifest_root:
            raise _reject("supplied repository root does not match RepoManifest")

    entry = manifest.indexes.get("symbol_graph")
    if entry is None:
        raise _reject("RepoManifest has no symbol graph entry")
    (
        canonical_language,
        exclude_patterns,
        node_count,
        edge_count,
        document_count,
        query_surface_digest,
    ) = _validate_builder_entry(manifest, entry, requested_language=language)
    source_selection = repository_source_selection_for_manifest(manifest)
    source_selection_payload = (
        None if source_selection is None else source_selection.to_dict()
    )
    source_selection_digest = (
        None if source_selection is None else source_selection.digest
    )
    repository_filter_policy = repository_filter_policy_for_manifest(manifest)
    entry_source_selection_digest = (
        None if source_selection is None else entry.source_selection_digest
    )
    artifact_root = resolved_manifest.parent / "symbol_graph"
    index_path, index_relative, expected_index = _resolve_artifact(
        entry,
        language=canonical_language,
        expected_root=artifact_root,
    )
    graph_path, expected_graph = _resolve_graph_artifact(
        entry,
        expected_root=artifact_root,
    )
    cache_root = resolved_manifest.parent.resolve()
    checkout_head_before = _checkout_head(manifest_root)
    if checkout_head_before != manifest.commit:
        raise _reject("live repository commit does not match RepoManifest")
    source_before, allowed_files = _source_and_allowed_files(
        manifest_root,
        cache_root=cache_root,
        exclude_patterns=exclude_patterns,
        target_dir=None,
        manifest=manifest,
    )
    if (
        source_before.value != manifest.source_fingerprint
        or source_before.file_count != manifest.file_count
        or entry.source_fingerprint != manifest.source_fingerprint
        or entry.commit != manifest.commit
    ):
        raise _reject("repository source does not match the symbol graph manifest")
    source_before_decode = fingerprint_repository_for_manifest(
        manifest_root,
        manifest,
        exclude_roots=(cache_root,),
    )
    if source_before_decode != source_before:
        raise _reject("repository source changed while computing allowed files")

    native = decode_native_fact_query_index(
        index_file=index_path,
        project_root=manifest_root,
        language=canonical_language,
        core_module=core_module,
    )
    native_index = native.input_receipt["index"]
    if (
        native_index["size_bytes"] != expected_index["size"]
        or native_index["sha256"] != expected_index["sha256"]
    ):
        raise _reject("native decoder read a different index generation")
    if native.timings.get("native_document_count") != document_count:
        raise _reject(
            "native decoder document count disagrees with the generation report"
        )
    allowed_file_set = set(allowed_files)
    for metadata_input in native.input_receipt["metadata"]["inputs"]:
        if metadata_input["path"] not in allowed_file_set:
            raise _reject(
                "native metadata input is outside the manifest source surface"
            )

    prove = getattr(native.index, "prove_filter_identity", None)
    if not callable(prove):
        raise _reject("native FactQueryIndex cannot prove filter identity")
    try:
        raw_proof = prove(list(allowed_files), query_surface_digest)
    except (TypeError, ValueError) as exc:
        raise _reject("native filter identity proof rejected candidate") from exc
    proof = _validate_filter_proof(
        raw_proof,
        allowed_files=allowed_files,
        native=native,
        expected_record_count=node_count,
        expected_edge_count=edge_count,
        expected_query_surface_sha256=query_surface_digest,
        repository_filter_policy=repository_filter_policy,
        source_selection_digest=source_selection_digest,
    )

    source_after = fingerprint_repository_for_manifest(
        manifest_root,
        manifest,
        exclude_roots=(cache_root,),
    )
    try:
        index_after = regular_file_fingerprint(index_path)
        graph_after = regular_file_fingerprint(graph_path)
    except (OSError, ValueError) as exc:
        raise _reject("symbol graph artifacts disappeared during construction") from exc
    if source_after != source_before:
        raise _reject("repository source changed during candidate construction")
    checkout_head_after = _checkout_head(manifest_root)
    if checkout_head_after != checkout_head_before:
        raise _reject("live repository commit changed during candidate construction")
    if index_after != expected_index:
        raise _reject("decoded SCIP artifact changed during candidate construction")
    if graph_after != expected_graph:
        raise _reject("symbol graph artifact changed during candidate construction")
    try:
        final_manifest_identity = resolved_manifest.stat()
        manifest_after = regular_file_fingerprint(resolved_manifest)
    except (OSError, ValueError) as exc:
        raise _reject("RepoManifest disappeared during candidate construction") from exc
    final_stat_identity = (
        final_manifest_identity.st_dev,
        final_manifest_identity.st_ino,
        final_manifest_identity.st_size,
        final_manifest_identity.st_mtime_ns,
    )
    if (
        resolved_manifest.is_symlink()
        or final_stat_identity != manifest_stat_identity
        or manifest_after != manifest_file_fingerprint
    ):
        raise _reject("RepoManifest changed during candidate construction")

    receipt: dict[str, Any] = {
        "schema_version": FACT_QUERY_RECEIPT_SCHEMA_VERSION,
        "language": canonical_language,
        "project_root": str(manifest_root),
        "manifest": {
            "path": str(resolved_manifest),
            "version": manifest.version,
            "size_bytes": manifest_file_fingerprint["size"],
            "sha256": manifest_file_fingerprint["sha256"],
            "commit": manifest.commit,
            "builder_schema": entry.config["builder_schema"],
            "node_count": node_count,
            "edge_count": edge_count,
            "document_count": document_count,
            "query_surface_sha256": query_surface_digest,
            "source_fingerprint": manifest.source_fingerprint,
            "file_count": manifest.file_count,
            "source_selection": source_selection_payload,
            "source_selection_digest": source_selection_digest,
            "entry_path": str(index_path.parent),
            "entry_commit": entry.commit,
            "entry_source_fingerprint": entry.source_fingerprint,
            "entry_source_selection_digest": entry_source_selection_digest,
        },
        "index": {
            "relative_path": index_relative,
            "size_bytes": expected_index["size"],
            "sha256": expected_index["sha256"],
        },
        "graph": {
            "relative_path": "graph.pkl",
            "size_bytes": expected_graph["size"],
            "sha256": expected_graph["sha256"],
            "query_surface_sha256": query_surface_digest,
        },
        "fact_query": {
            "abi_version": FACT_QUERY_ABI_VERSION,
            "format": FACT_QUERY_FORMAT,
            "capabilities": dict(FACT_QUERY_CAPABILITIES),
        },
        "source": {
            "fingerprint": source_before.value,
            "file_count": source_before.file_count,
            "repository_filter_policy": repository_filter_policy,
            "source_selection": source_selection_payload,
            "source_selection_digest": source_selection_digest,
        },
        "filters": {
            "graph_route": "active",
            "update_mode": "full_rebuild",
            "repository_filter_policy": repository_filter_policy,
            "source_selection": source_selection_payload,
            "source_selection_digest": source_selection_digest,
            "exclude_patterns": list(exclude_patterns),
            "target_dir": None,
            "allow_partial_languages": False,
            "allow_partial_index": False,
            "source_coverage_fallback": False,
            "allowed_file_count": len(allowed_files),
            "allowed_files_sha256": _allowed_files_digest(allowed_files),
        },
        "native_input": native.input_receipt,
        "filter_identity": proof,
    }
    receipt["receipt_sha256"] = _canonical_json_digest(receipt)
    timings = dict(native.timings)
    timings["candidate_total"] = time.perf_counter() - total_started
    timings["allowed_file_count"] = len(allowed_files)
    return FactQueryCandidate(index=native.index, receipt=receipt, timings=timings)


def observe_fact_query_snapshot(value: Mapping[str, Any]) -> dict[str, Any]:
    """Re-observe every mutable input bound by one admitted receipt.

    The observer deliberately remains graph-free.  A lazy consumer calls it
    immediately before and after loading the already-persisted ``graph.pkl``;
    equality of the two returned observations proves that the graph, decoded
    index, manifest, checkout, filter surface, and language metadata stayed on
    one generation throughout publication.
    """

    receipt = _mapping(value, label="FactQuery candidate receipt")
    _require_exact_keys(
        receipt,
        _FACT_QUERY_RECEIPT_KEYS,
        label="FactQuery candidate receipt",
    )
    if (
        type(receipt.get("schema_version")) is not int
        or receipt["schema_version"] != FACT_QUERY_RECEIPT_SCHEMA_VERSION
    ):
        raise _reject("FactQuery candidate receipt schema is not v1")
    recorded_digest = _sha256(
        receipt.get("receipt_sha256"), label="FactQuery receipt_sha256"
    )
    unsigned = {
        key: copy.deepcopy(item)
        for key, item in receipt.items()
        if key != "receipt_sha256"
    }
    if _canonical_json_digest(unsigned) != recorded_digest:
        raise _reject("FactQuery candidate receipt digest does not match its fields")

    raw_language = receipt.get("language")
    if type(raw_language) is not str:
        raise _reject("FactQuery candidate receipt language is malformed")
    language = normalize_language(raw_language)
    if language not in FACT_QUERY_PROMOTED_LANGUAGES or (
        receipt.get("language") != language
    ):
        raise _reject("FactQuery candidate receipt language is not canonical")
    project_root_value = receipt.get("project_root")
    if type(project_root_value) is not str or not project_root_value:
        raise _reject("FactQuery candidate project root is malformed")
    try:
        project_root = Path(project_root_value).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _reject("FactQuery candidate project root is unavailable") from exc
    if not project_root.is_dir() or str(project_root) != project_root_value:
        raise _reject("FactQuery candidate project root is not canonical")

    manifest = _mapping(receipt.get("manifest"), label="FactQuery manifest receipt")
    manifest_path_value = manifest.get("path")
    if type(manifest_path_value) is not str or not manifest_path_value:
        raise _reject("FactQuery manifest receipt path is malformed")
    raw_manifest_path = Path(manifest_path_value)
    if raw_manifest_path.is_symlink():
        raise _reject("FactQuery manifest receipt must name a regular file")
    try:
        manifest_path = raw_manifest_path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _reject("FactQuery manifest receipt is unavailable") from exc
    if not manifest_path.is_file() or str(manifest_path) != manifest_path_value:
        raise _reject("FactQuery manifest receipt path is not canonical")
    manifest_size = _nonnegative_int(
        manifest.get("size_bytes"), label="FactQuery manifest size_bytes"
    )
    manifest_digest = _sha256(manifest.get("sha256"), label="FactQuery manifest sha256")
    try:
        manifest_bytes, _manifest_stat_identity = _read_stable_file(
            manifest_path,
            label="FactQuery manifest",
        )
        observed_manifest = {
            "size": len(manifest_bytes),
            "sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        }
    except (OSError, ValueError) as exc:
        raise _reject("FactQuery manifest changed after candidate admission") from exc
    if observed_manifest != {"size": manifest_size, "sha256": manifest_digest}:
        raise _reject("FactQuery manifest changed after candidate admission")
    try:
        embedded_manifest = RepoManifest.from_dict(
            json.loads(manifest_bytes.decode("utf-8"))
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise _reject("FactQuery embedded RepoManifest is malformed") from exc
    embedded_selection = repository_source_selection_for_manifest(embedded_manifest)
    selection_payload = (
        None if embedded_selection is None else embedded_selection.to_dict()
    )
    selection_digest = None if embedded_selection is None else embedded_selection.digest
    repository_filter_policy = repository_filter_policy_for_manifest(embedded_manifest)
    embedded_entry = embedded_manifest.indexes.get("symbol_graph")
    if embedded_entry is None or not embedded_manifest.index_is_current("symbol_graph"):
        raise _reject("FactQuery embedded symbol graph entry is not current")
    entry_selection_digest = (
        None if embedded_selection is None else embedded_entry.source_selection_digest
    )
    if (
        embedded_manifest.version == MANIFEST_VERSION
        and not is_secure_source_fingerprint_v2(embedded_manifest.source_fingerprint)
    ):
        raise _reject("FactQuery embedded RepoManifest requires source fingerprint v2")
    if (
        manifest.get("version") != embedded_manifest.version
        or manifest.get("source_fingerprint") != embedded_manifest.source_fingerprint
        or manifest.get("file_count") != embedded_manifest.file_count
        or manifest.get("source_selection") != selection_payload
        or manifest.get("source_selection_digest") != selection_digest
        or manifest.get("entry_commit") != embedded_entry.commit
        or manifest.get("entry_source_fingerprint") != embedded_entry.source_fingerprint
        or manifest.get("entry_source_selection_digest") != entry_selection_digest
    ):
        raise _reject("FactQuery manifest receipt differs from its embedded manifest")
    try:
        embedded_root = Path(embedded_manifest.repo_path).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _reject("FactQuery embedded repository root is unavailable") from exc
    if embedded_root != project_root:
        raise _reject("FactQuery embedded repository root differs from its receipt")

    commit = manifest.get("commit")
    if type(commit) is not str or (commit and _GIT_COMMIT.fullmatch(commit) is None):
        raise _reject("FactQuery manifest commit is malformed")
    checkout_head = _checkout_head(project_root)
    if checkout_head != commit:
        raise _reject("live repository commit changed after candidate admission")
    if commit != embedded_manifest.commit:
        raise _reject("FactQuery manifest commit differs from its embedded manifest")

    entry_path_value = manifest.get("entry_path")
    if type(entry_path_value) is not str or not entry_path_value:
        raise _reject("FactQuery manifest entry path is malformed")
    expected_entry_path = manifest_path.parent / "symbol_graph"
    if Path(entry_path_value) != expected_entry_path:
        raise _reject("FactQuery manifest entry path changed after admission")
    try:
        entry_path = Path(entry_path_value).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _reject("FactQuery symbol graph view is unavailable") from exc
    if not entry_path.is_dir() or entry_path != expected_entry_path:
        raise _reject("FactQuery symbol graph view is not canonical")
    if Path(embedded_entry.path) != entry_path:
        raise _reject("FactQuery symbol graph path differs from its embedded manifest")

    index = _mapping(receipt.get("index"), label="FactQuery decoded index receipt")
    index_relative = _canonical_relative_path(
        index.get("relative_path"), label="FactQuery decoded index relative_path"
    )
    if index_relative != "index.decoded":
        raise _reject("FactQuery decoded index must be index.decoded")
    index_size = _nonnegative_int(
        index.get("size_bytes"), label="FactQuery decoded index size_bytes"
    )
    index_digest = _sha256(index.get("sha256"), label="FactQuery decoded index sha256")
    graph = _mapping(receipt.get("graph"), label="FactQuery graph receipt")
    graph_relative = _canonical_relative_path(
        graph.get("relative_path"), label="FactQuery graph relative_path"
    )
    if graph_relative != "graph.pkl":
        raise _reject("FactQuery graph receipt must name graph.pkl")
    graph_size = _nonnegative_int(
        graph.get("size_bytes"), label="FactQuery graph size_bytes"
    )
    graph_digest = _sha256(graph.get("sha256"), label="FactQuery graph sha256")
    graph_query_digest = _sha256(
        graph.get("query_surface_sha256"),
        label="FactQuery graph query_surface_sha256",
    )

    index_path = entry_path / index_relative
    graph_path = entry_path / graph_relative
    try:
        observed_index = regular_file_fingerprint(index_path)
        observed_graph = regular_file_fingerprint(graph_path)
    except (OSError, ValueError) as exc:
        raise _reject("FactQuery artifact changed after candidate admission") from exc
    if observed_index != {"size": index_size, "sha256": index_digest}:
        raise _reject("FactQuery decoded index changed after candidate admission")
    if observed_graph != {"size": graph_size, "sha256": graph_digest}:
        raise _reject("FactQuery graph changed after candidate admission")

    native_input = _validate_input_receipt(
        receipt.get("native_input"),
        index_path=index_path,
        project_root=project_root,
        language=language,
    )
    if native_input["index"] != {
        "path": str(index_path),
        "size_bytes": index_size,
        "sha256": index_digest,
    }:
        raise _reject("FactQuery native input no longer matches the decoded index")

    source = _mapping(receipt.get("source"), label="FactQuery source receipt")
    source_fingerprint = source.get("fingerprint")
    if (
        type(source_fingerprint) is not str
        or _SOURCE_FINGERPRINT.fullmatch(source_fingerprint) is None
    ):
        raise _reject("FactQuery source fingerprint is malformed")
    source_file_count = _nonnegative_int(
        source.get("file_count"), label="FactQuery source file_count"
    )
    if (
        source.get("repository_filter_policy") != repository_filter_policy
        or type(source.get("repository_filter_policy")) is not int
        or source.get("source_selection") != selection_payload
        or source.get("source_selection_digest") != selection_digest
    ):
        raise _reject("FactQuery source selection receipt is inconsistent")
    filters = _mapping(receipt.get("filters"), label="FactQuery filter receipt")
    patterns = filters.get("exclude_patterns")
    if type(patterns) is not list or not all(type(item) is str for item in patterns):
        raise _reject("FactQuery exclude patterns are malformed")
    if (
        filters.get("repository_filter_policy") != repository_filter_policy
        or type(filters.get("repository_filter_policy")) is not int
        or filters.get("source_selection") != selection_payload
        or filters.get("source_selection_digest") != selection_digest
    ):
        raise _reject("FactQuery filter selection receipt is inconsistent")
    observed_source, allowed_files = _source_and_allowed_files(
        project_root,
        cache_root=manifest_path.parent,
        exclude_patterns=patterns,
        target_dir=None,
        manifest=embedded_manifest,
    )
    if (
        observed_source.value != source_fingerprint
        or observed_source.file_count != source_file_count
    ):
        raise _reject("FactQuery repository source changed after candidate admission")
    allowed_count = _nonnegative_int(
        filters.get("allowed_file_count"),
        label="FactQuery filter allowed_file_count",
    )
    allowed_digest = _sha256(
        filters.get("allowed_files_sha256"),
        label="FactQuery filter allowed_files_sha256",
    )
    if len(allowed_files) != allowed_count or (
        _allowed_files_digest(allowed_files) != allowed_digest
    ):
        raise _reject("FactQuery allowed file set changed after candidate admission")
    if manifest.get("source_fingerprint") != source_fingerprint or (
        manifest.get("file_count") != source_file_count
    ):
        raise _reject("FactQuery manifest/source receipt fields disagree")
    if manifest.get("query_surface_sha256") != graph_query_digest:
        raise _reject("FactQuery graph query surface receipt fields disagree")
    filter_identity = _mapping(
        receipt.get("filter_identity"),
        label="FactQuery filter identity receipt",
    )
    if (
        filter_identity.get("identity") is not True
        or filter_identity.get("repository_filter_policy") != repository_filter_policy
        or type(filter_identity.get("repository_filter_policy")) is not int
        or filter_identity.get("source_selection_digest") != selection_digest
        or filter_identity.get("allowed_file_count") != len(allowed_files)
        or filter_identity.get("allowed_files_sha256") != allowed_digest
    ):
        raise _reject("FactQuery filter identity selection proof is inconsistent")

    try:
        final_manifest = regular_file_fingerprint(manifest_path)
        final_index = regular_file_fingerprint(index_path)
        final_graph = regular_file_fingerprint(graph_path)
    except (OSError, ValueError) as exc:
        raise _reject("FactQuery inputs changed during snapshot observation") from exc
    final_source = fingerprint_repository_for_manifest(
        project_root,
        embedded_manifest,
        exclude_roots=(manifest_path.parent,),
    )
    final_checkout_head = _checkout_head(project_root)
    if (
        final_manifest != observed_manifest
        or final_index != observed_index
        or final_graph != observed_graph
        or final_source != observed_source
        or final_checkout_head != checkout_head
    ):
        raise _reject("FactQuery inputs changed during snapshot observation")

    return {
        "schema_version": FACT_QUERY_RECEIPT_SCHEMA_VERSION,
        "receipt_sha256": recorded_digest,
        "language": language,
        "project_root": str(project_root),
        "checkout_head": checkout_head,
        "manifest": {
            "path": str(manifest_path),
            "size_bytes": observed_manifest["size"],
            "sha256": observed_manifest["sha256"],
        },
        "index": {
            "path": str(index_path),
            "size_bytes": observed_index["size"],
            "sha256": observed_index["sha256"],
        },
        "graph": {
            "path": str(graph_path),
            "size_bytes": observed_graph["size"],
            "sha256": observed_graph["sha256"],
            "query_surface_sha256": graph_query_digest,
        },
        "source": {
            "fingerprint": observed_source.value,
            "file_count": observed_source.file_count,
            "allowed_file_count": len(allowed_files),
            "allowed_files_sha256": allowed_digest,
            "repository_filter_policy": repository_filter_policy,
            "source_selection": selection_payload,
            "source_selection_digest": selection_digest,
        },
        "native_metadata": copy.deepcopy(native_input["metadata"]),
    }


__all__ = [
    "FACT_QUERY_ABI_VERSION",
    "FACT_QUERY_CAPABILITIES",
    "FACT_QUERY_FILTER_PROOF_SCHEMA_VERSION",
    "FACT_QUERY_FORMAT",
    "FACT_QUERY_PROMOTED_LANGUAGES",
    "FACT_QUERY_RECEIPT_SCHEMA_VERSION",
    "SCIP_INPUT_RECEIPT_SCHEMA_VERSION",
    "FactQueryCandidate",
    "FactQueryCandidateRejected",
    "NativeFactQueryIndex",
    "decode_native_fact_query_index",
    "load_fact_query_candidate",
    "observe_fact_query_snapshot",
    "validate_fact_query_contract",
]
