# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Notes anchored to code: what the code cannot tell a reader.

A note records a decision, a gotcha, an invariant or a contract, and names the
functions or classes it is about. Notes live in the repository under
``.codenib/notes/``, one Markdown file each, so they are reviewed and versioned
with the code::

    ---
    kind: gotcha
    anchors:
      - symbol: config/loader.py:Loader.__init__
        sha256: 3b1f...
    created: 2026-11-06
    ---
    Loader caches by path, so tests must clear it.

Each anchor stores a hash of its definition's source when the note was written
or last confirmed. When the definition changes, the note may be out of date:
the guard reports it until someone edits the note or confirms it.
"""

from __future__ import annotations

import ast
import datetime as _dt
import hashlib
import re
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import yaml

NOTES_DIR = Path(".codenib") / "notes"
KINDS = ("decision", "gotcha", "invariant", "contract", "note")
_FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.DOTALL)


class NoteError(ValueError):
    """A note or an anchor is malformed or cannot be resolved."""


@dataclass
class Anchor:
    symbol: str  # path:Qualified.name
    sha256: str

    @property
    def path(self) -> str:
        return self.symbol.split(":", 1)[0]


@dataclass
class Note:
    path: Path  # the note file, relative to the repository
    kind: str
    text: str
    anchors: List[Anchor] = field(default_factory=list)
    created: str = ""
    source: str = ""  # optional link to where the knowledge came from

    @property
    def title(self) -> str:
        return (
            self.text.strip().splitlines()[0] if self.text.strip() else self.path.stem
        )


@dataclass(frozen=True)
class AnchorStatus:
    anchor: Anchor
    state: str  # "current", "changed" or "missing"


def _definition(tree: ast.AST, qualname: str) -> Optional[ast.AST]:
    parts = qualname.split(".")
    scope: Sequence[ast.AST] = getattr(tree, "body", [])
    found: Optional[ast.AST] = None
    for part in parts:
        found = next(
            (
                node
                for node in scope
                if isinstance(
                    node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                )
                and node.name == part
            ),
            None,
        )
        if found is None:
            return None
        scope = found.body
    return found


def definition_hash(repo: Path, symbol: str) -> Optional[str]:
    """Hash of a definition's source, or ``None`` when it does not exist.

    The hash ignores indentation and trailing whitespace, so moving a function
    into a class or reformatting blank lines does not mark its notes stale.
    """

    path, _, qualname = symbol.partition(":")
    if not qualname:
        raise NoteError(f"anchor {symbol!r} must look like path/to/file.py:Name.method")
    try:
        source = (repo / path).read_text(encoding="utf-8")
        tree = ast.parse(source)
    except (OSError, SyntaxError, ValueError):
        return None
    node = _definition(tree, qualname)
    if node is None:
        return None
    lines = source.splitlines()
    first = min([node.lineno, *(d.lineno for d in getattr(node, "decorator_list", []))])
    body = textwrap.dedent("\n".join(lines[first - 1 : node.end_lineno]))
    normalized = "\n".join(line.rstrip() for line in body.splitlines() if line.strip())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def load_note(repo: Path, path: Path) -> Note:
    text = (repo / path).read_text(encoding="utf-8")
    match = _FRONT_MATTER.match(text.replace("\r\n", "\n"))
    if not match:
        raise NoteError(f"{path}: missing front matter")
    meta = yaml.safe_load(match.group(1)) or {}
    if not isinstance(meta, dict):
        raise NoteError(f"{path}: front matter must be a mapping")
    items = meta.get("anchors")
    if items is None:
        items = []
    if not isinstance(items, list):
        raise NoteError(f"{path}: anchors must be a list")
    anchors = []
    for item in items:
        if not isinstance(item, dict) or "symbol" not in item:
            raise NoteError(f"{path}: each anchor needs a symbol")
        anchors.append(Anchor(str(item["symbol"]), str(item.get("sha256") or "")))
    return Note(
        path=path,
        kind=str(meta.get("kind") or "note"),
        text=match.group(2).strip(),
        anchors=anchors,
        created=str(meta.get("created") or ""),
        source=str(meta.get("source") or ""),
    )


def load_notes(repo: Path) -> Tuple[List[Note], List[str]]:
    """Every note in the repository, and the errors of unreadable ones."""

    notes, errors = [], []
    directory = repo / NOTES_DIR
    for path in sorted(directory.glob("*.md")) if directory.is_dir() else ():
        try:
            notes.append(load_note(repo, path.relative_to(repo)))
        except (OSError, NoteError, yaml.YAMLError) as exc:
            errors.append(str(exc))
    return notes, errors


def _dump(note: Note) -> str:
    meta: Dict[str, object] = {"kind": note.kind}
    meta["anchors"] = [{"symbol": a.symbol, "sha256": a.sha256} for a in note.anchors]
    if note.created:
        meta["created"] = note.created
    if note.source:
        meta["source"] = note.source
    front = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{front}\n---\n{note.text.strip()}\n"


def _slug(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())[:6]
    return "-".join(words) or "note"


def add_note(
    repo: Path,
    text: str,
    symbols: Sequence[str],
    *,
    kind: str = "note",
    source: str = "",
) -> Note:
    if kind not in KINDS:
        raise NoteError(f"kind must be one of {', '.join(KINDS)}")
    if not symbols:
        raise NoteError("a note needs at least one --anchor")
    anchors = []
    for symbol in symbols:
        digest = definition_hash(repo, symbol)
        if digest is None:
            raise NoteError(f"no definition {symbol!r} in the working tree")
        anchors.append(Anchor(symbol, digest))
    directory = repo / NOTES_DIR
    directory.mkdir(parents=True, exist_ok=True)
    stem = _slug(text)
    path = directory / f"{stem}.md"
    counter = 2
    while path.exists():
        path = directory / f"{stem}-{counter}.md"
        counter += 1
    note = Note(
        path=path.relative_to(repo),
        kind=kind,
        text=text,
        anchors=anchors,
        created=_dt.date.today().isoformat(),
        source=source,
    )
    path.write_text(_dump(note), encoding="utf-8")
    return note


def confirm_note(repo: Path, note: Note) -> Note:
    """Re-record every anchor's hash: the note still holds for the current code."""

    for anchor in note.anchors:
        digest = definition_hash(repo, anchor.symbol)
        if digest is None:
            raise NoteError(
                f"{note.path}: {anchor.symbol} no longer exists; edit the anchor"
            )
        anchor.sha256 = digest
    (repo / note.path).write_text(_dump(note), encoding="utf-8")
    return note


def anchor_states(repo: Path, note: Note) -> List[AnchorStatus]:
    states = []
    for anchor in note.anchors:
        digest = definition_hash(repo, anchor.symbol)
        if digest is None:
            state = "missing"
        elif digest != anchor.sha256:
            state = "changed"
        else:
            state = "current"
        states.append(AnchorStatus(anchor, state))
    return states
