# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import multiprocessing
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from codenib import cli
from codenib import codegraph_onboarding as onboarding
from codenib import openrouter_auth as auth
from codenib._version import package_version


@pytest.fixture
def setup(tmp_path, monkeypatch):
    repo = tmp_path / "working copy"
    repo.mkdir()
    (repo / "app.py").write_text("def run():\n    return 'uncommitted source'\n")
    monkeypatch.setenv("CODENIB_HOME", str(tmp_path / "state"))
    configured, added, removed, metadata = {}, [], [], []
    monkeypatch.setattr(
        onboarding,
        "resolve_codenib_command",
        lambda _=None: ("/opt/code/bin/codenib", ()),
    )
    monkeypatch.setattr(
        onboarding,
        "resolve_requested_clients",
        lambda requested: tuple(requested or ("codex", "claude")),
    )
    monkeypatch.setattr(
        onboarding,
        "inspect_server_command",
        lambda *_: SimpleNamespace(ready=True, detail="ready"),
    )
    monkeypatch.setattr(
        auth, "credential", lambda: ("private-test-credential", "keyring")
    )

    def key_info(key):
        assert key == "private-test-credential"
        metadata.append(True)
        return {"limit": 2, "limit_remaining": 1.5, "usage": 0.5}

    monkeypatch.setattr(auth, "key_info", key_info)
    monkeypatch.setattr(
        cli,
        "index_repository",
        lambda *_a, **_k: pytest.fail("source-only setup must not index"),
    )

    def inspect(client, server, _repo):
        value = configured.get((client, server.name))
        return onboarding.ClientInspection(
            client,
            True,
            value is not None,
            value == server,
            "configuration matches" if value == server else "missing or different",
        )

    def add(client, server, _repo):
        assert (client, server.name) not in configured
        added.append(client)
        configured[client, server.name] = server

    def remove(client, server, _repo):
        removed.append(client)
        configured.pop((client, server.name))

    monkeypatch.setattr(onboarding, "inspect_client_registration", inspect)
    monkeypatch.setattr(onboarding, "add_client_registration", add)
    monkeypatch.setattr(onboarding, "remove_client_registration", remove)
    return SimpleNamespace(
        repo=repo,
        configured=configured,
        added=added,
        removed=removed,
        metadata=metadata,
    )


def test_init_registers_both_agents_without_indexing_or_repository_writes(
    setup, capsys
):
    before = {p.name: p.read_bytes() for p in setup.repo.iterdir()}
    assert cli.run(["init", str(setup.repo)]) == 0
    assert cli.run(["init", str(setup.repo)]) == 0
    receipt = onboarding.load_codegraph_receipt(setup.repo, retrieval_route="grep-jev")
    assert receipt.server.args == (
        "mcp",
        str(setup.repo),
        "--retrieval-route",
        "grep-jev",
    )
    assert {c.state for c in receipt.clients} == {"configured"}
    assert setup.added == ["codex", "claude"]
    assert onboarding.load_codegraph_receipt(setup.repo) is None
    assert {p.name: p.read_bytes() for p in setup.repo.iterdir()} == before
    assert (
        "private-test-credential"
        not in json.dumps(receipt.to_dict()) + capsys.readouterr().out
    )


def test_dry_run_does_not_open_keyring_or_authorize(setup, monkeypatch):
    monkeypatch.setattr(
        auth, "credential", lambda: pytest.fail("dry run must not open a keyring")
    )
    assert cli.run(["init", str(setup.repo), "--dry-run"]) == 0
    assert setup.added == setup.metadata == []
    assert not onboarding.codegraph_receipt_path(
        setup.repo, retrieval_route="grep-jev"
    ).exists()


def test_init_rejects_an_unmanaged_matching_registration(setup):
    server = onboarding.make_server_spec(
        setup.repo, command="/opt/code/bin/codenib", retrieval_route="grep-jev"
    )
    setup.configured["claude", server.name] = server
    assert cli.run(["init", str(setup.repo)]) == 2
    assert setup.added == setup.metadata == []
    assert (
        onboarding.load_codegraph_receipt(setup.repo, retrieval_route="grep-jev")
        is None
    )


def test_authorization_failure_precedes_receipt_and_native_mutation(setup, monkeypatch):
    def denied(_):
        raise auth.OpenRouterAuthError(
            "OpenRouter authorization failed (HTTP 401); no retry"
        )

    monkeypatch.setattr(auth, "key_info", denied)
    assert cli.run(["init", str(setup.repo)]) == 2
    assert setup.added == []
    assert (
        onboarding.load_codegraph_receipt(setup.repo, retrieval_route="grep-jev")
        is None
    )


def test_noninteractive_init_does_not_start_a_browser(setup, monkeypatch):
    monkeypatch.setattr(auth, "credential", lambda: (None, "none"))
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(
        cli, "_run_auth", lambda _: pytest.fail("interactive authorization")
    )
    assert cli.run(["init", str(setup.repo)]) == 2
    assert setup.added == []


def test_init_uses_existing_pkce_login_when_credential_is_absent(setup, monkeypatch):
    credentials = iter([(None, "none"), ("private-test-credential", "keyring")])
    monkeypatch.setattr(auth, "credential", lambda: next(credentials))
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    requests = []
    monkeypatch.setattr(cli, "_run_auth", lambda args: requests.append(vars(args)) or 0)
    assert cli.run(["init", str(setup.repo), "--headless", "--store", "file"]) == 0
    assert requests == [
        {
            "auth_command": "login",
            "store": "file",
            "headless": True,
            "import_key": False,
            "timeout": 300.0,
        }
    ]
    assert setup.metadata == []  # login already verifies the credential


def test_native_state_is_rechecked_after_authorization(setup, monkeypatch):
    def changed(_):
        server = onboarding.make_server_spec(
            setup.repo, command="/other/command", retrieval_route="grep-jev"
        )
        setup.configured["codex", server.name] = server
        return {"limit": 1}

    monkeypatch.setattr(auth, "key_info", changed)
    assert cli.run(["init", str(setup.repo)]) == 2
    assert setup.added == []
    assert (
        onboarding.load_codegraph_receipt(setup.repo, retrieval_route="grep-jev")
        is None
    )


def test_partial_native_failure_resumes_from_pending_receipt(setup, monkeypatch):
    original = onboarding.add_client_registration

    def fail_claude(client, server, repo):
        if client == "claude":
            raise onboarding.CodeGraphOnboardingError("native client unavailable")
        original(client, server, repo)

    monkeypatch.setattr(onboarding, "add_client_registration", fail_claude)
    assert cli.run(["init", str(setup.repo)]) == 2
    receipt = onboarding.load_codegraph_receipt(setup.repo, retrieval_route="grep-jev")
    assert [(c.name, c.state) for c in receipt.clients] == [
        ("codex", "configured"),
        ("claude", "pending"),
    ]
    monkeypatch.setattr(onboarding, "add_client_registration", original)
    assert cli.run(["init", str(setup.repo)]) == 0
    assert setup.added == ["codex", "claude"]


def test_native_success_with_pending_receipt_is_recoverable(setup, monkeypatch):
    original = onboarding.write_codegraph_receipt

    def interrupt(receipt):
        if any(c.state == "configured" for c in receipt.clients):
            raise OSError("receipt publication interrupted after native success")
        return original(receipt)

    monkeypatch.setattr(onboarding, "write_codegraph_receipt", interrupt)
    assert cli.run(["init", str(setup.repo), "--agent", "codex"]) == 2
    assert setup.added == ["codex"]
    monkeypatch.setattr(onboarding, "write_codegraph_receipt", original)
    assert cli.run(["init", str(setup.repo), "--agent", "codex"]) == 0
    assert setup.added == ["codex"]


def test_grep_uninstall_preserves_codegraph_registration_and_credentials(
    setup, monkeypatch
):
    graph = onboarding.make_server_spec(setup.repo, command="/opt/code/bin/codenib")
    graph_receipt = onboarding.CodeGraphReceipt(setup.repo, graph, ()).with_client(
        "codex", state="configured"
    )
    onboarding.write_codegraph_receipt(graph_receipt)
    setup.configured["codex", graph.name] = graph
    assert cli.run(["init", str(setup.repo)]) == 0
    monkeypatch.setattr(
        auth,
        "forget_key",
        lambda **_: pytest.fail("uninstall must not remove credentials"),
    )
    assert cli.run(["uninstall", str(setup.repo)]) == 0
    assert setup.configured == {("codex", graph.name): graph}
    assert onboarding.load_codegraph_receipt(setup.repo) == graph_receipt
    assert (
        onboarding.load_codegraph_receipt(setup.repo, retrieval_route="grep-jev")
        is None
    )


def test_drift_is_not_overwritten_or_removed_without_explicit_force(setup):
    assert cli.run(["init", str(setup.repo)]) == 0
    receipt = onboarding.load_codegraph_receipt(setup.repo, retrieval_route="grep-jev")
    setup.configured["codex", receipt.server.name] = replace(
        receipt.server, args=("user-change",)
    )
    assert cli.run(["init", str(setup.repo)]) == 2
    assert cli.run(["uninstall", str(setup.repo)]) == 2
    assert setup.removed == []
    assert cli.run(["uninstall", str(setup.repo), "--force"]) == 0
    assert setup.configured == {}


def test_status_reports_local_readiness_without_provider_call(
    setup, monkeypatch, capsys
):
    assert cli.run(["init", str(setup.repo)]) == 0
    capsys.readouterr()
    monkeypatch.setattr(
        auth, "key_info", lambda _: pytest.fail("status must not contact provider")
    )
    assert cli.run(["status", str(setup.repo), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ready"] and report["runtime_ready"]
    assert report["provider_verified"] is False
    assert report["credential_source"] == "keyring"
    assert len(report["clients"]) == 2


def test_grep_probe_checks_route_without_reading_source_or_credentials(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(
        auth, "credential", lambda: pytest.fail("probe must not unlock credentials")
    )
    monkeypatch.setattr(
        cli,
        "resolve_repo_path",
        lambda _: pytest.fail("probe must not read a checkout"),
    )
    calls = []
    monkeypatch.setattr(
        cli, "_require_modules", lambda modules, **_: calls.extend(modules)
    )
    monkeypatch.setattr(
        "codenib.agent.runtime.grep_jev.resolve_ripgrep", lambda: "/environment/bin/rg"
    )
    assert cli.run(["mcp", "--runtime-probe", "--retrieval-route", "grep-jev"]) == 0
    assert calls == ["requests", "mcp"]
    assert "grep-jev mcp runtime ready" in capsys.readouterr().out
    assert (
        cli.run(
            ["mcp", str(tmp_path), "--runtime-probe", "--retrieval-route", "grep-jev"]
        )
        == 2
    )


def test_server_command_probes_the_recorded_route(tmp_path):
    server = onboarding.make_server_spec(
        tmp_path,
        command="/python",
        command_prefix=("-m", "codenib"),
        retrieval_route="grep-jev",
    )
    calls = []

    def run(command, **_):
        calls.append(tuple(command))
        text = (
            f"codenib {package_version()}"
            if command[-1] == "--version"
            else "codenib grep-jev mcp runtime ready"
        )
        return subprocess.CompletedProcess(command, 0, text, "")

    assert onboarding.inspect_server_command(server, tmp_path, runner=run).ready
    assert calls[1] == (
        "/python",
        "-m",
        "codenib",
        "mcp",
        "--runtime-probe",
        "--retrieval-route",
        "grep-jev",
    )


def _register_from_stale_snapshot(repo_string, client, barrier, results):
    """Separate processes deliberately read the same empty receipt first."""
    repo = Path(repo_string)
    try:
        server = onboarding.make_server_spec(
            repo, command="/opt/code/bin/codenib", retrieval_route="grep-jev"
        )
        receipt = onboarding.load_codegraph_receipt(repo, retrieval_route="grep-jev")
        assert receipt is None

        def inspect(name, _server, _repo):
            exists = (repo.parent / f"{name}.native").is_file()
            return onboarding.ClientInspection(name, True, exists, exists, "fixture")

        def add(name, _server, _repo):
            (repo.parent / f"{name}.native").write_text("registered")

        onboarding.inspect_client_registration = inspect
        onboarding.add_client_registration = add
        barrier.wait(timeout=15)
        onboarding.configure_client_registrations(repo, [client], server, receipt)
        results.put(None)
    except Exception as exc:
        results.put(repr(exc))


def test_concurrent_processes_preserve_both_native_registration_owners(
    tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("CODENIB_HOME", str(tmp_path / "state"))
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(3)
    results = context.Queue()
    processes = [
        context.Process(
            target=_register_from_stale_snapshot,
            args=(str(repo), name, barrier, results),
        )
        for name in ("codex", "claude")
    ]
    try:
        for process in processes:
            process.start()
        barrier.wait(timeout=15)
        assert [results.get(timeout=15) for _ in processes] == [None, None]
        for process in processes:
            process.join(timeout=5)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        results.close()
    receipt = onboarding.load_codegraph_receipt(repo, retrieval_route="grep-jev")
    assert {c.name for c in receipt.clients} == {"codex", "claude"}
    assert all(c.state == "configured" for c in receipt.clients)
    assert (tmp_path / "codex.native").is_file()
    assert (tmp_path / "claude.native").is_file()


def test_status_keeps_healthy_client_after_another_inspection_fails(
    setup, monkeypatch, capsys
):
    assert cli.run(["init", str(setup.repo)]) == 0
    capsys.readouterr()
    original = onboarding.inspect_client_registration

    def inspect(client, *args):
        if client == "codex":
            raise onboarding.CodeGraphOnboardingError("codex config is corrupt")
        return original(client, *args)

    monkeypatch.setattr(onboarding, "inspect_client_registration", inspect)
    assert cli.run(["status", str(setup.repo), "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    clients = {c["name"]: c for c in report["clients"]}
    assert clients["codex"]["error"] == "codex config is corrupt"
    assert clients["claude"]["matches"]
    assert report["ready"] is False


def test_symlinked_ancestor_has_one_identity_through_init_status_and_uninstall(setup):
    alias = setup.repo.parent / "alias"
    alias.symlink_to(setup.repo.parent, target_is_directory=True)
    lexical = alias / setup.repo.name
    assert cli.run(["init", str(lexical), "--dry-run"]) == 0
    assert cli.run(["init", str(lexical)]) == 0
    assert cli.run(["status", str(lexical)]) == 0
    receipt = onboarding.load_codegraph_receipt(setup.repo, retrieval_route="grep-jev")
    assert receipt.repository == setup.repo
    assert receipt.server.args[1] == str(setup.repo)
    assert cli.run(["uninstall", str(lexical)]) == 0
    assert setup.configured == {}
