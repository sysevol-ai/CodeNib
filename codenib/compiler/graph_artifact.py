# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Authenticated loading for persisted symbol-graph artifacts."""

from __future__ import annotations

import re
import tempfile
from contextlib import ExitStack
from pathlib import Path
from typing import Any, BinaryIO, Mapping

from .._atomic_directory import (
    PublicationDirectoryReader,
    capture_directory_ownership,
    directory_ownership_file_records,
    reopen_authenticated_directory,
)
from ..graph.code_graph import CodeGraph

_GRAPH_FILENAME = "graph.pkl"
_LSP_OCCURRENCE_FILENAME = "lsp_index.pkl"
_LSP_OCCURRENCE_RECEIPT_KEY = "lsp_occurrence_artifact"
_GRAPH_RECEIPT_KEYS = frozenset({"relative_path", "size", "sha256"})
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_MAX_GRAPH_ARTIFACT_BYTES = 8 << 30
_MAX_LSP_OCCURRENCE_ARTIFACT_BYTES = 8 << 30
_ARTIFACT_COPY_MEMORY_BYTES = 32 << 20


def _entry_graph_receipt(entry: Any) -> dict[str, Any]:
    config = getattr(entry, "config", None)
    if not isinstance(config, Mapping):
        raise ValueError("symbol graph manifest entry has no artifact contract")
    receipt = config.get("graph_artifact")
    if type(receipt) is not dict:
        raise ValueError("symbol graph manifest entry has no exact graph receipt")
    return receipt


def _entry_lsp_occurrence_receipt(entry: Any) -> dict[str, Any] | None:
    config = getattr(entry, "config", None)
    if not isinstance(config, Mapping):
        raise ValueError("symbol graph manifest entry has no artifact contract")
    if _LSP_OCCURRENCE_RECEIPT_KEY not in config:
        return None
    receipt = config[_LSP_OCCURRENCE_RECEIPT_KEY]
    if receipt is None:
        return None
    if type(receipt) is not dict:
        raise ValueError(
            "symbol graph manifest entry has no exact LSP occurrence receipt"
        )
    return receipt


def _entry_graph_root(entry: Any) -> Path:
    root_value = getattr(entry, "path", None)
    if type(root_value) is not str or not root_value:
        raise ValueError("symbol graph manifest entry has no artifact directory")
    declared = Path(root_value)
    if declared.name == _GRAPH_FILENAME:
        return declared.parent
    if declared.suffix == ".pkl":
        raise ValueError("symbol graph manifest entry names an unsupported pickle")
    return declared


def _graph_receipt(receipt: Mapping[str, Any]) -> tuple[int, str]:
    if type(receipt) is not dict or set(receipt) != _GRAPH_RECEIPT_KEYS:
        raise ValueError("symbol graph artifact has no exact graph receipt")
    if receipt.get("relative_path") != _GRAPH_FILENAME:
        raise ValueError("symbol graph receipt has an invalid relative path")
    size = receipt.get("size")
    digest = receipt.get("sha256")
    if (
        isinstance(size, bool)
        or not isinstance(size, int)
        or size < 0
        or size > _MAX_GRAPH_ARTIFACT_BYTES
    ):
        raise ValueError("symbol graph receipt has an invalid artifact size")
    if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
        raise ValueError("symbol graph receipt has an invalid SHA-256 digest")
    return size, digest


def _lsp_occurrence_receipt(receipt: Mapping[str, Any]) -> tuple[int, str]:
    if type(receipt) is not dict or set(receipt) != _GRAPH_RECEIPT_KEYS:
        raise ValueError("LSP occurrence artifact has no exact receipt")
    if receipt.get("relative_path") != _LSP_OCCURRENCE_FILENAME:
        raise ValueError("LSP occurrence receipt has an invalid relative path")
    size = receipt.get("size")
    digest = receipt.get("sha256")
    if (
        isinstance(size, bool)
        or not isinstance(size, int)
        or size < 0
        or size > _MAX_LSP_OCCURRENCE_ARTIFACT_BYTES
    ):
        raise ValueError("LSP occurrence receipt has an invalid artifact size")
    if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
        raise ValueError("LSP occurrence receipt has an invalid SHA-256 digest")
    return size, digest


def _require_receipted_record(
    records: Mapping[str, Any],
    filename: str,
    *,
    expected_size: int,
    expected_digest: str,
    label: str,
) -> None:
    observed = records.get(filename)
    if (
        observed is None
        or observed.size != expected_size
        or observed.sha256 != expected_digest
    ):
        raise ValueError(f"{label} differs from its manifest receipt")


def _copy_authenticated_artifact(
    reader: PublicationDirectoryReader,
    copied: BinaryIO,
    *,
    filename: str,
    expected_size: int,
    expected_digest: str,
    label: str,
) -> None:
    with reader.open_authenticated_file(
        filename,
        max_bytes=expected_size,
    ) as authenticated:
        for chunk in authenticated.iter_bytes():
            copied.write(chunk)
    if (
        authenticated.record.size != expected_size
        or authenticated.record.sha256 != expected_digest
    ):
        raise ValueError(f"{label} changed before authenticated loading")


def load_authenticated_graph_generation(
    root_value: str | Path,
    receipt: Mapping[str, Any],
    *,
    lsp_occurrence_receipt: Mapping[str, Any] | None = None,
) -> CodeGraph:
    """Load exactly one graph generation through its persisted receipts.

    The directory is captured without following links, its canonical file
    records are matched to the persisted receipts, and the same generation is
    copied through authenticated descriptors/HANDLEs.  Every receipted stream
    is copied and verified before either pickle is loaded.  An unreceipted
    occurrence sidecar is deliberately ignored.
    """

    expected_size, expected_digest = _graph_receipt(receipt)
    expected_occurrence = (
        None
        if lsp_occurrence_receipt is None
        else _lsp_occurrence_receipt(lsp_occurrence_receipt)
    )
    if not isinstance(root_value, (str, Path)) or not str(root_value):
        raise ValueError("symbol graph artifact has no directory")
    root = Path(root_value)
    ownership = capture_directory_ownership(
        root,
        required_root_file=_GRAPH_FILENAME,
    )
    records = {
        record.path: record for record in directory_ownership_file_records(ownership)
    }
    _require_receipted_record(
        records,
        _GRAPH_FILENAME,
        expected_size=expected_size,
        expected_digest=expected_digest,
        label="symbol graph artifact",
    )
    if expected_occurrence is not None:
        occurrence_size, occurrence_digest = expected_occurrence
        _require_receipted_record(
            records,
            _LSP_OCCURRENCE_FILENAME,
            expected_size=occurrence_size,
            expected_digest=occurrence_digest,
            label="LSP occurrence artifact",
        )

    def load(reader: PublicationDirectoryReader) -> CodeGraph:
        with ExitStack() as stack:
            graph_copy = stack.enter_context(
                tempfile.SpooledTemporaryFile(
                    max_size=_ARTIFACT_COPY_MEMORY_BYTES,
                    mode="w+b",
                )
            )
            occurrence_copy = None
            if expected_occurrence is not None:
                occurrence_copy = stack.enter_context(
                    tempfile.SpooledTemporaryFile(
                        max_size=_ARTIFACT_COPY_MEMORY_BYTES,
                        mode="w+b",
                    )
                )

            _copy_authenticated_artifact(
                reader,
                graph_copy,
                filename=_GRAPH_FILENAME,
                expected_size=expected_size,
                expected_digest=expected_digest,
                label="symbol graph artifact",
            )
            if occurrence_copy is not None and expected_occurrence is not None:
                occurrence_size, occurrence_digest = expected_occurrence
                _copy_authenticated_artifact(
                    reader,
                    occurrence_copy,
                    filename=_LSP_OCCURRENCE_FILENAME,
                    expected_size=occurrence_size,
                    expected_digest=occurrence_digest,
                    label="LSP occurrence artifact",
                )

            graph_copy.seek(0)
            graph = CodeGraph.load_graph_stream(
                graph_copy,
                input_label=str(root / _GRAPH_FILENAME),
            )
            if occurrence_copy is not None:
                from ..scip_interface.lsp_occurrence_index import SCIPOccurrenceIndex

                occurrence_copy.seek(0)
                graph.lsp_occurrence_index = SCIPOccurrenceIndex.load_stream(
                    occurrence_copy,
                    input_label=str(root / _LSP_OCCURRENCE_FILENAME),
                )
            return graph

    return reopen_authenticated_directory(root, ownership, load)


def load_authenticated_graph_artifact(entry: Any) -> CodeGraph:
    """Load exactly the graph generation named by one manifest entry."""

    return load_authenticated_graph_generation(
        _entry_graph_root(entry),
        _entry_graph_receipt(entry),
        lsp_occurrence_receipt=_entry_lsp_occurrence_receipt(entry),
    )


def authenticated_graph_artifact_matches(entry: Any) -> bool:
    """Return whether every receipted file still matches one graph generation."""

    try:
        root = _entry_graph_root(entry)
        graph_size, graph_digest = _graph_receipt(_entry_graph_receipt(entry))
        occurrence_receipt = _entry_lsp_occurrence_receipt(entry)
        expected_occurrence = (
            None
            if occurrence_receipt is None
            else _lsp_occurrence_receipt(occurrence_receipt)
        )
        ownership = capture_directory_ownership(
            root,
            required_root_file=_GRAPH_FILENAME,
        )
        records = {
            record.path: record
            for record in directory_ownership_file_records(ownership)
        }
        _require_receipted_record(
            records,
            _GRAPH_FILENAME,
            expected_size=graph_size,
            expected_digest=graph_digest,
            label="symbol graph artifact",
        )
        if expected_occurrence is not None:
            occurrence_size, occurrence_digest = expected_occurrence
            _require_receipted_record(
                records,
                _LSP_OCCURRENCE_FILENAME,
                expected_size=occurrence_size,
                expected_digest=occurrence_digest,
                label="LSP occurrence artifact",
            )
    except (OSError, RuntimeError, TypeError, ValueError):
        return False
    return True


__all__ = [
    "authenticated_graph_artifact_matches",
    "load_authenticated_graph_artifact",
    "load_authenticated_graph_generation",
]
