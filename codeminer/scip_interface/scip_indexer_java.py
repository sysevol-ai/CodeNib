#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""SCIP indexers for JVM projects using scip-java."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional, Union

from ..languages import scip_cold_start_command_for_language
from ..log_utils import get_logger
from ..profiler import Profiler
from .scip_indexer_base import SCIPIndexerBase

logger = get_logger("scip_java_indexer")

_JVM_ADD_EXPORTS = (
    "--add-exports=jdk.compiler/com.sun.tools.javac.model=ALL-UNNAMED",
    "--add-exports=jdk.compiler/com.sun.tools.javac.api=ALL-UNNAMED",
    "--add-exports=jdk.compiler/com.sun.tools.javac.tree=ALL-UNNAMED",
    "--add-exports=jdk.compiler/com.sun.tools.javac.util=ALL-UNNAMED",
    "--add-exports=jdk.compiler/com.sun.tools.javac.code=ALL-UNNAMED",
)


class SCIPJavaIndexer(SCIPIndexerBase):
    """Run scip-java for Java and JVM candidate routes."""

    def __init__(
        self,
        project_root: Union[str, Path],
        output_dir: Optional[Union[str, Path]] = None,
        exclude_patterns: Optional[List] = None,
        profiler: Optional[Profiler] = None,
        decoder_backend: Optional[str] = None,
        scip_language: str = "java",
    ):
        self.scip_language = scip_language
        super().__init__(
            project_root=project_root,
            output_dir=output_dir,
            exclude_patterns=exclude_patterns,
            profiler=profiler,
            language=scip_language,
            decoder_backend=decoder_backend,
        )

    def _check_indexer_available(self) -> bool:
        command = scip_cold_start_command_for_language(self.scip_language)
        if not command:
            logger.error(
                "No SCIP cold-start command registered for %s", self.scip_language
            )
            return False
        if shutil.which(command[0]) is None:
            logger.error(
                "scip-java not found. Install it or set the language-specific "
                "CODEMINER_*_SCIP_CMD override."
            )
            return False
        return True

    def _build_index_command(self, **kwargs) -> list[str]:
        command = list(scip_cold_start_command_for_language(self.scip_language) or [])
        if not command:
            return []
        if "--output" not in command:
            command.extend(["--output", str(self.index_file)])
        return command

    def _get_decoder_class(self):
        from .scip_decode_java import SCIPJavaGraphDecoder

        return SCIPJavaGraphDecoder

    def generate_index(self, timeout: Optional[int] = None, **kwargs) -> bool:
        if not self._check_indexer_available():
            return False
        command = self._build_index_command(**kwargs)
        if not command:
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
                    env=self._java_env(),
                )
            except subprocess.TimeoutExpired as exc:
                logger.error("Java SCIP indexing timed out: %s", exc)
                return False
            except subprocess.CalledProcessError as exc:
                logger.error("Error generating Java SCIP index: %s", exc)
                detail = _combine_command_output(exc.stdout, exc.stderr)
                if detail:
                    logger.error(detail)
                return False

        duration = section.duration
        if self.index_file.exists():
            logger.info("Successfully generated Java SCIP index at %s", self.index_file)
            logger.info("Index generation took: %.2f seconds", duration)
            return True

        logger.error("scip-java completed but did not create %s", self.index_file)
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
        kwargs.pop("target_dir", None)
        return super().run_pipeline(
            output_file=output_file,
            skip_level=skip_level,
            reset_profiler=reset_profiler,
            report_profile=report_profile,
            **kwargs,
        )

    def _java_env(self) -> dict[str, str]:
        env = os.environ.copy()
        existing = env.get("JDK_JAVA_OPTIONS", "").strip()
        additions = " ".join(_JVM_ADD_EXPORTS)
        env["JDK_JAVA_OPTIONS"] = (
            f"{existing} {additions}".strip() if existing else additions
        )
        return env


def _combine_command_output(stdout: str | None, stderr: str | None) -> str:
    parts = []
    if stdout and stdout.strip():
        parts.append(stdout.strip())
    if stderr and stderr.strip():
        parts.append(stderr.strip())
    return "\n".join(parts)


class SCIPKotlinIndexer(SCIPJavaIndexer):
    """Candidate scip-java indexer for Kotlin projects."""

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
            decoder_backend=decoder_backend,
            scip_language="kotlin",
        )


class SCIPScalaIndexer(SCIPJavaIndexer):
    """Candidate scip-java indexer for Scala projects."""

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
            decoder_backend=decoder_backend,
            scip_language="scala",
        )
