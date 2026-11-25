# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""One annotation per Markdown file, in the guard's note format.

A guard note is front matter (``kind``, ``anchors``, ``created``) and a text.
An annotation keeps those keys, so ``codenib guard`` reads the same directory,
and adds its own::

    ---
    id: 01JDX3Q8V6M2N4P5R7S9T1W3Y5
    kind: question
    author: human
    status: draft
    on:
      page: request-lifecycle
      section: Sending a request
      quote: Session.send picks the adapter
      prefix: "Once prepared, "
      suffix: " by URL prefix."
    anchors: []
    created: 2026-11-23T19:40:05Z
    ---
    Why does the longest prefix win here?

``on`` says where the annotation sits: a page, and inside it the quoted words
with a little text on either side, so the quote can be found again after the
page is rewritten. Agent answers carry ``reply_to`` (the annotation they
answer), ``task`` (the run that wrote them) and ``files`` (the code they cite).
"""

from __future__ import annotations

import datetime as _dt
import os
import re
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import yaml

NOTES_DIR = Path(".codenib") / "notes"

#: What a person writes. ``note`` is a standing fact that later tasks must
#: respect; the guard's own kinds are notes too.
HUMAN_KINDS = ("question", "change", "note")
#: What the agent writes back: an answer to a question, or the summary of a
#: change it made.
AGENT_KINDS = ("answer", "summary")
ANNOTATION_KINDS = HUMAN_KINDS + AGENT_KINDS
#: Kinds the guard skips: they are conversation, not facts about the code.
CONVERSATION_KINDS = ("question", "change", "answer", "summary")

STATUSES = (
    "draft",  # written, not sent
    "open",  # a note or an agent result that stands
    "dispatched",  # sent in a task that has not finished
    "answered",  # a question with an answer
    "needs_review",  # a change whose diff waits for the person
    "applied",  # a change whose diff the person accepted
    "rejected",  # a change whose diff the person rejected
    "failed",  # the task ended without a result for it
    "resolved",  # closed by the person
)

_FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.DOTALL)
_ID = re.compile(r"\A[0-9A-HJKMNP-TV-Z]{26}\Z")
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_KNOWN_KEYS = {
    "id",
    "kind",
    "author",
    "status",
    "on",
    "reply_to",
    "task",
    "files",
    "anchors",
    "created",
    "updated",
    "source",
    "anchor_state",
}
_ON_TEXT_KEYS = {
    "page": 200,
    "section": 300,
    "quote": 2000,
    "prefix": 200,
    "suffix": 200,
    "block": 4000,
    "path": 1000,
    "commit": 64,  # the commit a code annotation was made at, to follow renames
    "before": 600,  # the lines above annotated code, to tell repeated code apart
    "after": 600,  # the lines below it
}
MAX_TEXT = 20_000


class AnnotationError(ValueError):
    """An annotation is malformed, unknown, or in the wrong state."""


def new_id(now: Optional[float] = None) -> str:
    """A ULID: 48 bits of milliseconds then 80 random bits, sortable by time."""

    millis = int((time.time() if now is None else now) * 1000)
    value = (millis << 80) | secrets.randbits(80)
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD[value & 31])
        value >>= 5
    return "".join(reversed(chars))


def is_id(value: str) -> bool:
    return bool(_ID.match(value or ""))


def utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean_on(on: Any) -> Dict[str, Any]:
    if on is None:
        return {}
    if not isinstance(on, dict):
        raise AnnotationError("'on' must be a mapping")
    clean: Dict[str, Any] = {}
    for key, limit in _ON_TEXT_KEYS.items():
        value = on.get(key)
        if value is None or value == "":
            continue
        if not isinstance(value, str):
            raise AnnotationError(f"on.{key} must be text")
        clean[key] = value[:limit]
    for key in ("start_line", "end_line"):
        value = on.get(key)
        if value is None:
            continue
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise AnnotationError(f"on.{key} must be a positive line number")
        clean[key] = value
    if "path" in clean and (
        clean["path"].startswith("/") or ".." in Path(clean["path"]).parts
    ):
        raise AnnotationError("on.path must be a path inside the repository")
    return clean


def _clean_files(files: Any) -> List[Dict[str, Any]]:
    if not files:
        return []
    if not isinstance(files, list):
        raise AnnotationError("files must be a list")
    clean = []
    for item in files[:50]:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise AnnotationError("each file needs a path")
        path = Path(item["path"])
        if path.is_absolute() or ".." in path.parts:
            raise AnnotationError("a file must be a path inside the repository")
        entry: Dict[str, Any] = {"path": item["path"][:1000]}
        for key in ("start_line", "end_line"):
            value = item.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                entry[key] = value
        clean.append(entry)
    return clean


@dataclass
class Annotation:
    id: str
    kind: str
    text: str
    author: str = "human"
    status: str = "draft"
    on: Dict[str, Any] = field(default_factory=dict)
    reply_to: str = ""
    task: str = ""
    files: List[Dict[str, Any]] = field(default_factory=list)
    anchors: List[Dict[str, Any]] = field(default_factory=list)
    created: str = ""
    updated: str = ""
    #: Where code it sits on stands now: current, moved, changed or detached.
    anchor_state: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def page(self) -> str:
        return str(self.on.get("page") or "")

    def validate(self) -> "Annotation":
        if not is_id(self.id):
            raise AnnotationError(f"bad annotation id {self.id!r}")
        if self.kind not in ANNOTATION_KINDS:
            raise AnnotationError(
                f"kind must be one of {', '.join(ANNOTATION_KINDS)}, not {self.kind!r}"
            )
        if self.author not in ("human", "agent"):
            raise AnnotationError("author must be human or agent")
        if self.status not in STATUSES:
            raise AnnotationError(f"unknown status {self.status!r}")
        if self.reply_to and not is_id(self.reply_to):
            raise AnnotationError("reply_to must be an annotation id")
        if self.task and not is_id(self.task):
            raise AnnotationError("task must be a task id")
        if not isinstance(self.text, str) or not self.text.strip():
            raise AnnotationError("an annotation needs text")
        if len(self.text) > MAX_TEXT:
            raise AnnotationError(f"text is longer than {MAX_TEXT} characters")
        self.on = _clean_on(self.on)
        self.files = _clean_files(self.files)
        return self

    def to_json(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "text": self.text,
            "author": self.author,
            "status": self.status,
            "on": dict(self.on),
            "reply_to": self.reply_to or None,
            "task": self.task or None,
            "files": list(self.files),
            "created": self.created,
            "updated": self.updated or self.created,
            "anchor_state": self.anchor_state or None,
        }

    def dump(self) -> str:
        meta: Dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "author": self.author,
            "status": self.status,
        }
        if self.on:
            meta["on"] = self.on
        if self.reply_to:
            meta["reply_to"] = self.reply_to
        if self.task:
            meta["task"] = self.task
        if self.files:
            meta["files"] = self.files
        if self.anchor_state:
            meta["anchor_state"] = self.anchor_state
        meta["anchors"] = self.anchors
        meta["created"] = self.created
        if self.updated and self.updated != self.created:
            meta["updated"] = self.updated
        for key, value in self.extra.items():
            meta.setdefault(key, value)
        front = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip()
        return f"---\n{front}\n---\n{self.text.strip()}\n"

    @classmethod
    def parse(cls, text: str, *, name: str = "") -> "Annotation":
        match = _FRONT_MATTER.match(text.replace("\r\n", "\n"))
        if not match:
            raise AnnotationError(f"{name}: missing front matter")
        meta = yaml.safe_load(match.group(1)) or {}
        if not isinstance(meta, dict):
            raise AnnotationError(f"{name}: front matter must be a mapping")
        anchors = meta.get("anchors") or []
        if not isinstance(anchors, list):
            raise AnnotationError(f"{name}: anchors must be a list")
        created = meta.get("created") or ""
        updated = meta.get("updated") or ""
        annotation = cls(
            id=str(meta.get("id") or ""),
            kind=str(meta.get("kind") or ""),
            text=match.group(2).strip(),
            author=str(meta.get("author") or "human"),
            status=str(meta.get("status") or "draft"),
            on=meta.get("on") or {},
            reply_to=str(meta.get("reply_to") or ""),
            task=str(meta.get("task") or ""),
            files=meta.get("files") or [],
            anchors=anchors,
            created=str(created),
            updated=str(updated),
            anchor_state=str(meta.get("anchor_state") or ""),
            extra={k: v for k, v in meta.items() if k not in _KNOWN_KEYS},
        )
        try:
            return annotation.validate()
        except AnnotationError as exc:
            raise AnnotationError(f"{name}: {exc}") from exc


def is_annotation_file(text: str) -> bool:
    """Whether a notes file is an annotation (it has an id) or a guard note."""

    match = _FRONT_MATTER.match(text.replace("\r\n", "\n"))
    if not match:
        return False
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        return False
    return isinstance(meta, dict) and is_id(str(meta.get("id") or ""))


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


_LOCKS: Dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def repo_lock(repo: Path) -> threading.RLock:
    """One lock per repository for every write this process makes there."""

    key = str(Path(repo).resolve())
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


class AnnotationStore:
    """Annotations of one repository, read from and written to its notes dir.

    ``state`` keeps them outside the repository instead (``<state>/notes``):
    the hosted Wiki serves one shared checkout and keeps each visitor's
    annotations in a folder of their own.
    """

    def __init__(self, repo: Path | str, state: Path | str | None = None):
        self.repo = Path(repo)
        self.state = Path(state) if state is not None else None
        self.directory = (
            self.state / "notes" if self.state is not None else self.repo / NOTES_DIR
        )
        self.lock = repo_lock(self.state if self.state is not None else self.repo)

    def _path(self, annotation_id: str) -> Path:
        if not is_id(annotation_id):
            raise AnnotationError(f"bad annotation id {annotation_id!r}")
        return self.directory / f"{annotation_id}.md"

    def get(self, annotation_id: str) -> Annotation:
        path = self._path(annotation_id)
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise AnnotationError(f"no annotation {annotation_id}") from None
        return Annotation.parse(text, name=path.name)

    def list(self, page: Optional[str] = None) -> List[Annotation]:
        """Every annotation, oldest first; guard notes without an id are skipped."""

        found: List[Annotation] = []
        if not self.directory.is_dir():
            return found
        for path in sorted(self.directory.glob("*.md")):
            if not is_id(path.stem):
                continue
            try:
                annotation = Annotation.parse(
                    path.read_text(encoding="utf-8"), name=path.name
                )
            except (OSError, AnnotationError, yaml.YAMLError):
                continue
            if page is None or annotation.page == page:
                found.append(annotation)
        return found

    def save(self, annotation: Annotation) -> Annotation:
        annotation.validate()
        with self.lock:
            _atomic_write(self._path(annotation.id), annotation.dump())
        return annotation

    def create(
        self,
        *,
        kind: str,
        text: str,
        on: Optional[Dict[str, Any]] = None,
        author: str = "human",
        status: Optional[str] = None,
        reply_to: str = "",
        task: str = "",
        files: Optional[Iterable[Dict[str, Any]]] = None,
    ) -> Annotation:
        if reply_to:
            self.get(reply_to)  # the thread must exist
        now = utc_now()
        annotation = Annotation(
            id=new_id(),
            kind=kind,
            text=text,
            author=author,
            status=status or ("open" if kind in ("note",) + AGENT_KINDS else "draft"),
            on=on if on is not None else {},
            reply_to=reply_to,
            task=task,
            files=list(files or []),
            created=now,
            updated=now,
        )
        return self.save(annotation)

    def update(self, annotation_id: str, **changes: Any) -> Annotation:
        with self.lock:
            annotation = self.get(annotation_id)
            for key, value in changes.items():
                if key not in {
                    "text",
                    "kind",
                    "status",
                    "on",
                    "task",
                    "files",
                    "anchor_state",
                }:
                    raise AnnotationError(f"cannot change {key!r}")
                setattr(annotation, key, value)
            annotation.updated = utc_now()
            return self.save(annotation)

    def delete(self, annotation_id: str) -> None:
        with self.lock:
            path = self._path(annotation_id)
            if not path.exists():
                raise AnnotationError(f"no annotation {annotation_id}")
            path.unlink()

    def thread(self, annotation_id: str) -> List[Annotation]:
        """The chain from the first annotation of a thread down to this one."""

        chain: List[Annotation] = []
        seen = set()
        current: Optional[str] = annotation_id
        while current and current not in seen and len(chain) < 50:
            seen.add(current)
            annotation = self.get(current)
            chain.append(annotation)
            current = annotation.reply_to
        return list(reversed(chain))
