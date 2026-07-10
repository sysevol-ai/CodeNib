# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Pinned CodeMiner Base manifest for the native LSP agent ablation."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from codeminer.compiler.snapshot_store import ArtifactProfile, SourceSnapshot

from .lsp_agent_study import (
    DEFAULT_BASE_DATASET,
    DEFAULT_BASE_REVISION,
    LANGUAGE_GROUPS,
    LSPAgentStudySpec,
    LSPAgentStudyTask,
)


def build_base_lsp_agent_manifest(
    rows: Iterable[Mapping[str, Any]],
    *,
    spec: LSPAgentStudySpec,
    dataset_fingerprint: Optional[str] = None,
    prebuilt_root: str | Path | None = None,
) -> dict[str, Any]:
    """Build a deterministic, leakage-aware manifest from CodeMiner Base."""

    root = Path(prebuilt_root).expanduser().resolve() if prebuilt_root else None
    development = set(spec.development_repositories)
    subjects = []
    seen_instances: set[str] = set()
    for row in sorted(rows, key=lambda item: str(item.get("instance_id") or "")):
        language_group = str(row.get("language_group") or "")
        if language_group not in spec.language_groups:
            continue
        required = ("instance_id", "repo", "base_commit", "problem_statement")
        missing = [key for key in required if not str(row.get(key) or "").strip()]
        if missing:
            raise ValueError(f"base row is missing {missing}: {row.get('instance_id')}")
        instance_id = str(row["instance_id"])
        if instance_id in seen_instances:
            raise ValueError(f"duplicate instance_id in dataset: {instance_id}")
        seen_instances.add(instance_id)
        repo = str(row["repo"])
        commit = str(row["base_commit"])
        subject = {
            "base_commit": commit,
            "instance_id": instance_id,
            "language": LANGUAGE_GROUPS[language_group],
            "language_group": language_group,
            "query_sha256": _sha256_text(str(row["problem_statement"])),
            "repo": repo,
            "role": "development" if repo in development else "confirmatory",
            "snapshot_id": SourceSnapshot(repo, commit).snapshot_id,
        }
        if root is not None:
            subject["artifact"] = _artifact_status(root, instance_id, repo, commit)
        subjects.append(subject)

    if not subjects:
        raise ValueError("no supported CodeMiner Base rows selected")
    present_repos = {subject["repo"] for subject in subjects}
    missing_development = development - present_repos
    if missing_development:
        raise ValueError(
            "development repositories missing from dataset: "
            f"{sorted(missing_development)}"
        )

    payload = {
        "schema_version": 1,
        "benchmark": "codeminer_base_lsp_agent_ablation",
        "dataset": {
            "name": spec.dataset,
            "revision": spec.dataset_revision,
            "split": spec.split,
            "fingerprint": dataset_fingerprint,
        },
        "protocol": spec.to_dict(),
        "subjects": subjects,
        "summary": _manifest_summary(subjects, spec=spec),
    }
    payload["manifest_sha256"] = _sha256_json(payload)
    return payload


def task_from_manifest_subject(
    row: Mapping[str, Any],
    subject: Mapping[str, Any],
    *,
    repo_path: str | Path,
) -> LSPAgentStudyTask:
    """Bind one pinned manifest subject to its dataset row and source tree."""

    for key in ("instance_id", "repo", "base_commit"):
        if str(row.get(key) or "") != str(subject.get(key) or ""):
            raise ValueError(f"manifest/dataset {key} mismatch")
    query = str(row.get("problem_statement") or "")
    if _sha256_text(query) != str(subject.get("query_sha256") or ""):
        raise ValueError("manifest/dataset query mismatch")
    expected_snapshot = SourceSnapshot(
        str(row["repo"]), str(row["base_commit"])
    ).snapshot_id
    if expected_snapshot != str(subject.get("snapshot_id") or ""):
        raise ValueError("manifest snapshot identity mismatch")

    declared_repo = Path(repo_path).expanduser().absolute()
    root = declared_repo.resolve()
    actual_commit = _git_commit(root)
    if actual_commit != str(row["base_commit"]):
        raise ValueError(
            f"source snapshot mismatch: expected {row['base_commit']}, "
            f"found {actual_commit or 'unavailable'}"
        )
    artifact = subject.get("artifact") or {}
    if artifact.get("status") != "ready":
        raise ValueError(
            "manifest artifact is not snapshot-verified: "
            f"{artifact.get('status') or 'missing'}"
        )
    graph_sha256 = artifact.get("graph_sha256")
    if graph_sha256:
        current_graph_hash = _sha256_file(declared_repo.parent / "graph.pkl")
        if current_graph_hash != graph_sha256:
            raise ValueError("graph artifact changed after manifest creation")
    return LSPAgentStudyTask.from_base_row(
        row,
        repo_path=root,
        role=str(subject.get("role") or "unspecified"),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default=DEFAULT_BASE_DATASET)
    parser.add_argument("--dataset-revision", default=DEFAULT_BASE_REVISION)
    parser.add_argument("--split", default="test")
    parser.add_argument("--prebuilt-root")
    parser.add_argument(
        "--allow-incomplete-artifacts",
        action="store_true",
        help="Write an audit manifest without failing on missing or stale artifacts.",
    )
    parser.add_argument("--output-json", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        from datasets import load_dataset

        dataset = load_dataset(
            args.dataset,
            revision=args.dataset_revision,
            split=args.split,
        )
        spec = LSPAgentStudySpec(
            dataset=args.dataset,
            dataset_revision=args.dataset_revision,
            split=args.split,
        )
        manifest = build_base_lsp_agent_manifest(
            dataset,
            spec=spec,
            dataset_fingerprint=getattr(dataset, "_fingerprint", None),
            prebuilt_root=args.prebuilt_root,
        )
        output = Path(args.output_json)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001 - CLI should fail cleanly.
        sys.stderr.write(f"codeminer-lsp-agent-study-manifest: {exc}\n")
        return 1
    sys.stdout.write(json.dumps(manifest["summary"], sort_keys=True) + "\n")
    artifact_status = manifest["summary"].get("artifact_status") or {}
    ready = int(artifact_status.get("ready") or 0)
    expected = int(manifest["summary"]["subject_count"])
    if args.prebuilt_root and ready != expected and not args.allow_incomplete_artifacts:
        return 2
    return 0


def _artifact_status(
    root: Path, instance_id: str, expected_repo: str, expected_commit: str
) -> dict[str, Any]:
    instance = root / instance_id
    repo = instance / "repo"
    graph = instance / "graph.pkl"
    actual_commit = _git_commit(repo) if repo.is_dir() else None
    identity = _snapshot_profile_identity(instance)
    if not repo.is_dir():
        status = "missing_repo"
    elif not graph.is_file():
        status = "missing_graph"
    elif actual_commit != expected_commit:
        status = "commit_mismatch"
    elif identity is None:
        status = "unverified_identity"
    elif not _identity_matches(
        identity,
        expected_repo=expected_repo,
        expected_commit=expected_commit,
    ):
        status = "identity_mismatch"
    else:
        status = "ready"
    return {
        "actual_commit": actual_commit,
        "graph_sha256": _sha256_file(graph) if graph.is_file() else None,
        "profile_id": identity.get("profile_id") if identity else None,
        "status": status,
    }


def _snapshot_profile_identity(instance: Path) -> Optional[dict[str, Any]]:
    try:
        profile_dir = instance.resolve(strict=True)
    except OSError:
        return None
    profile_path = profile_dir / "profile.json"
    if profile_dir.parent.name != "profiles" or not profile_path.is_file():
        return None
    snapshot_path = profile_dir.parent.parent / "snapshot.json"
    if not snapshot_path.is_file():
        return None
    try:
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return {
        "profile": profile,
        "profile_id": profile_dir.name,
        "snapshot": snapshot,
        "snapshot_id": profile_dir.parent.parent.name,
    }


def _identity_matches(
    identity: Mapping[str, Any],
    *,
    expected_repo: str,
    expected_commit: str,
) -> bool:
    snapshot = identity.get("snapshot") or {}
    profile = identity.get("profile") or {}
    if not isinstance(snapshot, Mapping) or not isinstance(profile, Mapping):
        return False
    expected_snapshot = SourceSnapshot(expected_repo, expected_commit)
    if identity.get("snapshot_id") != expected_snapshot.snapshot_id:
        return False
    expected_payload = expected_snapshot.payload()
    if not all(snapshot.get(key) == value for key, value in expected_payload.items()):
        return False
    try:
        expected_profile = ArtifactProfile.create(
            profile.get("languages") or [],
            name=str(profile.get("name") or ""),
            schema_versions=profile.get("schema_versions") or {},
            options=profile.get("options") or {},
        )
    except (TypeError, ValueError):
        return False
    return identity.get("profile_id") == expected_profile.profile_id


def _manifest_summary(
    subjects: Sequence[Mapping[str, Any]], *, spec: LSPAgentStudySpec
) -> dict[str, Any]:
    by_language: dict[str, int] = {}
    by_role: dict[str, int] = {}
    artifacts: dict[str, int] = {}
    snapshots = set()
    repos = set()
    for subject in subjects:
        language = str(subject["language"])
        role = str(subject["role"])
        by_language[language] = by_language.get(language, 0) + 1
        by_role[role] = by_role.get(role, 0) + 1
        artifact = subject.get("artifact") or {}
        if artifact:
            status = str(artifact.get("status") or "unknown")
            artifacts[status] = artifacts.get(status, 0) + 1
        snapshots.add(subject["snapshot_id"])
        repos.add(subject["repo"])
    return {
        "artifact_status": dict(sorted(artifacts.items())),
        "by_language": dict(sorted(by_language.items())),
        "by_role": dict(sorted(by_role.items())),
        "repository_count": len(repos),
        "snapshot_count": len(snapshots),
        "subject_count": len(subjects),
        "planned_cell_count": len(subjects) * spec.reps * len(spec.arms),
        "planned_primary_pairs_by_role": {
            role: count * spec.reps for role, count in sorted(by_role.items())
        },
    }


def _git_commit(repo: Path) -> Optional[str]:
    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                f"safe.directory={repo}",
                "-C",
                str(repo),
                "rev-parse",
                "HEAD",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256_text(json.dumps(value, sort_keys=True, separators=(",", ":")))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "build_base_lsp_agent_manifest",
    "main",
    "task_from_manifest_subject",
]
