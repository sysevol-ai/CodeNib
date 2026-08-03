#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""SCIP indexer for C# projects using scip-dotnet."""

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

logger = get_logger("scip_csharp_indexer")


class SCIPCSharpIndexer(SCIPIndexerBase):
    """Run scip-dotnet for the active C# graph backend."""

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
            language="csharp",
            decoder_backend=decoder_backend,
        )

    def _check_indexer_available(self) -> bool:
        command = scip_cold_start_command_for_language("csharp")
        if not command:
            logger.error("No SCIP cold-start command registered for C#")
            return False
        if shutil.which(command[0]) is None:
            logger.error(
                "scip-dotnet not found. Install it or set CODENIB_CSHARP_SCIP_CMD."
            )
            return False
        return True

    def _build_index_command(self, **kwargs) -> list[str]:
        command = list(scip_cold_start_command_for_language("csharp") or [])
        if not command:
            return []
        if "index" not in command[1:]:
            command.append("index")

        project_files = _project_files(self.project_root)
        project_arg = kwargs.get("project")
        if project_arg:
            project_files = [Path(project_arg)]
        command.extend(str(path) for path in project_files)

        if "--output" not in command:
            command.extend(["--output", str(self.index_file)])
        if "--working-directory" not in command:
            command.extend(["--working-directory", str(self.project_root)])
        return command

    def _get_decoder_class(self):
        from .scip_decode_csharp import SCIPCSharpGraphDecoder

        return SCIPCSharpGraphDecoder

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
                    env=_dotnet_env(),
                )
            except subprocess.TimeoutExpired as exc:
                logger.error("C# SCIP indexing timed out: %s", exc)
                return False
            except subprocess.CalledProcessError as exc:
                logger.error("Error generating C# SCIP index: %s", exc)
                detail = _combine_command_output(exc.stdout, exc.stderr)
                if detail:
                    logger.error(detail)
                return False

        duration = section.duration
        if self.index_file.exists():
            logger.info("Successfully generated C# SCIP index at %s", self.index_file)
            logger.info("Index generation took: %.2f seconds", duration)
            return True

        logger.error("scip-dotnet completed but did not create %s", self.index_file)
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
        return super().run_pipeline(
            output_file=output_file,
            skip_level=skip_level,
            reset_profiler=reset_profiler,
            report_profile=report_profile,
            **kwargs,
        )


def _project_files(project_root: Path) -> list[Path]:
    root = Path(project_root)
    patterns = ("*.sln", "*.csproj", "*.vbproj")
    root_level = [path for pattern in patterns for path in root.glob(pattern)]
    if root_level:
        return sorted(root_level)

    nested = []
    for pattern in patterns:
        nested.extend(
            path
            for path in root.rglob(pattern)
            if not any(part in {"bin", "obj"} for part in path.parts)
        )
    return sorted(nested)


def _dotnet_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("DOTNET_CLI_TELEMETRY_OPTOUT", "1")
    return env


def _combine_command_output(stdout: str | None, stderr: str | None) -> str:
    parts = []
    if stdout and stdout.strip():
        parts.append(stdout.strip())
    if stderr and stderr.strip():
        parts.append(stderr.strip())
    return "\n".join(parts)
