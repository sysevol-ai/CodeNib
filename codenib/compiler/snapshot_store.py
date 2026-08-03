# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Content-addressed repository snapshots and reusable analysis artifacts.

Benchmark instances are not artifact identities. Several instances, generated
queries, or agent runs may refer to the same repository commit. This module
stores that source snapshot once, stores analysis artifacts under an explicit
profile, and binds arbitrary instance IDs to the shared profile with symlinks.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

from ..git_snapshot import restore_git_worktree
from ..languages import normalize_language

SNAPSHOT_STORE_VERSION = "1"
_SNAPSHOT_DIR = ".snapshots"


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def normalize_repo(repo: str) -> str:
    """Normalize common GitHub URL and slug forms to ``owner/repo``."""
    value = repo.strip().rstrip("/")
    if value.startswith("git@") and ":" in value:
        value = value.split(":", 1)[1]
    elif "://" in value:
        value = urlparse(value).path.lstrip("/")
    if value.endswith(".git"):
        value = value[:-4]
    value = value.strip("/").lower()
    if not value or ".." in value.split("/"):
        raise ValueError(f"invalid repository identity: {repo!r}")
    return value


def _normalize_languages(languages: Sequence[str]) -> tuple[str, ...]:
    normalized = set()
    for language in languages:
        value = language.strip().lower()
        if value:
            normalized.add(normalize_language(value) or value)
    if not normalized:
        raise ValueError("an artifact profile requires at least one language")
    return tuple(sorted(normalized))


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    """Immutable source identity independent of benchmark instance IDs."""

    repo: str
    commit: str

    def payload(self) -> dict[str, Any]:
        commit = self.commit.strip().lower()
        if not commit:
            raise ValueError("snapshot commit must not be empty")
        return {
            "version": SNAPSHOT_STORE_VERSION,
            "repo": normalize_repo(self.repo),
            "commit": commit,
        }

    @property
    def snapshot_id(self) -> str:
        return _digest(self.payload())


@dataclass(frozen=True, slots=True)
class ArtifactProfile:
    """Analysis configuration that determines artifact compatibility."""

    languages: tuple[str, ...]
    name: str = "default"
    schema_versions: Mapping[str, str] = field(default_factory=dict)
    options: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        languages: Sequence[str],
        *,
        name: str = "default",
        schema_versions: Mapping[str, str] | None = None,
        options: Mapping[str, Any] | None = None,
    ) -> ArtifactProfile:
        return cls(
            languages=_normalize_languages(languages),
            name=name,
            schema_versions=dict(schema_versions or {}),
            options=dict(options or {}),
        )

    def payload(self) -> dict[str, Any]:
        if not self.name.strip():
            raise ValueError("artifact profile name must not be empty")
        return {
            "version": SNAPSHOT_STORE_VERSION,
            "name": self.name.strip(),
            "languages": list(_normalize_languages(self.languages)),
            "schema_versions": dict(self.schema_versions),
            "options": dict(self.options),
        }

    @property
    def profile_id(self) -> str:
        return _digest(self.payload())


@dataclass(frozen=True, slots=True)
class SnapshotBinding:
    """Resolved paths and cache state for one instance binding."""

    instance_id: str
    snapshot_id: str
    profile_id: str
    snapshot_dir: Path
    profile_dir: Path
    instance_path: Path
    snapshot_hit: bool
    profile_hit: bool
    alias_hit: bool


class SnapshotArtifactStore:
    """Bind benchmark instances to content-addressed snapshot artifacts."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.snapshots_dir = self.root / _SNAPSHOT_DIR

    def bind(
        self,
        instance_id: str,
        snapshot: SourceSnapshot,
        profile: ArtifactProfile,
    ) -> SnapshotBinding:
        snapshot_payload = snapshot.payload()
        profile_payload = profile.payload()
        snapshot_id = snapshot.snapshot_id
        profile_id = profile.profile_id
        snapshot_dir = self.snapshots_dir / snapshot_id
        profile_dir = snapshot_dir / "profiles" / profile_id
        instance_path = self.root / self._instance_name(instance_id)

        snapshot_hit = (snapshot_dir / "snapshot.json").is_file()
        profile_hit = (profile_dir / "profile.json").is_file()
        alias_hit = instance_path.is_symlink()

        snapshot_dir.mkdir(parents=True, exist_ok=True)
        self._write_identity(snapshot_dir / "snapshot.json", snapshot_payload)
        profile_dir.mkdir(parents=True, exist_ok=True)
        self._write_identity(profile_dir / "profile.json", profile_payload)
        self._ensure_repo_link(profile_dir, snapshot_dir)
        self._bind_alias(instance_path, profile_dir)

        return SnapshotBinding(
            instance_id=instance_id,
            snapshot_id=snapshot_id,
            profile_id=profile_id,
            snapshot_dir=snapshot_dir,
            profile_dir=profile_dir,
            instance_path=instance_path,
            snapshot_hit=snapshot_hit,
            profile_hit=profile_hit,
            alias_hit=alias_hit,
        )

    def ensure_worktree(
        self,
        binding: SnapshotBinding,
        *,
        source_repo: str | Path,
        commit: str,
    ) -> Path:
        """Create a durable detached worktree for the snapshot if absent."""
        target = binding.snapshot_dir / "repo"
        if target.exists():
            actual = self._git_output(target, "rev-parse", "HEAD")
            expected = self._git_output(
                source_repo, "rev-parse", f"{commit}^{{commit}}"
            )
            if actual != expected:
                raise ValueError(
                    f"snapshot worktree mismatch: expected {expected}, found {actual}"
                )
            restore_git_worktree(target, expected)
            return target

        expected = self._git_output(source_repo, "rev-parse", f"{commit}^{{commit}}")
        subprocess.run(
            [
                "git",
                "-c",
                f"safe.directory={Path(source_repo).expanduser().resolve()}",
                "-C",
                str(source_repo),
                "worktree",
                "add",
                "--detach",
                str(target),
                expected,
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        restore_git_worktree(target, expected)
        return target

    def resolve(self, instance_id: str) -> Path:
        path = self.root / self._instance_name(instance_id)
        if not path.exists():
            raise FileNotFoundError(f"no snapshot artifact binding for {instance_id}")
        return path.resolve()

    @staticmethod
    def _instance_name(instance_id: str) -> str:
        name = instance_id.replace("/", "__")
        if (
            not name
            or name in {".", ".."}
            or not re.fullmatch(r"[A-Za-z0-9_.-]+", name)
        ):
            raise ValueError(f"invalid instance id: {instance_id!r}")
        return name

    @staticmethod
    def _write_identity(path: Path, payload: Mapping[str, Any]) -> None:
        if path.exists():
            existing = json.loads(path.read_text(encoding="utf-8"))
            comparable = {k: existing.get(k) for k in payload}
            if comparable != dict(payload):
                raise ValueError(f"artifact identity mismatch at {path}")
            return
        body = dict(payload)
        body["created_at"] = datetime.now(timezone.utc).isoformat()
        path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")

    @staticmethod
    def _ensure_repo_link(profile_dir: Path, snapshot_dir: Path) -> None:
        link = profile_dir / "repo"
        target = snapshot_dir / "repo"
        if link.is_symlink():
            if link.resolve(strict=False) != target.resolve(strict=False):
                raise ValueError(
                    f"profile repo link points at the wrong snapshot: {link}"
                )
            return
        if link.exists():
            raise FileExistsError(f"profile repo path is not a symlink: {link}")
        link.symlink_to(
            os.path.relpath(target, start=profile_dir), target_is_directory=True
        )

    @staticmethod
    def _bind_alias(instance_path: Path, profile_dir: Path) -> None:
        if instance_path.is_symlink():
            if instance_path.resolve(strict=False) != profile_dir.resolve(strict=False):
                raise ValueError(
                    f"instance alias already targets another profile: {instance_path}"
                )
            return
        if instance_path.exists():
            raise FileExistsError(
                f"instance path already exists in legacy layout: {instance_path}"
            )
        instance_path.parent.mkdir(parents=True, exist_ok=True)
        instance_path.symlink_to(
            os.path.relpath(profile_dir, start=instance_path.parent),
            target_is_directory=True,
        )

    @staticmethod
    def _git_output(repo: str | Path, *args: str) -> str:
        resolved = Path(repo).expanduser().resolve()
        return subprocess.run(
            ["git", "-c", f"safe.directory={resolved}", "-C", str(repo), *args],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ).stdout.strip()


__all__ = [
    "ArtifactProfile",
    "SNAPSHOT_STORE_VERSION",
    "SnapshotArtifactStore",
    "SnapshotBinding",
    "SourceSnapshot",
    "normalize_repo",
]
