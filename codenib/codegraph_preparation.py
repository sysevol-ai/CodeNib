# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Source-preserving local CodeGraph preparation shared by onboarding and MCP."""

from __future__ import annotations

import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from .repository_source_selection import RepositorySourceSelection


class CodeGraphPreparationError(RuntimeError):
    """A preparation failure that must remain visible to the caller."""


def checkout_snapshot(repo_path: Path) -> bytes:
    """Return the exact Git worktree status used by the onboarding transaction."""

    command = [
        "git",
        "-C",
        str(repo_path),
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--ignore-submodules=none",
    ]
    try:
        result = subprocess.run(command, check=False, capture_output=True)
    except OSError as exc:
        raise CodeGraphPreparationError(
            "CodeGraph initialization requires Git on PATH"
        ) from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        suffix = f": {detail}" if detail else ""
        raise CodeGraphPreparationError(
            "CodeGraph initialization requires a Git working tree" + suffix
        )
    return bytes(result.stdout)


@contextmanager
def checkout_guard(repo_path: Path) -> Iterator[Callable[[str], None]]:
    """Preserve visible source content and Git status across initialization.

    A phase succeeds only if its postcondition matches the captured source
    and status. Status alone misses another edit to an already dirty path.
    Reuse a pinned root across fresh captures so a restored temporary provider
    config is allowed, but replacing the checkout root is not. The default
    source selection also guards visible custom-excluded source. Source and
    root descriptors close within this call; no recovery state survives it.
    """

    from .compiler.checkout_identity import checkout_commit
    from .paths import repo_index_dir
    from .source_fingerprint import (
        capture_repository_source,
        pin_repository_source_root,
    )

    def source_fingerprint(authority):
        with capture_repository_source(
            repo_path,
            exclude_roots=(repo_index_dir(repo_path),),
            expected_root_authority=authority,
        ) as source:
            return source.fingerprint

    try:
        authority = pin_repository_source_root(repo_path)
    except (OSError, RuntimeError, ValueError) as exc:
        raise CodeGraphPreparationError(
            f"CodeGraph cannot inspect the current working tree: {exc}"
        ) from exc
    with authority:
        status = checkout_snapshot(repo_path)
        commit = checkout_commit(repo_path)
        try:
            fingerprint = source_fingerprint(authority)
        except (OSError, RuntimeError, ValueError) as exc:
            raise CodeGraphPreparationError(
                f"CodeGraph cannot inspect the current working tree: {exc}"
            ) from exc

        def require_unchanged(stage: str) -> None:
            message = (
                f"CodeGraph {stage} changed the target checkout; inspect `git diff` "
                "and `git status` before retrying"
            )
            try:
                observed_fingerprint = source_fingerprint(authority)
                observed_status = checkout_snapshot(repo_path)
                observed_commit = checkout_commit(repo_path)
            except (OSError, RuntimeError, ValueError) as exc:
                raise CodeGraphPreparationError(message) from exc
            if (
                observed_fingerprint != fingerprint
                or observed_status != status
                or observed_commit != commit
            ):
                raise CodeGraphPreparationError(message)

        require_unchanged("source inspection")
        yield require_unchanged


def prepare_graph(
    repository: Path,
    *,
    languages: list[str],
    source_selection: RepositorySourceSelection,
    expected_source_fingerprint: str,
) -> None:
    """Prepare one graph through the existing toolchain/compiler boundaries.

    The MCP caller owns its context lock and first verifies the old source.
    The compiler owns publication under its existing process-wide cache lock;
    this function retains no job, background owner or recovery record. Never
    write project dependencies or accept partial-language graphs. Installer
    subprocess output goes to stderr to preserve the MCP stdio transport.
    """
    import sys

    from .compiler.index_builders import IndexBuilderRegistry, register_default_builders
    from .compiler.index_compiler import IndexCompiler, IndexCompilerConfig
    from .paths import repo_index_dir
    from .source_fingerprint import fingerprint_repository
    from .toolchains import (
        activate_managed_toolchain,
        install_requirements,
        plan_repository_toolchain,
    )

    def plan():
        return plan_repository_toolchain(
            repository, languages, scopes=("graph",), allow_project_preparation=False
        )

    def install_command(command, **kwargs):
        if not kwargs.get("capture_output"):
            kwargs.update(stdout=sys.stderr, stderr=sys.stderr)
        return subprocess.run(command, **kwargs)

    with checkout_guard(repository) as check_checkout:
        source = fingerprint_repository(
            repository,
            selection=source_selection,
            exclude_roots=(repo_index_dir(repository),),
        )
        if source.value != expected_source_fingerprint:
            raise CodeGraphPreparationError(
                "Indexed source changed; run `codenib codegraph init` and retry"
            )
        prerequisites = plan()
        if prerequisites.missing:
            _commands, manual = install_requirements(
                prerequisites.missing, root=prerequisites.root, runner=install_command
            )
            if manual:
                raise CodeGraphPreparationError(
                    "Graph prerequisites: " + "; ".join(manual)
                )
            prerequisites = plan()
        if not prerequisites.ready:
            details = [item.executable for item in prerequisites.missing]
            details.extend(prerequisites.notes)
            raise CodeGraphPreparationError(
                "Graph prerequisites: " + "; ".join(details)
            )
        check_checkout("toolchain setup")
        activate_managed_toolchain()
        registry = IndexBuilderRegistry()
        register_default_builders(
            registry,
            languages=languages,
            source_selection=source_selection,
            allow_graph_project_preparation=False,
            allow_partial_graph_languages=False,
            allow_partial_graph_index=False,
        )
        compiler = IndexCompiler(
            registry,
            IndexCompilerConfig(
                index_types=["symbol_graph"],
                languages=languages,
                source_selection=source_selection,
            ),
        )
        try:
            manifest = compiler.update_repo(
                str(repository),
                index_types=["symbol_graph"],
                cache_dir=str(repo_index_dir(repository)),
            )
        finally:
            check_checkout("indexing")
        if manifest.source_fingerprint != expected_source_fingerprint:
            raise CodeGraphPreparationError(
                "Indexed source changed during graph preparation; "
                "run `codenib codegraph init` and retry"
            )
        if not manifest.index_is_current("symbol_graph"):
            raise CodeGraphPreparationError(
                "Graph preparation failed; run `codenib codegraph init` and inspect "
                "the provider diagnostics before retrying"
            )
