# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the host-side DeepSWE Guardian controller."""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
from pathlib import Path

from codenib.clients.guardian import GuardianConfig, GuardianResult, ReviewStatus
from scripts.guardian.deepswe.harness.controller import GuardianHostController


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ("git", *args),
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _publish(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "solver"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.email", "solver@example.com")
    _git(repo, "config", "user.name", "Solver")
    (repo / "feature.py").write_text("enabled = False\n", encoding="utf-8")
    _git(repo, "add", "feature.py")
    _git(repo, "commit", "--quiet", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "feature.py").write_text("enabled = True\n", encoding="utf-8")
    _git(repo, "commit", "--quiet", "-am", "candidate")
    candidate = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/guardian/base", base)
    _git(repo, "update-ref", "refs/guardian/candidate", candidate)

    exchange = tmp_path / "exchange"
    (exchange / "requests").mkdir(parents=True)
    (exchange / "bundles").mkdir()
    bundle = exchange / "bundles" / f"{candidate}.bundle"
    _git(
        repo,
        "bundle",
        "create",
        str(bundle),
        "refs/guardian/base",
        "refs/guardian/candidate",
    )
    (exchange / "requests" / f"{candidate}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "request_id": candidate,
                "base_commit": base,
                "candidate_commit": candidate,
                "bundle_name": bundle.name,
                "bundle_sha256": hashlib.sha256(bundle.read_bytes()).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    return exchange, base, candidate


def _publish_next(repo: Path, exchange: Path, base: str) -> str:
    (repo / "feature.py").write_text("enabled = 'revised'\n", encoding="utf-8")
    _git(repo, "add", "feature.py")
    _git(repo, "commit", "--quiet", "-m", "revision")
    candidate = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/guardian/base", base)
    _git(repo, "update-ref", "refs/guardian/candidate", candidate)
    bundle = exchange / "bundles" / f"{candidate}.bundle"
    _git(
        repo,
        "bundle",
        "create",
        str(bundle),
        "refs/guardian/base",
        "refs/guardian/candidate",
    )
    (exchange / "requests" / f"{candidate}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "request_id": candidate,
                "base_commit": base,
                "candidate_commit": candidate,
                "bundle_name": bundle.name,
                "bundle_sha256": hashlib.sha256(bundle.read_bytes()).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    return candidate


class FakeReviewer:
    def __init__(self, requests: list) -> None:
        self.requests = requests

    async def review(self, request):
        self.requests.append(request)
        return GuardianResult(
            base_commit=request.base_commit,
            candidate_commit=request.candidate_commit,
            status=ReviewStatus.COMPLETE,
            summary="No admitted violation.",
        )


def test_host_controller_reviews_materialized_snapshot(tmp_path: Path) -> None:
    exchange, base, candidate = _publish(tmp_path)
    requests = []
    config = GuardianConfig(explorer_model="luna", aggregator_model="terra")
    controller = GuardianHostController(
        exchange_root=exchange,
        episodes_root=tmp_path / "episodes",
        config=config,
        reviewer_factory=lambda _workspace: FakeReviewer(requests),
        initial_base_commit=base,
        poll_interval_seconds=0.01,
    )

    assert asyncio.run(controller.process_pending()) == 1

    assert len(requests) == 1
    request = requests[0]
    assert request.base_commit == base
    assert request.candidate_commit == candidate
    assert "-enabled = False" in request.change_patch
    assert "+enabled = True" in request.change_patch
    status = json.loads(
        (exchange / "responses" / candidate / "status.json").read_text()
    )
    assert status["analysis_status"] == "complete"
    assert status["llm_backend"] == "codex-cli+codenib-sandbox"
    assert status["cycle_index"] == 1
    assert status["max_cycles"] == 3
    assert status["terminal"] is False
    assert (exchange / "latest" / "findings.md").is_file()
    assert (tmp_path / "episodes" / candidate / "status.json").is_file()


def test_host_controller_reports_invalid_bundle_without_review(tmp_path: Path) -> None:
    exchange, base, candidate = _publish(tmp_path)
    bundle = exchange / "bundles" / f"{candidate}.bundle"
    bundle.write_bytes(bundle.read_bytes() + b"tampered")
    requests = []
    config = GuardianConfig(explorer_model="luna", aggregator_model="terra")
    controller = GuardianHostController(
        exchange_root=exchange,
        episodes_root=tmp_path / "episodes",
        config=config,
        reviewer_factory=lambda _workspace: FakeReviewer(requests),
        initial_base_commit=base,
    )

    assert asyncio.run(controller.process_pending()) == 1

    assert requests == []
    status = json.loads(
        (exchange / "responses" / candidate / "status.json").read_text()
    )
    assert status["analysis_status"] == "failed"
    assert "checksum" in status["error"]


def test_host_controller_stops_before_review_beyond_cycle_limit(tmp_path: Path) -> None:
    exchange, base, candidate = _publish(tmp_path)
    requests = []
    config = GuardianConfig(explorer_model="luna", aggregator_model="terra")
    controller = GuardianHostController(
        exchange_root=exchange,
        episodes_root=tmp_path / "episodes",
        config=config,
        reviewer_factory=lambda _workspace: FakeReviewer(requests),
        initial_base_commit=base,
        max_cycles=1,
    )

    assert asyncio.run(controller.process_pending()) == 1
    first_status = json.loads(
        (exchange / "responses" / candidate / "status.json").read_text()
    )
    assert first_status["review_performed"] is True
    assert first_status["terminal"] is True
    assert first_status["termination_reason"] == "max_cycles_reached"

    repo = tmp_path / "solver"
    revised = _publish_next(repo, exchange, candidate)
    restarted = GuardianHostController(
        exchange_root=exchange,
        episodes_root=tmp_path / "episodes",
        config=config,
        reviewer_factory=lambda _workspace: FakeReviewer(requests),
        initial_base_commit=candidate,
        max_cycles=1,
    )
    assert asyncio.run(restarted.process_pending()) == 1

    assert len(requests) == 1
    terminal_status = json.loads(
        (exchange / "responses" / revised / "status.json").read_text()
    )
    assert terminal_status["review_performed"] is False
    assert terminal_status["analysis_status"] == "not_run"
    assert terminal_status["exit_reason"] == "ReviewLimitReached"
    assert terminal_status["terminal"] is True
