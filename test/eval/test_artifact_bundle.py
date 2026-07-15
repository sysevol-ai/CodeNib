from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest

from codeminer.eval.artifact_bundle import (
    ArtifactBundleError,
    ArtifactIntegrityError,
    build_bundle,
    create_archive,
    load_manifest,
    verify_bundle,
    write_source_lock,
)


def _write_manifest(path: Path, sources: list[dict[str, object]]) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "bundle": {"name": "test bundle", "version": "v1"},
                "sources": sources,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _source(
    source_id: str,
    path: str,
    destination: str,
    **extra: object,
) -> dict[str, object]:
    return {
        "id": source_id,
        "root": "data",
        "path": path,
        "destination": destination,
        **extra,
    }


def test_build_bundle_applies_selection_and_verifies_complete_inventory(
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    selected = data / "results"
    selected.mkdir(parents=True)
    (selected / "keep.json").write_text('{"ok": true}\n', encoding="utf-8")
    (selected / "skip.log").write_text("skip\n", encoding="utf-8")
    cache = selected / "cache"
    cache.mkdir()
    (cache / "large.bin").write_bytes(b"not bundled")
    readme = data / "README.md"
    readme.write_text("artifact\n", encoding="utf-8")

    manifest = load_manifest(
        _write_manifest(
            tmp_path / "manifest.json",
            [
                _source(
                    "results",
                    "results",
                    "inputs/results",
                    include=["*.json"],
                    exclude_dir_names=["cache"],
                ),
                _source("readme", "README.md", "README.md"),
            ],
        )
    )
    lock_path = tmp_path / "source-lock.json"
    lock = write_source_lock(manifest, {"data": data}, lock_path)
    output = tmp_path / "bundle"

    result = build_bundle(manifest, {"data": data}, output, source_lock=lock_path)

    assert lock["sources"]["results"]["files"] == 1
    assert (output / "inputs/results/keep.json").is_file()
    assert not (output / "inputs/results/skip.log").exists()
    assert not (output / "inputs/results/cache").exists()
    assert (output / "README.md").read_text(encoding="utf-8") == "artifact\n"
    assert result["files"] == verify_bundle(output)["files"]
    provenance = json.loads((output / "PROVENANCE.json").read_text())
    commit = provenance["roots"]["data"].get("git_commit")
    assert commit is None or "/" not in commit
    assert "tmp" not in json.dumps(provenance)


def test_source_lock_rejects_changed_content(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    source = data / "result.json"
    source.write_text('{"value": 1}\n', encoding="utf-8")
    manifest = load_manifest(
        _write_manifest(
            tmp_path / "manifest.json",
            [_source("result", "result.json", "inputs/result.json")],
        )
    )
    lock_path = tmp_path / "source-lock.json"
    write_source_lock(manifest, {"data": data}, lock_path)
    source.write_text('{"value": 2}\n', encoding="utf-8")

    with pytest.raises(ArtifactIntegrityError, match="differs from lock"):
        build_bundle(
            manifest,
            {"data": data},
            tmp_path / "bundle",
            source_lock=lock_path,
        )


def test_verify_bundle_rejects_mutated_and_unlisted_files(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "result.json").write_text("{}\n", encoding="utf-8")
    manifest = load_manifest(
        _write_manifest(
            tmp_path / "manifest.json",
            [_source("result", "result.json", "result.json")],
        )
    )
    output = tmp_path / "bundle"
    build_bundle(manifest, {"data": data}, output)
    (output / "result.json").write_text('{"changed": true}\n', encoding="utf-8")
    with pytest.raises(ArtifactIntegrityError, match="checksum mismatch"):
        verify_bundle(output)

    (output / "result.json").write_text("{}\n", encoding="utf-8")
    (output / "extra.txt").write_text("extra\n", encoding="utf-8")
    with pytest.raises(ArtifactIntegrityError, match="unlisted"):
        verify_bundle(output)


def test_manifest_rejects_unsafe_and_overlapping_destinations(tmp_path: Path) -> None:
    unsafe = _write_manifest(
        tmp_path / "unsafe.json",
        [_source("unsafe", "../outside", "input")],
    )
    with pytest.raises(ArtifactBundleError, match="stay relative"):
        load_manifest(unsafe)

    overlapping = _write_manifest(
        tmp_path / "overlap.json",
        [
            _source("parent", "a", "inputs"),
            _source("child", "b", "inputs/child"),
        ],
    )
    with pytest.raises(ArtifactBundleError, match="overlapping"):
        load_manifest(overlapping)


def test_build_rejects_symbolic_links_and_existing_output(tmp_path: Path) -> None:
    data = tmp_path / "data"
    tree = data / "tree"
    tree.mkdir(parents=True)
    target = data / "target.txt"
    target.write_text("target\n", encoding="utf-8")
    (tree / "link.txt").symlink_to(target)
    manifest = load_manifest(
        _write_manifest(
            tmp_path / "manifest.json",
            [_source("tree", "tree", "tree")],
        )
    )
    with pytest.raises(ArtifactBundleError, match="symbolic link"):
        build_bundle(manifest, {"data": data}, tmp_path / "bundle")

    output = tmp_path / "existing"
    output.mkdir()
    with pytest.raises(ArtifactBundleError, match="already exists"):
        build_bundle(manifest, {"data": data}, output)


def test_build_rejects_top_level_source_symlink(tmp_path: Path) -> None:
    data = tmp_path / "data"
    real = data / "real"
    real.mkdir(parents=True)
    (real / "result.json").write_text("{}\n", encoding="utf-8")
    (data / "linked").symlink_to(real, target_is_directory=True)
    manifest = load_manifest(
        _write_manifest(
            tmp_path / "manifest.json",
            [_source("linked", "linked", "inputs/linked")],
        )
    )

    with pytest.raises(ArtifactBundleError, match="traverses symbolic link"):
        build_bundle(manifest, {"data": data}, tmp_path / "bundle")


def test_archive_is_deterministic_and_contains_one_bundle_root(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "result.json").write_text("{}\n", encoding="utf-8")
    manifest = load_manifest(
        _write_manifest(
            tmp_path / "manifest.json",
            [_source("result", "result.json", "result.json")],
        )
    )
    output = tmp_path / "bundle"
    build_bundle(manifest, {"data": data}, output)
    first = create_archive(output, tmp_path / "first.tar.gz")
    second = create_archive(output, tmp_path / "second.tar.gz")

    assert first["sha256"] == second["sha256"]
    with tarfile.open(tmp_path / "first.tar.gz", "r:gz") as archive:
        names = archive.getnames()
    assert names[0] == "bundle"
    assert "bundle/result.json" in names
