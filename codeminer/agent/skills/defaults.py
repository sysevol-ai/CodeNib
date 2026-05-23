"""
Always-on default tool primitives: ``file_read`` and ``grep``.

These two tools are registered into ``AgentRunner``'s skill registry at
startup and are **never** removed by the ``exclude_skills`` argument — every
skill subset (A0–A6) builds on top of them.

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

import fnmatch
import re
from pathlib import Path
from typing import Any, List, Optional

from .core import Cost, SkillInputSpec, SkillMetadata, SkillOutputSpec, SkillType

# Canonical skill IDs for the always-on defaults.
DEFAULT_SKILL_IDS: frozenset = frozenset({"file_read", "grep"})

# Sensible token-safety caps.
_MAX_LINES_DEFAULT: int = 200
_MAX_RESULTS_DEFAULT: int = 50

# Directories to skip during recursive grep (avoids scanning VCS / cache noise).
_SKIP_DIR_PREFIXES = frozenset({
    ".git", ".hg", ".svn",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".tox", ".venv", "venv", "node_modules",
    ".cache", "dist", "build",
})


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
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            all_lines = fh.readlines()
    except FileNotFoundError:
        return f"Error: file not found: {path!r}"
    except OSError as exc:
        return f"Error reading {path!r}: {exc}"

    total = len(all_lines)
    if total == 0:
        return f"(empty file: {path})"

    # Clamp to valid 1-based range.
    start = max(1, int(start_line))
    end = int(end_line) if end_line is not None else total
    end = min(end, total)

    if start > total:
        return (
            f"Error: start_line {start} exceeds file length {total} in {path!r}"
        )
    if start > end:
        return f"Error: start_line {start} > end_line {end}"

    # Convert to 0-based slice.
    selected = all_lines[start - 1 : end]

    truncated_next: Optional[int] = None
    if len(selected) > max_lines:
        truncated_next = start + max_lines  # first omitted line (1-based)
        selected = selected[:max_lines]

    lines_out = [
        f"{lineno:6d} | {line.rstrip()}"
        for lineno, line in enumerate(selected, start=start)
    ]
    result = "\n".join(lines_out)

    if truncated_next is not None:
        remaining = end - truncated_next + 1
        result += (
            f"\n... ({remaining} more lines; "
            f"use start_line={truncated_next} to continue)"
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
# grep
# ---------------------------------------------------------------------------


def _grep(
    pattern: str,
    path: str = ".",
    include: Optional[str] = None,
    case_sensitive: bool = False,
    use_regex: bool = True,
    max_results: int = _MAX_RESULTS_DEFAULT,
) -> str:
    """Search files for a pattern, returning ``{file}:{lineno}: {content}`` lines.

    Args:
        pattern: Regex pattern or literal string.
        path: File or directory to search. Defaults to current directory.
        include: Glob filter applied to file *names* (e.g. ``*.py``).
        case_sensitive: Case-sensitive matching. Defaults to False.
        use_regex: Interpret ``pattern`` as a regex. Defaults to True.
        max_results: Hard cap on matches returned. Defaults to 50.

    Returns:
        One match per line: ``{relative_path}:{lineno}: {content}`` (1-based).
        Returns a no-match message when nothing is found.
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
                            file_path.relative_to(root)
                            if root.is_dir()
                            else file_path
                        )
                        results.append(f"{rel}:{lineno}: {line.rstrip()}")
                        if len(results) >= max_results:
                            return True
        except OSError:
            pass
        return False

    if root.is_file():
        _search_file(root)
    else:
        for file_path in sorted(root.rglob("*")):
            if not file_path.is_file():
                continue
            # Skip noise directories.
            if any(part in _SKIP_DIR_PREFIXES for part in file_path.parts):
                continue
            if include and not fnmatch.fnmatch(file_path.name, include):
                continue
            if _search_file(file_path):
                capped = True
                break

    if not results:
        return "No matches found."

    text = "\n".join(results)
    if capped:
        text += (
            f"\n... (max_results={max_results} reached; "
            "narrow your search with `include` or a more specific `pattern`)"
        )
    return text


_GREP_SKILL_DOC = """\
# grep

Search files for a regex pattern (or literal string), returning matches with
file paths and 1-based line numbers.

## Parameters

| Name | Type | Default | Description |
|------|------|---------|-------------|
| `pattern` | `str` | *(required)* | Regex pattern or literal string to search for. |
| `path` | `str` | `"."` | File or directory to search. |
| `include` | `str` | `null` | Glob filter for file names (e.g. `*.py`, `*.ts`). |
| `case_sensitive` | `bool` | `false` | Case-sensitive matching. |
| `use_regex` | `bool` | `true` | Treat `pattern` as regex; false = literal string. |
| `max_results` | `int` | `50` | Hard cap on matches returned. |

## Output

One match per line: `{relative_path}:{lineno}: {content}` (1-based numbers).

## When to Use

- Find all call sites of a function or class across the repo.
- Search for a string when no index is available.
- Narrow to specific file types with `include="*.py"`.
- Follow up with `file_read` to read surrounding context for a match.

## When NOT to Use

- Semantic / intent queries: use `embedding_search`.
- Ranked full-text retrieval: use `bm25_search`.
"""


def _build_grep_skill() -> SkillMetadata:
    return SkillMetadata(
        skill_id="grep",
        skill_type=SkillType.CUSTOM,
        inputs=[
            SkillInputSpec(
                name="pattern",
                type_hint="str",
                required=True,
                description="Regex pattern or literal string to search for.",
            ),
            SkillInputSpec(
                name="path",
                type_hint="str",
                required=False,
                default=".",
                description="File or directory to search (default: current directory).",
            ),
            SkillInputSpec(
                name="include",
                type_hint="str",
                required=False,
                default=None,
                description="Glob filter for file names (e.g. '*.py', '*.ts').",
            ),
            SkillInputSpec(
                name="case_sensitive",
                type_hint="bool",
                required=False,
                default=False,
                description="Case-sensitive matching (default: false).",
            ),
            SkillInputSpec(
                name="use_regex",
                type_hint="bool",
                required=False,
                default=True,
                description=(
                    "Interpret pattern as a regex (default: true). "
                    "Set to false for literal-string search."
                ),
            ),
            SkillInputSpec(
                name="max_results",
                type_hint="int",
                required=False,
                default=_MAX_RESULTS_DEFAULT,
                description=(
                    f"Hard cap on matches returned (default {_MAX_RESULTS_DEFAULT})."
                ),
            ),
        ],
        outputs=SkillOutputSpec(
            type_hint="str",
            description=(
                "Matches in `{file}:{lineno}: {content}` format (1-based numbers)."
            ),
        ),
        executor_fn=_grep,
        async_capable=False,
        cacheable=False,
        cost=Cost.LOW,
        dependencies=[],
        resources=[],
        defaults={
            "path": ".",
            "case_sensitive": False,
            "use_regex": True,
            "max_results": _MAX_RESULTS_DEFAULT,
        },
        skill_doc=_GREP_SKILL_DOC,
        description=(
            "Filesystem grep: search files for a pattern; returns "
            "file:lineno: content matches (1-based, no index required)."
        ),
    )


# ---------------------------------------------------------------------------
# Public API used by AgentRunner
# ---------------------------------------------------------------------------


def get_default_skill_metadata() -> List[SkillMetadata]:
    """Return fresh :class:`SkillMetadata` objects for all default tools.

    Returns a new list on every call so callers can safely mutate it.
    """
    return [_build_file_read_skill(), _build_grep_skill()]


def ensure_defaults_registered(registry: Any) -> None:
    """Register default skills into *registry* if not already present.

    Safe to call multiple times (idempotent): skips any skill already in the
    registry.  Called by :class:`~codeminer.agent.runner.AgentRunner` during
    ``__init__`` so that ``file_read`` and ``grep`` are available regardless
    of which ``Ax`` skill subset is loaded.
    """
    for meta in get_default_skill_metadata():
        if not registry.has(meta.skill_id):
            registry.register(meta)
