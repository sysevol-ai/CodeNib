# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Generic LSP cold-start indexer.

The first generic backend captures ``textDocument/documentSymbol`` responses
and decodes them into a definition/containment graph.  It deliberately does not
claim cross-file reference parity yet; that belongs to the backend-alignment
work for servers that support references/call hierarchy consistently.
"""

from __future__ import annotations

import fnmatch
import json
import os
from pathlib import Path
from typing import Iterable, Optional, Union

from ..graph.code_graph import CodeGraph
from ..graph.incremental.lsp_client import LSPClient, is_lsp_document_unavailable_error
from ..languages import extensions_for_language, normalize_graph_language
from ..log_utils import get_logger
from ..profiler import Profiler
from ..repository_files import iter_repository_source_files
from .lsp_graph_decode import GenericLSPGraphDecoder, iter_lsp_symbol_definitions

logger = get_logger("generic_lsp_indexer")

_INDEX_SCHEMA_VERSION = 1


class GenericLSPIndexer:
    """Build a CodeGraph from a language server's document symbols."""

    def __init__(
        self,
        project_root: Union[str, Path],
        output_dir: Optional[Union[str, Path]] = None,
        exclude_patterns: Optional[list[str]] = None,
        profiler: Optional[Profiler] = None,
        language: str = "java",
    ):
        self.project_root = Path(project_root).absolute()
        self.language = normalize_graph_language(language) or language
        self.output_dir = (
            Path(output_dir).absolute()
            if output_dir
            else Path("/tmp") / self.project_root.name
        )
        os.makedirs(self.output_dir, exist_ok=True)

        self.index_file = self.output_dir / "index.lsp.json"
        self.decoded_file = self.index_file
        self.graph_file = self.output_dir / "graph.pkl"
        self.exclude_patterns = exclude_patterns or []
        self.profiler = profiler or Profiler(f"lsp_{language}_indexer")

    def generate_index(self, **kwargs) -> bool:
        files = list(self._iter_source_files(kwargs.get("target_dir")))
        if not files:
            logger.warning(
                "No %s source files found under %s", self.language, self.project_root
            )
            return False

        command = LSPClient.get_lsp_command(self.language)
        if not command:
            logger.error("No LSP command registered for %s", self.language)
            return False

        logger.info(
            "Collecting document symbols for %d %s files", len(files), self.language
        )
        include_references = bool(kwargs.get("include_references", False))
        reference_timeout_s = float(kwargs.get("reference_timeout_s", 10.0))
        request_timeout_s = float(kwargs.get("request_timeout_s", reference_timeout_s))
        reference_retries = int(kwargs.get("reference_retries", 0))
        reference_concurrency = int(kwargs.get("reference_concurrency", 10))
        if reference_concurrency < 1:
            raise ValueError("reference_concurrency must be positive")
        strict_references = bool(kwargs.get("strict_references", False))
        try:
            with LSPClient(command, str(self.project_root), self.language) as client:
                client.operation_retries = reference_retries
                client.strict_request_failures = strict_references
                client.reference_timeout_s = reference_timeout_s
                client.reference_retries = reference_retries
                if strict_references:
                    client.document_symbol_timeout_s = request_timeout_s
                    client.definition_timeout_s = request_timeout_s
                    client.semantic_tokens_timeout_s = request_timeout_s
                records = []
                if include_references:
                    with self.profiler.section(
                        "generate_index.lsp_workspace_initial_idle"
                    ):
                        initial_idle = client.wait_until_idle()
                    if not initial_idle:
                        message = (
                            "LSP workspace did not become idle before "
                            "document-symbol collection"
                        )
                        if strict_references:
                            raise RuntimeError(message)
                        logger.warning(message)
                with self.profiler.section("generate_index.lsp_document_symbols"):
                    for rel_path in files:
                        file_path = str(self.project_root / rel_path)
                        try:
                            symbols = client.document_symbol(file_path)
                        finally:
                            client.close_document(file_path)
                        records.append(
                            {"path": rel_path.as_posix(), "symbols": symbols}
                        )

                # Complete workspace-wide symbol discovery before issuing
                # reference requests. Some servers finish project loading
                # incrementally as documents are opened, so interleaving
                # the two phases makes early reference coverage depend on
                # traversal order and background-index timing.
                if include_references:
                    with self.profiler.section("generate_index.lsp_workspace_idle"):
                        idle = client.wait_until_idle()
                    if not idle:
                        message = (
                            "LSP workspace did not become idle before "
                            "reference collection"
                        )
                        if strict_references:
                            raise RuntimeError(message)
                        logger.warning(message)
                    if files:
                        # Some servers end workDoneProgress before dependent
                        # document analysis is query-ready. A request barrier
                        # on an opened document drains that remaining work.
                        with self.profiler.section("generate_index.lsp_analysis_ready"):
                            analysis_ready = client.wait_for_analysis(
                                str(self.project_root / files[0])
                            )
                        if not analysis_ready:
                            message = (
                                "LSP document analysis did not become ready "
                                "before reference collection"
                            )
                            if strict_references:
                                raise RuntimeError(message)
                            logger.warning(message)
                    with self.profiler.section("generate_index.lsp_references"):
                        for rel_path, record in zip(files, records, strict=True):
                            file_path = str(self.project_root / rel_path)
                            try:
                                record["references"] = self._collect_references(
                                    client,
                                    rel_path,
                                    record["symbols"],
                                    strict=strict_references,
                                    max_in_flight=reference_concurrency,
                                )
                                last_error = getattr(client, "last_error", None)
                                if is_lsp_document_unavailable_error(last_error):
                                    record["provider_unavailable"] = str(
                                        last_error.get("message") or ""
                                    )
                            finally:
                                client.close_document(file_path)
        except TimeoutError:
            raise
        except Exception as exc:
            logger.error("LSP index generation failed for %s: %s", self.language, exc)
            return False

        payload = {
            "schema_version": _INDEX_SCHEMA_VERSION,
            "language": self.language,
            "project_root": str(self.project_root),
            "files": records,
        }
        self.index_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return True

    def decode_index(self) -> bool:
        return self.index_file.exists()

    def process_index(
        self, output_file: Optional[str] = None
    ) -> Union[CodeGraph, None]:
        if not self.index_file.exists():
            logger.error("LSP index file not found: %s", self.index_file)
            return None
        try:
            with self.profiler.section("process_index.decode"):
                decoder = GenericLSPGraphDecoder(
                    index_file_path=str(self.index_file),
                    project_root=str(self.project_root),
                )
                graph = decoder.decode()
            if output_file:
                with self.profiler.section("process_index.save_graph"):
                    graph.save_graph(output_file)
            return graph
        except Exception as exc:
            logger.error("Error decoding LSP index %s: %s", self.index_file, exc)
            return None

    def run_pipeline(
        self,
        output_file: Optional[str] = None,
        skip_level: Optional[str] = None,
        *,
        reset_profiler: bool = True,
        report_profile: bool = True,
        **kwargs,
    ) -> Union[CodeGraph, None]:
        if output_file is None:
            output_file = str(self.graph_file)

        if reset_profiler:
            self.profiler.reset()

        if skip_level == "graph" and self.graph_file.exists():
            try:
                return CodeGraph.load_graph(str(self.graph_file))
            except Exception as exc:
                logger.warning(
                    "Failed to load cached graph %s: %s", self.graph_file, exc
                )

        if skip_level not in ("raw", "decode", "graph") or not self.index_file.exists():
            if not self.generate_index(**kwargs):
                return None

        if not self.decode_index():
            return None

        graph = self.process_index(output_file=output_file)
        if report_profile:
            self.profiler.report(reset=False)
        return graph

    def clear_cache(self, level: str = "all") -> bool:
        targets = [self.graph_file]
        if level in ("all", "raw", "decode"):
            targets.append(self.index_file)
        ok = True
        for path in targets:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                ok = False
        return ok

    def _iter_source_files(self, target_dir: Optional[str] = None) -> Iterable[Path]:
        extensions = extensions_for_language(self.language, "graph")
        for rel_path in iter_repository_source_files(
            self.project_root,
            extensions=extensions,
            target_dir=target_dir,
        ):
            if self._is_excluded(rel_path):
                continue
            yield rel_path

    def _collect_references(
        self,
        client: LSPClient,
        rel_path: Path,
        symbols: list[dict],
        *,
        strict: bool = False,
        max_in_flight: int = 10,
    ) -> list[dict]:
        definitions = list(
            iter_lsp_symbol_definitions(
                rel_path.as_posix(),
                symbols,
                language=self.language,
            )
        )
        file_path = str(self.project_root / rel_path)
        outcomes = client.references_batch(
            [
                (
                    file_path,
                    definition["selection_line"],
                    definition["selection_character"],
                )
                for definition in definitions
            ],
            include_declaration=False,
            max_in_flight=max_in_flight,
        )
        references = []
        for definition, (locations, error) in zip(definitions, outcomes, strict=True):
            if is_lsp_document_unavailable_error(error):
                return []
            if strict and error and error.get("code") != -2:
                raise RuntimeError(
                    "reference oracle request failed for "
                    f"{rel_path}:{definition['selection_line']}: {error}"
                )
            if not locations:
                continue
            references.append(
                {
                    "target_unified_name": definition["unified_name"],
                    "target_start_line": definition["start_line"],
                    "target_file": rel_path.as_posix(),
                    "locations": locations,
                }
            )
        return references

    def _is_excluded(self, rel_path: Path) -> bool:
        text = rel_path.as_posix()
        return any(fnmatch.fnmatch(text, pattern) for pattern in self.exclude_patterns)


__all__ = ["GenericLSPIndexer"]
