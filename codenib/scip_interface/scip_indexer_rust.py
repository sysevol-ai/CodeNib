#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
SCIP indexer for Rust projects using rust-analyzer.
"""
import subprocess
from pathlib import Path
from typing import List, Optional, Union

from ..log_utils import get_logger
from ..profiler import Profiler
from .rust_analyzer import rust_analyzer_command, rust_toolchain
from .scip_indexer_base import SCIPIndexerBase

logger = get_logger("scip_rust_indexer")


class SCIPRustIndexer(SCIPIndexerBase):
    """
    SCIP indexer for Rust projects.

    Uses the rust-analyzer tool to generate SCIP indices for Rust codebases.
    rust-analyzer scip generates a SCIP index from Rust source code.
    """

    def __init__(
        self,
        project_root: Union[str, Path],
        output_dir: Optional[Union[str, Path]] = None,
        exclude_patterns: Optional[List] = None,
        profiler: Optional[Profiler] = None,
        decoder_backend: Optional[str] = None,
    ):
        """
        Initialize the Rust SCIP indexer.

        Args:
            project_root: Root directory of the Rust project (must contain Cargo.toml)
            output_dir: Directory to store output files (defaults to /tmp/project_name)
            exclude_patterns: List of patterns to exclude from indexing
            profiler: Profiler instance for performance tracking
            decoder_backend: ``"serial"`` (default) or ``"core"``.
        """
        super().__init__(
            project_root=project_root,
            output_dir=output_dir,
            exclude_patterns=exclude_patterns,
            profiler=profiler,
            language="rust",
            decoder_backend=decoder_backend,
        )

    def _check_indexer_available(self) -> bool:
        """
        Check if rust-analyzer is available.

        Returns:
            bool: True if rust-analyzer is available, False otherwise
        """
        import os

        env = os.environ.copy()
        env["RUSTUP_TOOLCHAIN"] = rust_toolchain()
        try:
            result = subprocess.run(
                rust_analyzer_command("--version"),
                check=True,
                capture_output=True,
                text=True,
                env=env,
            )
            logger.info(
                "rust-analyzer version: %s",
                result.stdout.strip(),
            )
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            logger.error(
                "rust-analyzer not found. Please install it with: "
                "rustup component add rust-analyzer "
                f"--toolchain {rust_toolchain()}"
            )
            return False

    def _build_index_command(
        self,
        config_path: Optional[str] = None,
        exclude_vendored_libraries: bool = False,
        **kwargs,
    ) -> List[str]:
        """
        Build the command to generate the SCIP index for Rust.

        Args:
            config_path: Path to a JSON configuration file for customizing cargo behavior
            exclude_vendored_libraries: Exclude code from vendored libraries from the index
            **kwargs: Additional arguments (ignored)

        Returns:
            List[str]: Command as list of strings
        """
        cmd = rust_analyzer_command("scip", str(self.project_root))

        # Output path
        cmd.extend(["--output", str(self.index_file)])

        # Config path for cargo customization
        if config_path:
            cmd.extend(["--config-path", str(config_path)])

        # Exclude vendored libraries
        if exclude_vendored_libraries:
            cmd.append("--exclude-vendored-libraries")

        return cmd

    def _get_decoder_class(self):
        """
        Get the decoder class for Rust.

        Returns:
            SCIPRustGraphDecoder class for Rust-specific symbol handling
        """
        from .scip_decode_rust import SCIPRustGraphDecoder

        return SCIPRustGraphDecoder

    def generate_index(
        self,
        config_path: Optional[str] = None,
        exclude_vendored_libraries: bool = False,
    ) -> bool:
        """
        Generate SCIP index for the Rust project.

        Args:
            config_path: Path to a JSON configuration file for customizing cargo behavior
            exclude_vendored_libraries: Exclude code from vendored libraries from the index

        Returns:
            bool: True if index generation was successful, False otherwise
        """
        # Check if Cargo.toml exists
        cargo_toml = self.project_root / "Cargo.toml"
        if not cargo_toml.exists():
            logger.error(
                f"Cargo.toml not found at {cargo_toml}. "
                "This doesn't appear to be a Rust project."
            )
            return False

        return super().generate_index(
            config_path=config_path,
            exclude_vendored_libraries=exclude_vendored_libraries,
        )

    def run_pipeline(
        self,
        output_file: Optional[str] = None,
        skip_level: Optional[str] = None,
        *,
        reset_profiler: bool = True,
        report_profile: bool = True,
        **kwargs,
    ):
        """
        Run Rust pipeline while ignoring Python-specific kwargs.
        """
        kwargs.pop("project_name", None)
        kwargs.pop("target_dir", None)
        kwargs.pop("cwd", None)

        return super().run_pipeline(
            output_file=output_file,
            skip_level=skip_level,
            reset_profiler=reset_profiler,
            report_profile=report_profile,
            **kwargs,
        )
