# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Repository identity checks for Guardian review requests."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .types import GuardianRequest

_GIT_ENV = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_TERMINAL_PROMPT": "0",
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "LANG": "C.UTF-8",
}


def _git(workspace: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("git", *args),
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env=_GIT_ENV,
    )


def validate_request_provenance(request: GuardianRequest) -> str | None:
    """Return an error when *request* is not the advertised candidate checkout."""

    try:
        resolved: dict[str, str] = {}
        for name, revision in (
            ("HEAD", "HEAD"),
            ("base", request.base_commit),
            ("candidate", request.candidate_commit),
        ):
            result = _git(
                request.workspace, "rev-parse", "--verify", f"{revision}^{{commit}}"
            )
            if result.returncode != 0:
                return f"repository provenance check could not resolve {name} commit"
            resolved[name] = result.stdout.strip().lower()

        if resolved["HEAD"] != resolved["candidate"]:
            return "workspace HEAD does not match the advertised candidate commit"
        ancestry = _git(
            request.workspace,
            "merge-base",
            "--is-ancestor",
            resolved["base"],
            resolved["candidate"],
        )
        if ancestry.returncode != 0:
            return "base commit is not an ancestor of the candidate commit"
    except (OSError, subprocess.SubprocessError) as exc:
        return f"repository provenance check failed: {type(exc).__name__}"
    return None


__all__ = ["validate_request_provenance"]
