#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Exercise installed graph setup, indexing, and Dependency Map serving."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
import urllib.parse
from contextlib import nullcontext
from pathlib import Path
from typing import Sequence

from smoke_release_services import (
    _free_port,
    _run,
    _stop_process_group,
    _structured_result,
    _wait_for_json,
)


def _fixture_repository(root: Path) -> Path:
    repo = root / "graph-smoke-repository"
    repo.mkdir()
    (repo / "calculator.py").write_text(
        "def release_sum(left: int, right: int) -> int:\n"
        '    """Return the release verification sum."""\n'
        "    return left + right\n\n\n"
        "def release_workflow() -> int:\n"
        '    """Exercise a source-linked call edge."""\n'
        "    return release_sum(20, 22)\n",
        encoding="utf-8",
    )
    (repo / "README.md").write_text(
        "# Graph smoke repository\n\n"
        "`release_workflow` delegates arithmetic to `release_sum`.\n",
        encoding="utf-8",
    )
    _run(["git", "init", "--quiet"], cwd=repo)
    _run(["git", "config", "user.email", "release-smoke@example.invalid"], cwd=repo)
    _run(["git", "config", "user.name", "CodeNib Release Smoke"], cwd=repo)
    _run(["git", "add", "."], cwd=repo)
    _run(["git", "commit", "--quiet", "-m", "initial fixture"], cwd=repo)
    return repo


def _assert_codemap(
    root: Path,
    repo: Path,
    *,
    executable: str,
    env: dict[str, str],
) -> None:
    frontend_port = _free_port()
    api_port = _free_port()
    log_path = root / "graph-wiki.log"
    command = [
        executable,
        "wiki",
        str(repo),
        "--no-index",
        "--no-open",
        "--port",
        str(frontend_port),
        "--api-port",
        str(api_port),
    ]
    print("+", " ".join(command), flush=True)

    with log_path.open("w", encoding="utf-8") as service_log:
        process = subprocess.Popen(
            command,
            cwd=root,
            env=env,
            stdout=service_log,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        try:
            api_base = f"http://127.0.0.1:{api_port}"
            repos = _wait_for_json(
                f"{api_base}/api/repos",
                process=process,
                timeout=120,
            )
            if len(repos) != 1:
                raise RuntimeError(f"unexpected repository list: {repos!r}")
            info = repos[0]
            if not info.get("capabilities", {}).get("codemap"):
                raise RuntimeError(f"Dependency Map capability is absent: {info!r}")

            repo_id = urllib.parse.quote(info["id"])
            query = urllib.parse.urlencode(
                {
                    "symbol": "release_workflow",
                    "direction": "callees",
                    "depth": 1,
                    "max_nodes": 12,
                }
            )
            codemap = _wait_for_json(
                f"{api_base}/api/repos/{repo_id}/codemap?{query}",
                process=process,
                timeout=60,
            )
            if not codemap.get("available"):
                raise RuntimeError(f"Dependency Map is unavailable: {codemap!r}")

            names = {node.get("name", "") for node in codemap.get("nodes", [])}
            if not any("release_workflow" in name for name in names):
                raise RuntimeError(f"caller symbol is missing: {codemap!r}")
            if not any("release_sum" in name for name in names):
                raise RuntimeError(f"callee symbol is missing: {codemap!r}")

            edges = codemap.get("edges") or []
            if not edges:
                raise RuntimeError(f"Dependency Map has no call edge: {codemap!r}")
            anchors = [
                anchor
                for edge in edges
                for anchor in edge.get("anchors") or []
                if anchor.get("file") == "calculator.py"
                and isinstance(anchor.get("line"), int)
            ]
            if not anchors:
                raise RuntimeError(
                    f"Dependency Map edge has no source anchor: {edges!r}"
                )
        except Exception:
            service_log.flush()
            details = log_path.read_text(encoding="utf-8", errors="replace")
            print(f"\n--- Graph Wiki log ---\n{details}", file=sys.stderr)
            raise
        finally:
            _stop_process_group(process)


async def _assert_codegraph_mcp_async(
    root: Path,
    repo: Path,
    *,
    executable: str,
    env: dict[str, str],
) -> None:
    from mcp import Client, StdioServerParameters
    from mcp.client.stdio import stdio_client

    stderr_path = root / "codegraph-mcp.log"
    parameters = StdioServerParameters(
        command=executable,
        args=["mcp", str(repo), "--tool-surface", "full"],
        cwd=root,
        env=env,
    )
    try:
        with stderr_path.open("w+", encoding="utf-8") as server_stderr:
            transport = stdio_client(parameters, errlog=server_stderr)
            async with Client(transport, mode="auto", cache=None) as client:
                tools = await client.list_tools()
                names = {tool.name for tool in tools.tools}
                required = {"explore_context", "dependency_subgraph"}
                if not required.issubset(names):
                    raise RuntimeError(
                        f"CodeGraph MCP tools are missing: {sorted(required - names)}"
                    )

                dependency = await client.call_tool(
                    "dependency_subgraph",
                    arguments={
                        "symbol": "release_workflow",
                        "direction": "dependencies",
                        "depth": 1,
                        "max_nodes": 12,
                        "max_edges": 24,
                    },
                )
                dependency_payload = _structured_result(dependency)
                dependency_result = dependency_payload.get("result", dependency_payload)
                node_names = {
                    str(node.get("name") or "")
                    for node in dependency_result.get("nodes", [])
                }
                if not any("release_sum" in name for name in node_names):
                    raise RuntimeError(
                        "CodeGraph MCP dependency traversal did not reach the "
                        f"callee: {dependency_result!r}"
                    )
                if not dependency_result.get("edges"):
                    raise RuntimeError(
                        f"CodeGraph MCP dependency traversal has no edge: "
                        f"{dependency_result!r}"
                    )

                explored = await client.call_tool(
                    "explore_context",
                    arguments={
                        "query": "How does release_workflow calculate its result?",
                        "symbols": ["release_workflow"],
                        "top_k": 4,
                        "budget": "fast",
                        "direction": "dependencies",
                    },
                )
                explore_payload = _structured_result(explored)
                explore_result = explore_payload.get("result", explore_payload)
                files = explore_result.get("files") or []
                calculator = next(
                    (item for item in files if item.get("file") == "calculator.py"),
                    None,
                )
                if calculator is None:
                    raise RuntimeError(
                        f"CodeGraph MCP explore returned no calculator source: "
                        f"{explore_result!r}"
                    )
                contexts = calculator.get("contexts") or []
                if not contexts or not all(
                    context.get("verified") is True for context in contexts
                ):
                    raise RuntimeError(
                        "CodeGraph MCP explore did not return verified source: "
                        f"{calculator!r}"
                    )
                if explore_result.get("summary", {}).get("dependency_graphs", 0) < 1:
                    raise RuntimeError(
                        "CodeGraph MCP explore did not compose graph relationships: "
                        f"{explore_result!r}"
                    )
    except Exception:
        details = stderr_path.read_text(encoding="utf-8", errors="replace")
        print(f"\n--- CodeGraph MCP log ---\n{details}", file=sys.stderr)
        raise


def _assert_codegraph_mcp(
    root: Path,
    repo: Path,
    *,
    executable: str,
    env: dict[str, str],
) -> None:
    print("+", executable, "mcp", str(repo), "--tool-surface", "full", flush=True)
    asyncio.run(
        _assert_codegraph_mcp_async(
            root,
            repo,
            executable=executable,
            env=env,
        )
    )


_FAKE_AGENT_CLIENT = r"""#!/usr/bin/env python3
import json
import os
import pathlib
import sys

client = pathlib.Path(sys.argv[0]).name
state_path = pathlib.Path(os.environ["CODENIB_FAKE_AGENT_STATE"])
state = json.loads(state_path.read_text()) if state_path.exists() else {}
clients = state.setdefault("clients", {})
adds = state.setdefault("adds", {})
args = sys.argv[1:]
if args[:2] == ["mcp", "get"]:
    name = args[-1]
    server = clients.get(client)
    if not server or server["name"] != name:
        print("not found", file=sys.stderr)
        raise SystemExit(1)
    if client == "codex":
        print(json.dumps({
            "name": name,
            "enabled": True,
            "transport": {
                "type": "stdio",
                "command": server["command"],
                "args": server["args"],
                "cwd": None,
                "env": None,
            },
        }))
    else:
        print(f"{name}:")
        print("  Scope: Local config (private to you in this project)")
        print("  Status: Connected")
        print("  Type: stdio")
        print(f"  Command: {server['command']}")
        print(f"  Args: {' '.join(server['args'])}")
        print("  Environment:")
elif args[:2] == ["mcp", "add"]:
    separator = args.index("--")
    name = args[2] if client == "codex" else args[4]
    command = args[separator + 1:]
    clients[client] = {"name": name, "command": command[0], "args": command[1:]}
    adds[client] = adds.get(client, 0) + 1
    state_path.write_text(json.dumps(state, sort_keys=True))
elif args[:2] == ["mcp", "remove"]:
    clients.pop(client, None)
    state_path.write_text(json.dumps(state, sort_keys=True))
else:
    print(f"unsupported fake {client} command: {args!r}", file=sys.stderr)
    raise SystemExit(2)
"""


def _fake_agent_clients(root: Path, env: dict[str, str]) -> Path:
    bin_dir = root / "agent-clients"
    bin_dir.mkdir()
    for name in ("codex", "claude"):
        path = bin_dir / name
        path.write_text(_FAKE_AGENT_CLIENT, encoding="utf-8")
        path.chmod(0o755)
    state_path = root / "agent-client-state.json"
    env["CODENIB_FAKE_AGENT_STATE"] = str(state_path)
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    return state_path


def smoke(root: Path, *, executable: str = "codenib") -> None:
    root.mkdir(parents=True, exist_ok=True)
    repo = _fixture_repository(root)
    env = os.environ.copy()
    env["CODENIB_HOME"] = str(root / "user-state")
    agent_state = _fake_agent_clients(root, env)

    doctor = _run(
        [
            executable,
            "doctor",
            str(repo),
            "--require",
            "graph",
            "--language",
            "python",
        ],
        cwd=root,
        env=env,
    )
    if "Python (python): scip" not in doctor.stdout:
        raise RuntimeError(
            "repository-aware graph provider was not reported:\n" + doctor.stdout
        )

    _run(
        [
            executable,
            "codegraph",
            "init",
            str(repo),
            "--language",
            "python",
            "--command",
            executable,
        ],
        cwd=root,
        env=env,
    )
    report = json.loads(
        _run(
            [executable, "codegraph", "status", str(repo), "--json"],
            cwd=root,
            env=env,
        ).stdout
    )
    if report.get("ready") is not True:
        raise RuntimeError(f"CodeGraph onboarding status is not ready: {report!r}")
    if {client.get("name") for client in report.get("clients", [])} != {
        "codex",
        "claude",
    }:
        raise RuntimeError(f"CodeGraph clients are incomplete: {report!r}")

    first_state = json.loads(agent_state.read_text(encoding="utf-8"))
    _run(
        [
            executable,
            "codegraph",
            "init",
            str(repo),
            "--language",
            "python",
        ],
        cwd=root,
        env=env,
    )
    second_state = json.loads(agent_state.read_text(encoding="utf-8"))
    if second_state.get("adds") != first_state.get("adds"):
        raise RuntimeError("idempotent CodeGraph init rewrote an agent registration")

    status = _run(["git", "status", "--porcelain"], cwd=repo, env=env).stdout
    if status:
        raise RuntimeError(f"graph indexing modified the repository: {status!r}")

    _assert_codegraph_mcp(root, repo, executable=executable, env=env)
    _assert_codemap(root, repo, executable=executable, env=env)
    _run(
        [executable, "codegraph", "uninstall", str(repo)],
        cwd=root,
        env=env,
    )
    removed = json.loads(agent_state.read_text(encoding="utf-8"))
    if removed.get("clients"):
        raise RuntimeError(f"CodeGraph uninstall left client state: {removed!r}")
    print("Installed CodeGraph, agent onboarding, MCP, and Dependency Map smoke passed")


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--executable", default="codenib")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    executable = shutil.which(args.executable)
    if executable is None:
        print(
            f"release graph smoke failed: command not found: {args.executable}",
            file=sys.stderr,
        )
        return 1

    context = (
        nullcontext(args.root.expanduser().resolve())
        if args.root is not None
        else tempfile.TemporaryDirectory(prefix="codenib-release-graph-")
    )
    try:
        with context as value:
            smoke(Path(value), executable=executable)
    except Exception as exc:
        print(f"release graph smoke failed: {exc}", file=sys.stderr)
        traceback.print_exception(exc)
        if isinstance(exc, subprocess.CalledProcessError):
            if exc.stdout:
                print(exc.stdout, file=sys.stderr)
            if exc.stderr:
                print(exc.stderr, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
