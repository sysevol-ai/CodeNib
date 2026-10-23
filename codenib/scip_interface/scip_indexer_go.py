#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
SCIP indexer for Go projects using scip-go.

scip-go indexes one module at a time, and ``./...`` at the repository root
stops at nested ``go.mod`` files. Repositories split into nested modules are
indexed module by module and merged into one index (see ``go_modules``), so
``go.etcd.io/etcd/server/v3/auth`` becomes ``go.etcd.io/etcd/v3/server/auth``
and the decoders name every package by its directory.
"""
import os
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple, Union

from ..log_utils import get_logger
from ..profiler import Profiler
from .go_modules import (
    active_workspace,
    go_module_path,
    local_replacements,
    merge_module_indexes,
    nested_go_modules,
    workspace_includes,
)
from .scip_indexer_base import SCIPIndexerBase

logger = get_logger("scip_go_indexer")


def _without_workspace() -> dict:
    """The environment with any enclosing ``go.work`` disabled.

    A nested module is indexed on its own, so a workspace file that lists
    only some modules must not apply to it.
    """
    return {**os.environ, "GOWORK": "off"}


class SCIPGoIndexer(SCIPIndexerBase):
    """
    SCIP indexer for Go projects.

    Uses the scip-go tool to generate SCIP indices for Go codebases.
    Install: make scip-go-tool
    """

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
            language="go",
            decoder_backend=decoder_backend,
        )

    def _check_indexer_available(self) -> bool:
        """Check if scip-go is available."""
        try:
            result = subprocess.run(
                ["scip-go", "--help"],
                capture_output=True,
                text=True,
            )
            # scip-go --help returns 0 on success
            if result.returncode == 0:
                logger.info("scip-go is available")
                return True
            # Some versions print help to stderr but still work
            logger.info("scip-go is available (non-zero exit on --help)")
            return True
        except FileNotFoundError:
            logger.error(
                "scip-go not found. Run make scip-go-tool and use "
                "make active-scip-env for PATH."
            )
            return False

    def _build_index_command(self, **kwargs) -> List[str]:
        """Build the command to generate the SCIP index for Go."""
        return ["scip-go", "--output", str(self.index_file)]

    def _get_decoder_class(self):
        """Get the decoder class for Go."""
        from .scip_decode_go import SCIPGoGraphDecoder

        return SCIPGoGraphDecoder

    def generate_index(self, **kwargs) -> bool:
        """Generate SCIP index for the Go project."""
        # Check if go.mod exists
        go_mod = self.project_root / "go.mod"
        if not go_mod.exists():
            logger.error(
                f"go.mod not found at {go_mod}. "
                "This doesn't appear to be a Go project."
            )
            return False

        if not self._check_indexer_available():
            return False

        cmd = self._build_index_command(**kwargs)
        self.index_file.unlink(missing_ok=True)
        nested = nested_go_modules(self.project_root)
        # A go.work that omits the root module would make ./... fail, so the
        # root run drops it; nested members may still need it.
        workspace_file = active_workspace(self.project_root)
        root_member = workspace_file is not None and workspace_includes(
            workspace_file, self.project_root
        )
        options = (
            {"env": _without_workspace()} if workspace_file and not root_member else {}
        )

        logger.debug(f"Running command: {' '.join(cmd)}")

        with self.profiler.section("generate_index") as section:
            try:
                subprocess.run(
                    cmd,
                    check=True,
                    cwd=self.project_root,
                    capture_output=True,
                    text=True,
                    **options,
                )
                success = True
            except subprocess.CalledProcessError as e:
                logger.error(f"Error generating SCIP index: {e}")
                if e.stdout:
                    logger.error(f"stdout: {e.stdout}")
                if e.stderr:
                    logger.error(f"stderr: {e.stderr}")
                success = False
            except BaseException:
                self.index_file.unlink(missing_ok=True)
                raise

            # Nested modules count toward the same generation time.
            indexed = self.index_file.is_file() and not self.index_file.is_symlink()
            if success and indexed and nested:
                try:
                    success = self._merge_nested_modules(go_mod, nested, workspace_file)
                except BaseException:
                    self.index_file.unlink(missing_ok=True)
                    raise

        duration = section.duration

        if success and not self.index_file.is_symlink() and self.index_file.is_file():
            logger.info(f"Successfully generated SCIP index at {self.index_file}")
            logger.info(f"Index generation took: {duration:.2f} seconds")
            return True
        self.index_file.unlink(missing_ok=True)
        logger.error(f"Index generation failed after {duration:.2f} seconds")
        return False

    def _merge_nested_modules(
        self, go_mod: Path, nested: List[Tuple[str, str]], workspace_file=None
    ) -> bool:
        """Index each nested module and merge it into ``self.index_file``.

        A module that fails to index is skipped and logged; the root index
        and the other modules are kept.
        """
        from .scip_pb2 import Index

        root_module = go_module_path(go_mod)
        if not root_module:
            logger.warning(f"No module path in {go_mod}; nested modules skipped")
            return True
        root = Index()
        root.ParseFromString(self.index_file.read_bytes())
        members = {
            directory
            for directory in ["", *(directory for _module, directory in nested)]
            if workspace_file is not None
            and workspace_includes(workspace_file, self.project_root / directory)
        }
        # A root go.work can already pull nested modules into the root run.
        # Each document belongs to the deepest module directory holding it.
        owners = set()
        for document in root.documents:
            holders = [
                directory
                for _module, directory in nested
                if document.relative_path.startswith(directory + "/")
            ]
            if holders:
                owners.add(max(holders, key=len))
        covered = [entry for entry in nested if entry[1] in owners]
        pending = [entry for entry in nested if entry not in covered]
        indexes = []
        with tempfile.TemporaryDirectory(dir=self.output_dir) as scratch:
            for number, (module, directory) in enumerate(pending):
                output = Path(scratch) / f"module-{number}.scip"
                try:
                    # A workspace member keeps the workspace that wires its
                    # siblings; any other module runs on its own go.mod.
                    member = directory in members
                    subprocess.run(
                        ["scip-go", "--output", str(output)],
                        check=True,
                        cwd=self.project_root / directory,
                        capture_output=True,
                        text=True,
                        **({} if member else {"env": _without_workspace()}),
                    )
                    index = Index()
                    index.ParseFromString(output.read_bytes())
                except (subprocess.CalledProcessError, OSError) as error:
                    logger.warning(
                        f"Skipping Go module {module} at {directory}: {error}"
                    )
                    continue
                indexes.append((module, directory, index))
        directories = {""} | {directory for _module, directory in nested}
        replacements = {
            source: [
                replacement
                for replacement in local_replacements(
                    self.project_root / source / "go.mod", self.project_root
                )
                if replacement.directory in directories
            ]
            for source in directories
        }
        workspace = [
            replacement
            for replacement in (
                local_replacements(workspace_file, self.project_root)
                if workspace_file
                else []
            )
            if replacement.directory in directories
        ]
        merge_module_indexes(
            root, indexes, root_module, covered, replacements, workspace, members
        )
        staging = self.index_file.with_name(self.index_file.name + ".merging")
        staging.write_bytes(root.SerializeToString())
        staging.replace(self.index_file)
        logger.info(
            f"Merged {len(indexes)} of {len(pending)} nested Go modules into the "
            f"index; the root run covered {len(covered)}"
        )
        return True

    def run_pipeline(
        self,
        output_file: Optional[str] = None,
        skip_level: Optional[str] = None,
        *,
        reset_profiler: bool = True,
        report_profile: bool = True,
        **kwargs,
    ):
        """Run Go pipeline while ignoring other-language kwargs."""
        kwargs.pop("project_name", None)
        kwargs.pop("target_dir", None)
        kwargs.pop("cwd", None)
        kwargs.pop("config_path", None)
        kwargs.pop("exclude_vendored_libraries", None)
        kwargs.pop("infer_tsconfig", None)
        kwargs.pop("yarn_workspaces", None)
        kwargs.pop("pnpm_workspaces", None)
        kwargs.pop("npm_workspaces", None)
        kwargs.pop("compdb_path", None)
        kwargs.pop("show_compiler_diagnostics", None)

        return super().run_pipeline(
            output_file=output_file,
            skip_level=skip_level,
            reset_profiler=reset_profiler,
            report_profile=report_profile,
            **kwargs,
        )
