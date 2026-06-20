#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""SCIP-backed Ruby graph routes using scip-ruby."""

from __future__ import annotations

import fnmatch
import os
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional, Union

from ..languages import scip_cold_start_command_for_language
from ..log_utils import get_logger
from ..profiler import Profiler
from ..types import NODE_TYPE_DIRECTORY, NODE_TYPE_FILE, ROOT_NODE, is_symbol_node
from .scip_indexer_base import SCIPIndexerBase

if TYPE_CHECKING:
    from ..ls_index.lsp_indexer import GenericLSPIndexer

logger = get_logger("scip_ruby_indexer")


class RubyHybridIndexer:
    """Active Ruby graph route: prepared scip-ruby bundles, LSP fallback otherwise."""

    def __init__(
        self,
        project_root: Union[str, Path],
        output_dir: Optional[Union[str, Path]] = None,
        exclude_patterns: Optional[List] = None,
        profiler: Optional[Profiler] = None,
        decoder_backend: Optional[str] = None,
    ):
        self.project_root = Path(project_root).absolute()
        self.output_dir = (
            Path(output_dir).absolute()
            if output_dir
            else Path("/tmp") / self.project_root.name
        )
        self.exclude_patterns = exclude_patterns or []
        self.profiler = profiler
        self.decoder_backend = decoder_backend
        self._delegate = self._preferred_delegate()
        self._sync_delegate_attrs()

    def _preferred_delegate(self):
        if self._scip_bundle_ready():
            return self._scip_delegate()
        return self._lsp_delegate()

    def _scip_delegate(self) -> "SCIPRubyIndexer":
        return SCIPRubyIndexer(
            self.project_root,
            output_dir=self.output_dir,
            exclude_patterns=self.exclude_patterns,
            profiler=self.profiler,
            decoder_backend=self.decoder_backend,
        )

    def _lsp_delegate(self) -> "GenericLSPIndexer":
        from ..ls_index.lsp_indexer import GenericLSPIndexer

        return GenericLSPIndexer(
            self.project_root,
            output_dir=self.output_dir,
            exclude_patterns=self.exclude_patterns,
            profiler=self.profiler,
            language="ruby",
        )

    def _sync_delegate_attrs(self) -> None:
        self.output_dir = self._delegate.output_dir
        self.index_file = self._delegate.index_file
        self.decoded_file = self._delegate.decoded_file
        self.graph_file = self._delegate.graph_file
        self.profiler = self._delegate.profiler

    def _switch_to_lsp(self) -> None:
        logger.warning("Falling back to Ruby LSP graph route for %s", self.project_root)
        self._delegate = self._lsp_delegate()
        self._sync_delegate_attrs()

    def _scip_bundle_ready(self) -> bool:
        gemfile = _ruby_bundle_gemfile(self.project_root)
        if gemfile is None or not gemfile.exists():
            return False
        if _has_explicit_bundle_gemfile():
            return True
        return _gemfile_mentions_scip_ruby(gemfile)

    def generate_index(self, **kwargs) -> bool:
        try:
            if self._delegate.generate_index(**kwargs):
                return True
        except Exception as exc:
            if not isinstance(self._delegate, SCIPRubyIndexer):
                raise
            logger.warning(
                "Ruby SCIP index generation failed for %s: %s",
                self.project_root,
                exc,
            )
        if isinstance(self._delegate, SCIPRubyIndexer):
            self._switch_to_lsp()
            return self._delegate.generate_index(**kwargs)
        return False

    def decode_index(self) -> bool:
        return self._delegate.decode_index()

    def process_index(self, output_file: Optional[str] = None):
        return self._delegate.process_index(output_file=output_file)

    def run_pipeline(
        self,
        output_file: Optional[str] = None,
        skip_level: Optional[str] = None,
        *,
        reset_profiler: bool = True,
        report_profile: bool = True,
        **kwargs,
    ):
        try:
            graph = self._delegate.run_pipeline(
                output_file=output_file,
                skip_level=skip_level,
                reset_profiler=reset_profiler,
                report_profile=report_profile,
                **kwargs,
            )
        except Exception as exc:
            if not isinstance(self._delegate, SCIPRubyIndexer):
                raise
            logger.warning(
                "Ruby SCIP graph route failed for %s: %s", self.project_root, exc
            )
            graph = None
        if graph is not None or not isinstance(self._delegate, SCIPRubyIndexer):
            return graph

        self._switch_to_lsp()
        return self._delegate.run_pipeline(
            output_file=output_file,
            skip_level=skip_level,
            reset_profiler=reset_profiler,
            report_profile=report_profile,
            **kwargs,
        )

    def clear_cache(self, level: str = "all") -> bool:
        return self._delegate.clear_cache(level=level)


class SCIPRubyIndexer(SCIPIndexerBase):
    """Pure scip-ruby route used by candidate gates and active hybrid routing."""

    def __init__(
        self,
        project_root: Union[str, Path],
        output_dir: Optional[Union[str, Path]] = None,
        exclude_patterns: Optional[List] = None,
        profiler: Optional[Profiler] = None,
        decoder_backend: Optional[str] = None,
    ):
        super().__init__(
            project_root=project_root,
            output_dir=output_dir,
            exclude_patterns=exclude_patterns,
            profiler=profiler,
            language="ruby",
            decoder_backend=decoder_backend,
        )
        self._target_dir: str | None = None

    def _check_indexer_available(self) -> bool:
        command = scip_cold_start_command_for_language("ruby")
        if not command:
            logger.error("No SCIP cold-start command registered for Ruby")
            return False
        if shutil.which(command[0]) is None:
            logger.error(
                "Ruby SCIP command %r was not found. Run make "
                "scip-cold-start-tools or set CODEMINER_RUBY_SCIP_CMD.",
                command[0],
            )
            return False
        return True

    def _build_index_command(self, **kwargs) -> list[str]:
        command = list(scip_cold_start_command_for_language("ruby") or [])
        if not command:
            return []
        if "--dir" not in command:
            command.extend(["--dir", "."])
        if "--index-file" not in command:
            command.extend(["--index-file", str(self.index_file)])
        if "--gem-metadata" not in command:
            command.extend(["--gem-metadata", self._gem_metadata()])
        for flag in ("--silence-dev-message", "--suppress-non-critical"):
            if flag not in command:
                command.append(flag)
        return command

    def _get_decoder_class(self):
        from .scip_decode_ruby import SCIPRubyGraphDecoder

        return SCIPRubyGraphDecoder

    def generate_index(self, timeout: Optional[int] = None, **kwargs) -> bool:
        if not self._check_indexer_available():
            return False
        command = self._build_index_command(**kwargs)
        if not command:
            return False

        if not self._prepare_bundle(command, timeout=timeout):
            return False

        with self.profiler.section("generate_index") as section:
            try:
                subprocess.run(
                    command,
                    check=True,
                    cwd=self.project_root,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    env=self._ruby_env(),
                )
            except subprocess.TimeoutExpired as exc:
                logger.error("Ruby SCIP indexing timed out: %s", exc)
                return False
            except subprocess.CalledProcessError as exc:
                logger.error("Error generating Ruby SCIP index: %s", exc)
                detail = _combine_command_output(exc.stdout, exc.stderr)
                if detail:
                    logger.error(detail)
                return False

        duration = section.duration
        if self.index_file.exists():
            logger.info("Successfully generated Ruby SCIP index at %s", self.index_file)
            logger.info("Index generation took: %.2f seconds", duration)
            return True

        logger.error("scip-ruby completed but did not create %s", self.index_file)
        return False

    def run_pipeline(
        self,
        output_file: Optional[str] = None,
        skip_level: Optional[str] = None,
        *,
        reset_profiler: bool = True,
        report_profile: bool = True,
        **kwargs,
    ):
        kwargs.pop("project_name", None)
        self._target_dir = _normalize_rel_path(kwargs.pop("target_dir", None))
        return super().run_pipeline(
            output_file=output_file,
            skip_level=skip_level,
            reset_profiler=reset_profiler,
            report_profile=report_profile,
            **kwargs,
        )

    def process_index(self, output_file: Optional[str] = None):
        graph = super().process_index(output_file=None)
        if graph is None:
            return None

        graph = self._filter_project_graph(graph)
        if output_file:
            graph.save_graph(output_file)
        return graph

    def _prepare_bundle(self, command: list[str], *, timeout: Optional[int]) -> bool:
        bundle_prefix = _bundle_prefix(command)
        if not bundle_prefix:
            return True
        bundle_gemfile = self._bundle_gemfile()
        if bundle_gemfile is None or not bundle_gemfile.exists():
            logger.error(
                "Ruby SCIP command uses Bundler, but no Gemfile was found. "
                "Set CODEMINER_RUBY_BUNDLE_GEMFILE for an overlay Gemfile."
            )
            return False

        for args in (
            [*bundle_prefix, "config", "set", "path", "vendor/bundle"],
            [*bundle_prefix, "install"],
        ):
            try:
                subprocess.run(
                    args,
                    check=True,
                    cwd=self.project_root,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    env=self._ruby_env(),
                )
            except subprocess.TimeoutExpired as exc:
                logger.error("Ruby bundle preparation timed out: %s", exc)
                return False
            except subprocess.CalledProcessError as exc:
                logger.error("Ruby bundle preparation failed: %s", exc)
                detail = _combine_command_output(exc.stdout, exc.stderr)
                if detail:
                    logger.error(detail)
                return False
        return True

    def _ruby_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.pop("GEM_PATH", None)
        gemfile = self._bundle_gemfile(env)
        if gemfile is not None:
            env["BUNDLE_GEMFILE"] = str(gemfile)
        return env

    def _bundle_gemfile(self, env: dict[str, str] | None = None) -> Path | None:
        return _ruby_bundle_gemfile(self.project_root, env=env)

    def _gem_metadata(self) -> str:
        return f"{self.project_root.name}@workspace"

    def _filter_project_graph(self, graph):
        if not self._target_dir and not self.exclude_patterns:
            return graph

        keep = [
            vertex.index
            for vertex in graph.graph.vs
            if self._keep_vertex(vertex.attributes())
        ]
        if len(keep) == graph.graph.vcount():
            return graph

        graph.graph = graph.graph.subgraph(keep)
        graph.name_to_vertex = {
            vertex["name"]: vertex.index
            for vertex in graph.graph.vs
            if "name" in vertex.attributes()
        }
        graph.symbol_ranges = {
            name: value
            for name, value in graph.symbol_ranges.items()
            if name in graph.name_to_vertex
        }
        graph._invalidate_edge_index()
        graph.build_range_indexes()
        return graph

    def _keep_vertex(self, attrs: dict) -> bool:
        node_type = attrs.get("type")
        name = attrs.get("name")
        if node_type == "root" or name == ROOT_NODE:
            return True
        if node_type in {NODE_TYPE_DIRECTORY, NODE_TYPE_FILE}:
            return self._path_allowed(str(name), allow_target_ancestor=True)
        if is_symbol_node(node_type):
            path = _definition_path(attrs) or attrs.get("file")
            return bool(path and self._path_allowed(str(path)))
        return True

    def _path_allowed(self, path: str, *, allow_target_ancestor: bool = False) -> bool:
        rel_path = _normalize_rel_path(path)
        if not rel_path:
            return False
        if self._target_dir:
            in_target = rel_path == self._target_dir or rel_path.startswith(
                f"{self._target_dir}/"
            )
            is_target_ancestor = allow_target_ancestor and self._target_dir.startswith(
                f"{rel_path}/"
            )
            if not in_target and not is_target_ancestor:
                return False
        return not _matches_any(rel_path, self.exclude_patterns)


def _bundle_prefix(command: list[str]) -> list[str]:
    if not command or Path(command[0]).name != "bundle":
        return []
    try:
        exec_index = command.index("exec")
    except ValueError:
        return [command[0]]
    return command[:exec_index]


def _has_explicit_bundle_gemfile(env: dict[str, str] | None = None) -> bool:
    values = env if env is not None else os.environ
    return bool(
        values.get("CODEMINER_RUBY_BUNDLE_GEMFILE") or values.get("BUNDLE_GEMFILE")
    )


def _ruby_bundle_gemfile(
    project_root: Path,
    env: dict[str, str] | None = None,
) -> Path | None:
    values = env if env is not None else os.environ
    override = values.get("CODEMINER_RUBY_BUNDLE_GEMFILE") or values.get(
        "BUNDLE_GEMFILE"
    )
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute():
            path = project_root / path
        return path.resolve()
    gemfile = project_root / "Gemfile"
    return gemfile if gemfile.exists() else None


def _gemfile_mentions_scip_ruby(gemfile: Path) -> bool:
    try:
        return "scip-ruby" in gemfile.read_text(encoding="utf-8")
    except OSError:
        return False


def _definition_path(attrs: dict) -> str | None:
    unified_name = attrs.get("unified_name")
    if not unified_name or ":" not in unified_name:
        return None
    return _normalize_rel_path(unified_name.split(":", 1)[0])


def _normalize_rel_path(path: str | None) -> str | None:
    if path is None:
        return None
    normalized = str(path).strip().replace("\\", "/").strip("/")
    return normalized or None


def _matches_any(path: str, patterns: list[str]) -> bool:
    return any(_matches_pattern(path, pattern) for pattern in patterns)


def _matches_pattern(path: str, pattern: str) -> bool:
    normalized = _normalize_rel_path(pattern)
    if not normalized:
        return False
    if fnmatch.fnmatch(path, normalized):
        return True
    if normalized.endswith("/**"):
        prefix = normalized[:-3]
        return path == prefix or path.startswith(f"{prefix}/")
    return False


def _combine_command_output(stdout: str | None, stderr: str | None) -> str:
    parts = []
    if stdout and stdout.strip():
        parts.append(stdout.strip())
    if stderr and stderr.strip():
        parts.append(stderr.strip())
    return "\n".join(parts)
