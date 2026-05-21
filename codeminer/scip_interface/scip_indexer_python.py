#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
SCIP indexer for Python projects using scip-python (via conda environment).
"""
import os
import signal
import subprocess
from pathlib import Path
from typing import List, Optional, Union

from ..log_utils import get_logger
from ..profiler import Profiler
from .scip_indexer_base import SCIPIndexerBase

logger = get_logger("scip_python_indexer")

# Upper bounds (seconds) that turn a hung child process into a fast, clear
# failure instead of letting it run until the CI job's wall-clock timeout.
# These are generous relative to normal runtime (scip-python indexes sympy in
# a few minutes) and only fire on a genuine stall.
_SCIP_PYTHON_INDEX_TIMEOUT_S = 1200  # scip-python (Node) index run
_CONDA_ENV_CREATE_TIMEOUT_S = 600  # fallback `conda env create`


def _run_checked_with_timeout(cmd, *, timeout, **popen_kwargs):
    """Like ``subprocess.run(cmd, check=True, timeout=...)`` but kills the whole
    process group on timeout.

    ``subprocess.run``'s own timeout only SIGKILLs the immediate child. conda
    (libmamba solver) and scip-python (Node) spawn grandchildren that survive
    as orphans, keep holding the conda env/pkg lock, and wedge a self-hosted
    runner so later steps block forever. Running in a new session and killing
    the group ensures no descendant leaks past the timeout.
    """
    with subprocess.Popen(cmd, start_new_session=True, **popen_kwargs) as proc:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.communicate()
            raise
        retcode = proc.poll()
        if retcode:
            raise subprocess.CalledProcessError(
                retcode, cmd, output=stdout, stderr=stderr
            )
    return subprocess.CompletedProcess(proc.args, retcode, stdout, stderr)


class SCIPPythonIndexer(SCIPIndexerBase):
    """
    SCIP indexer for Python projects.

    Uses the scip-python tool (installed in a conda environment) to generate
    SCIP indices for Python codebases.
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
        Initialize the Python SCIP indexer.

        Args:
            project_root: Root directory of the Python project
            output_dir: Directory to store output files (defaults to /tmp/project_name)
            exclude_patterns: List of patterns to exclude from indexing
            profiler: Profiler instance for performance tracking
            decoder_backend: ``"serial"`` (default) or ``"core"``.
        """
        # Python uses /tmp/project_name as default output dir for backward compatibility
        if output_dir is None:
            output_dir = Path("/tmp") / Path(project_root).absolute().name

        super().__init__(
            project_root=project_root,
            output_dir=output_dir,
            exclude_patterns=exclude_patterns,
            profiler=profiler,
            language="python",
            decoder_backend=decoder_backend,
        )

        # Conda environment configuration
        self.conda_env_name = "scip-env"
        self.env_file = self.module_dir / "scip-environment.yml"

    def _check_indexer_available(self) -> bool:
        """
        Check if the conda environment for scip-python is available.

        Returns:
            bool: True if the environment is available, False otherwise
        """
        return self._ensure_conda_env()

    def _build_index_command(
        self,
        cwd: Optional[str] = None,
        project_name: Optional[str] = None,
        target_dir: Optional[str] = None,
        **kwargs,
    ) -> List[str]:
        """
        Build the command to generate the SCIP index for Python.

        Args:
            cwd: Working directory to run the index command
            project_name: Project name to use in the index
            target_dir: Optional subdirectory to target for indexing
            **kwargs: Additional arguments (ignored)

        Returns:
            List[str]: Command as list of strings
        """
        cmd = ["scip-python", "index"]

        if cwd:
            cmd.append("--cwd")
            cmd.append(str(Path(cwd).resolve()))

        if project_name:
            cmd.extend(["--project-name", project_name])
        else:
            cmd.extend(["--project-name", self.project_root.name])

        cmd.extend(["--output", str(self.index_file)])

        if target_dir:
            cmd.extend(["--target-only", target_dir])

        # Note: scip-python does not support --exclude option
        # exclude_patterns are silently ignored for Python indexing
        if self.exclude_patterns:
            logger.warning(
                f"scip-python does not support exclude patterns. "
                f"Ignoring: {self.exclude_patterns}"
            )

        return cmd

    def _get_decoder_class(self):
        """
        Get the decoder class for Python.

        Returns:
            SCIPPythonGraphDecoder class for Python-specific symbol handling
        """
        from .scip_decode_python import SCIPPythonGraphDecoder

        return SCIPPythonGraphDecoder

    def generate_index(
        self,
        cwd: Optional[str] = None,
        project_name: Optional[str] = None,
        target_dir: Optional[str] = None,
        **kwargs,
    ) -> bool:
        """
        Generate SCIP index for the Python project.

        Uses conda environment to run scip-python.

        Args:
            cwd: Working directory (defaults to project_root)
            project_name: Project name to use in the index
            target_dir: Optional subdirectory to target for indexing
            **kwargs: Additional arguments (ignored)

        Returns:
            bool: True if index generation was successful, False otherwise
        """
        if not self._check_indexer_available():
            return False

        cmd = self._build_index_command(
            cwd=cwd or str(self.project_root),
            project_name=project_name,
            target_dir=target_dir,
        )

        logger.debug(f"Running command: {' '.join(cmd)}")

        with self.profiler.section("generate_index") as section:
            success = self._run_in_conda_env(cmd, self.project_root)
        duration = section.duration

        if success:
            logger.info(f"Successfully generated SCIP index at {self.index_file}")
            logger.info(f"Index generation took: {duration:.2f} seconds")
            return True
        else:
            logger.error(f"Index generation failed after {duration:.2f} seconds")
            # Remove partial index file so pipeline does not continue with broken data
            if self.index_file.exists():
                self.index_file.unlink()
                logger.info("Removed partial index file")
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
        """
        Run Python pipeline while ignoring non-Python kwargs.
        """
        # Pop kwargs from other languages
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

    # ── Conda environment helpers ──────────────────────────────────────

    def _ensure_conda_env(self) -> bool:
        """
        Ensure that the conda environment for SCIP is available.

        Returns:
            bool: True if environment is available, False otherwise
        """
        try:
            subprocess.run(["conda", "--version"], check=True, capture_output=True)

            result = subprocess.run(
                ["conda", "env", "list"], check=True, capture_output=True, text=True
            )

            if self.conda_env_name in result.stdout:
                logger.info(f"Conda environment {self.conda_env_name!r} already exists")
                return True

            if self.env_file.exists():
                logger.info(f"Creating conda environment {self.conda_env_name!r}...")

                create_cmd = [
                    "conda",
                    "env",
                    "create",
                    "--quiet",
                    "--file",
                    str(self.env_file),
                    "--solver=libmamba",
                ]

                try:
                    _run_checked_with_timeout(create_cmd, timeout=300)
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
                    logger.warning(
                        f"Fast environment creation failed: {e}. "
                        "Falling back to standard method..."
                    )
                    _run_checked_with_timeout(
                        ["conda", "env", "create", "--file", str(self.env_file)],
                        timeout=_CONDA_ENV_CREATE_TIMEOUT_S,
                    )

                logger.info(
                    f"Conda environment {self.conda_env_name!r} created successfully"
                )
                return True
            else:
                logger.error(f"Environment file not found at {self.env_file}")
                return False

        except subprocess.TimeoutExpired as e:
            logger.error(
                f"Conda environment creation timed out after {e.timeout}s "
                f"(cmd: {e.cmd}). Aborting instead of hanging the job."
            )
            return False
        except subprocess.CalledProcessError as e:
            logger.error(f"Error setting up conda environment: {e}")
            if hasattr(e, "output") and e.output:
                logger.error(f"Command output: {e.output}")
            if hasattr(e, "stderr") and e.stderr:
                logger.error(f"Error details: {e.stderr}")
            return False
        except FileNotFoundError:
            logger.error(
                "Conda not found in PATH. Please install conda or add it to PATH."
            )
            return False

    def _get_conda_env_bin(self) -> Optional[str]:
        """Return the bin directory for the scip conda environment."""
        try:
            result = subprocess.run(
                ["conda", "info", "--envs", "--json"],
                capture_output=True,
                text=True,
                check=True,
            )
            import json

            envs = json.loads(result.stdout).get("envs", [])
            for env_path in envs:
                if env_path.endswith(f"/{self.conda_env_name}"):
                    bin_dir = str(Path(env_path) / "bin")
                    logger.debug(f"Found scip-env bin directory: {bin_dir}")
                    return bin_dir
        except Exception as e:
            logger.warning(f"Failed to locate scip-env bin directory: {e}")
        logger.warning(
            "Could not find scip-env bin directory, will fall back to conda run"
        )
        return None

    def _run_in_conda_env(
        self, cmd: list, cwd: Optional[Union[str, Path]] = None
    ) -> bool:
        """
        Run a command in the SCIP conda environment.

        Args:
            cmd: Command to run
            cwd: Working directory

        Returns:
            bool: True if command succeeded, False otherwise
        """
        try:
            scip_bin = self._get_conda_env_bin()
            # Resolve symlinks so subprocess cwd matches real paths
            work_dir = Path(cwd if cwd else self.project_root).resolve()

            if scip_bin:
                # Run directly with scip-env/bin on PATH instead of
                # using `conda run`, which resets PATH during activation
                # and causes pip3 to resolve to the wrong environment.
                env = os.environ.copy()
                env["PATH"] = f"{scip_bin}:{env.get('PATH', '')}"
                # scip-python is Node-based; on large repos (e.g. sympy,
                # ~944 files) it blows past the default V8 ~4 GB old-space
                # limit and dies with a SIGABRT'd OOM. Bump for this
                # subprocess only; honor an explicit caller-set value.
                node_opts = env.get("NODE_OPTIONS", "")
                if "--max-old-space-size" not in node_opts:
                    env["NODE_OPTIONS"] = (
                        node_opts + " --max-old-space-size=8192"
                    ).strip()
                logger.info(f"Running with scip-env PATH ({scip_bin}): {cmd}")
                _run_checked_with_timeout(
                    cmd,
                    cwd=work_dir,
                    env=env,
                    timeout=_SCIP_PYTHON_INDEX_TIMEOUT_S,
                )
            else:
                # Fallback: use conda run (may have PATH issues)
                conda_cmd = ["conda", "run", "-n", self.conda_env_name] + cmd
                logger.info(f"Running via conda run (fallback): {cmd}")
                _run_checked_with_timeout(
                    conda_cmd,
                    cwd=work_dir,
                    timeout=_SCIP_PYTHON_INDEX_TIMEOUT_S,
                )
            return True
        except subprocess.TimeoutExpired as e:
            logger.error(
                f"scip-python timed out after {e.timeout}s (cmd: {e.cmd}). "
                "Aborting instead of hanging the job."
            )
            return False
        except subprocess.CalledProcessError as e:
            logger.error(f"Error running command in conda environment: {e}")
            return False
