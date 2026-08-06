# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Always-on default tool primitives: ``read`` / ``grep`` / ``glob`` / ``bash``.

These four tools are registered into ``AgentRunner``'s :class:`ToolRegistry`
(separate from the skill registry) at startup and are **never** removed by the
``exclude_skills`` or ``allow_skills`` arguments — every skill subset builds on
top of them.

Why four tools (and not the older single ``file_search`` multiplexer)?
----------------------------------------------------------------------
The shape mirrors the mainstream Claude Code tool reference
(https://code.claude.com/docs/en/tools-reference): one focused tool per
concern — ``Read`` / ``Grep`` / ``Glob`` / ``Bash`` — instead of one
``file_search`` skill with a ``mode`` discriminator and a flat schema whose
parameters are silently ignored depending on the mode. Splitting them gives
each tool a clean, self-describing schema: the model never sees ``timeout`` on
a content search or ``case_insensitive`` on a shell command.

Deviations from the upstream reference (and why)
------------------------------------------------
The registry dispatches tool calls as ``executor_fn(**json_args)`` (see
``AgentRunner._execute_tool_call``), so every schema parameter must be a valid
Python identifier. That rules out the reference's literal flag names
(``-i`` / ``-n`` / ``-A`` / ``-B`` / ``-C``). We therefore:

- name the skills in lowercase snake_case (``read`` / ``grep`` / ``glob`` /
  ``bash``) to match the existing registry convention (``bm25_search``,
  ``graph_expand``, ...), not the reference's TitleCase display names;
- expose ``-i`` as ``case_insensitive`` (defaulting to ``True`` to preserve the
  prior always-on behaviour, where upstream grep defaults to case-sensitive);
  line numbers are always emitted (the reference ``-n``) and context lines
  (``-A`` / ``-B`` / ``-C``) are not implemented;
- default ``grep.output_mode`` to ``content`` (upstream defaults to
  ``files_with_matches``) — content is the more useful default for a
  localization agent;
- express ``bash.timeout`` in **milliseconds** to match the reference units,
  drop ``run_in_background`` (no background-task infrastructure here), and add
  ``cwd`` (the reference relies on session ``cd`` persistence, which this
  subprocess-per-call executor lacks);
- cap ``glob`` at 100 results internally (matching the reference's documented
  cap) rather than exposing a ``max_results`` knob.

Line numbers follow **opencode** style (``{lineno:6d} | {content}``) and the
read/grep boundary is **1-based** throughout, aligned with #147/#153.

The ``grep`` tool scans the raw filesystem with no index. It uses ripgrep when
available for bounded, linear-time matching and falls back to the Python
implementation otherwise. It is the regex primitive for the agent (an earlier
index-backed ``regex_search`` skill was removed in favour of it).

Related issues: #133 (Agent Router RFC), #145 (defaults), #147 (line-number
convention), #153 (1-based boundary).
"""

from __future__ import annotations

import os
import re
import select
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import BinaryIO, Dict, List, Optional, Tuple

from .spec import ToolInputSpec, ToolRegistry, ToolSpec

# Canonical tool IDs for the always-on defaults.
DEFAULT_TOOL_IDS: frozenset[str] = frozenset({"read", "grep", "glob", "bash"})

# Sensible token-safety caps.
_MAX_LINES_DEFAULT: int = 200
_MAX_RESULTS_DEFAULT: int = 50
_GLOB_MAX_RESULTS: int = 100  # mirrors the reference Glob's documented cap
_GREP_TIMEOUT_SECONDS: int = 30
_BASH_TIMEOUT_MS_DEFAULT: int = 30_000  # 30s, expressed in ms (reference units)
_BASH_TIMEOUT_MS_MAX: int = 600_000
_BASH_MAX_COMMAND_CHARS: int = 32_000
_BASH_MAX_OUTPUT_CHARS: int = 16_000  # matches runner._MAX_RESULT_CHARS
_BASH_READ_CHUNK_BYTES: int = 64 * 1024

# Directories to skip during recursive search (avoids scanning VCS / cache noise).
_SKIP_DIR_PREFIXES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "venv",
        "node_modules",
        ".cache",
        "dist",
        "build",
    }
)

# `grep` file-type filter: maps a language token to the extensions it covers.
# Mirrors the reference Grep's ``type`` parameter (ripgrep types) for the
# languages CodeNib indexes, plus a few common siblings.
TYPE_EXTENSIONS: Dict[str, Tuple[str, ...]] = {
    "py": (".py", ".pyi"),
    "python": (".py", ".pyi"),
    "js": (".js", ".jsx", ".mjs", ".cjs"),
    "ts": (".ts", ".tsx"),
    "go": (".go",),
    "rust": (".rs",),
    "rs": (".rs",),
    "c": (".c", ".h"),
    "cpp": (".cpp", ".cc", ".cxx", ".hpp", ".hh", ".h"),
    "java": (".java",),
    "rb": (".rb",),
    "ruby": (".rb",),
    "md": (".md", ".markdown"),
}
_TYPE_EXTENSIONS = TYPE_EXTENSIONS

_GREP_OUTPUT_MODES = ("content", "files_with_matches", "count")

# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------


def _read(
    file_path: str,
    offset: int = 1,
    limit: int = _MAX_LINES_DEFAULT,
) -> str:
    """Read a file, returning its content with per-row 1-based line numbers.

    Output format::

           1 | def foo():
           2 |     return 42

    Args:
        file_path: Absolute or repo-relative path to the file.
        offset: First line to read (1-based, inclusive). Defaults to 1. Mirrors
            the reference ``Read.offset``.
        limit: Hard cap on lines returned for token safety. Defaults to 200. A
            truncation notice with the next ``offset`` is appended when the cap
            is reached. Mirrors the reference ``Read.limit``.

    Returns:
        Formatted string, or an error message prefixed with ``"Error: "``.
    """
    # Coerce inputs up front: a non-integer arg returns an Error string (the
    # module contract) instead of raising into the caller. `limit` is floored at
    # 1 so `limit=0` cannot emit a "0 lines; continue at the same offset" notice
    # that loops forever.
    try:
        start = max(1, int(offset))
        limit = max(1, int(limit))
    except (TypeError, ValueError):
        return "Error: offset and limit must be integers"

    # Stream the file: keep at most `limit` lines in memory and merely *count*
    # any further lines (for the truncation notice) without storing them, so a
    # huge file is never fully materialised.
    selected: List[str] = []
    extra = 0  # lines present beyond the kept window
    total = 0
    try:
        with open(file_path, encoding="utf-8", errors="replace") as fh:
            for idx, line in enumerate(fh, start=1):
                total = idx
                if idx < start:
                    continue
                if len(selected) < limit:
                    selected.append(line)
                else:
                    extra += 1
    except FileNotFoundError:
        return f"Error: file not found: {file_path!r}"
    except IsADirectoryError:
        return f"Error: {file_path!r} is a directory, not a file"
    except OSError as exc:
        return f"Error reading {file_path!r}: {exc}"

    if total == 0:
        return f"(empty file: {file_path})"
    if start > total:
        return f"Error: offset {start} exceeds file length {total} in {file_path!r}"

    lines_out = [
        f"{lineno:6d} | {line.rstrip()}"
        for lineno, line in enumerate(selected, start=start)
    ]
    result = "\n".join(lines_out)

    if extra > 0:
        next_start = start + limit  # first omitted line (1-based)
        result += f"\n... ({extra} more lines; " f"use offset={next_start} to continue)"

    return result


_READ_TOOL_DOC = """\
# read

Read a source file and return its content with per-row 1-based line numbers.
Shape mirrors the mainstream Claude Code `Read` tool.

## Parameters

| Name | Type | Default | Description |
|------|------|---------|-------------|
| `file_path` | `str` | *(required)* | Absolute or repo-relative file path. |
| `offset` | `int` | `1` | First line to read (1-based, inclusive). |
| `limit` | `int` | `200` | Max lines returned; prevents context blowup. |

## Output

Lines formatted as `{lineno:6d} | {content}` (opencode-style, 1-based).
A truncation notice with the next `offset` is appended when `limit` is reached.

## When to Use

- Inspect a function or class after a search tool narrowed the location.
- Read context lines around a `grep` match.
- Retrieve a known file at a specific line range (pass `offset`/`limit`).

## Safety

`read` opens `file_path` as given — there is **no path jail**. An absolute
path reads any file the process can (`/etc/shadow`, `~/.ssh/id_rsa`, ...). As
an always-on tool every agent turn can call, untrusted query input or prompt
injection could exfiltrate any readable file. Callers running the agent on
untrusted input MUST sandbox at the container / VM / process level.
"""


def _build_read_tool() -> ToolSpec:
    return ToolSpec(
        tool_id="read",
        inputs=[
            ToolInputSpec(
                name="file_path",
                type_hint="str",
                required=True,
                description="Absolute or repo-relative path to the file.",
            ),
            ToolInputSpec(
                name="offset",
                type_hint="int",
                required=False,
                default=1,
                description="First line to read (1-based, inclusive). Defaults to 1.",
            ),
            ToolInputSpec(
                name="limit",
                type_hint="int",
                required=False,
                default=_MAX_LINES_DEFAULT,
                description=(f"Max lines returned (default {_MAX_LINES_DEFAULT})."),
            ),
        ],
        executor_fn=_read,
        defaults={"offset": 1, "limit": _MAX_LINES_DEFAULT},
        tool_doc=_READ_TOOL_DOC,
        output_type_hint="str",
        description=(
            "Read a file with 1-based line numbers (opencode-style); "
            "output is bounded by `limit` for token safety."
        ),
    )


# ---------------------------------------------------------------------------
# grep — content search over the raw filesystem
# ---------------------------------------------------------------------------


def _grep_candidate_files(
    root: Path,
    glob: Optional[str],
    type_: Optional[str],
):
    """Yield candidate files under *root* honouring ``glob`` / ``type`` filters.

    ``glob`` filters file *names* (passed to ``rglob``); ``type`` further
    restricts by extension via ``_TYPE_EXTENSIONS``. Noise directories
    (``_SKIP_DIR_PREFIXES``) are skipped.
    """
    exts = _TYPE_EXTENSIONS.get((type_ or "").lower()) if type_ else None
    for file_path in root.rglob(glob or "*"):
        if not file_path.is_file():
            continue
        if any(part in _SKIP_DIR_PREFIXES for part in file_path.parts):
            continue
        if exts is not None and file_path.suffix.lower() not in exts:
            continue
        yield file_path


def _grep_python(
    pattern: str,
    path: str = ".",
    glob: Optional[str] = None,
    type: Optional[str] = None,  # noqa: A002 — matches the reference param name
    output_mode: str = "content",
    case_insensitive: bool = True,
    multiline: bool = False,
    head_limit: int = _MAX_RESULTS_DEFAULT,
) -> str:
    """Regex content search over file contents (the reference ``Grep`` shape).

    Returns text shaped by ``output_mode``:

    - ``content`` *(default)* — ``{file}:{lineno}: {line}`` per match (1-based).
    - ``files_with_matches`` — unique file paths that contain a match.
    - ``count`` — ``{file}:{count}`` per matching file.

    ``head_limit`` caps the number of emitted rows (matches in content mode,
    files in the other two). A no-match message is returned when nothing hits.
    """
    if output_mode not in _GREP_OUTPUT_MODES:
        return (
            f"Error: invalid output_mode {output_mode!r}; "
            f"expected one of {list(_GREP_OUTPUT_MODES)}"
        )
    try:
        head_limit = max(1, int(head_limit))
    except (TypeError, ValueError):
        return "Error: head_limit must be an integer"

    flags = 0 if case_insensitive is False else re.IGNORECASE
    if multiline:
        flags |= re.MULTILINE | re.DOTALL
    try:
        compiled = re.compile(pattern, flags)
    except re.error as exc:
        return f"Error: invalid regex {pattern!r}: {exc}"

    root = Path(path)
    if not root.exists():
        return f"Error: path {path!r} does not exist"

    content_rows: List[str] = []  # "{rel}:{lineno}: {line}"
    file_match_count: "Dict[str, int]" = {}  # rel -> match count, insertion order
    capped = False

    def _rel(file_path: Path) -> str:
        if root.is_dir():
            try:
                return str(file_path.relative_to(root))
            except ValueError:
                return str(file_path)
        return str(file_path)

    def _scan(file_path: Path) -> bool:
        """Record matches for *file_path*; return True if the head cap was hit."""
        rel = _rel(file_path)
        try:
            if multiline:
                text = file_path.read_text(encoding="utf-8", errors="replace")
                for m in compiled.finditer(text):
                    lineno = text.count("\n", 0, m.start()) + 1
                    snippet = text[m.start() : m.end()].splitlines()[0]
                    file_match_count[rel] = file_match_count.get(rel, 0) + 1
                    if output_mode == "content":
                        content_rows.append(f"{rel}:{lineno}: {snippet}")
                        if len(content_rows) >= head_limit:
                            return True
            else:
                with open(file_path, encoding="utf-8", errors="replace") as fh:
                    for lineno, line in enumerate(fh, start=1):
                        if compiled.search(line):
                            file_match_count[rel] = file_match_count.get(rel, 0) + 1
                            if output_mode == "content":
                                content_rows.append(f"{rel}:{lineno}: {line.rstrip()}")
                                if len(content_rows) >= head_limit:
                                    return True
        except OSError:
            # Best-effort: a single unreadable file must not abort the scan.
            pass
        # In file-granular modes, cap on the number of distinct matching files.
        if output_mode != "content" and len(file_match_count) >= head_limit:
            return True
        return False

    if root.is_file():
        _scan(root)
    else:
        try:
            for file_path in _grep_candidate_files(root, glob, type):
                if _scan(file_path):
                    capped = True
                    break
        except (ValueError, OSError, NotImplementedError) as exc:
            return f"Error: invalid glob {glob!r}: {exc}"

    if not file_match_count:
        return "No matches found."

    if output_mode == "content":
        rows = content_rows
        cap_unit = "matches"
    elif output_mode == "files_with_matches":
        rows = sorted(file_match_count)[:head_limit]
        cap_unit = "files"
    else:  # count
        rows = [f"{rel}:{n}" for rel, n in list(file_match_count.items())[:head_limit]]
        cap_unit = "files"

    text = "\n".join(rows)
    if capped:
        text += (
            f"\n... (head_limit={head_limit} {cap_unit} reached; "
            "narrow with `glob`/`type` or a more specific `pattern`)"
        )
    return text


def _terminate_process_group(proc: subprocess.Popen[bytes]) -> None:
    try:
        # ``start_new_session=True`` makes the child PID its process-group ID.
        # Use it directly because the group can outlive an already-exited shell.
        os.killpg(proc.pid, signal.SIGKILL)
    except (AttributeError, OSError):
        if proc.poll() is None:
            proc.kill()


def _grep_ripgrep(
    rg_path: str,
    pattern: str,
    root: Path,
    glob: Optional[str],
    type_: Optional[str],
    output_mode: str,
    case_insensitive: bool,
    multiline: bool,
    head_limit: int,
) -> str:
    """Run the bounded Grep contract on ripgrep's linear-time regex engine."""
    command = [
        rg_path,
        "--color=never",
        "--no-heading",
        "--hidden",
        "--no-ignore",
        "--text",
    ]
    if case_insensitive is not False:
        command.append("--ignore-case")
    if multiline:
        command.extend(("--multiline", "--multiline-dotall"))
    if output_mode == "content":
        # NUL separates the path from line/content fields, so paths containing
        # colons can still be normalized without guessing where the path ends.
        command.extend(("--line-number", "--with-filename", "--null"))
    elif output_mode == "files_with_matches":
        command.extend(("--files-with-matches", "--sort", "path"))
    else:
        # Python's fallback counts matching lines, not every match on a line.
        command.append("--count")

    for skipped_dir in sorted(_SKIP_DIR_PREFIXES):
        command.extend(("--glob", f"!**/{skipped_dir}/**"))
    if glob:
        command.extend(("--glob", glob))
    normalized_type = (type_ or "").lower()
    if not root.is_file() and normalized_type in _TYPE_EXTENSIONS:
        # Define our own type instead of using rg's broader built-ins (for
        # example rg's ``js`` also includes Vue files). This keeps the rg and
        # Python backends on the same public extension contract.
        for extension in _TYPE_EXTENSIONS[normalized_type]:
            command.extend(("--type-add", f"codenib:*{extension}"))
        command.extend(("--type", "codenib"))

    if root.is_file():
        cwd = root.parent or Path(".")
        target = root.name
    else:
        cwd = root
        target = "."
    command.extend(("--", pattern, target))

    with tempfile.TemporaryFile() as stderr_file:
        try:
            proc = subprocess.Popen(  # noqa: S603 - fixed executable and argv
                command,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=stderr_file,
                bufsize=0,
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            return f"Error executing grep: {exc}"

        assert proc.stdout is not None
        deadline = time.monotonic() + _GREP_TIMEOUT_SECONDS
        pending = bytearray()
        rows: List[str] = []
        timed_out = False
        eof = False
        while len(rows) <= head_limit and not eof:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            ready, _, _ = select.select((proc.stdout.fileno(),), (), (), remaining)
            if not ready:
                timed_out = True
                break
            chunk = os.read(proc.stdout.fileno(), 64 * 1024)
            if not chunk:
                eof = True
            else:
                pending.extend(chunk)
            while b"\n" in pending and len(rows) <= head_limit:
                raw, _, remainder = pending.partition(b"\n")
                pending = bytearray(remainder)
                rows.append(raw.rstrip(b"\r").decode("utf-8", errors="replace"))
        if eof and pending and len(rows) <= head_limit:
            rows.append(bytes(pending).rstrip(b"\r").decode("utf-8", errors="replace"))

        capped = len(rows) > head_limit
        if timed_out or capped:
            _terminate_process_group(proc)
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            _terminate_process_group(proc)
            proc.wait()
        stderr_file.seek(0)
        stderr = stderr_file.read().decode("utf-8", errors="replace")
        proc.stdout.close()

    if timed_out:
        return f"Error: grep timed out after {_GREP_TIMEOUT_SECONDS}s"
    if not capped and proc.returncode not in (0, 1):
        detail = stderr.strip() or f"ripgrep exited with status {proc.returncode}"
        if "regex parse error" in detail.lower():
            return f"Error: invalid regex {pattern!r}: {detail}"
        return f"Error executing grep for pattern {pattern!r}: {detail}"

    rows = rows[:head_limit]
    if output_mode == "content":
        normalized_rows = []
        for row in rows:
            path_field, separator, match = row.partition("\0")
            line_number, line_separator, content = match.partition(":")
            if separator and line_separator and line_number.isdigit():
                normalized_rows.append(f"{path_field}:{line_number}: {content}")
            else:  # Defensive fallback for an unexpected rg output shape.
                normalized_rows.append(row.replace("\0", ":", 1))
        rows = normalized_rows
    if root.is_file():
        prefix = f"{root.name}:"
        replacement = f"{root}:"
        rows = [
            replacement + row[len(prefix) :] if row.startswith(prefix) else row
            for row in rows
        ]
    else:
        rows = [row[2:] if row.startswith("./") else row for row in rows]

    if not rows:
        return "No matches found."
    text = "\n".join(rows)
    if capped:
        cap_unit = "matches" if output_mode == "content" else "files"
        text += (
            f"\n... (head_limit={head_limit} {cap_unit} reached; "
            "narrow with `glob`/`type` or a more specific `pattern`)"
        )
    return text


def _grep(
    pattern: str,
    path: str = ".",
    glob: Optional[str] = None,
    type: Optional[str] = None,  # noqa: A002 - matches the reference param name
    output_mode: str = "content",
    case_insensitive: bool = True,
    multiline: bool = False,
    head_limit: int = _MAX_RESULTS_DEFAULT,
) -> str:
    """Regex content search with a bounded ripgrep backend when available."""
    if output_mode not in _GREP_OUTPUT_MODES:
        return (
            f"Error: invalid output_mode {output_mode!r}; "
            f"expected one of {list(_GREP_OUTPUT_MODES)}"
        )
    try:
        head_limit = max(1, int(head_limit))
    except (TypeError, ValueError):
        return "Error: head_limit must be an integer"

    root = Path(path)
    if not root.exists():
        return f"Error: path {path!r} does not exist"
    rg_path = shutil.which("rg")
    # ripgrep emits one output row per physical line of a multiline match,
    # whereas this tool's contract emits one row per match. Preserve that
    # contract by retaining the Python implementation for multiline queries.
    if rg_path and not multiline:
        return _grep_ripgrep(
            rg_path,
            pattern,
            root,
            glob,
            type,
            output_mode,
            case_insensitive,
            multiline,
            head_limit,
        )
    return _grep_python(
        pattern,
        path=path,
        glob=glob,
        type=type,
        output_mode=output_mode,
        case_insensitive=case_insensitive,
        multiline=multiline,
        head_limit=head_limit,
    )


_GREP_TOOL_DOC = """\
# grep

Regex content search over the raw filesystem (no index). Shape mirrors the
mainstream Claude Code `Grep` tool. Searches use ripgrep when installed, with a
30-second wall-clock bound and a Python fallback.

## Parameters

| Name | Type | Default | Description |
|------|------|---------|-------------|
| `pattern` | str | *required* | Regex (rg; Python for multiline/fallback). |
| `path` | str | `.` | Directory (or single file) to search. |
| `glob` | str | null | Filter file *names* (e.g. `*.py`, `**/test_*.py`). |
| `type` | str | null | Filter by language (`py`, `ts`, `go`, `rust`, `cpp`, ...). |
| `output_mode` | str | `content` | `content` / `files_with_matches` / `count`. |
| `case_insensitive` | bool | true | Case-insensitive matching (reference `-i`). |
| `multiline` | bool | false | Let `.`/`^`/`$` span lines (reference `multiline`). |
| `head_limit` | int | 50 | Cap on rows (matches, or files in the other modes). |

## Output

- `content` — `{file}:{lineno}: {line}` per match (1-based).
- `files_with_matches` — sorted unique file paths that contain a match.
- `count` — `{file}:{count}` per matching file.

## When to Use

- Find call sites, error strings, or identifier usage anywhere in the repo.
- A base-class method often has the real change in a SUBCLASS OVERRIDE — grep
  the method name to find all definitions, then `read` them.
- Use `output_mode="files_with_matches"` to locate *which* files first, then
  `read` them; `count` to gauge how widespread a symbol is.

## Note vs the reference

Parameter names differ from upstream where they must be Python identifiers:
`-i` → `case_insensitive` (defaulting to true here), line numbers are always
on, and context lines (`-A`/`-B`/`-C`) are not implemented.
"""


def _build_grep_tool() -> ToolSpec:
    return ToolSpec(
        tool_id="grep",
        inputs=[
            ToolInputSpec(
                name="pattern",
                type_hint="str",
                required=True,
                description="Regular expression to search for (Python re syntax).",
            ),
            ToolInputSpec(
                name="path",
                type_hint="str",
                required=False,
                default=".",
                description="Directory (or single file) to search. Defaults to '.'.",
            ),
            ToolInputSpec(
                name="glob",
                type_hint="str",
                required=False,
                default=None,
                description="Filter file names by glob (e.g. '*.py', '**/test_*.py').",
            ),
            ToolInputSpec(
                name="type",
                type_hint="str",
                required=False,
                default=None,
                description=(
                    "Filter by language: py, js, ts, go, rust, c, cpp, " "java, rb, md."
                ),
            ),
            ToolInputSpec(
                name="output_mode",
                type_hint="str",
                required=False,
                default="content",
                enum=list(_GREP_OUTPUT_MODES),
                description=(
                    "'content' (matching lines), 'files_with_matches' (paths "
                    "only), or 'count' (matches per file). Defaults to 'content'."
                ),
            ),
            ToolInputSpec(
                name="case_insensitive",
                type_hint="bool",
                required=False,
                default=True,
                description="Case-insensitive matching (reference '-i'). Default true.",
            ),
            ToolInputSpec(
                name="multiline",
                type_hint="bool",
                required=False,
                default=False,
                description="Let '.'/'^'/'$' span line boundaries. Default false.",
            ),
            ToolInputSpec(
                name="head_limit",
                type_hint="int",
                required=False,
                default=_MAX_RESULTS_DEFAULT,
                description=(
                    "Cap on rows returned (matches in content mode, files "
                    f"otherwise; default {_MAX_RESULTS_DEFAULT})."
                ),
            ),
        ],
        executor_fn=_grep,
        defaults={
            "path": ".",
            "output_mode": "content",
            "case_insensitive": True,
            "multiline": False,
            "head_limit": _MAX_RESULTS_DEFAULT,
        },
        tool_doc=_GREP_TOOL_DOC,
        output_type_hint="str",
        description=(
            "Regex content search over the raw filesystem; output shaped by "
            "output_mode (content / files_with_matches / count)."
        ),
    )


# ---------------------------------------------------------------------------
# glob — filename enumeration
# ---------------------------------------------------------------------------


def _glob(pattern: str, path: str = ".") -> str:
    """Glob-style filename enumeration under *path* (the reference ``Glob``).

    ``pattern`` follows ``Path.rglob`` syntax (e.g. ``"*.py"``,
    ``"**/test_*.py"``). Returns a sorted newline-separated list of relative
    paths, or a no-match message when empty. Capped at ``_GLOB_MAX_RESULTS``
    (mirrors the reference cap); the cap is applied during traversal
    (filesystem order) and only the returned subset is sorted.
    """
    root_path = Path(path)
    if not root_path.exists():
        return f"Error: path {path!r} does not exist"
    if not root_path.is_dir():
        return f"Error: glob root {path!r} is not a directory"

    matches: List[str] = []
    capped = False
    # rglob is a generator: a bad pattern (empty, absolute, unsupported glob)
    # raises during *iteration*, and may raise NotImplementedError as well as
    # ValueError — so wrap the whole loop, not just the rglob() call.
    try:
        for found in root_path.rglob(pattern):
            if not found.is_file():
                continue
            if any(part in _SKIP_DIR_PREFIXES for part in found.parts):
                continue
            try:
                rel = found.relative_to(root_path)
            except ValueError:
                rel = found
            matches.append(str(rel))
            if len(matches) >= _GLOB_MAX_RESULTS:
                capped = True
                break
    except (ValueError, OSError, NotImplementedError) as exc:
        return f"Error: invalid glob pattern {pattern!r}: {exc}"

    if not matches:
        return f"No files match {pattern!r} under {path!r}."

    text = "\n".join(sorted(matches))
    if capped:
        text += f"\n... ({_GLOB_MAX_RESULTS}-result cap reached; " "narrow the pattern)"
    return text


_GLOB_TOOL_DOC = """\
# glob

Enumerate files by name pattern (the mainstream Claude Code `Glob` shape).

## Parameters

| Name | Type | Default | Description |
|------|------|---------|-------------|
| `pattern` | str | *required* | `Path.rglob` glob (e.g. `*.py`, `**/*.proto`). |
| `path` | str | `.` | Root directory to search. |

## Output

Sorted newline-separated relative paths. Capped at 100 results (narrow the
pattern if you hit it).

## When to Use

- Enumerate files matching a structure (`**/__init__.py`, `**/*.proto`).
- Confirm a file exists before `read`.
- Map the layout of an unfamiliar subtree.
"""


def _build_glob_tool() -> ToolSpec:
    return ToolSpec(
        tool_id="glob",
        inputs=[
            ToolInputSpec(
                name="pattern",
                type_hint="str",
                required=True,
                description="Glob pattern (Path.rglob syntax, e.g. '**/*.py').",
            ),
            ToolInputSpec(
                name="path",
                type_hint="str",
                required=False,
                default=".",
                description="Root directory to search. Defaults to '.'.",
            ),
        ],
        executor_fn=_glob,
        defaults={"path": "."},
        tool_doc=_GLOB_TOOL_DOC,
        output_type_hint="str",
        description=(
            "Enumerate files by glob pattern; returns sorted relative paths "
            "(capped at 100)."
        ),
    )


# ---------------------------------------------------------------------------
# bash — shell command execution
# ---------------------------------------------------------------------------


class _BoundedPipeOutput:
    """Drain a subprocess pipe while retaining only a fixed byte prefix."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._buffer = bytearray()
        self.truncated = False

    def drain(self, stream: BinaryIO) -> None:
        try:
            while chunk := stream.read(_BASH_READ_CHUNK_BYTES):
                remaining = self._limit - len(self._buffer)
                if remaining > 0:
                    self._buffer.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    self.truncated = True
        except (OSError, ValueError):
            # Forced process-group cleanup may close a pipe under the reader.
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def text(self) -> str:
        return bytes(self._buffer).decode("utf-8", errors="replace")


def _wait_for_bash(
    proc: subprocess.Popen[bytes],
    readers: tuple[threading.Thread, threading.Thread],
    *,
    timeout_seconds: float,
) -> bool:
    """Wait for both the shell and inherited output pipes until one deadline."""

    deadline = time.monotonic() + timeout_seconds
    timed_out = False
    try:
        proc.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True

    if not timed_out:
        for reader in readers:
            reader.join(max(0.0, deadline - time.monotonic()))
        # A background child can inherit the pipes after its shell exits. Treat
        # that child as part of the command under the same wall-clock deadline.
        timed_out = any(reader.is_alive() for reader in readers)

    if timed_out:
        _terminate_process_group(proc)
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=1)
        for reader in readers:
            reader.join(timeout=1)
    return timed_out


def _bash(
    command: str,
    timeout: int = _BASH_TIMEOUT_MS_DEFAULT,
    description: Optional[str] = None,  # noqa: ARG001 — reference metadata field
    cwd: Optional[str] = None,
) -> str:
    """Execute a shell command (the reference ``Bash`` shape).

    ``command`` runs in its own session (``start_new_session=True``) so that on
    timeout the entire process *group* is killed — otherwise ``subprocess``
    kills only the spawned ``/bin/sh`` and leaves its children orphaned. Returns
    a multi-section string (command, exit code, stdout, stderr) capped at
    ``_BASH_MAX_OUTPUT_CHARS``. On timeout / spawn failure returns ``"Error: "``.

    ``timeout`` is in **milliseconds** (reference units); ``description`` is
    accepted for parity with the reference and is otherwise unused. Loose safety
    policy — see the "Safety" section of the skill_doc.
    """
    if not isinstance(command, str) or not command.strip():
        return "Error: command must be a non-empty string"
    if len(command) > _BASH_MAX_COMMAND_CHARS:
        return f"Error: command exceeds {_BASH_MAX_COMMAND_CHARS} characters"
    if isinstance(timeout, bool):
        return "Error: timeout must be an integer (milliseconds)"
    try:
        timeout_ms = int(timeout)
    except (TypeError, ValueError):
        return "Error: timeout must be an integer (milliseconds)"
    if not 1 <= timeout_ms <= _BASH_TIMEOUT_MS_MAX:
        return f"Error: timeout must be between 1 and {_BASH_TIMEOUT_MS_MAX} ms"
    timeout_s = max(1.0, timeout_ms / 1000.0)

    try:
        proc = subprocess.Popen(  # noqa: S602 — loose shell policy by design
            command,
            shell=True,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except (OSError, TypeError, ValueError) as exc:
        return f"Error executing command {command!r}: {exc}"

    assert proc.stdout is not None
    assert proc.stderr is not None
    stdout_capture = _BoundedPipeOutput(_BASH_MAX_OUTPUT_CHARS)
    stderr_capture = _BoundedPipeOutput(_BASH_MAX_OUTPUT_CHARS)
    readers = (
        threading.Thread(
            target=stdout_capture.drain,
            args=(proc.stdout,),
            daemon=True,
        ),
        threading.Thread(
            target=stderr_capture.drain,
            args=(proc.stderr,),
            daemon=True,
        ),
    )
    for reader in readers:
        reader.start()
    if _wait_for_bash(proc, readers, timeout_seconds=timeout_s):
        return f"Error: command timed out after {timeout_s:g}s: {command!r}"

    stdout = stdout_capture.text()
    stderr = stderr_capture.text()

    parts: List[str] = [f"$ {command}", f"(exit code: {proc.returncode})"]
    if stdout:
        parts.append(f"--- stdout ---\n{stdout.rstrip()}")
    if stderr:
        parts.append(f"--- stderr ---\n{stderr.rstrip()}")
    text = "\n".join(parts)

    if (
        stdout_capture.truncated
        or stderr_capture.truncated
        or len(text) > _BASH_MAX_OUTPUT_CHARS
    ):
        text = text[:_BASH_MAX_OUTPUT_CHARS] + "\n... (output truncated)"
    return text


_BASH_TOOL_DOC = """\
# bash

Execute a shell command (the mainstream Claude Code `Bash` shape). Anything
that doesn't fit `read` / `grep` / `glob`: `git log`, `find -newer`, `wc -l`,
test runners.

## Parameters

| Name | Type | Default | Description |
|------|------|---------|-------------|
| `command` | str | *required* | Shell command line to run (up to 32k chars). |
| `timeout` | int | 30000 | Wall-clock **milliseconds** before kill (1–600000). |
| `description` | str | null | Short human label (accepted for parity; unused). |
| `cwd` | str | null | Working directory for the command. |

## Output

`$ <cmd>` / `(exit code: N)` / stdout / stderr sections, capped while streaming
at 16k chars. On timeout or spawn failure, an `Error: ...` string.

## Safety

Runs `subprocess(command, shell=True)` with **no command filtering, no
allow/deny list, and no path jail** — loose by design. An in-process filter is
either too restrictive (blocks legitimate `pytest`/`git`) or trivially bypassed
(`bash -c '...'`), so the trust boundary is the *environment*: callers running
the agent on untrusted input MUST sandbox at the container / VM / process level.
`timeout` guards against hangs; a 16k-char retained-output cap guards against
runaway producers (`yes`, `seq`), and stdin is closed so commands cannot consume
the agent's protocol stream.

## Note vs the reference

`timeout` is in milliseconds (matching the reference units); `run_in_background`
is not supported (no background-task infrastructure), and `cwd` is added here
because this executor has no session `cd` persistence to rely on.
"""


def _build_bash_tool() -> ToolSpec:
    return ToolSpec(
        tool_id="bash",
        inputs=[
            ToolInputSpec(
                name="command",
                type_hint="str",
                required=True,
                description="Shell command line to execute.",
            ),
            ToolInputSpec(
                name="timeout",
                type_hint="int",
                required=False,
                default=_BASH_TIMEOUT_MS_DEFAULT,
                description=(
                    "Wall-clock milliseconds before kill "
                    f"(1–{_BASH_TIMEOUT_MS_MAX}; "
                    f"default {_BASH_TIMEOUT_MS_DEFAULT})."
                ),
            ),
            ToolInputSpec(
                name="description",
                type_hint="str",
                required=False,
                default=None,
                description="Short human-readable label for the command (optional).",
            ),
            ToolInputSpec(
                name="cwd",
                type_hint="str",
                required=False,
                default=None,
                description="Working directory for the command (optional).",
            ),
        ],
        executor_fn=_bash,
        defaults={"timeout": _BASH_TIMEOUT_MS_DEFAULT},
        tool_doc=_BASH_TOOL_DOC,
        output_type_hint="str",
        description=(
            "Execute a shell command; returns exit code plus stdout/stderr. "
            "Loose safety policy — sandbox untrusted input at the OS level."
        ),
    )


# ---------------------------------------------------------------------------
# Public API used by AgentRunner
# ---------------------------------------------------------------------------


def get_default_tool_specs() -> List[ToolSpec]:
    """Return fresh :class:`ToolSpec` objects for all default tools.

    Returns a new list on every call so callers can safely mutate it.
    """
    return [
        _build_read_tool(),
        _build_grep_tool(),
        _build_glob_tool(),
        _build_bash_tool(),
    ]


def ensure_default_tools_registered(registry: ToolRegistry) -> None:
    """Register default tools into *registry* if not already present.

    Safe to call multiple times (idempotent): skips any tool already in the
    registry. Called by :class:`~codenib.agent.runner.AgentRunner` during
    ``__init__`` so that the default tools (``read``, ``grep``, ``glob``,
    ``bash``) are available regardless of which ``Ax`` skill subset is loaded.
    """
    for spec in get_default_tool_specs():
        if not registry.has(spec.tool_id):
            registry.register(spec)
