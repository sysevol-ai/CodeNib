#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Unified language-server router for indexing and decoding across all languages.

Routes to language-specific implementations:
    - 'cpp', 'c++', 'c':  clangd             (codeminer.ls_index)
    - 'rust', 'rs':       rust-analyzer       (codeminer.scip_interface)
    - 'ts', 'typescript', 'js', 'javascript': (codeminer.scip_interface)
    - 'python', 'py':     scip-python         (codeminer.scip_interface)

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
from typing import List, Optional, Type, Union

from .graph.code_graph import CodeGraph
from .languages import (
    graph_cold_start_backend,
    graph_decoder_path,
    graph_indexer_path,
    graph_language_aliases,
    normalize_graph_language,
)
from .log_utils import get_logger
from .profiler import Profiler

logger = get_logger("ls_router")

LANGUAGE_ALIASES = graph_language_aliases()


def _normalize_language(language: Optional[str]) -> str:
    """Normalize language string. Defaults to 'python'."""
    if language is None:
        return "python"
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
    ):
        self.project_root = Path(project_root).absolute()
        self.language = _normalize_language(language)
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

        logger.info(f"Initialized LSIndexer for {self.language} at {self.project_root}")

    def _create_indexer(
        self,
        project_root,
        output_dir,
        exclude_patterns,
        profiler,
    ):
        indexer_path = graph_indexer_path(self.language)
        if indexer_path is None:
            raise ValueError(f"No indexer for language: {self.language}")

        backend = graph_cold_start_backend(self.language)
        cls = _load_class(indexer_path)
        kwargs = {
            "project_root": project_root,
            "output_dir": output_dir,
            "exclude_patterns": exclude_patterns,
            "profiler": profiler,
        }
        if backend == "scip":
            kwargs["decoder_backend"] = self.decoder_backend
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
