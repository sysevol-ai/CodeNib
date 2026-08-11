#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Profile clean publication versus unchanged FactBatch generation reuse."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from codenib.facts import (
    ClangdFactProfile,
    fact_batches_from_clangd_records,
    load_fact_batch_generation,
    publish_fact_batch_generation,
    sha256_digest,
)
from codenib.ls_index.clangd_decode import ClangdGraphDecoder
from codenib.storage import LocalCAS, SQLiteCatalog

REPORT_SCHEMA = "clangd_fact_batch_generation_profile_v1"


def _file_digest(path: Path) -> str:
    if not path.is_file():
        raise ValueError(f"required profile file is unavailable: {path}")
    return sha256_digest(path.read_bytes())


def _canonical_digest(value: str, field: str) -> str:
    if (
        not value.startswith("sha256:")
        or len(value) != 71
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise ValueError(f"{field} must use canonical sha256:<64 lowercase hex>")
    return value


def generation_reuse_gate(
    *,
    unit_count: int,
    first_published_units: int,
    reused_units: int,
    published_units: int,
    reachable_object_count: int,
    semantic_match: bool,
    graph_materialized: bool,
) -> dict:
    for value, field in (
        (unit_count, "unit_count"),
        (first_published_units, "first_published_units"),
        (reused_units, "reused_units"),
        (published_units, "published_units"),
        (reachable_object_count, "reachable_object_count"),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{field} must be a non-negative integer")
    failures = []
    if unit_count == 0:
        failures.append("no FactBatch units were produced")
    if first_published_units != unit_count:
        failures.append("clean publication did not write every unit")
    if not semantic_match:
        failures.append("semantic mismatch")
    if reused_units != unit_count:
        failures.append("not every unchanged unit was reused")
    if published_units != 0:
        failures.append("unchanged publication wrote new units")
    if graph_materialized:
        failures.append("legacy graph was materialized")
    if reachable_object_count != unit_count + 1:
        failures.append("catalog reachability does not cover manifest plus units")
    return {"passed": not failures, "failures": failures}


def profile_clangd_fact_batch_generation(
    *,
    idx_directory: Path,
    project_root: Path,
    clangd_executable: str,
    target_triple: str,
    compile_commands: Path,
    build_context_digest: str,
    position_encoding: str = "UTF16",
) -> dict:
    executable = shutil.which(clangd_executable)
    if executable is None:
        raise RuntimeError(f"clangd executable is unavailable: {clangd_executable}")
    executable_path = Path(executable).resolve()
    analyzer_version = subprocess.check_output(
        [str(executable_path), "--version"],
        text=True,
    ).splitlines()[0]
    profile = ClangdFactProfile.from_runtime(
        analyzer_version=analyzer_version,
        target_triple=target_triple,
        toolchain_digest=_file_digest(executable_path),
        compile_commands_digest=_file_digest(compile_commands),
        build_context_digest=_canonical_digest(
            build_context_digest, "build context digest"
        ),
        position_encoding=position_encoding,
    )
    decoder = ClangdGraphDecoder(
        str(idx_directory),
        str(project_root),
        position_encoding=position_encoding,
    )

    started = time.perf_counter()
    batches = fact_batches_from_clangd_records(decoder, profile=profile)
    adapter_seconds = time.perf_counter() - started

    with tempfile.TemporaryDirectory(prefix="codenib-fact-generation-profile-") as raw:
        temporary = Path(raw)
        store = LocalCAS(temporary / "objects")
        with SQLiteCatalog(temporary / "catalog.sqlite3") as catalog:
            repository_key = (
                "profile/"
                + hashlib.sha256(
                    str(project_root.resolve()).encode("utf-8")
                ).hexdigest()
            )
            repository_id = catalog.create_repository(repository_key)
            snapshot_id = str(decoder.query_snapshot_id)
            source_one = catalog.create_source_revision(
                repository_id,
                dirty=True,
                source_fingerprint=f"profile:first:{snapshot_id}",
            )
            started = time.perf_counter()
            first = publish_fact_batch_generation(
                catalog,
                store,
                repository_id=repository_id,
                source_revision_id=source_one,
                profile=profile,
                upserts=batches,
                replace_all=True,
            )
            first_publication_seconds = time.perf_counter() - started

            source_two = catalog.create_source_revision(
                repository_id,
                dirty=True,
                source_fingerprint=f"profile:unchanged:{snapshot_id}",
            )
            started = time.perf_counter()
            second = publish_fact_batch_generation(
                catalog,
                store,
                repository_id=repository_id,
                source_revision_id=source_two,
                profile=profile,
                upserts=(),
                expected_generation=first.ref_generation,
            )
            reuse_publication_seconds = time.perf_counter() - started
            loaded = load_fact_batch_generation(catalog, store, repository_id)
            manifest_bytes = store.verify(second.manifest_object_digest).byte_size

    clean_seconds = adapter_seconds + first_publication_seconds
    speedup = (
        (clean_seconds - reuse_publication_seconds) / clean_seconds
        if clean_seconds
        else 0.0
    )
    semantic_match = tuple(batch.digest() for batch in loaded.batches) == tuple(
        batch.digest() for batch in batches
    )
    gate = generation_reuse_gate(
        unit_count=len(batches),
        first_published_units=first.published_units,
        reused_units=second.reused_units,
        published_units=second.published_units,
        reachable_object_count=len(loaded.reachable_object_digests),
        semantic_match=semantic_match,
        graph_materialized=decoder._graph_materialized,
    )
    return {
        "schema": REPORT_SCHEMA,
        "complete_gate_passed": gate["passed"],
        "gate": gate,
        "subject": {
            "project_root": str(project_root.resolve()),
            "idx_directory": str(idx_directory.resolve()),
            "native_snapshot_id": decoder.query_snapshot_id,
            "analyzer_version": analyzer_version,
            "profile_digest": profile.digest,
        },
        "facts": {
            "unit_count": len(batches),
            "definition_count": sum(len(batch.symbols) for batch in batches),
            "occurrence_count": sum(len(batch.occurrences) for batch in batches),
            "edge_count": sum(len(batch.edges) for batch in batches),
            "unresolved_edge_count": sum(
                edge.target_moniker is not None
                for batch in batches
                for edge in batch.edges
            ),
            "manifest_bytes": manifest_bytes,
            "reachable_object_count": len(loaded.reachable_object_digests),
            "batch_set_digest": second.manifest.batch_set_digest,
        },
        "timing_seconds": {
            "adapter": adapter_seconds,
            "first_publication": first_publication_seconds,
            "clean_total": clean_seconds,
            "unchanged_reuse_publication": reuse_publication_seconds,
            "unchanged_reuse_speedup_fraction": speedup,
        },
        "reuse": {
            "first_published_units": first.published_units,
            "unchanged_reused_units": second.reused_units,
            "unchanged_published_units": second.published_units,
            "semantic_match": semantic_match,
            "graph_materialized": decoder._graph_materialized,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--idx-directory", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--clangd", default="clangd")
    parser.add_argument("--target-triple", required=True)
    parser.add_argument("--compile-commands", type=Path, required=True)
    parser.add_argument("--build-context-digest", required=True)
    parser.add_argument("--position-encoding", default="UTF16")
    parser.add_argument("--output-json", type=Path)
    arguments = parser.parse_args()

    report = profile_clangd_fact_batch_generation(
        idx_directory=arguments.idx_directory,
        project_root=arguments.project_root,
        clangd_executable=arguments.clangd,
        target_triple=arguments.target_triple,
        compile_commands=arguments.compile_commands,
        build_context_digest=arguments.build_context_digest,
        position_encoding=arguments.position_encoding,
    )
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if arguments.output_json is not None:
        arguments.output_json.parent.mkdir(parents=True, exist_ok=True)
        arguments.output_json.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0 if report["complete_gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
