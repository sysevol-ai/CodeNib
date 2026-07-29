# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
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
from ..graph.incremental.lsp_client import LSPClient
from ..languages import extensions_for_language, normalize_graph_language
from ..log_utils import get_logger
from ..paths import temp_state_dir
from ..profiler import Profiler
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
            else temp_state_dir() / self.project_root.name
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
        try:
            with self.profiler.section("generate_index.lsp_document_symbols"):
                with LSPClient(
                    command, str(self.project_root), self.language
                ) as client:
                    records = []
                    for rel_path in files:
                        symbols = client.document_symbol(
                            str(self.project_root / rel_path)
                        )
                        record = {"path": rel_path.as_posix(), "symbols": symbols}
                        if include_references:
                            record["references"] = self._collect_references(
                                client,
                                rel_path,
                                symbols,
                            )
                        records.append(record)
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
        """Remove artifacts above the requested preservation boundary.

        Generic LSP has one raw/decoded JSON artifact, followed by graph.pkl.
        Its public cache levels match the SCIP and clangd indexers:

        - ``graph`` keeps every artifact;
        - ``decode`` and ``raw`` keep index.lsp.json and remove graph.pkl;
        - ``all`` removes both artifacts.
        """
        if level not in ("graph", "decode", "raw", "all"):
            logger.error("Invalid cache level: %s", level)
            return False

        targets = []
        if level in ("decode", "raw", "all"):
            targets.append(self.graph_file)
        if level == "all":
            targets.append(self.index_file)
        ok = True
        for path in targets:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                ok = False
        return ok

    def _iter_source_files(self, target_dir: Optional[str] = None) -> Iterable[Path]:
        root = (
            (self.project_root / target_dir).resolve()
            if target_dir
            else self.project_root
        )
        extensions = extensions_for_language(self.language, "graph")
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            rel_path = path.relative_to(self.project_root)
            if path.suffix not in extensions:
                continue
            if self._is_excluded(rel_path):
                continue
            yield rel_path

    def _collect_references(
        self,
        client: LSPClient,
        rel_path: Path,
        symbols: list[dict],
    ) -> list[dict]:
        references = []
        for definition in iter_lsp_symbol_definitions(
            rel_path.as_posix(),
            symbols,
            language=self.language,
        ):
            locations = client.references(
                str(self.project_root / rel_path),
                definition["selection_line"],
                definition["selection_character"],
                include_declaration=False,
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
