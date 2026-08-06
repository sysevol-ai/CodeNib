# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the commit-addressed Guardian exchange protocol."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from codenib.clients.guardian.exchange import (
    ExchangeProtocolError,
    load_exchange_request,
    materialize_exchange_request,
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ("git", *args),
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _exchange(tmp_path: Path) -> tuple[Path, Path, str, str]:
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.email", "guardian@example.com")
    _git(repo, "config", "user.name", "Guardian")
    (repo / "feature.py").write_text("value = 1\n", encoding="utf-8")
    _git(repo, "add", "feature.py")
    _git(repo, "commit", "--quiet", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "feature.py").write_text("value = 2\n", encoding="utf-8")
    _git(repo, "commit", "--quiet", "-am", "candidate")
    candidate = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/guardian/base", base)
    _git(repo, "update-ref", "refs/guardian/candidate", candidate)

    root = tmp_path / "exchange"
    requests = root / "requests"
    bundles = root / "bundles"
    requests.mkdir(parents=True)
    bundles.mkdir()
    bundle = bundles / f"{candidate}.bundle"
    _git(
        repo,
        "bundle",
        "create",
        str(bundle),
        "refs/guardian/base",
        "refs/guardian/candidate",
    )
    digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
    manifest = requests / f"{candidate}.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "request_id": candidate,
                "base_commit": base,
                "candidate_commit": candidate,
                "bundle_name": bundle.name,
                "bundle_sha256": digest,
            }
        ),
        encoding="utf-8",
    )
    return root, manifest, base, candidate


def test_exchange_materializes_exact_candidate(tmp_path: Path) -> None:
    root, manifest, base, candidate = _exchange(tmp_path)

    request, bundle = load_exchange_request(root, manifest)
    checkout = materialize_exchange_request(request, bundle, tmp_path / "checkout")

    assert request.base_commit == base
    assert request.candidate_commit == candidate
    assert _git(checkout, "rev-parse", "HEAD") == candidate
    assert (checkout / "feature.py").read_text(encoding="utf-8") == "value = 2\n"


def test_exchange_rejects_tampered_bundle(tmp_path: Path) -> None:
    root, manifest, _base, candidate = _exchange(tmp_path)
    bundle = root / "bundles" / f"{candidate}.bundle"
    bundle.write_bytes(bundle.read_bytes() + b"tampered")

    with pytest.raises(ExchangeProtocolError, match="checksum"):
        load_exchange_request(root, manifest)


def test_exchange_rejects_noncanonical_manifest_path(tmp_path: Path) -> None:
    root, manifest, _base, _candidate = _exchange(tmp_path)
    moved = root / "outside.json"
    moved.write_bytes(manifest.read_bytes())

    with pytest.raises(ExchangeProtocolError, match="canonical requests"):
        load_exchange_request(root, moved)
