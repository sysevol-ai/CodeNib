# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Always-on default tool primitives: ``file_read`` and ``file_search``.

These two tools are registered into ``AgentRunner``'s skill registry at
startup and are **never** removed by the ``exclude_skills`` or
``allow_skills`` arguments — every skill subset (A0–A6) builds on top of
them.

Per #145, the search default is "grep / glob / bash-style primitives
(single tool or split — your call)". This module takes the **single
tool** route: ``file_search`` is one skill that dispatches via a
``mode`` argument:

- ``mode="content"`` *(default)* — grep-style regex over file contents.
- ``mode="files"`` — glob-style filename enumeration (``Path.rglob``).
- ``mode="shell"`` — bash-style shell command execution.

The tool is named ``file_search`` (not ``regex_search``) so it does not
collide with the existing index-backed ``regex_search`` retrieval skill
in ``skills/regex_search/`` — that one searches the in-memory node index
(``regex.retrieve``), this one scans the raw filesystem with no index.

The internal helpers ``_file_search_content`` / ``_file_search_files``
/ ``_file_search_shell`` implement each back-end; the public
``_file_search`` is the dispatcher referenced by
``_build_file_search_skill``. Shell mode has a loose safety policy
(``shell=True``, no allow/deny list); see the shell skill_doc for the
trust-boundary contract.

Reference design
----------------
Line-number format follows **opencode** (``{lineno:6d} | {content}``), as
requested in the code review on issue #145 by @siriuxyu:

    "Please make line numbers visible per row (opencode-style
    ``<lineno> | <content>``, or similar)."

The ``file_read`` input/output boundary convention uses **1-based** line
numbers throughout, aligned with the cross-cutting convention settled in
#147/#153.

Why opencode over openhands / codex?
- opencode's ``{n} | {line}`` format is the clearest visual separator for an
  LLM that needs to pick a line number for a follow-up call (e.g.
  ``graph_expand(ranges=[...])``) without having to count from the top of the
  snippet — the number is right there.
- openhands uses a similar scheme but with a different delimiter that is less
  scan-friendly in monospace output.
- codex embeds numbers in XML-like tags that add token overhead without
  readability benefit.

Related issues: #133 (Agent Router RFC), #145 (this PR), #147 (line-number
convention), #153 (1-based boundary).
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
from pathlib import Path
from typing import List, Optional

from .core import Cost, SkillInputSpec, SkillMetadata, SkillOutputSpec, SkillType
from .registry import SkillRegistry

# Canonical skill IDs for the always-on defaults.
DEFAULT_SKILL_IDS: frozenset[str] = frozenset({"file_read", "file_search"})

# Sensible token-safety caps.
_MAX_LINES_DEFAULT: int = 200
_MAX_RESULTS_DEFAULT: int = 50
_BASH_TIMEOUT_DEFAULT: int = 30  # seconds
_BASH_MAX_OUTPUT_CHARS: int = 16_000  # matches runner._MAX_RESULT_CHARS

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


# ---------------------------------------------------------------------------
# file_read
# ---------------------------------------------------------------------------


def _file_read(
    path: str,
    start_line: int = 1,
    end_line: Optional[int] = None,
    max_lines: int = _MAX_LINES_DEFAULT,
) -> str:
    """Read a file, returning its content with per-row 1-based line numbers.

    Output format::

           1 | def foo():
           2 |     return 42

    Args:
        path: Absolute or repo-relative path to the file.
        start_line: First line to read (1-based, inclusive). Defaults to 1.
        end_line: Last line to read (1-based, inclusive). Defaults to EOF.
        max_lines: Hard cap on lines returned for token safety. Defaults to
            200.  A truncation notice with the next ``start_line`` is appended
            when the cap is reached.

    Returns:
        Formatted string, or an error message prefixed with ``"Error: "``.
    """
    # Coerce inputs up front: a non-integer arg returns an Error string (the
    # module contract) instead of raising into the caller. `max_lines` is
    # floored at 1 so `max_lines=0` cannot emit a "0 lines; continue at the
    # same start_line" notice that loops forever.
    try:
        start = max(1, int(start_line))
        max_lines = max(1, int(max_lines))
        end = int(end_line) if end_line is not None else None
    except (TypeError, ValueError):
        return "Error: start_line, end_line, and max_lines must be integers"
    if end is not None and start > end:
        return f"Error: start_line {start} > end_line {end}"

    # Stream the file: keep at most `max_lines` lines in memory and merely
    # *count* any further requested lines (for the truncation notice) without
    # storing them, so a huge file is never fully materialised.
    selected: List[str] = []
    extra = 0  # requested lines present beyond the kept window
    total = 0
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for idx, line in enumerate(fh, start=1):
                total = idx
                if idx < start:
                    continue
                if end is not None and idx > end:
                    break
                if len(selected) < max_lines:
                    selected.append(line)
                else:
                    extra += 1
    except FileNotFoundError:
        return f"Error: file not found: {path!r}"
    except OSError as exc:
        return f"Error reading {path!r}: {exc}"

    if total == 0:
        return f"(empty file: {path})"
    if start > total:
        return f"Error: start_line {start} exceeds file length {total} in {path!r}"

    lines_out = [
        f"{lineno:6d} | {line.rstrip()}"
        for lineno, line in enumerate(selected, start=start)
    ]
    result = "\n".join(lines_out)

    if extra > 0:
        next_start = start + max_lines  # first omitted line (1-based)
        result += (
            f"\n... ({extra} more lines; "
            f"use start_line={next_start} to continue)"
        )

    return result


_FILE_READ_SKILL_DOC = """\
# file_read

Read a source file and return its content with per-row 1-based line numbers.

## Parameters

| Name | Type | Default | Description |
|------|------|---------|-------------|
| `path` | `str` | *(required)* | Absolute or repo-relative file path. |
| `start_line` | `int` | `1` | First line to read (1-based, inclusive). |
| `end_line` | `int` | EOF | Last line to read (1-based, inclusive). |
| `max_lines` | `int` | `200` | Hard cap on lines returned; prevents context blowup. |

## Output

Lines formatted as `{lineno:6d} | {content}` (opencode-style, 1-based).
A truncation notice with the next `start_line` is appended when `max_lines`
is reached.

## When to Use

- Inspect a function or class after a search skill narrowed the location.
- Read context lines around a match.
- Retrieve a known file at a specific line range.
- Follow up on a `grep` result to read surrounding code.

## Safety

`file_read` opens the `path` as given — there is **no path jail**. The
`path` description says "repo-relative", but this is not enforced: an
absolute path reads any file the process can (`/etc/shadow`,
`~/.ssh/id_rsa`, ...). As an always-on tool every agent turn can call,
this means untrusted query input or prompt injection could exfiltrate any
readable file. Callers running the agent on untrusted input MUST sandbox
at the container / VM / process level.
"""


def _build_file_read_skill() -> SkillMetadata:
    return SkillMetadata(
        skill_id="file_read",
        skill_type=SkillType.CUSTOM,
        inputs=[
            SkillInputSpec(
                name="path",
                type_hint="str",
                required=True,
                description="Absolute or repo-relative path to the file.",
            ),
            SkillInputSpec(
                name="start_line",
                type_hint="int",
                required=False,
                default=1,
                description="First line to read (1-based, inclusive). Defaults to 1.",
            ),
            SkillInputSpec(
                name="end_line",
                type_hint="int",
                required=False,
                default=None,
                description=(
                    "Last line to read (1-based, inclusive). "
                    "Defaults to end of file."
                ),
            ),
            SkillInputSpec(
                name="max_lines",
                type_hint="int",
                required=False,
                default=_MAX_LINES_DEFAULT,
                description=(
                    f"Hard cap on lines returned (default {_MAX_LINES_DEFAULT})."
                ),
            ),
        ],
        outputs=SkillOutputSpec(
            type_hint="str",
            description=(
                "File content with 1-based line numbers in "
                "`{lineno:6d} | {line}` format."
            ),
        ),
        executor_fn=_file_read,
        async_capable=False,
        cacheable=False,  # filesystem state may change between calls
        cost=Cost.LOW,
        dependencies=[],
        resources=[],
        defaults={"start_line": 1, "max_lines": _MAX_LINES_DEFAULT},
        skill_doc=_FILE_READ_SKILL_DOC,
        description=(
            "Read a file with 1-based line numbers (opencode-style); "
            "output is bounded by max_lines for token safety."
        ),
    )


# ---------------------------------------------------------------------------
# file_search — multi-mode primitive (content / files / shell)
#
# Per #145, the ``file_search`` slot is "grep / glob / bash-style primitives
# (single tool or split — your call)". This module takes the **single tool**
# route: one skill, three internal modes, dispatched by the ``mode`` argument.
# ---------------------------------------------------------------------------


def _file_search_content(
    pattern: str,
    path: str = ".",
    include: Optional[str] = None,
    case_sensitive: bool = False,
    use_regex: bool = True,
    max_results: int = _MAX_RESULTS_DEFAULT,
) -> str:
    """Grep-style content search.

    Returns one match per line: ``{relative_path}:{lineno}: {content}``
    (1-based). Returns a no-match message when nothing is found.
    """
    flags = 0 if case_sensitive else re.IGNORECASE
    if use_regex:
        try:
            compiled = re.compile(pattern, flags)
        except re.error as exc:
            return f"Error: invalid regex {pattern!r}: {exc}"
    else:
        compiled = re.compile(re.escape(pattern), flags)

    root = Path(path)
    if not root.exists():
        return f"Error: path {path!r} does not exist"

    results: List[str] = []
    capped = False

    def _search_file(file_path: Path) -> bool:
        """Append matches from *file_path*; return True if cap was hit."""
        try:
            with open(file_path, encoding="utf-8", errors="replace") as fh:
                for lineno, line in enumerate(fh, start=1):
                    if compiled.search(line):
                        rel = (
                            file_path.relative_to(root) if root.is_dir() else file_path
                        )
                        results.append(f"{rel}:{lineno}: {line.rstrip()}")
                        if len(results) >= max_results:
                            return True
        except OSError:
            # Best-effort search: a single unreadable file (permission denied,
            # broken symlink, encoding issue past errors="replace", etc.) must
            # not abort the overall scan. Silently skip and move on so the LLM
            # still sees the matches from other files.
            pass
        return False

    if root.is_file():
        _search_file(root)
    else:
        # Pass `include` straight to rglob so the OS filters file *names*
        # during traversal — avoids materialising the whole tree (a `sorted()`
        # over `rglob("*")` would allocate one Path per file before any match
        # check, and the max_results cap gives no protection against that).
        # Traversal order is filesystem-dependent, but content matches are
        # identified by `{file}:{lineno}`, so cross-file ordering is not
        # load-bearing. rglob is a generator, so a bad `include` glob raises
        # during iteration (ValueError/OSError/NotImplementedError) — wrap it.
        try:
            for file_path in root.rglob(include or "*"):
                if not file_path.is_file():
                    continue
                # Skip noise directories.
                if any(part in _SKIP_DIR_PREFIXES for part in file_path.parts):
                    continue
                if _search_file(file_path):
                    capped = True
                    break
        except (ValueError, OSError, NotImplementedError) as exc:
            return f"Error: invalid include glob {include!r}: {exc}"

    if not results:
        return "No matches found."

    text = "\n".join(results)
    if capped:
        text += (
            f"\n... (max_results={max_results} reached; "
            "narrow your search with `include` or a more specific `pattern`)"
        )
    return text


def _file_search_files(
    pattern: str,
    path: str = ".",
    max_results: int = _MAX_RESULTS_DEFAULT,
) -> str:
    """Glob-style filename enumeration under *path*.

    ``pattern`` follows ``Path.rglob`` syntax (e.g. ``"*.py"``,
    ``"**/test_*.py"``). Returns a sorted newline-separated list of relative
    paths, or a no-match message when empty.

    Ordering contract: the ``max_results`` cap is applied during traversal
    (filesystem order) and only the *returned* subset is sorted — so which
    files survive the cap is filesystem-dependent on large match sets. The
    output is sorted among returned results, not globally sorted.
    """
    root_path = Path(path)
    if not root_path.exists():
        return f"Error: path {path!r} does not exist"
    if not root_path.is_dir():
        return f"Error: files-mode root {path!r} is not a directory"

    matches: List[str] = []
    capped = False
    # rglob is a generator: a bad pattern (empty, absolute, unsupported glob)
    # raises during *iteration*, not at creation, and may raise
    # NotImplementedError as well as ValueError — so wrap the whole loop, not
    # just the rglob() call.
    try:
        for found in root_path.rglob(pattern):
            if not found.is_file():
                continue
            # Skip noise directories.
            if any(part in _SKIP_DIR_PREFIXES for part in found.parts):
                continue
            try:
                rel = found.relative_to(root_path)
            except ValueError:
                rel = found
            matches.append(str(rel))
            if len(matches) >= max_results:
                capped = True
                break
    except (ValueError, OSError, NotImplementedError) as exc:
        return f"Error: invalid glob pattern {pattern!r}: {exc}"

    if not matches:
        return f"No files match {pattern!r} under {path!r}."

    text = "\n".join(sorted(matches))
    if capped:
        text += (
            f"\n... (max_results={max_results} reached; "
            "narrow the pattern or raise max_results)"
        )
    return text


def _file_search_shell(
    command: str,
    cwd: Optional[str] = None,
    timeout: int = _BASH_TIMEOUT_DEFAULT,
) -> str:
    """Bash-style shell execution.

    ``command`` runs in its own session (``start_new_session=True``) so that on
    timeout the entire process *group* is killed — otherwise ``subprocess``
    kills only the spawned ``/bin/sh`` and leaves its children (the actual
    ``pytest`` / ``find`` / ...) orphaned and still running. Returns a
    multi-section string with the command, exit code, stdout, stderr; capped at
    ``_BASH_MAX_OUTPUT_CHARS``. On timeout / spawn failure returns
    ``"Error: ..."``.

    Loose safety policy — see the "Safety" section of the skill_doc.
    """
    try:
        proc = subprocess.Popen(  # noqa: S602 — loose shell policy by design
            command,
            shell=True,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        return f"Error executing command {command!r}: {exc}"

    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # Kill the whole process group, not just the shell, then reap.
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            proc.kill()
        proc.communicate()
        return f"Error: command timed out after {timeout}s: {command!r}"
    except (OSError, ValueError) as exc:
        proc.kill()
        proc.communicate()
        return f"Error executing command {command!r}: {exc}"

    parts: List[str] = [f"$ {command}", f"(exit code: {proc.returncode})"]
    if stdout:
        parts.append(f"--- stdout ---\n{stdout.rstrip()}")
    if stderr:
        parts.append(f"--- stderr ---\n{stderr.rstrip()}")
    text = "\n".join(parts)

    if len(text) > _BASH_MAX_OUTPUT_CHARS:
        text = text[:_BASH_MAX_OUTPUT_CHARS] + "\n... (output truncated)"
    return text


# ---------------------------------------------------------------------------
# Public dispatcher
# ---------------------------------------------------------------------------


_FILE_SEARCH_MODES = frozenset({"content", "files", "shell"})


def _file_search(
    pattern: str,
    mode: str = "content",
    path: str = ".",
    max_results: int = _MAX_RESULTS_DEFAULT,
    # content mode:
    include: Optional[str] = None,
    case_sensitive: bool = False,
    use_regex: bool = True,
    # shell mode:
    cwd: Optional[str] = None,
    timeout: int = _BASH_TIMEOUT_DEFAULT,
) -> str:
    """Multi-mode search primitive (the ``file_search`` slot in #145).

    The ``mode`` argument dispatches between three back-ends:

    - ``"content"`` (default) — grep-style regex over file *contents*. Uses
      ``path``, ``include``, ``case_sensitive``, ``use_regex``, ``max_results``.
    - ``"files"`` — glob-style enumeration of file *names*. Uses ``pattern``
      as a ``Path.rglob`` glob, plus ``path`` (root) and ``max_results``.
    - ``"shell"`` — execute ``pattern`` as a shell command line. Uses
      ``cwd`` and ``timeout``. Loose safety policy.

    Mode-irrelevant parameters are accepted (the SkillMetadata is one flat
    schema) but silently ignored.
    """
    if mode not in _FILE_SEARCH_MODES:
        return (
            f"Error: invalid mode {mode!r}; "
            f"expected one of {sorted(_FILE_SEARCH_MODES)}"
        )
    # Coerce numeric args: a non-integer (e.g. an LLM emitting "5") returns an
    # Error string rather than raising a TypeError into the caller.
    try:
        max_results = max(1, int(max_results))
        timeout = max(1, int(timeout))
    except (TypeError, ValueError):
        return "Error: max_results and timeout must be integers"
    if mode == "content":
        return _file_search_content(
            pattern=pattern,
            path=path,
            include=include,
            case_sensitive=case_sensitive,
            use_regex=use_regex,
            max_results=max_results,
        )
    if mode == "files":
        return _file_search_files(pattern=pattern, path=path, max_results=max_results)
    # mode == "shell"
    return _file_search_shell(command=pattern, cwd=cwd, timeout=timeout)


_FILE_SEARCH_SKILL_DOC = """\
# file_search

Multi-mode search primitive — pick a `mode` to choose the back-end. This
is the always-on search default from #145 / #133, bundling
grep / glob / bash-style search into one tool per #145's "single tool"
option. It scans the raw filesystem (no index); for index-backed regex
retrieval over parsed nodes, use the separate `regex_search` skill.

## Modes

- `"content"` *(default)* — grep-style regex over file contents.
  Returns `{file}:{lineno}: {content}` lines (1-based).
- `"files"` — glob-style filename enumeration (`Path.rglob`).
  Returns a sorted newline-separated list of paths.
- `"shell"` — execute the pattern as a shell command line.
  Returns `$ <cmd>` / exit code / stdout / stderr.

## Parameters

| Name | Type | Default | Used by | Description |
|------|------|---------|---------|-------------|
| `pattern` | str | *required* | all | Regex / glob / shell command. |
| `mode` | str | `content` | all | `content` / `files` / `shell`. |
| `path` | str | `.` | content, files | Directory (or file) to search. |
| `max_results` | int | 50 | content, files | Cap on matches / paths. |
| `include` | str | null | content | Glob filter for file *names*. |
| `case_sensitive` | bool | false | content | Case-sensitive matching. |
| `use_regex` | bool | true | content | Regex (true) vs literal (false). |
| `cwd` | str | null | shell | Working dir for the command. |
| `timeout` | int | 30 | shell | Wall-clock seconds before kill. |

## When to Use which mode

- **`content`** — find call sites, error strings, identifier usage anywhere
  in the repo without an index. Narrow by `include="*.py"`.
- **`files`** — enumerate files matching a structure (`**/__init__.py`,
  `**/*.proto`); confirm a file exists before `file_read`.
- **`shell`** — anything that doesn't fit the above (`pytest`, `git log`,
  `find . -newer ...`, `wc -l`). See Safety below.

## Safety (shell mode)

Shell mode runs `subprocess.run(command, shell=True)` with **no command
filtering, no allow/deny list, and no path jail** — loose by design. An
in-process filter is either too restrictive (blocks legitimate `pytest` /
`git`) or trivially bypassed (`bash -c '...'`, `eval`), so the trust
boundary is the *environment*: callers running the agent on untrusted
input MUST sandbox at the container / VM / process level. `timeout`
(default 30s) guards against hangs; a 16k-char output cap guards against
runaway producers (`yes`, `seq`).

## When NOT to Use

- Single file content — prefer `file_read` (predictable, line-numbered).
- Index-backed regex over parsed nodes — use the `regex_search` skill.
- Semantic / intent queries — use `embedding_search`.
- Ranked full-text retrieval — use `bm25_search`.
"""


def _build_file_search_skill() -> SkillMetadata:
    return SkillMetadata(
        skill_id="file_search",
        skill_type=SkillType.CUSTOM,
        inputs=[
            SkillInputSpec(
                name="pattern",
                type_hint="str",
                required=True,
                description=(
                    "Mode-dependent: regex (content) / glob (files) / "
                    "shell command (shell)."
                ),
            ),
            SkillInputSpec(
                name="mode",
                type_hint="str",
                required=False,
                default="content",
                description=(
                    "Back-end to dispatch to: 'content' (grep), 'files' "
                    "(glob), 'shell' (bash). Defaults to 'content'."
                ),
            ),
            SkillInputSpec(
                name="path",
                type_hint="str",
                required=False,
                default=".",
                description=(
                    "Directory (or file, content mode) to search. "
                    "Ignored in shell mode (use cwd)."
                ),
            ),
            SkillInputSpec(
                name="max_results",
                type_hint="int",
                required=False,
                default=_MAX_RESULTS_DEFAULT,
                description=(
                    "Hard cap on matches/paths returned "
                    f"(content / files modes; default {_MAX_RESULTS_DEFAULT})."
                ),
            ),
            SkillInputSpec(
                name="include",
                type_hint="str",
                required=False,
                default=None,
                description=("Content mode: glob filter for file names (e.g. '*.py')."),
            ),
            SkillInputSpec(
                name="case_sensitive",
                type_hint="bool",
                required=False,
                default=False,
                description="Content mode: case-sensitive matching.",
            ),
            SkillInputSpec(
                name="use_regex",
                type_hint="bool",
                required=False,
                default=True,
                description=(
                    "Content mode: treat pattern as regex (true) or "
                    "literal string (false)."
                ),
            ),
            SkillInputSpec(
                name="cwd",
                type_hint="str",
                required=False,
                default=None,
                description="Shell mode: working directory for the command.",
            ),
            SkillInputSpec(
                name="timeout",
                type_hint="int",
                required=False,
                default=_BASH_TIMEOUT_DEFAULT,
                description=(
                    f"Shell mode: wall-clock seconds before kill "
                    f"(default {_BASH_TIMEOUT_DEFAULT})."
                ),
            ),
        ],
        outputs=SkillOutputSpec(
            type_hint="str",
            description=(
                "Mode-dependent text: grep-style matches, sorted paths, "
                "or shell stdout/stderr/exit code."
            ),
        ),
        executor_fn=_file_search,
        async_capable=False,
        cacheable=False,
        cost=Cost.LOW,
        dependencies=[],
        resources=[],
        defaults={
            "mode": "content",
            "path": ".",
            "case_sensitive": False,
            "use_regex": True,
            "max_results": _MAX_RESULTS_DEFAULT,
            "timeout": _BASH_TIMEOUT_DEFAULT,
        },
        skill_doc=_FILE_SEARCH_SKILL_DOC,
        description=(
            "Multi-mode search: grep-style content (default), glob-style "
            "filename enumeration, or shell command execution. One tool, "
            "three modes via `mode` argument."
        ),
    )


# ---------------------------------------------------------------------------
# Public API used by AgentRunner
# ---------------------------------------------------------------------------


def get_default_skill_metadata() -> List[SkillMetadata]:
    """Return fresh :class:`SkillMetadata` objects for all default tools.

    Returns a new list on every call so callers can safely mutate it.
    """
    return [_build_file_read_skill(), _build_file_search_skill()]


def ensure_defaults_registered(registry: SkillRegistry) -> None:
    """Register default skills into *registry* if not already present.

    Safe to call multiple times (idempotent): skips any skill already in the
    registry.  Called by :class:`~codeminer.agent.runner.AgentRunner` during
    ``__init__`` so that the default tools (``file_read``, ``file_search``)
    are available regardless of which ``Ax`` skill subset is loaded.
    """
    for meta in get_default_skill_metadata():
        if not registry.has(meta.skill_id):
            registry.register(meta)
