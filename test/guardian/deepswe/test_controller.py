# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the host-side DeepSWE Guardian controller."""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import threading
import time
from pathlib import Path, PurePosixPath

from codenib.clients.guardian import GuardianConfig, GuardianResult, ReviewStatus
from scripts.guardian.deepswe.harness import controller as controller_module
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
    def __init__(
        self,
        requests: list,
        *,
        status: ReviewStatus = ReviewStatus.COMPLETE,
        errors: tuple[str, ...] = (),
    ) -> None:
        self.requests = requests
        self.status = status
        self.errors = errors

    async def review(self, request):
        self.requests.append(request)
        return GuardianResult(
            base_commit=request.base_commit,
            candidate_commit=request.candidate_commit,
            status=self.status,
            summary="No admitted violation.",
            errors=self.errors,
        )


def test_sandbox_reviewer_uses_stable_codex_tool_execution(
    tmp_path: Path, monkeypatch
) -> None:
    executor_options = {}

    class FakeExecutor:
        def __init__(self, **kwargs) -> None:
            executor_options.update(kwargs)

    monkeypatch.setattr(controller_module, "CodexExecutor", FakeExecutor)
    factory = controller_module.sandbox_reviewer_factory(
        config=GuardianConfig(explorer_model="luna", aggregator_model="terra"),
        image="fixture:latest",
        codex_executable="/usr/local/bin/codex-guardian",
        auth_json=None,
    )

    reviewer = factory(tmp_path)

    assert reviewer.executor is not None
    assert executor_options["enabled_features"] == ("unified_exec",)
    assert executor_options["disabled_features"] == (
        "code_mode",
        "code_mode_host",
    )
    assert executor_options["process_runner"]._environment["PYTHONPATH"] == (
        "/workspace"
    )
    assert (
        executor_options["process_runner"]._staged_files[
            PurePosixPath(".guardian-runtime/.keep")
        ]
        == b""
    )


def test_sandbox_reviewer_stages_codex_auth_with_runtime_directory(
    tmp_path: Path, monkeypatch
) -> None:
    executor_options = {}

    class FakeExecutor:
        def __init__(self, **kwargs) -> None:
            executor_options.update(kwargs)

    monkeypatch.setattr(controller_module, "CodexExecutor", FakeExecutor)
    factory = controller_module.sandbox_reviewer_factory(
        config=GuardianConfig(explorer_model="luna", aggregator_model="terra"),
        image="fixture:latest",
        codex_executable="/usr/local/bin/codex-guardian",
        auth_json=b"credential",
    )

    factory(tmp_path)

    assert executor_options["process_runner"]._staged_files == {
        PurePosixPath(".guardian-runtime/.keep"): b"",
        PurePosixPath(".guardian-runtime/auth.json"): b"credential",
    }


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
        task_description="Enable the feature without breaking existing callers.",
        poll_interval_seconds=0.01,
    )

    assert asyncio.run(controller.process_pending()) == 1

    assert len(requests) == 1
    request = requests[0]
    assert request.base_commit == base
    assert request.candidate_commit == candidate
    assert request.task_description == (
        "Enable the feature without breaking existing callers."
    )
    assert "-enabled = False" in request.change_patch
    assert "+enabled = True" in request.change_patch
    status = json.loads(
        (exchange / "responses" / candidate / "status.json").read_text()
    )
    assert status["analysis_status"] == "complete"
    assert status["llm_backend"] == "codex-cli+codenib-sandbox"
    assert status["explorer_model"] == "luna"
    assert status["aggregator_model"] == "terra"
    assert status["cycle_index"] == 1
    assert status["max_cycles"] == 3
    assert status["terminal"] is False
    assert status["memory_specifications"] == 0
    assert status["memory_snapshots"] == 1
    assert status["probe_metrics"] == {
        "accepted": 0,
        "declared": 0,
        "inconclusive": 0,
        "rejected": 0,
        "rejected_by_reason": {},
        "repair_accepted": False,
        "repair_attempted": False,
        "repaired": 0,
        "satisfied": 0,
        "violated": 0,
    }
    assert (exchange / "latest" / "findings.md").is_file()
    assert (tmp_path / "episodes" / candidate / "status.json").is_file()
    assert (exchange / "last_reviewed_commit").read_text().strip() == candidate


def test_host_controller_ignores_unverified_persisted_review_head(
    tmp_path: Path,
) -> None:
    exchange, base, candidate = _publish(tmp_path)
    (exchange / "last_reviewed_commit").write_text(candidate + "\n")
    requests = []
    controller = GuardianHostController(
        exchange_root=exchange,
        episodes_root=tmp_path / "episodes",
        config=GuardianConfig(explorer_model="luna", aggregator_model="terra"),
        reviewer_factory=lambda _workspace: FakeReviewer(requests),
        initial_base_commit=base,
    )

    assert asyncio.run(controller.process_pending()) == 1
    assert [request.candidate_commit for request in requests] == [candidate]


def test_blocking_controller_serves_while_caller_thread_is_blocked(
    tmp_path: Path,
) -> None:
    exchange, base, candidate = _publish(tmp_path)
    requests = []
    controller = GuardianHostController(
        exchange_root=exchange,
        episodes_root=tmp_path / "episodes",
        config=GuardianConfig(explorer_model="luna", aggregator_model="terra"),
        reviewer_factory=lambda _workspace: FakeReviewer(requests),
        initial_base_commit=base,
        poll_interval_seconds=0.01,
    )
    stop = threading.Event()
    worker = threading.Thread(target=controller.serve_blocking, args=(stop,))
    worker.start()
    status = exchange / "responses" / candidate / "status.json"
    deadline = time.monotonic() + 5

    # Deliberately block this thread rather than yield an asyncio event loop.
    while not status.is_file() and time.monotonic() < deadline:
        time.sleep(0.01)
    stop.set()
    worker.join(timeout=5)

    assert status.is_file()
    assert requests
    assert not worker.is_alive()


def test_host_controller_waits_for_inflight_review(tmp_path: Path) -> None:
    exchange, base, candidate = _publish(tmp_path)

    async def exercise() -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        class BlockingReviewer(FakeReviewer):
            async def review(self, request):
                started.set()
                await release.wait()
                return await super().review(request)

        controller = GuardianHostController(
            exchange_root=exchange,
            episodes_root=tmp_path / "episodes",
            config=GuardianConfig(explorer_model="luna", aggregator_model="terra"),
            reviewer_factory=lambda _workspace: BlockingReviewer([]),
            initial_base_commit=base,
            poll_interval_seconds=0.01,
        )
        stop = asyncio.Event()
        server = asyncio.create_task(controller.serve(stop))
        await started.wait()
        waiter = asyncio.create_task(controller.wait_until_idle())
        await asyncio.sleep(0.02)
        assert not waiter.done()
        release.set()
        await waiter
        assert (exchange / "responses" / candidate / "status.json").is_file()
        stop.set()
        await server

    asyncio.run(exercise())


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
    assert not (exchange / "last_reviewed_commit").exists()


def test_host_controller_exposes_degraded_analysis_warnings(tmp_path: Path) -> None:
    exchange, base, candidate = _publish(tmp_path)
    validation_error = "explorer_1 candidate 2 evidence path does not resolve"
    controller = GuardianHostController(
        exchange_root=exchange,
        episodes_root=tmp_path / "episodes",
        config=GuardianConfig(explorer_model="luna", aggregator_model="terra"),
        reviewer_factory=lambda _workspace: FakeReviewer(
            [],
            status=ReviewStatus.DEGRADED,
            errors=(validation_error,),
        ),
        initial_base_commit=base,
    )

    assert asyncio.run(controller.process_pending()) == 1
    status = json.loads(
        (exchange / "responses" / candidate / "status.json").read_text()
    )
    assert status["analysis_status"] == "degraded"
    assert status["exit_reason"] == "ReviewCompleted"
    assert status["error"] == ""
    assert status["analysis_warnings"] == [validation_error]
    assert (exchange / "last_reviewed_commit").read_text().strip() == candidate


def test_host_controller_labels_failed_review_truthfully(tmp_path: Path) -> None:
    exchange, base, candidate = _publish(tmp_path)
    controller = GuardianHostController(
        exchange_root=exchange,
        episodes_root=tmp_path / "episodes",
        config=GuardianConfig(explorer_model="luna", aggregator_model="terra"),
        reviewer_factory=lambda _workspace: FakeReviewer(
            [],
            status=ReviewStatus.FAILED,
            errors=("explorers unavailable",),
        ),
        initial_base_commit=base,
    )

    assert asyncio.run(controller.process_pending()) == 1
    status = json.loads(
        (exchange / "responses" / candidate / "status.json").read_text()
    )
    assert status["analysis_status"] == "failed"
    assert status["exit_reason"] == "ReviewFailed"
    assert status["review_performed"] is True
    assert status["error"] == "explorers unavailable"
    assert status["analysis_warnings"] == ["explorers unavailable"]
    assert not (exchange / "last_reviewed_commit").exists()


def test_host_controller_processes_pending_requests_in_commit_order(
    tmp_path: Path,
) -> None:
    exchange, base, candidate = _publish(tmp_path)
    revised = _publish_next(tmp_path / "solver", exchange, candidate)
    # Make filesystem order disagree with commit order. The controller must
    # still start with the request rooted at its current review head.
    first_manifest = exchange / "requests" / f"{candidate}.json"
    second_manifest = exchange / "requests" / f"{revised}.json"
    first_manifest.touch()
    second_manifest.touch()
    second_mtime = second_manifest.stat().st_mtime_ns
    first_manifest.touch()
    assert first_manifest.stat().st_mtime_ns >= second_mtime

    requests = []
    controller = GuardianHostController(
        exchange_root=exchange,
        episodes_root=tmp_path / "episodes",
        config=GuardianConfig(explorer_model="luna", aggregator_model="terra"),
        reviewer_factory=lambda _workspace: FakeReviewer(requests),
        initial_base_commit=base,
    )

    assert asyncio.run(controller.process_pending()) == 2
    assert [request.candidate_commit for request in requests] == [candidate, revised]
    assert requests[1].base_commit == candidate
    assert requests[0].memory.observed_snapshots == ()
    assert requests[1].memory.observed_snapshots == (candidate,)
    assert (exchange / "last_reviewed_commit").read_text().strip() == revised
    memory = json.loads((tmp_path / "guardian_state" / "memory.json").read_text())
    assert memory["observed_snapshots"] == [candidate, revised]
    assert (tmp_path / "guardian_state" / "events.jsonl").read_text().count("\n") == 2


def test_host_controller_marks_stale_request_superseded_without_replacing_latest(
    tmp_path: Path,
) -> None:
    exchange, base, candidate = _publish(tmp_path)
    requests = []
    controller = GuardianHostController(
        exchange_root=exchange,
        episodes_root=tmp_path / "episodes",
        config=GuardianConfig(explorer_model="luna", aggregator_model="terra"),
        reviewer_factory=lambda _workspace: FakeReviewer(requests),
        initial_base_commit=base,
    )
    assert asyncio.run(controller.process_pending()) == 1

    stale = _publish_next(tmp_path / "solver", exchange, base)
    assert asyncio.run(controller.process_pending()) == 1

    assert len(requests) == 1
    stale_status = json.loads(
        (exchange / "responses" / stale / "status.json").read_text()
    )
    assert stale_status["analysis_status"] == "not_run"
    assert stale_status["exit_reason"] == "ReviewSuperseded"
    assert stale_status["termination_reason"] == "stale_base_commit"
    latest_status = json.loads((exchange / "latest" / "status.json").read_text())
    assert latest_status["commit"] == candidate
    assert (exchange / "last_reviewed_commit").read_text().strip() == candidate


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
        initial_base_commit=base,
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
    assert (exchange / "last_reviewed_commit").read_text().strip() == candidate
