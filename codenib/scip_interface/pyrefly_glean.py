# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Convert a pyrefly Glean report into a scip-python-shaped SCIP index.

``pyrefly check --report-glean DIR`` writes one JSON file per checked module.
This module reads the predicates CodeNib needs and emits a SCIP ``Index`` that
uses scip-python's symbol conventions, so the existing serial and C++ graph
decoders, the occurrence index and downstream naming consume it unchanged:

* one document per checked project file;
* a definition occurrence per class, function, module/class variable and
  parameter, with ``enclosing_range`` for classes and functions;
* a reference occurrence per cross reference that resolves to a project
  definition (through import aliases), or to a typeshed module;
* descriptors ``/`` (module), ``#`` (class), ``().`` (function or method),
  ``.`` (term) and ``(name)`` (parameter); function locals are ``local N``.

Pyrefly qualifies nested definitions with ``<locals>``, so a nested function
gets its enclosing function's symbol as prefix instead of being flattened into
the class (#871). Several physical definitions of one qualified name in one
file (branches, property accessors, overloads) receive the same position
markers ``python_identity`` publishes, and a reference to that name targets
every one of them: the static union, not a runtime choice.

Pyrefly reports no definition for an instance attribute assigned through a
method's receiver (``self.x = ...``) and no cross reference at any attribute
write. Like Pyright, the converter declares the first receiver write in a
class as that attribute's definition and records later receiver writes as
references. Other attribute writes stay unreported.

Spans are UTF-8 byte offsets. The index uses UTF-8 code-unit columns
(``position_encoding = 1``), the encoding ``python_identity`` normalizes to.
"""

from __future__ import annotations

import bisect
import json
import logging
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

_DEFINITION = 1
_READ = 8
_SIMPLE_NAME = re.compile(r"[A-Za-z0-9_$+\-]+")
_DEF_KEYWORD = re.compile(rb"(?:async\s+)?(?:def|class)\s+")
logger = logging.getLogger(__name__)

_PREDICATES = frozenset(
    {
        "src.FileLines.1",
        "python.DefinitionLocation.4",
        "python.DeclarationLocation.4",
        "python.xrefs.XRefsByFile.1",
        "python.FileCall.4",
    }
)
_ALIAS_DEPTH = 16
_UNKNOWN_MODULE = "__unknown__"
_SUPER_CALL = re.compile(rb"super\s*\([^()]*\)\s*\.\s*$")


def _escape(name: str) -> str:
    if _SIMPLE_NAME.fullmatch(name):
        return name
    return "`" + name.replace("`", "``") + "`"


@dataclass(eq=False)
class _Definition:
    kind: str  # module | cls | func | variable
    qname: str
    file: str
    start: int
    end: int
    container_kind: Optional[str]
    container: Optional[str]
    params: tuple[str, ...] = ()
    bases: tuple[str, ...] = ()
    decorators: tuple[str, ...] = ()
    name_start: int = -1
    name_end: int = -1
    parent: Optional["_Definition"] = None
    symbol: str = ""
    marker: bool = False

    @property
    def name(self) -> str:
        return self.qname.rsplit(".", 1)[-1]


@dataclass
class _Source:
    relative: str
    content: bytes
    line_starts: list[int] = field(default_factory=list)

    def __post_init__(self):
        starts = [0]
        find = self.content.find
        position = find(b"\n")
        while position >= 0:
            starts.append(position + 1)
            position = find(b"\n", position + 1)
        self.line_starts = starts

    def position(self, offset: int) -> tuple[int, int]:
        line = bisect.bisect_right(self.line_starts, offset) - 1
        return line, offset - self.line_starts[line]

    def range(self, start: int, end: int) -> list[int]:
        line, column = self.position(start)
        end_line, end_column = self.position(end)
        if line == end_line:
            return [line, column, end_column]
        return [line, column, end_line, end_column]

    def enclosing(self, start: int, end: int) -> list[int]:
        line, column = self.position(start)
        end_line, end_column = self.position(end)
        return [line, column, end_line, end_column]


@dataclass
class GleanReport:
    """The subset of a pyrefly Glean report the converter reads."""

    files: set[str] = field(default_factory=set)
    definitions: list[_Definition] = field(default_factory=list)
    # (file, qname) -> starts of class/function declarations without decorators
    declaration_starts: dict[tuple[str, str], list[int]] = field(
        default_factory=lambda: defaultdict(list)
    )
    aliases: dict[str, str] = field(default_factory=dict)
    # file -> [(start, length, [(target file or None, target qname)])]
    xrefs: dict[str, list] = field(default_factory=lambda: defaultdict(list))
    # file -> [(paren offset, [(label, label start)])] for keyword calls
    calls: dict[str, list] = field(default_factory=lambda: defaultdict(list))


def _key(value):
    return value["key"] if isinstance(value, dict) and "key" in value else value


def _qualified(value) -> str:
    return _key(_key(value)["name"])


def _definition(file: str, kind: str, body: dict, span: dict) -> _Definition:
    body = _key(body)
    container_kind = container = None
    if kind == "module":
        qname = _key(_key(body["module"])["name"])
    else:
        qname = _qualified(body["declaration"])
        outer = body.get("container")
        if outer:
            ((container_kind, value),) = outer.items()
            container = _qualified(value)
    params: tuple[str, ...] = ()
    bases: tuple[str, ...] = ()
    if kind == "cls":
        bases = tuple(_qualified(base) for base in body.get("bases") or ())
    if kind == "func":
        names = []
        for group in ("posonly_params", "params", "kwonly_params"):
            names.extend(_key(row["name"]) for row in body.get(group) or ())
        for group in ("star_arg", "star_kwarg"):
            row = body.get(group)
            if row:
                names.append(_key(row["name"]))
        params = tuple(names)
    start = span["start"]
    return _Definition(
        kind=kind,
        qname=qname,
        file=file,
        start=start,
        end=start + span["length"],
        container_kind=container_kind,
        container=container,
        params=params,
        bases=bases,
        decorators=tuple(body.get("decorators") or ()),
    )


# A Glean report has one JSON file per module; parsing them dominated the
# conversion (16 s of 24 s for 1,033 files). Large reports are parsed by worker
# interpreters started as ``python -m codenib.scip_interface._glean_worker``.
# Unlike multiprocessing they never import the caller's ``__main__`` (an
# unguarded script would run again in every worker) and never fork a server's
# threads. A worker that fails leaves its files to be parsed in-process.
_PARALLEL_MIN_FILES = 64
_MAX_GLEAN_WORKERS = 8
_WORKERS_ENV = "CODENIB_GLEAN_WORKERS"


def _glean_file_records(path: str) -> list[tuple]:
    """Extract, in report order, the facts the converter reads from one file.

    Each record is ``(kind, file, *payload)``. Names are already renamed for a
    non-importable module, so merging the records of every file in name order
    rebuilds exactly what a single sequential pass reads. Runs in a worker.
    """

    with open(path, "rb") as handle:
        blocks = json.load(handle)
    rename = _unknown_module_rename(blocks)
    records: list[tuple] = []
    definitions: list[_Definition] = []
    for block in blocks:
        predicate = block.get("predicate")
        if predicate not in _PREDICATES:
            continue
        for fact in block.get("facts") or ():
            key = fact["key"]
            file = _key(key["file"])
            if predicate == "src.FileLines.1":
                records.append(("file", file))
            elif predicate == "python.DefinitionLocation.4":
                ((kind, body),) = key["definition"].items()
                definition = _definition(file, kind, body, key["span"])
                definitions.append(definition)
                records.append(("definition", file, definition))
            elif predicate == "python.DeclarationLocation.4":
                ((kind, body),) = key["declaration"].items()
                body = _key(body)
                if kind == "imp":
                    records.append(
                        (
                            "alias",
                            file,
                            rename(_key(body["as_name"])),
                            _key(body["from_name"]),
                        )
                    )
                elif kind in {"cls", "func"}:
                    records.append(
                        (
                            "declaration",
                            file,
                            rename(_key(body["name"])),
                            key["span"]["start"],
                        )
                    )
            elif predicate == "python.FileCall.4":
                labels = [
                    (_key(argument["label"]), argument["span"]["start"])
                    for argument in key.get("call_args") or ()
                    if argument.get("label")
                ]
                if labels:
                    first = min(
                        argument["span"]["start"] for argument in key["call_args"]
                    )
                    records.append(
                        ("call", file, (key["callee_span"]["start"], first, labels))
                    )
            else:
                rows = []
                for xref in key["xrefs"]:
                    source = xref["source"]
                    target = xref["target"]
                    target_file = target.get("file")
                    target_file = _key(target_file) if target_file else None
                    name = _key(target["name"])
                    rows.append(
                        (
                            source["start"],
                            source["length"],
                            (
                                target_file,
                                rename(name) if target_file == file else name,
                            ),
                        )
                    )
                records.append(("xrefs", file, rows))
    if rename is not _same:
        for definition in definitions:
            definition.qname = rename(definition.qname)
            if definition.container:
                definition.container = rename(definition.container)
    return records


def _glean_workers(file_count: int) -> int:
    configured = os.environ.get(_WORKERS_ENV, "").strip()
    if configured:
        return max(1, int(configured))
    if file_count < _PARALLEL_MIN_FILES:
        return 1
    try:
        available = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):  # not on Linux
        available = os.cpu_count() or 1
    return max(1, min(available, _MAX_GLEAN_WORKERS))


def _map_glean_files(paths: list[str], workers: int) -> Iterable[list[tuple]]:
    """The records of each file, in the order of *paths*."""

    if workers <= 1 or len(paths) < 2:
        yield from map(_glean_file_records, paths)
        return
    import pickle
    import subprocess
    import tempfile

    size = -(-len(paths) // workers)
    chunks = [paths[i : i + size] for i in range(0, len(paths), size)]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}
    with tempfile.TemporaryDirectory(prefix="codenib-glean-") as scratch:
        jobs = []
        for number, chunk in enumerate(chunks):
            out = os.path.join(scratch, f"{number}.pickle")
            try:
                process = subprocess.Popen(
                    [sys.executable, "-m", "codenib.scip_interface._glean_worker", out],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    env=env,
                )
            except OSError as exc:
                logger.warning("Glean worker did not start: %s", exc)
                process = None
            jobs.append((chunk, out, process))
        for chunk, _, process in jobs:
            if process is None or process.stdin is None:
                continue
            try:
                process.stdin.write(json.dumps(chunk).encode("utf-8"))
                process.stdin.close()
            except OSError:  # the worker exited before reading its files
                pass
        for chunk, out, process in jobs:
            if process is None:
                yield from map(_glean_file_records, chunk)
                continue
            error = process.stderr.read() if process.stderr else b""
            if process.wait() == 0 and os.path.exists(out):
                with open(out, "rb") as handle:
                    # Written by our own worker for this call, never by a user.
                    results = pickle.load(handle)  # noqa: S301
                if len(results) == len(chunk):
                    yield from results
                    continue
            logger.warning(
                "Glean worker failed (%s); parsing its %d files in-process",
                error.decode("utf-8", "replace").strip()[-300:] or process.returncode,
                len(chunk),
            )
            yield from map(_glean_file_records, chunk)


def read_glean_report(
    directory: str | Path,
    *,
    path_allowed: Optional[Callable[[str], bool]] = None,
    workers: Optional[int] = None,
) -> GleanReport:
    """Read the predicates the converter needs from every report file.

    Files are parsed in worker processes when there are many of them (see
    ``CODENIB_GLEAN_WORKERS``); the result does not depend on the worker count.
    """

    paths = [
        entry.path
        for entry in sorted(os.scandir(directory), key=lambda item: item.name)
        if entry.name.endswith(".json")
    ]
    if workers is None:
        workers = _glean_workers(len(paths))
    report = GleanReport()
    allowed: dict[str, bool] = {}
    for records in _map_glean_files(paths, workers):
        for record in records:
            kind, file = record[0], record[1]
            if path_allowed is not None:
                keep = allowed.get(file)
                if keep is None:
                    keep = allowed[file] = bool(path_allowed(file))
                if not keep:
                    continue
            if kind == "file":
                report.files.add(file)
            elif kind == "definition":
                report.definitions.append(record[2])
            elif kind == "alias":
                report.aliases[record[2]] = record[3]
            elif kind == "declaration":
                report.declaration_starts[(file, record[2])].append(record[3])
            elif kind == "call":
                report.calls[file].append(record[2])
            else:
                report.xrefs[file].extend(record[2])
    return report


def _same(qname: str) -> str:
    return qname


def _unknown_module_rename(blocks) -> Callable[[str], str]:
    """Name a non-importable module (``generate-docs.py``) by its path.

    pyrefly names every such module ``__unknown__``, so their definitions
    would share symbols across files. scip-python uses the dotted path.
    """

    for block in blocks:
        if block.get("predicate") != "python.DefinitionLocation.4":
            continue
        for fact in block.get("facts") or ():
            definition = fact["key"]["definition"]
            if "module" not in definition:
                continue
            qname = _key(_key(_key(definition["module"])["module"])["name"])
            if qname != _UNKNOWN_MODULE:
                return _same
            module = _module_of_path(_key(fact["key"]["file"]))
            return _prefix_rename(_UNKNOWN_MODULE, module) if module else _same
    return _same


def _prefix_rename(old: str, new: str) -> Callable[[str], str]:
    prefix = old + "."

    def rename(name: str) -> str:
        if name == old:
            return new
        if name.startswith(prefix):
            return new + name[len(old) :]
        return name

    return rename


def _module_of_path(path: str) -> Optional[str]:
    for suffix in (".pyi", ".py"):
        if path.endswith(suffix):
            module = path[: -len(suffix)]
            break
    else:
        return None
    if module.endswith("/__init__"):
        module = module[: -len("/__init__")]
    if not module or module.startswith("/") or ".." in module.split("/"):
        return None
    return module.replace("/", ".")


class _Converter:
    def __init__(
        self,
        report: GleanReport,
        project_root: Path,
        project_name: str,
        project_version: str,
        stdlib_version: str,
    ):
        self.report = report
        self.root = project_root
        self.prefix = f"scip-python python {project_name} {project_version} "
        self.stdlib_prefix = f"scip-python python python-stdlib {stdlib_version} "
        self.sources: dict[str, _Source] = {}
        self.registry: dict[str, list[_Definition]] = defaultdict(list)
        self.module_files: dict[str, str] = {}
        self.locals: dict[str, dict[tuple[str, int], str]] = defaultdict(dict)
        self.writes: list[_Definition] = []
        self.members: dict[tuple[str, str], list[_Definition]] = defaultdict(list)
        self.classes_by_name: dict[str, list[_Definition]] = defaultdict(list)
        self.by_symbol: dict[str, _Definition] = {}
        self.classes_by_file: dict[str, list[_Definition]] = defaultdict(list)
        self.functions_by_file: dict[str, list[_Definition]] = defaultdict(list)
        self.file_modules: dict[str, str] = {}
        self.occurrences: dict[str, dict[tuple, tuple]] = defaultdict(dict)
        self.stats: Counter = Counter()

    # ── sources and positions ────────────────────────────────────────

    def source(self, relative: str) -> Optional[_Source]:
        source = self.sources.get(relative)
        if source is None and relative in self.report.files:
            try:
                content = (self.root / relative).read_bytes()
            except OSError:
                return None
            source = self.sources[relative] = _Source(relative, content)
        return source

    def _locate_name(self, definition: _Definition, source: _Source) -> bool:
        name = definition.name.encode("utf-8")
        content = source.content
        if definition.kind in {"cls", "func"}:
            starts = [
                start
                for start in self.report.declaration_starts.get(
                    (definition.file, definition.qname), ()
                )
                if definition.start <= start < definition.end
            ] or [definition.start]
            for start in starts:
                match = _DEF_KEYWORD.match(content, start)
                if match and content.startswith(name, match.end()):
                    definition.name_start = match.end()
                    definition.name_end = match.end() + len(name)
                    return True
            pattern = re.compile(
                rb"(?:^|\s)(?:def|class)\s+(" + re.escape(name) + rb")\b", re.M
            )
            match = pattern.search(content, definition.start, definition.end)
            if match:
                definition.name_start, definition.name_end = match.span(1)
                return True
            return False
        if definition.kind == "variable":
            if content.startswith(name, definition.start):
                definition.name_start = definition.start
                definition.name_end = definition.start + len(name)
                return True
            match = re.compile(rb"\b" + re.escape(name) + rb"\b").search(
                content, definition.start, definition.end
            )
            if match:
                definition.name_start, definition.name_end = match.span()
                return True
            return False
        definition.name_start = definition.name_end = 0
        return True

    # ── symbols ──────────────────────────────────────────────────────

    def module_namespace(self, module: str) -> str:
        return f"{self.prefix}{_escape(module)}/"

    def _resolve_parents(self) -> None:
        by_file_qname: dict[tuple[str, str], list[_Definition]] = defaultdict(list)
        for definition in self.report.definitions:
            if definition.kind in {"cls", "func"}:
                by_file_qname[(definition.file, definition.qname)].append(definition)
        for definition in self.report.definitions:
            if definition.container_kind in {"cls", "func"}:
                candidates = [
                    parent
                    for parent in by_file_qname.get(
                        (definition.file, definition.container), ()
                    )
                    if parent.start <= definition.start
                    and definition.end <= parent.end
                    and parent is not definition
                ]
                if candidates:
                    definition.parent = min(
                        candidates, key=lambda parent: parent.end - parent.start
                    )

    def _symbol(self, definition: _Definition, stack=()) -> str:
        if definition.symbol:
            return definition.symbol
        if definition in stack:
            raise ValueError(f"pyrefly definition cycle at {definition.qname}")
        source = self.sources[definition.file]
        line, column = source.position(max(definition.name_start, 0))
        name = definition.name
        if definition.kind == "module":
            symbol = self.module_namespace(definition.qname) + "__init__:"
        elif definition.container_kind in {"cls", "func"}:
            parent = definition.parent
            if parent is None:
                return ""
            base = self._symbol(parent, (*stack, definition))
            if not base:
                return ""
            if (
                parent.kind == "func"
                and definition.kind == "variable"
                and not definition.marker
            ):
                if name in parent.params:
                    symbol = base + "(" + _escape(name) + ")"
                else:
                    # Branch definitions share a qualified name; keep each
                    # physical function's locals apart.
                    table = self.locals[definition.file]
                    symbol = table.setdefault(
                        (definition.qname, id(parent)), f"local {len(table)}"
                    )
            else:
                symbol = base + self._descriptor(definition, name, line, column)
        else:
            module = definition.container or definition.qname.rsplit(".", 1)[0]
            symbol = self.module_namespace(module) + self._descriptor(
                definition, name, line, column
            )
        definition.symbol = symbol
        return symbol

    @staticmethod
    def _descriptor(definition: _Definition, name: str, line: int, column: int):
        if definition.kind == "cls":
            marker = f"@codenib-l{line}-c{column}" if definition.marker else ""
            return _escape(name + marker) + "#"
        if definition.kind == "func":
            marker = f"_codenib_l{line}_c{column}" if definition.marker else ""
            return _escape(name) + f"({marker})."
        marker = f"@codenib-l{line}-c{column}" if definition.marker else ""
        return _escape(name + marker) + "."

    # ── conversion ───────────────────────────────────────────────────

    def prepare_definitions(self) -> None:
        kept = []
        for definition in self.report.definitions:
            if definition.kind not in {"module", "cls", "func", "variable"}:
                continue
            source = self.source(definition.file)
            if source is None:
                self.stats["definitions_without_source"] += 1
                continue
            if definition.kind == "module":
                self.module_files.setdefault(definition.qname, definition.file)
                self.file_modules.setdefault(definition.file, definition.qname)
            if not self._locate_name(definition, source):
                self.stats["definitions_without_name"] += 1
                continue
            kept.append(definition)
        kept.sort(key=lambda d: (d.file, d.name_start, d.start))
        self.report.definitions = kept
        self._resolve_parents()

        # Several physical class/function definitions of one name in one
        # scope get position markers, as do assignments sharing a class or
        # function binding (``python_identity`` keeps both as declarations).
        # A later plain variable binding is a write.
        groups: dict[tuple, list[_Definition]] = defaultdict(list)
        for definition in kept:
            groups[(definition.file, definition.qname, id(definition.parent))].append(
                definition
            )
        writes = []
        for rows in groups.values():
            callables = [row for row in rows if row.kind in {"cls", "func"}]
            by_kind = defaultdict(list)
            for row in callables:
                by_kind[row.kind].append(row)
            for same in by_kind.values():
                if len(same) > 1:
                    for row in same:
                        row.marker = True
            variables = [row for row in rows if row.kind == "variable"]
            if callables:
                for row in variables:
                    row.marker = True
            elif len(variables) > 1:
                writes.extend(variables[1:])
        self.writes = writes
        written = set(map(id, writes))
        for definition in kept:
            if id(definition) in written:
                continue
            self.registry[definition.qname].append(definition)
            if definition.container_kind == "cls":
                self.members[(definition.file, definition.name)].append(definition)
            if definition.kind == "cls":
                self.classes_by_name[definition.name].append(definition)
                self.classes_by_file[definition.file].append(definition)
            if definition.kind == "func":
                self.functions_by_file[definition.file].append(definition)
        for definition in kept:
            if id(definition) not in written and definition.kind in {"cls", "func"}:
                symbol = self._symbol(definition)
                if symbol:
                    self.by_symbol[symbol] = definition
        for definition in kept:
            if id(definition) in written:
                continue
            symbol = self._symbol(definition)
            if not symbol:
                self.stats["definitions_without_parent"] += 1
                continue
            source = self.sources[definition.file]
            if definition.kind == "module":
                occurrence_range = [0, 0, 0]
                enclosing = None
            else:
                occurrence_range = source.range(
                    definition.name_start, definition.name_end
                )
                enclosing = (
                    source.enclosing(definition.start, definition.end)
                    if definition.kind in {"cls", "func"}
                    else None
                )
            self._add(definition.file, occurrence_range, symbol, _DEFINITION, enclosing)
            self.stats["definitions"] += 1

    def _add(self, file, occurrence_range, symbol, roles, enclosing=None):
        key = (tuple(occurrence_range), symbol)
        existing = self.occurrences[file].get(key)
        if existing is not None:
            if existing[0] & _DEFINITION:
                return
            if not roles & _DEFINITION:
                return
        self.occurrences[file][key] = (roles, enclosing)

    def resolve(
        self,
        file: Optional[str],
        qname: str,
        depth: int = 0,
        site: Optional[tuple[str, int]] = None,
    ) -> list[str]:
        """Return project symbols for a target, following import aliases.

        pyrefly names an attribute through the class it is read from; an
        inherited member is found on the first project base declaring it.
        """

        # Python resolves a name in the enclosing function scopes before the
        # module: a parameter or local that shares a module-level name is the
        # binding a read inside that function uses, whatever pyrefly names it.
        if site is not None:
            local = self._lexical_local(qname, site)
            if local:
                return local
        rows = self.registry.get(qname)
        if rows:
            if site is not None and len(rows) > 1:
                rows = self._lexical_filter(rows, site)
            return [row.symbol for row in rows if row.symbol]
        if qname in self.module_files:
            return [self.module_namespace(qname) + "__init__:"]
        alias = self.report.aliases.get(qname)
        if alias is not None and depth < _ALIAS_DEPTH and alias != qname:
            return self.resolve(None, alias, depth + 1)
        owner, _, member = qname.rpartition(".")
        if not owner or depth >= _ALIAS_DEPTH:
            return []
        found = self._inherited(owner, member, depth)
        if not found and file is not None:
            found = self._declared_in(file, owner, member, site)
        return found

    def _enclosing_functions(self, file: str, offset: int) -> list[_Definition]:
        rows = [
            row
            for row in self.functions_by_file.get(file, ())
            if row.start <= offset < row.end
        ]
        rows.sort(key=lambda row: row.end - row.start)
        return rows

    def _lexical_filter(
        self, rows: list[_Definition], site: tuple[str, int]
    ) -> list[_Definition]:
        """Keep the function-local declarations whose scope holds the site.

        Branch definitions share one qualified name, and so do their
        parameters and locals; a use inside one of them names its own.
        """

        file, offset = site
        inside = [
            row
            for row in rows
            if row.file == file
            and row.parent is not None
            and row.parent.kind == "func"
            and row.parent.start <= offset < row.parent.end
        ]
        if not inside:
            return rows
        innermost = min(row.parent.end - row.parent.start for row in inside)
        return [row for row in inside if row.parent.end - row.parent.start == innermost]

    def _lexical_local(self, qname: str, site: tuple[str, int]) -> list[str]:
        """Resolve pyrefly's module-qualified name for a function-local def.

        pyrefly 1.4 names a call to a function defined in an enclosing
        function body ``<module>.<name>`` while the definition is
        ``<function>.<locals>.<name>``. Look the name up lexically.
        """

        file, offset = site
        module = self.file_modules.get(file)
        if module is None or not qname.startswith(module + "."):
            return []
        name = qname[len(module) + 1 :]
        if "." in name:
            return []
        for function in self._enclosing_functions(file, offset):
            rows = self.registry.get(f"{function.qname}.<locals>.{name}")
            if rows:
                rows = self._lexical_filter(rows, site)
                return [row.symbol for row in rows if row.symbol]
        return []

    def _lineage(self, roots: Iterable[str]) -> set[str]:
        lineage: set[str] = set()
        queue = list(roots)
        while queue and len(lineage) < 256:
            current = queue.pop()
            if current in lineage:
                continue
            lineage.add(current)
            for cls in self.registry.get(current, ()):
                if cls.kind == "cls":
                    queue.extend(self.report.aliases.get(b, b) for b in cls.bases)
        return lineage

    def _declared_in(
        self,
        file: str,
        owner: str,
        member: str,
        site: Optional[tuple[str, int]],
    ) -> list[str]:
        """Resolve pyrefly's names for members found through another class.

        pyrefly can name an inherited member by the module that declares it
        and the class it is read through (``http.base.GraphQLRouter.f`` for
        ``BaseView.f``), or by that module alone for ``super().f``. Accept
        only a member declared in that module on a base of the receiver
        class, or for ``super()`` of the class enclosing the reference.
        """

        module = self.file_modules.get(file)
        if module is None:
            return []
        candidates = [
            row
            for row in self.members.get((file, member), ())
            if row.symbol and row.parent is not None
        ]
        if not candidates:
            return []
        if owner == module:
            if site is None or not _SUPER_CALL.search(
                self.sources[site[0]].content, max(0, site[1] - 80), site[1]
            ):
                return []
            roots = [
                cls.qname
                for cls in self.classes_by_file.get(site[0], ())
                if cls.start <= site[1] < cls.end
            ]
        else:
            prefix, _, short = owner.rpartition(".")
            if prefix != module:
                return []
            roots = [cls.qname for cls in self.classes_by_name.get(short, ())]
        lineage = self._lineage(roots)
        return [row.symbol for row in candidates if row.parent.qname in lineage]

    def _inherited(self, owner: str, member: str, depth: int) -> list[str]:
        """Use Python's C3 order; ambiguous or incomplete hierarchies fail closed."""

        memo: dict[str, list[str]] = {}

        def linearize(name: str, active: frozenset[str]) -> Optional[list[str]]:
            aliases: set[str] = set()
            while name in self.report.aliases:
                if name in aliases or len(aliases) + depth >= _ALIAS_DEPTH:
                    return None
                aliases.add(name)
                name = self.report.aliases[name]
            if name in active or len(active) >= 64 or len(memo) >= 64:
                return None
            if name in memo:
                return memo[name]
            if name in {"object", "builtins.object"}:
                return ["builtins.object"]
            classes = [r for r in self.registry.get(name, ()) if r.kind == "cls"]
            if len(classes) != 1 or len(classes[0].bases) > 64:
                return None
            parents = [linearize(base, active | {name}) for base in classes[0].bases]
            if any(parent is None for parent in parents):
                return None
            # Canonical parent identities also handle aliases in direct bases.
            sequences = [list(parent) for parent in parents]
            sequences.append([parent[0] for parent in parents])
            order = [name]
            while sequences := [sequence for sequence in sequences if sequence]:
                candidate = next(
                    (
                        sequence[0]
                        for sequence in sequences
                        if not any(sequence[0] in other[1:] for other in sequences)
                    ),
                    None,
                )
                if candidate is None or candidate in order or len(order) >= 64:
                    return None
                order.append(candidate)
                for sequence in sequences:
                    if sequence[0] == candidate:
                        sequence.pop(0)
            memo[name] = order
            return order

        order = linearize(owner, frozenset())
        for base in order[1:] if order else ():
            rows = self.registry.get(f"{base}.{member}")
            if rows:
                return [row.symbol for row in rows if row.symbol]
        return []

    def external(self, file: Optional[str], qname: str) -> Optional[str]:
        if not file or file.startswith("/") or file in self.report.files:
            return None
        module = _module_of_path(file)
        if module is None:
            return None
        namespace = self.stdlib_prefix + _escape(module) + "/"
        if qname == module:
            return namespace + "__init__:"
        if not qname.startswith(module + "."):
            return None
        return namespace + "".join(
            _escape(part) + "." for part in qname[len(module) + 1 :].split(".")
        )

    def convert_references(self) -> None:
        exports: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for definition in self.report.definitions:
            if definition.qname.endswith(".__all__") and definition.parent is None:
                exports[definition.file].append((definition.start, definition.end))
        for file, rows in self.report.xrefs.items():
            source = self.source(file)
            if source is None:
                continue
            spans: dict[tuple[int, int], list] = defaultdict(list)
            excluded = exports.get(file, ())
            for start, length, target in rows:
                if excluded and any(low <= start < high for low, high in excluded):
                    self.stats["export_list_references_dropped"] += 1
                    continue
                spans[(start, length)].append(target)
            resolved = []
            for (start, length), targets in sorted(spans.items()):
                project: list[str] = []
                for target_file, qname in targets:
                    for symbol in self.resolve(target_file, qname, site=(file, start)):
                        if symbol not in project:
                            project.append(symbol)
                if project:
                    resolved.append([start, start + length, project])
                    continue
                externals = [
                    symbol
                    for symbol in (
                        self.external(target_file, qname)
                        for target_file, qname in targets
                    )
                    if symbol
                ]
                if externals:
                    resolved.append([start, start + length, externals[:1]])
                elif any(
                    target_file in self.report.files and "<locals>" not in qname
                    for target_file, qname in targets
                ):
                    self.stats["unresolved_project_references"] += 1
            # One dotted module path (``a.b.c``) is one module reference,
            # as scip-python reports it.
            merged = []
            for row in resolved:
                previous = merged[-1] if merged else None
                if (
                    previous is not None
                    and previous[1] + 1 == row[0]
                    and source.content[previous[1] : row[0]] == b"."
                    and all(symbol.endswith("/__init__:") for symbol in previous[2])
                    and all(symbol.endswith("/__init__:") for symbol in row[2])
                ):
                    row[0] = previous[0]
                    merged[-1] = row
                    continue
                merged.append(row)
            for start, end, symbols in merged:
                occurrence_range = source.range(start, end)
                for symbol in symbols:
                    self._add(file, occurrence_range, symbol, _READ)
                    self.stats["references"] += 1
            calls = self.report.calls.get(file)
            if calls:
                callees = {end: symbols for _, end, symbols in resolved}
                self._convert_keyword_arguments(source, calls, callees)
        for definition in self.writes:
            symbols = self.resolve(
                definition.file,
                definition.qname,
                site=(definition.file, definition.name_start),
            )
            source = self.sources[definition.file]
            for symbol in symbols:
                self._add(
                    definition.file,
                    source.range(definition.name_start, definition.name_end),
                    symbol,
                    _READ,
                )
                self.stats["variable_rebindings"] += 1

    def _convert_keyword_arguments(self, source: _Source, calls, callees) -> None:
        """Reference what a keyword argument names, as Pyright resolves it.

        pyrefly reports call labels (``FileCall``) but no cross reference for
        them. A label names a parameter of the called function or of the
        first explicit project ``__init__`` on the class; for a class
        without one (a dataclass or similar synthesized constructor) it names
        the field. Parameters add occurrences only; fields add graph edges.
        """

        content = source.content
        for callee_start, first_argument, labels in calls:
            paren = content.rfind(b"(", callee_start, first_argument)
            if paren < 0:
                continue
            end = paren
            while end > callee_start and content[end - 1 : end].isspace():
                end -= 1
            for symbol in callees.get(end, ()):
                callee = self.by_symbol.get(symbol)
                if callee is None:
                    continue
                for label, start in labels:
                    encoded = label.encode("utf-8")
                    if not content.startswith(encoded, start):
                        continue
                    for target in self._keyword_targets(callee, label):
                        self._add(
                            source.relative,
                            source.range(start, start + len(encoded)),
                            target,
                            _READ,
                        )
                        self.stats["keyword_argument_references"] += 1

    def _keyword_targets(self, callee: _Definition, label: str) -> list[str]:
        if callee.kind == "func":
            return (
                [callee.symbol + "(" + _escape(label) + ")"]
                if label in callee.params
                else []
            )
        if callee.kind != "cls":
            return []
        # A field is a constructor keyword only for a synthesized
        # constructor: a decorated class (dataclass, attrs, strawberry.type)
        # or a NamedTuple/TypedDict. An unresolved base proves nothing.
        queue, seen, dataclass_like = [callee.qname], set(), False
        while queue and len(seen) < 64:
            current = queue.pop(0)
            if current in seen:
                continue
            seen.add(current)
            if current.rsplit(".", 1)[-1] in {"NamedTuple", "TypedDict"}:
                dataclass_like = True
            initializers = [
                row
                for row in self.registry.get(f"{current}.__init__", ())
                if row.kind == "func" and row.symbol
            ]
            if initializers:
                return [
                    row.symbol + "(" + _escape(label) + ")"
                    for row in initializers
                    if label in row.params
                ]
            for cls in self.registry.get(current, ()):
                if cls.kind == "cls":
                    dataclass_like = dataclass_like or bool(cls.decorators)
                    queue.extend(self.report.aliases.get(b, b) for b in cls.bases)
        if not dataclass_like:
            return []
        return [
            symbol
            for symbol in self.resolve(None, f"{callee.qname}.{label}")
            if symbol.endswith(".") and not symbol.endswith(").")
        ]

    def convert_receiver_attributes(self) -> None:
        """Declare ``self.x = ...`` attributes as Pyright does."""

        methods: dict[str, list[_Definition]] = defaultdict(list)
        for definition in self.report.definitions:
            parent = definition.parent
            if (
                definition.kind == "func"
                and parent is not None
                and parent.kind == "cls"
                and definition.params
                and not any(
                    decorator.strip().lstrip("@").split("(")[0].endswith("staticmethod")
                    for decorator in definition.decorators
                )
            ):
                methods[definition.file].append(definition)
        if not methods:
            return
        parser = _python_parser()
        writes: dict[int, list[tuple[int, int, str]]] = defaultdict(list)
        classes: dict[int, _Definition] = {}
        for file, rows in methods.items():
            tree = parser.parse(self.sources[file].content)
            for method in rows:
                node = tree.root_node.descendant_for_byte_range(
                    method.name_start, method.name_end
                )
                function = node.parent if node is not None else None
                if function is None or function.type != "function_definition":
                    self.stats["methods_without_syntax"] += 1
                    continue
                body = function.child_by_field_name("body")
                if body is None:
                    continue
                receiver = method.params[0].encode("utf-8")
                classes[id(method.parent)] = method.parent
                writes[id(method.parent)].extend(_receiver_writes(body, receiver))
        # Bases first, so a subclass write finds an attribute its base declares.
        depths: dict[str, int] = {}
        for key in sorted(
            writes,
            key=lambda key: (self._class_depth(classes[key].qname, depths, ()), key),
        ):
            owner = classes[key]
            source = self.sources[owner.file]
            declared: dict[str, list[str]] = {}
            for start, end, name in sorted(writes[key]):
                symbols = declared.get(name)
                if symbols is None:
                    qname = f"{owner.qname}.{name}"
                    symbols = [
                        row.symbol
                        for row in self.registry.get(qname, ())
                        if row.parent is owner and row.symbol
                    ] or self._inherited(owner.qname, name, 0)
                    declared[name] = symbols
                    if not symbols:
                        symbol = owner.symbol + _escape(name) + "."
                        declared[name] = [symbol]
                        attribute = _Definition(
                            kind="variable",
                            qname=qname,
                            file=owner.file,
                            start=start,
                            end=end,
                            container_kind="cls",
                            container=owner.qname,
                            name_start=start,
                            name_end=end,
                            parent=owner,
                            symbol=symbol,
                        )
                        self.registry[qname].append(attribute)
                        self.members[(owner.file, name)].append(attribute)
                        self._add(
                            owner.file, source.range(start, end), symbol, _DEFINITION
                        )
                        self.stats["receiver_attribute_definitions"] += 1
                        continue
                for symbol in symbols:
                    self._add(owner.file, source.range(start, end), symbol, _READ)
                    self.stats["receiver_attribute_writes"] += 1

    def _class_depth(self, qname: str, depths: dict[str, int], stack) -> int:
        depth = depths.get(qname)
        if depth is not None:
            return depth
        if qname in stack or len(stack) > 32:
            return 0
        depth = 0
        for cls in self.registry.get(qname, ()):
            if cls.kind != "cls":
                continue
            for base in cls.bases:
                base = self.report.aliases.get(base, base)
                if base in self.registry:
                    depth = max(
                        depth, 1 + self._class_depth(base, depths, (*stack, qname))
                    )
        depths[qname] = depth
        return depth

    def index(self, metadata_root: str, tool_version: str):
        from .scip_pb2 import Index

        index = Index()
        index.metadata.tool_info.name = "pyrefly"
        index.metadata.tool_info.version = tool_version
        index.metadata.project_root = metadata_root
        index.metadata.text_document_encoding = 1
        for file in sorted(self.report.files):
            if self.source(file) is None:
                continue
            document = index.documents.add()
            document.relative_path = file
            document.language = "python"
            document.position_encoding = 1
            rows = sorted(
                self.occurrences.get(file, {}).items(),
                key=lambda item: (
                    item[0][0][:2],
                    not item[1][0] & _DEFINITION,
                    item[0][1],
                ),
            )
            for (occurrence_range, symbol), (roles, enclosing) in rows:
                occurrence = document.occurrences.add()
                occurrence.range.extend(occurrence_range)
                occurrence.symbol = symbol
                occurrence.symbol_roles = roles
                if enclosing:
                    occurrence.enclosing_range.extend(enclosing)
        return index


def _python_parser():
    from ..code_chunking.base import BaseCodeChunker

    return BaseCodeChunker._create_parser(
        BaseCodeChunker._get_tree_sitter_language("python")
    )


_SCOPES = frozenset({"function_definition", "class_definition", "lambda"})
_TARGETS = frozenset(
    {
        "pattern_list",
        "tuple_pattern",
        "list_pattern",
        "parenthesized_expression",
        "as_pattern_target",
    }
)


def _receiver_writes(body, receiver: bytes) -> Iterable[tuple[int, int, str]]:
    """Yield attribute writes only when the method's receiver stays bound.

    This is a conservative whole-body check, not control-flow type inference:
    any observed receiver rebinding rejects synthesis for this method.
    """

    writes = []
    stack = [body]
    while stack:
        node = stack.pop()
        if node.type in _SCOPES:
            name = node.child_by_field_name("name")
            if name is not None and name.text == receiver:
                return
            continue
        if node.type in {"import_statement", "import_from_statement"}:
            module = node.child_by_field_name("module_name")
            for imported in node.named_children:
                if imported == module:
                    continue
                alias = imported.child_by_field_name("alias")
                if alias is None and imported.type == "dotted_name":
                    alias = imported.named_children[0]
                if alias is not None and alias.text == receiver:
                    return
        target = None
        if node.type in {"assignment", "augmented_assignment"}:
            target = node.child_by_field_name("left")
        elif node.type == "named_expression":
            target = node.child_by_field_name("name")
        elif node.type in {"for_statement", "for_in_clause"}:
            target = node.child_by_field_name("left")
        elif node.type == "as_pattern":
            target = node.child_by_field_name("alias")
        elif node.type in {"case_pattern", "delete_statement"}:
            target = node
        if _binds_receiver(target, receiver):
            return
        if node.type in {
            "assignment",
            "augmented_assignment",
            "as_pattern",
            "for_statement",
        }:
            field_name = "alias" if node.type == "as_pattern" else "left"
            targets = [node.child_by_field_name(field_name)]
            while targets:
                target = targets.pop()
                if target is None:
                    continue
                if target.type == "attribute":
                    obj = target.child_by_field_name("object")
                    attribute = target.child_by_field_name("attribute")
                    if (
                        obj is not None
                        and attribute is not None
                        and obj.type == "identifier"
                        and obj.text == receiver
                    ):
                        writes.append(
                            (
                                attribute.start_byte,
                                attribute.end_byte,
                                attribute.text.decode("utf-8"),
                            )
                        )
                elif target.type in _TARGETS:
                    targets.extend(target.named_children)
            if node.type == "assignment":
                right = node.child_by_field_name("right")
                if right is not None:
                    stack.append(right)
                continue
            stack.extend(
                child
                for child in node.named_children
                if child is not node.child_by_field_name(field_name)
            )
            continue
        stack.extend(node.named_children)
    yield from writes


def _binds_receiver(target, receiver: bytes) -> bool:
    if target is None or target.type in {"attribute", "subscript"}:
        return False
    if target.type == "identifier":
        return target.text == receiver
    return any(_binds_receiver(child, receiver) for child in target.named_children)


def glean_to_scip_index(
    glean_dir: str | Path,
    project_root: str | Path,
    *,
    project_name: str,
    project_version: str = "0",
    tool_version: str = "",
    path_allowed: Optional[Callable[[str], bool]] = None,
    report: Optional[GleanReport] = None,
):
    """Return ``(Index, stats)`` for a pyrefly Glean report directory."""

    root = Path(project_root).resolve()
    if report is None:
        report = read_glean_report(glean_dir, path_allowed=path_allowed)
    converter = _Converter(
        report,
        root,
        project_name or root.name or "project",
        project_version or "0",
        f"{sys.version_info.major}.{sys.version_info.minor}",
    )
    converter.prepare_definitions()
    converter.convert_receiver_attributes()
    converter.convert_references()
    index = converter.index(root.as_uri(), tool_version)
    stats = dict(converter.stats)
    stats["documents"] = len(index.documents)
    return index, stats


__all__ = ["GleanReport", "glean_to_scip_index", "read_glean_report"]
