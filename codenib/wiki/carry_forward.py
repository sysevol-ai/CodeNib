# SPDX-FileCopyrightText: 2026 CodeNib Contributors
# SPDX-License-Identifier: Apache-2.0

"""Carry a published Wiki's unaffected pages forward to a new commit.

Every cached page is keyed by its commit and source fingerprint, so a push
would otherwise rewrite the whole Wiki. After a Wiki passes its audit, its
outline and ready pages are saved beside it. On the next commit, a page whose
recorded source files or cited symbols are all unchanged is copied under the
new commit's key; only the others, and anything never generated, are written
by the model.

The outline is kept as it was, so new areas of the code appear only after a
full rebuild. A snapshot is used only when its prompt versions match and Git
can name every file changed since its commit; otherwise the Wiki is rebuilt.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

SNAPSHOT_NAME = "carry-forward.json"
SNAPSHOT_VERSION = 1
# Keys whose string values name repository files in a page's payload.
_FILE_KEYS = frozenset(
    {
        "file",
        "anchor_file",
        "source_file",
        "caller_file",
        "callee_file",
        "spelling_file",
        "artifact_path",
        "path",
    }
)
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class CarryForward:
    """What a carry-forward reused, and why it could not reuse more."""

    previous_commit: str = ""
    outline_reused: bool = False
    reused: list[str] = field(default_factory=list)
    rewritten: list[str] = field(default_factory=list)
    reason: str = ""


@dataclass(frozen=True)
class SymbolSpan:
    """Enclosing symbol definition span in source code."""

    name: str
    qualname: str
    start_line: int
    end_line: int
    kind: str


@dataclass
class FileDiffHunk:
    """Line ranges changed between two commits."""

    old_start: int
    old_count: int
    new_start: int
    new_count: int


@dataclass
class FileChangeStatus:
    """Impact analysis of changes within a modified file."""

    module_changed: bool = False
    changed_symbols: set[str] = field(default_factory=set)
    existing_symbols: set[str] = field(default_factory=set)


def _prompt_versions() -> dict[str, str]:
    from .agent_wiki import _OUTLINE_PROMPT_VERSION, _PAGE_PROMPT_VERSION

    return {"outline": _OUTLINE_PROMPT_VERSION, "page": _PAGE_PROMPT_VERSION}


def _flatten(pages: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    flat: list[Mapping[str, Any]] = []
    for page in pages:
        if isinstance(page, Mapping):
            flat.append(page)
            flat.extend(_flatten(page.get("children") or []))
    return flat


def _page_meta(builder: Any, page_id: str, pages: list) -> Optional[dict]:
    meta = builder._find(page_id, pages)
    if meta is not None and page_id == "overview":
        meta = builder._overview_page_meta(meta, pages[1:])
    return meta


def page_files(page: Any) -> set[str]:
    """Repository-relative files a page's citations and evidence name."""

    files: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if key in _FILE_KEYS and isinstance(item, str):
                    path = item.strip()
                    while path.startswith("./"):
                        path = path[2:]
                    if path and not path.startswith("/") and ".." not in path:
                        files.add(path)
                else:
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(page)
    return files


def page_symbols(page: Any) -> dict[str, set[str]]:
    """Repository-relative files to cited symbol names/anchors in page payload."""

    symbols_by_file: dict[str, set[str]] = {}

    def add_symbol(file_path: Optional[str], sym: Optional[str]) -> None:
        if not sym or not isinstance(sym, str):
            return
        sym = sym.strip()
        if not sym:
            return
        if ":" in sym:
            candidate_file, _, cand_sym = sym.rpartition(":")
            candidate_file = candidate_file.strip()
            cand_sym = cand_sym.strip()
            if candidate_file and ("/" in candidate_file or candidate_file.endswith(".py") or file_path is None):
                file_path = candidate_file
                sym = cand_sym
        if not file_path or not isinstance(file_path, str):
            return
        path = file_path.strip().replace("\\", "/")
        while path.startswith("./"):
            path = path[2:]
        if not path or path.startswith("/") or ".." in path:
            return
        cleaned_sym = sym.strip()
        while cleaned_sym.endswith("()"):
            cleaned_sym = cleaned_sym[:-2].strip()
        if cleaned_sym.startswith("::"):
            cleaned_sym = cleaned_sym[2:].strip()
        if cleaned_sym:
            symbols_by_file.setdefault(path, set()).add(cleaned_sym)

    def visit(value: Any, current_file: Optional[str] = None) -> None:
        if isinstance(value, Mapping):
            file_in_mapping = None
            for k in _FILE_KEYS:
                if k in value and isinstance(value[k], str):
                    f = value[k].strip()
                    while f.startswith("./"):
                        f = f[2:]
                    if f and not f.startswith("/") and ".." not in f:
                        file_in_mapping = f
                        break
            effective_file = file_in_mapping or current_file

            for sym_key in ("symbol", "node_name", "anchor", "name"):
                if sym_key in value:
                    item = value[sym_key]
                    if isinstance(item, str):
                        add_symbol(effective_file, item)
                    elif isinstance(item, (list, tuple, set)):
                        for s in item:
                            if isinstance(s, str):
                                add_symbol(effective_file, s)

            for plural_key in ("symbols", "anchors"):
                if plural_key in value:
                    item = value[plural_key]
                    if isinstance(item, Mapping):
                        for k, v in item.items():
                            if isinstance(v, str):
                                add_symbol(k, v)
                            elif isinstance(v, (list, tuple, set)):
                                for s in v:
                                    if isinstance(s, str):
                                        add_symbol(k, s)
                    elif isinstance(item, (list, tuple, set)):
                        for s in item:
                            if isinstance(s, str):
                                add_symbol(effective_file, s)
                            elif isinstance(s, Mapping):
                                visit(s, effective_file)

            for k, v in value.items():
                if k not in ("symbols", "anchors"):
                    visit(v, effective_file)

        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item, current_file)

    visit(page)
    return symbols_by_file


def parse_symbols(source: str) -> Optional[list[SymbolSpan]]:
    """Extract enclosing AST function and class symbol spans from Python source."""

    try:
        if source.startswith("\ufeff"):
            source = source[1:]
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    spans: list[SymbolSpan] = []

    def visit(nodes: list[ast.AST], prefix: str = "") -> None:
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qualname = f"{prefix}.{node.name}" if prefix else node.name
                start = min(
                    [node.lineno, *(d.lineno for d in getattr(node, "decorator_list", []))]
                )
                end = getattr(node, "end_lineno", None) or node.lineno
                spans.append(
                    SymbolSpan(
                        name=node.name,
                        qualname=qualname,
                        start_line=start,
                        end_line=end,
                        kind=type(node).__name__,
                    )
                )
                visit(getattr(node, "body", []), prefix=qualname)

    visit(getattr(tree, "body", []))
    return spans


def parse_diff_hunks(diff_text: str) -> dict[str, list[FileDiffHunk]]:
    """Parse git diff -U0 output into file paths and their hunks."""

    files: dict[str, list[FileDiffHunk]] = {}
    current_file: Optional[str] = None
    old_path: Optional[str] = None

    for line in diff_text.splitlines():
        if line.startswith("--- "):
            source = line[4:].split("\t", 1)[0].strip()
            old_path = None if source == "/dev/null" else (source[2:] if source.startswith("a/") else source)
            continue
        if line.startswith("+++ "):
            target = line[4:].split("\t", 1)[0].strip()
            new_path = None if target == "/dev/null" else (target[2:] if target.startswith("b/") else target)
            current_file = new_path or old_path
            if current_file and current_file not in files:
                files[current_file] = []
            continue
        if line.startswith("@@ ") and current_file is not None:
            match = _HUNK.match(line)
            if match:
                old_start = int(match.group(1))
                old_count = 1 if match.group(2) is None else int(match.group(2))
                new_start = int(match.group(3))
                new_count = 1 if match.group(4) is None else int(match.group(4))
                files[current_file].append(
                    FileDiffHunk(
                        old_start=old_start,
                        old_count=old_count,
                        new_start=new_start,
                        new_count=new_count,
                    )
                )
    return files


def git_show(repo: Path, commit: str, path: str) -> Optional[str]:
    """Retrieve file content from git object database at commit."""

    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "-c", "core.quotepath=false", "show", f"{commit}:{path}"],
            capture_output=True,
            encoding="utf-8",
            errors="surrogateescape",
            check=False,
            timeout=30,
        )
        if proc.returncode == 0:
            return proc.stdout
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def analyze_changed_files(
    repo: Path, old: str, new: str, modified_files: set[str]
) -> dict[str, FileChangeStatus]:
    """Analyze whether modified files had module-level changes or symbol modifications."""

    statuses: dict[str, FileChangeStatus] = {}
    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "-c",
                "core.quotepath=false",
                "diff",
                "-U0",
                "--no-renames",
                old,
                new,
            ],
            capture_output=True,
            encoding="utf-8",
            errors="surrogateescape",
            timeout=120,
            check=False,
        )
        diff_text = proc.stdout if proc.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        diff_text = ""

    all_hunks = parse_diff_hunks(diff_text)
    for f in modified_files:
        if not f.endswith((".py", ".pyw", ".pyi")):
            statuses[f] = FileChangeStatus(module_changed=True)
            continue
        old_source = git_show(repo, old, f)
        new_source = git_show(repo, new, f)
        if old_source is None or new_source is None:
            statuses[f] = FileChangeStatus(module_changed=True)
            continue
        old_spans = parse_symbols(old_source)
        new_spans = parse_symbols(new_source)
        if old_spans is None or new_spans is None:
            statuses[f] = FileChangeStatus(module_changed=True)
            continue

        hunks = all_hunks.get(f, [])
        changed_symbols: set[str] = set()
        module_changed = False
        old_lines = old_source.splitlines()
        new_lines = new_source.splitlines()

        for h in hunks:
            if h.old_count > 0:
                for L in range(h.old_start, h.old_start + h.old_count):
                    if 1 <= L <= len(old_lines):
                        if not old_lines[L - 1].strip():
                            continue
                        enclosing = [s for s in old_spans if s.start_line <= L <= s.end_line]
                        if enclosing:
                            for s in enclosing:
                                changed_symbols.add(s.name)
                                changed_symbols.add(s.qualname)
                        else:
                            module_changed = True
            if h.new_count > 0:
                for L in range(h.new_start, h.new_start + h.new_count):
                    if 1 <= L <= len(new_lines):
                        if not new_lines[L - 1].strip():
                            continue
                        enclosing = [s for s in new_spans if s.start_line <= L <= s.end_line]
                        if enclosing:
                            for s in enclosing:
                                changed_symbols.add(s.name)
                                changed_symbols.add(s.qualname)
                        else:
                            module_changed = True

        existing_symbols = {s.name for s in new_spans} | {s.qualname for s in new_spans}
        statuses[f] = FileChangeStatus(
            module_changed=module_changed,
            changed_symbols=changed_symbols,
            existing_symbols=existing_symbols,
        )
    return statuses


def changed_symbols(repo: Path, old: str, new: str) -> Optional[dict[str, set[str]]]:
    """Changed AST symbols per file between two commits, or None if Git cannot say."""

    files = changed_files(repo, old, new)
    if files is None:
        return None
    statuses = analyze_changed_files(repo, old, new, files)
    return {f: status.changed_symbols for f, status in statuses.items()}


def changed_files(repo: Path, old: str, new: str) -> Optional[set[str]]:
    """Files that differ between two commits, or None if Git cannot say."""

    if not old or not new:
        return None
    if old == new:
        return set()
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "diff",
                "--name-only",
                "-z",
                "--no-renames",
                old,
                new,
            ],
            capture_output=True,
            encoding="utf-8",
            errors="surrogateescape",
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return {path for path in result.stdout.split("\0") if path}


def save_snapshot(builder: Any, path: Path, commit: str) -> int:
    """Save a passed Wiki's outline and ready pages; return the page count."""

    outline = builder.cached_outline()
    if outline is None:
        raise ValueError("there is no cached outline to carry forward")
    pages = {}
    for meta in _flatten(outline.get("pages") or []):
        page_id = str(meta.get("id") or "")
        page = builder.cached_page(page_id)
        if page is not None:
            pages[page_id] = page
    payload = {
        "version": SNAPSHOT_VERSION,
        "commit": commit,
        "prompt_versions": _prompt_versions(),
        "outline": outline,
        "pages": pages,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)
    return len(pages)


def carry_forward(
    builder: Any,
    path: Path,
    repo: Path,
    commit: str,
    *,
    changed: Optional[set[str]] = None,
) -> CarryForward:
    """Copy the snapshot's unaffected pages under the builder's cache key.

    ``changed`` names the files that changed since the snapshot when Git
    cannot (edits in the working tree that are not committed); otherwise Git
    compares the snapshot's commit with ``commit``.
    """

    result = CarryForward()
    if not path.is_file():
        result.reason = "no earlier Wiki"
        return result
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        result.reason = "the earlier Wiki could not be read"
        return result
    if not isinstance(snapshot, dict) or snapshot.get("version") != SNAPSHOT_VERSION:
        result.reason = "the earlier Wiki has another format"
        return result
    result.previous_commit = str(snapshot.get("commit") or "")
    if snapshot.get("prompt_versions") != _prompt_versions():
        result.reason = "the Wiki prompts changed"
        return result
    if changed is None:
        changed = changed_files(repo, result.previous_commit, commit)
    if changed is None:
        result.reason = (
            f"Git cannot compare {result.previous_commit[:12]} with {commit[:12]}"
        )
        return result
    outline = snapshot.get("outline")
    pages = snapshot.get("pages")
    if not isinstance(outline, dict) or not isinstance(pages, dict):
        result.reason = "the earlier Wiki is incomplete"
        return result
    tree = outline.get("pages")
    if not isinstance(tree, list) or builder.cached_outline() is not None:
        result.reason = "this commit already has an outline"
        return result
    builder._write_cache("outline", outline)
    result.outline_reused = True

    file_statuses = (
        analyze_changed_files(repo, result.previous_commit, commit, changed)
        if changed
        else {}
    )

    for meta in _flatten(tree):
        page_id = str(meta.get("id") or "")
        page = pages.get(page_id)
        symbols_by_file = page_symbols(page) if isinstance(page, dict) else {}
        if isinstance(meta, dict):
            for f, syms in page_symbols(meta).items():
                symbols_by_file.setdefault(f, set()).update(syms)

        files = (page_files(page) if isinstance(page, dict) else set()) | set(
            symbols_by_file.keys()
        )
        # A page that names no file cannot show it is unaffected.
        if not files:
            result.rewritten.append(page_id)
            continue

        touched = files & changed
        if touched:
            can_carry = True
            for f in touched:
                cited_syms = symbols_by_file.get(f)
                if not cited_syms:
                    can_carry = False
                    break
                status = file_statuses.get(f)
                if status is None or status.module_changed:
                    can_carry = False
                    break
                for sym in cited_syms:
                    if sym in status.changed_symbols:
                        can_carry = False
                        break
                    if (
                        sym not in status.existing_symbols
                        and sym.split(".")[-1] not in status.existing_symbols
                    ):
                        can_carry = False
                        break
                if not can_carry:
                    break
            if not can_carry:
                result.rewritten.append(page_id)
                continue

        suffix_meta = _page_meta(builder, page_id, tree)
        if suffix_meta is None:
            result.rewritten.append(page_id)
            continue
        builder._write_cache(builder._page_cache_suffix(suffix_meta), page)
        result.reused.append(page_id)
    return result


__all__ = [
    "SNAPSHOT_NAME",
    "SNAPSHOT_VERSION",
    "CarryForward",
    "FileChangeStatus",
    "FileDiffHunk",
    "SymbolSpan",
    "carry_forward",
    "changed_files",
    "changed_symbols",
    "page_files",
    "page_symbols",
    "parse_diff_hunks",
    "parse_symbols",
    "save_snapshot",
]
