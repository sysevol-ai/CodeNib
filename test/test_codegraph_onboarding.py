# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from codenib import cli
from codenib.codegraph_onboarding import (
    ClientInspection,
    CodeGraphOnboardingError,
    CodeGraphReceipt,
    MCPServerSpec,
    add_client_registration,
    codegraph_receipt_path,
    codegraph_server_name,
    inspect_client_registration,
    inspect_server_command,
    load_codegraph_receipt,
    make_server_spec,
    remove_client_registration,
    resolve_codenib_command,
    resolve_requested_clients,
    write_codegraph_receipt,
)
from codenib.compiler.manifest import IndexEntry, RepoManifest


def _repository(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    (repo / "sample.py").write_text("def sample():\n    return 1\n", encoding="utf-8")
    return repo


def _initialize_git_repository(repo: Path) -> None:
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "CodeNib Test"],
        check=True,
    )
    subprocess.run(["git", "-C", str(repo), "add", "sample.py"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-qm", "test fixture"],
        check=True,
    )


def _server(repo: Path) -> MCPServerSpec:
    return make_server_spec(repo, command="/opt/codenib/bin/codenib")


def _manifest(repo: Path) -> RepoManifest:
    fingerprint = "sha256-v2:" + "a" * 64
    entries = {
        name: IndexEntry(
            index_type=name,
            path=f"/state/{name}",
            built_at="2026-08-13T00:00:00Z",
            built_at_epoch=1.0,
            status="fresh",
            commit="",
            source_fingerprint=fingerprint,
        )
        for name in ("bm25", "symbol_graph")
    }
    return RepoManifest(
        repo_path=str(repo),
        source_fingerprint=fingerprint,
        last_indexed_source_fingerprint=fingerprint,
        languages=["python"],
        indexes=entries,
    )


def test_codegraph_parser_exposes_init_status_and_uninstall() -> None:
    parser = cli.build_parser()

    init = parser.parse_args(
        [
            "codegraph",
            "init",
            ".",
            "--agent",
            "codex",
            "--agent",
            "claude",
            "--dry-run",
        ]
    )
    status = parser.parse_args(["codegraph", "status", ".", "--json"])
    uninstall = parser.parse_args(
        ["codegraph", "uninstall", ".", "--agent", "codex", "--force"]
    )

    assert init.codegraph_command == "init"
    assert init.agent == ["codex", "claude"]
    assert init.dry_run is True
    assert status.codegraph_command == "status"
    assert status.json is True
    assert uninstall.codegraph_command == "uninstall"
    assert uninstall.force is True


def test_server_name_is_bounded_readable_and_checkout_specific(tmp_path: Path) -> None:
    first = _repository(tmp_path, "A repository " + "x" * 100)
    second = _repository(tmp_path, "other")

    first_name = codegraph_server_name(first)
    second_name = codegraph_server_name(second)

    assert first_name.startswith("codenib-a-repository-")
    assert len(first_name) <= 54
    assert first_name != second_name
    assert set(first_name) <= set("abcdefghijklmnopqrstuvwxyz0123456789._-")


def test_command_resolution_prefers_installed_executable(tmp_path: Path) -> None:
    executable = tmp_path / "codenib"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)

    command, prefix = resolve_codenib_command(
        which=lambda value: str(executable) if value == "codenib" else None
    )

    assert command == str(executable)
    assert prefix == ()


def test_command_resolution_falls_back_to_current_python() -> None:
    command, prefix = resolve_codenib_command(which=lambda _value: None)

    assert Path(command).is_absolute()
    assert prefix == ("-m", "codenib")


def test_auto_client_resolution_is_stable_and_requires_a_client() -> None:
    assert resolve_requested_clients(
        None,
        which=lambda value: f"/bin/{value}" if value in {"codex", "claude"} else None,
    ) == ("codex", "claude")
    assert resolve_requested_clients(
        ["claude"],
        which=lambda value: f"/bin/{value}" if value == "claude" else None,
    ) == ("claude",)

    with pytest.raises(CodeGraphOnboardingError, match="no supported agent"):
        resolve_requested_clients(None, which=lambda _value: None)
    with pytest.raises(CodeGraphOnboardingError, match="cannot be combined"):
        resolve_requested_clients(
            ["auto", "codex"],
            which=lambda value: f"/bin/{value}",
        )


def test_receipt_round_trip_is_private_and_deterministic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repository(tmp_path)
    monkeypatch.setenv("CODENIB_HOME", str(tmp_path / "state"))
    receipt = CodeGraphReceipt(repo, _server(repo), ()).with_client(
        "codex", state="configured"
    )

    path = write_codegraph_receipt(receipt)
    first = path.read_bytes()
    write_codegraph_receipt(receipt)

    assert load_codegraph_receipt(repo) == receipt
    assert path.read_bytes() == first
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
    assert path == codegraph_receipt_path(repo)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(extra=True),
        lambda value: value.update(schema_version=True),
        lambda value: value["server"].update(args=["mcp", "/other"]),
        lambda value: value["server"].update(command="relative-codenib"),
        lambda value: value["clients"].update(codex={"scope": "user"}),
    ],
)
def test_receipt_validation_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation,
) -> None:
    repo = _repository(tmp_path)
    monkeypatch.setenv("CODENIB_HOME", str(tmp_path / "state"))
    receipt = CodeGraphReceipt(repo, _server(repo), ()).with_client(
        "codex", state="configured"
    )
    path = write_codegraph_receipt(receipt)
    value = json.loads(path.read_text(encoding="utf-8"))
    mutation(value)
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(CodeGraphOnboardingError, match="receipt"):
        load_codegraph_receipt(repo)


class _NativeClientHarness:
    def __init__(self) -> None:
        self.configs: dict[str, MCPServerSpec] = {}
        self.commands: list[tuple[str, ...]] = []

    @staticmethod
    def which(client: str) -> str | None:
        return f"/clients/{client}" if client in {"codex", "claude"} else None

    def run(self, command, **_kwargs):
        argv = tuple(command)
        self.commands.append(argv)
        client = Path(argv[0]).name
        operation = argv[2]
        if operation == "get":
            name = argv[-1]
            server = self.configs.get(client)
            if server is None or server.name != name:
                return subprocess.CompletedProcess(argv, 1, "", "not found")
            if client == "codex":
                stdout = json.dumps(
                    {
                        "name": server.name,
                        "enabled": True,
                        "transport": {
                            "type": "stdio",
                            "command": server.command,
                            "args": list(server.args),
                            "cwd": None,
                            "env": None,
                        },
                    }
                )
            else:
                stdout = (
                    f"{server.name}:\n"
                    "  Scope: Local config (private to you in this project)\n"
                    "  Status: Connected\n"
                    "  Type: stdio\n"
                    f"  Command: {server.command}\n"
                    f"  Args: {' '.join(server.args)}\n"
                    "  Environment:\n"
                )
            return subprocess.CompletedProcess(argv, 0, stdout, "")
        if operation == "add":
            separator = argv.index("--")
            name = argv[3] if client == "codex" else argv[5]
            server_argv = argv[separator + 1 :]
            self.configs[client] = MCPServerSpec(
                name,
                server_argv[0],
                tuple(server_argv[1:]),
            )
            return subprocess.CompletedProcess(argv, 0, "", "")
        if operation == "remove":
            self.configs.pop(client, None)
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(argv)


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_native_client_add_inspect_remove_contract(
    tmp_path: Path,
    client: str,
) -> None:
    repo = _repository(tmp_path, "repo with spaces")
    server = _server(repo)
    native = _NativeClientHarness()

    missing = inspect_client_registration(
        client,
        server,
        repo,
        runner=native.run,
        which=native.which,
    )
    add_client_registration(
        client,
        server,
        repo,
        runner=native.run,
        which=native.which,
    )
    configured = inspect_client_registration(
        client,
        server,
        repo,
        runner=native.run,
        which=native.which,
    )
    remove_client_registration(
        client,
        server,
        repo,
        runner=native.run,
        which=native.which,
    )

    assert missing.exists is False
    assert configured.matches is True
    assert client not in native.configs
    if client == "codex":
        assert any(
            command[1:4] == ("mcp", "get", "--json") for command in native.commands
        )
    else:
        add = next(command for command in native.commands if command[2] == "add")
        assert add[3:5] == ("--scope", "local")


def test_native_client_inspection_does_not_treat_cli_failure_as_absent(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path)

    def fail(command, **_kwargs):
        return subprocess.CompletedProcess(command, 2, "", "configuration is invalid")

    with pytest.raises(CodeGraphOnboardingError, match="cannot inspect codex"):
        inspect_client_registration(
            "codex",
            _server(repo),
            repo,
            runner=fail,
            which=lambda _value: "/clients/codex",
        )


def test_server_command_must_report_the_active_codenib_version(tmp_path: Path) -> None:
    from codenib._version import package_version

    repo = _repository(tmp_path)
    server = make_server_spec(
        repo,
        command="/python",
        command_prefix=("-m", "codenib"),
    )
    commands: list[tuple[str, ...]] = []

    def runner(command, **_kwargs):
        commands.append(tuple(command))
        output = (
            "codenib codegraph mcp runtime ready\n"
            if command[-1] == "--runtime-probe"
            else f"codenib {package_version()}\n"
        )
        return subprocess.CompletedProcess(
            command,
            0,
            output,
            "",
        )

    inspection = inspect_server_command(server, repo, runner=runner)

    assert inspection.ready is True
    assert commands == [
        ("/python", "-m", "codenib", "--version"),
        (
            "/python",
            "-m",
            "codenib",
            "mcp",
            str(repo.resolve()),
            "--tool-surface",
            "full",
            "--runtime-probe",
        ),
    ]

    mismatch = inspect_server_command(
        server,
        repo,
        runner=lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 0, "different tool\n", ""
        ),
    )
    assert mismatch.ready is False

    failed_probe = inspect_server_command(
        server,
        repo,
        runner=lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            0 if command[-1] == "--version" else 1,
            f"codenib {package_version()}\n" if command[-1] == "--version" else "",
            "missing graph runtime" if command[-1] == "--runtime-probe" else "",
        ),
    )
    assert failed_probe.ready is False
    assert "runtime probe failed" in failed_probe.detail


def _ready_plan(repo: Path) -> SimpleNamespace:
    return SimpleNamespace(
        repository=repo,
        root=repo / "tools",
        languages=("python",),
        requirements=(),
        missing=(),
        notes=(),
        ready=True,
    )


def test_init_is_idempotent_and_writes_no_repository_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import codenib.codegraph_onboarding as onboarding

    repo = _repository(tmp_path)
    _initialize_git_repository(repo)
    monkeypatch.setenv("CODENIB_HOME", str(tmp_path / "state"))
    configured: dict[str, MCPServerSpec] = {}
    additions: list[str] = []

    monkeypatch.setattr(
        onboarding,
        "resolve_requested_clients",
        lambda _requested: ("codex", "claude"),
    )
    monkeypatch.setattr(
        onboarding,
        "resolve_codenib_command",
        lambda _explicit=None: ("/opt/codenib/bin/codenib", ()),
    )
    monkeypatch.setattr(
        onboarding,
        "inspect_server_command",
        lambda *_args, **_kwargs: SimpleNamespace(ready=True, detail="ready"),
    )

    def inspect(client, server, _repo):
        current = configured.get(client)
        return ClientInspection(
            client,
            True,
            current is not None,
            current == server,
            "configuration matches" if current == server else "not configured",
        )

    def add(client, server, _repo):
        additions.append(client)
        configured[client] = server

    monkeypatch.setattr(onboarding, "inspect_client_registration", inspect)
    monkeypatch.setattr(onboarding, "add_client_registration", add)
    monkeypatch.setattr(
        cli, "_codegraph_toolchain_plan", lambda *_args: _ready_plan(repo)
    )
    monkeypatch.setattr(cli, "_require_modules", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli, "_check_view_dependencies", lambda *_args, **_kwargs: None)
    index_calls: list[dict[str, object]] = []

    def index_repository(*_args, **kwargs):
        index_calls.append(kwargs)
        return _manifest(repo), []

    monkeypatch.setattr(cli, "index_repository", index_repository)
    monkeypatch.setattr(cli, "_print_index_summary", lambda *_args: None)
    args = cli.build_parser().parse_args(["codegraph", "init", str(repo)])
    initial = sorted(path.relative_to(repo) for path in repo.rglob("*"))

    assert cli._run_codegraph_init(args) == 0
    assert cli._run_codegraph_init(args) == 0

    receipt = load_codegraph_receipt(repo)
    assert receipt is not None
    assert [(item.name, item.state) for item in receipt.clients] == [
        ("codex", "configured"),
        ("claude", "configured"),
    ]
    assert additions == ["codex", "claude"]
    assert all(call["allow_graph_project_preparation"] is False for call in index_calls)
    assert all(call["allow_partial_graph_languages"] is False for call in index_calls)
    assert sorted(path.relative_to(repo) for path in repo.rglob("*")) == initial
    assert "CodeGraph is ready" in capsys.readouterr().out


def test_init_rejects_dirty_checkout_before_toolchain_or_index_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import codenib.codegraph_onboarding as onboarding

    repo = _repository(tmp_path)
    _initialize_git_repository(repo)
    (repo / "untracked.py").write_text("value = 1\n", encoding="utf-8")
    monkeypatch.setattr(
        onboarding,
        "resolve_requested_clients",
        lambda _requested: ("codex",),
    )
    monkeypatch.setattr(
        onboarding,
        "resolve_codenib_command",
        lambda _explicit=None: ("/opt/codenib/bin/codenib", ()),
    )
    monkeypatch.setattr(
        onboarding,
        "inspect_server_command",
        lambda *_args, **_kwargs: SimpleNamespace(ready=True, detail="ready"),
    )
    monkeypatch.setattr(
        onboarding,
        "inspect_client_registration",
        lambda client, _server, _repo: ClientInspection(
            client, True, False, False, "not configured"
        ),
    )
    monkeypatch.setattr(
        cli,
        "_codegraph_toolchain_plan",
        lambda *_args: pytest.fail("dirty checkout must fail before toolchain work"),
    )
    args = cli.build_parser().parse_args(["codegraph", "init", str(repo)])

    with pytest.raises(cli.CLIError, match="clean Git checkout"):
        cli._run_codegraph_init(args)


def test_repeated_auto_init_redetects_graph_languages_from_current_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codenib.compiler.manifest import MANIFEST_FILENAME
    from codenib.paths import repo_index_dir

    repo = _repository(tmp_path)
    monkeypatch.setenv("CODENIB_HOME", str(tmp_path / "state"))
    manifest = _manifest(repo)
    manifest.languages = ["python"]
    manifest.save(repo_index_dir(repo) / MANIFEST_FILENAME)
    (repo / "new.go").write_text("package example\n", encoding="utf-8")
    args = cli.build_parser().parse_args(["codegraph", "init", str(repo)])

    assert cli._codegraph_languages_for_init(repo, args, object()) == (
        "go",
        "python",
    )


def test_codegraph_language_selection_ignores_chunk_only_languages(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path)
    (repo / "Example.swift").write_text("let value = 1\n", encoding="utf-8")
    (repo / "plugin.lua").write_text("return {}\n", encoding="utf-8")
    args = cli.build_parser().parse_args(["codegraph", "init", str(repo)])

    assert cli._codegraph_languages_for_init(repo, args, None) == ("python",)


def test_codegraph_language_selection_rejects_unsupported_explicit_language(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path)
    args = cli.build_parser().parse_args(
        ["codegraph", "init", str(repo), "--language", "swift"]
    )

    with pytest.raises(cli.CLIError, match="no symbol-graph provider"):
        cli._codegraph_languages_for_init(repo, args, None)


def test_dry_run_reports_project_prerequisites_and_exits_nonzero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import codenib.codegraph_onboarding as onboarding
    import codenib.toolchains as toolchains

    repo = _repository(tmp_path)
    _initialize_git_repository(repo)
    monkeypatch.setenv("CODENIB_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(
        onboarding,
        "resolve_requested_clients",
        lambda _requested: ("codex",),
    )
    monkeypatch.setattr(
        onboarding,
        "resolve_codenib_command",
        lambda _explicit=None: ("/opt/codenib/bin/codenib", ()),
    )
    monkeypatch.setattr(
        onboarding,
        "inspect_server_command",
        lambda *_args, **_kwargs: SimpleNamespace(ready=True, detail="ready"),
    )
    monkeypatch.setattr(
        onboarding,
        "inspect_client_registration",
        lambda client, _server, _repo: ClientInspection(
            client, True, False, False, "not configured"
        ),
    )
    plan = SimpleNamespace(
        root=tmp_path / "tools",
        missing=(),
        notes=(
            "MISSING: Go project prerequisite go.mod: missing from repository root",
        ),
        ready=False,
    )
    monkeypatch.setattr(cli, "_codegraph_toolchain_plan", lambda *_args: plan)
    monkeypatch.setattr(
        toolchains,
        "install_requirements",
        lambda *_args, **_kwargs: ([], []),
    )
    monkeypatch.setattr(
        cli,
        "index_repository",
        lambda *_args, **_kwargs: pytest.fail("dry-run must not build indexes"),
    )
    args = cli.build_parser().parse_args(
        [
            "codegraph",
            "init",
            str(repo),
            "--agent",
            "codex",
            "--language",
            "go",
            "--dry-run",
        ]
    )

    assert cli._run_codegraph_init(args) == 1
    output = capsys.readouterr().out
    assert "prerequisite: Go project prerequisite go.mod" in output
    assert "Readiness:  blocked" in output
    assert "no tools, indexes, receipts, or clients changed" in output


def test_init_does_not_overwrite_registration_created_during_indexing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import codenib.codegraph_onboarding as onboarding

    repo = _repository(tmp_path)
    _initialize_git_repository(repo)
    monkeypatch.setenv("CODENIB_HOME", str(tmp_path / "state"))
    inspections = 0

    monkeypatch.setattr(
        onboarding,
        "resolve_requested_clients",
        lambda _requested: ("codex",),
    )
    monkeypatch.setattr(
        onboarding,
        "resolve_codenib_command",
        lambda _explicit=None: ("/opt/codenib/bin/codenib", ()),
    )
    monkeypatch.setattr(
        onboarding,
        "inspect_server_command",
        lambda *_args, **_kwargs: SimpleNamespace(ready=True, detail="ready"),
    )

    def inspect(client, _server, _repo):
        nonlocal inspections
        inspections += 1
        return ClientInspection(
            client,
            True,
            inspections > 1,
            False,
            "configuration differs" if inspections > 1 else "not configured",
        )

    monkeypatch.setattr(onboarding, "inspect_client_registration", inspect)
    monkeypatch.setattr(
        onboarding,
        "add_client_registration",
        lambda *_args: pytest.fail("a raced registration must not be overwritten"),
    )
    monkeypatch.setattr(
        cli, "_codegraph_toolchain_plan", lambda *_args: _ready_plan(repo)
    )
    monkeypatch.setattr(cli, "_require_modules", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli, "_check_view_dependencies", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli,
        "index_repository",
        lambda *_args, **_kwargs: (_manifest(repo), []),
    )
    monkeypatch.setattr(cli, "_print_index_summary", lambda *_args: None)
    args = cli.build_parser().parse_args(
        ["codegraph", "init", str(repo), "--language", "python"]
    )

    with pytest.raises(cli.CLIError, match="changed during indexing"):
        cli._run_codegraph_init(args)


def test_init_rejects_unmanaged_name_collision_before_indexing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import codenib.codegraph_onboarding as onboarding

    repo = _repository(tmp_path)
    monkeypatch.setenv("CODENIB_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(
        onboarding,
        "resolve_requested_clients",
        lambda _requested: ("codex",),
    )
    monkeypatch.setattr(
        onboarding,
        "resolve_codenib_command",
        lambda _explicit=None: ("/opt/codenib/bin/codenib", ()),
    )
    monkeypatch.setattr(
        onboarding,
        "inspect_server_command",
        lambda *_args, **_kwargs: SimpleNamespace(ready=True, detail="ready"),
    )
    monkeypatch.setattr(
        onboarding,
        "inspect_client_registration",
        lambda client, _server, _repo: ClientInspection(
            client, True, True, True, "configuration matches"
        ),
    )
    monkeypatch.setattr(
        cli,
        "_codegraph_toolchain_plan",
        lambda *_args: pytest.fail("collision must fail before toolchain work"),
    )
    args = cli.build_parser().parse_args(["codegraph", "init", str(repo)])

    with pytest.raises(cli.CLIError, match="unmanaged MCP server"):
        cli._run_codegraph_init(args)


def test_uninstall_refuses_drift_unless_forced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import codenib.codegraph_onboarding as onboarding

    repo = _repository(tmp_path)
    monkeypatch.setenv("CODENIB_HOME", str(tmp_path / "state"))
    receipt = CodeGraphReceipt(repo, _server(repo), ()).with_client(
        "codex", state="configured"
    )
    write_codegraph_receipt(receipt)
    state = {"exists": True}
    removals: list[str] = []

    def inspect(client, _server, _repo):
        return ClientInspection(
            client,
            True,
            state["exists"],
            False,
            "configuration differs",
        )

    def remove(client, _server, _repo):
        removals.append(client)
        state["exists"] = False

    monkeypatch.setattr(onboarding, "inspect_client_registration", inspect)
    monkeypatch.setattr(onboarding, "remove_client_registration", remove)
    normal = cli.build_parser().parse_args(["codegraph", "uninstall", str(repo)])
    forced = cli.build_parser().parse_args(
        ["codegraph", "uninstall", str(repo), "--force"]
    )

    with pytest.raises(cli.CLIError, match="refusing to remove drifted"):
        cli._run_codegraph_uninstall(normal)
    assert codegraph_receipt_path(repo).is_file()

    assert cli._run_codegraph_uninstall(forced) == 0
    assert removals == ["codex"]
    assert not codegraph_receipt_path(repo).exists()


def test_status_json_is_nonzero_without_managed_clients(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _repository(tmp_path)
    monkeypatch.setenv("CODENIB_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(
        cli,
        "_codegraph_index_report",
        lambda _repo: {"ready": True, "detail": "current"},
    )
    monkeypatch.setattr(
        cli,
        "_codegraph_toolchain_report",
        lambda *_args: {
            "ready": True,
            "languages": ["python"],
            "missing": [],
            "notes": [],
        },
    )
    args = cli.build_parser().parse_args(["codegraph", "status", str(repo), "--json"])

    assert cli._run_codegraph_status(args) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["ready"] is False
    assert report["clients"] == []


def test_human_status_does_not_call_a_graph_only_index_current(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _repository(tmp_path)
    monkeypatch.setattr(
        cli,
        "_codegraph_status_report",
        lambda _repo: {
            "repository": str(repo),
            "ready": False,
            "index": {
                "ready": False,
                "detail": "bm25 artifact is missing or does not match its fingerprint",
            },
            "toolchain": {"ready": True, "missing": [], "notes": []},
            "server_command": {"ready": True, "detail": "ready"},
            "clients": [],
        },
    )
    args = cli.build_parser().parse_args(["codegraph", "status", str(repo)])

    assert cli._run_codegraph_status(args) == 1
    output = capsys.readouterr().out
    assert "bm25 artifact is missing" in output
    assert "bm25 + symbol_graph are current" not in output


@pytest.mark.parametrize(
    ("mutation", "expected_detail"),
    [
        ("remove-bm25", "bm25 artifact is missing"),
        ("corrupt-graph", "symbol_graph artifact is missing"),
    ],
)
def test_index_status_verifies_persisted_artifact_fingerprints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    expected_detail: str,
) -> None:
    from codenib.compiler import checkout_identity
    from codenib.compiler.artifact_fingerprints import (
        bm25_artifact_file_fingerprints,
        regular_file_fingerprint,
    )
    from codenib.compiler.manifest import MANIFEST_FILENAME
    from codenib.paths import repo_index_dir

    repo = _repository(tmp_path)
    state = tmp_path / "state"
    monkeypatch.setenv("CODENIB_HOME", str(state))
    bm25 = state / "artifacts" / "bm25"
    graph = state / "artifacts" / "symbol_graph"
    bm25.mkdir(parents=True)
    graph.mkdir(parents=True)
    (bm25 / "documents.json").write_text("[]\n", encoding="utf-8")
    (bm25 / "bm25_metadata.json").write_text("{}\n", encoding="utf-8")
    (graph / "graph.pkl").write_bytes(b"graph-generation-one")
    fingerprint = "sha256-v2:" + "b" * 64
    manifest = RepoManifest(
        repo_path=str(repo),
        source_fingerprint=fingerprint,
        last_indexed_source_fingerprint=fingerprint,
        languages=["python"],
        indexes={
            "bm25": IndexEntry(
                index_type="bm25",
                path=str(bm25),
                built_at="2026-08-13T00:00:00Z",
                built_at_epoch=1.0,
                status="fresh",
                config={
                    "artifact_file_fingerprints": (
                        bm25_artifact_file_fingerprints(bm25)
                    )
                },
                source_fingerprint=fingerprint,
            ),
            "symbol_graph": IndexEntry(
                index_type="symbol_graph",
                path=str(graph),
                built_at="2026-08-13T00:00:00Z",
                built_at_epoch=1.0,
                status="fresh",
                config={
                    "allow_partial_languages": False,
                    "allow_partial_index": False,
                    "graph_artifact": {
                        "relative_path": "graph.pkl",
                        **regular_file_fingerprint(graph / "graph.pkl"),
                    },
                },
                source_fingerprint=fingerprint,
            ),
        },
    )
    manifest.save(repo_index_dir(repo) / MANIFEST_FILENAME)
    monkeypatch.setattr(
        checkout_identity,
        "validate_checkout_identity",
        lambda *_args, **_kwargs: None,
    )

    assert cli._codegraph_index_report(repo)["ready"] is True
    if mutation == "remove-bm25":
        (bm25 / "documents.json").unlink()
    else:
        (graph / "graph.pkl").write_bytes(b"graph-generation-two")

    report = cli._codegraph_index_report(repo)
    assert report["ready"] is False
    assert expected_detail in report["detail"]
