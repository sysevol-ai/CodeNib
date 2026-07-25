#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Unified language-server router for indexing and decoding across all languages.

Routes to language-specific implementations:
    - 'cpp', 'c++', 'c':  clangd             (codenib.ls_index)
    - 'rust', 'rs':       rust-analyzer       (codenib.scip_interface)
    - 'ts', 'typescript', 'js', 'javascript': (codenib.scip_interface)
    - 'python', 'py':     scip-python         (codenib.scip_interface)

C/C++ uses clangd .idx format (ls_index package).
All other languages use the SCIP protocol (scip_interface package).

Example:
    indexer = LSIndexer(project_root="/path/to/project", language="cpp")
    graph = indexer.run_pipeline()

    decoder = LSGraphDecoder(index_file_path="/path/to/index.decoded", language="rust")
    graph = decoder.decode()
"""
from importlib import import_module
from pathlib import Path
from typing import List, Optional, Sequence, Type, Union

from .graph.code_graph import CodeGraph
from .languages import (
    graph_cold_start_backend,
    graph_decoder_path,
    graph_indexer_path,
    graph_language_aliases,
    lsp_command_for_language,
    normalize_graph_language,
    normalize_language,
    scip_candidate_indexer_path,
    scip_candidate_indexer_paths,
)
from .log_utils import get_logger
from .profiler import Profiler

logger = get_logger("ls_router")

LANGUAGE_ALIASES = graph_language_aliases()
ACTIVE_GRAPH_ROUTE = "active"
SCIP_CANDIDATE_GRAPH_ROUTE = "scip-candidate"
LSP_GRAPH_ROUTE = "lsp"
LSP_GRAPH_INDEXER_PATH = "codenib.ls_index.lsp_indexer:GenericLSPIndexer"


def _normalize_language(
    language: Optional[str],
    *,
    graph_route: str = ACTIVE_GRAPH_ROUTE,
) -> str:
    """Normalize language string for the requested graph route."""
    if language is None:
        return "python"
    if graph_route == SCIP_CANDIDATE_GRAPH_ROUTE:
        key = normalize_language(language)
        if key is not None and scip_candidate_indexer_path(key) is not None:
            return key
        supported = ", ".join(sorted(set(scip_candidate_indexer_paths().keys())))
        raise ValueError(
            f"Unsupported SCIP candidate language: {language!r}. "
            f"Supported: {supported}"
        )
    if graph_route == LSP_GRAPH_ROUTE:
        key = normalize_graph_language(language)
        if key is not None and lsp_command_for_language(key) is not None:
            return key
        supported = ", ".join(
            sorted(
                alias
                for alias in set(LANGUAGE_ALIASES.keys())
                if lsp_command_for_language(alias) is not None
            )
        )
        raise ValueError(
            f"Unsupported LSP graph language: {language!r}. Supported: {supported}"
        )
    if graph_route != ACTIVE_GRAPH_ROUTE:
        raise ValueError(
            "graph_route must be 'active', 'lsp', or 'scip-candidate', "
            f"got {graph_route!r}"
        )
    key = normalize_graph_language(language)
    if key is None:
        supported = ", ".join(sorted(set(LANGUAGE_ALIASES.keys())))
        raise ValueError(f"Unsupported language: {language!r}. Supported: {supported}")
    return key


def _load_class(path: str) -> Type:
    """Import a class from a ``module:Class`` registry path."""

    module_name, separator, class_name = path.partition(":")
    if not separator or not module_name or not class_name:
        raise ValueError(f"Invalid class path: {path!r}")
    module = import_module(module_name)
    cls = getattr(module, class_name)
    if not isinstance(cls, type):
        raise TypeError(f"Registered object is not a class: {path}")
    return cls


def _normalize_language_sequence(
    languages: Optional[Sequence[str]],
    *,
    graph_route: str = ACTIVE_GRAPH_ROUTE,
) -> List[str]:
    """Normalize a language sequence, preserving order and removing aliases."""
    normalized: List[str] = []
    seen = set()
    for language in languages or ["python"]:
        key = _normalize_language(language, graph_route=graph_route)
        if key in seen:
            continue
        seen.add(key)
        normalized.append(key)
    return normalized


# ── LSIndexer ─────────────────────────────────────────────────────────


class LSIndexer:
    """Unified indexer that delegates to language-specific implementations."""

    def __init__(
        self,
        project_root: Union[str, Path],
        output_dir: Optional[Union[str, Path]] = None,
        exclude_patterns: Optional[List] = None,
        profiler: Optional[Profiler] = None,
        language: Optional[str] = None,
        decoder_backend: Optional[str] = None,
        graph_route: str = ACTIVE_GRAPH_ROUTE,
    ):
        self.project_root = Path(project_root).absolute()
        self.graph_route = graph_route
        self.language = _normalize_language(language, graph_route=graph_route)
        self.decoder_backend = decoder_backend

        self._delegate = self._create_indexer(
            project_root=project_root,
            output_dir=output_dir,
            exclude_patterns=exclude_patterns,
            profiler=profiler,
        )

        # Expose delegate attributes for backward compatibility
        self.output_dir = self._delegate.output_dir
        self.index_file = self._delegate.index_file
        self.decoded_file = self._delegate.decoded_file
        self.graph_file = self._delegate.graph_file
        self.profiler = self._delegate.profiler

        logger.info(
            "Initialized LSIndexer for %s at %s (route=%s)",
            self.language,
            self.project_root,
            self.graph_route,
        )

    def _create_indexer(
        self,
        project_root,
        output_dir,
        exclude_patterns,
        profiler,
    ):
        if self.graph_route == SCIP_CANDIDATE_GRAPH_ROUTE:
            indexer_path = scip_candidate_indexer_path(self.language)
            backend = "scip"
        elif self.graph_route == LSP_GRAPH_ROUTE:
            indexer_path = LSP_GRAPH_INDEXER_PATH
            backend = "lsp"
        else:
            indexer_path = graph_indexer_path(self.language)
            backend = graph_cold_start_backend(self.language)

        if indexer_path is None:
            raise ValueError(
                f"No indexer for language: {self.language} "
                f"(route={self.graph_route})"
            )

        cls = _load_class(indexer_path)
        kwargs = {
            "project_root": project_root,
            "output_dir": output_dir,
            "exclude_patterns": exclude_patterns,
            "profiler": profiler,
        }
        if backend == "scip":
            kwargs["decoder_backend"] = self.decoder_backend
        elif backend == "lsp":
            kwargs["language"] = self.language
        elif self.decoder_backend is not None:
            logger.warning(
                "decoder_backend=%r ignored for %s (%s has no SCIP decoder)",
                self.decoder_backend,
                self.language,
                backend,
            )
        return cls(**kwargs)

    # ── Delegated methods ─────────────────────────────────────────────

    def generate_index(self, **kwargs) -> bool:
        return self._delegate.generate_index(**kwargs)

    def decode_index(self) -> bool:
        return self._delegate.decode_index()

    def process_index(
        self, output_file: Optional[str] = None
    ) -> Union[CodeGraph, None]:
        return self._delegate.process_index(output_file=output_file)

    def run_pipeline(
        self,
        output_file: Optional[str] = None,
        skip_level: Optional[str] = None,
        *,
        reset_profiler: bool = True,
        report_profile: bool = True,
        **kwargs,
    ) -> Union[CodeGraph, None]:
        return self._delegate.run_pipeline(
            output_file=output_file,
            skip_level=skip_level,
            reset_profiler=reset_profiler,
            report_profile=report_profile,
            **kwargs,
        )

    def clear_cache(self, level: str = "all") -> bool:
        return self._delegate.clear_cache(level=level)

    @property
    def index_quality_report(self):
        """Latest backend quality report, when the indexer provides one."""

        return getattr(self._delegate, "index_quality_report", None)

    def graph_patch(
        self,
        graph: "CodeGraph",
        base_commit: str,
        target_commit: str = "HEAD",
    ) -> dict:
        """Incrementally update graph using LSP graph-patching.

        Args:
            graph: Existing CodeGraph to update in place.
            base_commit: Git commit hash the graph was built from.
            target_commit: Git commit hash to patch to (default HEAD).

        Returns:
            Statistics dict from the patcher.
        """
        from .graph.incremental.graph_patcher import LANGUAGE_EXTENSIONS, GraphPatcher

        patcher = GraphPatcher(
            project_root=str(self.project_root),
            code_graph=graph,
            language=self.language,
        )

        changed = patcher.detect_changed_files(
            str(self.project_root),
            base_commit,
            target_commit,
            extensions=LANGUAGE_EXTENSIONS.get(self.language),
        )
        return patcher.patch_files(changed)


def build_graph_for_languages(
    project_root: Union[str, Path],
    output_dir: Union[str, Path],
    *,
    languages: Optional[Sequence[str]] = None,
    project_name: Optional[str] = None,
    skip_level: Optional[str] = "graph",
    target_dir: Optional[str] = None,
    include_references: bool = False,
    exclude_patterns: Optional[List] = None,
    profiler: Optional[Profiler] = None,
    decoder_backend: Optional[str] = None,
    graph_route: str = ACTIVE_GRAPH_ROUTE,
) -> Union[CodeGraph, None]:
    """Build a CodeGraph for one or more graph languages.

    Single-language builds preserve the historical cache layout by delegating
    directly to ``LSIndexer(output_dir=output_dir)``. Multi-language builds use
    ``<output_dir>/graphs/<language>/`` for per-language artifacts, then merge
    the resulting graphs and save the combined graph at
    ``<output_dir>/graph.pkl``. ``graph_route="active"`` preserves the public
    language route; ``graph_route="lsp"`` forces the generic LSP graph route
    for backend comparison; ``graph_route="scip-candidate"`` is an explicit
    opt-in for evaluating candidate SCIP cold-start backends without promoting
    them.
    """
    normalized = _normalize_language_sequence(languages, graph_route=graph_route)
    base_output = Path(output_dir)
    pname = project_name or base_output.name

    indexer_kwargs = {}
    if exclude_patterns is not None:
        indexer_kwargs["exclude_patterns"] = exclude_patterns

    pipeline_kwargs = {}
    if target_dir is not None:
        pipeline_kwargs["target_dir"] = target_dir
    if include_references:
        pipeline_kwargs["include_references"] = True

    if len(normalized) == 1:
        indexer = LSIndexer(
            project_root,
            output_dir=base_output,
            language=normalized[0],
            profiler=profiler,
            decoder_backend=decoder_backend,
            graph_route=graph_route,
            **indexer_kwargs,
        )
        return indexer.run_pipeline(
            project_name=pname,
            skip_level=skip_level,
            **pipeline_kwargs,
        )

    combined = CodeGraph(str(Path(project_root).absolute()))
    for language in normalized:
        language_output = base_output / "graphs" / language
        language_project = f"{pname}-{language}"
        indexer = LSIndexer(
            project_root,
            output_dir=language_output,
            language=language,
            profiler=profiler,
            decoder_backend=decoder_backend,
            graph_route=graph_route,
            **indexer_kwargs,
        )
        graph = indexer.run_pipeline(
            project_name=language_project,
            skip_level=skip_level,
            **pipeline_kwargs,
        )
        if graph is None:
            raise RuntimeError(
                f"Failed to build code graph for language {language!r} "
                f"under {project_root!r}"
            )
        combined.merge_from(graph)

    base_output.mkdir(parents=True, exist_ok=True)
    combined.save_graph(base_output / "graph.pkl")
    logger.info(
        "Built combined graph for %s languages at %s",
        ", ".join(normalized),
        base_output,
    )
    return combined


# ── LSGraphDecoder ─────────────────────────────────────────────────────────


class LSGraphDecoder:
    """Unified decoder that delegates to language-specific implementations."""

    def __init__(
        self,
        index_file_path: str,
        project_root: Optional[str] = None,
        language: Optional[str] = None,
    ):
        self.index_file_path = index_file_path
        self.project_root = project_root
        self.language = _normalize_language(language)

        self._delegate = self._create_decoder(
            index_file_path=index_file_path,
            project_root=project_root,
        )

        self.code_graph = self._delegate.code_graph

    def _create_decoder(self, index_file_path, project_root):
        decoder_path = graph_decoder_path(self.language)
        if decoder_path is None:
            raise ValueError(f"No decoder for language: {self.language}")

        cls = _load_class(decoder_path)
        backend = graph_cold_start_backend(self.language)
        if backend == "clangd":
            return cls(
                # clangd expects a directory of .idx files, not a single file.
                # Default: <project_root>/.cache/clangd/index/
                idx_directory=index_file_path,
                project_root=project_root,
            )
        return cls(index_file_path=index_file_path, project_root=project_root)

    # ── Delegated methods ─────────────────────────────────────────────

    def decode(self):
        return self._delegate.decode()

    def save_graph(self, output_path: str):
        return self._delegate.save_graph(output_path)
