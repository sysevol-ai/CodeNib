# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Managed MCP client registration for the CodeGraph product path.

The client applications own their configuration files.  CodeNib invokes their
public CLIs, records only the registration it created, and refuses to overwrite
or remove a registration whose observed command has drifted.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from ._version import package_version
from .paths import repo_state_dir, repository_state_key

CODEGRAPH_CLIENTS = ("codex", "claude")
CODEGRAPH_RECEIPT_SCHEMA = 1
CODEGRAPH_RECEIPT_DIRNAME = "codegraph"
CODEGRAPH_RECEIPT_FILENAME = "agent-integrations.json"

_CLIENT_SCOPES = {"codex": "user", "claude": "local"}
_CLIENT_STATES = frozenset({"pending", "configured"})
_SERVER_NAME_PART = re.compile(r"[^a-z0-9._-]+")


class CodeGraphOnboardingError(RuntimeError):
    """A safe, user-actionable onboarding failure."""


@dataclass(frozen=True, slots=True)
class MCPServerSpec:
    """One stdio server registration shared by supported agent clients."""

    name: str
    command: str
    args: tuple[str, ...]

    @property
    def argv(self) -> tuple[str, ...]:
        return (self.command, *self.args)


@dataclass(frozen=True, slots=True)
class ManagedClient:
    """One client registration retained in the CodeNib receipt."""

    name: str
    scope: str
    state: str


@dataclass(frozen=True, slots=True)
class CodeGraphReceipt:
    """CodeNib-owned evidence for safe idempotency and removal."""

    repository: Path
    server: MCPServerSpec
    clients: tuple[ManagedClient, ...]

    def client(self, name: str) -> ManagedClient | None:
        return next((item for item in self.clients if item.name == name), None)

    def with_client(self, name: str, *, state: str) -> "CodeGraphReceipt":
        if name not in CODEGRAPH_CLIENTS:
            raise CodeGraphOnboardingError(f"unsupported agent client: {name}")
        if state not in _CLIENT_STATES:
            raise CodeGraphOnboardingError(f"invalid client receipt state: {state}")
        retained = [item for item in self.clients if item.name != name]
        retained.append(ManagedClient(name, _CLIENT_SCOPES[name], state))
        retained.sort(key=lambda item: CODEGRAPH_CLIENTS.index(item.name))
        return CodeGraphReceipt(self.repository, self.server, tuple(retained))

    def without_client(self, name: str) -> "CodeGraphReceipt":
        return CodeGraphReceipt(
            self.repository,
            self.server,
            tuple(item for item in self.clients if item.name != name),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": CODEGRAPH_RECEIPT_SCHEMA,
            "repository": str(self.repository),
            "server": {
                "name": self.server.name,
                "command": self.server.command,
                "args": list(self.server.args),
            },
            "clients": {
                item.name: {"scope": item.scope, "state": item.state}
                for item in self.clients
            },
        }


@dataclass(frozen=True, slots=True)
class ClientInspection:
    """Observed state of one native client registration."""

    client: str
    installed: bool
    exists: bool
    matches: bool
    detail: str


@dataclass(frozen=True, slots=True)
class ServerCommandInspection:
    """Observed startup command identity for the managed MCP server."""

    ready: bool
    detail: str


RunCommand = Callable[..., subprocess.CompletedProcess[str]]
FindCommand = Callable[[str], str | None]


def codegraph_receipt_path(repository: str | Path) -> Path:
    """Return the per-checkout onboarding receipt path."""

    return (
        repo_state_dir(repository)
        / CODEGRAPH_RECEIPT_DIRNAME
        / CODEGRAPH_RECEIPT_FILENAME
    )


def codegraph_server_name(repository: str | Path) -> str:
    """Return a readable, bounded, collision-resistant MCP server name."""

    key = repository_state_key(repository)
    slug, separator, digest = key.rpartition("-")
    if not separator:
        slug, digest = "repository", key[-12:]
    slug = _SERVER_NAME_PART.sub("-", slug.lower()).strip("-._")[:32]
    return f"codenib-{slug or 'repository'}-{digest}"


def resolve_codenib_command(
    explicit: str | None = None,
    *,
    which: FindCommand = shutil.which,
) -> tuple[str, tuple[str, ...]]:
    """Resolve a durable command used by the agent to start CodeNib."""

    if explicit is not None:
        value = explicit.strip()
        if not value or any(character in value for character in "\x00\r\n"):
            raise CodeGraphOnboardingError(
                "--command must be a non-empty single-line executable"
            )
        resolved = which(value)
        if resolved is None:
            candidate = Path(value).expanduser()
            if not candidate.is_absolute():
                raise CodeGraphOnboardingError(
                    f"CodeNib command is not executable or on PATH: {value}"
                )
            resolved = str(candidate)
        if not os.path.isfile(resolved) or not os.access(resolved, os.X_OK):
            raise CodeGraphOnboardingError(
                f"CodeNib command is not executable: {resolved}"
            )
        return os.path.abspath(resolved), ()

    resolved = which("codenib")
    if resolved and os.path.isfile(resolved) and os.access(resolved, os.X_OK):
        return os.path.abspath(resolved), ()
    return os.path.abspath(sys.executable), ("-m", "codenib")


def make_server_spec(
    repository: str | Path,
    *,
    command: str,
    command_prefix: Sequence[str] = (),
) -> MCPServerSpec:
    """Build the exact full-surface MCP command for one checkout."""

    repo = Path(repository).expanduser().resolve()
    if any(character in str(repo) for character in "\x00\r\n"):
        raise CodeGraphOnboardingError(
            "CodeGraph repository paths must not contain control line breaks"
        )
    return MCPServerSpec(
        name=codegraph_server_name(repo),
        command=command,
        args=tuple(command_prefix) + ("mcp", str(repo), "--tool-surface", "full"),
    )


def resolve_requested_clients(
    requested: Sequence[str] | None,
    *,
    which: FindCommand = shutil.which,
) -> tuple[str, ...]:
    """Resolve explicit or auto-detected clients in stable order."""

    values = tuple(requested or ("auto",))
    unknown = sorted(set(values) - {"auto", *CODEGRAPH_CLIENTS})
    if unknown:
        raise CodeGraphOnboardingError(
            f"unsupported agent client: {', '.join(unknown)}"
        )
    if "auto" in values and len(values) != 1:
        raise CodeGraphOnboardingError(
            "--agent auto cannot be combined with an explicit client"
        )
    selected = (
        tuple(client for client in CODEGRAPH_CLIENTS if which(client) is not None)
        if values == ("auto",)
        else tuple(client for client in CODEGRAPH_CLIENTS if client in values)
    )
    if not selected:
        raise CodeGraphOnboardingError(
            "no supported agent client was found; install Codex or Claude Code, "
            "or pass --agent after its CLI is available"
        )
    missing = [client for client in selected if which(client) is None]
    if missing:
        raise CodeGraphOnboardingError(
            f"agent client command not found: {', '.join(missing)}"
        )
    return selected


def _strict_string(value: object, *, field: str) -> str:
    if (
        type(value) is not str
        or not value
        or any(character in value for character in "\x00\r\n")
    ):
        raise CodeGraphOnboardingError(
            f"invalid CodeGraph integration receipt field: {field}"
        )
    return value


def _strict_keys(
    value: object,
    expected: set[str],
    *,
    field: str,
) -> Mapping[str, object]:
    if type(value) is not dict or set(value) != expected:
        raise CodeGraphOnboardingError(
            f"invalid CodeGraph integration receipt object: {field}"
        )
    return value


def load_codegraph_receipt(repository: str | Path) -> CodeGraphReceipt | None:
    """Load and strictly validate the per-checkout receipt, if present."""

    repo = Path(repository).expanduser().resolve()
    path = codegraph_receipt_path(repo)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CodeGraphOnboardingError(
            f"cannot read CodeGraph integration receipt {path}: {exc}"
        ) from exc

    root = _strict_keys(
        payload,
        {"schema_version", "repository", "server", "clients"},
        field="root",
    )
    if type(root["schema_version"]) is not int or root["schema_version"] != 1:
        raise CodeGraphOnboardingError(
            "unsupported CodeGraph integration receipt schema"
        )
    recorded_repo = Path(
        _strict_string(root["repository"], field="repository")
    ).expanduser()
    if not recorded_repo.is_absolute() or recorded_repo != repo:
        raise CodeGraphOnboardingError(
            "CodeGraph integration receipt repository does not match this checkout"
        )

    server_value = _strict_keys(
        root["server"], {"name", "command", "args"}, field="server"
    )
    raw_args = server_value["args"]
    if (
        type(raw_args) is not list
        or not raw_args
        or not all(
            type(item) is str
            and item
            and not any(character in item for character in "\x00\r\n")
            for item in raw_args
        )
    ):
        raise CodeGraphOnboardingError(
            "invalid CodeGraph integration receipt field: server.args"
        )
    server = MCPServerSpec(
        _strict_string(server_value["name"], field="server.name"),
        _strict_string(server_value["command"], field="server.command"),
        tuple(raw_args),
    )
    if not os.path.isabs(server.command):
        raise CodeGraphOnboardingError(
            "CodeGraph integration receipt has a non-absolute MCP command"
        )
    if server.name != codegraph_server_name(repo):
        raise CodeGraphOnboardingError(
            "CodeGraph integration receipt has an unexpected server name"
        )
    suffix = ("mcp", str(repo), "--tool-surface", "full")
    prefix = server.args[: -len(suffix)]
    if server.args[-len(suffix) :] != suffix or prefix not in ((), ("-m", "codenib")):
        raise CodeGraphOnboardingError(
            "CodeGraph integration receipt has an unexpected MCP command"
        )

    clients_value = root["clients"]
    if type(clients_value) is not dict or not set(clients_value).issubset(
        CODEGRAPH_CLIENTS
    ):
        raise CodeGraphOnboardingError(
            "invalid CodeGraph integration receipt object: clients"
        )
    clients: list[ManagedClient] = []
    for name in CODEGRAPH_CLIENTS:
        if name not in clients_value:
            continue
        item = _strict_keys(
            clients_value[name], {"scope", "state"}, field=f"clients.{name}"
        )
        scope = _strict_string(item["scope"], field=f"clients.{name}.scope")
        state = _strict_string(item["state"], field=f"clients.{name}.state")
        if scope != _CLIENT_SCOPES[name] or state not in _CLIENT_STATES:
            raise CodeGraphOnboardingError(
                f"invalid CodeGraph integration receipt client: {name}"
            )
        clients.append(ManagedClient(name, scope, state))
    return CodeGraphReceipt(repo, server, tuple(clients))


def write_codegraph_receipt(receipt: CodeGraphReceipt) -> Path:
    """Atomically write a private, deterministic integration receipt."""

    path = codegraph_receipt_path(receipt.repository)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = (
        json.dumps(receipt.to_dict(), ensure_ascii=True, indent=2, sort_keys=True)
        + "\n"
    )
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            fchmod = getattr(os, "fchmod", None)
            if fchmod is not None:
                fchmod(handle.fileno(), 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return path


def remove_codegraph_receipt(receipt: CodeGraphReceipt) -> None:
    """Remove the receipt after every managed client has been reconciled."""

    codegraph_receipt_path(receipt.repository).unlink(missing_ok=True)


def _invoke_client(
    command: Sequence[str],
    *,
    repository: Path,
    runner: RunCommand,
) -> subprocess.CompletedProcess[str]:
    try:
        return runner(
            tuple(command),
            cwd=repository,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CodeGraphOnboardingError(
            f"command failed to start: {command[0]}: {exc}"
        ) from exc


def inspect_server_command(
    server: MCPServerSpec,
    repository: str | Path,
    *,
    runner: RunCommand = subprocess.run,
) -> ServerCommandInspection:
    """Verify that the recorded executable resolves this CodeNib runtime."""

    if not os.path.isabs(server.command):
        raise CodeGraphOnboardingError("managed MCP server command is not absolute")
    mcp_suffix = (
        "mcp",
        str(Path(repository).expanduser().resolve()),
        "--tool-surface",
        "full",
    )
    if server.args[-len(mcp_suffix) :] != mcp_suffix:
        raise CodeGraphOnboardingError("managed MCP server command is malformed")
    prefix = server.args[: -len(mcp_suffix)]
    result = _invoke_client(
        (server.command, *prefix, "--version"),
        repository=Path(repository).expanduser().resolve(),
        runner=runner,
    )
    raw_output = result.stdout or result.stderr or ""
    observed = " ".join((raw_output or "no output").split())
    expected = f"codenib {package_version()}"
    version_ready = result.returncode == 0 and expected in {
        line.strip() for line in raw_output.splitlines()
    }
    if not version_ready:
        return ServerCommandInspection(
            False,
            f"expected {expected!r}, observed {observed[:240]!r}",
        )
    probe = _invoke_client(
        (server.command, *prefix, "mcp", "--runtime-probe"),
        repository=Path(repository).expanduser().resolve(),
        runner=runner,
    )
    probe_output = probe.stdout or probe.stderr or ""
    probe_marker = "codenib codegraph mcp runtime ready"
    ready = probe.returncode == 0 and probe_marker in {
        line.strip() for line in probe_output.splitlines()
    }
    return ServerCommandInspection(
        ready,
        (
            expected
            if ready
            else "CodeGraph MCP runtime probe failed: "
            + " ".join((probe_output or "no output").split())[:240]
        ),
    )


def _codex_matches(output: str, server: MCPServerSpec) -> bool:
    try:
        value = json.loads(output)
    except json.JSONDecodeError:
        return False
    if type(value) is not dict:
        return False
    transport = value.get("transport")
    return bool(
        value.get("name") == server.name
        and value.get("enabled") is True
        and type(transport) is dict
        and transport.get("type") == "stdio"
        and transport.get("command") == server.command
        and transport.get("args") == list(server.args)
        and transport.get("cwd") is None
        and transport.get("env") in (None, {})
    )


def _claude_fields(output: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in output.splitlines():
        if not line.startswith("  ") or ":" not in line:
            continue
        key, value = line.strip().split(":", 1)
        if key and key not in fields:
            fields[key] = value.strip()
    return fields


def _claude_matches(output: str, server: MCPServerSpec) -> bool:
    fields = _claude_fields(output)
    return bool(
        fields.get("Scope", "").startswith("Local config")
        and fields.get("Type") == "stdio"
        and fields.get("Command") == server.command
        and fields.get("Args") == " ".join(server.args)
    )


def inspect_client_registration(
    client: str,
    server: MCPServerSpec,
    repository: str | Path,
    *,
    runner: RunCommand = subprocess.run,
    which: FindCommand = shutil.which,
) -> ClientInspection:
    """Inspect one registration using the owning client's public CLI."""

    if client not in CODEGRAPH_CLIENTS:
        raise CodeGraphOnboardingError(f"unsupported agent client: {client}")
    executable = which(client)
    if executable is None:
        return ClientInspection(client, False, False, False, "command not found")
    command = (
        (executable, "mcp", "get", "--json", server.name)
        if client == "codex"
        else (executable, "mcp", "get", server.name)
    )
    result = _invoke_client(
        command,
        repository=Path(repository).expanduser().resolve(),
        runner=runner,
    )
    if result.returncode != 0:
        detail = " ".join((result.stderr or result.stdout or "not configured").split())
        missing_markers = ("no mcp server named", "not found")
        if not any(marker in detail.lower() for marker in missing_markers):
            raise CodeGraphOnboardingError(
                f"cannot inspect {client} MCP configuration: {detail[:400]}"
            )
        return ClientInspection(client, True, False, False, detail[:240])
    matches = (
        _codex_matches(result.stdout, server)
        if client == "codex"
        else _claude_matches(result.stdout, server)
    )
    return ClientInspection(
        client,
        True,
        True,
        matches,
        "configuration matches" if matches else "configuration differs",
    )


def add_client_registration(
    client: str,
    server: MCPServerSpec,
    repository: str | Path,
    *,
    runner: RunCommand = subprocess.run,
    which: FindCommand = shutil.which,
) -> None:
    """Ask a native client to add the exact stdio registration."""

    executable = which(client)
    if executable is None:
        raise CodeGraphOnboardingError(f"agent client command not found: {client}")
    if client == "codex":
        command = (executable, "mcp", "add", server.name, "--", *server.argv)
    elif client == "claude":
        command = (
            executable,
            "mcp",
            "add",
            "--scope",
            "local",
            server.name,
            "--",
            *server.argv,
        )
    else:
        raise CodeGraphOnboardingError(f"unsupported agent client: {client}")
    result = _invoke_client(
        command,
        repository=Path(repository).expanduser().resolve(),
        runner=runner,
    )
    if result.returncode != 0:
        detail = " ".join((result.stderr or result.stdout or "unknown error").split())
        raise CodeGraphOnboardingError(
            f"{client} rejected the MCP registration: {detail[:400]}"
        )


def remove_client_registration(
    client: str,
    server: MCPServerSpec,
    repository: str | Path,
    *,
    runner: RunCommand = subprocess.run,
    which: FindCommand = shutil.which,
) -> None:
    """Ask a native client to remove one previously verified registration."""

    executable = which(client)
    if executable is None:
        raise CodeGraphOnboardingError(f"agent client command not found: {client}")
    command = (
        (executable, "mcp", "remove", server.name)
        if client == "codex"
        else (executable, "mcp", "remove", server.name, "--scope", "local")
    )
    result = _invoke_client(
        command,
        repository=Path(repository).expanduser().resolve(),
        runner=runner,
    )
    if result.returncode != 0:
        detail = " ".join((result.stderr or result.stdout or "unknown error").split())
        raise CodeGraphOnboardingError(f"{client} rejected MCP removal: {detail[:400]}")


__all__ = [
    "CODEGRAPH_CLIENTS",
    "ClientInspection",
    "CodeGraphOnboardingError",
    "CodeGraphReceipt",
    "MCPServerSpec",
    "ServerCommandInspection",
    "add_client_registration",
    "codegraph_receipt_path",
    "codegraph_server_name",
    "inspect_client_registration",
    "inspect_server_command",
    "load_codegraph_receipt",
    "make_server_spec",
    "remove_client_registration",
    "remove_codegraph_receipt",
    "resolve_codenib_command",
    "resolve_requested_clients",
    "write_codegraph_receipt",
]
