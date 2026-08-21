# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Graph-free manifest and receipt gates for native SCIP symbol queries."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
from functools import partial
from pathlib import Path
from typing import Any, Callable

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib

from codenib.compiler.artifact_fingerprints import regular_file_fingerprint
from codenib.compiler.manifest import (
    LEGACY_MANIFEST_VERSION,
    MANIFEST_VERSION,
    IndexEntry,
    RepoManifest,
)
from codenib.compiler.manifest_source import (
    fingerprint_repository_for_manifest,
    repository_filter_policy_for_manifest,
)
from codenib.repository_filters import (
    REPOSITORY_FILTER_POLICY_VERSION,
    default_exclude_patterns,
)
from codenib.repository_source_selection import RepositorySourceSelection
from codenib.scip_interface.scip_filter import (
    matches_exclude_patterns,
    normalize_scip_path,
    scip_path_is_allowed,
)
from codenib.scip_interface.scip_query import (
    FACT_QUERY_CAPABILITIES,
    FactQueryCandidateRejected,
    load_fact_query_candidate,
)
from codenib.source_fingerprint import fingerprint_repository

_PYTHON_PARITY_INDEX = r"""
documents {
  relative_path: "src/alpha.py"
  occurrences {
    range: 0
    range: 0
    range: 3
    symbol: "src.alpha`/run()."
    symbol_roles: 1
    enclosing_range: 0
    enclosing_range: 0
    enclosing_range: 4
    enclosing_range: 0
  }
  occurrences {
    range: 2
    range: 4
    range: 7
    symbol: "src.beta`/run()."
    symbol_roles: 8
  }
  occurrences {
    range: 3
    range: 4
    range: 8
    symbol: "scip-python python demo 0.1 `requests.models`/Response#"
    symbol_roles: 8
  }
}
documents {
  relative_path: "src/beta.py"
  occurrences {
    range: 0
    range: 0
    range: 3
    symbol: "src.beta`/run()."
    symbol_roles: 1
    enclosing_range: 0
    enclosing_range: 0
    enclosing_range: 4
    enclosing_range: 0
  }
  occurrences {
    range: 2
    range: 4
    range: 7
    symbol: "src.alpha`/run()."
    symbol_roles: 8
  }
}
"""

_RUST_PARITY_INDEX = r"""
documents {
  relative_path: "src/alpha.rs"
  occurrences {
    range: 0
    range: 0
    range: 3
    symbol: "rust-analyzer cargo demo 0.1 alpha/run()."
    symbol_roles: 1
    enclosing_range: 0
    enclosing_range: 0
    enclosing_range: 4
    enclosing_range: 0
  }
  occurrences {
    range: 2
    range: 4
    range: 7
    symbol: "rust-analyzer cargo demo 0.1 beta/run()."
    symbol_roles: 8
  }
  occurrences {
    range: 3
    range: 4
    range: 8
    symbol: "rust-analyzer cargo demo 0.1 missing/External#"
    symbol_roles: 8
  }
}
documents {
  relative_path: "src/beta.rs"
  occurrences {
    range: 0
    range: 0
    range: 3
    symbol: "rust-analyzer cargo demo 0.1 beta/run()."
    symbol_roles: 1
    enclosing_range: 0
    enclosing_range: 0
    enclosing_range: 4
    enclosing_range: 0
  }
  occurrences {
    range: 2
    range: 4
    range: 7
    symbol: "rust-analyzer cargo demo 0.1 alpha/run()."
    symbol_roles: 8
  }
}
"""


def _allowed_digest(paths: list[str]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _outcome(call: Callable[[], Any]) -> tuple[Any, ...]:
    try:
        return "ok", call()
    except Exception as exc:  # noqa: BLE001 - public error parity is required
        return "error", type(exc).__name__, str(exc)


class _MockIndex:
    fact_query_index = True
    materializes_graph = False
    capabilities = FACT_QUERY_CAPABILITIES
    record_count = 5
    edge_count = 4
    symbol_count = 1
    reference_count = 1
    occurrence_count = 0
    query_surface_sha256 = "0" * 64

    def __init__(self, *, proof_updates: dict[str, Any] | None = None):
        self._proof_updates = proof_updates or {}
        self.allowed_files: list[str] = []
        self.expected_query_surface_sha256: str | None = None

    def prove_filter_identity(
        self,
        allowed_files: list[str],
        expected_query_surface_sha256: str,
    ) -> dict[str, Any]:
        self.allowed_files = list(allowed_files)
        self.expected_query_surface_sha256 = expected_query_surface_sha256
        if expected_query_surface_sha256 != self.query_surface_sha256:
            raise ValueError("query surface digest mismatch")
        proof = {
            "schema_version": 1,
            "identity": True,
            "allowed_file_count": len(allowed_files),
            "allowed_files_sha256": _allowed_digest(allowed_files),
            "record_count": self.record_count,
            "directory_count": 1,
            "file_count": 1,
            "definition_count": 1,
            "reference_only_count": 1,
            "edge_count": self.edge_count,
            "reference_count": self.reference_count,
            "occurrence_count": self.occurrence_count,
            "query_surface_sha256": self.query_surface_sha256,
        }
        proof.update(self._proof_updates)
        return proof

    def has_symbol(self, name: str) -> bool:
        return name == "demo"


class _MockCore:
    def __init__(
        self,
        *,
        index: _MockIndex | None = None,
        receipt_updates: dict[str, Any] | None = None,
        failure: BaseException | None = None,
    ):
        self.index = index or _MockIndex()
        self.receipt_updates = receipt_updates or {}
        self.failure = failure
        self.calls: list[dict[str, Any]] = []

    @staticmethod
    def fact_query_index_contract() -> dict[str, Any]:
        return {
            "abi_version": 1,
            "format": "fact-query-index-v1",
            "requires_anchored_references": True,
            "filter_identity_proof_schema_version": 1,
            "input_receipt_schema_version": 1,
            "capabilities": dict(FACT_QUERY_CAPABILITIES),
        }

    def decode_scip_fact_query_index(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.failure is not None:
            raise self.failure
        index_path = Path(kwargs["index_file"])
        fingerprint = regular_file_fingerprint(index_path)
        language = kwargs["language"]
        root = Path(kwargs["project_root"])
        from codenib.scip_interface.query_surface import query_surface_sha256

        decoder_class = (
            __import__(
                "codenib.scip_interface.scip_decode_rust",
                fromlist=["SCIPRustGraphDecoder"],
            ).SCIPRustGraphDecoder
            if language == "rust"
            else __import__(
                "codenib.scip_interface.scip_decode_python",
                fromlist=["SCIPPythonGraphDecoder"],
            ).SCIPPythonGraphDecoder
        )
        serial_graph = decoder_class(str(index_path), project_root=str(root)).decode()
        self.index.query_surface_sha256 = query_surface_sha256(serial_graph)
        metadata: dict[str, Any] = {
            "kind": "none",
            "complete": True,
            "inputs": [],
            "internal_crates": [],
        }
        if language == "rust":
            cargo_paths = [root / "Cargo.toml"]
            cargo_paths.extend(sorted(root.glob("crates/*/Cargo.toml")))
            inputs = []
            crates = []
            for cargo_path in cargo_paths:
                cargo = regular_file_fingerprint(cargo_path)
                inputs.append(
                    {
                        "path": cargo_path.relative_to(root).as_posix(),
                        "size_bytes": cargo["size"],
                        "sha256": cargo["sha256"],
                    }
                )
                with cargo_path.open("rb") as handle:
                    package = tomllib.load(handle).get("package", {})
                if package.get("name"):
                    crates.append(package["name"])
            metadata = {
                "kind": "rust-cargo",
                "complete": True,
                "inputs": inputs,
                "internal_crates": sorted(crates),
            }
        receipt = {
            "schema_version": 1,
            "index": {
                "path": str(index_path),
                "size_bytes": fingerprint["size"],
                "sha256": fingerprint["sha256"],
            },
            "metadata": metadata,
        }
        receipt.update(copy.deepcopy(self.receipt_updates))
        return {
            "format": "fact-query-index-v1",
            "index": self.index,
            "native_decode_ns": 10,
            "native_index_ns": 5,
            "decode_profile_ns": {
                "worker_count": 1,
                "document_count": 1,
                "parse": 4,
            },
            "graph_materialized": False,
            "input_receipt": receipt,
        }


def _write_candidate_manifest(
    tmp_path: Path,
    *,
    language: str = "python",
    rust_workspace: bool = False,
    node_count: int = 5,
    edge_count: int = 4,
    manifest_version: str = MANIFEST_VERSION,
    source_selection: RepositorySourceSelection | None = None,
) -> tuple[Path, RepoManifest]:
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    if language == "rust":
        cargo_text = '[package]\nname = "demo"\nversion = "0.1.0"\n'
        if rust_workspace:
            cargo_text += '[workspace]\nmembers = ["crates/*"]\n'
        (root / "Cargo.toml").write_text(cargo_text, encoding="utf-8")
        if rust_workspace:
            for directory, crate in (("a", "alpha"), ("b", "beta")):
                member = root / "crates" / directory
                member.mkdir(parents=True)
                (member / "Cargo.toml").write_text(
                    f'[package]\nname = {json.dumps(crate)}\nversion = "0.1.0"\n',
                    encoding="utf-8",
                )
        source_path = root / "src" / "lib.rs"
        source_path.parent.mkdir()
        source_path.write_text("pub fn demo() {}\n", encoding="utf-8")
    else:
        source_path = root / "src" / "main.py"
        source_path.parent.mkdir()
        source_path.write_text("def demo():\n    return 1\n", encoding="utf-8")

    cache = root / ".codenib_cache"
    artifact_root = cache / "symbol_graph"
    artifact_root.mkdir(parents=True)
    decoded = artifact_root / "index.decoded"
    symbol = (
        "rust-analyzer cargo demo 0.1 demo()."
        if language == "rust"
        else "src.main`/demo()."
    )
    external_symbol = (
        "rust-analyzer cargo demo 0.1 missing/External#"
        if language == "rust"
        else "scip-python python demo 0.1 `requests.models`/Response#"
    )
    source_relative = "src/lib.rs" if language == "rust" else "src/main.py"
    decoded.write_text(
        "documents {\n"
        f"  relative_path: {json.dumps(source_relative)}\n"
        "  occurrences {\n"
        "    range: 0\n"
        "    range: 0\n"
        "    range: 4\n"
        f"    symbol: {json.dumps(symbol)}\n"
        "    symbol_roles: 1\n"
        "    enclosing_range: 0\n"
        "    enclosing_range: 0\n"
        "    enclosing_range: 4\n"
        "    enclosing_range: 0\n"
        "  }\n"
        "  occurrences {\n"
        "    range: 1\n"
        "    range: 4\n"
        "    range: 8\n"
        f"    symbol: {json.dumps(external_symbol)}\n"
        "    symbol_roles: 8\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    from codenib.scip_interface.query_surface import query_surface_sha256

    decoder_class = (
        __import__(
            "codenib.scip_interface.scip_decode_rust",
            fromlist=["SCIPRustGraphDecoder"],
        ).SCIPRustGraphDecoder
        if language == "rust"
        else __import__(
            "codenib.scip_interface.scip_decode_python",
            fromlist=["SCIPPythonGraphDecoder"],
        ).SCIPPythonGraphDecoder
    )
    serial_graph = decoder_class(str(decoded), project_root=str(root)).decode()
    query_digest = query_surface_sha256(serial_graph)
    graph_file = artifact_root / "graph.pkl"
    serial_graph.save_graph(str(graph_file))
    index_fingerprint = regular_file_fingerprint(decoded)
    graph_fingerprint = regular_file_fingerprint(graph_file)
    persisted_selection = None
    if manifest_version != LEGACY_MANIFEST_VERSION:
        persisted_selection = source_selection or RepositorySourceSelection()
    identity_manifest = RepoManifest(
        version=manifest_version,
        repo_path=str(root),
        source_selection=persisted_selection,
    )
    source = fingerprint_repository_for_manifest(
        root,
        identity_manifest,
        exclude_roots=(cache,),
    )
    repository_filter_policy = repository_filter_policy_for_manifest(identity_manifest)
    patterns = sorted(default_exclude_patterns())
    artifacts = {
        language: {
            "relative_path": "index.decoded",
            "size": index_fingerprint["size"],
            "sha256": index_fingerprint["sha256"],
        }
    }
    reports: dict[str, Any] = {}
    if language == "python":
        reports = {
            "python": {
                "backend": "scip-python",
                "status": "complete",
                "complete": True,
                "partial": False,
                "document_count": 1,
            }
        }
    else:
        reports = {
            "rust": {
                "backend": "rust-analyzer-scip",
                "status": "complete",
                "complete": True,
                "partial": False,
                "document_count": 1,
            }
        }
    metadata = {
        "builder_schema": (4 if manifest_version == LEGACY_MANIFEST_VERSION else 6),
        "languages": [language],
        "graph_route": "active",
        "exclude_patterns": patterns,
        "allow_partial_languages": False,
        "allow_partial_index": False,
        "source_coverage_fallback": False,
        "target_dir": None,
        "repository_filter_policy": repository_filter_policy,
        "node_count": node_count,
        "edge_count": edge_count,
        "language": language,
        "available_languages": [language],
        "compiler_available_languages": [language],
        "index_generation_reports": reports,
        "scip_decoded_artifacts": artifacts,
        "graph_artifact": {
            "relative_path": "graph.pkl",
            **graph_fingerprint,
        },
        "query_surface_sha256": query_digest,
        "update_mode": "full_rebuild",
        "partial_index": False,
        "failed_languages": {},
        "partial": False,
    }
    if persisted_selection is not None:
        metadata.update(
            {
                "allow_project_preparation": True,
                "lsp_occurrence_artifact": None,
                "source_selection_digest": persisted_selection.digest,
                "source_selection_report": {
                    "source_selection_digest": persisted_selection.digest,
                    "nodes_before": node_count,
                    "nodes_after": node_count,
                    "edges_before": edge_count,
                    "edges_after": edge_count,
                    "removed_excluded_path_count": 0,
                    "excluded_path_count_after": 0,
                    "invalid_path_count_before": 0,
                    "invalid_path_count_after": 0,
                },
            }
        )
    entry = IndexEntry(
        index_type="symbol_graph",
        path=str(artifact_root),
        built_at="2026-01-01T00:00:00+00:00",
        built_at_epoch=0.0,
        status="fresh",
        config=copy.deepcopy(metadata),
        metadata={**copy.deepcopy(metadata), "build_duration_seconds": 1.0},
        commit="",
        source_fingerprint=source.value,
        source_selection_digest=(
            "" if persisted_selection is None else persisted_selection.digest
        ),
    )
    manifest = RepoManifest(
        version=manifest_version,
        repo_path=str(root),
        source_fingerprint=source.value,
        last_indexed_source_fingerprint=source.value,
        source_selection=persisted_selection,
        last_indexed_source_selection_digest=(
            "" if persisted_selection is None else persisted_selection.digest
        ),
        languages=[language],
        file_count=source.file_count,
        indexes={"symbol_graph": entry},
    )
    manifest_path = cache / "repo_manifest.json"
    manifest.save(manifest_path)
    return manifest_path, manifest


def test_graph_free_module_import_and_candidate_wrapper_do_not_import_graph() -> None:
    script = """
import sys
from codenib.scip_interface.scip_query import FactQueryCandidate

candidate = FactQueryCandidate(index=object(), receipt={}, timings={})
assert candidate.index is not None
for name in (
    'igraph',
    'codenib.graph.code_graph',
    'codenib.scip_interface.scip_indexer_base',
    'codenib.ls_router',
):
    assert name not in sys.modules, name
"""
    subprocess.run([sys.executable, "-c", script], check=True, text=True)


def test_candidate_rejects_language_without_active_scip_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path, _manifest = _write_candidate_manifest(tmp_path)
    monkeypatch.setattr(
        "codenib.scip_interface.scip_query.graph_indexer_path",
        lambda _language: "codenib.ls_index.lsp_indexer:GenericLSPIndexer",
    )

    with pytest.raises(FactQueryCandidateRejected, match="active SCIP route"):
        load_fact_query_candidate(manifest_path, core_module=_MockCore())


def test_clean_process_loads_real_candidate_without_importing_graph(
    tmp_path: Path,
) -> None:
    pytest.importorskip("codenib_core")
    manifest_path, _ = _write_candidate_manifest(tmp_path)
    script = """
import sys
from codenib.scip_interface.scip_query import load_fact_query_candidate

candidate = load_fact_query_candidate(sys.argv[1])
assert candidate.fact_query_index is True
for name in (
    'igraph',
    'codenib.graph.code_graph',
    'codenib.scip_interface.scip_indexer_base',
    'codenib.ls_router',
):
    assert name not in sys.modules, name
"""
    subprocess.run(
        [sys.executable, "-c", script, str(manifest_path)],
        check=True,
        text=True,
    )


def test_real_candidate_admits_reference_only_surface_and_preserves_errors(
    tmp_path: Path,
) -> None:
    codenib_core = pytest.importorskip("codenib_core")
    from codenib.agent.lsp_graph import lsp_definition, lsp_references
    from codenib.graph.code_graph import CodeGraph

    manifest_path, manifest = _write_candidate_manifest(tmp_path)
    candidate = load_fact_query_candidate(
        manifest_path,
        core_module=codenib_core,
    )
    graph = CodeGraph.load_graph(
        str(Path(manifest.indexes["symbol_graph"].path) / "graph.pkl")
    )
    node_name = "requests/models.py:Response"
    query_symbol = "requests.models:Response"
    info = graph.get_node_info_by_name(node_name)

    assert info is not None
    assert info["has_definition"] is False
    assert candidate.receipt["filter_identity"]["reference_only_count"] == 1
    assert _outcome(
        lambda: lsp_definition(graph, symbol=query_symbol, top_k=10)
    ) == _outcome(
        lambda: lsp_definition(candidate.index, symbol=query_symbol, top_k=10)
    )
    for include_declaration in (False, True):
        assert _outcome(
            lambda include_declaration=include_declaration: lsp_references(
                graph,
                symbol=query_symbol,
                include_declaration=include_declaration,
                top_k=10,
            )
        ) == _outcome(
            lambda include_declaration=include_declaration: lsp_references(
                candidate.index,
                symbol=query_symbol,
                include_declaration=include_declaration,
                top_k=10,
            )
        )


def test_filter_helpers_preserve_route_semantics_and_add_repository_policy() -> None:
    assert normalize_scip_path(r"src\pkg\demo.py") == "src/pkg/demo.py"
    assert matches_exclude_patterns("vendor/pkg/a.py", ["vendor/**"])
    assert scip_path_is_allowed(
        "src/pkg/demo.py",
        target_dir="src/pkg",
        exclude_patterns=["**/tests/**"],
    )
    assert not scip_path_is_allowed(
        "src/tests/test_demo.py",
        target_dir="src",
        exclude_patterns=["**/tests/**"],
    )
    assert not scip_path_is_allowed(
        ".codenib-private/demo.py",
        enforce_repository_policy=True,
    )


def test_query_surface_digest_matches_cpp_known_vector() -> None:
    from codenib.scip_interface.query_surface import query_surface_sha256

    class Vertex:
        __slots__ = ("attrs",)

        def __init__(self, **attrs: Any) -> None:
            self.attrs = attrs

        def attributes(self) -> dict[str, Any]:
            return self.attrs

    class Edge(Vertex):
        __slots__ = ("source", "target")

        def __init__(self, source: int, target: int, **attrs: Any) -> None:
            super().__init__(**attrs)
            self.source = source
            self.target = target

    class Storage:
        __slots__ = ("vs", "es")

        def __init__(self, vertices: list[Vertex], edges: list[Edge]) -> None:
            self.vs = vertices
            self.es = edges

    class Graph:
        __slots__ = ("graph",)

        def __init__(self, vertices: list[Vertex], edges: list[Edge]) -> None:
            self.graph = Storage(vertices, edges)

    vertices = [
        Vertex(name=".", type="root"),
        Vertex(name="src", type="directory", unified_name="src"),
        Vertex(name="src/main.py", type="file", unified_name="src/main.py"),
        Vertex(
            name="canonical-caller",
            type="function",
            file="src/main.py",
            start_line=8,
            end_line=10,
            selection_line=8,
            unified_name="src/main.py:caller()",
            has_definition=True,
        ),
        Vertex(
            name="canonical-target",
            type="function",
            file="src/main.py",
            start_line=2,
            end_line=4,
            selection_line=2,
            unified_name="src/main.py:target()",
            has_definition=True,
        ),
    ]
    edges = [
        Edge(0, 1, type="contain"),
        Edge(1, 2, type="contain"),
        Edge(2, 3, type="contain"),
        Edge(
            3,
            4,
            type="reference",
            anchor_file="src/main.py",
            anchor_line=9,
        ),
    ]
    graph = Graph(vertices, edges)

    assert query_surface_sha256(graph) == (
        "0112871fe1aafef3930d910a15c12b7632f73803fd55f5620f219c9dd87d64df"
    )
    vertices[-1].attrs["selection_line"] = 3
    assert query_surface_sha256(graph) != (
        "0112871fe1aafef3930d910a15c12b7632f73803fd55f5620f219c9dd87d64df"
    )


def test_scip_process_records_only_a_stable_consumed_decoded_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codenib.scip_interface.scip_indexer_base import SCIPIndexerBase

    class GraphStorage:
        vs: list[Any] = []
        es: list[Any] = []

        @staticmethod
        def vcount() -> int:
            return 1

        @staticmethod
        def ecount() -> int:
            return 0

    class Graph:
        graph = GraphStorage()

        @staticmethod
        def build_range_indexes() -> None:
            return None

        @staticmethod
        def save_graph(path: str) -> None:
            Path(path).write_bytes(b"stable graph output\n")

    class Decoder:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        @staticmethod
        def decode() -> Graph:
            return Graph()

    class Indexer(SCIPIndexerBase):
        def _check_indexer_available(self) -> bool:
            return True

        def _build_index_command(self, **kwargs: Any) -> list[str]:
            return []

        def _get_decoder_class(self):
            return Decoder

    root = tmp_path / "repo"
    root.mkdir()
    indexer = Indexer(root, output_dir=tmp_path / "out", language="python")
    indexer.decoded_file.write_text(
        'documents { relative_path: "src/main.py" }\n', encoding="utf-8"
    )
    expected = regular_file_fingerprint(indexer.decoded_file)

    import codenib.compiler.artifact_fingerprints as fingerprint_module
    import codenib.scip_interface.query_surface as query_surface_module

    def unexpected_capture(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("default SCIP processing must not capture artifact receipts")

    with monkeypatch.context() as scoped:
        scoped.setattr(
            fingerprint_module, "regular_file_fingerprint", unexpected_capture
        )
        scoped.setattr(query_surface_module, "query_surface_sha256", unexpected_capture)
        assert indexer.process_index() is not None
    assert indexer.decoded_input_receipt is None
    assert indexer.graph_output_receipt is None
    indexer._capture_artifact_receipts = True
    graph = indexer.process_index(str(indexer.graph_file))

    assert graph is not None
    assert indexer.decoded_input_receipt == {
        "path": str(indexer.decoded_file.resolve()),
        **expected,
    }
    assert indexer.graph_output_receipt == {
        "path": str(indexer.graph_file.resolve()),
        **regular_file_fingerprint(indexer.graph_file),
        "query_surface_sha256": indexer.graph_output_receipt["query_surface_sha256"],
    }
    indexer.decoded_file.unlink()
    assert indexer.process_index() is None
    assert indexer.decoded_input_receipt is None
    assert indexer.graph_output_receipt is None
    indexer.decoded_file.write_text(
        'documents { relative_path: "src/main.py" }\n', encoding="utf-8"
    )

    class MutatingDecoder:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        @staticmethod
        def decode() -> Graph:
            indexer.decoded_file.write_text(
                'documents { relative_path: "src/replaced.py" }\n',
                encoding="utf-8",
            )
            return Graph()

    monkeypatch.setattr(indexer, "_get_decoder_class", lambda: MutatingDecoder)
    assert indexer.process_index() is None
    assert indexer.decoded_input_receipt is None


def test_rust_full_generation_reports_complete_and_discards_failed_stale_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codenib.scip_interface.scip_indexer_rust import SCIPRustIndexer
    from codenib.scip_interface.scip_pb2 import Index

    manifest_path, manifest = _write_candidate_manifest(tmp_path, language="rust")
    root = Path(manifest.repo_path)
    output = Path(manifest.indexes["symbol_graph"].path)
    indexer = SCIPRustIndexer(root, output_dir=output)
    indexer.index_file.write_bytes(b"stale raw")
    indexer.decoded_file.write_text("stale decoded", encoding="utf-8")
    generated = Index()
    generated.documents.add(relative_path="src/lib.rs")

    monkeypatch.setattr(indexer, "_check_indexer_available", lambda: True)

    def successful_run(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess:
        indexer.index_file.write_bytes(generated.SerializeToString())
        return subprocess.CompletedProcess([], 0, "", "")

    with monkeypatch.context() as scoped:
        scoped.setattr(subprocess, "run", successful_run)
        assert indexer.generate_index() is True
    assert not indexer.decoded_file.exists()
    assert indexer.index_generation_report == {
        "backend": "rust-analyzer-scip",
        "status": "complete",
        "complete": True,
        "partial": False,
        "document_count": 1,
    }

    indexer.decoded_file.write_text("stale decoded", encoding="utf-8")

    def failed_run(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess:
        raise subprocess.CalledProcessError(1, ["rust-analyzer", "scip"])

    with monkeypatch.context() as scoped:
        scoped.setattr(subprocess, "run", failed_run)
        assert indexer.run_pipeline(report_profile=False) is None
    assert not indexer.index_file.exists()
    assert not indexer.decoded_file.exists()
    assert indexer.decoded_input_receipt is None
    assert indexer.index_generation_report == {
        "backend": "rust-analyzer-scip",
        "status": "failed",
        "complete": False,
        "partial": False,
        "document_count": 0,
    }
    with pytest.raises(FactQueryCandidateRejected, match="artifact"):
        load_fact_query_candidate(manifest_path, core_module=_MockCore())


def test_manifest_candidate_binds_source_filter_index_and_native_receipts(
    tmp_path: Path,
) -> None:
    manifest_path, _ = _write_candidate_manifest(tmp_path)
    core = _MockCore()

    candidate = load_fact_query_candidate(manifest_path, core_module=core)

    assert candidate.has_symbol("demo") is True
    assert candidate.receipt["language"] == "python"
    assert candidate.receipt["manifest"]["version"] == MANIFEST_VERSION
    manifest_fingerprint = regular_file_fingerprint(manifest_path)
    assert candidate.receipt["manifest"]["size_bytes"] == manifest_fingerprint["size"]
    assert candidate.receipt["manifest"]["sha256"] == manifest_fingerprint["sha256"]
    assert candidate.receipt["manifest"]["builder_schema"] == 6
    assert candidate.receipt["manifest"]["document_count"] == 1
    assert candidate.receipt["graph"]["relative_path"] == "graph.pkl"
    assert (
        candidate.receipt["filters"]["repository_filter_policy"]
        == REPOSITORY_FILTER_POLICY_VERSION
    )
    expected_selection = RepositorySourceSelection()
    assert candidate.receipt["manifest"]["source_selection"] == (
        expected_selection.to_dict()
    )
    assert candidate.receipt["source"]["source_selection_digest"] == (
        expected_selection.digest
    )
    assert candidate.receipt["filters"]["source_selection_digest"] == (
        expected_selection.digest
    )
    assert candidate.receipt["filter_identity"]["identity"] is True
    assert candidate.receipt["filter_identity"]["reference_only_count"] == 1
    assert (
        core.index.expected_query_surface_sha256
        == candidate.receipt["graph"]["query_surface_sha256"]
    )
    assert len(candidate.receipt["receipt_sha256"]) == 64
    assert core.calls[0]["language"] == "python"


def test_manifest_candidate_preserves_legacy_v11_source_policy(tmp_path: Path) -> None:
    manifest_path, _ = _write_candidate_manifest(
        tmp_path,
        manifest_version=LEGACY_MANIFEST_VERSION,
    )

    candidate = load_fact_query_candidate(manifest_path, core_module=_MockCore())

    assert candidate.receipt["manifest"]["version"] == LEGACY_MANIFEST_VERSION
    assert candidate.receipt["manifest"]["source_selection"] is None
    assert candidate.receipt["source"]["source_selection_digest"] is None
    assert candidate.receipt["filters"]["repository_filter_policy"] == 3
    assert candidate.receipt["filter_identity"]["source_selection_digest"] is None


def test_manifest_candidate_excludes_selected_subtrees_from_native_proof(
    tmp_path: Path,
) -> None:
    selection = RepositorySourceSelection(("generated",))
    manifest_path, manifest = _write_candidate_manifest(
        tmp_path,
        source_selection=selection,
    )
    generated = Path(manifest.repo_path) / "generated" / "ignored.py"
    generated.parent.mkdir()
    generated.write_text("SHOULD_NOT_BE_READ = True\n", encoding="utf-8")
    core = _MockCore()

    candidate = load_fact_query_candidate(manifest_path, core_module=core)

    assert "generated/ignored.py" not in core.index.allowed_files
    assert candidate.receipt["source"]["source_selection"] == selection.to_dict()
    assert candidate.receipt["filter_identity"]["source_selection_digest"] == (
        selection.digest
    )


def test_manifest_change_during_native_decode_rejects_mixed_generation(
    tmp_path: Path,
) -> None:
    manifest_path, _ = _write_candidate_manifest(tmp_path)

    class MutatingCore(_MockCore):
        def decode_scip_fact_query_index(self, **kwargs: Any) -> dict[str, Any]:
            payload = super().decode_scip_fact_query_index(**kwargs)
            manifest_path.write_text("{}", encoding="utf-8")
            return payload

    with pytest.raises(FactQueryCandidateRejected, match="changed"):
        load_fact_query_candidate(manifest_path, core_module=MutatingCore())


def test_graph_change_during_native_decode_rejects_mixed_generation(
    tmp_path: Path,
) -> None:
    manifest_path, manifest = _write_candidate_manifest(tmp_path)
    graph_file = Path(manifest.indexes["symbol_graph"].path) / "graph.pkl"

    class MutatingCore(_MockCore):
        def decode_scip_fact_query_index(self, **kwargs: Any) -> dict[str, Any]:
            payload = super().decode_scip_fact_query_index(**kwargs)
            graph_file.write_bytes(b"replacement-graph\n")
            return payload

    with pytest.raises(FactQueryCandidateRejected, match="graph artifact changed"):
        load_fact_query_candidate(manifest_path, core_module=MutatingCore())


def test_candidate_ignores_non_regular_repository_entries(tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO creation is unavailable")
    manifest_path, manifest = _write_candidate_manifest(tmp_path)
    fifo = Path(manifest.repo_path) / "events.pipe"
    os.mkfifo(fifo)
    index = _MockIndex()

    load_fact_query_candidate(manifest_path, core_module=_MockCore(index=index))

    assert "events.pipe" not in index.allowed_files


@pytest.mark.parametrize(("field", "value"), [("node_count", 6), ("edge_count", 5)])
def test_candidate_binds_manifest_graph_counts(
    tmp_path: Path, field: str, value: int
) -> None:
    manifest_path, manifest = _write_candidate_manifest(tmp_path)
    entry = manifest.indexes["symbol_graph"]
    entry.config[field] = value
    entry.metadata[field] = value
    manifest.save(manifest_path)

    with pytest.raises(FactQueryCandidateRejected, match="manifest"):
        load_fact_query_candidate(manifest_path, core_module=_MockCore())


def test_candidate_rejects_same_count_query_surface_mismatch(tmp_path: Path) -> None:
    manifest_path, manifest = _write_candidate_manifest(tmp_path)
    entry = manifest.indexes["symbol_graph"]
    entry.config["query_surface_sha256"] = "d" * 64
    entry.metadata["query_surface_sha256"] = "d" * 64
    manifest.save(manifest_path)

    with pytest.raises(FactQueryCandidateRejected, match="proof rejected candidate"):
        load_fact_query_candidate(manifest_path, core_module=_MockCore())


def test_candidate_rejects_unknown_builder_field_and_report_shape(
    tmp_path: Path,
) -> None:
    manifest_path, manifest = _write_candidate_manifest(tmp_path)
    entry = manifest.indexes["symbol_graph"]
    entry.config["unknown"] = "unsafe"
    entry.metadata["unknown"] = "unsafe"
    manifest.save(manifest_path)
    with pytest.raises(FactQueryCandidateRejected, match="builder schema"):
        load_fact_query_candidate(manifest_path, core_module=_MockCore())

    manifest_path, manifest = _write_candidate_manifest(tmp_path / "report")
    report = manifest.indexes["symbol_graph"].config["index_generation_reports"][
        "python"
    ]
    report["extra"] = True
    manifest.indexes["symbol_graph"].metadata["index_generation_reports"] = (
        copy.deepcopy(
            manifest.indexes["symbol_graph"].config["index_generation_reports"]
        )
    )
    manifest.save(manifest_path)
    with pytest.raises(FactQueryCandidateRejected, match="report fields"):
        load_fact_query_candidate(manifest_path, core_module=_MockCore())


def test_candidate_rejects_empty_or_mismatched_generation_report(
    tmp_path: Path,
) -> None:
    manifest_path, manifest = _write_candidate_manifest(tmp_path)
    reports = manifest.indexes["symbol_graph"].config["index_generation_reports"]
    reports["python"]["document_count"] = 0
    manifest.indexes["symbol_graph"].metadata["index_generation_reports"] = (
        copy.deepcopy(reports)
    )
    manifest.save(manifest_path)
    with pytest.raises(FactQueryCandidateRejected, match="no source documents"):
        load_fact_query_candidate(manifest_path, core_module=_MockCore())

    manifest_path, _ = _write_candidate_manifest(tmp_path / "mismatch")

    class WrongDocumentCount(_MockCore):
        def decode_scip_fact_query_index(self, **kwargs: Any) -> dict[str, Any]:
            payload = super().decode_scip_fact_query_index(**kwargs)
            payload["decode_profile_ns"]["document_count"] = 2
            return payload

    with pytest.raises(FactQueryCandidateRejected, match="document count disagrees"):
        load_fact_query_candidate(manifest_path, core_module=WrongDocumentCount())


@pytest.mark.parametrize("field", ["built_at_epoch", "build_duration_seconds"])
def test_candidate_rejects_nonfinite_manifest_times(tmp_path: Path, field: str) -> None:
    manifest_path, manifest = _write_candidate_manifest(tmp_path)
    entry = manifest.indexes["symbol_graph"]
    if field == "built_at_epoch":
        entry.built_at_epoch = float("inf")
    else:
        entry.metadata[field] = float("nan")
    manifest_path.write_text(
        json.dumps(manifest.to_dict(), allow_nan=True),
        encoding="utf-8",
    )

    with pytest.raises(FactQueryCandidateRejected, match="malformed"):
        load_fact_query_candidate(manifest_path, core_module=_MockCore())


def test_candidate_binds_live_git_head_before_decode(tmp_path: Path) -> None:
    manifest_path, manifest = _write_candidate_manifest(tmp_path)
    root = Path(manifest.repo_path)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "config", "user.name", "CodeNib Test"],
        check=True,
    )
    subprocess.run(["git", "-C", str(root), "add", "src/main.py"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-q", "-m", "initial"], check=True
    )
    head = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    manifest.commit = head
    manifest.indexes["symbol_graph"].commit = head
    manifest.save(manifest_path)
    load_fact_query_candidate(manifest_path, core_module=_MockCore())

    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw["indexes"]["symbol_graph"]["commit"] = ""
    manifest_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(FactQueryCandidateRejected, match="commit provenance"):
        load_fact_query_candidate(manifest_path, core_module=_MockCore())
    manifest.save(manifest_path)

    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "commit",
            "--allow-empty",
            "-q",
            "-m",
            "move head",
        ],
        check=True,
    )
    with pytest.raises(FactQueryCandidateRejected, match="live repository commit"):
        load_fact_query_candidate(manifest_path, core_module=_MockCore())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("builder_schema", 3),
        ("target_dir", "src"),
        ("update_mode", "incremental"),
        ("partial", True),
        ("partial_index", True),
        ("source_coverage_fallback", True),
        ("lsp_occurrence_artifact", {}),
    ],
)
def test_manifest_candidate_rejects_unsafe_builder_states(
    tmp_path: Path, field: str, value: Any
) -> None:
    manifest_path, manifest = _write_candidate_manifest(tmp_path)
    entry = manifest.indexes["symbol_graph"]
    entry.config[field] = value
    entry.metadata[field] = value
    manifest.save(manifest_path)

    with pytest.raises(FactQueryCandidateRejected):
        load_fact_query_candidate(manifest_path, core_module=_MockCore())


def test_manifest_candidate_rejects_source_or_index_mutation(tmp_path: Path) -> None:
    manifest_path, manifest = _write_candidate_manifest(tmp_path)
    source = Path(manifest.repo_path) / "src" / "main.py"
    source.write_text("def changed(): pass\n", encoding="utf-8")
    with pytest.raises(FactQueryCandidateRejected, match="source"):
        load_fact_query_candidate(manifest_path, core_module=_MockCore())

    manifest_path, manifest = _write_candidate_manifest(tmp_path / "other")
    decoded = Path(manifest.indexes["symbol_graph"].path) / "index.decoded"
    decoded.write_text("mutated\n", encoding="utf-8")
    with pytest.raises(FactQueryCandidateRejected, match="artifact"):
        load_fact_query_candidate(manifest_path, core_module=_MockCore())

    manifest_path, manifest = _write_candidate_manifest(tmp_path / "graph")
    graph_file = Path(manifest.indexes["symbol_graph"].path) / "graph.pkl"
    graph_file.write_bytes(b"tampered-graph!\n")
    with pytest.raises(FactQueryCandidateRejected, match="graph artifact"):
        load_fact_query_candidate(manifest_path, core_module=_MockCore())


def test_manifest_candidate_rejects_malformed_or_false_native_proof(
    tmp_path: Path,
) -> None:
    manifest_path, _ = _write_candidate_manifest(tmp_path)
    core = _MockCore(index=_MockIndex(proof_updates={"identity": False}))
    with pytest.raises(FactQueryCandidateRejected, match="identity"):
        load_fact_query_candidate(manifest_path, core_module=core)

    core = _MockCore(index=_MockIndex(proof_updates={"unexpected": 1}))
    with pytest.raises(FactQueryCandidateRejected, match="keys mismatch"):
        load_fact_query_candidate(manifest_path, core_module=core)

    core = _MockCore(index=_MockIndex(proof_updates={"directory_count": 2}))
    with pytest.raises(FactQueryCandidateRejected, match="record class"):
        load_fact_query_candidate(manifest_path, core_module=core)

    occurrence_index = _MockIndex()
    occurrence_index.occurrence_count = 1
    core = _MockCore(index=occurrence_index)
    with pytest.raises(
        FactQueryCandidateRejected, match="occurrence count must be zero"
    ):
        load_fact_query_candidate(manifest_path, core_module=core)

    manifest_path, _ = _write_candidate_manifest(
        tmp_path / "no-definitions", node_count=3, edge_count=2
    )
    empty_index = _MockIndex(proof_updates={"definition_count": 0})
    empty_index.record_count = 3
    empty_index.edge_count = 2
    empty_index.symbol_count = 0
    with pytest.raises(FactQueryCandidateRejected, match="no queryable"):
        load_fact_query_candidate(
            manifest_path, core_module=_MockCore(index=empty_index)
        )


def test_manifest_candidate_rejects_malformed_receipt_and_propagates_memory(
    tmp_path: Path,
) -> None:
    manifest_path, _ = _write_candidate_manifest(tmp_path)
    core = _MockCore(receipt_updates={"unexpected": True})
    with pytest.raises(FactQueryCandidateRejected, match="keys mismatch"):
        load_fact_query_candidate(manifest_path, core_module=core)

    with pytest.raises(MemoryError, match="allocation"):
        load_fact_query_candidate(
            manifest_path,
            core_module=_MockCore(failure=MemoryError("allocation exhausted")),
        )


def test_rust_candidate_recomputes_serial_cargo_identity(tmp_path: Path) -> None:
    manifest_path, _ = _write_candidate_manifest(tmp_path, language="rust")
    candidate = load_fact_query_candidate(manifest_path, core_module=_MockCore())
    assert candidate.receipt["native_input"]["metadata"] == {
        "kind": "rust-cargo",
        "complete": True,
        "inputs": [
            {
                "path": "Cargo.toml",
                "size_bytes": (tmp_path / "repo" / "Cargo.toml").stat().st_size,
                "sha256": regular_file_fingerprint(tmp_path / "repo" / "Cargo.toml")[
                    "sha256"
                ],
            }
        ],
        "internal_crates": ["demo"],
    }

    class WrongCrates(_MockCore):
        def decode_scip_fact_query_index(self, **kwargs: Any) -> dict[str, Any]:
            payload = super().decode_scip_fact_query_index(**kwargs)
            payload["input_receipt"]["metadata"]["internal_crates"] = ["wrong"]
            return payload

    with pytest.raises(FactQueryCandidateRejected, match="serial Rust"):
        load_fact_query_candidate(manifest_path, core_module=WrongCrates())


def test_rust_workspace_receipt_matches_all_serial_cargo_inputs(tmp_path: Path) -> None:
    manifest_path, _ = _write_candidate_manifest(
        tmp_path, language="rust", rust_workspace=True
    )
    candidate = load_fact_query_candidate(manifest_path, core_module=_MockCore())
    metadata = candidate.receipt["native_input"]["metadata"]
    assert [item["path"] for item in metadata["inputs"]] == [
        "Cargo.toml",
        "crates/a/Cargo.toml",
        "crates/b/Cargo.toml",
    ]
    assert metadata["internal_crates"] == ["alpha", "beta", "demo"]

    class WrongCargoDigest(_MockCore):
        def decode_scip_fact_query_index(self, **kwargs: Any) -> dict[str, Any]:
            payload = super().decode_scip_fact_query_index(**kwargs)
            payload["input_receipt"]["metadata"]["inputs"][1]["sha256"] = "0" * 64
            return payload

    with pytest.raises(FactQueryCandidateRejected, match="changed after native"):
        load_fact_query_candidate(manifest_path, core_module=WrongCargoDigest())


def test_rust_cargo_input_outside_source_surface_rejects_candidate(
    tmp_path: Path,
) -> None:
    manifest_path, manifest = _write_candidate_manifest(tmp_path, language="rust")
    root = Path(manifest.repo_path)
    (root / "Cargo.toml").write_text(
        '[package]\nname = "demo"\nversion = "0.1.0"\n'
        '[workspace]\nmembers = ["vendor/*"]\n',
        encoding="utf-8",
    )
    vendor = root / "vendor" / "hidden"
    vendor.mkdir(parents=True)
    vendor_cargo = vendor / "Cargo.toml"
    vendor_cargo.write_text(
        '[package]\nname = "hidden"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    source = fingerprint_repository(root, exclude_roots=(manifest_path.parent,))
    manifest.source_fingerprint = source.value
    manifest.last_indexed_source_fingerprint = source.value
    manifest.file_count = source.file_count
    manifest.indexes["symbol_graph"].source_fingerprint = source.value
    manifest.save(manifest_path)

    class VendorCargoCore(_MockCore):
        def decode_scip_fact_query_index(self, **kwargs: Any) -> dict[str, Any]:
            payload = super().decode_scip_fact_query_index(**kwargs)
            fingerprint = regular_file_fingerprint(vendor_cargo)
            metadata = payload["input_receipt"]["metadata"]
            metadata["inputs"].append(
                {
                    "path": "vendor/hidden/Cargo.toml",
                    "size_bytes": fingerprint["size"],
                    "sha256": fingerprint["sha256"],
                }
            )
            metadata["internal_crates"] = ["demo", "hidden"]
            return payload

    with pytest.raises(FactQueryCandidateRejected, match="source surface"):
        load_fact_query_candidate(manifest_path, core_module=VendorCargoCore())


def test_candidate_rejects_symlinks_old_manifest_and_missing_build_keys(
    tmp_path: Path,
) -> None:
    manifest_path, manifest = _write_candidate_manifest(tmp_path)
    manifest_link = manifest_path.with_name("manifest-link.json")
    manifest_link.symlink_to(manifest_path)
    with pytest.raises(FactQueryCandidateRejected, match="non-symlink"):
        load_fact_query_candidate(manifest_link, core_module=_MockCore())

    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw.pop("version")
    manifest_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(FactQueryCandidateRejected, match="explicitly"):
        load_fact_query_candidate(manifest_path, core_module=_MockCore())

    manifest_path, _ = _write_candidate_manifest(tmp_path / "missing-entry-commit")
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    del raw["indexes"]["symbol_graph"]["commit"]
    manifest_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(FactQueryCandidateRejected, match="entry keys mismatch"):
        load_fact_query_candidate(manifest_path, core_module=_MockCore())

    manifest_path, manifest = _write_candidate_manifest(tmp_path / "missing-key")
    entry = manifest.indexes["symbol_graph"]
    del entry.config["target_dir"]
    del entry.metadata["target_dir"]
    manifest.save(manifest_path)
    with pytest.raises(FactQueryCandidateRejected, match="builder schema"):
        load_fact_query_candidate(manifest_path, core_module=_MockCore())

    manifest_path, manifest = _write_candidate_manifest(tmp_path / "artifact-link")
    artifact = Path(manifest.indexes["symbol_graph"].path) / "index.decoded"
    target = artifact.with_name("actual.decoded")
    artifact.rename(target)
    artifact.symlink_to(target)
    with pytest.raises(FactQueryCandidateRejected, match="non-symlink"):
        load_fact_query_candidate(manifest_path, core_module=_MockCore())


@pytest.mark.parametrize(
    ("language", "decoded_text", "source_suffix"),
    [
        ("python", _PYTHON_PARITY_INDEX, ".py"),
        ("rust", _RUST_PARITY_INDEX, ".rs"),
    ],
)
def test_native_python_and_rust_query_results_match_filtered_legacy_graph(
    tmp_path: Path,
    language: str,
    decoded_text: str,
    source_suffix: str,
) -> None:
    codenib_core = pytest.importorskip("codenib_core")
    from codenib.agent.lsp_graph import lsp_definition, lsp_references
    from codenib.scip_interface.query_surface import query_surface_sha256
    from codenib.scip_interface.scip_decode_python import SCIPPythonGraphDecoder
    from codenib.scip_interface.scip_decode_rust import SCIPRustGraphDecoder
    from codenib.scip_interface.scip_indexer_base import SCIPIndexerBase
    from codenib.scip_interface.scip_query import decode_native_fact_query_index

    class FilterOnlyIndexer(SCIPIndexerBase):
        def _check_indexer_available(self) -> bool:
            return True

        def _build_index_command(self, **kwargs: Any) -> list[str]:
            return []

        def _get_decoder_class(self):
            raise NotImplementedError

    root = tmp_path / language
    (root / "src").mkdir(parents=True)
    for stem in ("alpha", "beta"):
        (root / "src" / f"{stem}{source_suffix}").write_text(
            "fn run() {}\n" if language == "rust" else "def run(): pass\n",
            encoding="utf-8",
        )
    if language == "rust":
        (root / "Cargo.toml").write_text(
            '[package]\nname = "demo"\nversion = "0.1.0"\n', encoding="utf-8"
        )
    index_path = root / ".codenib_cache" / "index.decoded"
    index_path.parent.mkdir()
    index_path.write_text(decoded_text, encoding="utf-8")

    decoder_class = (
        SCIPPythonGraphDecoder if language == "python" else SCIPRustGraphDecoder
    )
    graph = decoder_class(str(index_path), project_root=str(root)).decode()
    filter_indexer = FilterOnlyIndexer(
        root,
        output_dir=root / ".filter",
        exclude_patterns=sorted(default_exclude_patterns()),
        language=language,
    )
    graph = filter_indexer._filter_project_graph(graph)

    native = decode_native_fact_query_index(
        index_file=index_path,
        project_root=root,
        language=language,
        core_module=codenib_core,
    )
    allowed = sorted(
        [f"src/alpha{source_suffix}", f"src/beta{source_suffix}"]
        + (["Cargo.toml"] if language == "rust" else []),
        key=lambda item: item.encode("utf-8"),
    )
    serial_query_digest = query_surface_sha256(graph)
    proof = native.index.prove_filter_identity(allowed, serial_query_digest)
    assert proof["identity"] is True
    assert proof["allowed_files_sha256"] == _allowed_digest(allowed)
    assert proof["query_surface_sha256"] == serial_query_digest
    assert set(native.input_receipt) == {"schema_version", "index", "metadata"}
    assert native.input_receipt["index"] == {
        "path": str(index_path.resolve()),
        "size_bytes": index_path.stat().st_size,
        "sha256": regular_file_fingerprint(index_path)["sha256"],
    }
    assert set(native.input_receipt["metadata"]) == {
        "kind",
        "complete",
        "inputs",
        "internal_crates",
    }
    with pytest.raises(TypeError):
        native.index.prove_filter_identity(allowed)
    with pytest.raises(ValueError):
        native.index.prove_filter_identity(list(reversed(allowed)), serial_query_digest)
    with pytest.raises(ValueError):
        native.index.prove_filter_identity([*allowed, allowed[-1]], serial_query_digest)
    with pytest.raises(ValueError):
        native.index.prove_filter_identity(allowed, "0" * 64)

    names = [
        name
        for name in graph.name_to_vertex
        if (graph.get_node_info_by_name(name) or {}).get("type")
        in {"symbol", "class", "function", "method", "field"}
    ]
    reference_only_names = [
        name
        for name in names
        if (graph.get_node_info_by_name(name) or {}).get("has_definition") is False
    ]
    assert proof["reference_only_count"] == len(reference_only_names)
    if language == "python":
        assert reference_only_names == ["requests/models.py:Response"]
    else:
        assert reference_only_names == ["demo/missing/External"]
    seeds = list(names)
    for name in names:
        unified = str((graph.get_node_info_by_name(name) or {}).get("unified_name"))
        bare = unified.split(":")[-1].split("/")[-1].split(".")[-1]
        bare = bare.rstrip("()")
        seeds.extend((unified, bare, f"`{bare}`"))
    seeds.append("definitely-missing-symbol")

    for seed in dict.fromkeys(seeds):
        for top_k in (1, 2, 10_000):
            assert _outcome(
                lambda seed=seed, top_k=top_k: lsp_definition(
                    graph, symbol=seed, top_k=top_k
                )
            ) == _outcome(
                lambda seed=seed, top_k=top_k: lsp_definition(
                    native.index, symbol=seed, top_k=top_k
                )
            ), (
                language,
                seed,
                top_k,
            )
            for include_declaration in (False, True):
                assert _outcome(
                    partial(
                        lsp_references,
                        graph,
                        symbol=seed,
                        include_declaration=include_declaration,
                        top_k=top_k,
                    )
                ) == _outcome(
                    partial(
                        lsp_references,
                        native.index,
                        symbol=seed,
                        include_declaration=include_declaration,
                        top_k=top_k,
                    )
                ), (
                    language,
                    seed,
                    top_k,
                    include_declaration,
                )
