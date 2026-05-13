#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
SCIP indexer for Go projects using scip-go.
"""
import subprocess
from pathlib import Path
from typing import List, Optional, Union

from ..log_utils import get_logger
from ..profiler import Profiler
from .scip_indexer_base import SCIPIndexerBase

logger = get_logger("scip_go_indexer")


class SCIPGoIndexer(SCIPIndexerBase):
    """
    SCIP indexer for Go projects.

    Uses the scip-go tool to generate SCIP indices for Go codebases.
    Install: go install github.com/sourcegraph/scip-go/cmd/scip-go@latest
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
                "scip-go not found. Install with: "
                "go install github.com/sourcegraph/scip-go/cmd/scip-go@latest"
            )
            return False

    def _build_index_command(self, **kwargs) -> List[str]:
        """Build the command to generate the SCIP index for Go."""
        # scip-go runs from project root and outputs index.scip by default.
        # We move/rename the output afterward if needed, but scip-go doesn't
        # have a --output flag in all versions, so we rely on the default.
        cmd = ["scip-go"]
        return cmd

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

        logger.debug(f"Running command: {' '.join(cmd)}")

        with self.profiler.section("generate_index") as section:
            try:
                subprocess.run(
                    cmd,
                    check=True,
                    cwd=self.project_root,
                    capture_output=True,
                    text=True,
                )
                success = True
            except subprocess.CalledProcessError as e:
                logger.error(f"Error generating SCIP index: {e}")
                if e.stdout:
                    logger.error(f"stdout: {e.stdout}")
                if e.stderr:
                    logger.error(f"stderr: {e.stderr}")
                success = False

        duration = section.duration

        if success:
            # scip-go outputs index.scip in the project root by default.
            # Move it to our output directory if needed.
            default_output = self.project_root / "index.scip"
            if default_output.exists() and default_output != self.index_file:
                import shutil

                shutil.move(str(default_output), str(self.index_file))

            logger.info(f"Successfully generated SCIP index at {self.index_file}")
            logger.info(f"Index generation took: {duration:.2f} seconds")
            return True
        else:
            logger.error(f"Index generation failed after {duration:.2f} seconds")
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
