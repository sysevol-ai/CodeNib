"""Build content-locked evaluation bundles from explicit local sources."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

from .artifact_integrity import (
    FileRecord,
    SelectionIdentity,
    create_archive,
    select_source,
    selection_identity,
    sha256_file,
    source_path,
    verify_bundle,
    write_checksums,
)
from .artifact_manifest import (
    LOCK_FILE,
    LOCK_SCHEMA_VERSION,
    MANIFEST_FILE,
    PROVENANCE_FILE,
    PROVENANCE_SCHEMA_VERSION,
    ArtifactBundleError,
    ArtifactIntegrityError,
    BundleManifest,
    SourceSpec,
    load_manifest,
    load_source_lock,
    parse_root_arguments,
)


def write_source_lock(
    manifest: BundleManifest,
    roots: Mapping[str, Path | str],
    output: Path | str,
) -> dict[str, Any]:
    """Hash every selected source and write a reviewable content lock."""

    resolved_roots = _resolve_roots(manifest, roots)
    resolved_sources = {
        spec.source_id: source_path(spec, resolved_roots[spec.root])
        for spec in manifest.sources
    }
    output_path = Path(output).expanduser().resolve(strict=False)
    _validate_output_location(output_path, resolved_sources)
    identities = {
        spec.source_id: selection_identity(
            select_source(spec, resolved_roots[spec.root])
        ).as_dict()
        for spec in manifest.sources
    }
    payload = {
        "schema_version": LOCK_SCHEMA_VERSION,
        "manifest_sha256": manifest.sha256,
        "sources": identities,
    }
    if output_path.exists():
        raise ArtifactBundleError(f"source lock already exists: {output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(output_path, payload)
    return payload


def build_bundle(
    manifest: BundleManifest,
    roots: Mapping[str, Path | str],
    output: Path | str,
    *,
    source_lock: Path | str | None = None,
) -> dict[str, Any]:
    """Atomically stage a bundle and verify its complete checksum inventory."""

    output_path = Path(output).expanduser().resolve(strict=False)
    if output_path.exists():
        raise ArtifactBundleError(f"bundle output already exists: {output_path}")
    resolved_roots = _resolve_roots(manifest, roots)
    resolved_sources = {
        spec.source_id: source_path(spec, resolved_roots[spec.root])
        for spec in manifest.sources
    }
    _validate_output_location(output_path, resolved_sources)

    lock_payload = (
        load_source_lock(source_lock, manifest) if source_lock is not None else None
    )
    lock_path = (
        Path(source_lock).expanduser().resolve(strict=True) if source_lock else None
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    staging = output_path.parent / f".{output_path.name}.tmp-{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        provenance_sources = []
        for spec in manifest.sources:
            records = select_source(spec, resolved_roots[spec.root])
            identity = selection_identity(records)
            if lock_payload is not None:
                _check_locked_identity(lock_payload, spec.source_id, identity)
            _copy_selection(
                spec,
                records,
                staging,
                source_is_file=resolved_sources[spec.source_id].is_file(),
            )
            provenance_sources.append(
                {
                    "id": spec.source_id,
                    "root": spec.root,
                    "path": spec.path.as_posix(),
                    "destination": spec.destination.as_posix(),
                    "identity": identity.as_dict(),
                }
            )

        (staging / MANIFEST_FILE).write_bytes(manifest.raw)
        lock_sha256 = None
        if lock_path is not None:
            lock_bytes = lock_path.read_bytes()
            (staging / LOCK_FILE).write_bytes(lock_bytes)
            lock_sha256 = hashlib.sha256(lock_bytes).hexdigest()
        provenance = {
            "schema_version": PROVENANCE_SCHEMA_VERSION,
            "bundle": {"name": manifest.name, "version": manifest.version},
            "manifest_sha256": manifest.sha256,
            "source_lock_sha256": lock_sha256,
            "roots": {
                name: _root_provenance(path)
                for name, path in sorted(resolved_roots.items())
            },
            "sources": provenance_sources,
        }
        _write_json(staging / PROVENANCE_FILE, provenance)
        write_checksums(staging)
        verification = verify_bundle(staging)
        os.replace(staging, output_path)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    return {
        "bundle": str(output_path),
        "files": verification["files"],
        "bytes": verification["bytes"],
        "manifest_sha256": manifest.sha256,
        "source_lock_sha256": lock_sha256,
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point for source locking, staging, and verification."""

    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    lock_parser = subparsers.add_parser("lock", help="hash selected source trees")
    _add_manifest_and_roots(lock_parser)
    lock_parser.add_argument("--output", type=Path, required=True)

    build_parser = subparsers.add_parser("build", help="stage an artifact bundle")
    _add_manifest_and_roots(build_parser)
    build_parser.add_argument("--source-lock", type=Path)
    build_parser.add_argument("--output", type=Path, required=True)
    build_parser.add_argument("--archive", type=Path)

    verify_parser = subparsers.add_parser("verify", help="verify a staged bundle")
    verify_parser.add_argument("--bundle", type=Path, required=True)

    archive_parser = subparsers.add_parser(
        "archive", help="archive an already verified bundle"
    )
    archive_parser.add_argument("--bundle", type=Path, required=True)
    archive_parser.add_argument("--output", type=Path, required=True)

    args = parser.parse_args(argv)
    if args.command == "verify":
        result = verify_bundle(args.bundle)
    elif args.command == "archive":
        result = create_archive(args.bundle, args.output)
    else:
        manifest = load_manifest(args.manifest)
        roots = parse_root_arguments(args.root)
        if args.command == "lock":
            result = write_source_lock(manifest, roots, args.output)
        else:
            result = build_bundle(
                manifest,
                roots,
                args.output,
                source_lock=args.source_lock,
            )
            if args.archive:
                result["archive"] = create_archive(args.output, args.archive)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _resolve_roots(
    manifest: BundleManifest, roots: Mapping[str, Path | str]
) -> dict[str, Path]:
    required = {source.root for source in manifest.sources}
    missing = sorted(required - set(roots))
    if missing:
        raise ArtifactBundleError(f"missing source roots: {missing}")
    resolved = {}
    for name in sorted(required):
        path = Path(roots[name]).expanduser().resolve(strict=True)
        if not path.is_dir():
            raise ArtifactBundleError(
                f"source root {name!r} is not a directory: {path}"
            )
        resolved[name] = path
    return resolved


def _validate_output_location(output: Path, sources: Mapping[str, Path]) -> None:
    resolved_output = output.resolve(strict=False)
    for source_id, source in sources.items():
        if source.is_dir() and _filesystem_contains(source, resolved_output):
            raise ArtifactBundleError(
                f"bundle output is inside selected source {source_id!r}"
            )
        if _filesystem_contains(resolved_output, source):
            raise ArtifactBundleError(
                f"selected source {source_id!r} is inside bundle output"
            )


def _copy_selection(
    spec: SourceSpec,
    records: Sequence[FileRecord],
    staging: Path,
    *,
    source_is_file: bool,
) -> None:
    for record in records:
        destination = staging / Path(*spec.destination.parts)
        if not source_is_file:
            destination = destination / Path(*record.relative_path.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(record.source, destination)
        if destination.stat().st_size != record.size:
            raise ArtifactIntegrityError(
                f"source changed while copying {spec.source_id!r}: "
                f"{record.relative_path}"
            )
        if sha256_file(destination) != record.sha256:
            raise ArtifactIntegrityError(
                f"source changed while copying {spec.source_id!r}: "
                f"{record.relative_path}"
            )


def _check_locked_identity(
    lock: Mapping[str, Any], source_id: str, identity: SelectionIdentity
) -> None:
    expected = lock["sources"][source_id]
    if not isinstance(expected, Mapping):
        raise ArtifactBundleError(f"invalid lock entry for source {source_id!r}")
    actual = identity.as_dict()
    if dict(expected) != actual:
        raise ArtifactIntegrityError(
            f"source {source_id!r} differs from lock; "
            f"expected={dict(expected)}, actual={actual}"
        )


def _root_provenance(root: Path) -> dict[str, Any]:
    try:
        top_level = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        commit = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        return {"git_commit": None}
    try:
        root.resolve().relative_to(Path(top_level).resolve())
    except ValueError:
        return {"git_commit": None}
    return {"git_commit": commit}


def _add_manifest_and_roots(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--root",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="bind a manifest root name to a local directory; repeat as needed",
    )


def _filesystem_contains(parent: Path, child: Path) -> bool:
    return parent == child or parent in child.parents


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    raise SystemExit(main())
