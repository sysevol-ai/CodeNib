#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""SCIP-backed PHP graph routes using scip-php."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional, Union

from ..languages import scip_cold_start_command_for_language
from ..log_utils import get_logger
from ..paths import USER_STATE_DIRNAME, temp_state_dir
from ..profiler import Profiler
from .scip_indexer_base import SCIPIndexerBase

if TYPE_CHECKING:
    from ..ls_index.lsp_indexer import GenericLSPIndexer

logger = get_logger("scip_php_indexer")

_DEFAULT_COMPOSER_IMAGE = "composer:2"
_DEFAULT_SCIP_PHP_PACKAGE = "davidrjenni/scip-php:0.0.2"


class PHPHybridIndexer:
    """Active PHP graph route: SCIP for Composer projects, LSP fallback otherwise."""

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
            else temp_state_dir() / self.project_root.name
        )
        self.exclude_patterns = exclude_patterns or []
        self.profiler = profiler
        self.decoder_backend = decoder_backend
        self._delegate = self._preferred_delegate()
        self._sync_delegate_attrs()

    def _preferred_delegate(self):
        if (self.project_root / "composer.json").exists():
            return self._scip_delegate()
        return self._lsp_delegate()

    def _scip_delegate(self) -> "SCIPPHPIndexer":
        return SCIPPHPIndexer(
            self.project_root,
            output_dir=self.output_dir,
            exclude_patterns=self.exclude_patterns,
            profiler=self.profiler,
            decoder_backend=self.decoder_backend,
        )

    def _lsp_delegate(self) -> GenericLSPIndexer:
        from ..ls_index.lsp_indexer import GenericLSPIndexer

        return GenericLSPIndexer(
            self.project_root,
            output_dir=self.output_dir,
            exclude_patterns=self.exclude_patterns,
            profiler=self.profiler,
            language="php",
        )

    def _sync_delegate_attrs(self) -> None:
        self.output_dir = self._delegate.output_dir
        self.index_file = self._delegate.index_file
        self.decoded_file = self._delegate.decoded_file
        self.graph_file = self._delegate.graph_file
        self.profiler = self._delegate.profiler

    def _switch_to_lsp(self) -> None:
        logger.warning("Falling back to PHP LSP graph route for %s", self.project_root)
        self._delegate = self._lsp_delegate()
        self._sync_delegate_attrs()

    def generate_index(self, **kwargs) -> bool:
        try:
            if self._delegate.generate_index(**kwargs):
                return True
        except Exception as exc:
            if not isinstance(self._delegate, SCIPPHPIndexer):
                raise
            logger.warning(
                "PHP SCIP index generation failed for %s: %s",
                self.project_root,
                exc,
            )
        if isinstance(self._delegate, SCIPPHPIndexer):
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
            if not isinstance(self._delegate, SCIPPHPIndexer):
                raise
            logger.warning(
                "PHP SCIP graph route failed for %s: %s",
                self.project_root,
                exc,
            )
            graph = None
        if graph is not None or not isinstance(self._delegate, SCIPPHPIndexer):
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


class SCIPPHPIndexer(SCIPIndexerBase):
    """Pure scip-php route used by candidate gates and the active hybrid route."""

    def __init__(
        self,
        project_root: Union[str, Path],
        output_dir: Optional[Union[str, Path]] = None,
        exclude_patterns: Optional[List] = None,
        profiler: Optional[Profiler] = None,
        decoder_backend: Optional[str] = None,
    ):
        self._index_via_docker = False
        self._index_root: Path | None = None
        super().__init__(
            project_root=project_root,
            output_dir=output_dir,
            exclude_patterns=exclude_patterns,
            profiler=profiler,
            language="php",
            decoder_backend=decoder_backend,
        )

    def _check_indexer_available(self) -> bool:
        command = scip_cold_start_command_for_language("php")
        if not command:
            logger.error("No SCIP cold-start command registered for PHP")
            return False
        if not self._project_prerequisites_ok():
            return False
        self._index_via_docker = False
        if self._local_tooling_available(command):
            return True
        if self._docker_tooling_available(command):
            self._index_via_docker = True
            return True

        if shutil.which("php") is None:
            logger.error(
                "php is not available and Docker fallback is unavailable. Run "
                "make scip-php-system-deps-ubuntu or make scip-php-docker-tool."
            )
            return False
        logger.error(
            "PHP SCIP command %r was not available. Install Composer/PHP so "
            "the pure SCIP route can prepare an output-local worktree, run "
            "make scip-php-docker-tool for Docker fallback, or preinstall "
            "project-local vendor/bin/scip-php in a disposable checkout.",
            command[0],
        )
        return False

    def _local_tooling_available(self, command: list[str]) -> bool:
        if shutil.which("php") is None:
            return False
        if self._is_project_local_command(command):
            return (
                self._project_local_scip_php_ready(self.project_root)
                or self._composer_command() is not None
            )
        executable = self.project_root / command[0] if "/" in command[0] else None
        if executable is None and shutil.which(command[0]) is None:
            return False
        if executable is not None and not executable.exists():
            return False
        return True

    def _docker_tooling_available(self, command: list[str]) -> bool:
        return (
            self._is_project_local_command(command)
            and shutil.which("docker") is not None
        )

    def _is_project_local_command(self, command: list[str]) -> bool:
        return (
            len(command) == 1
            and "/" in command[0]
            and Path(command[0]).name == "scip-php"
        )

    def _build_index_command(self, **kwargs) -> list[str]:
        command = list(scip_cold_start_command_for_language("php") or [])
        if self._index_via_docker and command:
            return self._php_docker_command(command[0])
        return command

    def _get_decoder_class(self):
        from .scip_decode_php import SCIPPHPGraphDecoder

        return SCIPPHPGraphDecoder

    def generate_index(self, timeout: Optional[int] = None, **kwargs) -> bool:
        if not self._check_indexer_available():
            return False

        try:
            index_root = self._prepare_index_root()
        except RuntimeError as exc:
            logger.error("Failed to prepare PHP SCIP worktree: %s", exc)
            return False
        command = self._build_index_command(**kwargs)
        if not command:
            return False
        project_index = index_root / "index.scip"
        project_index.unlink(missing_ok=True)
        with self.profiler.section("generate_index") as section:
            try:
                subprocess.run(
                    command,
                    check=True,
                    cwd=index_root,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    env=os.environ.copy(),
                )
            except subprocess.TimeoutExpired as exc:
                logger.error("PHP SCIP indexing timed out: %s", exc)
                return False
            except subprocess.CalledProcessError as exc:
                logger.error("Error generating PHP SCIP index: %s", exc)
                detail = _combine_command_output(exc.stdout, exc.stderr)
                if detail:
                    logger.error(detail)
                return False

        duration = section.duration
        if not project_index.exists():
            logger.error("scip-php completed but did not create %s", project_index)
            return False

        if project_index != self.index_file:
            self.index_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(project_index), str(self.index_file))

        logger.info("Successfully generated PHP SCIP index at %s", self.index_file)
        logger.info("Index generation took: %.2f seconds", duration)
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
        kwargs.pop("project_name", None)
        return super().run_pipeline(
            output_file=output_file,
            skip_level=skip_level,
            reset_profiler=reset_profiler,
            report_profile=report_profile,
            **kwargs,
        )

    def _project_prerequisites_ok(self) -> bool:
        if not (self.project_root / "composer.json").exists():
            logger.error(
                "scip-php requires a Composer project with composer.json. "
                "The pure SCIP route installs any missing Composer state in a "
                "throwaway copy under the output directory, not in the source repo."
            )
            return False
        return True

    def _prepare_index_root(self) -> Path:
        command = list(scip_cold_start_command_for_language("php") or [])
        if self._index_root is None:
            self._index_root = self.output_dir / "worktree"
        self._copy_project_to_index_root(self._index_root)
        if self._is_project_local_command(command):
            self._prepare_project_local_scip_php(self._index_root)
        return self._index_root

    def _copy_project_to_index_root(self, index_root: Path) -> None:
        if index_root.exists():
            shutil.rmtree(index_root)
        index_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            self.project_root,
            index_root,
            ignore=self._copy_ignore,
            symlinks=True,
        )
        self._ensure_git_reference(index_root)

    def _copy_ignore(self, directory: str, names: list[str]) -> set[str]:
        ignored = {
            ".git",
            USER_STATE_DIRNAME,
            "index.scip",
            "index.decoded",
            "graph.pkl",
        }
        directory_path = Path(directory).resolve()
        output_dir = self.output_dir.resolve()
        for name in names:
            child = (directory_path / name).resolve()
            if child == output_dir or _is_relative_to(output_dir, child):
                ignored.add(name)
        return ignored.intersection(names)

    def _ensure_git_reference(self, index_root: Path) -> None:
        if shutil.which("git") is None:
            logger.warning("git is unavailable; scip-php may fail to derive metadata")
            return
        if (index_root / ".git").exists():
            return
        commands = (
            ["git", "init", "-q"],
            ["git", "config", "user.email", "codenib@example.invalid"],
            ["git", "config", "user.name", "CodeNib"],
            ["git", "add", "-A"],
            ["git", "commit", "-qm", "codenib php scip worktree"],
        )
        for command in commands:
            try:
                subprocess.run(
                    command,
                    check=True,
                    cwd=index_root,
                    capture_output=True,
                    text=True,
                )
            except subprocess.CalledProcessError as exc:
                logger.warning(
                    "Failed to prepare git metadata in PHP SCIP worktree: %s",
                    _combine_command_output(exc.stdout, exc.stderr) or exc,
                )
                return

    def _prepare_project_local_scip_php(self, index_root: Path) -> None:
        if self._project_local_scip_php_ready(index_root):
            return
        if self._index_via_docker:
            self._prepare_project_local_scip_php_with_docker(index_root)
            return

        composer = self._composer_command()
        if composer is None:
            raise RuntimeError("composer is required to prepare project-local scip-php")
        self._run_prepare_command(
            [*composer, "install", "--no-interaction", "--no-progress"],
            cwd=index_root,
        )
        self._run_prepare_command(
            [
                *composer,
                "require",
                "--dev",
                self._scip_php_package(),
                "--no-interaction",
                "--no-progress",
                "--no-security-blocking",
            ],
            cwd=index_root,
        )
        self._run_prepare_command(
            [*composer, "dump-autoload", "-o", "--no-interaction"],
            cwd=index_root,
        )
        self._install_scip_php_package_dependencies(index_root, composer)

    def _prepare_project_local_scip_php_with_docker(self, index_root: Path) -> None:
        install_command = (
            "git config --global --add safe.directory /app && "
            "composer install --no-interaction --no-progress && "
            f"composer require --dev {self._scip_php_package()} "
            "--no-interaction --no-progress --no-security-blocking && "
            "composer dump-autoload -o --no-interaction"
        )
        self._run_prepare_command(
            self._php_docker_command("sh", "-lc", install_command, root=index_root),
            cwd=index_root,
        )
        package_root = index_root / "vendor/davidrjenni/scip-php"
        if package_root.exists():
            self._run_prepare_command(
                self._php_docker_command(
                    "composer",
                    "install",
                    "--no-interaction",
                    "--no-progress",
                    "--no-security-blocking",
                    root=package_root,
                ),
                cwd=package_root,
            )

    def _project_local_scip_php_ready(self, root: Path) -> bool:
        command_path = root / "vendor/bin/scip-php"
        if not command_path.exists():
            return False
        package_root = root / "vendor/davidrjenni/scip-php"
        return (
            not package_root.exists() or (package_root / "vendor/autoload.php").exists()
        )

    def _install_scip_php_package_dependencies(
        self,
        index_root: Path,
        composer: list[str],
    ) -> None:
        package_root = index_root / "vendor/davidrjenni/scip-php"
        if not package_root.exists():
            return
        self._run_prepare_command(
            [
                *composer,
                "install",
                "--no-interaction",
                "--no-progress",
                "--no-security-blocking",
            ],
            cwd=package_root,
        )

    def _run_prepare_command(self, command: list[str], *, cwd: Path) -> None:
        try:
            subprocess.run(
                command,
                check=True,
                cwd=cwd,
                capture_output=True,
                text=True,
                env=os.environ.copy(),
            )
        except subprocess.CalledProcessError as exc:
            detail = _combine_command_output(exc.stdout, exc.stderr)
            raise RuntimeError(
                f"Failed to prepare PHP SCIP worktree with {command[0]!r}: {detail}"
            ) from exc

    def _composer_command(self) -> list[str] | None:
        if shutil.which("composer") is not None:
            return ["composer"]
        composer = os.environ.get("CODENIB_PHP_COMPOSER")
        if composer:
            return [composer]
        tools_dir = os.environ.get("CODENIB_SCIP_TOOLS_DIR")
        if tools_dir:
            candidate = Path(tools_dir) / "composer"
            if candidate.exists():
                return [str(candidate)]
        return None

    def _scip_php_package(self) -> str:
        return os.environ.get("CODENIB_PHP_SCIP_PACKAGE", _DEFAULT_SCIP_PHP_PACKAGE)

    def _php_docker_command_for_root(self, root: Path, *args: str) -> list[str]:
        image = os.environ.get("CODENIB_PHP_COMPOSER_IMAGE", _DEFAULT_COMPOSER_IMAGE)
        return [
            "docker",
            "run",
            "--rm",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "-e",
            "HOME=/tmp",
            "-e",
            "COMPOSER_HOME=/tmp/composer",
            "-v",
            f"{root}:/app",
            "-w",
            "/app",
            image,
            *args,
        ]

    def _php_docker_command(self, *args: str, root: Path | None = None) -> list[str]:
        return self._php_docker_command_for_root(
            root or self._index_root or self.project_root,
            *args,
        )


def _combine_command_output(stdout: str | None, stderr: str | None) -> str:
    parts = []
    if stdout and stdout.strip():
        parts.append(stdout.strip())
    if stderr and stderr.strip():
        parts.append(stderr.strip())
    return "\n".join(parts)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False
