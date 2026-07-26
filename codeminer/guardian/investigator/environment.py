# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
# SPDX-License-Identifier: Apache-2.0

"""Deterministic, token-free environment prelude for Guardian L3."""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Tuple

from .report_parser import parse_pytest_junit
from .sandbox import SandboxHandle
from .types import ProcessStatus, TestStatus


@dataclass(frozen=True)
class TestRecipe:
    """A validated command prefix for invoking pytest in one repository."""

    command_prefix: Tuple[str, ...]
    source: str

    def command(
        self,
        target: Optional[str],
        report_path: str,
        *,
        collect_only: bool = False,
    ) -> list[str]:
        command = list(self.command_prefix)
        if target:
            command.append(target)
        if collect_only:
            command.append("--collect-only")
        command.extend(["-q", "--tb=short", f"--junitxml={report_path}"])
        return command


@dataclass(frozen=True)
class PreludeResult:
    """Available investigation capabilities in one disposable snapshot."""

    blocked: bool
    reason: str
    recipe: Optional[TestRecipe] = None
    capabilities: dict[str, bool] = field(default_factory=dict)


def _repo_key(repo_path: str, commit: str) -> str:
    identity = f"{os.path.realpath(repo_path)}\0{commit}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _candidate_recipes(repo_path: str) -> Iterable[TestRecipe]:
    """Yield conservative candidates with Guardian's interpreter first."""

    seen = set()

    def emit(parts: tuple[str, ...], source: str):
        if parts in seen:
            return None
        seen.add(parts)
        return TestRecipe(parts, source)

    first = emit((sys.executable, "-m", "pytest"), "guardian-interpreter")
    if first:
        yield first

    root = Path(repo_path)
    if (root / "uv.lock").is_file() and shutil.which("uv"):
        recipe = emit(("uv", "run", "python", "-m", "pytest"), "uv.lock")
        if recipe:
            yield recipe
    if (root / "poetry.lock").is_file() and shutil.which("poetry"):
        recipe = emit(("poetry", "run", "python", "-m", "pytest"), "poetry.lock")
        if recipe:
            yield recipe
    for executable in ("python", "python3"):
        if shutil.which(executable):
            recipe = emit((executable, "-m", "pytest"), "PATH")
            if recipe:
                yield recipe


def _load_cached_recipe(
    memory_store: object, repo_path: str, commit: str
) -> Optional[TestRecipe]:
    getter = getattr(memory_store, "get_test_recipe", None)
    if getter is None:
        return None
    raw = getter(_repo_key(repo_path, commit)) or getter(_repo_key(repo_path, "*"))
    if not raw:
        return None
    try:
        return TestRecipe(tuple(raw["command_prefix"]), str(raw["source"]))
    except (KeyError, TypeError, ValueError):
        return None


def _save_recipe(
    memory_store: object, repo_path: str, commit: str, recipe: TestRecipe
) -> None:
    setter = getattr(memory_store, "set_test_recipe", None)
    if setter is not None:
        setter(_repo_key(repo_path, commit), asdict(recipe))
        setter(_repo_key(repo_path, "*"), asdict(recipe))


def validate_recipe(
    recipe: TestRecipe, sandbox: SandboxHandle, *, timeout: int = 60
) -> tuple[bool, str]:
    """Validate collection and its report without interpreting an exit code."""

    report_rel = f".guardian/reports/collect-{uuid.uuid4().hex}.xml"
    report_abs = os.path.join(sandbox.repo_path, report_rel)
    Path(report_abs).parent.mkdir(parents=True, exist_ok=True)
    command = recipe.command(None, report_rel, collect_only=True)
    result = sandbox.run_command(command, timeout=timeout)
    parsed = parse_pytest_junit(report_abs)
    if result.status is not ProcessStatus.EXITED:
        return False, f"collect-only process {result.status.value}: {result.stderr}"
    if not parsed.parseable:
        detail = parsed.error or result.stderr or result.stdout
        return False, f"collect-only report unavailable: {detail}"
    if any(status is TestStatus.ERROR for status in parsed.tests.values()):
        return False, "collect-only report contains collection errors"
    return True, ""


def run_prelude(
    sandbox: SandboxHandle,
    *,
    memory_store: object = None,
    commit: str = "",
    repo_identity: str = "",
    timeout: int = 60,
) -> PreludeResult:
    """Check the disposable root and select a validated pytest recipe."""

    root = Path(sandbox.repo_path)
    if not root.is_dir():
        return PreludeResult(
            True,
            "disposable snapshot is not materialized",
            capabilities={"source": False, "python_probe": False, "pytest": False},
        )
    try:
        health_dir = root / ".guardian" / "tmp"
        health_dir.mkdir(parents=True, exist_ok=True)
        marker = health_dir / f"health-{uuid.uuid4().hex}"
        marker.write_text("ok", encoding="utf-8")
        marker.unlink()
    except OSError as exc:
        return PreludeResult(
            True,
            f"disposable snapshot is not writable: {exc}",
            capabilities={"source": True, "python_probe": False, "pytest": False},
        )

    candidates = []
    identity = repo_identity or sandbox.repo_path
    cached = _load_cached_recipe(memory_store, identity, commit)
    if cached is not None:
        candidates.append(cached)
    candidates.extend(_candidate_recipes(sandbox.repo_path))

    failures = []
    seen = set()
    for recipe in candidates:
        if recipe.command_prefix in seen:
            continue
        seen.add(recipe.command_prefix)
        valid, reason = validate_recipe(recipe, sandbox, timeout=timeout)
        if valid:
            _save_recipe(memory_store, identity, commit, recipe)
            return PreludeResult(
                False,
                "",
                recipe,
                {"source": True, "python_probe": True, "pytest": True},
            )
        failures.append(f"{' '.join(recipe.command_prefix)}: {reason}")
    detail = "; ".join(failures) if failures else "no Python test recipe candidates"
    # Pytest discovery is advisory.  Source inspection and generic Python
    # probes remain useful and must not be disabled by a missing project test
    # environment.
    return PreludeResult(
        False,
        detail,
        None,
        {"source": True, "python_probe": True, "pytest": False},
    )
