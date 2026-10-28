# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Recover physical Python declarations before either SCIP graph decoder runs.

scip-python can flatten nested functions into their enclosing class and mark
later definitions as reads. Source syntax supplies physical definitions;
compiler occurrences supply references. A lexical binding retains all its
physical declarations. Positional LSP definitions distinguish provider IDs
that span different lexical bindings. Source-only definitions are reported;
no reference is invented for them. The raw compiler artifact is retained.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections import Counter, defaultdict
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Mapping, Sequence
from urllib.parse import urlparse
from urllib.request import url2pathname

if TYPE_CHECKING:
    from .scip_pb2 import Index, Occurrence

_IDENTIFIER = r"(?:`(?:``|[^`])+`|[A-Za-z0-9_$+\-]+)"
_DESCRIPTOR = re.compile(rf"({_IDENTIFIER})(\([^)]*\))?([.#/:])")
_PARAMETER = re.compile(rf"\(({_IDENTIFIER})\)$")
_CLASS_MARKER = re.compile(r"@codenib-l\d+-c\d+$")
_TERM_MARKER = re.compile(r"@codenib-l\d+-c\d+`?\.$")
_DEFINITION = 1
_READ = 8
_REPAIR_TIMEOUT = 120.0

DefinitionResolver = Callable[[str, int, int], Sequence[Mapping]]


@dataclass(eq=False)
class _Declaration:
    name: str
    kind: str
    start: tuple[int, int]
    end: tuple[int, int]
    extent: tuple[int, int, int, int]
    parent: _Declaration | None
    occurrence: Occurrence | None = None
    original: str = ""
    symbol: str = ""
    nonlocals: set[str] = field(default_factory=set)


@dataclass
class _Source:
    path: Path
    content: bytes
    lines: list[str]
    declarations: list[_Declaration] = field(default_factory=list)
    parameter_anchors: dict[tuple[int, int], set[str]] = field(default_factory=dict)

    def column(self, line: int, column: int, encoding: int) -> int:
        """Convert a SCIP column to a UTF-8 byte column without rounding."""

        if line < 0 or line >= len(self.lines) or column < 0:
            raise ValueError("Python SCIP source position is outside the source")
        text = self.lines[line]
        if encoding == 1:
            prefix = text.encode("utf-8")[:column].decode("utf-8", errors="strict")
            if len(prefix.encode("utf-8")) != column:
                raise ValueError("Python SCIP byte column is outside the source")
        elif encoding == 2:
            prefix = text.encode("utf-16-le")[: column * 2].decode("utf-16-le")
            if len(prefix.encode("utf-16-le")) != column * 2:
                raise ValueError("Python SCIP UTF-16 column is outside the source")
        elif encoding == 3:
            if column > len(text):
                raise ValueError("Python SCIP character column is outside the source")
            prefix = text[:column]
        else:
            raise ValueError(f"unsupported Python SCIP position encoding: {encoding}")
        return len(prefix.encode("utf-8"))

    def utf16(self, line: int, column: int) -> int:
        prefix = self.lines[line].encode("utf-8")[:column].decode("utf-8")
        return len(prefix.encode("utf-16-le")) // 2


def _unescape(name: str) -> str:
    return name[1:-1].replace("``", "`") if name.startswith("`") else name


def _escape(name: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_$+\-]+", name):
        return name
    return "`" + name.replace("`", "``") + "`"


def _symbol_parts(symbol: str) -> tuple[str, str]:
    """Split four SCIP global fields, honoring doubled-space field escapes."""

    position = 0
    for _ in range(4):
        while position < len(symbol):
            if symbol[position] != " ":
                position += 1
            elif symbol[position : position + 2] == "  ":
                position += 2
            else:
                position += 1
                break
        else:
            raise ValueError(f"invalid global Python SCIP symbol: {symbol!r}")
    return symbol[:position], symbol[position:]


def _namespace(symbol: str) -> str:
    prefix, descriptors = _symbol_parts(symbol)
    for match in _DESCRIPTOR.finditer(descriptors):
        if match[3] == "/":
            return prefix + descriptors[: match.end()]
    raise ValueError(f"Python SCIP symbol has no module namespace: {symbol!r}")


def _name(symbol: str) -> str:
    parameter = _PARAMETER.search(symbol)
    if parameter:
        return _unescape(parameter[1])
    _, descriptors = _symbol_parts(symbol)
    parts = list(_DESCRIPTOR.finditer(descriptors))
    return _CLASS_MARKER.sub("", _unescape(parts[-1][1])) if parts else ""


def _range(values: Sequence[int]) -> tuple[int, int, int, int]:
    if len(values) == 3:
        return values[0], values[1], values[0], values[2]
    if len(values) == 4:
        return tuple(values)
    raise ValueError("Python SCIP range must contain three or four integers")


def _convert_range(values: Sequence[int], source: _Source, encoding: int) -> list[int]:
    start_line, start, end_line, end = _range(values)
    result = [
        start_line,
        source.column(start_line, start, encoding),
        end_line,
        source.column(end_line, end, encoding),
    ]
    if tuple(result[:2]) > tuple(result[2:]):
        raise ValueError("Python SCIP source range is reversed")
    return result


def _parameter_names(node):
    for child in node.named_children:
        if child.type == "identifier":
            yield child
        elif child.type in {
            "typed_parameter",
            "typed_default_parameter",
            "default_parameter",
            "list_splat_pattern",
            "dictionary_splat_pattern",
        }:
            name = child.child_by_field_name("name")
            if name is None and child.named_children:
                name = child.named_children[0]
            if name is not None:
                if name.type == "identifier":
                    yield name
                elif name.type in {"list_splat_pattern", "dictionary_splat_pattern"}:
                    yield from _parameter_names(name)


def _declarations(source: _Source, parser) -> list[_Declaration]:
    # Use the compiler/LSP's UTF-8 text view; keep original bytes for receipts.
    content = "\n".join(source.lines).encode("utf-8")
    tree = parser.parse(content)
    result = []

    def visit(node, parent):
        if node.type == "nonlocal_statement" and parent is not None:
            parent.nonlocals.update(
                content[child.start_byte : child.end_byte].decode("utf-8")
                for child in node.named_children
                if child.type == "identifier"
            )
        if node.type in {"function_definition", "class_definition"}:
            name = node.child_by_field_name("name")
            if name is not None and not node.has_error:
                extent = (
                    node.parent if node.parent.type == "decorated_definition" else node
                )
                declaration = _Declaration(
                    name=content[name.start_byte : name.end_byte].decode("utf-8"),
                    kind="class" if node.type == "class_definition" else "function",
                    start=tuple(name.start_point),
                    end=tuple(name.end_point),
                    extent=(*extent.start_point, *extent.end_point),
                    parent=parent,
                )
                result.append(declaration)
                parameters = node.child_by_field_name("parameters")
                if parameters is not None:
                    for parameter in _parameter_names(parameters):
                        result.append(
                            _Declaration(
                                name=content[
                                    parameter.start_byte : parameter.end_byte
                                ].decode("utf-8"),
                                kind="parameter",
                                start=tuple(parameter.start_point),
                                end=tuple(parameter.end_point),
                                extent=(*parameter.start_point, *parameter.end_point),
                                parent=declaration,
                            )
                        )
                parent = declaration
        elif node.type in {"assignment", "augmented_assignment", "named_expression"}:
            target = node.child_by_field_name("left") or node.child_by_field_name(
                "name"
            )
            kind = "assignment"
            if target is not None and target.type == "attribute":
                target = target.child_by_field_name("attribute")
                kind = "attribute"
            if target is not None and target.type == "identifier":
                result.append(
                    _Declaration(
                        name=content[target.start_byte : target.end_byte].decode(
                            "utf-8"
                        ),
                        kind=kind,
                        start=tuple(target.start_point),
                        end=tuple(target.end_point),
                        extent=(*node.start_point, *node.end_point),
                        parent=parent,
                    )
                )
        for child in node.named_children:
            visit(child, parent)

    visit(tree.root_node, None)
    return result


def _binding_key(relative, symbol):
    return (relative if symbol.startswith("local ") else "", symbol)


def _source_binding(declaration):
    """A repeated class binding also shares its member's static target set.

    Function parameters keep their physical function scope. A member of two
    alternative declarations of the same class name can target either class,
    while classes in different enclosing functions remain separate bindings.
    """

    def scope(parent):
        if parent is not None and parent.kind == "class":
            return (scope(parent.parent), parent.name)
        return parent

    return scope(declaration.parent), declaration.name


def _local_binding_declarations(source, by_position, observed):
    """Keep compiler definition anchors sharing a local callable's binding.

    Local IDs identify one document-scoped lexical binding, not a physical
    declaration. A prior import, assignment or loop target can share that ID
    with a later class/function. References conservatively retain all its
    source-declared targets; this does not infer a runtime call path.
    """

    names = {
        d.original: d.name
        for d in observed
        if d.kind in {"function", "class"} and d.original.startswith("local ")
    }
    occupied = {d.start for d in observed}
    result = []
    for position, occurrences in by_position.items():
        if position in occupied:
            continue
        for occurrence in occurrences:
            name = names.get(occurrence.symbol)
            if name is None or not occurrence.symbol_roles & _DEFINITION:
                continue
            line, start, end_line, end = occurrence.range
            if line != end_line or (
                source.lines[line].encode("utf-8")[start:end].decode("utf-8") != name
            ):
                raise ValueError("Python local binding has an inconsistent source name")
            scopes = [
                d
                for d in source.declarations
                if d.kind in {"function", "class"}
                and tuple(d.extent[:2]) <= position < tuple(d.extent[2:])
            ]
            parent = (
                min(scopes, key=lambda d: (d.extent[2] - d.extent[0], -d.extent[1]))
                if scopes
                else None
            )
            result.append(
                _Declaration(
                    name=name,
                    kind="assignment",
                    start=position,
                    end=(end_line, end),
                    extent=tuple(occurrence.range),
                    parent=parent,
                    occurrence=occurrence,
                    original=occurrence.symbol,
                )
            )
            occupied.add(position)
    return result


def _parameter_assignment_target(source, position, original, family):
    """A parameter reassignment belongs to its lexical parameter binding.

    LSP legitimately returns the latest assignment instead of the argument's
    declaration. Require the compiler's same parameter symbol at that exact
    source anchor and follow only the owning scope or explicit nonlocal scopes.
    """

    if original not in source.parameter_anchors.get(position, ()):
        return None
    scopes = [
        d
        for d in source.declarations
        if d.kind in {"class", "function"}
        and tuple(d.extent[:2]) <= position < tuple(d.extent[2:])
    ]
    if not scopes:
        return None
    owner = min(scopes, key=lambda d: (d.extent[2] - d.extent[0], -d.extent[1]))
    name = family[0][1].name
    while owner is not None:
        matches = [d for _, d in family if d.kind == "parameter" and d.parent is owner]
        if len(matches) == 1:
            return matches[0]
        if name not in owner.nonlocals:
            return None
        owner = owner.parent
    return None


def _parameter_selection_target(source, position, span, family):
    """LSP may select `*args`/`**kwargs`; SCIP selects the identifier only."""

    end = span.get("end") or {}
    end_line, end_column = end.get("line"), end.get("character")
    if type(end_line) is not int or type(end_column) is not int:
        return None
    end_position = (end_line, source.column(end_line, end_column, 2))
    matches = []
    for _, declaration in family:
        if declaration not in source.declarations or declaration.kind != "parameter":
            continue
        if declaration.start[0] != position[0] or declaration.end > end_position:
            continue
        prefix = source.lines[position[0]].encode("utf-8")[
            position[1] : declaration.start[1]
        ]
        if prefix in {b"*", b"**"}:
            matches.append(declaration)
    return matches[0] if len(matches) == 1 else None


def normalize_python_index(
    index: Index,
    project_root: str | Path,
    *,
    resolve_definitions: DefinitionResolver | None = None,
    path_allowed: Callable[[str], bool] | None = None,
) -> dict:
    """Normalize a derived index atomically; retain raw ``index.scip`` upstream.

    No mutation is published until all positional targets and source bytes
    validate. The compiler's existing source receipt remains the publication
    authority. One call-scoped LSP client is acquired lazily and registered for
    shutdown before startup, including startup failures and cancellation.
    """

    from ..code_chunking.base import BaseCodeChunker
    from .scip_pb2 import Index as SCIPIndex

    root = Path(project_root).resolve()
    candidate = SCIPIndex()
    candidate.CopyFrom(index)
    parser = BaseCodeChunker._create_parser(
        BaseCodeChunker._get_tree_sitter_language("python")
    )
    sources = {}
    declarations_by_position = {}
    families = defaultdict(list)
    all_declarations = []
    replacement_sources = []
    source_only = []
    unmapped_attributes = []
    for document in candidate.documents:
        relative = document.relative_path
        if path_allowed is not None and not path_allowed(relative):
            continue
        path = root / relative
        if (
            not relative
            or "\\" in relative
            or any(part in {"", ".", ".."} for part in relative.split("/"))
        ):
            raise ValueError(f"noncanonical Python SCIP source path: {relative!r}")
        path.resolve(strict=True).relative_to(root)
        if relative in sources:
            raise ValueError(f"duplicate Python SCIP source document: {relative!r}")
        content = path.read_bytes()
        text = content.decode("utf-8", errors="replace")
        if text.encode("utf-8") != content:
            replacement_sources.append(relative)
        source = _Source(path, content, text.split("\n"))
        sources[relative] = source
        encoding = document.position_encoding or 2  # pinned scip-python uses JS offsets
        by_position = defaultdict(list)
        for occurrence in document.occurrences:
            if not occurrence.range:
                continue
            occurrence.range[:] = _convert_range(occurrence.range, source, encoding)
            if occurrence.enclosing_range:
                occurrence.enclosing_range[:] = _convert_range(
                    occurrence.enclosing_range, source, encoding
                )
            by_position[tuple(occurrence.range[:2])].append(occurrence)
            if _PARAMETER.search(occurrence.symbol):
                line, column, end_line, end = occurrence.range
                if line == end_line and (
                    source.lines[line].encode("utf-8")[column:end].decode("utf-8")
                    == _name(occurrence.symbol)
                ):
                    source.parameter_anchors.setdefault((line, column), set()).add(
                        occurrence.symbol
                    )
        document.position_encoding = 1
        module_namespace = next(
            (
                _namespace(o.symbol)
                for o in document.occurrences
                if o.symbol_roles & _DEFINITION and o.symbol.endswith("/__init__:")
            ),
            None,
        )
        project_prefix = (
            _symbol_parts(module_namespace)[0] if module_namespace else None
        )
        source.declarations = _declarations(source, parser)
        observed = []
        assignments = []
        for declaration in source.declarations:
            matching = [
                occurrence
                for occurrence in by_position.get(declaration.start, ())
                if occurrence.symbol
                and tuple(occurrence.range[2:]) == declaration.end
                and (
                    occurrence.symbol.startswith("local ")
                    or (
                        _name(occurrence.symbol) == declaration.name
                        and not occurrence.symbol.startswith(
                            "scip-python python python-stdlib "
                        )
                        and (
                            project_prefix is None
                            or _symbol_parts(occurrence.symbol)[0] == project_prefix
                        )
                    )
                )
            ]
            if not matching:
                continue
            originals = {occurrence.symbol for occurrence in matching}
            if len(originals) != 1:
                raise ValueError("Python definition has conflicting provider symbols")
            declaration.occurrence = matching[0]
            declaration.original = matching[0].symbol
            if declaration.kind == "attribute":
                if not matching[0].symbol_roles & _DEFINITION:
                    continue
                if declaration.original.startswith("local "):
                    continue
                owner = declaration.parent
                while owner is not None and owner.kind != "class":
                    owner = owner.parent
                parts = list(
                    _DESCRIPTOR.finditer(_symbol_parts(declaration.original)[1])
                )
                if (
                    owner is None
                    or len(parts) < 2
                    or parts[-2][3] != "#"
                    or _CLASS_MARKER.sub("", _unescape(parts[-2][1])) != owner.name
                ):
                    unmapped_attributes.append(
                        {
                            "file": relative,
                            "line": declaration.start[0],
                            "character": declaration.start[1],
                            "symbol": declaration.original,
                        }
                    )
                    continue
                declaration.parent = owner
                declaration.kind = "assignment"
            if declaration.kind == "assignment":
                assignments.append(declaration)
            else:
                observed.append(declaration)
        for declaration in source.declarations:
            if declaration.kind not in {"class", "function"} or declaration in observed:
                continue
            # scip-python deliberately omits some names such as `_`, while
            # still emitting their parameters. Source syntax proves the
            # physical definition; an observed parameter can also bind the
            # compiler's function identity without guessing a reference.
            owners = {
                d.original[: _PARAMETER.search(d.original).start()]
                for d in observed
                if d.parent is declaration
                and d.kind == "parameter"
                and not d.original.startswith("local ")
                and _PARAMETER.search(d.original)
            }
            if len(owners) == 1 and _name(next(iter(owners))) == declaration.name:
                declaration.original = next(iter(owners))
            observed.append(declaration)
        callable_counts = Counter(
            d.original for d in observed if d.kind in {"function", "class"}
        )
        observed.extend(
            d
            for d in assignments
            if callable_counts[d.original]
            or (
                not d.original.startswith("local ")
                and not _PARAMETER.search(d.original)
                and d.occurrence.symbol_roles & _DEFINITION
            )
        )
        observed.extend(_local_binding_declarations(source, by_position, observed))
        observed.sort(key=lambda d: d.start)
        counts = Counter((d.parent, d.name, d.kind) for d in observed)
        for declaration in observed:
            parent = declaration.parent
            if parent is not None and not parent.symbol:
                raise ValueError(
                    f"Python declaration at {relative}:{declaration.start[0] + 1} "
                    f"has an unobserved enclosing scope {parent.name!r}"
                )
            prefix = (
                parent.symbol
                if parent is not None
                else (
                    module_namespace
                    if not declaration.original
                    or declaration.original.startswith("local ")
                    else _namespace(declaration.original)
                )
            )
            if not prefix:
                raise ValueError("Python local declaration has no module namespace")
            line, column = declaration.start
            duplicate = counts[(parent, declaration.name, declaration.kind)] > 1
            if declaration.kind == "parameter":
                descriptor = "(" + _escape(declaration.name) + ")"
            elif declaration.kind == "class":
                suffix = f"@codenib-l{line}-c{column}" if duplicate else ""
                descriptor = _escape(declaration.name + suffix) + "#"
            elif declaration.kind == "assignment":
                suffix = (
                    f"@codenib-l{line}-c{column}"
                    if duplicate
                    or callable_counts[declaration.original]
                    or _TERM_MARKER.search(declaration.original)
                    else ""
                )
                descriptor = _escape(declaration.name + suffix) + "."
            else:
                marker = f"_codenib_l{line}_c{column}" if duplicate else ""
                descriptor = _escape(declaration.name) + f"({marker})."
            declaration.symbol = prefix + descriptor
            if declaration.original:
                families[_binding_key(relative, declaration.original)].append(
                    (relative, declaration)
                )
            if declaration.occurrence is None:
                occurrence = document.occurrences.add()
                occurrence.range.extend((*declaration.start, *declaration.end))
                occurrence.symbol = declaration.symbol
                occurrence.symbol_roles = _DEFINITION
                occurrence.enclosing_range.extend(declaration.extent)
                source_only.append(
                    {
                        "file": relative,
                        "name": declaration.name,
                        "line": declaration.start[0],
                        "character": declaration.start[1],
                        "compiler_owner_from_parameter": bool(declaration.original),
                    }
                )
            declarations_by_position[(relative, *declaration.start)] = declaration
            all_declarations.append(declaration)

    changed = {
        original: rows
        for original, rows in families.items()
        if len(rows) > 1 or any(d.symbol != original[1] for _, d in rows)
    }
    changed_files = {relative for rows in changed.values() for relative, _ in rows}
    # Several physical declarations can intentionally define one lexical
    # binding (branches, reassignments, or property accessors). Keep their
    # static union. LSP is needed when a provider ID spans different bindings,
    # such as an enclosing method and a same-named inner function.
    shared_binding_families = {
        key
        for key, rows in changed.items()
        if len({_source_binding(d) for _, d in rows}) == 1
    }
    queries = 0
    restored = 0
    provider = None
    deadline = time.monotonic() + _REPAIR_TIMEOUT
    with ExitStack() as cleanup:
        client = None

        def resolve(relative, line, column):
            nonlocal client, queries, provider
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "Python identity recovery exceeded its 120s deadline"
                )
            queries += 1
            source = sources[relative]
            character = source.utf16(line, column)
            if resolve_definitions is not None:
                return resolve_definitions(relative, line, character)
            from ..graph.incremental.lsp_client import LSPClient
            from ..toolchains import resolve_command

            if client is None:
                command = LSPClient.get_lsp_command("python")
                if not command or resolve_command(command[0]) is None:
                    raise RuntimeError(
                        "Python identity recovery requires basedpyright-langserver; "
                        "run `codenib toolchain install --scope lsp`"
                    )
                command[0] = resolve_command(command[0])
                client = LSPClient(command, str(root), "python")
                cleanup.callback(client.shutdown)
                initialized = client.start(
                    skip_probe=True,
                    initialize_timeout=max(0.001, deadline - time.monotonic()),
                )
                position_encoding = initialized.get("capabilities", {}).get(
                    "positionEncoding", "utf-16"
                )
                if position_encoding != "utf-16":
                    raise ValueError(
                        "Python identity resolver requires UTF-16 LSP positions"
                    )
                provider = {
                    "command": command,
                    "server_info": initialized.get("serverInfo"),
                    "position_encoding": position_encoding,
                }
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(
                    "Python identity recovery exceeded its 120s deadline"
                )
            return client.definition(
                str(source.path), line, character, timeout=min(10.0, remaining)
            )

        for document in candidate.documents:
            relative = document.relative_path
            if relative not in sources:
                continue
            source = sources[relative]
            rewritten = []
            for occurrence in document.occurrences:
                family = changed.get(_binding_key(relative, occurrence.symbol))
                if not family:
                    rewritten.append(occurrence)
                    continue
                position = tuple(occurrence.range[:2])
                own = declarations_by_position.get((relative, *position))
                if own is not None and own.original == occurrence.symbol:
                    if not occurrence.symbol_roles & _DEFINITION:
                        restored += 1
                    occurrence.symbol = own.symbol
                    occurrence.symbol_roles = (
                        occurrence.symbol_roles | _DEFINITION
                    ) & ~_READ
                    if own.kind in {"class", "function"}:
                        occurrence.enclosing_range[:] = own.extent
                    elif own.kind == "assignment":
                        del occurrence.enclosing_range[:]
                    rewritten.append(occurrence)
                    continue
                if (
                    len(family) == 1
                    or occurrence.symbol.startswith("local ")
                    or _binding_key(relative, occurrence.symbol)
                    in shared_binding_families
                ):
                    targets = [d for _, d in family]
                else:
                    targets = []
                    locations = resolve(relative, *position)
                    for location in locations or ():
                        uri = location.get("targetUri") or location.get("uri")
                        parsed = urlparse(str(uri or ""))
                        if parsed.scheme != "file" or parsed.netloc not in {
                            "",
                            "localhost",
                        }:
                            raise ValueError(
                                "Python identity target is not a local file"
                            )
                        target_file = (
                            Path(url2pathname(parsed.path))
                            .resolve()
                            .relative_to(root)
                            .as_posix()
                        )
                        target_source = sources.get(target_file)
                        if target_source is None:
                            raise ValueError(
                                "Python identity target has no indexed source"
                            )
                        span = (
                            location.get("targetSelectionRange")
                            or location.get("range")
                            or {}
                        )
                        start = span.get("start") or {}
                        line, character = start.get("line"), start.get("character")
                        if type(line) is not int or type(character) is not int:
                            raise ValueError(
                                "Python identity target has an invalid range"
                            )
                        column = target_source.column(line, character, 2)
                        target = declarations_by_position.get(
                            (target_file, line, column)
                        )
                        if target is None and _PARAMETER.search(occurrence.symbol):
                            target = _parameter_selection_target(
                                target_source, (line, column), span, family
                            )
                        if target is None and _PARAMETER.search(occurrence.symbol):
                            target = _parameter_assignment_target(
                                target_source, (line, column), occurrence.symbol, family
                            )
                        if target is None or target not in [d for _, d in family]:
                            raise ValueError(
                                "Python identity target is unresolved at "
                                f"{target_file}:{line + 1}:{column} "
                                f"for {relative}:{position[0] + 1}:{position[1]} "
                                f"({occurrence.symbol!r}; "
                                f"target={getattr(target, 'original', None)!r})"
                            )
                        if target not in targets:
                            targets.append(target)
                    if not targets:
                        raise ValueError(
                            "Python identity reference is unresolved at "
                            f"{relative}:{position[0] + 1}"
                        )
                for target in targets:
                    copy = type(occurrence)()
                    copy.CopyFrom(occurrence)
                    copy.symbol = target.symbol
                    rewritten.append(copy)
            rewritten.sort(key=lambda occurrence: tuple(occurrence.range[:2]))
            del document.occurrences[:]
            document.occurrences.extend(rewritten)
            # Ambiguous provider documentation has no source position. Do not
            # attach an inner function's signature to its enclosing method.
            retained = [
                s
                for s in document.symbols
                if _binding_key(relative, s.symbol) not in changed
            ]
            del document.symbols[:]
            document.symbols.extend(retained)
    for relative, source in sources.items():
        if source.path.read_bytes() != source.content:
            raise RuntimeError(
                f"Python source changed during identity recovery: {relative}"
            )
    index.CopyFrom(candidate)
    return {
        "schema": 1,
        "source_file_count": len(sources),
        "changed_symbol_families": len(changed),
        "physical_declarations": len(all_declarations),
        "restored_definition_roles": restored,
        "positional_definition_queries": queries,
        "local_binding_families": sum(bool(scope) for scope, _ in changed),
        "local_binding_semantics": "all_source_declarations_for_compiler_binding",
        "shared_lexical_binding_families": len(shared_binding_families),
        "utf8_replacement_sources": replacement_sources,
        "source_only_definitions": source_only,
        "unmapped_attribute_definitions": unmapped_attributes,
        "definition_provider": provider,
        "position_encoding": "UTF8",
        "source_sha256": {
            relative: hashlib.sha256(source.content).hexdigest()
            for relative, source in sources.items()
            if relative in changed_files
        },
    }
