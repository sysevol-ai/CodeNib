#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""SCIP indexer for Python projects using scip-python."""
import hashlib
import json
import math
import os
import shutil
import signal
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List, Optional, Union

from ..log_utils import get_logger
from ..profiler import Profiler
from .scip_indexer_base import SCIPIndexerBase

logger = get_logger("scip_python_indexer")

# Upper bounds (seconds) that turn a hung child process into a fast, clear
# failure instead of letting it run until the CI job's wall-clock timeout.
# scip-python indexes sympy in a few minutes, so these are generous and only
# fire on a genuine stall. They must stay comfortably below the integration-
# serial job's timeout-minutes: the final test reaches scip-python ~26 min in,
# so a too-high bound (e.g. 20 min) can never fire before the 45-min job cap
# kills the whole job — defeating the point of the timeout.
_SCIP_PYTHON_INDEX_TIMEOUT_S = 600  # scip-python (Node) index run
_SCIP_PYTHON_INDEX_TIMEOUT_ENV = "CODENIB_SCIP_PYTHON_INDEX_TIMEOUT_SECONDS"
_CONDA_ENV_CREATE_TIMEOUT_S = 600  # fallback `conda env create`
_SCIP_PYTHON_MAX_OLD_SPACE_MB = 16384


def _scip_python_index_timeout() -> float:
    raw = os.environ.get(_SCIP_PYTHON_INDEX_TIMEOUT_ENV)
    if raw is None:
        return float(_SCIP_PYTHON_INDEX_TIMEOUT_S)
    try:
        timeout = float(raw)
    except ValueError:
        timeout = 0.0
    if not math.isfinite(timeout) or timeout <= 0:
        logger.warning(
            "Ignoring invalid %s=%r; using %ss",
            _SCIP_PYTHON_INDEX_TIMEOUT_ENV,
            raw,
            _SCIP_PYTHON_INDEX_TIMEOUT_S,
        )
        return float(_SCIP_PYTHON_INDEX_TIMEOUT_S)
    return timeout


def _read_varint(handle) -> Optional[int]:
    value = shift = 0
    while True:
        byte = handle.read(1)
        if not byte:
            return None
        value |= (byte[0] & 0x7F) << shift
        if byte[0] < 0x80:
            return value
        shift += 7


def _scip_index_has_documents(path: Path) -> bool:
    """Return whether a SCIP index holds at least one complete document.

    Every top-level ``Index`` field is length-delimited, so this reads field
    headers and skips their payloads instead of parsing a large cached index.
    """

    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            while (tag := _read_varint(handle)) is not None:
                length = _read_varint(handle)
                if tag & 0x7 != 2 or length is None:
                    return False
                end = handle.tell() + length
                if end > size:
                    return False
                if tag >> 3 == 2:
                    return True
                handle.seek(end)
    except OSError:
        return False
    return False


def _run_checked_with_timeout(cmd, *, timeout, **popen_kwargs):
    """Like ``subprocess.run(cmd, check=True, timeout=...)`` but kills the whole
    process group on timeout or caller cancellation.

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
                # The process group may already have exited between timeout and kill.
                # In that race, there is nothing left to terminate.
                logger.debug("Process group %s already exited before SIGKILL", proc.pid)
            proc.communicate()
            raise
        except BaseException:
            # A benchmark cancellation must not orphan the Node indexer. The
            # next run could otherwise race the old process for the same
            # output and temporary repository configuration.
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                logger.debug(
                    "Process group %s already exited during cancellation",
                    proc.pid,
                )
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

    Uses a ``scip-python`` executable already available on ``PATH`` when
    possible. The historical managed Conda environment remains a fallback for
    development and CI environments that do not expose the tool directly.
    """

    supports_partial_index = True

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
            output_dir: Directory to store output files
            exclude_patterns: List of patterns to exclude from indexing
            profiler: Profiler instance for performance tracking
            decoder_backend: ``"serial"`` (default) or ``"core"``.
        """
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
        self._direct_indexer_path: Optional[str] = None
        self.index_generation_report: Optional[dict] = None

    def _check_indexer_available(self) -> bool:
        """
        Check for a directly installed scip-python before the Conda fallback.

        Returns:
            bool: True if the environment is available, False otherwise
        """
        self._direct_indexer_path = shutil.which("scip-python")
        if self._direct_indexer_path:
            logger.info("Using scip-python from %s", self._direct_indexer_path)
            return True
        return self._ensure_conda_env()

    def _build_index_command(
        self,
        cwd: Optional[str] = None,
        project_name: Optional[str] = None,
        target_dir: Optional[str] = None,
        output_file: Optional[Path] = None,
        **kwargs,
    ) -> List[str]:
        """
        Build the command to generate the SCIP index for Python.

        Args:
            cwd: Working directory to run the index command
            project_name: Project name to use in the index
            target_dir: Optional subdirectory to target for indexing
            output_file: Where scip-python writes (defaults to ``index_file``)
            **kwargs: Additional arguments (ignored)

        Returns:
            List[str]: Command as list of strings
        """
        cmd = [self._direct_indexer_path or "scip-python", "index"]

        if cwd:
            cmd.append("--cwd")
            cmd.append(str(Path(cwd).resolve()))

        if project_name:
            cmd.extend(["--project-name", project_name])
        else:
            cmd.extend(["--project-name", self.project_root.name])

        cmd.extend(["--output", str(output_file or self.index_file)])

        if self._direct_indexer_path:
            # Repository-context serving does not expose navigation into the
            # caller's Python environment. Supplying an explicit empty external
            # package inventory avoids scip-python enumerating every package in
            # a large global/Conda environment, which is slow, unreproducible,
            # and can exceed old scip-python subprocess buffers.
            environment_file = self.output_dir / "python_environment.json"
            environment_file.write_text("[]\n", encoding="utf-8")
            cmd.extend(["--environment", str(environment_file)])

        if target_dir:
            cmd.extend(["--target-only", target_dir])

        return cmd

    @property
    def _pending_index_file(self) -> Path:
        """Where scip-python writes until the run has ended.

        scip-python writes index metadata first and then each document as it
        is emitted, so a run killed from outside (job timeout, OOM, SIGKILL)
        would otherwise leave a truncated ``index.scip`` for the cache paths.
        """
        return self.index_file.with_name(self.index_file.name + ".partial")

    def _discard_index_without_documents(self) -> None:
        """Drop a document-free ``index.scip`` and every cache built from it.

        Before publication was atomic, an interrupted run left its
        metadata-only prefix at ``index.scip``; the cache paths then reused it,
        and the graph decoded from it, as an empty graph without an error.
        """

        if not self.index_file.is_file() or _scip_index_has_documents(self.index_file):
            return
        logger.warning(
            "Discarding SCIP index without documents at %s and its derived caches",
            self.index_file,
        )
        for path in (
            self.index_file,
            self.decoded_file,
            self.graph_file,
            self.lsp_index_file,
        ):
            path.unlink(missing_ok=True)

    def _has_project_pyright_config(self) -> bool:
        """Return whether the project already owns Pyright configuration."""

        if (self.project_root / "pyrightconfig.json").is_file():
            return True
        pyproject = self.project_root / "pyproject.toml"
        try:
            return any(
                line.strip() == "[tool.pyright]"
                for line in pyproject.read_text(encoding="utf-8").splitlines()
            )
        except OSError:
            return False

    @contextmanager
    def _temporary_exclude_config(self) -> Iterator[None]:
        """Apply CodeNib exclusions to scip-python without persisting config.

        scip-python has no exclude flag, but it delegates project discovery to
        Pyright. When the repository does not already own Pyright
        configuration, an exact temporary ``pyrightconfig.json`` prevents
        generated and vendored trees from entering the expensive analysis.
        Existing project configuration always wins.
        """

        if not self.exclude_patterns or self._has_project_pyright_config():
            yield
            return

        path = self.project_root / "pyrightconfig.json"
        payload = (
            json.dumps(
                {"exclude": sorted(dict.fromkeys(self.exclude_patterns))},
                indent=2,
            )
            + "\n"
        )
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            yield
            return
        except OSError as exc:
            logger.warning("Could not apply temporary Pyright excludes: %s", exc)
            yield
            return

        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            logger.info(
                "Applying %d temporary Pyright exclusions",
                len(self.exclude_patterns),
            )
            yield
        finally:
            try:
                if path.read_text(encoding="utf-8") == payload:
                    path.unlink()
            except OSError:
                logger.warning("Could not remove temporary Pyright config at %s", path)

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
        allow_partial_index: bool = False,
        **kwargs,
    ) -> bool:
        """
        Generate SCIP index for the Python project.

        Uses the resolved PATH executable, with Conda as a compatibility
        fallback.

        Args:
            cwd: Working directory (defaults to project_root)
            project_name: Project name to use in the index
            target_dir: Optional subdirectory to target for indexing
            allow_partial_index: Preserve a parseable compiler prefix for
                downstream source-coverage repair.
            **kwargs: Additional arguments (ignored)

        Returns:
            bool: True if index generation was successful, False otherwise
        """
        self.index_generation_report = None
        if self.index_file.exists():
            self.index_file.unlink()
            logger.info("Removed pre-existing SCIP index before generation")
        pending = self._pending_index_file
        pending.unlink(missing_ok=True)
        if not self._check_indexer_available():
            self.index_generation_report = {
                "backend": "scip-python",
                "status": "unavailable",
                "complete": False,
                "partial": False,
                "document_count": 0,
            }
            return False

        cmd = self._build_index_command(
            cwd=cwd or str(self.project_root),
            project_name=project_name,
            target_dir=target_dir,
            output_file=pending,
        )

        logger.debug(f"Running command: {' '.join(cmd)}")

        with self.profiler.section("generate_index") as section:
            with self._temporary_exclude_config():
                if self._direct_indexer_path:
                    success = self._run_direct(cmd, self.project_root)
                else:
                    success = self._run_in_conda_env(cmd, self.project_root)
        duration = section.duration

        if success:
            if pending.exists():
                os.replace(pending, self.index_file)
            document_count = self._usable_index_document_count()
            self.index_generation_report = {
                "backend": "scip-python",
                "status": "complete",
                "complete": True,
                "partial": False,
                "document_count": document_count,
            }
            logger.info(f"Successfully generated SCIP index at {self.index_file}")
            logger.info(f"Index generation took: {duration:.2f} seconds")
            return True
        else:
            logger.error(f"Index generation failed after {duration:.2f} seconds")
            document_count = self._usable_index_document_count(pending)
            if allow_partial_index and document_count > 0:
                os.replace(pending, self.index_file)
                self.index_generation_report = {
                    "backend": "scip-python",
                    "status": "partial",
                    "complete": False,
                    "partial": True,
                    "document_count": document_count,
                }
                logger.warning(
                    "Preserving parseable partial SCIP index with %d documents",
                    document_count,
                )
                return False
            # Never let the generic pipeline mistake an unvalidated partial file
            # for a complete graph unless the caller explicitly opted into repair.
            if pending.exists():
                pending.unlink()
                logger.info("Removed partial index file")
            self.index_generation_report = {
                "backend": "scip-python",
                "status": "failed",
                "complete": False,
                "partial": False,
                "document_count": document_count,
            }
            return False

    def _usable_index_document_count(self, path: Optional[Path] = None) -> int:
        """Count path-addressable documents in a parseable SCIP artifact."""

        path = path or self.index_file
        if not path.is_file():
            return 0
        try:
            from google.protobuf.message import DecodeError

            from .scip_pb2 import Index

            index = Index()
            index.ParseFromString(path.read_bytes())
        except (OSError, DecodeError, ValueError):
            return 0
        return sum(bool(document.relative_path) for document in index.documents)

    def process_index(self, output_file: Optional[str] = None):
        """Recover identities before serial/core share the decoded input.

        The compiler owns publication of the output directory. Do not replace
        a decoded input that changed during recovery; the derived-file publish
        point is the final atomic replace, before the shared consumer seals it.
        """

        from google.protobuf.text_format import MessageToString, Parse

        from .python_identity import normalize_python_index
        from .scip_pb2 import Index

        self.decoded_input_receipt = None
        self.graph_output_receipt = None
        temporary = None
        try:
            from ..compiler.artifact_fingerprints import regular_file_fingerprint

            before = regular_file_fingerprint(self.decoded_file)
            payload = self.decoded_file.read_bytes()
            if before != {
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }:
                raise RuntimeError(
                    "decoded SCIP input changed before identity recovery"
                )
            index = Parse(payload.decode("utf-8"), Index())
            del payload
            with self.profiler.section("process_index.python_identity"):
                report = normalize_python_index(
                    index,
                    self.project_root,
                    path_allowed=self._path_allowed,
                )
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.decoded_file.parent,
                prefix=".python-identity-",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(MessageToString(index, as_utf8=True))
            if regular_file_fingerprint(self.decoded_file) != before:
                raise RuntimeError(
                    "decoded SCIP input changed during identity recovery"
                )
            os.replace(temporary, self.decoded_file)
            if self.index_generation_report is not None:
                self.index_generation_report["identity_recovery"] = {
                    "status": "complete",
                    **report,
                }
        except Exception as exc:
            logger.error("Python SCIP identity recovery failed: %s", exc)
            if self.index_generation_report is not None:
                self.index_generation_report["identity_recovery"] = {
                    "status": "failed",
                    "error": str(exc),
                }
            return None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return super().process_index(output_file)

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

        if skip_level is not None:
            self._discard_index_without_documents()
        return super().run_pipeline(
            output_file=output_file,
            skip_level=skip_level,
            reset_profiler=reset_profiler,
            report_profile=report_profile,
            **kwargs,
        )

    # ── Conda environment helpers ──────────────────────────────────────

    @staticmethod
    def _subprocess_env() -> dict[str, str]:
        env = os.environ.copy()
        node_opts = env.get("NODE_OPTIONS", "")
        if "--max-old-space-size" not in node_opts:
            env["NODE_OPTIONS"] = (
                node_opts + f" --max-old-space-size={_SCIP_PYTHON_MAX_OLD_SPACE_MB}"
            ).strip()
        return env

    def _run_direct(self, cmd: list, cwd: Optional[Union[str, Path]] = None) -> bool:
        """Run an explicitly resolved scip-python without creating an environment."""

        work_dir = Path(cwd if cwd else self.project_root).resolve()
        try:
            _run_checked_with_timeout(
                cmd,
                cwd=work_dir,
                env=self._subprocess_env(),
                timeout=_scip_python_index_timeout(),
            )
            return True
        except subprocess.TimeoutExpired as exc:
            logger.error(
                "scip-python timed out after %ss (cmd: %s)",
                exc.timeout,
                exc.cmd,
            )
            return False
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            logger.error("Error running scip-python: %s", exc)
            return False

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
                env = self._subprocess_env()
                env["PATH"] = f"{scip_bin}:{env.get('PATH', '')}"
                logger.info(f"Running with scip-env PATH ({scip_bin}): {cmd}")
                _run_checked_with_timeout(
                    cmd,
                    cwd=work_dir,
                    env=env,
                    timeout=_scip_python_index_timeout(),
                )
            else:
                # Fallback: use conda run (may have PATH issues)
                conda_cmd = ["conda", "run", "-n", self.conda_env_name] + cmd
                logger.info(f"Running via conda run (fallback): {cmd}")
                _run_checked_with_timeout(
                    conda_cmd,
                    cwd=work_dir,
                    timeout=_scip_python_index_timeout(),
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
