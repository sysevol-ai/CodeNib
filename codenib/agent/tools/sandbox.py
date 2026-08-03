# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Default ``read``/``grep``/``glob``/``bash`` tools bound to a sandbox."""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import List, Optional

from ...sandbox import ExecRequest, SandboxSession
from ...sandbox.types import normalize_workspace_path
from .defaults import TYPE_EXTENSIONS
from .spec import ToolInputSpec, ToolRegistry, ToolSpec

_READ_MAX_FILE_BYTES = 4 * 1024**2
_READ_LINES_DEFAULT = 200
_GREP_RESULTS_DEFAULT = 50
_GLOB_RESULTS_MAX = 100
_BASH_TIMEOUT_MS_DEFAULT = 30_000
_BASH_MAX_OUTPUT_CHARS = 16_000
_SEARCH_CAPTURE_BYTES = 40 * 1024
_SKIP_GLOBS = (
    "!.git/**",
    "!.hg/**",
    "!.svn/**",
    "!**/__pycache__/**",
    "!**/.mypy_cache/**",
    "!**/.pytest_cache/**",
    "!**/.ruff_cache/**",
    "!**/.tox/**",
    "!**/.venv/**",
    "!**/venv/**",
    "!**/node_modules/**",
    "!**/.cache/**",
    "!**/dist/**",
    "!**/build/**",
)

_BOUNDED_LINES_HELPER = r"""
import base64, json, pathlib, subprocess, sys

row_limit = int(sys.argv[1])
byte_limit = int(sys.argv[2])
workspace = pathlib.Path(sys.argv[3])
relative = pathlib.PurePosixPath(sys.argv[4])
target = workspace
for part in relative.parts:
    target = target / part
    if target.is_symlink():
        raise SystemExit('search paths cannot contain symlinks')
target = target.resolve(strict=True)
if target != workspace and workspace not in target.parents:
    raise SystemExit('search path escaped workspace')
command = list(sys.argv[5:])
root_is_file = target.is_file()
if root_is_file:
    cwd = target.parent
    command[-1] = target.name
elif target.is_dir():
    cwd = target
    command[-1] = '.'
else:
    raise SystemExit('search path must be a regular file or directory')
process = subprocess.Popen(command, cwd=cwd, stdout=subprocess.PIPE)
rows = []
used = 0
truncated = False
assert process.stdout is not None
while len(rows) <= row_limit:
    row = process.stdout.readline(65537)
    if not row:
        break
    if len(row) > 65536 and not row.endswith(b'\n'):
        truncated = True
        break
    if used + len(row) > byte_limit:
        truncated = True
        break
    rows.append(row)
    used += len(row)
if len(rows) > row_limit:
    rows = rows[:row_limit]
    truncated = True
if truncated:
    process.terminate()
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
else:
    process.wait()
process.stdout.close()
payload = {
    'data': base64.b64encode(b''.join(rows)).decode('ascii'),
    'returncode': process.returncode,
    'root_is_file': root_is_file,
    'truncated': truncated,
}
print(json.dumps(payload, sort_keys=True, separators=(',', ':')))
""".strip()


def get_sandboxed_tool_specs(session: SandboxSession) -> List[ToolSpec]:
    """Return the four default tool shapes backed only by *session*."""

    return [
        _build_read_tool(session),
        _build_grep_tool(session),
        _build_glob_tool(session),
        _build_bash_tool(session),
    ]


def ensure_sandbox_tools_registered(
    registry: ToolRegistry,
    session: SandboxSession,
) -> None:
    """Idempotently register sandbox-backed default tools."""

    for spec in get_sandboxed_tool_specs(session):
        if not registry.has(spec.tool_id):
            registry.register(spec)


def _build_read_tool(session: SandboxSession) -> ToolSpec:
    def read(file_path: str, offset: int = 1, limit: int = _READ_LINES_DEFAULT) -> str:
        try:
            start = max(1, int(offset))
            line_limit = max(1, int(limit))
        except (TypeError, ValueError):
            return "Error: offset and limit must be integers"
        try:
            data = session.read_file(
                file_path,
                max_bytes=min(
                    _READ_MAX_FILE_BYTES,
                    int(
                        _session_limit(session, "artifact_bytes", _READ_MAX_FILE_BYTES)
                    ),
                ),
            )
        except Exception as exc:
            return f"Error reading {file_path!r} in sandbox: {exc}"
        text = data.decode("utf-8", errors="replace")
        lines = text.splitlines()
        if not lines and not text:
            return f"(empty file: {file_path})"
        if start > len(lines):
            return (
                f"Error: offset {start} exceeds file length {len(lines)} "
                f"in {file_path!r}"
            )
        selected = lines[start - 1 : start - 1 + line_limit]
        result = "\n".join(
            f"{number:6d} | {line}" for number, line in enumerate(selected, start=start)
        )
        extra = len(lines) - (start - 1 + len(selected))
        if extra > 0:
            result += (
                f"\n... ({extra} more lines; "
                f"use offset={start + line_limit} to continue)"
            )
        return result

    return ToolSpec(
        tool_id="read",
        inputs=[
            ToolInputSpec("file_path", "str", description="Repo-relative file path."),
            ToolInputSpec(
                "offset",
                "int",
                required=False,
                default=1,
                description="First 1-based line to read.",
            ),
            ToolInputSpec(
                "limit",
                "int",
                required=False,
                default=_READ_LINES_DEFAULT,
                description="Maximum lines to return.",
            ),
        ],
        executor_fn=read,
        defaults={"offset": 1, "limit": _READ_LINES_DEFAULT},
        tool_doc=(
            "Read a repository-relative file from the isolated sandbox. "
            "Absolute paths, traversal, symlinks, and special files are rejected."
        ),
        description="Read bounded source lines from the isolated workspace.",
    )


def _build_grep_tool(session: SandboxSession) -> ToolSpec:
    def grep(
        pattern: str,
        path: str = ".",
        glob: Optional[str] = None,
        type: Optional[str] = None,
        output_mode: str = "content",
        case_insensitive: bool = True,
        multiline: bool = False,
        head_limit: int = _GREP_RESULTS_DEFAULT,
    ) -> str:
        try:
            relative = normalize_workspace_path(path)
            limit = max(1, min(1000, int(head_limit)))
        except (TypeError, ValueError) as exc:
            return f"Error: {exc}"
        if output_mode not in {"content", "files_with_matches", "count"}:
            return f"Error: unsupported output_mode: {output_mode!r}"
        # Ripgrep emits one physical output row for every line touched by a
        # multiline match.  The public grep contract, however, emits one row
        # per regex match.  Do not silently return a different shape from the
        # isolated backend; a bounded, provider-neutral multiline scanner can
        # be added later without weakening the command/output limits here.
        if multiline:
            return "Error: multiline grep is not supported in sandbox mode"
        output_limit = int(_session_limit(session, "output_bytes", 64 * 1024))
        if output_limit < 512:
            return "Error: sandbox output limit is too small for grep helper metadata"
        capture_bytes = min(
            _SEARCH_CAPTURE_BYTES,
            max(1, ((output_limit - 256) * 3) // 4),
        )

        argv = [
            "rg",
            "--no-heading",
            "--color",
            "never",
            "--hidden",
            "--no-ignore",
            "--text",
            "--with-filename",
        ]
        if case_insensitive:
            argv.append("--ignore-case")
        if output_mode == "content":
            argv.extend(["--line-number", "--null"])
        elif output_mode == "files_with_matches":
            argv.extend(["--files-with-matches", "--sort", "path"])
        else:
            argv.append("--count")
        if glob:
            argv.extend(["--glob", glob])
        normalized_type = (type or "").lower()
        if normalized_type in TYPE_EXTENSIONS:
            for extension in TYPE_EXTENSIONS[normalized_type]:
                argv.extend(["--type-add", f"codenib:*{extension}"])
            argv.extend(["--type", "codenib"])
        for skipped in _SKIP_GLOBS:
            argv.extend(["--glob", skipped])
        argv.extend(["--", pattern, str(relative)])

        try:
            result = session.execute(
                ExecRequest(
                    argv=(
                        "python3",
                        "-I",
                        "-S",
                        "-c",
                        _BOUNDED_LINES_HELPER,
                        str(limit),
                        str(capture_bytes),
                        "/workspace",
                        str(relative),
                        *argv,
                    )
                )
            )
        except Exception as exc:
            return f"Error executing sandbox grep: {exc}"
        if result.timed_out:
            return "Error: sandbox grep timed out"
        if result.output_limited:
            return "Error: sandbox grep exceeded the audit output limit"
        if result.exit_code != 0:
            return f"Error: {result.stderr.strip() or 'sandbox grep failed'}"
        try:
            payload = json.loads(result.stdout)
            raw = base64.b64decode(payload["data"], validate=True)
            search_returncode = int(payload["returncode"])
            root_is_file = bool(payload["root_is_file"])
            helper_truncated = bool(payload["truncated"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            return f"Error: sandbox grep helper returned invalid output: {exc}"
        if not helper_truncated and search_returncode == 1:
            return f"No matches found for {pattern!r}"
        if not helper_truncated and search_returncode != 0:
            return f"Error: {result.stderr.strip() or 'sandbox grep failed'}"

        rows = raw.decode("utf-8", errors="replace").splitlines()
        if output_mode == "content":
            normalized_rows = []
            for row in rows:
                path_field, path_separator, match = row.partition("\0")
                line_number, line_separator, content = match.partition(":")
                if path_separator and line_separator and line_number.isdigit():
                    normalized_rows.append(
                        f"{_normalize_rg_path(path_field)}:{line_number}: {content}"
                    )
                else:
                    normalized_rows.append(
                        re.sub(
                            r"^(.+?:\d+:)(\S)",
                            r"\1 \2",
                            _normalize_rg_path(row.replace("\0", ":", 1)),
                        )
                    )
            rows = normalized_rows
        else:
            rows = [_normalize_rg_path(row) for row in rows]
        if root_is_file:
            basename = relative.name
            root_display = str(relative)
            rows = [
                (
                    root_display + row[len(basename) :]
                    if row == basename or row.startswith(f"{basename}:")
                    else row
                )
                for row in rows
            ]
        truncated = helper_truncated or result.stdout_truncated
        text = "\n".join(rows)
        if truncated:
            text += f"\n... (results truncated at head_limit={limit})"
        return text or f"No matches found for {pattern!r}"

    return ToolSpec(
        tool_id="grep",
        inputs=[
            ToolInputSpec("pattern", "str", description="Ripgrep pattern."),
            ToolInputSpec("path", "str", required=False, default="."),
            ToolInputSpec("glob", "str", required=False, default=None),
            ToolInputSpec("type", "str", required=False, default=None),
            ToolInputSpec(
                "output_mode",
                "str",
                required=False,
                default="content",
                enum=["content", "files_with_matches", "count"],
            ),
            ToolInputSpec("case_insensitive", "bool", required=False, default=True),
            ToolInputSpec(
                "multiline",
                "bool",
                required=False,
                default=False,
                description="Unavailable in sandbox mode; keep false.",
            ),
            ToolInputSpec(
                "head_limit",
                "int",
                required=False,
                default=_GREP_RESULTS_DEFAULT,
            ),
        ],
        executor_fn=grep,
        defaults={
            "path": ".",
            "output_mode": "content",
            "case_insensitive": True,
            "multiline": False,
            "head_limit": _GREP_RESULTS_DEFAULT,
        },
        tool_doc=(
            "Search repository content with ripgrep inside the isolated sandbox. "
            "The pinned sandbox image must provide `rg`. Multiline matching is "
            "rejected because ripgrep's row shape differs from the local tool "
            "contract."
        ),
        description="Search source content inside the isolated workspace.",
    )


def _build_glob_tool(session: SandboxSession) -> ToolSpec:
    def glob(pattern: str, path: str = ".") -> str:
        if not pattern:
            return "Error: pattern must be non-empty"
        try:
            relative = normalize_workspace_path(path)
        except ValueError as exc:
            return f"Error: {exc}"
        output_limit = int(_session_limit(session, "output_bytes", 64 * 1024))
        if output_limit < 512:
            return "Error: sandbox output limit is too small for glob helper metadata"
        capture_bytes = min(
            _SEARCH_CAPTURE_BYTES,
            max(1, ((output_limit - 256) * 3) // 4),
        )
        argv = ["rg", "--files", "--hidden", "--no-ignore", "--glob", pattern]
        for skipped in _SKIP_GLOBS:
            argv.extend(["--glob", skipped])
        argv.extend(["--", str(relative)])
        try:
            result = session.execute(
                ExecRequest(
                    argv=(
                        "python3",
                        "-I",
                        "-S",
                        "-c",
                        _BOUNDED_LINES_HELPER,
                        str(_GLOB_RESULTS_MAX),
                        str(capture_bytes),
                        "/workspace",
                        str(relative),
                        *argv,
                    )
                )
            )
        except Exception as exc:
            return f"Error executing sandbox glob: {exc}"
        if result.timed_out:
            return "Error: sandbox glob timed out"
        if result.output_limited:
            return "Error: sandbox glob exceeded the audit output limit"
        if result.exit_code != 0:
            return f"Error: {result.stderr.strip() or 'sandbox glob failed'}"
        try:
            payload = json.loads(result.stdout)
            raw = base64.b64decode(payload["data"], validate=True)
            search_returncode = int(payload["returncode"])
            root_is_file = bool(payload["root_is_file"])
            helper_truncated = bool(payload["truncated"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            return f"Error: sandbox glob helper returned invalid output: {exc}"
        if not helper_truncated and search_returncode not in {0, 1}:
            return f"Error: {result.stderr.strip() or 'sandbox glob failed'}"
        rows = sorted(
            {
                _normalize_rg_path(row)
                for row in raw.decode("utf-8", errors="replace").splitlines()
                if row
            }
        )
        if root_is_file:
            rows = [str(relative) if row == relative.name else row for row in rows]
        truncated = helper_truncated or result.stdout_truncated
        text = "\n".join(rows)
        if truncated:
            text += f"\n... (results truncated at {_GLOB_RESULTS_MAX})"
        return text or f"No files matched {pattern!r}"

    return ToolSpec(
        tool_id="glob",
        inputs=[
            ToolInputSpec("pattern", "str", description="File glob pattern."),
            ToolInputSpec("path", "str", required=False, default="."),
        ],
        executor_fn=glob,
        defaults={"path": "."},
        tool_doc=(
            "List matching repository files inside the isolated sandbox. "
            "The pinned sandbox image must provide `rg`."
        ),
        description="Enumerate files inside the isolated workspace.",
    )


def _build_bash_tool(session: SandboxSession) -> ToolSpec:
    def bash(
        command: str,
        timeout: int = _BASH_TIMEOUT_MS_DEFAULT,
        description: Optional[str] = None,  # noqa: ARG001
        cwd: Optional[str] = None,
    ) -> str:
        try:
            timeout_ms = int(timeout)
        except (TypeError, ValueError):
            return "Error: timeout must be an integer (milliseconds)"
        timeout_seconds = min(
            max(0.001, timeout_ms / 1000),
            float(_session_limit(session, "command_timeout_seconds", 300.0)),
        )
        try:
            request = ExecRequest(
                argv=("/bin/sh", "-lc", command),
                cwd=cwd or PurePosixPath("."),
                timeout_seconds=timeout_seconds,
            )
            result = session.execute(request)
        except Exception as exc:
            return f"Error executing sandbox command {command!r}: {exc}"
        if result.timed_out:
            return f"Error: command timed out after {timeout_seconds}s: {command!r}"
        if result.output_limited:
            return f"Error: command exceeded the sandbox output limit: {command!r}"

        parts = [f"$ {command}", f"(exit code: {result.exit_code})"]
        if result.stdout:
            parts.append(f"--- stdout ---\n{result.stdout.rstrip()}")
        if result.stderr:
            parts.append(f"--- stderr ---\n{result.stderr.rstrip()}")
        text = "\n".join(parts)
        if result.stdout_truncated or result.stderr_truncated:
            text += (
                "\n... (output truncated; full bounded logs are in the audit record)"
            )
        if len(text) > _BASH_MAX_OUTPUT_CHARS:
            text = text[:_BASH_MAX_OUTPUT_CHARS] + "\n... (output truncated)"
        return text

    return ToolSpec(
        tool_id="bash",
        inputs=[
            ToolInputSpec("command", "str", description="Shell command line."),
            ToolInputSpec(
                "timeout",
                "int",
                required=False,
                default=_BASH_TIMEOUT_MS_DEFAULT,
            ),
            ToolInputSpec("description", "str", required=False, default=None),
            ToolInputSpec("cwd", "str", required=False, default=None),
        ],
        executor_fn=bash,
        defaults={"timeout": _BASH_TIMEOUT_MS_DEFAULT},
        tool_doc=(
            "Execute a shell command inside the isolated sandbox. The host "
            "never interprets the command and no host environment is forwarded."
        ),
        description="Execute a bounded command inside the isolated workspace.",
    )


def _normalize_rg_path(row: str) -> str:
    return row[2:] if row.startswith("./") else row


def _session_limit(
    session: SandboxSession, name: str, default: float | int
) -> float | int:
    policy = session.metadata.policy
    limits = policy.get("limits") if isinstance(policy, Mapping) else None
    value = limits.get(name) if isinstance(limits, Mapping) else None
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return value
    return default


__all__ = ["ensure_sandbox_tools_registered", "get_sandboxed_tool_specs"]
