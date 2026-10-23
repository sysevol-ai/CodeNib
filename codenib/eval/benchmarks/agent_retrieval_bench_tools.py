# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""CodeNib symbol and reference lookups for ARB's indexed-interactive mode.

ARB loads this provider with
``--tool-provider codenib.eval.benchmarks.agent_retrieval_bench_tools:CodeNibToolProvider``
and gates every result itself; see ARB's ``indexed_tools`` module for the
interface. Each snapshot's graph comes from SCIP/LSP indexing of a clean
checkout at its ``base_commit``, as for the static ``graph_expansion`` method,
so the lookups carry full-source information. Hits are limited to released
files and report 1-based, inclusive line ranges.

- ``symbol_search(symbol)``: definitions whose name is ``symbol``. A dotted or
  ``::`` name matches the end of the qualified name; exact case ranks first.
- ``references(path, symbol)``: other files whose symbols reference, or are
  referenced by, ``symbol`` defined in ``path`` or the symbols it contains,
  ordered by the number of connecting references.

Graphs are cached under ``cache`` per snapshot, so later runs reuse a build
and report its original cost with ``cached: true``. A cached graph is reused
only when the CodeNib sources and graph settings that built it match the
current ones. A failed build is retried on the next run unless
``retry_failed`` is false, which keeps index availability fixed across
arms that share a prebuilt cache.
"""

from __future__ import annotations

import contextlib
import json
import re
import shutil
import subprocess
import time
from pathlib import Path, PurePosixPath
from typing import Any

from codenib.types import (
    EDGE_TYPE_CONTAIN,
    EDGE_TYPE_REFERENCE,
    NODE_TYPE_FIELD,
    NODE_TYPE_FILE,
    is_symbol_node,
    node_has_definition,
)

from . import _arb_methods as methods
from .agent_retrieval_bench import implementation_receipt

_RECEIPT = "provider_receipt.json"
_SEPARATORS = re.compile(r"::|[.#/]")
# Parameter lists such as ``run(String)`` are not part of a lookup name.
_PARAMETERS = re.compile(r"\([^()]*\)")


class CodeNibToolProvider:
    def __init__(
        self,
        workspace: str,
        cache: str | None = None,
        retry_failed: bool | str = True,
    ):
        self.workspace = Path(workspace).resolve()
        self.cache = Path(cache).resolve() if cache else self.workspace / "tool-graphs"
        # Graphs live below cache/graphs and checkouts directly below the
        # workspace, so a graph directory is never another provider's checkout,
        # even when two providers swap their roots.
        self._graphs = self.cache / "graphs"
        if self.workspace in (self.cache, self._graphs):
            raise ValueError("workspace must differ from cache and from cache/graphs")
        self.retry_failed = _flag(retry_failed)
        self._snapshots: dict[tuple[str, str], _SnapshotIndex | None] = {}
        implementation = implementation_receipt()
        self._identity = {
            "codenib_sources_sha256": implementation["codenib_sources_sha256"],
            "python": implementation["python"],
            "packages": implementation["packages"],
            "graph_tools_sha256": _graph_tools(),
            "graph_build_timeout_seconds": methods.GRAPH_BUILD_TIMEOUT,
            "scip_python_index_timeout_seconds": _scip_python_timeout(),
            "min_language_share": methods.GRAPH_MIN_LANGUAGE_SHARE,
        }

    def describe(self) -> dict[str, Any]:
        return {
            "name": "codenib",
            "commit": _git_commit(),
            "source_access": "full-source",
            "retry_failed": self.retry_failed,
            **self._identity,
        }

    def prepare(self, *, repo: str, base_commit: str, paths: list[str]) -> dict:
        released = set(paths)
        directory = methods.snapshot_path(repo, base_commit, self._graphs)
        directory.parent.mkdir(parents=True, exist_ok=True)
        checkout = methods.snapshot_path(repo, base_commit, self.workspace)
        checkout.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.ExitStack() as held:
            # Both snapshot locks, in one global order, so no two providers
            # can each hold one and wait for the other.
            for lock in sorted(
                (methods.snapshot_lock(directory), methods.snapshot_lock(checkout)),
                key=lambda lock: lock.lock_file,
            ):
                held.enter_context(lock)
            _finish_swap(directory)
            receipt_path = directory / _RECEIPT
            receipt = _read_receipt(receipt_path)
            languages = methods.snapshot_languages(
                [{"path": path, "kind": "file"} for path in paths]
            )
            cached = self._reusable(receipt, languages)
            if not cached:
                self._build(repo, base_commit, languages, directory, receipt)
                receipt = _read_receipt(receipt_path)
            # Load under the lock, so another provider cannot replace the
            # directory between reading the receipt and loading its graph.
            graph_path = directory / "arb_graph.pkl"
            index = None
            if receipt.get("graph") and graph_path.is_file():
                from codenib.graph.code_graph import CodeGraph

                index = _SnapshotIndex(
                    CodeGraph.load_graph(str(graph_path)),
                    released,
                    receipt.get("languages") or [],
                )
        self._snapshots[(repo, base_commit)] = index
        status = {
            key: receipt[key]
            for key in (
                "languages",
                "available",
                "failed",
                "error",
                "build_seconds",
                "fetch_seconds",
            )
            if key in receipt
        }
        if not cached and "last_retry" in receipt:
            # This run paid for the retry, not for the build it kept.
            for key in ("build_seconds", "fetch_seconds"):
                status[key] = receipt["last_retry"].get(key, status.get(key))
        status.update(cached=cached, available=index is not None)
        if index is not None:
            status["size"] = index.size
        else:
            status["reason"] = receipt.get("error") or (
                "no graph language"
                if not receipt.get("languages")
                else "no graph built"
            )
        return status

    def _reusable(self, receipt: dict | None, languages: list[str]) -> bool:
        if receipt is None or receipt.get("identity") != self._identity:
            return False
        # Another released file set can call for other graph languages.
        if receipt.get("languages") != languages:
            return False
        # A snapshot without graph languages has nothing to retry; a graph
        # missing one of its languages counts as a failed build.
        failed = receipt.get("languages") and (
            not receipt.get("graph") or receipt.get("failed")
        )
        return not (failed and self.retry_failed)

    def _build(
        self,
        repo: str,
        base_commit: str,
        languages: list[str],
        directory: Path,
        previous: dict | None = None,
    ) -> None:
        started = time.perf_counter()
        staging = directory.with_name(directory.name + ".partial")
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        receipt: dict[str, Any] = {
            "identity": self._identity,
            "languages": languages,
            "graph": False,
        }
        fetch_seconds = 0.0
        try:
            if languages:
                # prepare() holds the checkout's snapshot lock.
                fetched = time.perf_counter()
                try:
                    checkout = methods.fetch_snapshot(repo, base_commit, self.workspace)
                finally:
                    # A failed fetch is still acquisition, not build time.
                    fetch_seconds = time.perf_counter() - fetched
                report = methods.build_graph(checkout, staging, languages)
                receipt["available"] = report.get("available_languages", [])
                if report.get("failed_languages"):
                    receipt["failed"] = {
                        lang: str(reason)[:300]
                        for lang, reason in report["failed_languages"].items()
                    }
                if report.get("graph"):
                    built = Path(report["graph"])
                    if built != staging / "arb_graph.pkl":
                        shutil.move(str(built), staging / "arb_graph.pkl")
                    receipt["graph"] = True
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            receipt["error"] = f"{type(exc).__name__}: {exc}"[:500]
        # Fetching source is acquisition, not index construction.
        receipt["build_seconds"] = time.perf_counter() - started - fetch_seconds
        receipt["fetch_seconds"] = fetch_seconds
        usable = (
            previous is not None
            and previous.get("graph")
            and previous.get("identity") == self._identity
            and previous.get("languages") == languages
        )
        # A retry replaces a usable graph only if it covers every language
        # the old one did; otherwise the old graph stays.
        retry_lost_coverage = usable and (
            not receipt["graph"]
            or not set(previous.get("available") or [])
            <= set(receipt.get("available") or [])
        )
        if retry_lost_coverage:
            # The kept graph records the attempt that did not replace it.
            shutil.rmtree(staging, ignore_errors=True)
            _write_atomically(directory / _RECEIPT, {**previous, "last_retry": receipt})
            return
        (staging / _RECEIPT).write_text(json.dumps(receipt) + "\n", encoding="utf-8")
        # Retire the live graph rather than delete it, so an interrupted swap
        # leaves a graph for _finish_swap to restore.
        # Only a graph this provider published is replaced, never, say, a
        # checkout of a workspace that shares the root; nothing is removed
        # before that is known.
        if directory.exists() and not (directory / _RECEIPT).is_file():
            shutil.rmtree(staging, ignore_errors=True)
            raise ValueError(f"{directory} holds no tool graph; refusing to replace it")
        retired = _retired(directory)
        shutil.rmtree(retired, ignore_errors=True)
        if directory.exists():
            directory.rename(retired)
        staging.rename(directory)
        shutil.rmtree(retired, ignore_errors=True)

    def call(
        self,
        *,
        tool: str,
        arguments: dict[str, str],
        repo: str,
        base_commit: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        index = self._snapshots.get((repo, base_commit))
        if index is None:
            raise RuntimeError(f"no graph prepared for {repo}@{base_commit}")
        if tool == "symbol_search":
            return index.symbol_search(arguments["symbol"], limit)
        if tool == "references":
            return index.references(arguments["path"], arguments["symbol"], limit)
        raise ValueError(f"unknown tool {tool!r}")


class _SnapshotIndex:
    """Name lookups over one loaded graph, restricted to released files."""

    def __init__(self, graph: Any, released: set[str], languages: list[str]):
        from codenib.languages import extension_to_language_map

        self._graph = graph
        self._released = released
        self._by_name: dict[str, list[int]] = {}
        g = graph.graph
        routes = extension_to_language_map("graph")
        source = {
            path
            for path in released
            if routes.get(PurePosixPath(path).suffix.lower()) in languages
        }
        graph_files = {
            vertex["name"] for vertex in g.vs if vertex["type"] == NODE_TYPE_FILE
        }
        for vertex in g.vs:
            attributes = vertex.attributes()
            if not is_symbol_node(attributes.get("type")):
                continue
            # Reference-only vertices (imports, unresolved names) are not
            # definitions, even when a decoder files them under the importer.
            if not node_has_definition(attributes):
                continue
            if attributes.get("file") not in released:
                continue
            self._by_name.setdefault(_bare(_lookup(attributes)).lower(), []).append(
                vertex.index
            )
        self.size = {
            "vertices": g.vcount(),
            "edges": g.ecount(),
            "reference_edges": sum(
                edge["type"] == EDGE_TYPE_REFERENCE for edge in g.es
            ),
            "released_symbols": sum(len(ids) for ids in self._by_name.values()),
            # Released files in the graph's languages, and how many it covers.
            "released_source_files": len(source),
            "covered_source_files": len(source & graph_files),
        }

    def symbol_search(self, symbol: str, limit: int) -> list[dict[str, Any]]:
        matches = self._matching(symbol)
        return [self._hit(vertex) for vertex in matches[:limit]]

    def references(self, path: str, symbol: str, limit: int) -> list[dict[str, Any]]:
        g = self._graph.graph
        roots = [
            vertex for vertex in self._matching(symbol) if self._file(vertex) == path
        ]
        scope = set()
        for root in roots:
            scope.update(self._contained(root))
        counts: dict[tuple[int, str], int] = {}
        anchors: dict[tuple[int, str], int] = {}
        for vertex in scope:
            for mode, relation in (("in", "uses symbol"), ("out", "used by symbol")):
                for edge in g.incident(vertex, mode=mode):
                    if g.es[edge]["type"] != EDGE_TYPE_REFERENCE:
                        continue
                    other = g.es[edge].source if mode == "in" else g.es[edge].target
                    other_path = self._file(other)
                    if other in scope or other_path == path:
                        continue
                    if other_path not in self._released:
                        continue
                    key = (other, relation)
                    counts[key] = counts.get(key, 0) + 1
                    # File-scope references have no symbol span; keep the
                    # first line where the reference occurs, when that site
                    # is in the hit's own file.
                    edge_attributes = g.es[edge].attributes()
                    line = edge_attributes.get("anchor_line")
                    if (
                        edge_attributes.get("anchor_file") == other_path
                        and isinstance(line, int)
                        and line >= 0
                    ):
                        anchors[key] = min(anchors.get(key, line), line)
        ranked = sorted(
            counts,
            key=lambda key: (-counts[key], self._file(key[0]), self._start(key[0])),
        )
        hits = []
        for vertex, relation in ranked[:limit]:
            hit = self._hit(vertex)
            line = anchors.get((vertex, relation))
            if "start_line" not in hit and line is not None:
                hit["start_line"] = hit["end_line"] = line + 1
            hit["relation"] = f"{relation}, {counts[(vertex, relation)]} refs"
            hits.append(hit)
        return hits

    def _matching(self, symbol: str) -> list[int]:
        query = _PARAMETERS.sub("", symbol.strip().strip("`'\""))
        parts = [part for part in _SEPARATORS.split(query) if part]
        if not parts:
            return []
        candidates = self._by_name.get(parts[-1].lower(), [])
        wanted = ".".join(parts)

        def qualified(vertex: int) -> str:
            name = _lookup(self._graph.graph.vs[vertex].attributes())
            return ".".join(part for part in _SEPARATORS.split(name) if part)

        def suffix(name: str, target: str) -> bool:
            return name == target or name.endswith("." + target)

        exact = [v for v in candidates if suffix(qualified(v), wanted)]
        folded = [
            v
            for v in candidates
            if v not in exact and suffix(qualified(v).lower(), wanted.lower())
        ]
        return sorted(exact, key=self._order) + sorted(folded, key=self._order)

    def _order(self, vertex: int) -> tuple:
        attributes = self._graph.graph.vs[vertex].attributes()
        # Fields rank after definitions of types and callables.
        return (
            attributes.get("type") == NODE_TYPE_FIELD,
            attributes.get("file") or "",
            self._start(vertex),
        )

    def _contained(self, root: int) -> list[int]:
        g = self._graph.graph
        found, pending = [root], [root]
        while pending:
            for edge in g.incident(pending.pop(), mode="out"):
                child = g.es[edge].target
                if (
                    g.es[edge]["type"] == EDGE_TYPE_CONTAIN
                    and g.vs[child]["type"] != NODE_TYPE_FILE
                ):
                    found.append(child)
                    pending.append(child)
        return found

    def _file(self, vertex: int) -> str:
        attributes = self._graph.graph.vs[vertex].attributes()
        if attributes.get("type") == NODE_TYPE_FILE:
            return attributes.get("name") or ""
        return attributes.get("file") or ""

    def _start(self, vertex: int) -> int:
        value = self._graph.graph.vs[vertex].attributes().get("start_line")
        return value if isinstance(value, int) else -1

    def _hit(self, vertex: int) -> dict[str, Any]:
        attributes = self._graph.graph.vs[vertex].attributes()
        is_file = attributes.get("type") == NODE_TYPE_FILE
        hit: dict[str, Any] = {
            "path": self._file(vertex),
            "symbol": "" if is_file else _qualified(attributes),
            "kind": attributes.get("type") or "",
        }
        start, end = attributes.get("start_line"), attributes.get("end_line")
        # Graph lines are 0-based; ARB hits are 1-based and inclusive.
        if isinstance(start, int) and start >= 0:
            hit["start_line"] = start + 1
            if isinstance(end, int) and end >= start:
                hit["end_line"] = end + 1
        return hit


def _qualified(attributes: dict[str, Any]) -> str:
    """The symbol's name inside its file, without a trailing ``()``."""
    display = attributes.get("unified_name") or attributes.get("name") or ""
    prefix = f"{attributes.get('file') or ''}:"
    if prefix != ":" and display.startswith(prefix):
        display = display[len(prefix) :]
    elif ":" in display and "::" not in display:
        display = display.split(":", 1)[1]
    return display.removesuffix("()")


def _lookup(attributes: dict[str, Any]) -> str:
    """The qualified name without parameter lists, for matching."""
    return _PARAMETERS.sub("", _qualified(attributes))


def _bare(name: str) -> str:
    parts = [part for part in _SEPARATORS.split(name) if part]
    return parts[-1] if parts else name


def _flag(value: bool | str) -> bool:
    """A provider option that may arrive as a command-line string."""
    if isinstance(value, bool):
        return value
    if value.strip().lower() in {"1", "true", "yes"}:
        return True
    if value.strip().lower() in {"0", "false", "no"}:
        return False
    raise ValueError(f"expected true or false, got {value!r}")


def _graph_tools() -> dict[str, dict[str, Any]]:
    """Every graph backend command the language registry can run: each graph
    language's SCIP and LSP command, with environment overrides applied, and
    the SHA-256 of its executable and of any file its arguments name (such as
    a launcher's JAR)."""
    from codenib.languages import (
        LANGUAGE_SPECS,
        lsp_command_for_language,
        scip_cold_start_command_for_language,
    )
    from codenib.toolchains import resolve_command

    from .agent_retrieval_bench import sha256_file

    backends: dict[str, dict[str, Any]] = {}
    for spec in LANGUAGE_SPECS:
        if not spec.graph_language:
            continue
        for kind, lookup in (
            ("scip", scip_cold_start_command_for_language),
            ("lsp", lsp_command_for_language),
        ):
            try:
                command = lookup(spec.graph_language)
            except Exception:  # A broken factory leaves that backend unhashed.
                command = None
            if not command:
                continue
            executable = resolve_command(command[0])
            files = {
                argument: sha256_file(Path(argument))
                for argument in command[1:]
                if Path(argument).is_file()
            }
            # Keyed by kind: SCIP and LSP may share a launcher such as java.
            backends[f"{spec.graph_language}:{kind}"] = {
                "command": list(command),
                "executable_sha256": (
                    sha256_file(Path(executable)) if executable else None
                ),
                "argument_files_sha256": files,
            }
    return backends


def _retired(directory: Path) -> Path:
    return directory.with_name(directory.name + ".previous")


def _finish_swap(directory: Path) -> None:
    """Settle a graph swap that stopped between its two renames."""
    retired = _retired(directory)
    if not retired.is_dir():
        return
    if not directory.exists():
        retired.rename(directory)
    elif (directory / _RECEIPT).is_file():
        shutil.rmtree(retired, ignore_errors=True)
    # Anything else at the live path is not ours; the retired graph stays.


def _write_atomically(path: Path, value: dict) -> None:
    """Replace ``path`` so readers see the old or the new receipt, never a part."""
    staging = path.with_name(path.name + ".writing")
    staging.write_text(json.dumps(value) + "\n", encoding="utf-8")
    staging.replace(path)


def _read_receipt(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _scip_python_timeout() -> float:
    from codenib.scip_interface.scip_indexer_python import _scip_python_index_timeout

    return _scip_python_index_timeout()


def _git_commit() -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(Path(__file__).resolve().parent), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None if result.returncode == 0 else None
