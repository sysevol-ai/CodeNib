#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Validate CodeNib release filenames, metadata, and packaged runtime assets."""

from __future__ import annotations

import argparse
import email
import sys
import tarfile
import zipfile
from pathlib import Path
from typing import Sequence

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib


class ReleaseValidationError(RuntimeError):
    """A release artifact does not satisfy the publication contract."""


README_CITATION_MARKERS = (
    "## Citation",
    "https://arxiv.org/abs/2607.25431",
    "@misc{yu2026codenibmultiviewdataserving,",
)


def validate_readme_citation(readme: str) -> None:
    """Require the canonical paper citation in the packaged README."""
    missing = [marker for marker in README_CITATION_MARKERS if marker not in readme]
    if missing:
        raise ReleaseValidationError(
            "README.md is missing citation markers: " + ", ".join(missing)
        )


def project_identity(project_file: Path) -> tuple[str, str]:
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


def _single(paths: list[Path], kind: str) -> Path:
    if len(paths) != 1:
        found = ", ".join(path.name for path in paths) or "none"
        raise ReleaseValidationError(
            f"expected exactly one {kind} artifact, found {len(paths)}: {found}"
        )
    return paths[0]


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
        "codenib/toolchains.py",
        "codenib/web/frontend/index.html",
        "codenib/web/frontend/codenib-icon.svg",
        "codenib/web/frontend/runtime-config.js",
    }
    with zipfile.ZipFile(wheel) as archive:
        members = set(archive.namelist())
        missing = sorted(required - members)
        if missing:
            raise ReleaseValidationError(
                f"{wheel.name} is missing packaged runtime files: {', '.join(missing)}"
            )
        if not any(
            member.startswith("codenib/web/frontend/assets/") and member.endswith(".js")
            for member in members
        ):
            raise ReleaseValidationError(
                f"{wheel.name} has no compiled Wiki JavaScript assets"
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
        f"{root}/pyproject.toml",
        f"{root}/web/package-lock.json",
    }
    with tarfile.open(sdist, mode="r:gz") as archive:
        members = {member.name for member in archive.getmembers()}
        missing = sorted(required - members)
        if missing:
            raise ReleaseValidationError(
                f"{sdist.name} is missing source-release files: {', '.join(missing)}"
            )
        readme_file = archive.extractfile(f"{root}/README.md")
        if readme_file is None:  # guarded by the required-members check
            raise ReleaseValidationError(f"{sdist.name} has no readable README.md")
        validate_readme_citation(readme_file.read().decode("utf-8"))


def validate_release(
    dist_dir: Path,
    *,
    project_file: Path,
    tag: str | None = None,
) -> tuple[Path, Path]:
    name, version = project_identity(project_file)
    validate_tag(tag, version)

    wheel = _single(sorted(dist_dir.glob("*.whl")), "wheel")
    sdist = _single(sorted(dist_dir.glob("*.tar.gz")), "sdist")
    normalized = name.replace("-", "_")
    wheel_prefix = f"{normalized}-{version}-"
    sdist_name = f"{name}-{version}.tar.gz"
    if not wheel.name.startswith(wheel_prefix):
        raise ReleaseValidationError(
            f"wheel filename {wheel.name!r} must start with {wheel_prefix!r}"
        )
    if sdist.name != sdist_name:
        raise ReleaseValidationError(
            f"sdist filename {sdist.name!r} must equal {sdist_name!r}"
        )

    _validate_wheel(wheel, name, version)
    _validate_sdist(sdist, name, version)
    return wheel, sdist


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dist_dir", type=Path)
    parser.add_argument(
        "--project-file",
        type=Path,
        default=Path("pyproject.toml"),
    )
    parser.add_argument("--tag")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        wheel, sdist = validate_release(
            args.dist_dir,
            project_file=args.project_file,
            tag=args.tag,
        )
    except (
        OSError,
        ReleaseValidationError,
        tarfile.TarError,
        zipfile.BadZipFile,
    ) as exc:
        print(f"release validation failed: {exc}", file=sys.stderr)
        return 1
    print(f"Release artifacts verified: {wheel.name}, {sdist.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
