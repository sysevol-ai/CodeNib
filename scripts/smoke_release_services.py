#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Exercise the installed CodeNib Wiki and MCP services from a clean directory."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Sequence


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(command), flush=True)
    return subprocess.run(
        list(command),
        cwd=cwd,
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )


def _fixture_repository(root: Path) -> Path:
    repo = root / "service-smoke-repository"
    repo.mkdir()
    (repo / "calculator.py").write_text(
        "def release_signature(left: int, right: int) -> int:\n"
        '    """Return a deterministic value for release verification."""\n'
        "    return left + right\n",
        encoding="utf-8",
    )
    (repo / "README.md").write_text(
        "# Service smoke repository\n",
        encoding="utf-8",
    )

    _run(["git", "init", "--quiet"], cwd=repo)
    _run(["git", "config", "user.email", "release-smoke@example.invalid"], cwd=repo)
    _run(["git", "config", "user.name", "CodeNib Release Smoke"], cwd=repo)
    _run(["git", "add", "."], cwd=repo)
    _run(["git", "commit", "--quiet", "-m", "initial fixture"], cwd=repo)
    return repo


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _request(url: str, *, timeout: float = 5.0) -> tuple[int, bytes]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.status, response.read()


def _wait_for_json(url: str, *, process: subprocess.Popen[Any], timeout: float) -> Any:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"service exited before {url} became ready "
                f"(status {process.returncode})"
            )
        try:
            status, body = _request(url)
            if status < 500:
                return json.loads(body)
        except (OSError, ValueError, urllib.error.URLError) as exc:
            last_error = exc
        time.sleep(0.5)
    raise RuntimeError(f"timed out waiting for {url}: {last_error}")


def _wait_for_page(url: str, *, process: subprocess.Popen[Any], timeout: float) -> str:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"service exited before {url} became ready "
                f"(status {process.returncode})"
            )
        try:
            status, body = _request(url)
            if status < 500 and body:
                return body.decode("utf-8", errors="replace")
        except (OSError, urllib.error.URLError) as exc:
            last_error = exc
        time.sleep(0.5)
    raise RuntimeError(f"timed out waiting for {url}: {last_error}")


def _stop_process_group(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    if os.name == "posix":
        os.killpg(process.pid, signal.SIGINT)
    else:  # pragma: no cover - release CI currently runs on Linux
        process.terminate()
    try:
        process.wait(timeout=15)
        return
    except subprocess.TimeoutExpired:
        pass

    if os.name == "posix":
        os.killpg(process.pid, signal.SIGTERM)
    else:  # pragma: no cover
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:  # pragma: no cover
            process.kill()
        process.wait(timeout=5)


def _assert_wiki(
    root: Path,
    repo: Path,
    *,
    executable: str,
    env: dict[str, str],
) -> None:
    """Prove that one Wiki command indexes and serves a fresh repository."""

    frontend_port = _free_port()
    api_port = _free_port()
    log_path = root / "wiki-service.log"
    command = [
        executable,
        "wiki",
        str(repo),
        "--no-open",
        "--host",
        "0.0.0.0",
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
            frontend_base = f"http://127.0.0.1:{frontend_port}"
            runtime_config = _wait_for_page(
                f"{frontend_base}/runtime-config.js",
                process=process,
                timeout=240,
            )
            if 'window.__CODENIB_API_BASE__ = "";' not in runtime_config:
                raise RuntimeError(
                    "packaged Wiki did not retain the same-origin API contract: "
                    f"{runtime_config!r}"
                )

            health = _wait_for_json(
                f"{frontend_base}/api/health",
                process=process,
                timeout=30,
            )
            if health != {"status": "ok", "repos": 1}:
                raise RuntimeError(f"unexpected Wiki health response: {health!r}")

            repos = _wait_for_json(
                f"{frontend_base}/api/repos",
                process=process,
                timeout=30,
            )
            if len(repos) != 1:
                raise RuntimeError(f"unexpected Wiki repository list: {repos!r}")
            repo_id = repos[0]["id"]
            if not repos[0].get("capabilities", {}).get("sparse_search"):
                raise RuntimeError(f"Wiki did not expose sparse search: {repos[0]!r}")

            tree = _wait_for_json(
                f"{frontend_base}/api/repos/{urllib.parse.quote(repo_id)}/wiki",
                process=process,
                timeout=30,
            )
            page_ids = [page.get("id") for page in tree.get("pages", [])]
            if "overview" not in page_ids:
                raise RuntimeError(f"Wiki tree has no overview page: {tree!r}")

            page = _wait_for_json(
                f"{frontend_base}/api/repos/"
                f"{urllib.parse.quote(repo_id)}/wiki/overview",
                process=process,
                timeout=30,
            )
            if "service-smoke-repository" not in page.get("markdown", "").lower():
                raise RuntimeError(
                    f"Wiki overview is not repository-specific: {page!r}"
                )

            source_url = (
                f"{frontend_base}/api/repos/{urllib.parse.quote(repo_id)}/source?"
                + urllib.parse.urlencode({"file": "calculator.py"})
            )
            source = _wait_for_json(source_url, process=process, timeout=30)
            if "release_signature" not in source.get("content", ""):
                raise RuntimeError(
                    f"Wiki source link returned wrong content: {source!r}"
                )

            frontend = _wait_for_page(
                f"{frontend_base}/{urllib.parse.quote(repo_id)}",
                process=process,
                timeout=30,
            )
            if "CodeNib" not in frontend:
                raise RuntimeError("Wiki frontend response did not identify CodeNib")
        except Exception:
            service_log.flush()
            details = log_path.read_text(encoding="utf-8", errors="replace")
            print(f"\n--- Wiki service log ---\n{details}", file=sys.stderr)
            raise
        finally:
            _stop_process_group(process)


class _ProtocolErrorHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def _structured_result(result: Any) -> dict[str, Any]:
    if getattr(result, "is_error", False):
        raise RuntimeError(f"MCP tool returned a protocol error: {result!r}")
    payload = getattr(result, "structured_content", None)
    if not isinstance(payload, dict):
        raise RuntimeError(f"MCP tool did not return structured content: {result!r}")
    return payload


async def _assert_mcp_async(
    root: Path,
    *,
    executable: str,
    env: dict[str, str],
    arguments: Sequence[str],
) -> None:
    from mcp import Client, StdioServerParameters
    from mcp.client.stdio import stdio_client

    protocol_errors = _ProtocolErrorHandler()
    protocol_logger = logging.getLogger("mcp.client.stdio")
    protocol_logger.addHandler(protocol_errors)
    stderr_path = root / "mcp-service.log"

    parameters = StdioServerParameters(
        command=executable,
        args=list(arguments),
        cwd=root,
        env=env,
    )
    try:
        try:
            with stderr_path.open("w+", encoding="utf-8") as server_stderr:
                for mode in ("auto", "legacy"):
                    transport = stdio_client(parameters, errlog=server_stderr)
                    async with Client(transport, mode=mode, cache=None) as client:
                        tools = await client.list_tools()
                        names = {tool.name for tool in tools.tools}
                        required = {
                            "get_manifest",
                            "read_source",
                            "search_context",
                            "search_bm25",
                            "search_semantic",
                        }
                        if not required.issubset(names):
                            raise RuntimeError(
                                f"MCP tools are missing in {mode} mode: "
                                f"{sorted(required - names)}"
                            )

                        tool_by_name = {tool.name: tool for tool in tools.tools}
                        bm25_schema = tool_by_name["search_bm25"].input_schema
                        top_k_schema = bm25_schema.get("properties", {}).get(
                            "top_k",
                            {},
                        )
                        if (
                            top_k_schema.get("minimum") != 1
                            or top_k_schema.get("maximum") != 100
                        ):
                            raise RuntimeError(
                                "installed MCP service did not publish bounded "
                                f"search inputs: {bm25_schema!r}"
                            )
                        source_schema = tool_by_name["read_source"].input_schema
                        source_path_schema = source_schema.get("properties", {}).get(
                            "file_path",
                            {},
                        )
                        if source_path_schema.get("maxLength") != 4096:
                            raise RuntimeError(
                                "installed MCP service did not publish a bounded "
                                f"source path: {source_schema!r}"
                            )

                        if mode == "legacy":
                            continue

                        result = await client.call_tool(
                            "search_bm25",
                            arguments={"query": "release_signature", "top_k": 5},
                        )
                        payload = _structured_result(result)
                        hits = payload.get("result")
                        if not isinstance(hits, list) or not hits:
                            raise RuntimeError(
                                f"MCP BM25 search returned no hits: {payload!r}"
                            )
                        if "release_signature" not in hits[0].get("content", ""):
                            raise RuntimeError(
                                f"MCP BM25 hit was unexpected: {hits[0]!r}"
                            )

                        source_result = await client.call_tool(
                            "read_source",
                            arguments={
                                "file_path": "calculator.py",
                                "start_line": 1,
                                "end_line": 3,
                            },
                        )
                        source_payload = _structured_result(source_result)
                        source_window = source_payload.get("result", source_payload)
                        if (
                            not isinstance(source_window, dict)
                            or "release_signature"
                            not in source_window.get("content", "")
                            or source_window.get("source", {}).get("verified")
                            is not True
                        ):
                            raise RuntimeError(
                                "MCP source read did not preserve verified source "
                                f"identity: {source_payload!r}"
                            )

                        ranked = await client.call_tool(
                            "search_context",
                            arguments={"query": "release_signature", "top_k": 5},
                        )
                        ranked_payload = _structured_result(ranked)
                        ranked_result = ranked_payload.get("result", ranked_payload)
                        if not isinstance(ranked_result, dict):
                            raise RuntimeError(
                                f"MCP ranked search returned no object: {ranked_payload!r}"
                            )
                        ranked_hits = ranked_result.get("results")
                        if (
                            ranked_result.get("plan", {}).get("name") != "fast_lexical"
                            or not isinstance(ranked_hits, list)
                            or not ranked_hits
                        ):
                            raise RuntimeError(
                                "MCP ranked search did not execute its available "
                                f"BM25 route: {ranked_result!r}"
                            )

                        unavailable = await client.call_tool(
                            "search_semantic",
                            arguments={"query": "release signature", "top_k": 5},
                        )
                        unavailable_payload = _structured_result(unavailable)
                        unavailable_result = unavailable_payload.get(
                            "result",
                            unavailable_payload,
                        )
                        if (
                            not isinstance(unavailable_result, dict)
                            or "error" not in unavailable_result
                        ):
                            raise RuntimeError(
                                "missing semantic view did not return a capability "
                                f"error: {unavailable_payload!r}"
                            )
        except Exception:
            server_log = stderr_path.read_text(encoding="utf-8", errors="replace")
            print(f"\n--- MCP service log ---\n{server_log}", file=sys.stderr)
            raise
    finally:
        protocol_logger.removeHandler(protocol_errors)

    parse_errors = [
        message
        for message in protocol_errors.messages
        if "Failed to parse JSONRPC message from server" in message
    ]
    if parse_errors:
        server_log = stderr_path.read_text(encoding="utf-8", errors="replace")
        raise RuntimeError(
            "MCP server wrote non-protocol data to stdout; "
            f"server stderr follows:\n{server_log}"
        )


def _assert_mcp(
    root: Path,
    *,
    executable: str,
    env: dict[str, str],
    arguments: Sequence[str],
) -> None:
    print("+", executable, *arguments, flush=True)
    asyncio.run(
        _assert_mcp_async(
            root,
            executable=executable,
            env=env,
            arguments=arguments,
        )
    )


def _assert_portable_artifact_mcp(
    root: Path,
    repo: Path,
    *,
    executable: str,
    env: dict[str, str],
) -> None:
    """Pack, rebind, and query the release artifact from another checkout."""

    artifact = root / "context-artifact"
    checkout = root / "artifact-checkout"
    repository = "example/release-smoke"
    _run(
        [
            executable,
            "artifact",
            "pack",
            str(repo),
            "--output",
            str(artifact),
            "--repository",
            repository,
            "--view",
            "bm25",
        ],
        cwd=root,
        env=env,
    )
    _run(
        ["git", "clone", "--quiet", "--no-hardlinks", str(repo), str(checkout)],
        cwd=root,
        env=env,
    )
    _run(
        [
            executable,
            "artifact",
            "verify",
            str(artifact),
            "--repo",
            str(checkout),
            "--repository",
            repository,
        ],
        cwd=root,
        env=env,
    )
    _assert_mcp(
        root,
        executable=executable,
        env=env,
        arguments=[
            "mcp",
            "--artifact",
            str(artifact),
            "--repo",
            str(checkout),
            "--repository",
            repository,
        ],
    )


def _assert_stale_snapshot_rejected(
    root: Path,
    repo: Path,
    *,
    executable: str,
    env: dict[str, str],
) -> None:
    """Advance the checkout and prove that the installed Wiki rejects the index."""

    source_path = repo / "calculator.py"
    source_path.write_text(
        source_path.read_text(encoding="utf-8") + "\ndef release_marker() -> str:\n"
        '    return "new checkout"\n',
        encoding="utf-8",
    )
    _run(["git", "add", "calculator.py"], cwd=repo, env=env)
    _run(["git", "commit", "--quiet", "-m", "advance fixture"], cwd=repo, env=env)

    missing_frontend = root / "intentionally-missing-frontend"
    command = [
        executable,
        "wiki",
        str(repo),
        "--no-index",
        "--no-open",
        "--frontend-dir",
        str(missing_frontend),
        "--no-install-frontend",
    ]
    print("+", " ".join(command), flush=True)
    result = subprocess.run(
        command,
        cwd=root,
        env=env,
        check=False,
        text=True,
        capture_output=True,
        timeout=30,
    )
    stale_messages = (
        "repository checkout does not match the indexed snapshot",
        "repository source files do not match the indexed content",
    )
    if result.returncode != 2 or not any(
        message in result.stderr for message in stale_messages
    ):
        raise RuntimeError(
            "installed Wiki did not reject a stale repository snapshot "
            f"(status {result.returncode}, stderr={result.stderr!r})"
        )


def smoke(root: Path, *, executable: str = "codenib") -> None:
    root.mkdir(parents=True, exist_ok=True)
    repo = _fixture_repository(root)
    env = os.environ.copy()
    env["CODENIB_HOME"] = str(root / "user-state")

    _run(
        [
            executable,
            "doctor",
            "--require",
            "core",
            "--require",
            "wiki",
            "--require",
            "mcp",
        ],
        cwd=root,
        env=env,
    )
    _assert_wiki(root, repo, executable=executable, env=env)
    status = _run(["git", "status", "--porcelain"], cwd=repo, env=env).stdout
    if status:
        raise RuntimeError(
            f"one-command Wiki startup modified the target repository: {status!r}"
        )
    _assert_mcp(
        root,
        executable=executable,
        env=env,
        arguments=["mcp", str(repo)],
    )
    _assert_portable_artifact_mcp(
        root,
        repo,
        executable=executable,
        env=env,
    )
    _assert_stale_snapshot_rejected(
        root,
        repo,
        executable=executable,
        env=env,
    )
    print("Installed Wiki, MCP, and portable artifact service smoke passed")


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
            f"release service smoke failed: command not found: {args.executable}",
            file=sys.stderr,
        )
        return 1

    context = (
        nullcontext(args.root.expanduser().resolve())
        if args.root is not None
        else tempfile.TemporaryDirectory(prefix="codenib-release-services-")
    )
    try:
        with context as value:
            smoke(Path(value), executable=executable)
    except Exception as exc:
        print(f"release service smoke failed: {exc}", file=sys.stderr)
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
