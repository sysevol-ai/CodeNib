#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""SCIP indexer for Python projects using scip-python."""

import hashlib
import json
import math
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List, Optional, Union

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib

from .._compilation_process import run_compilation_process
from ..log_utils import get_logger
from ..profiler import Profiler
from .pyrefly_provider import (
    PYREFLY_PROVIDER,
    SCIP_PYTHON_PROVIDER,
    select_python_graph_provider,
)
from .scip_indexer_base import SCIPIndexerBase, index_text

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


def _pyrefly_identity_recovery() -> bool:
    """Whether to rerun ``normalize_python_index`` on pyrefly output (diagnostic)."""

    return os.environ.get("CODENIB_PYREFLY_IDENTITY_RECOVERY", "") == "1"


def _scip_tool_name(path: Path) -> str:
    """Return the indexer named in a SCIP file's metadata, reading only its head.

    Recovery depends on which tool wrote the index, not on the configured
    provider: a fallback or a direct scip-python run produces scip-python
    output under the default setting. Serializers write ``metadata`` (field 1)
    first, so the head of the file holds it.
    """

    from google.protobuf.message import DecodeError

    from .scip_pb2 import Metadata

    try:
        with open(path, "rb") as stream:
            head = stream.read(64 * 1024)
    except OSError:
        return ""
    if not head or head[0] != 0x0A:
        return ""
    length, shift, position = 0, 0, 1
    while position < len(head) and shift < 64:
        byte = head[position]
        position += 1
        length |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            break
    else:
        return ""
    if position + length > len(head):
        return ""
    metadata = Metadata()
    try:
        metadata.ParseFromString(head[position : position + length])
    except DecodeError:
        return ""
    return metadata.tool_info.name


_MIRRORED_SUFFIXES = frozenset({".py", ".pyi"})
_MIRRORED_NAMES = frozenset({"py.typed"})


def _mirror_python_sources(source: Path, target: Path) -> int:
    """Copy the files Pyright analyzes into *target*, keeping relative paths.

    Files are copied rather than hard-linked: a link changes the original's
    link count and change time, which source verification treats as an edit.
    Symlinked directories are followed once, as Pyright follows them, and
    ``.git`` is skipped. Returns the number of mirrored files.
    """

    count = 0
    for directory, names, files in os.walk(source, followlinks=True):
        relative = Path(directory).relative_to(source)
        real = os.path.realpath(directory)
        ancestors = {
            os.path.realpath(source.joinpath(*relative.parts[:depth]))
            for depth in range(len(relative.parts))
        }
        if real in ancestors:
            names[:] = []
            continue
        names[:] = [name for name in names if name != ".git"]
        for name in files:
            if Path(name).suffix not in _MIRRORED_SUFFIXES and (
                name not in _MIRRORED_NAMES
            ):
                continue
            origin = Path(directory) / name
            if not origin.is_file():
                continue
            destination = target / relative / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(origin, destination)
            count += 1
    target.mkdir(parents=True, exist_ok=True)
    return count


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
    return run_compilation_process(cmd, timeout=timeout, **popen_kwargs)


@contextmanager
def _stdlib_only_python_shims(
    shim_parent: Optional[Path] = None,
) -> Iterator[Optional[str]]:
    """Yield a directory whose ``python3``/``python`` see only the stdlib.

    Pyright, and the basedpyright server built on it, ask ``python3`` (then
    ``python``) on ``PATH`` for their import search paths. Inheriting the
    caller's environment lets them type-evaluate the repository against
    whatever that environment installs; on scikit-learn, current numpy/scipy
    stubs turn a 36 s index into one that does not finish in 25 minutes. The
    shims run the caller's own interpreter with ``-I -S``, so the probe reports
    the standard library only, like the dependency-free interpreter of the
    managed Conda fallback. Prepend the directory to ``PATH``; ``None`` means
    no interpreter path is known and the environment is left as it is.
    """

    if not sys.executable:
        yield None
        return
    script = f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -I -S "$@"\n'
    with tempfile.TemporaryDirectory(
        prefix="python-shims-", dir=shim_parent
    ) as shim_dir:
        for name in ("python3", "python"):
            shim = Path(shim_dir) / name
            shim.write_text(script, encoding="utf-8")
            shim.chmod(0o755)
        yield shim_dir


@contextmanager
def _stdlib_only_python(
    env: dict[str, str], shim_parent: Path
) -> Iterator[dict[str, str]]:
    """Return ``env`` with stdlib-only Python shims ahead of ``PATH``."""

    with _stdlib_only_python_shims(shim_parent) as shim_dir:
        if shim_dir is None:
            yield env
            return
        isolated = dict(env)
        isolated["PATH"] = os.pathsep.join(
            part for part in (shim_dir, env.get("PATH")) if part
        )
        yield isolated


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
        # pyrefly by default; scip-python when pyrefly is missing or pinned.
        self.graph_provider_selection = select_python_graph_provider()
        self.graph_provider = self.graph_provider_selection.name

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
    def _indexing_root(self) -> Iterator[Path]:
        """Yield the directory scip-python runs in, never writing the checkout.

        scip-python has no exclude flag; it reads Pyright configuration only
        from its working directory. When the repository does not own Pyright
        configuration, CodeNib's exclusions go into a private mirror of the
        project's Python files, so generated and vendored trees stay out of
        the expensive analysis while the checkout and its Git status are left
        untouched (a concurrent source check sees no temporary file). SCIP
        document paths are relative to the root, and the caller pins the
        project version to the checkout's commit. Existing project
        configuration always wins and runs in place.
        """

        if not self.exclude_patterns or self._has_project_pyright_config():
            yield self.project_root
            return

        payload = (
            json.dumps(
                {"exclude": sorted(dict.fromkeys(self.exclude_patterns))},
                indent=2,
            )
            + "\n"
        )
        with tempfile.TemporaryDirectory(prefix="codenib-scip-python-") as scratch:
            root = Path(scratch) / (self.project_root.name or "project")
            with self.profiler.section("generate_index.mirror"):
                count = _mirror_python_sources(self.project_root, root)
            (root / "pyrightconfig.json").write_text(payload, encoding="utf-8")
            logger.info(
                "Applying %d Pyright exclusions in a private mirror of %d files",
                len(self.exclude_patterns),
                count,
            )
            yield root

    def _inferred_project_version(self) -> Optional[str]:
        """Return the project version scip-python infers for the checkout.

        scip-python reads ``[project].version``, then ``[tool.poetry].version``,
        from the nearest ``pyproject.toml`` at or above the project root, and
        otherwise uses ``git rev-parse HEAD`` in its working directory. The
        version is part of every symbol, and a mirror has neither file, so
        the caller passes this value explicitly.
        """

        for directory in (self.project_root, *self.project_root.parents):
            candidate = directory / "pyproject.toml"
            if not candidate.is_file():
                continue
            try:
                data = tomllib.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
                data = {}
            project = data.get("project")
            version = project.get("version") if isinstance(project, dict) else None
            if version is None:
                tool = data.get("tool")
                poetry = tool.get("poetry") if isinstance(tool, dict) else None
                version = poetry.get("version") if isinstance(poetry, dict) else None
            if isinstance(version, str) and version:
                return version
            break
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self.project_root,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        version = result.stdout.strip()
        return version if result.returncode == 0 and version else None

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
        selection = self.graph_provider_selection
        fallback = None
        if self.graph_provider == PYREFLY_PROVIDER:
            if self._generate_pyrefly_index(
                selection.executable,
                note=selection.note,
                project_name=project_name,
                target_dir=target_dir,
            ):
                return True
            if selection.configured:
                return False
            # pyrefly was chosen by default; scip-python can still build it.
            fallback = {
                "provider": PYREFLY_PROVIDER,
                "reason": f"pyrefly {self.index_generation_report['status']}",
            }
            logger.warning("pyrefly built no index; falling back to scip-python")
            self.graph_provider = SCIP_PYTHON_PROVIDER
        elif selection.note:
            fallback = {"provider": PYREFLY_PROVIDER, "reason": selection.note}
        success = self._generate_scip_python_index(
            cwd=cwd,
            project_name=project_name,
            target_dir=target_dir,
            allow_partial_index=allow_partial_index,
        )
        if fallback is not None:
            self.index_generation_report["fallback_from"] = fallback
        return success

    def _generate_scip_python_index(
        self,
        *,
        cwd: Optional[str],
        project_name: Optional[str],
        target_dir: Optional[str],
        allow_partial_index: bool,
    ) -> bool:
        pending = self._pending_index_file
        if not self._check_indexer_available():
            self.index_generation_report = {
                "backend": "scip-python",
                "status": "unavailable",
                "complete": False,
                "partial": False,
                "document_count": 0,
            }
            return False

        with self.profiler.section("generate_index") as section:
            with self._indexing_root() as root:
                mirrored = root != self.project_root
                cmd = self._build_index_command(
                    cwd=str(root) if mirrored or not cwd else cwd,
                    project_name=project_name,
                    target_dir=target_dir,
                    output_file=pending,
                )
                if mirrored:
                    version = self._inferred_project_version()
                    if version:
                        cmd.extend(["--project-version", version])
                logger.debug(f"Running command: {' '.join(cmd)}")
                if self._direct_indexer_path:
                    success = self._run_direct(cmd, root)
                else:
                    success = self._run_in_conda_env(cmd, root)
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

    def _generate_pyrefly_index(
        self,
        executable: Optional[str],
        *,
        note: str = "",
        project_name: Optional[str] = None,
        target_dir: Optional[str] = None,
    ) -> bool:
        """Build ``index.scip`` from a pyrefly Glean report.

        pyrefly checks a private config naming every Python file under the
        root, with CodeNib's exclusions, against a standard-library-only
        interpreter like scip-python's. Its report is converted into a
        scip-python-shaped index; nothing in the checkout is written.
        """

        from .pyrefly_provider import pyrefly_version, write_pyrefly_config

        report = {
            "backend": PYREFLY_PROVIDER,
            "status": "unavailable",
            "complete": False,
            "partial": False,
            "document_count": 0,
        }
        self.index_generation_report = report
        if executable is None:
            report["detail"] = note
            logger.error("pyrefly is unavailable: %s", note)
            return False
        version = pyrefly_version(executable)
        report["provider_version"] = version
        glean_dir = self.output_dir / "pyrefly-glean"
        shutil.rmtree(glean_dir, ignore_errors=True)
        config = write_pyrefly_config(
            self.output_dir / "pyrefly.toml",
            self.project_root,
            exclude_patterns=self.exclude_patterns,
            target_dir=target_dir,
        )
        try:
            return self._run_pyrefly(
                executable, version, config, glean_dir, report, project_name
            )
        finally:
            # The report reaches 150 MB on large repositories; keep only the index.
            shutil.rmtree(glean_dir, ignore_errors=True)

    def _run_pyrefly(
        self,
        executable: str,
        version: str,
        config: Path,
        glean_dir: Path,
        report: dict,
        project_name: Optional[str],
    ) -> bool:
        from .pyrefly_glean import glean_to_scip_index
        from .pyrefly_provider import pyrefly_command

        pending = self._pending_index_file
        with self.profiler.section("generate_index") as section:
            with self.profiler.section("generate_index.pyrefly") as run:
                with _stdlib_only_python_shims(self.output_dir) as shim_dir:
                    command = pyrefly_command(
                        executable,
                        config,
                        glean_dir,
                        interpreter=(
                            str(Path(shim_dir) / "python3") if shim_dir else None
                        ),
                    )
                    logger.debug("Running command: %s", " ".join(command))
                    try:
                        result = _run_checked_with_timeout(
                            command,
                            cwd=self.project_root,
                            timeout=_scip_python_index_timeout(),
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                        )
                        returncode = result.returncode
                    except subprocess.CalledProcessError as exc:
                        # Type errors exit 1; the report is still complete.
                        returncode = exc.returncode
                        if exc.stderr:
                            logger.debug("pyrefly stderr: %s", exc.stderr[-2000:])
                    except (subprocess.TimeoutExpired, OSError) as exc:
                        logger.error("pyrefly failed: %s", exc)
                        report["status"] = "failed"
                        return False
            report["pyrefly_seconds"] = round(run.duration, 3)
            report["pyrefly_returncode"] = returncode
            if returncode not in (0, 1) or not glean_dir.is_dir():
                logger.error("pyrefly exited %s without a Glean report", returncode)
                report["status"] = "failed"
                return False
            with self.profiler.section("generate_index.convert") as convert:
                version_label = self._inferred_project_version() or "0"
                try:
                    index, stats = glean_to_scip_index(
                        glean_dir,
                        self.project_root,
                        project_name=project_name or self.project_root.name,
                        project_version=version_label,
                        tool_version=version,
                        path_allowed=self._path_allowed,
                    )
                except (
                    Exception
                ) as exc:  # noqa: BLE001 - report it; caller may fall back
                    logger.error("pyrefly report conversion failed: %s", exc)
                    report["status"] = "failed"
                    report["detail"] = f"conversion failed: {exc}"
                    return False
                pending.write_bytes(index.SerializeToString())
                os.replace(pending, self.index_file)
            report["convert_seconds"] = round(convert.duration, 3)
        report.update(
            {
                "status": "complete",
                "complete": True,
                "document_count": len(index.documents),
                "conversion": stats,
                "generate_seconds": round(section.duration, 3),
            }
        )
        logger.info(
            "pyrefly index: %d documents in %.2fs (pyrefly %.2fs, convert %.2fs)",
            len(index.documents),
            section.duration,
            run.duration,
            convert.duration,
        )
        return True

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

        from .lsp_occurrence_index import SCIPOccurrenceIndex
        from .python_identity import normalize_python_index
        from .scip_decode_view import text_blocks_match

        self.decoded_input_receipt = None
        self.graph_output_receipt = None
        if (
            _scip_tool_name(self.index_file) == PYREFLY_PROVIDER
            and not _pyrefly_identity_recovery()
        ):
            # pyrefly qualifies nested definitions with ``<locals>`` and the
            # converter marks same-scope redefinitions, so the index already
            # carries the identities recovery would restore.
            if self.index_generation_report is not None:
                self.index_generation_report["identity_recovery"] = {
                    "status": "skipped",
                    "reason": "pyrefly provides physical identities",
                }
            # Read occurrences and the graph from the binary index this run
            # decoded instead of scanning the decoded text again; the text
            # still feeds the native fact query unchanged.
            index, fingerprint = self._decoded_scip_index()
            occurrence_index, fingerprint = self._occurrences_from_binary_index(
                index, fingerprint
            )
            return self._process_decoded_index(
                output_file,
                occurrence_index=occurrence_index,
                decoded_fingerprint=fingerprint,
                scip_index=self._graph_scip_index(index),
            )
        temporary = None
        graph_index = None
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
            with self.profiler.section("process_index.read_python_identity_index"):
                index = self._read_identity_index(payload)
            del payload
            with self.profiler.section("process_index.python_identity"):
                report = normalize_python_index(
                    index,
                    self.project_root,
                    path_allowed=self._path_allowed,
                )
            text = index_text(index)
            # The graph decoder reads the normalized index when it may.
            graph_index = index if text_blocks_match(index, text) else None
            encoded = text.encode("utf-8")
            del text
            normalized_fingerprint = {
                "size": len(encoded),
                "sha256": hashlib.sha256(encoded).hexdigest(),
            }
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=self.decoded_file.parent,
                prefix=".python-identity-",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(encoded)
            del encoded
            with self.profiler.section("process_index.prepare_python_occurrences"):
                occurrence_index = SCIPOccurrenceIndex.from_scip_index(
                    index,
                    path_filter=(
                        self._path_allowed
                        if self._target_dir or self.exclude_patterns
                        else None
                    ),
                )
            del index
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
        return self._process_decoded_index(
            output_file,
            occurrence_index=occurrence_index,
            decoded_fingerprint=normalized_fingerprint,
            scip_index=graph_index,
        )

    def _read_identity_index(self, decoded: bytes):
        """Reuse binary structure only when it exactly reproduces the input.

        The optional raw artifact is a candidate, never authority over a
        cached/edited decoded file. The equality check also covers all metadata
        and text escaping. Nothing is cached across calls or source versions.
        """
        from google.protobuf.message import DecodeError
        from google.protobuf.text_format import Parse

        from .scip_pb2 import Index

        if self.index_file.is_file() and not self.index_file.is_symlink():
            try:
                index = Index.FromString(self.index_file.read_bytes())
                if index_text(index).encode("utf-8") == decoded:
                    return index
                del index
            except (OSError, DecodeError, ValueError):
                logger.debug("Raw SCIP candidate unavailable; parsing decoded input")
        return Parse(decoded.decode("utf-8"), Index())

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
        self.output_dir.mkdir(parents=True, exist_ok=True)
        try:
            with _stdlib_only_python(self._subprocess_env(), self.output_dir) as env:
                _run_checked_with_timeout(
                    cmd,
                    cwd=work_dir,
                    env=env,
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
