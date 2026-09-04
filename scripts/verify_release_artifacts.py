#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Validate CodeNib release filenames, metadata, and packaged runtime assets."""

from __future__ import annotations

import argparse
import email
import platform
import sys
import tarfile
import zipfile
from pathlib import Path
from typing import Sequence


class ReleaseValidationError(RuntimeError):
    """A release artifact does not satisfy the publication contract."""


README_CITATION_MARKERS = (
    "## Citation",
    "https://arxiv.org/abs/2607.25431",
    "@misc{yu2026codenibmultiviewdataserving,",
)
MCP_OWNERSHIP_MARKER = "mcp-name: ai.codenib/codenib"
WORKSPACE_OWNER_EXTENSION = "codenib/_workspace_owner_impl.abi3.so"
PRODUCTION_WHEEL_TAGS = (
    "cp310-abi3-manylinux_2_28_x86_64",
    "cp310-abi3-manylinux_2_28_aarch64",
)
_MACHINE_ARCHITECTURES = {
    "amd64": "x86_64",
    "arm64": "aarch64",
    "aarch64": "aarch64",
    "x86_64": "x86_64",
}
_COMPILED_BINARY_SUFFIXES = (
    ".a",
    ".dll",
    ".dylib",
    ".exe",
    ".lib",
    ".o",
    ".obj",
    ".pyd",
    ".so",
)
_RETIRED_RUNTIME_PREFIXES = (
    "codenib/index/persistence/",
    "codenib/storage/",
)
_RETIRED_RUNTIME_MEMBERS = frozenset(
    {
        "codenib/compiler/cache_import.py",
        "codenib/compiler/manifest_export.py",
        "codenib/compiler/manifest_import.py",
        "codenib/compiler/manifest_materialization.py",
        "codenib/compiler/manifest_storage.py",
        "codenib/compiler/retained_manifest_contract.py",
        "codenib/compiler/retained_snapshot.py",
        "codenib/facts/clangd.py",
        "codenib/facts/generation.py",
        "codenib/facts/storage.py",
    }
)


def validate_readme_citation(readme: str) -> None:
    """Require the canonical paper citation in the packaged README."""
    missing = [marker for marker in README_CITATION_MARKERS if marker not in readme]
    if missing:
        raise ReleaseValidationError(
            "README.md is missing citation markers: " + ", ".join(missing)
        )


def validate_readme_mcp_ownership(readme: str) -> None:
    """Require the official MCP Registry ownership marker in package metadata."""
    if MCP_OWNERSHIP_MARKER not in readme:
        raise ReleaseValidationError(
            f"README.md is missing MCP ownership marker: {MCP_OWNERSHIP_MARKER}"
        )


def project_identity(project_file: Path) -> tuple[str, str]:
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
        import tomli as tomllib

    with project_file.open("rb") as handle:
        project = tomllib.load(handle)["project"]
    return str(project["name"]), str(project["version"])


def expected_tag(version: str) -> str:
    return f"v{version}"


def validate_tag(tag: str | None, version: str) -> None:
    if tag is not None and tag != expected_tag(version):
        raise ReleaseValidationError(
            f"release tag {tag!r} does not match {expected_tag(version)!r}"
        )


def validate_release_notes(project_file: Path, notes_dir: Path | None = None) -> Path:
    """Require curated, version-matched notes before any release publication."""
    name, version = project_identity(project_file)
    root = project_file.resolve().parent
    directory = notes_dir if notes_dir is not None else root / "docs" / "releases"
    notes = directory / f"{version}.md"
    try:
        text = notes.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ReleaseValidationError(
            f"missing curated release notes for {name} {version}: {notes}"
        ) from exc

    heading = f"# CodeNib {version}"
    if heading not in text:
        raise ReleaseValidationError(
            f"{notes} must contain the version heading {heading!r}"
        )
    if f"=={version}" not in text:
        raise ReleaseValidationError(
            f"{notes} must contain an exact {version} installation example"
        )
    forbidden = ("test-files.pythonhosted.org", "--extra-index-url", "--index-url")
    found = [marker for marker in forbidden if marker in text]
    if found:
        raise ReleaseValidationError(
            f"{notes} contains prerelease installation markers: {', '.join(found)}"
        )
    return notes


def _single(paths: list[Path], kind: str) -> Path:
    if len(paths) != 1:
        found = ", ".join(path.name for path in paths) or "none"
        raise ReleaseValidationError(
            f"expected exactly one {kind} artifact, found {len(paths)}: {found}"
        )
    return paths[0]


def _wheel_name(name: str, version: str, tag: str) -> str:
    normalized = name.replace("-", "_")
    return f"{normalized}-{version}-{tag}.whl"


def _is_compiled_binary(member: str) -> bool:
    filename = Path(member).name.lower()
    return filename.endswith(_COMPILED_BINARY_SUFFIXES) or ".so." in filename


def _retired_runtime_members(
    members: set[str],
    *,
    archive_root: str | None = None,
) -> list[str]:
    prefix = f"{archive_root}/" if archive_root else ""
    retired = []
    for member in members:
        if prefix and not member.startswith(prefix):
            continue
        relative = member[len(prefix) :]
        if relative in _RETIRED_RUNTIME_MEMBERS or any(
            relative.startswith(retired_prefix)
            for retired_prefix in _RETIRED_RUNTIME_PREFIXES
        ):
            retired.append(member)
    return sorted(retired)


def select_compatible_wheel(
    dist_dir: Path,
    *,
    system: str | None = None,
    machine: str | None = None,
) -> Path:
    """Select the one production wheel compatible with this Linux machine."""
    current_system = sys.platform if system is None else system
    current_machine = platform.machine() if machine is None else machine
    if not current_system.startswith("linux"):
        raise ReleaseValidationError(
            f"production wheels require Linux, found {current_system!r}"
        )
    architecture = _MACHINE_ARCHITECTURES.get(current_machine.lower())
    if architecture is None:
        raise ReleaseValidationError(
            f"no production wheel supports machine {current_machine!r}"
        )
    suffix = f"-cp310-abi3-manylinux_2_28_{architecture}.whl"
    return _single(
        sorted(path for path in dist_dir.glob("*.whl") if path.name.endswith(suffix)),
        f"compatible {architecture} wheel",
    )


def _validate_wheel(wheel: Path, name: str, version: str) -> None:
    required = {
        "codenib/__init__.py",
        "codenib/__main__.py",
        "codenib/agent/tools/sandbox.py",
        "codenib/cli.py",
        "codenib/sandbox/__init__.py",
        "codenib/sandbox/docker.py",
        "codenib/sandbox/protocol.py",
        "codenib/sandbox/types.py",
        "codenib/storage.py",
        "codenib/toolchains.py",
        "codenib/web/frontend/index.html",
        "codenib/web/frontend/codenib-icon.svg",
        "codenib/web/frontend/runtime-config.js",
    }
    with zipfile.ZipFile(wheel) as archive:
        member_names = archive.namelist()
        members = set(member_names)
        missing = sorted(required - members)
        if missing:
            raise ReleaseValidationError(
                f"{wheel.name} is missing packaged runtime files: {', '.join(missing)}"
            )
        retired = _retired_runtime_members(members)
        if retired:
            raise ReleaseValidationError(
                f"{wheel.name} contains retired storage runtime files: "
                + ", ".join(retired)
            )
        if not any(
            member.startswith("codenib/web/frontend/assets/") and member.endswith(".js")
            for member in members
        ):
            raise ReleaseValidationError(
                f"{wheel.name} has no compiled Wiki JavaScript assets"
            )

        workspace_owner_members = [
            member
            for member in member_names
            if member.startswith("codenib/_workspace_owner_impl")
        ]
        if workspace_owner_members != [WORKSPACE_OWNER_EXTENSION]:
            found = ", ".join(workspace_owner_members) or "none"
            raise ReleaseValidationError(
                f"{wheel.name} must contain exactly one "
                f"{WORKSPACE_OWNER_EXTENSION}, found: {found}"
            )

        metadata_members = sorted(
            member for member in members if member.endswith(".dist-info/METADATA")
        )
        metadata_path = _single(
            [Path(member) for member in metadata_members],
            "wheel METADATA",
        ).as_posix()
        message = email.message_from_bytes(archive.read(metadata_path))
        if message["Name"] != name or message["Version"] != version:
            raise ReleaseValidationError(
                f"{wheel.name} metadata identifies "
                f"{message['Name']} {message['Version']}, expected {name} {version}"
            )
        if message["License-Expression"] != "Apache-2.0":
            raise ReleaseValidationError(
                f"{wheel.name} has unexpected license expression "
                f"{message['License-Expression']!r}"
            )
        validate_readme_mcp_ownership(message.get_payload())

        entry_members = sorted(
            member
            for member in members
            if member.endswith(".dist-info/entry_points.txt")
        )
        entry_path = _single(
            [Path(member) for member in entry_members],
            "wheel entry-points",
        ).as_posix()
        entries = archive.read(entry_path).decode("utf-8")
        if "codenib = codenib.cli:main" not in entries:
            raise ReleaseValidationError(
                f"{wheel.name} does not expose the unified codenib command"
            )


def _validate_sdist(sdist: Path, name: str, version: str) -> None:
    root = f"{name}-{version}"
    required = {
        f"{root}/CHANGELOG.md",
        f"{root}/LICENSE",
        f"{root}/README.md",
        f"{root}/codenib/storage.py",
        f"{root}/native/workspace_owner.c",
        f"{root}/server.json",
        f"{root}/pyproject.toml",
        f"{root}/web/package-lock.json",
    }
    with tarfile.open(sdist, mode="r:gz") as archive:
        archive_members = archive.getmembers()
        members = {member.name for member in archive_members}
        missing = sorted(required - members)
        if missing:
            raise ReleaseValidationError(
                f"{sdist.name} is missing source-release files: {', '.join(missing)}"
            )
        retired = _retired_runtime_members(members, archive_root=root)
        if retired:
            raise ReleaseValidationError(
                f"{sdist.name} contains retired storage runtime files: "
                + ", ".join(retired)
            )
        binary_members = sorted(
            member.name
            for member in archive_members
            if member.isfile() and _is_compiled_binary(member.name)
        )
        if binary_members:
            raise ReleaseValidationError(
                f"{sdist.name} contains compiled binaries: " + ", ".join(binary_members)
            )
        readme_file = archive.extractfile(f"{root}/README.md")
        if readme_file is None:  # guarded by the required-members check
            raise ReleaseValidationError(f"{sdist.name} has no readable README.md")
        readme = readme_file.read().decode("utf-8")
        validate_readme_citation(readme)
        validate_readme_mcp_ownership(readme)


def validate_release(
    dist_dir: Path,
    *,
    project_file: Path,
    tag: str | None = None,
) -> tuple[tuple[Path, ...], Path]:
    name, version = project_identity(project_file)
    validate_tag(tag, version)
    validate_release_notes(project_file)

    wheels = tuple(sorted(dist_dir.glob("*.whl")))
    sdist = _single(sorted(dist_dir.glob("*.tar.gz")), "sdist")
    expected_wheel_names = {
        _wheel_name(name, version, tag) for tag in PRODUCTION_WHEEL_TAGS
    }
    actual_wheel_names = {wheel.name for wheel in wheels}
    sdist_name = f"{name}-{version}.tar.gz"
    if actual_wheel_names != expected_wheel_names:
        missing = sorted(expected_wheel_names - actual_wheel_names)
        unexpected = sorted(actual_wheel_names - expected_wheel_names)
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unexpected:
            details.append("unexpected " + ", ".join(unexpected))
        raise ReleaseValidationError(
            "production wheels must be exactly the cp310-abi3 manylinux_2_28 "
            "x86_64 and aarch64 artifacts: " + "; ".join(details)
        )
    if sdist.name != sdist_name:
        raise ReleaseValidationError(
            f"sdist filename {sdist.name!r} must equal {sdist_name!r}"
        )

    for wheel in wheels:
        _validate_wheel(wheel, name, version)
    _validate_sdist(sdist, name, version)
    return wheels, sdist


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dist_dir", type=Path)
    parser.add_argument(
        "--project-file",
        type=Path,
        default=Path("pyproject.toml"),
    )
    parser.add_argument("--tag")
    parser.add_argument(
        "--print-compatible-wheel",
        action="store_true",
        help="print the current Linux architecture's production wheel",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        if args.print_compatible_wheel:
            print(select_compatible_wheel(args.dist_dir))
            return 0
        wheels, sdist = validate_release(
            args.dist_dir, project_file=args.project_file, tag=args.tag
        )
    except (
        OSError,
        ReleaseValidationError,
        tarfile.TarError,
        zipfile.BadZipFile,
    ) as exc:
        print(f"release validation failed: {exc}", file=sys.stderr)
        return 1
    artifacts = ", ".join([*(wheel.name for wheel in wheels), sdist.name])
    print(f"Release artifacts verified: {artifacts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
