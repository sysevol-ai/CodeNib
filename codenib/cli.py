# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""User-facing CodeNib command line interface."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import posixpath
import re
import shlex
import shutil
import sqlite3
import stat
import subprocess
import sys
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence

from ._version import package_version
from .provider_routes import (
    InferenceRoute,
    normalize_provider,
    resolve_embedding_artifact_route,
    resolve_inference_route,
)
from .repository_filters import walk_repository_files
from .repository_source_selection import (
    DEFAULT_REPOSITORY_SOURCE_SELECTION,
    RepositorySourceSelection,
)
from .web.ports import argparse_tcp_port, validate_local_wiki_ports

_PRESET_VIEWS = {
    "fast": ("bm25",),
    "semantic": ("bm25", "vector"),
    "graph": ("bm25", "symbol_graph"),
    "full": ("bm25", "vector", "symbol_graph", "zoekt"),
}
_PRESET_CHOICES = ("auto", *_PRESET_VIEWS)
_REMOTE_EMBEDDING_DEFAULTS = {
    "openai": ("text-embedding-3-small", 1536),
}
_RETAINED_REPOSITORY_RE = re.compile(
    r"[a-z0-9_.-]+(?:/[a-z0-9_.-]+)*\Z",
    re.ASCII,
)
_SIGNED_INT64_MAX = 9_223_372_036_854_775_807
_MAX_RETAINED_MOUNTINFO_ENTRIES = 65_536
_MAX_RETAINED_MOUNTINFO_LINE_BYTES = 64 * 1024
_MAX_RETAINED_ALIAS_COMPONENTS = 256
_MAX_RETAINED_ALIAS_PATH_BYTES = 4_096


class CLIError(RuntimeError):
    """A user-actionable command error."""


@dataclass(frozen=True, slots=True)
class _ResolvedModelBackend:
    model: str
    api_base: str | None
    api_key: str | None = field(default=None, repr=False)
    auth_source: str | None = None


@dataclass(frozen=True, slots=True)
class _ResolvedRepositorySourceSelection:
    """One canonical source-selection decision for a CLI transaction."""

    selection: RepositorySourceSelection
    action: str
    manifest_present: bool


def _optional_int(value: object, *, source: str) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise CLIError(f"{source} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise CLIError(f"{source} must be a positive integer") from exc
    if parsed <= 0:
        raise CLIError(f"{source} must be a positive integer")
    return parsed


def _argparse_positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed < 1 or parsed > _SIGNED_INT64_MAX:
        raise argparse.ArgumentTypeError("must be a positive signed 64-bit integer")
    return parsed


def _argparse_nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a non-negative integer") from exc
    if parsed < 0 or parsed > _SIGNED_INT64_MAX:
        raise argparse.ArgumentTypeError("must be a non-negative signed 64-bit integer")
    return parsed


def _embedding_route_for_args(args: argparse.Namespace) -> InferenceRoute:
    """Resolve the secret-free embedding identity selected by CLI and env."""

    from .index.embedding.model_policy import (
        DEFAULT_EMBEDDING_DIMENSION,
        DEFAULT_EMBEDDING_MODEL,
    )

    raw_provider = (
        getattr(args, "embedding_provider", None)
        or os.environ.get("CODENIB_EMBEDDING_PROVIDER")
        or "huggingface"
    )
    try:
        provider = normalize_provider(raw_provider)
    except ValueError as exc:
        raise CLIError(str(exc)) from exc

    explicit_model = getattr(args, "embedding_model", None) or os.environ.get(
        "CODENIB_EMBEDDING_MODEL"
    )
    dimension = _optional_int(
        getattr(args, "embedding_dimension", None)
        or os.environ.get("CODENIB_EMBEDDING_DIMENSION"),
        source="embedding dimension",
    )
    if provider in _REMOTE_EMBEDDING_DEFAULTS:
        default_model, default_dimension = _REMOTE_EMBEDDING_DEFAULTS[provider]
        model = explicit_model or default_model
        if dimension is None:
            if explicit_model and explicit_model != default_model:
                raise CLIError(
                    "--embedding-dimension is required for a non-default remote model"
                )
            dimension = default_dimension
    else:
        model = explicit_model or DEFAULT_EMBEDDING_MODEL
        dimension = dimension or DEFAULT_EMBEDDING_DIMENSION

    endpoint = (
        getattr(args, "embedding_endpoint", None)
        or os.environ.get("CODENIB_EMBEDDING_ENDPOINT")
        or os.environ.get("CODENIB_EMBEDDING_BASE_URL")
    )
    credential_env = getattr(args, "embedding_api_key_env", None) or os.environ.get(
        "CODENIB_EMBEDDING_API_KEY_ENV"
    )
    try:
        return resolve_inference_route(
            operation="embeddings",
            provider=provider,
            model=model,
            endpoint=endpoint,
            dimension=dimension,
            credential_env=credential_env,
        )
    except ValueError as exc:
        raise CLIError(str(exc)) from exc


def _embedding_identity_is_configured(args: argparse.Namespace) -> bool:
    return any(
        (
            getattr(args, "embedding_provider", None),
            getattr(args, "embedding_model", None),
            getattr(args, "embedding_dimension", None),
            getattr(args, "embedding_endpoint", None),
            os.environ.get("CODENIB_EMBEDDING_PROVIDER"),
            os.environ.get("CODENIB_EMBEDDING_MODEL"),
            os.environ.get("CODENIB_EMBEDDING_DIMENSION"),
            os.environ.get("CODENIB_EMBEDDING_ENDPOINT"),
            os.environ.get("CODENIB_EMBEDDING_BASE_URL"),
        )
    )


def _model_backend_for_args(
    args: argparse.Namespace,
    *,
    options: dict[str, object] | None = None,
) -> _ResolvedModelBackend | None:
    """Resolve a chat route while retaining legacy raw LiteLLM model strings."""

    model = (
        getattr(args, "model", None)
        or os.environ.get("CODENIB_DEMO_WIKI_MODEL")
        or os.environ.get("CODENIB_DEMO_MODEL")
    )
    if not model:
        return None
    provider = getattr(args, "model_provider", None) or os.environ.get(
        "CODENIB_DEMO_MODEL_PROVIDER"
    )
    api_base = (
        getattr(args, "api_base", None)
        or os.environ.get("CODENIB_DEMO_WIKI_API_BASE")
        or os.environ.get("CODENIB_DEMO_API_BASE")
    )
    key_env = getattr(args, "api_key_env", None)
    direct_key_source = None
    direct_key = None
    if key_env:
        direct_key_source = key_env
        direct_key = os.environ.get(key_env)
    elif os.environ.get("CODENIB_DEMO_WIKI_API_KEY"):
        direct_key_source = "CODENIB_DEMO_WIKI_API_KEY"
        direct_key = os.environ[direct_key_source]
    elif os.environ.get("CODENIB_DEMO_API_KEY"):
        direct_key_source = "CODENIB_DEMO_API_KEY"
        direct_key = os.environ[direct_key_source]

    if key_env and not direct_key:
        raise CLIError(f"{key_env} is unset or empty")
    if not provider:
        return _ResolvedModelBackend(
            model=model,
            api_base=api_base,
            api_key=direct_key,
            auth_source=direct_key_source,
        )

    try:
        route = resolve_inference_route(
            operation="chat",
            provider=provider,
            model=model,
            endpoint=api_base,
            credential_env=key_env,
            compatibility_options=options,
        )
        api_key = direct_key if direct_key is not None else route.credential()
    except ValueError as exc:
        raise CLIError(str(exc)) from exc
    return _ResolvedModelBackend(
        model=route.client_model,
        api_base=route.endpoint,
        api_key=api_key,
        auth_source=direct_key_source or route.credential_env,
    )


def _split_values(values: Iterable[str] | None) -> list[str]:
    result: list[str] = []
    for value in values or ():
        result.extend(part.strip() for part in value.split(",") if part.strip())
    return result


def _detect_languages_for_surface(
    repo_path: str | os.PathLike[str],
    *,
    surface: str,
    source_selection: RepositorySourceSelection = (DEFAULT_REPOSITORY_SOURCE_SELECTION),
) -> list[str]:
    """Detect registered source languages for one capability surface."""
    from .languages import extension_to_language_map
    from .source_fingerprint import lexical_repository_path

    root = lexical_repository_path(repo_path)
    extension_map = extension_to_language_map(surface)
    counts: Counter[str] = Counter()

    for path in walk_repository_files(root, selection=source_selection):
        language = extension_map.get(path.suffix.lower())
        if language:
            counts[language] += 1

    return [
        language
        for language, _ in sorted(
            counts.items(),
            key=lambda item: (-item[1], item[0]),
        )
    ]


def detect_languages(
    repo_path: str | os.PathLike[str],
    *,
    source_selection: RepositorySourceSelection = (DEFAULT_REPOSITORY_SOURCE_SELECTION),
) -> list[str]:
    """Detect chunkable source languages, ordered by source-file count."""

    return _detect_languages_for_surface(
        repo_path,
        surface="chunker",
        source_selection=source_selection,
    )


def normalize_languages(values: Iterable[str]) -> list[str]:
    """Normalize user language aliases while preserving their order."""
    from .languages import get_language_spec

    normalized: list[str] = []
    for value in _split_values(values):
        spec = get_language_spec(value)
        if spec is None or spec.chunker_language is None:
            raise CLIError(f"unsupported language: {value}")
        language = spec.chunker_language
        if language not in normalized:
            normalized.append(language)
    return normalized


def resolve_repo_path(value: str) -> Path:
    from .source_fingerprint import lexical_repository_path

    path = lexical_repository_path(value)
    try:
        metadata = path.lstat()
    except OSError:
        metadata = None
    if (
        metadata is None
        or stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
    ):
        raise CLIError(f"repository directory does not exist: {path}")
    return path


def resolve_manifest_path(value: str) -> Path:
    """Resolve either a repository directory or a manifest path."""
    from .compiler.manifest import MANIFEST_FILENAME
    from .paths import legacy_repo_index_dir, repo_index_dir

    path = Path(os.path.abspath(os.fspath(Path(value).expanduser())))
    try:
        metadata = path.lstat()
    except OSError:
        metadata = None
    if metadata is not None and stat.S_ISLNK(metadata.st_mode):
        raise CLIError(f"manifest path must not be a symbolic link: {path}")
    if metadata is not None and stat.S_ISDIR(metadata.st_mode):
        candidates = (
            repo_index_dir(path) / MANIFEST_FILENAME,
            legacy_repo_index_dir(path) / MANIFEST_FILENAME,
        )
        path = next(
            (
                candidate
                for candidate in candidates
                if _is_regular_file_nofollow(candidate)
            ),
            candidates[0],
        )
    if not _is_regular_file_nofollow(path):
        raise CLIError(
            f"manifest not found: {path}\n"
            "Run `codenib index <repo>` before starting this command."
        )
    return path


def _is_regular_file_nofollow(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(metadata.st_mode)


def _resolve_repository_source_selection(
    repo_path: Path,
    args: argparse.Namespace,
) -> _ResolvedRepositorySourceSelection:
    """Resolve one explicit or persisted exact-subtree selection.

    An explicit CLI policy replaces the complete custom exclusion set.  With
    no policy flags, a current manifest is the sole persisted authority.  A
    legacy v1.1 manifest deliberately migrates to the current empty v4 policy
    instead of pretending that its policy-3 identity was already equivalent.
    """

    from .compiler.manifest import MANIFEST_FILENAME, RepoManifest
    from .paths import repo_index_dir

    exclude_dirs = getattr(args, "exclude_dir", None)
    clear = bool(getattr(args, "clear_exclude_dirs", False))
    if exclude_dirs is not None and clear:
        raise CLIError("--exclude-dir and --clear-exclude-dirs are mutually exclusive")
    if clear:
        return _ResolvedRepositorySourceSelection(
            selection=RepositorySourceSelection(),
            action="clear",
            manifest_present=False,
        )
    if exclude_dirs is not None:
        try:
            selection = RepositorySourceSelection(exclude_dirs)
        except (TypeError, ValueError) as exc:
            raise CLIError(f"invalid --exclude-dir: {exc}") from exc
        return _ResolvedRepositorySourceSelection(
            selection=selection,
            action="replace",
            manifest_present=False,
        )

    manifest_path = repo_index_dir(repo_path) / MANIFEST_FILENAME
    if not _is_regular_file_nofollow(manifest_path):
        return _ResolvedRepositorySourceSelection(
            selection=RepositorySourceSelection(),
            action="default",
            manifest_present=False,
        )
    try:
        manifest = RepoManifest.load(manifest_path)
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise CLIError(
            "the existing manifest source selection cannot be read; pass "
            "--exclude-dir to replace it or --clear-exclude-dirs to clear it"
        ) from exc
    if manifest.source_selection is None:
        return _ResolvedRepositorySourceSelection(
            selection=RepositorySourceSelection(),
            action="migrate",
            manifest_present=True,
        )
    return _ResolvedRepositorySourceSelection(
        selection=RepositorySourceSelection(
            manifest.source_selection.exclude_subtrees,
        ),
        action="reuse",
        manifest_present=True,
    )


def _source_selection_display(selection: RepositorySourceSelection) -> str:
    return ", ".join(selection.exclude_subtrees) or "none"


def _selected_languages(
    repo_path: Path,
    explicit: Iterable[str],
    *,
    source_selection: RepositorySourceSelection = (DEFAULT_REPOSITORY_SOURCE_SELECTION),
) -> list[str]:
    languages = normalize_languages(explicit)
    if languages:
        return languages
    languages = detect_languages(repo_path, source_selection=source_selection)
    if not languages:
        raise CLIError(
            "no supported source language was detected; pass --language explicitly"
        )
    return languages


def _selected_codegraph_languages(
    repo_path: Path,
    explicit: Iterable[str],
    *,
    source_selection: RepositorySourceSelection = (DEFAULT_REPOSITORY_SOURCE_SELECTION),
) -> list[str]:
    """Select only languages with a registered symbol-graph backend."""

    from .languages import graph_indexer_path, normalize_graph_language

    requested = _split_values(explicit)
    if requested:
        selected: list[str] = []
        for value in requested:
            language = normalize_graph_language(value)
            if language is None or graph_indexer_path(language) is None:
                raise CLIError(f"language has no symbol-graph provider: {value}")
            if language not in selected:
                selected.append(language)
        return selected

    languages = [
        language
        for language in _detect_languages_for_surface(
            repo_path,
            surface="graph",
            source_selection=source_selection,
        )
        if graph_indexer_path(language) is not None
    ]
    if not languages:
        raise CLIError(
            "no graph-capable source language was detected; pass --language "
            "for a registered symbol-graph provider"
        )
    return languages


def _selected_views(preset: str, explicit: Iterable[str]) -> list[str]:
    views = _split_values(explicit)
    if not views:
        return list(_PRESET_VIEWS[preset])
    unknown = sorted(set(views) - set(_PRESET_VIEWS["full"]))
    if unknown:
        raise CLIError(f"unsupported view: {', '.join(unknown)}")
    return list(dict.fromkeys(views))


def _selected_views_for_args(args: argparse.Namespace) -> list[str]:
    explicit = _split_values(args.view)
    if explicit or args.preset != "auto":
        return _selected_views(args.preset, explicit)

    route = _embedding_route_for_args(args)
    embedding_module = (
        "sentence_transformers" if route.provider == "huggingface" else "openai"
    )
    resolved = (
        "semantic"
        if _embedding_identity_is_configured(args)
        or (_check_module("faiss") and _check_module(embedding_module))
        else "fast"
    )
    reason = (
        "explicit embedding route"
        if _embedding_identity_is_configured(args)
        else (
            "dense dependencies available"
            if resolved == "semantic"
            else "no-model fallback"
        )
    )
    print(f"Auto preset: {resolved} ({reason})")
    return list(_PRESET_VIEWS[resolved])


def _prepare_index_compiler(
    repo_path: Path,
    *,
    languages: Sequence[str],
    views: Sequence[str],
    source_selection: RepositorySourceSelection = (DEFAULT_REPOSITORY_SOURCE_SELECTION),
    embedding_provider: str = "huggingface",
    embedding_model: str | None = None,
    embedding_dimension: int | None = None,
    embedding_endpoint: str | None = None,
    embedding_credential_env: str | None = None,
    allow_graph_project_preparation: bool = True,
    allow_partial_graph_languages: bool = True,
) -> tuple[object, Path]:
    """Construct the shared compiler configuration and canonical cache path."""
    from .toolchains import activate_managed_toolchain

    activate_managed_toolchain()
    from .compiler.index_builders import IndexBuilderRegistry, register_default_builders
    from .compiler.index_compiler import IndexCompiler, IndexCompilerConfig
    from .index.embedding.model_policy import (
        DEFAULT_EMBEDDING_DIMENSION,
        DEFAULT_EMBEDDING_MODEL,
    )
    from .paths import repo_index_dir

    registry = IndexBuilderRegistry()
    register_default_builders(
        registry,
        languages=list(languages),
        embedding_provider=embedding_provider,
        embedding_model=embedding_model or DEFAULT_EMBEDDING_MODEL,
        embedding_dimension=(
            DEFAULT_EMBEDDING_DIMENSION
            if embedding_dimension is None
            else embedding_dimension
        ),
        embedding_endpoint=embedding_endpoint,
        embedding_credential_env=embedding_credential_env,
        allow_partial_graph_languages=allow_partial_graph_languages,
        allow_graph_project_preparation=allow_graph_project_preparation,
        source_selection=source_selection,
    )
    compiler = IndexCompiler(
        registry,
        IndexCompilerConfig(
            index_types=list(views),
            languages=list(languages),
            source_selection=source_selection,
        ),
    )
    cache_dir = repo_index_dir(repo_path)
    return compiler, cache_dir


def index_repository(
    repo_path: Path,
    *,
    languages: Sequence[str],
    views: Sequence[str],
    source_selection: RepositorySourceSelection = (DEFAULT_REPOSITORY_SOURCE_SELECTION),
    rebuild: bool = False,
    embedding_provider: str = "huggingface",
    embedding_model: str | None = None,
    embedding_dimension: int | None = None,
    embedding_endpoint: str | None = None,
    embedding_credential_env: str | None = None,
    allow_graph_project_preparation: bool = True,
    allow_partial_graph_languages: bool = True,
):
    """Build or update the requested repository views."""
    from .compiler.manifest import MANIFEST_FILENAME

    compiler, cache_dir = _prepare_index_compiler(
        repo_path,
        languages=languages,
        views=views,
        source_selection=source_selection,
        embedding_provider=embedding_provider,
        embedding_model=embedding_model,
        embedding_dimension=embedding_dimension,
        embedding_endpoint=embedding_endpoint,
        embedding_credential_env=embedding_credential_env,
        allow_graph_project_preparation=allow_graph_project_preparation,
        allow_partial_graph_languages=allow_partial_graph_languages,
    )
    manifest_path = cache_dir / MANIFEST_FILENAME
    if manifest_path.is_file() and not rebuild:
        manifest = compiler.update_repo(
            str(repo_path),
            index_types=list(views),
            cache_dir=str(cache_dir),
        )
    else:
        manifest = compiler.compile_repo(
            str(repo_path),
            index_types=list(views),
            cache_dir=str(cache_dir),
        )

    failed = [view for view in views if not manifest.index_is_current(view)]
    return manifest, failed


def _print_index_summary(manifest, views: Sequence[str]) -> None:
    from .compiler.manifest import MANIFEST_FILENAME
    from .paths import repo_index_dir

    manifest_path = repo_index_dir(manifest.repo_path) / MANIFEST_FILENAME
    print(f"Repository: {manifest.repo_path}")
    print(f"Languages:  {', '.join(manifest.languages)}")
    source_selection = getattr(manifest, "source_selection", None)
    if source_selection is not None:
        print("Excluded:   " + _source_selection_display(source_selection))
    print(f"Manifest:   {manifest_path}")
    print("Views:")
    for view in views:
        entry = manifest.indexes.get(view)
        if entry is None:
            print(f"  {view:<14} missing")
            continue
        duration = entry.metadata.get("build_duration_seconds")
        suffix = f" ({duration:.2f}s)" if isinstance(duration, (int, float)) else ""
        if entry.metadata.get("partial"):
            available = ", ".join(entry.metadata.get("available_languages") or ())
            unavailable = ", ".join(
                (entry.metadata.get("failed_languages") or {}).keys()
            )
            suffix += (
                f" [partial: {available or 'none'}; "
                f"unavailable: {unavailable or 'none'}]"
            )
        print(f"  {view:<14} {entry.status}{suffix}")


@dataclass(frozen=True, slots=True)
class _RetainedIndexSelection:
    repository: str
    namespace: str
    ref_name: str
    expected_generation: int


def _retained_index_selection(
    args: argparse.Namespace,
) -> _RetainedIndexSelection | None:
    """Validate the all-or-none retained publication option group."""

    if not hasattr(args, "publish_retained"):
        return None
    publish = bool(getattr(args, "publish_retained", False))
    retained_options = {
        "catalog": getattr(args, "catalog", None),
        "cas_root": getattr(args, "cas_root", None),
        "workspace_root": getattr(args, "workspace_root", None),
        "repository": getattr(args, "repository", None),
        "namespace": getattr(args, "namespace", None),
        "ref": getattr(args, "ref", None),
        "expected_generation": getattr(args, "expected_generation", None),
    }
    if not publish:
        selected = [
            name for name, value in retained_options.items() if value is not None
        ]
        if selected:
            rendered = ", ".join("--" + name.replace("_", "-") for name in selected)
            raise CLIError(f"{rendered} require --publish-retained")
        return None

    if bool(getattr(args, "rebuild", False)):
        raise CLIError("--rebuild is not supported with --publish-retained")
    required = ("catalog", "cas_root", "workspace_root", "repository")
    missing = [name for name in required if retained_options[name] is None]
    if missing:
        rendered = ", ".join("--" + name.replace("_", "-") for name in missing)
        raise CLIError(f"--publish-retained requires {rendered}")

    repository, namespace = _retained_materialization_identity(
        retained_options["repository"],
        retained_options["namespace"] or "default",
    )
    ref_name, expected_generation = _compiler_cache_import_selection(
        ref_name=retained_options["ref"],
        expected_generation=retained_options["expected_generation"],
    )
    return _RetainedIndexSelection(
        repository=repository,
        namespace=namespace,
        ref_name=ref_name,
        expected_generation=expected_generation,
    )


def _require_retained_index_views(views: Sequence[str]) -> tuple[str, ...]:
    selected = tuple(views)
    unsupported = sorted(set(selected) - set(_COMPILER_CACHE_IMPORT_VIEWS))
    if unsupported:
        raise CLIError(
            "--publish-retained supports only compiler cache views: "
            + ", ".join(unsupported)
        )
    return tuple(view for view in _COMPILER_CACHE_IMPORT_VIEWS if view in set(selected))


def _run_index(
    args: argparse.Namespace,
    *,
    selected_views: Sequence[str] | None = None,
) -> int:
    retained_selection = _retained_index_selection(args)
    retained_views = None
    if retained_selection is not None:
        retained_views = _require_retained_index_views(
            list(selected_views)
            if selected_views is not None
            else _selected_views_for_args(args)
        )
    repo_path = resolve_repo_path(args.repo)
    resolved_selection = _resolve_repository_source_selection(repo_path, args)
    languages = _selected_languages(
        repo_path,
        args.language,
        source_selection=resolved_selection.selection,
    )
    views = (
        list(retained_views)
        if retained_views is not None
        else (
            list(selected_views)
            if selected_views is not None
            else _selected_views_for_args(args)
        )
    )
    embedding_route = None
    if "vector" in views:
        embedding_route = _embedding_route_for_args(args)
        _check_view_dependencies(views, embedding_provider=embedding_route.provider)
        try:
            embedding_route.credential()
        except ValueError as exc:
            raise CLIError(str(exc)) from exc
    else:
        _check_view_dependencies(views)
    index_kwargs = {
        "languages": languages,
        "views": views,
        "source_selection": resolved_selection.selection,
        "rebuild": args.rebuild,
    }
    if embedding_route is not None:
        index_kwargs.update(
            embedding_provider=embedding_route.provider,
            embedding_model=embedding_route.model,
            embedding_dimension=embedding_route.dimension,
            embedding_endpoint=embedding_route.endpoint,
            embedding_credential_env=embedding_route.credential_env,
        )
    if retained_selection is not None:
        return _run_index_retained(
            args,
            repo_path=repo_path,
            selection=retained_selection,
            compiler_kwargs={
                key: value for key, value in index_kwargs.items() if key != "rebuild"
            },
        )
    manifest, failed = index_repository(repo_path, **index_kwargs)
    _print_index_summary(manifest, views)
    if failed:
        print(
            f"Failed views: {', '.join(failed)}",
            file=sys.stderr,
        )
        return 1
    return 0


def _mcp_context_mode(args: argparse.Namespace) -> str:
    """Select one MCP startup mode before module or filesystem access."""

    runtime_probe = bool(getattr(args, "runtime_probe", False))
    explicit_path = getattr(args, "path", None) is not None
    artifact = getattr(args, "artifact", None)
    repo = getattr(args, "repo", None)
    repository = getattr(args, "repository", None)
    retained_values = {
        "--catalog": getattr(args, "catalog", None),
        "--cas-root": getattr(args, "cas_root", None),
        "--workspace-root": getattr(args, "workspace_root", None),
        "--output": getattr(args, "output", None),
        "--namespace": getattr(args, "namespace", None),
        "--ref": getattr(args, "ref", None),
        "--snapshot": getattr(args, "snapshot", None),
        "--expected-generation": getattr(args, "expected_generation", None),
    }
    retained_requested = any(value is not None for value in retained_values.values())
    any_context = (
        explicit_path
        or artifact is not None
        or repo is not None
        or repository is not None
        or retained_requested
    )
    if runtime_probe:
        if any_context:
            raise CLIError("--runtime-probe cannot be combined with MCP context input")
        return "probe"
    if retained_requested:
        if (
            retained_values["--snapshot"] is not None
            and retained_values["--expected-generation"] is not None
        ):
            raise CLIError("--expected-generation requires retained ref selection")
        if explicit_path or artifact is not None:
            raise CLIError(
                "retained MCP storage cannot be combined with a manifest or --artifact"
            )
        missing = [
            option
            for option in (
                "--catalog",
                "--cas-root",
                "--workspace-root",
                "--output",
            )
            if retained_values[option] is None
        ]
        if repository is None:
            missing.append("--repository")
        if missing:
            raise CLIError("retained MCP storage requires " + ", ".join(missing))
        return "retained"
    if artifact is not None:
        if explicit_path:
            raise CLIError("choose either a manifest or --artifact, not both")
        return "artifact"
    if repo is not None:
        raise CLIError("--repo requires --artifact or retained MCP storage")
    if repository is not None:
        raise CLIError("--repo and --repository require --artifact")
    return "manifest"


def _run_mcp(args: argparse.Namespace) -> int:
    mode = _mcp_context_mode(args)
    runtime_probe = mode == "probe"
    if runtime_probe:
        _require_modules(
            ("mcp", "igraph", "google.protobuf"),
            extra="graph,mcp",
            feature="the CodeGraph MCP server",
        )
    else:
        _require_modules(("mcp",), extra="mcp", feature="the MCP server")
    from .mcp.server import main as mcp_main

    if runtime_probe:
        print("codenib codegraph mcp runtime ready")
        return 0

    if mode == "retained":
        return _run_mcp_retained(args)
    if mode == "artifact":
        command = [
            "--artifact",
            str(Path(os.path.abspath(os.fspath(Path(args.artifact).expanduser())))),
        ]
        if args.repo is not None:
            command.extend(("--repo", str(resolve_repo_path(args.repo))))
        command.extend(
            (
                "--log-level",
                args.log_level,
                "--tool-surface",
                args.tool_surface,
            )
        )
        if args.repository:
            command.extend(("--repository", args.repository))
        mcp_main(command)
    else:
        manifest_path = resolve_manifest_path(args.path or ".")
        mcp_main(
            [
                str(manifest_path),
                "--log-level",
                args.log_level,
                "--tool-surface",
                args.tool_surface,
            ]
        )
    return 0


def _run_mcp_retained(args: argparse.Namespace) -> int:
    """Cold-start MCP from one retained ref or immutable snapshot."""

    from . import LocalWorkspaceProvider
    from ._atomic_directory import (
        _annotate_secondary_error,
        _OrderedAction,
        _run_context_with_cleanup_actions,
    )
    from .artifacts.runtime import SourceBindingCleanupOwner
    from .mcp.retained_context import (
        RetainedServerContextOwner,
        load_retained_server_context_ref,
        load_retained_server_context_snapshot,
    )
    from .mcp.server import serve_retained_context
    from .source_fingerprint import pin_repository_source_root
    from .storage import LocalCAS, SQLiteCatalog

    repository, namespace = _retained_materialization_identity(
        args.repository,
        args.namespace or "default",
    )
    ref_name, snapshot_id, expected_generation = _retained_materialization_selector(
        ref_name=args.ref,
        snapshot_id=args.snapshot,
        expected_generation=args.expected_generation,
    )
    repo_path = _retained_mcp_repository_path(getattr(args, "repo", None))
    catalog_path, cas_root, workspace_root, output = _retained_materialization_paths(
        args,
        repo_path=repo_path,
    )
    runtime_owner = RetainedServerContextOwner()
    topology_owner = _RetainedMaterializationResourceOwner()
    object_store_owner = _RetainedMaterializationResourceOwner()
    catalog_owner = _RetainedMaterializationResourceOwner()
    repository_authority_owner = SourceBindingCleanupOwner()
    runtime_cleanup = (
        _OrderedAction(
            label="retained MCP runtime cleanup also failed",
            action=runtime_owner.close,
            complete=lambda: runtime_owner.closed,
            retry_incomplete="cancellation",
            incomplete_owner=runtime_owner,
        ),
    )
    storage_cleanup = (
        _OrderedAction(
            label="retained SQLite catalog cleanup also failed",
            action=catalog_owner.close,
            complete=lambda: catalog_owner.closed,
            retry_incomplete="cancellation",
            incomplete_owner=catalog_owner,
        ),
        _OrderedAction(
            label="retained local CAS cleanup also failed",
            action=object_store_owner.close,
            complete=lambda: object_store_owner.closed,
            retry_incomplete="cancellation",
            incomplete_owner=object_store_owner,
        ),
        _OrderedAction(
            label="retained path authority cleanup also failed",
            action=topology_owner.close,
            complete=lambda: topology_owner.closed,
            retry_incomplete="cancellation",
            incomplete_owner=topology_owner,
        ),
        _OrderedAction(
            label="retained repository authority cleanup also failed",
            action=repository_authority_owner.close,
            complete=lambda: repository_authority_owner.closed,
            retry_incomplete="cancellation",
            incomplete_owner=repository_authority_owner,
        ),
    )
    try:
        with _run_context_with_cleanup_actions(runtime_cleanup):
            with _run_context_with_cleanup_actions(storage_cleanup):
                repository_authority = (
                    None
                    if repo_path is None
                    else pin_repository_source_root(
                        repo_path,
                        _source_owner=repository_authority_owner.retain,
                    )
                )
                base_provider = LocalWorkspaceProvider(workspace_root)
                base_provider.require_support()
                topology = topology_owner.acquire(
                    lambda: _require_retained_materialization_topology(
                        catalog_path,
                        cas_root,
                        workspace_root,
                        output,
                        repo_path=repo_path,
                        repository_authority=repository_authority,
                    )
                )
                provider = _RetainedTopologyWorkspaceProvider(
                    base_provider,
                    topology,
                )
                topology.verify()
                object_store = object_store_owner.acquire(
                    lambda: LocalCAS(
                        topology.cas_root,
                        require_preprovisioned=True,
                    )
                )
                _require_retained_catalog_binding(
                    topology.catalog_path,
                    topology.catalog_identity,
                )
                catalog = catalog_owner.acquire(
                    lambda: SQLiteCatalog(
                        topology.catalog_path,
                        create=False,
                        expected_file_identity=topology.catalog_identity,
                    )
                )
                _require_retained_catalog_binding(
                    topology.catalog_path,
                    topology.catalog_identity,
                )
                topology.verify()
                if snapshot_id is None:
                    load_retained_server_context_ref(
                        repository,
                        output,
                        namespace_name=namespace,
                        ref_name=ref_name or "main",
                        expected_generation=expected_generation,
                        catalog=catalog,
                        object_store=object_store,
                        workspace_provider=provider,
                        runtime_owner=runtime_owner,
                        repo_path=topology.repo_path,
                        expected_root_authority=topology.repository_authority,
                    )
                else:
                    load_retained_server_context_snapshot(
                        repository,
                        snapshot_id,
                        output,
                        namespace_name=namespace,
                        catalog=catalog,
                        object_store=object_store,
                        workspace_provider=provider,
                        runtime_owner=runtime_owner,
                        repo_path=topology.repo_path,
                        expected_root_authority=topology.repository_authority,
                    )
                topology.verify_bindings()
            serve_retained_context(
                runtime_owner,
                log_level=args.log_level,
                tool_surface=args.tool_surface,
            )
        return 0
    except BaseException as primary_error:  # noqa: B036 - preserve cancellation
        try:
            _warn_possible_retained_publication(output)
        except BaseException as warning_error:  # noqa: B036 - diagnostic only
            _annotate_secondary_error(
                primary_error,
                "retained MCP publication warning also failed",
                warning_error,
            )
        if isinstance(primary_error, CLIError):
            raise
        if isinstance(
            primary_error,
            (OSError, RuntimeError, ValueError, sqlite3.Error),
        ):
            wrapped = CLIError(str(primary_error))
            _inherit_publication_cleanup_owners(wrapped, primary_error)
            raise wrapped from primary_error
        raise


def _run_export(args: argparse.Namespace) -> int:
    repo_path = resolve_repo_path(args.repo)
    manifest_path = resolve_manifest_path(str(repo_path))
    output_dir = (
        Path(os.path.abspath(os.fspath(Path(args.output).expanduser())))
        if args.output
        else manifest_path.parent / "static-wiki"
    )

    from .web.static_export import export_static_wiki

    try:
        result = export_static_wiki(
            repo_path,
            manifest_path,
            output_dir,
            frontend_dir=args.frontend_dir,
            base_path=args.base_path,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise CLIError(str(exc)) from exc

    print(f"Static Wiki: {result.output_dir}")
    print(f"Repository:  {result.repo_id}")
    print(f"Pages:       {result.page_count}")
    print(f"Manifest:    {result.manifest_path}")
    return 0


def _default_distribution_dir(manifest_path: Path, name: str, commit: str) -> Path:
    identity = (commit or "working-tree")[:12]
    return manifest_path.parent.parent / "exports" / f"{name}-{identity}"


def _publication_environment(credential_env: str | None = None) -> dict[str, str]:
    environment = dict(os.environ)
    selected = credential_env or environment.get("CODENIB_EMBEDDING_API_KEY_ENV")
    if selected and environment.get(selected):
        environment["CODENIB_PUBLICATION_CREDENTIAL_SECRET"] = environment[selected]
    return environment


def _require_clean_artifact_checkout(
    repo_path: Path,
    *,
    source_selection: RepositorySourceSelection = (DEFAULT_REPOSITORY_SOURCE_SELECTION),
) -> None:
    """Require portable artifacts to describe the checked-out Git commit."""

    from .source_fingerprint import repository_source_is_dirty

    if repository_source_is_dirty(repo_path, selection=source_selection):
        raise CLIError(
            "portable context artifacts require a clean Git checkout at HEAD. "
            "Commit or stash source-visible changes, or use `codenib wiki` or "
            "`codenib export` to inspect the current working tree."
        )


def _paths_overlap(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def _canonical_publication_path(value: str | Path) -> Path:
    """Resolve existing output ancestors before validation and publication."""

    lexical = Path(os.path.abspath(os.fspath(Path(value).expanduser())))
    try:
        return lexical.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise CLIError(
            f"publication output path could not be resolved safely: {lexical}"
        ) from exc


def _validate_publish_outputs(
    repo_path: Path,
    *,
    site_output: Path | None,
    context_output: Path | None,
) -> None:
    """Reject destructive publication paths before any index work starts."""

    site_identity = (
        _canonical_publication_path(site_output) if site_output is not None else None
    )
    context_identity = (
        _canonical_publication_path(context_output)
        if context_output is not None
        else None
    )
    if (
        site_identity is not None
        and context_identity is not None
        and _paths_overlap(site_identity, context_identity)
    ):
        raise CLIError("Wiki and context artifact outputs must not overlap")

    from .paths import repo_index_dir

    protected = (
        (_canonical_publication_path(repo_path), "repository"),
        (
            _canonical_publication_path(repo_index_dir(repo_path)),
            "index state",
        ),
    )
    for output, output_name in (
        (site_identity, "Wiki"),
        (context_identity, "context artifact"),
    ):
        if output is None:
            continue
        for source, source_name in protected:
            if _paths_overlap(output, source):
                raise CLIError(
                    f"{output_name} output must not overlap the {source_name}: "
                    f"{output}"
                )


def _run_artifact_pack(args: argparse.Namespace) -> int:
    repo_path = resolve_repo_path(args.repo)
    _artifact_checkout_commit(repo_path, None)
    manifest_path = resolve_manifest_path(str(repo_path))
    from .artifacts import stage_context_artifact
    from .compiler.manifest import RepoManifest

    manifest = RepoManifest.load(manifest_path)
    _require_clean_artifact_checkout(
        repo_path,
        source_selection=(
            manifest.source_selection or DEFAULT_REPOSITORY_SOURCE_SELECTION
        ),
    )
    output_dir = (
        Path(os.path.abspath(os.fspath(Path(args.output).expanduser())))
        if args.output
        else _default_distribution_dir(
            manifest_path,
            "context",
            manifest.commit,
        )
    )
    selected_views = _split_values(args.view) or None
    try:
        result = stage_context_artifact(
            repo_path,
            manifest_path,
            output_dir,
            repository=args.repository or os.environ.get("GITHUB_REPOSITORY"),
            views=selected_views,
            environ=_publication_environment(),
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise CLIError(str(exc)) from exc

    print(f"Context artifact: {result.output_dir}")
    print(f"Repository:       {result.repository}")
    print(f"Commit:           {result.commit or 'working tree'}")
    print(f"Views:            {', '.join(result.views)}")
    print(f"Manifest:         {result.metadata_path}")
    return 0


def _artifact_checkout_commit(repo_path: Path, explicit: str | None) -> str:
    from .compiler.checkout_identity import checkout_commit

    commit = (explicit or checkout_commit(repo_path) or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise CLIError(
            "artifact operations require a clean Git checkout with a full "
            "resolved HEAD"
        )
    return commit


def _run_artifact_verify(args: argparse.Namespace) -> int:
    from .artifacts import bind_context_artifact, verify_context_artifact

    try:
        if args.repo:
            with bind_context_artifact(
                args.path,
                resolve_repo_path(args.repo),
                expected_repository=args.repository,
                expected_commit=args.commit,
            ) as binding:
                artifact = binding.artifact
                checkout = binding.repo_path
                summary = (
                    artifact.root,
                    artifact.repository,
                    artifact.commit,
                    tuple(artifact.views),
                    artifact.file_count,
                    artifact.byte_count,
                    checkout,
                )
        else:
            artifact = verify_context_artifact(
                args.path,
                expected_repository=args.repository,
                expected_commit=args.commit,
            )
            summary = (
                artifact.root,
                artifact.repository,
                artifact.commit,
                tuple(artifact.views),
                artifact.file_count,
                artifact.byte_count,
                None,
            )
    except (OSError, RuntimeError, ValueError) as exc:
        raise CLIError(str(exc)) from exc

    root, repository, commit, views, file_count, byte_count, checkout = summary
    print(f"Context artifact: {root}")
    print(f"Repository:       {repository}")
    print(f"Commit:           {commit}")
    print(f"Views:            {', '.join(views)}")
    print(f"Files:            {file_count}")
    print(f"Bytes:            {byte_count}")
    print(f"Checkout:         {checkout or 'not checked'}")
    return 0


def _run_artifact_fetch(args: argparse.Namespace) -> int:
    from .artifacts import bind_context_artifact, fetch_github_context_artifact

    repo_path = resolve_repo_path(args.repo)
    commit = _artifact_checkout_commit(repo_path, args.commit)
    try:
        result = fetch_github_context_artifact(
            args.repository,
            commit,
            output_dir=args.output,
            artifact_name=args.artifact_name,
            token_env=args.token_env,
            api_url=args.github_api_url,
            force=args.force,
        )
        with bind_context_artifact(
            result.artifact.root,
            repo_path,
            expected_repository=args.repository,
            expected_commit=commit,
        ) as binding:
            artifact_summary = (
                binding.artifact.root,
                binding.artifact.repository,
                binding.artifact.commit,
                tuple(binding.artifact.views),
            )
    except (OSError, RuntimeError, ValueError) as exc:
        raise CLIError(str(exc)) from exc

    state = "downloaded" if result.downloaded else "cached"
    artifact_root, repository, artifact_commit, views = artifact_summary
    print(f"Context artifact: {artifact_root}")
    print(f"Repository:       {repository}")
    print(f"Commit:           {artifact_commit}")
    print(f"Views:            {', '.join(views)}")
    print(f"State:            {state}")
    if result.record is not None:
        print(f"GitHub artifact:  {result.record.artifact_id}")
    return 0


def _retained_materialization_identity(
    repository: object,
    namespace: object,
) -> tuple[str, str]:
    from .compiler.snapshot_store import normalize_repo
    from .storage.models import NamespaceIdentity, RepositoryIdentity

    if type(repository) is not str:
        raise CLIError("retained repository must be exact text")
    if (
        not repository
        or len(repository) > 32_768
        or "\x00" in repository
        or repository != repository.strip()
    ):
        raise CLIError("retained repository must be a canonical owner/repository slug")
    try:
        normalized_repository = normalize_repo(repository)
    except ValueError as exc:
        raise CLIError(
            "retained repository must be a canonical owner/repository slug"
        ) from exc
    if (
        normalized_repository != repository
        or _RETAINED_REPOSITORY_RE.fullmatch(repository) is None
    ):
        raise CLIError("retained repository must be a canonical owner/repository slug")
    if type(namespace) is not str:
        raise CLIError("retained namespace must be exact text")
    try:
        namespace_identity = NamespaceIdentity(namespace)
        RepositoryIdentity(
            namespace_id=namespace_identity.namespace_id,
            repository_key=repository,
        )
    except (RuntimeError, ValueError) as exc:
        raise CLIError(str(exc)) from exc
    if namespace_identity.name != namespace:
        raise CLIError("retained namespace must be canonical")
    return repository, namespace


def _retained_materialization_selector(
    *,
    ref_name: object,
    snapshot_id: object,
    expected_generation: object,
) -> tuple[str | None, str | None, int | None]:
    generation = _optional_int(
        expected_generation,
        source="expected ref generation",
    )
    if generation is not None and generation > _SIGNED_INT64_MAX:
        raise CLIError("expected ref generation exceeds signed 64-bit range")
    if snapshot_id is not None:
        if generation is not None:
            raise CLIError("--expected-generation requires ref selection")
        if (
            type(snapshot_id) is not str
            or not snapshot_id
            or snapshot_id != snapshot_id.strip()
            or "\x00" in snapshot_id
            or len(snapshot_id) > 32_768
        ):
            raise CLIError("retained snapshot ID is not canonical")
        return None, snapshot_id, None
    selected_ref = "main" if ref_name is None else ref_name
    if (
        type(selected_ref) is not str
        or not selected_ref
        or selected_ref != selected_ref.strip()
        or "\x00" in selected_ref
        or len(selected_ref) > 32_768
    ):
        raise CLIError("retained ref name is not canonical")
    return selected_ref, None, generation


def _lexical_cli_path(value: object, *, label: str) -> Path:
    if type(value) is not str or not value:
        raise CLIError(f"{label} path is required")
    if "\x00" in value:
        raise CLIError(f"{label} path is invalid")
    try:
        return Path(os.path.abspath(os.fspath(Path(value).expanduser())))
    except (OSError, RuntimeError, ValueError) as exc:
        raise CLIError(f"{label} path is invalid") from exc


def _retained_mcp_repository_path(value: object) -> Path | None:
    """Freeze one explicit retained source checkout before provider work."""

    if value is None:
        return None
    path = _lexical_cli_path(value, label="repository source")
    try:
        metadata = path.lstat()
    except (OSError, RuntimeError, ValueError) as exc:
        raise CLIError(
            "repository source must resolve to one existing real directory"
        ) from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or not metadata.st_dev
        or not metadata.st_ino
    ):
        raise CLIError("repository source must resolve to one existing real directory")
    return path


def _retained_materialization_paths(
    args: argparse.Namespace,
    *,
    repo_path: Path | None = None,
) -> tuple[Path, ...]:
    """Freeze lexical paths without touching the destination namespace."""

    catalog_path = _lexical_cli_path(args.catalog, label="catalog")
    cas_root = _lexical_cli_path(args.cas_root, label="CAS root")
    workspace_root = _lexical_cli_path(args.workspace_root, label="workspace root")
    output = _lexical_cli_path(args.output, label="output")
    try:
        output_relative = output.relative_to(workspace_root)
    except ValueError as exc:
        raise CLIError("output must be below the workspace root") from exc
    if not output_relative.parts:
        raise CLIError("output must be a child of the workspace root")
    comparisons = (
        (catalog_path, "catalog", cas_root, "CAS root"),
        (catalog_path, "catalog", workspace_root, "workspace root"),
        (cas_root, "CAS root", workspace_root, "workspace root"),
    )
    if repo_path is not None:
        comparisons = (
            *comparisons,
            (repo_path, "repository source", catalog_path, "catalog"),
            (repo_path, "repository source", cas_root, "CAS root"),
            (repo_path, "repository source", output, "output"),
            (repo_path, "repository source", workspace_root, "workspace root"),
        )
    for first, first_label, second, second_label in comparisons:
        if _paths_overlap(first, second):
            raise CLIError(f"{first_label} must not overlap the {second_label}")
    return catalog_path, cas_root, workspace_root, output


def _retained_open_directory_identity(authority: object) -> tuple[int, ...]:
    """Read one stable directory object identity from its opened authority."""

    from ._atomic_directory import publication_parent_identity

    try:
        resource = authority.resource  # type: ignore[attr-defined]
        observed = publication_parent_identity(resource)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise CLIError("directory authority identity could not be verified") from exc
    if (
        type(observed) is not tuple
        or len(observed) < 2
        or any(type(value) is not int for value in observed)
        or observed[0] < 1
        or observed[1] < 1
    ):
        raise CLIError("directory authority has no reliable filesystem identity")
    return observed


@dataclass(frozen=True, slots=True)
class _RetainedDirectoryBinding:
    label: str
    path: Path
    identity: tuple[int, ...]
    owner: object = field(repr=False, compare=False)

    @property
    def authority(self) -> object:
        authority = self.owner.authority  # type: ignore[attr-defined]
        if authority is None:
            raise CLIError(f"{self.label} directory authority is closed")
        return authority

    def verify(self) -> tuple[int, ...]:
        authority = self.authority
        try:
            authority.verify_path_binding()  # type: ignore[attr-defined]
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise CLIError(f"{self.label} directory binding changed") from exc
        observed = _retained_open_directory_identity(authority)
        if observed != self.identity:
            raise CLIError(f"{self.label} directory authority identity changed")
        return observed


def _retained_descendant_paths(ancestor: Path, descendant: Path) -> tuple[Path, ...]:
    try:
        relative = descendant.relative_to(ancestor)
    except ValueError as exc:
        raise CLIError("physical storage ancestry changed") from exc
    current = ancestor
    paths: list[Path] = []
    for component in relative.parts:
        current = current / component
        paths.append(current)
    return tuple(paths)


@dataclass(frozen=True, slots=True)
class _RetainedLinuxMountMapping:
    mount_id: int
    device: str
    root: PurePosixPath
    mount_point: Path


@dataclass(frozen=True, slots=True)
class _RetainedLinuxPhysicalPath:
    device: str
    path: PurePosixPath


def _retained_physical_paths_overlap(
    first: _RetainedLinuxPhysicalPath,
    second: _RetainedLinuxPhysicalPath,
) -> bool:
    if first.device != second.device:
        return False
    return (
        first.path == second.path
        or first.path in second.path.parents
        or second.path in first.path.parents
    )


def _retained_linux_mount_mappings() -> tuple[_RetainedLinuxMountMapping, ...]:
    """Snapshot bounded Linux mount roots needed for physical path mapping."""

    if not sys.platform.startswith("linux"):
        return ()
    from ._atomic_directory import _mountinfo_path

    mappings: list[_RetainedLinuxMountMapping] = []
    try:
        with open("/proc/self/mountinfo", "rb") as mountinfo:
            for index, line in enumerate(mountinfo):
                if index >= _MAX_RETAINED_MOUNTINFO_ENTRIES:
                    raise CLIError("Linux mount table exceeds its safe entry limit")
                if len(line) > _MAX_RETAINED_MOUNTINFO_LINE_BYTES:
                    raise CLIError("Linux mount table contains an oversized entry")
                fields = line.split()
                try:
                    separator = fields.index(b"-")
                except ValueError as exc:
                    raise CLIError(
                        "Linux mount table contains a malformed entry"
                    ) from exc
                if separator < 6 or len(fields) < separator + 4:
                    raise CLIError("Linux mount table contains a malformed entry")
                try:
                    mount_id = int(fields[0])
                except ValueError as exc:
                    raise CLIError(
                        "Linux mount table contains an invalid mount ID"
                    ) from exc
                device = os.fsdecode(fields[2])
                device_parts = device.split(":", 1)
                if (
                    mount_id < 1
                    or len(device_parts) != 2
                    or any(not part.isdigit() for part in device_parts)
                ):
                    raise CLIError("Linux mount table contains an invalid identity")
                root_text = posixpath.normpath(_mountinfo_path(os.fsdecode(fields[3])))
                mount_text = os.path.normpath(_mountinfo_path(os.fsdecode(fields[4])))
                root = PurePosixPath(root_text)
                mount_point = Path(mount_text)
                # nsfs and similar pseudo mounts expose opaque roots such as
                # ``net:[id]``. They cannot contain a regular SQLite catalog
                # and have no filesystem-relative suffix to map.
                if not root.is_absolute():
                    continue
                if not mount_point.is_absolute():
                    raise CLIError("Linux mount table contains a relative path")
                mappings.append(
                    _RetainedLinuxMountMapping(
                        mount_id=mount_id,
                        device=device,
                        root=root,
                        mount_point=mount_point,
                    )
                )
    except CLIError:
        raise
    except (OSError, UnicodeError) as exc:
        raise CLIError("Linux mount table could not be inspected safely") from exc
    if not mappings:
        raise CLIError("Linux mount table is empty")
    return tuple(mappings)


def _retained_linux_physical_path(
    path: Path,
    mappings: tuple[_RetainedLinuxMountMapping, ...],
) -> _RetainedLinuxPhysicalPath | None:
    candidates = tuple(
        mapping
        for mapping in mappings
        if path == mapping.mount_point or mapping.mount_point in path.parents
    )
    if not candidates:
        return None
    deepest = max(len(mapping.mount_point.parts) for mapping in candidates)
    selected_candidates = tuple(
        mapping for mapping in candidates if len(mapping.mount_point.parts) == deepest
    )
    if len(selected_candidates) != 1:
        raise CLIError("Linux mount table contains an ambiguous stacked mapping")
    selected = selected_candidates[0]
    try:
        relative = path.relative_to(selected.mount_point)
    except ValueError as exc:  # pragma: no cover - candidates prove containment
        raise CLIError("Linux mount mapping changed") from exc
    internal = PurePosixPath(
        posixpath.normpath(
            posixpath.join(selected.root.as_posix(), relative.as_posix())
        )
    )
    if not internal.is_absolute():  # pragma: no cover - normalized absolute root
        raise CLIError("Linux mount mapping produced a relative physical path")
    return _RetainedLinuxPhysicalPath(selected.device, internal)


def _retained_alias_file_identity(path: Path) -> tuple[int, int] | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except (OSError, RuntimeError, ValueError) as exc:
        raise CLIError("mapped catalog alias could not be inspected safely") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or not metadata.st_dev
        or not metadata.st_ino
    ):
        raise CLIError("mapped catalog alias is not one real regular file")
    return int(metadata.st_dev), int(metadata.st_ino)


def _require_catalog_outside_retained_roots(
    catalog_path: Path,
    catalog_identity: tuple[int, int, int],
    bindings: tuple[_RetainedDirectoryBinding, ...],
    mappings: tuple[_RetainedLinuxMountMapping, ...],
) -> None:
    """Deny one exact catalog suffix reachable through a protected mount."""

    catalog_physical = _retained_linux_physical_path(catalog_path, mappings)
    if catalog_physical is None:
        return
    expected_device = f"{os.major(catalog_identity[0])}:{os.minor(catalog_identity[0])}"
    if catalog_physical.device != expected_device:
        raise CLIError("catalog mount mapping differs from its file authority")
    for binding in bindings:
        root_physical = _retained_linux_physical_path(binding.path, mappings)
        if root_physical is None or root_physical.device != catalog_physical.device:
            continue
        try:
            suffix = catalog_physical.path.relative_to(root_physical.path)
        except ValueError:
            continue
        if (
            not suffix.parts
            or len(suffix.parts) > _MAX_RETAINED_ALIAS_COMPONENTS
            or len(os.fsencode(suffix.as_posix())) > _MAX_RETAINED_ALIAS_PATH_BYTES
            or any(component in {"", ".", ".."} for component in suffix.parts)
        ):
            raise CLIError("catalog mount alias has an unsafe relative path")
        candidate = binding.path.joinpath(*suffix.parts)
        candidate_physical = _retained_linux_physical_path(candidate, mappings)
        if candidate_physical != catalog_physical:
            continue
        candidate_identity = _retained_alias_file_identity(candidate)
        if candidate_identity is None:
            raise CLIError("mapped catalog alias disappeared during validation")
        if candidate_identity != catalog_identity[:2]:
            raise CLIError("mapped catalog alias identity changed during validation")
        raise CLIError(f"catalog is physically reachable below the {binding.label}")


def _require_retained_mount_topology(
    catalog_path: Path,
    catalog_identity: tuple[int, int, int],
    cas: _RetainedDirectoryBinding,
    workspace: _RetainedDirectoryBinding,
    output_parent: _RetainedDirectoryBinding,
) -> None:
    """Reject mount crossings that can re-enter another protected tree."""

    from ._atomic_directory import _path_is_mount_point

    mappings = _retained_linux_mount_mappings()
    mount_points = frozenset(
        os.path.normpath(os.fspath(mapping.mount_point)) for mapping in mappings
    )
    if os.path.normpath(os.path.realpath(catalog_path)) in mount_points:
        raise CLIError("catalog must not be a file mount point")
    _require_catalog_outside_retained_roots(
        catalog_path,
        catalog_identity,
        (cas, workspace, output_parent),
        mappings,
    )
    for candidate in _retained_descendant_paths(
        workspace.path,
        output_parent.path,
    ):
        if _path_is_mount_point(candidate, mount_points=mount_points):
            raise CLIError("output parent must not cross a mount point")

    cas_physical = _retained_linux_physical_path(cas.path, mappings)
    workspace_physical = _retained_linux_physical_path(workspace.path, mappings)
    if (
        cas_physical is not None
        and workspace_physical is not None
        and _retained_physical_paths_overlap(cas_physical, workspace_physical)
    ):
        raise CLIError("retained roots must not traverse a same-device mount alias")


def _require_retained_repository_topology(
    repo_path: Path,
    catalog_path: Path,
    cas_root: Path,
    workspace_root: Path,
    output: Path,
) -> None:
    """Deny lexical aliases, inode aliases, and mapped physical source aliases."""

    for storage, storage_label in (
        (catalog_path, "catalog"),
        (cas_root, "CAS root"),
        (output, "output"),
        (workspace_root, "workspace root"),
    ):
        if _paths_overlap(repo_path, storage):
            raise CLIError(
                f"repository source must not physically overlap the {storage_label}"
            )

    repository_ancestry = _retained_directory_ancestry(
        repo_path,
        label="repository source",
    )
    for storage_ancestry, storage_label in (
        (
            _retained_directory_ancestry(
                catalog_path.parent,
                label="catalog parent",
            ),
            "catalog",
        ),
        (
            _retained_directory_ancestry(cas_root, label="CAS root"),
            "CAS root",
        ),
        (
            _retained_directory_ancestry(
                output.parent,
                label="output parent",
            ),
            "output",
        ),
        (
            _retained_directory_ancestry(
                workspace_root,
                label="workspace root",
            ),
            "workspace root",
        ),
    ):
        _require_distinct_directory_ancestry(
            repository_ancestry,
            "repository source",
            storage_ancestry,
            storage_label,
        )

    mappings = _retained_linux_mount_mappings()
    repository_physical = _retained_linux_physical_path(repo_path, mappings)
    for storage, storage_label in (
        (catalog_path, "catalog"),
        (cas_root, "CAS root"),
        (output, "output"),
        (workspace_root, "workspace root"),
    ):
        storage_physical = _retained_linux_physical_path(storage, mappings)
        if (
            repository_physical is not None
            and storage_physical is not None
            and _retained_physical_paths_overlap(
                repository_physical,
                storage_physical,
            )
        ):
            raise CLIError(
                "repository source must not traverse a physical "
                f"{storage_label} alias"
            )


@dataclass(slots=True)
class _RetainedMaterializationTopology:
    catalog_path: Path
    catalog_identity: tuple[int, int, int]
    cas_root: Path
    workspace_root: Path
    output: Path
    cas_binding: _RetainedDirectoryBinding
    workspace_binding: _RetainedDirectoryBinding
    output_parent_binding: _RetainedDirectoryBinding
    repo_path: Path | None = None
    repository_identity: tuple[object, ...] | None = None
    repository_authority: object | None = field(
        default=None,
        repr=False,
    )
    _closed: bool = field(default=False, init=False, repr=False)

    @property
    def closed(self) -> bool:
        if not self._closed and all(
            binding.owner.closed  # type: ignore[attr-defined]
            for binding in self._bindings
        ):
            self._closed = True
        return self._closed

    @property
    def _bindings(self) -> tuple[_RetainedDirectoryBinding, ...]:
        return (
            self.cas_binding,
            self.workspace_binding,
            self.output_parent_binding,
        )

    def verify_bindings(self) -> None:
        if self.closed:
            raise CLIError("retained materialization topology authority is closed")
        cas_identity = self.cas_binding.verify()
        workspace_identity = self.workspace_binding.verify()
        output_parent_identity = self.output_parent_binding.verify()
        if cas_identity == workspace_identity:
            raise CLIError("CAS root and workspace root are the same physical object")
        if cas_identity == output_parent_identity:
            raise CLIError("output parent physically aliases the CAS root")
        if (
            self.output_parent_binding.path != self.workspace_binding.path
            and workspace_identity == output_parent_identity
        ):
            raise CLIError("output parent physically aliases the workspace root")
        _require_retained_mount_topology(
            self.catalog_path,
            self.catalog_identity,
            self.cas_binding,
            self.workspace_binding,
            self.output_parent_binding,
        )
        repository_values = (
            self.repo_path,
            self.repository_identity,
            self.repository_authority,
        )
        if all(value is None for value in repository_values):
            return
        if any(value is None for value in repository_values):
            raise CLIError("retained repository source topology is incomplete")
        repo_path = self.repo_path
        repository_identity = self.repository_identity
        repository_authority = self.repository_authority
        if (
            repo_path is None
            or repository_identity is None
            or repository_authority is None
        ):  # pragma: no cover - guarded above for type narrowing
            raise CLIError("retained repository source topology is incomplete")
        try:
            repository_authority.verify()  # type: ignore[attr-defined]
            observed_repository_path = repository_authority.root  # type: ignore[attr-defined]
            observed_repository_identity = (  # type: ignore[attr-defined]
                repository_authority.root_identity
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise CLIError("repository source authority changed") from exc
        if observed_repository_path != repo_path:
            raise CLIError("repository source authority path changed")
        if observed_repository_identity != repository_identity:
            raise CLIError("repository source authority identity changed")
        for storage_identity, storage_label in (
            (cas_identity, "CAS root"),
            (workspace_identity, "workspace root"),
            (output_parent_identity, "output parent"),
        ):
            if observed_repository_identity == tuple(storage_identity[:2]):
                raise CLIError(
                    f"repository source physically aliases the {storage_label}"
                )
        _require_retained_repository_topology(
            repo_path,
            self.catalog_path,
            self.cas_root,
            self.workspace_root,
            self.output,
        )
        try:
            repository_authority.verify()  # type: ignore[attr-defined]
            if (  # type: ignore[attr-defined]
                repository_authority.root != repo_path
                or repository_authority.root_identity != repository_identity
            ):
                raise CLIError("repository source authority changed")
        except CLIError:
            raise
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise CLIError("repository source authority changed") from exc

    def verify(self) -> None:
        self.verify_bindings()
        try:
            output_metadata = self.output_parent_binding.authority.child_metadata(  # type: ignore[attr-defined]
                self.output.name,
                path=self.output,
                label="output",
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise CLIError("output cannot be inspected through its authority") from exc
        if output_metadata is not None:
            raise CLIError("output must be missing")

    def close(self) -> None:
        from ._atomic_directory import _annotate_secondary_error

        if self.closed:
            return
        primary_error: BaseException | None = None
        for binding in reversed(self._bindings):
            try:
                binding.owner.close()  # type: ignore[attr-defined]
            except BaseException as close_error:  # noqa: B036 - visit every owner
                if primary_error is None:
                    primary_error = close_error
                else:
                    _annotate_secondary_error(
                        primary_error,
                        "retained directory authority cleanup also failed",
                        close_error,
                    )
        self._closed = all(
            binding.owner.closed  # type: ignore[attr-defined]
            for binding in self._bindings
        )
        if primary_error is not None:
            raise primary_error


@dataclass(frozen=True, slots=True)
class _RetainedTopologyWorkspaceProvider:
    """Recheck retained path authorities before provider provisioning."""

    delegate: object
    topology: _RetainedMaterializationTopology

    def require_support(self) -> None:
        self.topology.verify()
        self.delegate.require_support()  # type: ignore[attr-defined]
        self.topology.verify()

    def run_workspace(
        self,
        request: object,
        *,
        receipt_owner: object,
        operation: object,
    ) -> object:
        self.topology.verify()
        result = self.delegate.run_workspace(  # type: ignore[attr-defined]
            request,
            receipt_owner=receipt_owner,
            operation=operation,
            _expected_parent_identity=self.topology.output_parent_binding.identity,
        )
        self.topology.verify_bindings()
        return result


def _resolved_retained_materialization_path(
    path: Path,
    *,
    label: str,
    strict: bool,
) -> Path:
    try:
        return path.resolve(strict=strict)
    except (OSError, RuntimeError, ValueError) as exc:
        raise CLIError(f"{label} path could not be resolved safely") from exc


def _retained_real_directory_identity(
    path: Path,
    *,
    label: str,
) -> tuple[int, int]:
    try:
        metadata = path.lstat()
    except (OSError, RuntimeError, ValueError) as exc:
        raise CLIError(f"{label} cannot be inspected safely") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or not metadata.st_dev
        or not metadata.st_ino
    ):
        raise CLIError(f"{label} must resolve to one real directory")
    return int(metadata.st_dev), int(metadata.st_ino)


def _retained_directory_ancestry(
    path: Path,
    *,
    label: str,
) -> tuple[tuple[Path, tuple[int, int]], ...]:
    ancestry: list[tuple[Path, tuple[int, int]]] = []
    current = path
    while True:
        ancestry.append(
            (
                current,
                _retained_real_directory_identity(current, label=label),
            )
        )
        parent = current.parent
        if parent == current:
            return tuple(ancestry)
        current = parent


def _require_distinct_directory_ancestry(
    first: tuple[tuple[Path, tuple[int, int]], ...],
    first_label: str,
    second: tuple[tuple[Path, tuple[int, int]], ...],
    second_label: str,
) -> None:
    for first_path, first_identity in first:
        for second_path, second_identity in second:
            if first_identity == second_identity and first_path != second_path:
                raise CLIError(
                    f"{first_label} must not physically alias the {second_label}"
                )


def _retained_catalog_file_identity(path: Path) -> tuple[int, int, int]:
    try:
        metadata = path.lstat()
    except (OSError, RuntimeError, ValueError) as exc:
        raise CLIError("catalog file cannot be inspected safely") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or not metadata.st_dev
        or not metadata.st_ino
        or metadata.st_nlink != 1
    ):
        raise CLIError("catalog must resolve to one single-linked regular file")
    return int(metadata.st_dev), int(metadata.st_ino), int(metadata.st_nlink)


def _require_retained_catalog_binding(
    path: Path,
    expected_identity: tuple[int, int, int],
) -> None:
    try:
        observed_identity = _retained_catalog_file_identity(path)
    except CLIError as exc:
        raise CLIError("catalog changed after physical topology validation") from exc
    if observed_identity != expected_identity:
        raise CLIError("catalog changed after physical topology validation")


def _require_retained_materialization_topology(
    catalog_path: Path,
    cas_root: Path,
    workspace_root: Path,
    output: Path,
    *,
    repo_path: Path | None = None,
    repository_authority: object | None = None,
) -> _RetainedMaterializationTopology:
    """Open and retain physical authorities for every mutable storage root."""

    from ._atomic_directory import (
        _annotate_secondary_error,
        _attach_publication_cleanup_owner,
        _open_publication_authority,
        _PublicationAuthorityOwner,
    )
    from .source_fingerprint import RepositorySourceRootAuthority

    if (repo_path is None) != (repository_authority is None):
        raise CLIError("retained repository source topology is incomplete")
    if (
        repository_authority is not None
        and type(repository_authority) is not RepositorySourceRootAuthority
    ):
        raise CLIError("retained repository source authority has an invalid type")

    try:
        output.lstat()
    except FileNotFoundError:
        pass
    except (OSError, RuntimeError, ValueError) as exc:
        raise CLIError("output cannot be inspected safely") from exc
    else:
        raise CLIError("output must be missing")

    resolved_catalog = _resolved_retained_materialization_path(
        catalog_path,
        label="catalog",
        strict=True,
    )
    resolved_cas = _resolved_retained_materialization_path(
        cas_root,
        label="CAS root",
        strict=True,
    )
    resolved_workspace = _resolved_retained_materialization_path(
        workspace_root,
        label="workspace root",
        strict=True,
    )
    catalog_identity = _retained_catalog_file_identity(resolved_catalog)
    try:
        workspace_metadata = resolved_workspace.lstat()
    except (OSError, RuntimeError, ValueError) as exc:
        raise CLIError("workspace root cannot be inspected safely") from exc
    if (
        not stat.S_ISDIR(workspace_metadata.st_mode)
        or stat.S_ISLNK(workspace_metadata.st_mode)
        or not workspace_metadata.st_dev
    ):
        raise CLIError("workspace root must resolve to one real directory")
    workspace_device = int(workspace_metadata.st_dev)
    try:
        relative_parent = output.parent.relative_to(workspace_root)
    except ValueError as exc:  # pragma: no cover - lexical preflight already binds it
        raise CLIError("output parent escaped the workspace root") from exc
    current_parent = workspace_root
    for component in relative_parent.parts:
        current_parent = current_parent / component
        try:
            metadata = current_parent.lstat()
        except FileNotFoundError as exc:
            raise CLIError("output parent must already exist") from exc
        except (OSError, RuntimeError, ValueError) as exc:
            raise CLIError("output parent cannot be inspected safely") from exc
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise CLIError("output parent must contain only existing real directories")
        if metadata.st_dev != workspace_device:
            raise CLIError("output parent must remain on the workspace filesystem")
    resolved_output_parent = _resolved_retained_materialization_path(
        current_parent,
        label="output parent",
        strict=True,
    )
    resolved_output = resolved_output_parent / output.name
    try:
        resolved_output_relative = resolved_output.relative_to(resolved_workspace)
    except ValueError as exc:
        raise CLIError("output must remain below the physical workspace root") from exc
    if not resolved_output_relative.parts:
        raise CLIError("output must be a child of the physical workspace root")
    repository_identity: tuple[object, ...] | None = None
    if repository_authority is not None:
        try:
            repository_authority.verify()
            pinned_repo = repository_authority.root
            repository_identity = repository_authority.root_identity
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise CLIError("repository source authority changed") from exc
        if pinned_repo != repo_path:
            raise CLIError("repository source authority differs from its path")
        repo_path = pinned_repo
    for first, first_label, second, second_label in (
        (resolved_catalog, "catalog", resolved_cas, "CAS root"),
        (resolved_catalog, "catalog", resolved_workspace, "workspace root"),
        (resolved_cas, "CAS root", resolved_workspace, "workspace root"),
        (resolved_cas, "CAS root", resolved_output_parent, "output parent"),
    ):
        if _paths_overlap(first, second):
            raise CLIError(
                f"{first_label} must not physically overlap the {second_label}"
            )
    if repo_path is not None:
        _require_retained_repository_topology(
            repo_path,
            resolved_catalog,
            resolved_cas,
            resolved_workspace,
            resolved_output,
        )

    owners = tuple(_PublicationAuthorityOwner() for _index in range(3))

    def open_binding(
        path: Path,
        label: str,
        owner: object,
    ) -> _RetainedDirectoryBinding:
        authority = _open_publication_authority(
            path,
            parent_resource=None,
            expected_parent_identity=None,
            authority_owner=owner,  # type: ignore[arg-type]
        )
        return _RetainedDirectoryBinding(
            label=label,
            path=path,
            identity=_retained_open_directory_identity(authority),
            owner=owner,
        )

    try:
        cas_binding = open_binding(resolved_cas, "CAS root", owners[0])
        workspace_binding = open_binding(
            resolved_workspace,
            "workspace root",
            owners[1],
        )
        output_parent_binding = open_binding(
            resolved_output_parent,
            "output parent",
            owners[2],
        )
        topology = _RetainedMaterializationTopology(
            catalog_path=resolved_catalog,
            catalog_identity=catalog_identity,
            cas_root=resolved_cas,
            workspace_root=resolved_workspace,
            output=resolved_output,
            cas_binding=cas_binding,
            workspace_binding=workspace_binding,
            output_parent_binding=output_parent_binding,
            repo_path=repo_path,
            repository_identity=repository_identity,
            repository_authority=repository_authority,
        )
        topology.verify()
        return topology
    except BaseException as primary_error:  # noqa: B036 - close every authority
        for owner in reversed(owners):
            try:
                owner.close()
            except BaseException as close_error:  # noqa: B036 - preserve primary
                _annotate_secondary_error(
                    primary_error,
                    "retained directory authority cleanup also failed",
                    close_error,
                )
            finally:
                _attach_publication_cleanup_owner(primary_error, owner)
        raise


def _warn_possible_retained_publication(output: Path) -> None:
    try:
        output.lstat()
    except (OSError, RuntimeError, ValueError):
        return
    print(
        "warning: retained context output now exists and may have been published; "
        f"verify it before reuse: {output}",
        file=sys.stderr,
    )


def _new_retained_output_receipt_owner():
    from ._captured_directory import PublishedWorkspaceReceiptOwner

    return PublishedWorkspaceReceiptOwner()


_RETAINED_RESOURCE_MISSING = object()


class _RetainedMaterializationResourceOwner:
    """Keep one CLI-opened resource retryable across ordered cleanup."""

    __slots__ = ("_resource",)

    def __init__(self) -> None:
        self._resource: object = _RETAINED_RESOURCE_MISSING

    @property
    def closed(self) -> bool:
        return self._resource is _RETAINED_RESOURCE_MISSING

    def acquire(self, factory):
        if self._resource is not _RETAINED_RESOURCE_MISSING:
            raise RuntimeError("retained materialization resource is already acquired")
        # The cleanup owner is reachable before the factory runs.  A direct
        # attribute store leaves only the native-return-to-STORE_ATTR edge that
        # Python cannot protect without a constructor-owned handoff protocol.
        self._resource = factory()
        return self._resource

    def close(self) -> None:
        resource = self._resource
        if resource is _RETAINED_RESOURCE_MISSING:
            return
        resource.close()  # type: ignore[attr-defined]
        self._resource = _RETAINED_RESOURCE_MISSING


def _inherit_publication_cleanup_owners(
    target: BaseException,
    source: BaseException,
) -> None:
    """Preserve incomplete internal cleanup owners across a public wrapper."""

    from ._atomic_directory import _attach_publication_cleanup_owner

    try:
        owners = BaseException.__getattribute__(
            source,
            "publication_cleanup_owners",
        )
    except AttributeError:
        return
    if type(owners) is not tuple:
        return
    for owner in owners:
        _attach_publication_cleanup_owner(target, owner)


def _compiler_cache_import_selection(
    *,
    ref_name: object,
    expected_generation: object,
) -> tuple[str, int]:
    selected_ref = "main" if ref_name is None else ref_name
    if (
        type(selected_ref) is not str
        or not selected_ref
        or selected_ref != selected_ref.strip()
        or "\x00" in selected_ref
        or len(selected_ref) > 32_768
    ):
        raise CLIError("retained ref name is not canonical")
    raw_generation = 0 if expected_generation is None else expected_generation
    if isinstance(raw_generation, bool):
        raise CLIError("expected ref generation must be a non-negative integer")
    try:
        generation = int(raw_generation)
    except (TypeError, ValueError) as exc:
        raise CLIError(
            "expected ref generation must be a non-negative integer"
        ) from exc
    if generation < 0 or generation > _SIGNED_INT64_MAX:
        raise CLIError(
            "expected ref generation must be a non-negative signed 64-bit integer"
        )
    return selected_ref, generation


_COMPILER_CACHE_IMPORT_VIEWS = ("bm25", "vector")


def _compiler_cache_import_views(explicit: Iterable[str]) -> tuple[str, ...]:
    """Normalize an explicit cache-ingress view set into canonical order."""

    requested = _split_values(explicit)
    if not requested:
        return ("bm25",)
    unknown = sorted(set(requested) - set(_COMPILER_CACHE_IMPORT_VIEWS))
    if unknown:
        raise CLIError(f"unsupported compiler cache view: {', '.join(unknown)}")
    selected = set(requested)
    return tuple(view for view in _COMPILER_CACHE_IMPORT_VIEWS if view in selected)


def _new_compiler_cache_import_nonce() -> str:
    import secrets

    nonce = secrets.token_hex(16)
    if re.fullmatch(r"[0-9a-f]{32}", nonce, flags=re.ASCII) is None:
        raise RuntimeError("cache import destination nonce is invalid")
    return nonce


@dataclass(frozen=True, slots=True)
class _CompilerCacheImportPaths:
    repo_path: Path
    cache_dir: Path
    catalog_path: Path
    cas_root: Path
    workspace_root: Path
    view_destinations: tuple[tuple[str, Path], ...]
    context_destination: Path
    suggested_materialization: Path

    @property
    def destination_map(self) -> dict[str, Path]:
        return dict(self.view_destinations)


def _compiler_cache_import_paths(
    args: argparse.Namespace,
    *,
    views: tuple[str, ...] = ("bm25",),
    repo_path: Path | None = None,
    cache_dir: Path | None = None,
) -> _CompilerCacheImportPaths:
    """Freeze all user paths and allocate missing-only generation names."""

    selected_repo = resolve_repo_path(args.repo) if repo_path is None else repo_path
    selected_cache = (
        _lexical_cli_path(args.cache_dir, label="cache directory")
        if cache_dir is None
        else Path(os.path.abspath(os.fspath(cache_dir)))
    )
    catalog_path = _lexical_cli_path(args.catalog, label="catalog")
    cas_root = _lexical_cli_path(args.cas_root, label="CAS root")
    workspace_root = _lexical_cli_path(args.workspace_root, label="workspace root")
    for first, first_label, second, second_label in (
        (catalog_path, "catalog", cas_root, "CAS root"),
        (catalog_path, "catalog", workspace_root, "workspace root"),
        (cas_root, "CAS root", workspace_root, "workspace root"),
        (selected_repo, "repository", catalog_path, "catalog"),
        (selected_repo, "repository", cas_root, "CAS root"),
        (selected_repo, "repository", workspace_root, "workspace root"),
        (selected_cache, "cache directory", catalog_path, "catalog"),
        (selected_cache, "cache directory", cas_root, "CAS root"),
        (selected_cache, "cache directory", workspace_root, "workspace root"),
    ):
        if _paths_overlap(first, second):
            raise CLIError(f"{first_label} must not overlap the {second_label}")
    if selected_cache == selected_repo or selected_cache in selected_repo.parents:
        raise CLIError("cache directory must not contain the repository")

    nonce = _new_compiler_cache_import_nonce()
    prefix = f".codenib-cache-import-{nonce}"
    view_destinations = tuple(
        (view, workspace_root / f"{prefix}-{view}") for view in views
    )
    return _CompilerCacheImportPaths(
        repo_path=selected_repo,
        cache_dir=selected_cache,
        catalog_path=catalog_path,
        cas_root=cas_root,
        workspace_root=workspace_root,
        view_destinations=view_destinations,
        context_destination=workspace_root / f"{prefix}-context",
        suggested_materialization=workspace_root / f"{prefix}-materialized",
    )


@dataclass(frozen=True, slots=True)
class _CompilerCacheImportTopology:
    cache_dir: Path
    cache_identity: tuple[int, int]
    catalog_path: Path
    catalog_identity: tuple[int, int, int]
    cas_root: Path
    cas_identity: tuple[int, ...]
    workspace_root: Path
    workspace_identity: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _ExistingIndexJobCatalogFactory:
    """Open exact existing-only SQLite sessions for one worker invocation."""

    catalog_path: Path
    catalog_identity: tuple[int, int, int]

    @contextmanager
    def __call__(self):
        from ._atomic_directory import _OrderedAction, _run_context_with_cleanup_actions
        from .storage import SQLiteCatalog

        catalog_owner = _RetainedMaterializationResourceOwner()
        cleanup_actions = (
            (
                "worker SQLite catalog exit binding validation also failed",
                lambda: _require_retained_catalog_binding(
                    self.catalog_path,
                    self.catalog_identity,
                ),
            ),
            _OrderedAction(
                label="worker SQLite catalog cleanup also failed",
                action=catalog_owner.close,
                complete=lambda: catalog_owner.closed,
                retry_incomplete="cancellation",
                incomplete_owner=catalog_owner,
            ),
        )
        with _run_context_with_cleanup_actions(cleanup_actions):
            _require_retained_catalog_binding(
                self.catalog_path,
                self.catalog_identity,
            )
            catalog = catalog_owner.acquire(
                lambda: SQLiteCatalog(
                    self.catalog_path,
                    create=False,
                    expected_file_identity=self.catalog_identity,
                )
            )
            _require_retained_catalog_binding(
                self.catalog_path,
                self.catalog_identity,
            )
            yield catalog


@dataclass(frozen=True, slots=True)
class _CompilerRetainedStorageTopology:
    repo_path: Path
    repository_identity: tuple[int, int]
    planned_cache_path: Path
    cache_parent_path: Path
    cache_parent_identity: tuple[int, int]
    catalog_path: Path
    catalog_identity: tuple[int, int, int]
    cas_root: Path
    cas_identity: tuple[int, ...]
    workspace_root: Path
    workspace_identity: tuple[int, ...]


@dataclass(slots=True)
class _CompilerRetainedCacheTopologyGuard:
    """Sandwich one exact cache/storage topology inside the compiler lease."""

    paths: _CompilerCacheImportPaths
    storage: _CompilerRetainedStorageTopology
    _captured: _CompilerCacheImportTopology | None = field(
        default=None,
        init=False,
        repr=False,
    )
    _verify_count: int = field(default=0, init=False, repr=False)

    def _observe(
        self,
        cache: Path,
        *,
        allow_published_outputs: bool,
    ) -> _CompilerCacheImportTopology:
        lexical_cache = Path(os.path.abspath(os.fspath(cache)))
        if lexical_cache != self.paths.cache_dir:
            raise CLIError("compiler cache lease did not bind the canonical cache")
        observed = _require_compiler_cache_import_topology(
            self.paths,
            allow_published_outputs=allow_published_outputs,
        )
        if (
            observed.cache_dir != self.storage.planned_cache_path
            or observed.catalog_path != self.storage.catalog_path
            or observed.catalog_identity != self.storage.catalog_identity
            or observed.cas_root != self.storage.cas_root
            or observed.cas_identity != self.storage.cas_identity
            or observed.workspace_root != self.storage.workspace_root
            or observed.workspace_identity != self.storage.workspace_identity
        ):
            raise CLIError("retained storage topology changed during compilation")
        if (
            _resolved_retained_materialization_path(
                self.storage.cache_parent_path,
                label="cache parent",
                strict=True,
            )
            != self.storage.cache_parent_path
            or _retained_real_directory_identity(
                self.storage.cache_parent_path,
                label="cache parent",
            )
            != self.storage.cache_parent_identity
        ):
            raise CLIError("cache parent topology changed during compilation")
        if (
            _resolved_retained_materialization_path(
                self.paths.repo_path,
                label="repository",
                strict=True,
            )
            != self.storage.repo_path
            or _retained_real_directory_identity(
                self.storage.repo_path,
                label="repository",
            )
            != self.storage.repository_identity
        ):
            raise CLIError("repository topology changed during compilation")
        return observed

    def capture(self, cache: Path) -> None:
        if self._captured is not None:
            raise CLIError("compiler cache topology was already captured")
        self._captured = self._observe(cache, allow_published_outputs=False)

    def verify(self, cache: Path) -> None:
        captured = self._captured
        if captured is None:
            raise CLIError("compiler cache topology was not captured")
        observed = self._observe(
            cache,
            allow_published_outputs=self._verify_count > 0,
        )
        if observed != captured:
            raise CLIError("compiler cache topology changed during compilation")
        self._verify_count += 1


def _compiler_cache_import_materialization_snapshot(
    paths: _CompilerCacheImportPaths,
    *,
    allow_published_outputs: bool = False,
) -> tuple[
    Path,
    tuple[int, int, int],
    Path,
    Path,
    Path,
    tuple[int, ...],
    tuple[int, ...],
    tuple[int, ...],
]:
    """Bind every retained output to one unchanged storage topology."""

    from ._atomic_directory import _OrderedAction, _run_context_with_cleanup_actions

    materialization_snapshots: list[
        tuple[
            Path,
            tuple[int, int, int],
            Path,
            Path,
            Path,
            tuple[int, ...],
            tuple[int, ...],
            tuple[int, ...],
        ]
    ] = []
    destinations = (
        (paths.suggested_materialization,)
        if allow_published_outputs
        else (
            *(destination for _view, destination in paths.view_destinations),
            paths.context_destination,
            paths.suggested_materialization,
        )
    )
    for destination in destinations:
        topology_owner = _RetainedMaterializationResourceOwner()
        cleanup_action = _OrderedAction(
            label="cache import path authority cleanup also failed",
            action=topology_owner.close,
            complete=lambda owner=topology_owner: owner.closed,
            retry_incomplete="cancellation",
            incomplete_owner=topology_owner,
        )

        def acquire_topology(
            selected_destination: Path = destination,
        ) -> _RetainedMaterializationTopology:
            return _require_retained_materialization_topology(
                paths.catalog_path,
                paths.cas_root,
                paths.workspace_root,
                selected_destination,
            )

        with _run_context_with_cleanup_actions((cleanup_action,)):
            materialization_topology = topology_owner.acquire(acquire_topology)
            materialization_topology.verify()
            materialization_snapshots.append(
                (
                    materialization_topology.catalog_path,
                    materialization_topology.catalog_identity,
                    materialization_topology.cas_root,
                    materialization_topology.workspace_root,
                    materialization_topology.output.parent,
                    materialization_topology.cas_binding.identity,
                    materialization_topology.workspace_binding.identity,
                    materialization_topology.output_parent_binding.identity,
                )
            )
    first = materialization_snapshots[0]
    if any(candidate != first for candidate in materialization_snapshots[1:]):
        raise CLIError("storage topology changed while allocating import outputs")
    return first


def _compiler_retained_cache_creation_topology(
    cache_dir: Path,
) -> tuple[Path, Path, tuple[int, int], tuple[tuple[Path, tuple[int, int]], ...]]:
    """Bind the nearest existing real parent of a possibly missing cache."""

    current = cache_dir
    while True:
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            parent = current.parent
            if parent == current:
                raise CLIError("cache directory has no existing parent") from None
            current = parent
            continue
        except (OSError, RuntimeError, ValueError) as exc:
            raise CLIError("cache parent cannot be inspected safely") from exc
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise CLIError("cache path must contain only existing real directories")
        break

    # This also inspects every lexical ancestor without following a symlink.
    ancestry = _retained_directory_ancestry(current, label="cache parent")
    resolved_parent = _resolved_retained_materialization_path(
        current,
        label="cache parent",
        strict=True,
    )
    try:
        suffix = cache_dir.relative_to(current)
    except ValueError as exc:  # pragma: no cover - upward walk proves containment
        raise CLIError("cache path escaped its existing parent") from exc
    planned_cache = resolved_parent.joinpath(*suffix.parts)
    return (
        planned_cache,
        resolved_parent,
        _retained_real_directory_identity(resolved_parent, label="cache parent"),
        ancestry,
    )


def _require_compiler_retained_storage_topology(
    paths: _CompilerCacheImportPaths,
) -> _CompilerRetainedStorageTopology:
    """Freeze storage and repository authorities before a cache is created."""

    (
        frozen_catalog_path,
        frozen_catalog_identity,
        frozen_cas_root,
        frozen_workspace_root,
        _frozen_output_parent,
        frozen_cas_identity,
        frozen_workspace_identity,
        _frozen_output_parent_identity,
    ) = _compiler_cache_import_materialization_snapshot(paths)

    resolved_repo = _resolved_retained_materialization_path(
        paths.repo_path,
        label="repository",
        strict=True,
    )
    repository_identity = _retained_real_directory_identity(
        resolved_repo,
        label="repository",
    )
    repository_ancestry = _retained_directory_ancestry(
        resolved_repo,
        label="repository",
    )
    (
        planned_cache,
        cache_parent,
        cache_parent_identity,
        cache_parent_ancestry,
    ) = _compiler_retained_cache_creation_topology(paths.cache_dir)
    catalog_ancestry = _retained_directory_ancestry(
        frozen_catalog_path.parent,
        label="catalog parent",
    )
    cas_ancestry = _retained_directory_ancestry(
        frozen_cas_root,
        label="CAS root",
    )
    workspace_ancestry = _retained_directory_ancestry(
        frozen_workspace_root,
        label="workspace root",
    )
    for storage, storage_label in (
        (frozen_catalog_path, "catalog"),
        (frozen_cas_root, "CAS root"),
        (frozen_workspace_root, "workspace root"),
    ):
        if _paths_overlap(resolved_repo, storage):
            raise CLIError(
                f"repository must not physically overlap the {storage_label}"
            )
        if _paths_overlap(planned_cache, storage):
            raise CLIError(
                f"cache directory must not physically overlap the {storage_label}"
            )
    for first_ancestry, first_label, second_ancestry, second_label in (
        (
            cache_parent_ancestry,
            "cache parent",
            repository_ancestry,
            "repository",
        ),
        (cache_parent_ancestry, "cache parent", catalog_ancestry, "catalog"),
        (cache_parent_ancestry, "cache parent", cas_ancestry, "CAS root"),
        (
            cache_parent_ancestry,
            "cache parent",
            workspace_ancestry,
            "workspace root",
        ),
        (repository_ancestry, "repository", catalog_ancestry, "catalog"),
        (repository_ancestry, "repository", cas_ancestry, "CAS root"),
        (
            repository_ancestry,
            "repository",
            workspace_ancestry,
            "workspace root",
        ),
        (catalog_ancestry, "catalog", cas_ancestry, "CAS root"),
        (catalog_ancestry, "catalog", workspace_ancestry, "workspace root"),
        (cas_ancestry, "CAS root", workspace_ancestry, "workspace root"),
    ):
        _require_distinct_directory_ancestry(
            first_ancestry,
            first_label,
            second_ancestry,
            second_label,
        )
    mappings = _retained_linux_mount_mappings()
    planned_physical = _retained_linux_physical_path(planned_cache, mappings)
    for storage, storage_label in (
        (resolved_repo, "repository"),
        (frozen_catalog_path, "catalog"),
        (frozen_cas_root, "CAS root"),
        (frozen_workspace_root, "workspace root"),
    ):
        storage_physical = _retained_linux_physical_path(storage, mappings)
        if (
            planned_physical is not None
            and storage_physical is not None
            and _retained_physical_paths_overlap(
                planned_physical,
                storage_physical,
            )
        ):
            raise CLIError(
                f"cache directory must not traverse a physical {storage_label} alias"
            )
    return _CompilerRetainedStorageTopology(
        repo_path=resolved_repo,
        repository_identity=repository_identity,
        planned_cache_path=planned_cache,
        cache_parent_path=cache_parent,
        cache_parent_identity=cache_parent_identity,
        catalog_path=frozen_catalog_path,
        catalog_identity=frozen_catalog_identity,
        cas_root=frozen_cas_root,
        cas_identity=frozen_cas_identity,
        workspace_root=frozen_workspace_root,
        workspace_identity=frozen_workspace_identity,
    )


def _require_compiler_cache_import_topology(
    paths: _CompilerCacheImportPaths,
    *,
    allow_published_outputs: bool = False,
) -> _CompilerCacheImportTopology:
    """Freeze existing inputs and deny physical storage/source aliases."""

    (
        frozen_catalog_path,
        frozen_catalog_identity,
        frozen_cas_root,
        frozen_workspace_root,
        _frozen_output_parent,
        frozen_cas_identity,
        frozen_workspace_identity,
        _frozen_output_parent_identity,
    ) = _compiler_cache_import_materialization_snapshot(
        paths,
        allow_published_outputs=allow_published_outputs,
    )

    resolved_repo = _resolved_retained_materialization_path(
        paths.repo_path,
        label="repository",
        strict=True,
    )
    resolved_cache = _resolved_retained_materialization_path(
        paths.cache_dir,
        label="cache directory",
        strict=True,
    )
    cache_identity = _retained_real_directory_identity(
        resolved_cache,
        label="cache directory",
    )
    repository_ancestry = _retained_directory_ancestry(
        resolved_repo,
        label="repository",
    )
    cache_ancestry = _retained_directory_ancestry(
        resolved_cache,
        label="cache directory",
    )
    if resolved_cache == resolved_repo or resolved_cache in resolved_repo.parents:
        raise CLIError("cache directory must not physically contain the repository")

    resolved_workspace = frozen_workspace_root
    catalog_ancestry = _retained_directory_ancestry(
        frozen_catalog_path.parent,
        label="catalog parent",
    )
    cas_ancestry = _retained_directory_ancestry(
        frozen_cas_root,
        label="CAS root",
    )
    workspace_ancestry = _retained_directory_ancestry(
        resolved_workspace,
        label="workspace root",
    )
    for source, source_label in (
        (resolved_repo, "repository"),
        (resolved_cache, "cache directory"),
    ):
        for storage, storage_label in (
            (frozen_catalog_path, "catalog"),
            (frozen_cas_root, "CAS root"),
            (resolved_workspace, "workspace root"),
        ):
            if _paths_overlap(source, storage):
                raise CLIError(
                    f"{source_label} must not physically overlap the {storage_label}"
                )
    for first_ancestry, first_label, second_ancestry, second_label in (
        (repository_ancestry, "repository", cache_ancestry, "cache directory"),
        (repository_ancestry, "repository", catalog_ancestry, "catalog"),
        (repository_ancestry, "repository", cas_ancestry, "CAS root"),
        (
            repository_ancestry,
            "repository",
            workspace_ancestry,
            "workspace root",
        ),
        (cache_ancestry, "cache directory", catalog_ancestry, "catalog"),
        (cache_ancestry, "cache directory", cas_ancestry, "CAS root"),
        (
            cache_ancestry,
            "cache directory",
            workspace_ancestry,
            "workspace root",
        ),
        (catalog_ancestry, "catalog", cas_ancestry, "CAS root"),
        (catalog_ancestry, "catalog", workspace_ancestry, "workspace root"),
        (cas_ancestry, "CAS root", workspace_ancestry, "workspace root"),
    ):
        _require_distinct_directory_ancestry(
            first_ancestry,
            first_label,
            second_ancestry,
            second_label,
        )
    return _CompilerCacheImportTopology(
        cache_dir=resolved_cache,
        cache_identity=cache_identity,
        catalog_path=frozen_catalog_path,
        catalog_identity=frozen_catalog_identity,
        cas_root=frozen_cas_root,
        cas_identity=frozen_cas_identity,
        workspace_root=resolved_workspace,
        workspace_identity=frozen_workspace_identity,
    )


def _run_index_retained(
    args: argparse.Namespace,
    *,
    repo_path: Path,
    selection: _RetainedIndexSelection,
    compiler_kwargs: dict[str, object],
) -> int:
    """Compile and retain selected views through one cache lease."""

    from . import LocalWorkspaceProvider
    from ._atomic_directory import (
        _annotate_secondary_error,
        _OrderedAction,
        _run_context_with_cleanup_actions,
    )
    from .artifacts.runtime import SourceBindingCleanupOwner
    from .compiler.cache_import import compile_and_import_repo
    from .paths import repo_index_dir
    from .source_fingerprint import capture_repository_source
    from .storage import LocalCAS, SQLiteCatalog

    views = tuple(compiler_kwargs["views"])  # type: ignore[arg-type]
    canonical_cache = Path(os.path.abspath(os.fspath(repo_index_dir(repo_path))))
    paths = _compiler_cache_import_paths(
        args,
        views=views,
        repo_path=repo_path,
        cache_dir=canonical_cache,
    )
    view_destinations = paths.destination_map
    try:
        provider = LocalWorkspaceProvider(paths.workspace_root)
        provider.require_support()
        storage_topology = _require_compiler_retained_storage_topology(paths)
    except CLIError:
        raise
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        wrapped = CLIError(str(exc))
        _inherit_publication_cleanup_owners(wrapped, exc)
        raise wrapped from exc

    source_exclusions = tuple(
        dict.fromkeys(
            (
                paths.cache_dir,
                *view_destinations.values(),
                paths.context_destination,
                paths.suggested_materialization,
            )
        )
    )
    publication_forbidden_paths = tuple(
        dict.fromkeys(
            (
                paths.cache_dir,
                storage_topology.catalog_path,
                storage_topology.cas_root,
                storage_topology.workspace_root,
                *view_destinations.values(),
                paths.context_destination,
                paths.suggested_materialization,
            )
        )
    )
    try:
        view_owners = {view: _new_retained_output_receipt_owner() for view in views}
        context_owner = _new_retained_output_receipt_owner()
        source_owner = SourceBindingCleanupOwner()
        object_store_owner = _RetainedMaterializationResourceOwner()
        catalog_owner = _RetainedMaterializationResourceOwner()
        view_cleanup_actions = tuple(
            _OrderedAction(
                label=(
                    "retained index "
                    + ("BM25" if view == "bm25" else view.title())
                    + " receipt cleanup also failed"
                ),
                action=owner.close,
                complete=lambda owner=owner: owner.closed,
                retry_incomplete="cancellation",
                incomplete_owner=owner,
            )
            for view, owner in reversed(tuple(view_owners.items()))
        )
        cleanup_actions = (
            _OrderedAction(
                label="retained index SQLite catalog cleanup also failed",
                action=catalog_owner.close,
                complete=lambda: catalog_owner.closed,
                retry_incomplete="cancellation",
                incomplete_owner=catalog_owner,
            ),
            _OrderedAction(
                label="retained index local CAS cleanup also failed",
                action=object_store_owner.close,
                complete=lambda: object_store_owner.closed,
                retry_incomplete="cancellation",
                incomplete_owner=object_store_owner,
            ),
            _OrderedAction(
                label="retained index context receipt cleanup also failed",
                action=context_owner.close,
                complete=lambda: context_owner.closed,
                retry_incomplete="cancellation",
                incomplete_owner=context_owner,
            ),
            *view_cleanup_actions,
            _OrderedAction(
                label="retained index repository source cleanup also failed",
                action=source_owner.close,
                complete=lambda: source_owner.closed,
                retry_incomplete="cancellation",
                incomplete_owner=source_owner,
            ),
        )
        result = None
        with _run_context_with_cleanup_actions(cleanup_actions):
            object_store = object_store_owner.acquire(
                lambda: LocalCAS(
                    storage_topology.cas_root,
                    require_preprovisioned=True,
                )
            )
            _require_retained_catalog_binding(
                storage_topology.catalog_path,
                storage_topology.catalog_identity,
            )
            catalog = catalog_owner.acquire(
                lambda: SQLiteCatalog(
                    storage_topology.catalog_path,
                    create=False,
                    expected_file_identity=storage_topology.catalog_identity,
                )
            )
            _require_retained_catalog_binding(
                storage_topology.catalog_path,
                storage_topology.catalog_identity,
            )
            repository_source = capture_repository_source(
                repo_path,
                exclude_roots=source_exclusions,
                selection=compiler_kwargs["source_selection"],
                _source_owner=source_owner.retain,
            )
            compiler, prepared_cache = _prepare_index_compiler(
                repo_path,
                **compiler_kwargs,  # type: ignore[arg-type]
            )
            if Path(os.path.abspath(os.fspath(prepared_cache))) != paths.cache_dir:
                raise CLIError("compiler did not select the canonical cache directory")
            cache_guard = _CompilerRetainedCacheTopologyGuard(
                paths,
                storage_topology,
            )
            result = compile_and_import_repo(
                compiler,
                repo_path,
                cache_dir=paths.cache_dir,
                views=views,
                repository_source=repository_source,
                view_output_owners=view_owners,
                context_output_owner=context_owner,
                view_destinations=view_destinations,
                context_destination=paths.context_destination,
                workspace_provider=provider,
                repository_key=selection.repository,
                catalog=catalog,
                object_store=object_store,
                namespace_name=selection.namespace,
                ref_name=selection.ref_name,
                expected_generation=selection.expected_generation,
                forbidden_paths=publication_forbidden_paths,
                environ=_publication_environment(),
                cache_topology_guard=cache_guard,
            )
        if result is None:  # pragma: no cover - successful producer always sets it
            raise RuntimeError("retained compiler publication returned no summary")

        retained = result.retained_import.import_result
        _print_index_summary(result.manifest, views)
        print(f"Retained Snapshot:   {retained.snapshot_id}")
        print(f"Retained Ref:        {retained.ref_name}")
        print(f"Retained Generation: {retained.generation}")
        print(f"Retained Views:      {','.join(views)}")
        for view, destination in view_destinations.items():
            display = "BM25" if view == "bm25" else view.title()
            print(f"{display + ' evidence:':<21}{destination}")
        print(f"Context evidence:    {paths.context_destination}")
        print(
            "Materialize snapshot: "
            + shlex.join(
                [
                    "codenib",
                    "artifact",
                    "materialize",
                    "--catalog",
                    os.fspath(storage_topology.catalog_path),
                    "--cas-root",
                    os.fspath(storage_topology.cas_root),
                    "--workspace-root",
                    os.fspath(storage_topology.workspace_root),
                    "--repository",
                    selection.repository,
                    "--namespace",
                    selection.namespace,
                    "--snapshot",
                    retained.snapshot_id,
                    "--output",
                    os.fspath(paths.suggested_materialization),
                ]
            )
        )
        return 0
    except BaseException as primary_error:  # noqa: B036 - preserve cancellation
        for destination, label in (
            *(
                (
                    destination,
                    ("BM25" if view == "bm25" else view.title()) + " evidence",
                )
                for view, destination in view_destinations.items()
            ),
            (paths.context_destination, "context evidence"),
        ):
            try:
                _warn_possible_compiler_cache_import_publication(
                    destination,
                    label=label,
                )
            except BaseException as warning_error:  # noqa: B036 - diagnostic only
                _annotate_secondary_error(
                    primary_error,
                    f"retained index {label} warning also failed",
                    warning_error,
                )
        if isinstance(primary_error, CLIError):
            raise
        if isinstance(
            primary_error, (OSError, RuntimeError, ValueError, sqlite3.Error)
        ):
            wrapped = CLIError(str(primary_error))
            _inherit_publication_cleanup_owners(wrapped, primary_error)
            raise wrapped from primary_error
        raise


def _warn_possible_compiler_cache_import_publication(
    destination: Path,
    *,
    label: str,
) -> None:
    try:
        destination.lstat()
    except (OSError, RuntimeError, ValueError):
        return
    print(
        f"warning: cache import {label} now exists and may have been published; "
        f"verify it before reuse: {destination}",
        file=sys.stderr,
    )


def _run_artifact_materialize(args: argparse.Namespace) -> int:
    from . import LocalWorkspaceProvider
    from ._atomic_directory import (
        _annotate_secondary_error,
        _OrderedAction,
        _run_context_with_cleanup_actions,
    )
    from .compiler.manifest_materialization import (
        materialize_retained_repo_manifest_ref,
        materialize_retained_repo_manifest_snapshot,
    )
    from .storage import LocalCAS, SQLiteCatalog

    repository, namespace = _retained_materialization_identity(
        args.repository,
        args.namespace,
    )
    ref_name, snapshot_id, expected_generation = _retained_materialization_selector(
        ref_name=args.ref,
        snapshot_id=args.snapshot,
        expected_generation=args.expected_generation,
    )
    catalog_path, cas_root, workspace_root, output = _retained_materialization_paths(
        args
    )
    summary: tuple[str, str, str, str, tuple[str, ...], Path] | None = None
    try:
        owner = _new_retained_output_receipt_owner()
        topology_owner = _RetainedMaterializationResourceOwner()
        object_store_owner = _RetainedMaterializationResourceOwner()
        catalog_owner = _RetainedMaterializationResourceOwner()
        cleanup_actions = (
            _OrderedAction(
                label="retained SQLite catalog cleanup also failed",
                action=catalog_owner.close,
                complete=lambda: catalog_owner.closed,
                retry_incomplete="cancellation",
                incomplete_owner=catalog_owner,
            ),
            _OrderedAction(
                label="retained local CAS cleanup also failed",
                action=object_store_owner.close,
                complete=lambda: object_store_owner.closed,
                retry_incomplete="cancellation",
                incomplete_owner=object_store_owner,
            ),
            _OrderedAction(
                label="retained output receipt cleanup also failed",
                action=owner.close,
                complete=lambda: owner.closed,
                retry_incomplete="cancellation",
                incomplete_owner=owner,
            ),
            _OrderedAction(
                label="retained path authority cleanup also failed",
                action=topology_owner.close,
                complete=lambda: topology_owner.closed,
                retry_incomplete="cancellation",
                incomplete_owner=topology_owner,
            ),
        )
        with _run_context_with_cleanup_actions(cleanup_actions):
            base_provider = LocalWorkspaceProvider(workspace_root)
            # Preserve provider/platform failure precedence before any storage
            # authority is opened.  The retained wrapper then repeats the
            # physical checks at the strict provisioning boundary.
            base_provider.require_support()
            topology = topology_owner.acquire(
                lambda: _require_retained_materialization_topology(
                    catalog_path,
                    cas_root,
                    workspace_root,
                    output,
                )
            )
            provider = _RetainedTopologyWorkspaceProvider(base_provider, topology)
            topology.verify()
            object_store = object_store_owner.acquire(
                lambda: LocalCAS(topology.cas_root, require_preprovisioned=True)
            )
            # Python's sqlite3 API reopens a pathname and its WAL/SHM sidecars;
            # it cannot consume a caller-owned descriptor.  The documented
            # deployment boundary therefore keeps that resolved namespace
            # quiescent and excludes an A-B-A rename during connect, while the
            # identity handoff and surrounding checks reject persistent
            # rebinding around authority acquisition and SQLite construction.
            _require_retained_catalog_binding(
                topology.catalog_path,
                topology.catalog_identity,
            )
            catalog = catalog_owner.acquire(
                lambda: SQLiteCatalog(
                    topology.catalog_path,
                    create=False,
                    expected_file_identity=topology.catalog_identity,
                )
            )
            _require_retained_catalog_binding(
                topology.catalog_path,
                topology.catalog_identity,
            )
            if snapshot_id is None:
                result = materialize_retained_repo_manifest_ref(
                    repository,
                    output,
                    namespace_name=namespace,
                    ref_name=ref_name or "main",
                    expected_generation=expected_generation,
                    catalog=catalog,
                    object_store=object_store,
                    workspace_provider=provider,
                    output_receipt_owner=owner,
                )
            else:
                result = materialize_retained_repo_manifest_snapshot(
                    repository,
                    snapshot_id,
                    output,
                    namespace_name=namespace,
                    catalog=catalog,
                    object_store=object_store,
                    workspace_provider=provider,
                    output_receipt_owner=owner,
                )
            receipt = result.export_receipt
            selector = (
                f"{receipt.ref_name}@{receipt.ref_generation}"
                if receipt.ref_name is not None
                else "immutable snapshot"
            )
            summary = (
                result.artifact.repository,
                receipt.snapshot_id,
                selector,
                result.artifact.commit,
                result.artifact.views,
                result.artifact.output_dir,
            )
        if summary is None:  # pragma: no cover - successful producer always sets it
            raise RuntimeError("retained materialization returned no summary")

        (
            materialized_repository,
            materialized_snapshot,
            selector,
            commit,
            views,
            root,
        ) = summary
        print(f"Context artifact: {root}")
        print(f"Repository:       {materialized_repository}")
        print(f"Snapshot:         {materialized_snapshot}")
        print(f"Selection:        {selector}")
        print(f"Commit:           {commit}")
        print(f"Views:            {', '.join(views)}")
        print(
            "Next:             "
            + shlex.join(
                [
                    "codenib",
                    "mcp",
                    "--artifact",
                    os.fspath(root),
                    "--repository",
                    materialized_repository,
                ]
            )
        )
        return 0
    except BaseException as primary_error:  # noqa: B036 - preserve cancellation
        try:
            _warn_possible_retained_publication(output)
        except BaseException as warning_error:  # noqa: B036 - diagnostic only
            _annotate_secondary_error(
                primary_error,
                "retained publication warning also failed",
                warning_error,
            )
        if isinstance(primary_error, CLIError):
            raise
        if isinstance(
            primary_error, (OSError, RuntimeError, ValueError, sqlite3.Error)
        ):
            wrapped = CLIError(str(primary_error))
            _inherit_publication_cleanup_owners(wrapped, primary_error)
            raise wrapped from primary_error
        raise


def _run_artifact_import_cache(args: argparse.Namespace) -> int:
    from . import LocalWorkspaceProvider
    from ._atomic_directory import (
        _annotate_secondary_error,
        _OrderedAction,
        _run_context_with_cleanup_actions,
    )
    from .artifacts.runtime import SourceBindingCleanupOwner
    from .compiler.cache_import import (
        compiler_cache_source_selection,
        import_compiler_cache,
    )
    from .source_fingerprint import capture_repository_source
    from .storage import LocalCAS, SQLiteCatalog

    repository, namespace = _retained_materialization_identity(
        args.repository,
        args.namespace,
    )
    ref_name, expected_generation = _compiler_cache_import_selection(
        ref_name=args.ref,
        expected_generation=args.expected_generation,
    )
    views = _compiler_cache_import_views(args.view)
    paths = _compiler_cache_import_paths(args, views=views)
    view_destinations = paths.destination_map
    try:
        provider = LocalWorkspaceProvider(paths.workspace_root)
        provider.require_support()
        topology = _require_compiler_cache_import_topology(paths)
    except CLIError:
        raise

    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        wrapped = CLIError(str(exc))
        _inherit_publication_cleanup_owners(wrapped, exc)
        raise wrapped from exc

    source_exclusions = tuple(
        dict.fromkeys(
            (
                paths.cache_dir,
                topology.cache_dir,
                *view_destinations.values(),
                paths.context_destination,
                paths.suggested_materialization,
            )
        )
    )
    publication_forbidden_paths = tuple(
        dict.fromkeys(
            (
                paths.cache_dir,
                topology.cache_dir,
                topology.catalog_path,
                topology.cas_root,
                paths.workspace_root,
                *view_destinations.values(),
                paths.context_destination,
                paths.suggested_materialization,
            )
        )
    )
    summary: tuple[str, str, int] | None = None
    try:
        source_selection = compiler_cache_source_selection(topology.cache_dir)
        view_owners = {view: _new_retained_output_receipt_owner() for view in views}
        context_owner = _new_retained_output_receipt_owner()
        source_owner = SourceBindingCleanupOwner()
        object_store_owner = _RetainedMaterializationResourceOwner()
        catalog_owner = _RetainedMaterializationResourceOwner()
        view_cleanup_actions = tuple(
            _OrderedAction(
                label=(
                    "cache import "
                    + ("BM25" if view == "bm25" else view.title())
                    + " receipt cleanup also failed"
                ),
                action=owner.close,
                complete=lambda owner=owner: owner.closed,
                retry_incomplete="cancellation",
                incomplete_owner=owner,
            )
            for view, owner in reversed(tuple(view_owners.items()))
        )
        cleanup_actions = (
            _OrderedAction(
                label="cache import SQLite catalog cleanup also failed",
                action=catalog_owner.close,
                complete=lambda: catalog_owner.closed,
                retry_incomplete="cancellation",
                incomplete_owner=catalog_owner,
            ),
            _OrderedAction(
                label="cache import local CAS cleanup also failed",
                action=object_store_owner.close,
                complete=lambda: object_store_owner.closed,
                retry_incomplete="cancellation",
                incomplete_owner=object_store_owner,
            ),
            _OrderedAction(
                label="cache import context receipt cleanup also failed",
                action=context_owner.close,
                complete=lambda: context_owner.closed,
                retry_incomplete="cancellation",
                incomplete_owner=context_owner,
            ),
            *view_cleanup_actions,
            _OrderedAction(
                label="cache import repository source cleanup also failed",
                action=source_owner.close,
                complete=lambda: source_owner.closed,
                retry_incomplete="cancellation",
                incomplete_owner=source_owner,
            ),
        )
        with _run_context_with_cleanup_actions(cleanup_actions):
            repository_source = capture_repository_source(
                paths.repo_path,
                exclude_roots=source_exclusions,
                selection=source_selection,
                _source_owner=source_owner.retain,
            )
            object_store = object_store_owner.acquire(
                lambda: LocalCAS(topology.cas_root, require_preprovisioned=True)
            )
            _require_retained_catalog_binding(
                topology.catalog_path,
                topology.catalog_identity,
            )
            catalog = catalog_owner.acquire(
                lambda: SQLiteCatalog(
                    topology.catalog_path,
                    create=False,
                    expected_file_identity=topology.catalog_identity,
                )
            )
            _require_retained_catalog_binding(
                topology.catalog_path,
                topology.catalog_identity,
            )
            result = import_compiler_cache(
                topology.cache_dir,
                views=views,
                repository_source=repository_source,
                view_output_owners=view_owners,
                context_output_owner=context_owner,
                view_destinations=view_destinations,
                context_destination=paths.context_destination,
                workspace_provider=provider,
                repository_key=repository,
                catalog=catalog,
                object_store=object_store,
                namespace_name=namespace,
                ref_name=ref_name,
                expected_generation=expected_generation,
                forbidden_paths=publication_forbidden_paths,
                environ=_publication_environment(),
            )
            imported = result.import_result
            summary = (
                imported.snapshot_id,
                imported.ref_name,
                imported.generation,
            )
        if summary is None:  # pragma: no cover - successful producer always sets it
            raise RuntimeError("compiler cache import returned no summary")

        snapshot_id, published_ref, generation = summary
        print(f"Snapshot:           {snapshot_id}")
        print(f"Ref:                {published_ref}")
        print(f"Generation:         {generation}")
        print(f"Views:              {','.join(views)}")
        for view, destination in view_destinations.items():
            display = "BM25" if view == "bm25" else view.title()
            print(f"{display + ' generation:':<20}{destination}")
        print(f"Context generation: {paths.context_destination}")
        print(
            "Suggested output:   " f"{paths.suggested_materialization} (not reserved)"
        )
        print(
            "Next:               "
            + shlex.join(
                [
                    "codenib",
                    "artifact",
                    "materialize",
                    "--catalog",
                    os.fspath(topology.catalog_path),
                    "--cas-root",
                    os.fspath(topology.cas_root),
                    "--workspace-root",
                    os.fspath(paths.workspace_root),
                    "--repository",
                    repository,
                    "--namespace",
                    namespace,
                    "--snapshot",
                    snapshot_id,
                    "--output",
                    os.fspath(paths.suggested_materialization),
                ]
            )
        )
        return 0
    except BaseException as primary_error:  # noqa: B036 - preserve cancellation
        for destination, label in (
            *(
                (
                    destination,
                    ("BM25" if view == "bm25" else view.title()) + " generation",
                )
                for view, destination in view_destinations.items()
            ),
            (paths.context_destination, "context generation"),
        ):
            try:
                _warn_possible_compiler_cache_import_publication(
                    destination,
                    label=label,
                )
            except BaseException as warning_error:  # noqa: B036 - diagnostic only
                _annotate_secondary_error(
                    primary_error,
                    f"cache import {label} warning also failed",
                    warning_error,
                )
        if isinstance(primary_error, CLIError):
            raise
        if isinstance(
            primary_error, (OSError, RuntimeError, ValueError, sqlite3.Error)
        ):
            wrapped = CLIError(str(primary_error))
            _inherit_publication_cleanup_owners(wrapped, primary_error)
            raise wrapped from primary_error
        raise


def _run_with_local_index_job_worker(args: argparse.Namespace, operation):
    """Run one callback while retaining an exact local worker topology."""

    from . import LocalWorkspaceProvider
    from ._atomic_directory import _OrderedAction, _run_context_with_cleanup_actions
    from .compiler.job_resolver import CompilerCacheJobResolver
    from .compiler.job_resources import (
        LocalCompilerCacheJobResourceFactory,
        LocalCompilerCacheJobTarget,
    )
    from .storage import IndexJobWorker, LocalCAS

    repository, namespace = _retained_materialization_identity(
        args.repository,
        args.namespace,
    )
    paths = _compiler_cache_import_paths(args, views=("bm25",))
    try:
        provider = LocalWorkspaceProvider(paths.workspace_root)
        provider.require_support()
        topology = _require_compiler_cache_import_topology(paths)
    except CLIError:
        raise
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        wrapped = CLIError(str(exc))
        _inherit_publication_cleanup_owners(wrapped, exc)
        raise wrapped from exc

    if not callable(operation):
        raise TypeError("local index job worker operation must be callable")
    missing_result = object()
    result = missing_result
    try:
        object_store_owner = _RetainedMaterializationResourceOwner()
        cleanup_actions = (
            _OrderedAction(
                label="worker local CAS cleanup also failed",
                action=object_store_owner.close,
                complete=lambda: object_store_owner.closed,
                retry_incomplete="cancellation",
                incomplete_owner=object_store_owner,
            ),
        )
        with _run_context_with_cleanup_actions(cleanup_actions):
            object_store = object_store_owner.acquire(
                lambda: LocalCAS(
                    topology.cas_root,
                    require_preprovisioned=True,
                )
            )
            target = LocalCompilerCacheJobTarget(
                repository_root=paths.repo_path,
                cache_dir=topology.cache_dir,
                workspace_provider=provider,
                repository_key=repository,
                namespace_name=namespace,
                environ=_publication_environment(),
            )
            resources = LocalCompilerCacheJobResourceFactory((target,))
            worker = IndexJobWorker(
                catalog_factory=_ExistingIndexJobCatalogFactory(
                    topology.catalog_path,
                    topology.catalog_identity,
                ),
                object_store=object_store,
                resolver=CompilerCacheJobResolver(
                    resource_factory=resources,
                    object_store=object_store,
                ),
                lease_duration_ms=args.lease_duration_ms,
                heartbeat_interval_ms=args.heartbeat_interval_ms,
                scan_limit=args.scan_limit,
                candidate_filter=resources.accepts_candidate,
            )
            result = operation(worker)
        if result is missing_result:  # pragma: no cover - callback always returns
            raise RuntimeError("index job worker operation returned no result")
        return result
    except BaseException as primary_error:  # noqa: B036 - preserve cancellation
        if isinstance(primary_error, CLIError):
            raise
        if isinstance(
            primary_error,
            (OSError, RuntimeError, ValueError, sqlite3.Error),
        ):
            wrapped = CLIError(str(primary_error))
            _inherit_publication_cleanup_owners(wrapped, primary_error)
            raise wrapped from primary_error
        raise


def _index_job_run_payload(result) -> dict[str, object]:
    return {
        "attempt_count": result.attempt_count,
        "disposition": result.disposition.value,
        "job_id": result.job_id,
    }


def _compact_json_line(payload: dict[str, object]) -> str:
    return json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def _run_jobs_run_once(args: argparse.Namespace) -> int:
    """Run one bounded prepare-only compiler-cache worker scan."""

    result = _run_with_local_index_job_worker(args, lambda worker: worker.run_once())
    payload = _index_job_run_payload(result)
    if args.json:
        print(_compact_json_line(payload))
    else:
        print(f"Disposition: {result.disposition.value}")
        print(f"Job:         {result.job_id or '-'}")
        print(
            "Attempt:     "
            + ("-" if result.attempt_count is None else str(result.attempt_count))
        )
    return 0


@contextmanager
def _jobs_scheduler_stop_signal():
    """Translate SIGTERM into a cooperative stop after the active worker page."""

    import signal
    import threading

    if threading.current_thread() is not threading.main_thread():
        raise CLIError("continuous index job scheduling must run on the main thread")
    stop_signal = threading.Event()
    termination_signal = getattr(signal, "SIGTERM", None)
    if termination_signal is None:  # pragma: no cover - POSIX production route
        yield stop_signal
        return
    previous_handler = signal.getsignal(termination_signal)

    def request_stop(_signum, _frame) -> None:
        stop_signal.set()

    try:
        signal.signal(termination_signal, request_stop)
    except (OSError, RuntimeError, ValueError) as exc:
        raise CLIError(f"cannot install scheduler SIGTERM handler: {exc}") from exc
    primary_error: BaseException | None = None
    try:
        yield stop_signal
    except BaseException as exc:  # noqa: B036 - preserve scheduler failure
        primary_error = exc
        raise
    finally:
        try:
            signal.signal(termination_signal, previous_handler)
        except (OSError, RuntimeError, ValueError) as exc:
            if primary_error is None:
                raise CLIError(
                    f"cannot restore scheduler SIGTERM handler: {exc}"
                ) from exc
            try:
                primary_error.add_note(
                    "scheduler SIGTERM handler restoration also failed: "
                    f"{type(exc).__name__}"
                )
            except BaseException:  # noqa: B036 - best-effort diagnostic only
                pass


def _run_jobs_continuous(args: argparse.Namespace) -> int:
    """Continuously traverse eligible compiler-cache jobs with cursor fairness."""

    from .storage import IndexJobWorkerScheduler

    def emit_result(result) -> None:
        if args.json:
            payload = {"type": "job", **_index_job_run_payload(result)}
            print(_compact_json_line(payload), flush=True)
        else:
            print(
                f"Job {result.job_id} attempt {result.attempt_count}: "
                f"{result.disposition.value}",
                flush=True,
            )

    with _jobs_scheduler_stop_signal() as stop_signal:

        def run_scheduler(worker):
            scheduler = IndexJobWorkerScheduler(
                worker=worker,
                initial_idle_delay_ms=args.initial_idle_delay_ms,
                max_idle_delay_ms=args.max_idle_delay_ms,
                on_result=emit_result,
            )
            return scheduler.run(stop_signal, max_cycles=args.max_cycles)

        summary = _run_with_local_index_job_worker(args, run_scheduler)

    payload = {
        "cycle_count": summary.cycle_count,
        "job_count": summary.job_count,
        "page_count": summary.page_count,
        "type": "summary",
    }
    if args.json:
        print(_compact_json_line(payload), flush=True)
    else:
        print(
            "Scheduler stopped: "
            f"{summary.job_count} jobs, {summary.page_count} pages, "
            f"{summary.cycle_count} cycles",
            flush=True,
        )
    return 0


def _run_artifact_mcp_config(args: argparse.Namespace) -> int:
    from .artifacts import render_artifact_mcp_config

    try:
        config = render_artifact_mcp_config(
            args.path,
            resolve_repo_path(args.repo),
            host=args.host,
            server_name=args.name,
            command=args.command,
            expected_repository=args.repository,
            claude_scope=args.claude_scope,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise CLIError(str(exc)) from exc
    print(config, end="")
    return 0


def _run_publish(args: argparse.Namespace) -> int:
    repo_path = resolve_repo_path(args.repo)
    site_output = (
        _canonical_publication_path(args.site_output) if args.site_output else None
    )
    context_output = (
        _canonical_publication_path(args.context_output)
        if args.context_output
        else None
    )
    _validate_publish_outputs(
        repo_path,
        site_output=site_output,
        context_output=context_output,
    )
    resolved_selection = _resolve_repository_source_selection(repo_path, args)
    _require_clean_artifact_checkout(
        repo_path,
        source_selection=resolved_selection.selection,
    )

    selected_views = _selected_views_for_args(args)
    unsupported = sorted(set(selected_views) - {"bm25", "vector"})
    if unsupported:
        raise CLIError(
            "portable publication currently supports bm25 and vector views; "
            f"unsupported: {', '.join(unsupported)}"
        )
    index_result = _run_index(args, selected_views=selected_views)
    if index_result:
        return index_result

    manifest_path = resolve_manifest_path(str(repo_path))
    from .artifacts import stage_context_artifact
    from .compiler.manifest import RepoManifest
    from .web.static_export import export_static_wiki

    manifest = RepoManifest.load(manifest_path)
    site_output = site_output or _canonical_publication_path(
        _default_distribution_dir(manifest_path, "wiki", manifest.commit)
    )
    context_output = context_output or _canonical_publication_path(
        _default_distribution_dir(manifest_path, "context", manifest.commit)
    )
    _validate_publish_outputs(
        repo_path,
        site_output=site_output,
        context_output=context_output,
    )
    publication_environment = _publication_environment(args.embedding_api_key_env)
    try:
        site = export_static_wiki(
            repo_path,
            manifest_path,
            site_output,
            frontend_dir=args.frontend_dir,
            base_path=args.base_path,
            environ=publication_environment,
        )
        context = stage_context_artifact(
            repo_path,
            manifest_path,
            context_output,
            repository=args.repository or os.environ.get("GITHUB_REPOSITORY"),
            views=selected_views,
            environ=publication_environment,
            validate_checkout=False,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise CLIError(str(exc)) from exc

    print(f"Published Wiki:    {site.output_dir}")
    print(f"Context artifact:  {context.output_dir}")
    print(f"Repository:        {context.repository}")
    print(f"Commit:            {context.commit or 'working tree'}")
    print(f"Views:             {', '.join(context.views)}")
    return 0


def _model_options_for_args(
    args: argparse.Namespace,
    *,
    include_wiki_environment: bool = False,
) -> dict[str, object]:
    """Resolve provider options from environment and repeatable CLI flags."""

    from .llm.options import (
        merge_model_options,
        parse_model_option_assignments,
        parse_model_options_json,
    )

    try:
        environment = parse_model_options_json(
            os.environ.get("CODENIB_DEMO_MODEL_OPTIONS"),
            source="CODENIB_DEMO_MODEL_OPTIONS",
        )
        wiki_environment = {}
        if include_wiki_environment:
            wiki_environment = parse_model_options_json(
                os.environ.get("CODENIB_DEMO_WIKI_MODEL_OPTIONS"),
                source="CODENIB_DEMO_WIKI_MODEL_OPTIONS",
            )
        command_line = parse_model_option_assignments(
            getattr(args, "model_option", None)
        )
        return merge_model_options(environment, wiki_environment, command_line)
    except ValueError as exc:
        raise CLIError(str(exc)) from exc


def _audit_local_wiki(local) -> dict[str, object]:
    """Load the prepared local runtime and audit its current generated pages."""

    from .llm.litellm_chat import LiteLLMChat
    from .web.config import load_config
    from .web.native_authority import authorize_local_manifest_vector
    from .web.repo_registry import RepoRegistry
    from .wiki.agent_wiki import AgentWiki
    from .wiki.quality import audit_wiki

    config = load_config(str(local.config_path))
    if local.runtime_env.get("CODENIB_DEMO_MODEL"):
        config.model = local.runtime_env["CODENIB_DEMO_MODEL"]
    if local.runtime_env.get("CODENIB_DEMO_API_BASE"):
        config.model_api_base = local.runtime_env["CODENIB_DEMO_API_BASE"]
    if local.runtime_env.get("CODENIB_DEMO_API_KEY"):
        config.model_api_key = local.runtime_env["CODENIB_DEMO_API_KEY"]
    if local.runtime_env.get("CODENIB_EMBEDDING_API_KEY"):
        config.embedding_api_key = local.runtime_env["CODENIB_EMBEDDING_API_KEY"]

    registry = RepoRegistry(
        config,
        native_index_authorization_resolver=authorize_local_manifest_vector,
    )
    registry.load_all()
    bundle = registry.get(local.repo_id)
    if bundle is None:
        raise CLIError(
            f"prepared repository {local.repo_id!r} was not loaded for auditing"
        )
    model = config.wiki_generation_model
    client = LiteLLMChat(
        model=model,
        temperature=0.2,
        max_tokens=4096,
        api_base=config.wiki_generation_api_base,
        api_key=config.wiki_generation_api_key,
        extra_kwargs=config.wiki_generation_options,
    )
    builder = AgentWiki(
        bundle,
        model,
        cache_dir=str(local.data_dir / "wiki_cache"),
        llm=client,
        api_base=config.wiki_generation_api_base,
        api_key=config.wiki_generation_api_key,
    )
    report = audit_wiki(builder)
    return {
        "repository": bundle.entry.repo,
        "commit": bundle.entry.base_commit,
        "model": model,
        **report,
    }


def _print_wiki_audit(report: dict[str, object], *, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return

    expected = int(report.get("expected_pages") or 0)
    print(f"Wiki quality audit: {report.get('repository') or 'repository'}")
    print(f"  Ready:      {int(report.get('ready_pages') or 0)}/{expected}")
    print(f"  Generated:  {int(report.get('generated_pages') or 0)}/{expected}")
    print(f"  Grounded:   {int(report.get('grounding_valid') or 0)}/{expected}")
    print(f"  Structured: {int(report.get('structural_valid') or 0)}/{expected}")
    print(f"  Narrative:  {int(report.get('narrative_valid') or 0)}/{expected}")
    print(f"  Fallbacks:  {int(report.get('fallbacks') or 0)}")
    for detail in report.get("details") or []:
        if not isinstance(detail, dict) or detail.get("ready"):
            continue
        failures = ", ".join(str(item) for item in detail.get("failures") or [])
        error = str(detail.get("error") or "").strip()
        suffix = f": {error}" if error else ""
        print(f"  FAIL {detail.get('id')}: {failures}{suffix}")
    print("Result: PASS" if report.get("passed") else "Result: FAIL")


def _manifest_embedding_route(
    manifest_path: Path,
    *,
    credential_env: str | None,
) -> InferenceRoute | None:
    from .compiler.manifest import RepoManifest

    manifest = RepoManifest.load(manifest_path)
    entry = manifest.indexes.get("vector")
    if entry is None or not manifest.index_is_current("vector"):
        return None
    try:
        return resolve_embedding_artifact_route(
            entry.config,
            credential_env=credential_env,
        )
    except ValueError as exc:
        raise CLIError(str(exc)) from exc


def _run_wiki(args: argparse.Namespace) -> int:
    try:
        args.port, args.api_port = validate_local_wiki_ports(
            frontend_port=args.port,
            api_port=args.api_port,
        )
    except ValueError as exc:
        raise CLIError(str(exc)) from exc

    repo_path = resolve_repo_path(args.repo)
    resolved_selection = _resolve_repository_source_selection(repo_path, args)
    if args.no_index and resolved_selection.action in {"replace", "clear"}:
        raise CLIError(
            "--exclude-dir and --clear-exclude-dirs require indexing; "
            "remove --no-index"
        )
    languages = _selected_languages(
        repo_path,
        args.language,
        source_selection=resolved_selection.selection,
    )
    if args.no_index and args.preset == "auto" and not _split_values(args.view):
        views = list(_PRESET_VIEWS["fast"])
        print("Auto preset: reuse manifest capabilities")
    else:
        views = _selected_views_for_args(args)
    audit = bool(args.audit or args.audit_json)
    model_options = _model_options_for_args(args)
    if (
        args.agent_wiki
        or audit
        or args.model
        or args.model_provider
        or args.api_base
        or args.api_key_env
        or args.model_option
    ):
        _require_modules(
            ("litellm",),
            extra="agent",
            feature="model-backed Wiki features",
        )
    model_backend = _model_backend_for_args(args, options=model_options)
    if args.model_provider and model_backend is None:
        raise CLIError("--model is required when --model-provider is selected")

    if args.no_index:
        manifest_path = resolve_manifest_path(str(repo_path))
    else:
        build_embedding_route = None
        if "vector" in views:
            build_embedding_route = _embedding_route_for_args(args)
            _check_view_dependencies(
                views,
                embedding_provider=build_embedding_route.provider,
            )
            try:
                build_embedding_route.credential()
            except ValueError as exc:
                raise CLIError(str(exc)) from exc
        else:
            _check_view_dependencies(views)

        index_kwargs = {
            "languages": languages,
            "views": views,
            "source_selection": resolved_selection.selection,
            "rebuild": args.rebuild,
        }
        if build_embedding_route is not None:
            index_kwargs.update(
                embedding_provider=build_embedding_route.provider,
                embedding_model=build_embedding_route.model,
                embedding_dimension=build_embedding_route.dimension,
                embedding_endpoint=build_embedding_route.endpoint,
                embedding_credential_env=build_embedding_route.credential_env,
            )
        manifest, failed = index_repository(repo_path, **index_kwargs)
        _print_index_summary(manifest, views)
        if failed:
            raise CLIError(
                "the Wiki was not started because requested views failed: "
                + ", ".join(failed)
            )
        manifest_path = resolve_manifest_path(str(repo_path))

    credential_env = args.embedding_api_key_env or os.environ.get(
        "CODENIB_EMBEDDING_API_KEY_ENV"
    )
    embedding_route = _manifest_embedding_route(
        manifest_path,
        credential_env=credential_env,
    )
    if embedding_route is not None:
        if _embedding_identity_is_configured(args):
            requested_route = _embedding_route_for_args(args)
            if (
                requested_route.compatibility_fingerprint
                != embedding_route.compatibility_fingerprint
            ):
                raise CLIError(
                    "selected embedding route does not match the current vector "
                    "artifact; rebuild the vector view or remove the route override"
                )
        _check_view_dependencies(
            ["vector"],
            embedding_provider=embedding_route.provider,
        )
        try:
            embedding_route.credential()
        except ValueError as exc:
            raise CLIError(str(exc)) from exc
    elif "vector" in views and not args.no_index:
        raise CLIError("the requested vector view is missing from the manifest")

    if args.no_index:
        dependency_views = [view for view in views if view != "vector"]
        if dependency_views:
            _check_view_dependencies(dependency_views)
        if "vector" in views and embedding_route is None:
            raise CLIError(
                "--no-index requested a vector view, but the manifest has no "
                "current vector artifact"
            )

    from .web.launcher import launch_local_wiki
    from .web.local import prepare_local_wiki

    try:
        local = prepare_local_wiki(
            repo_path,
            manifest_path,
            frontend_port=args.port,
            agent_wiki=args.agent_wiki or audit,
            model=model_backend.model if model_backend else None,
            api_base=model_backend.api_base if model_backend else None,
            api_key_env=model_backend.auth_source if model_backend else None,
            model_options=model_options,
            embedding_api_key_env=(
                embedding_route.credential_env if embedding_route else None
            ),
        )
    except ValueError as exc:
        raise CLIError(str(exc)) from exc
    if audit:
        report = _audit_local_wiki(local)
        _print_wiki_audit(report, as_json=args.audit_json)
        return 0 if report.get("passed") else 1
    try:
        return launch_local_wiki(
            local,
            frontend_dir=args.frontend_dir,
            api_host=args.api_host,
            api_port=args.api_port,
            frontend_host=args.host,
            frontend_port=args.port,
            install_frontend=not args.no_install_frontend,
            open_browser=not args.no_open,
        )
    except (OSError, RuntimeError) as exc:
        raise CLIError(str(exc)) from exc


def _check_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _require_modules(
    modules: Sequence[str],
    *,
    extra: str,
    feature: str,
) -> None:
    missing = [module for module in modules if not _check_module(module)]
    if not missing:
        return
    raise CLIError(
        f"{feature} requires the `{extra}` optional dependencies "
        f"(missing: {', '.join(missing)}). "
        f"Install them with `pip install 'codenib[{extra}]'`."
    )


def _check_view_dependencies(
    views: Sequence[str],
    *,
    embedding_provider: str = "huggingface",
) -> None:
    if "vector" in views:
        provider = normalize_provider(embedding_provider)
        embedding_module = (
            "sentence_transformers" if provider == "huggingface" else "openai"
        )
        extra = "semantic" if provider == "huggingface" else "semantic-remote"
        _require_modules(
            ("faiss", embedding_module),
            extra=extra,
            feature="the vector view",
        )
    if "symbol_graph" in views:
        _require_modules(
            ("igraph",),
            extra="graph",
            feature="the symbol graph view",
        )


def _doctor_model_config(
    args: argparse.Namespace,
) -> tuple[str, bool, str] | None:
    options = _model_options_for_args(
        args,
        include_wiki_environment=(
            not getattr(args, "model", None)
            and bool(os.environ.get("CODENIB_DEMO_WIKI_MODEL"))
        ),
    )
    try:
        backend = _model_backend_for_args(args, options=options)
    except CLIError as exc:
        model = getattr(args, "model", None) or "model"
        return ("Model configuration", False, f"{model}; {exc}")
    if backend is None:
        return None
    if not _check_module("litellm"):
        return (
            "Model configuration",
            False,
            f"{backend.model}; LiteLLM is missing",
        )
    try:
        from .llm.diagnostics import diagnose_model_backend

        report = diagnose_model_backend(
            model=backend.model,
            api_base=backend.api_base,
            api_key=backend.api_key,
            auth_source=backend.auth_source,
            options=options,
        )
    except Exception as exc:  # noqa: BLE001 - diagnostic must report, not crash
        return ("Model configuration", False, f"{backend.model}; {exc}")
    return ("Model configuration", report.configured, report.detail())


def _doctor_rows(
    args: argparse.Namespace | None = None,
) -> dict[str, list[tuple[str, bool, str]]]:
    from .web.launcher import (
        find_frontend_dir,
        is_prebuilt_frontend,
        node_runtime_status,
    )

    py_ok = sys.version_info >= (3, 10)
    frontend = find_frontend_dir()
    node_ok, node_detail = node_runtime_status()
    frontend_prebuilt = frontend is not None and is_prebuilt_frontend(frontend)
    runtime_detail = (
        "not required (prebuilt frontend)" if frontend_prebuilt else node_detail
    )
    try:
        embedding_route = _embedding_route_for_args(args or argparse.Namespace())
        try:
            embedding_route.credential()
            route_ok = True
            route_issue = ""
        except ValueError as exc:
            route_ok = False
            route_issue = str(exc)
        embedding_module = (
            "sentence_transformers"
            if embedding_route.provider == "huggingface"
            else "openai"
        )
        embedding_label = (
            "sentence-transformers"
            if embedding_route.provider == "huggingface"
            else "OpenAI SDK"
        )
        route_parts = [
            f"{embedding_route.provider}:{embedding_route.model}",
            f"dimension={embedding_route.dimension}",
        ]
        if embedding_route.endpoint:
            route_parts.append(f"endpoint={embedding_route.endpoint}")
        if embedding_route.credential_env:
            route_parts.append(f"auth={embedding_route.credential_env}")
        if route_issue:
            route_parts.append(route_issue)
        embedding_checks = [
            ("Embedding route", route_ok, "; ".join(route_parts)),
            (
                embedding_label,
                _check_module(embedding_module),
                "installed" if _check_module(embedding_module) else "missing",
            ),
            (
                "FAISS",
                _check_module("faiss"),
                "installed" if _check_module("faiss") else "missing",
            ),
        ]
    except CLIError as exc:
        embedding_checks = [
            ("Embedding route", False, str(exc)),
            (
                "FAISS",
                _check_module("faiss"),
                "installed" if _check_module("faiss") else "missing",
            ),
        ]
    rows = {
        "core": [
            ("Python >= 3.10", py_ok, sys.version.split()[0]),
            ("git", shutil.which("git") is not None, shutil.which("git") or "missing"),
            (
                "tree-sitter",
                _check_module("tree_sitter"),
                "installed" if _check_module("tree_sitter") else "missing",
            ),
            (
                "language pack",
                _check_module("tree_sitter_language_pack"),
                (
                    "installed"
                    if _check_module("tree_sitter_language_pack")
                    else "missing"
                ),
            ),
            (
                "BM25",
                _check_module("rank_bm25"),
                "installed" if _check_module("rank_bm25") else "missing",
            ),
        ],
        "wiki": [
            (
                "FastAPI",
                _check_module("fastapi"),
                "installed" if _check_module("fastapi") else "missing",
            ),
            (
                "Uvicorn",
                _check_module("uvicorn"),
                "installed" if _check_module("uvicorn") else "missing",
            ),
            (
                "Node.js",
                frontend_prebuilt or node_ok,
                runtime_detail,
            ),
            (
                "npm",
                frontend_prebuilt or shutil.which("npm") is not None,
                (
                    "not required (prebuilt frontend)"
                    if frontend_prebuilt
                    else shutil.which("npm") or "missing"
                ),
            ),
            (
                "Wiki frontend",
                frontend is not None,
                str(frontend) if frontend is not None else "missing",
            ),
        ],
        "semantic": embedding_checks,
        "graph": [
            (
                "igraph",
                _check_module("igraph"),
                "installed" if _check_module("igraph") else "missing",
            ),
            (
                "protobuf",
                _check_module("google.protobuf"),
                "installed" if _check_module("google.protobuf") else "missing",
            ),
        ],
        "agent": [
            (
                "LiteLLM",
                _check_module("litellm"),
                "installed" if _check_module("litellm") else "missing",
            ),
        ],
        "mcp": [
            (
                "MCP SDK",
                _check_module("mcp"),
                "installed" if _check_module("mcp") else "missing",
            ),
        ],
    }
    if args is not None:
        if not getattr(args, "repo", None):
            rows["graph"].append(
                (
                    "Repository toolchain",
                    True,
                    "pass a repository path for language-specific checks",
                )
            )
        model_check = _doctor_model_config(args)
        if model_check is not None:
            rows["agent"].append(model_check)
    return rows


def _model_probe_error(exc: BaseException, *, api_key: str | None) -> str:
    detail = " ".join(str(exc).split())
    detail = re.sub(r"\x1b\[[0-9;]*m", "", detail)
    if api_key:
        detail = detail.replace(api_key, "***")
    return detail[:240] or type(exc).__name__


def _probe_doctor_model(
    args: argparse.Namespace,
) -> list[tuple[str, bool, str]]:
    options = _model_options_for_args(
        args,
        include_wiki_environment=(
            not getattr(args, "model", None)
            and bool(os.environ.get("CODENIB_DEMO_WIKI_MODEL"))
        ),
    )
    try:
        backend = _model_backend_for_args(args, options=options)
    except CLIError as exc:
        detail = str(exc)
        return [
            ("Model text probe", False, detail),
            ("Model tool probe", False, "skipped: model route is invalid"),
            ("Model structured probe", False, "skipped: model route is invalid"),
        ]
    if backend is None:
        return [
            ("Model text probe", False, "set --model or CODENIB_DEMO_MODEL"),
            ("Model tool probe", False, "skipped: model is not configured"),
            ("Model structured probe", False, "skipped: model is not configured"),
        ]
    try:
        from pydantic import BaseModel

        from .llm.litellm_chat import LiteLLMChat, RetryConfig, human_message

        probe_options = dict(options)
        probe_options.setdefault("timeout", 20)
        llm = LiteLLMChat(
            model=backend.model,
            temperature=0.0,
            max_tokens=32,
            api_base=backend.api_base,
            api_key=backend.api_key,
            extra_kwargs=probe_options,
            retry=RetryConfig(max_retries=0),
        )
        response = llm.complete(
            [{"role": "user", "content": "Reply with OK."}],
        )
    except Exception as exc:  # noqa: BLE001 - diagnostic result
        detail = _model_probe_error(exc, api_key=backend.api_key)
        return [
            ("Model text probe", False, detail),
            ("Model tool probe", False, "skipped: text completion failed"),
            ("Model structured probe", False, "skipped: text completion failed"),
        ]

    checks = [
        (
            "Model text probe",
            bool(response),
            "response received" if response else "empty response",
        )
    ]
    tool = {
        "type": "function",
        "function": {
            "name": "report_backend_ready",
            "description": "Report that function calling is available.",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["ok"],
                    }
                },
                "required": ["status"],
                "additionalProperties": False,
            },
        },
    }
    try:
        tool_response = llm._call_raw(
            [
                {
                    "role": "user",
                    "content": "Call report_backend_ready with status ok.",
                }
            ],
            tools=[tool],
            tool_choice={
                "type": "function",
                "function": {"name": "report_backend_ready"},
            },
        )
        tool_calls = getattr(tool_response.choices[0].message, "tool_calls", None)
        function = getattr(tool_calls[0], "function", None) if tool_calls else None
        function_name = (
            function.get("name")
            if isinstance(function, dict)
            else getattr(function, "name", "")
        )
        tool_ok = bool(tool_calls and function_name == "report_backend_ready")
        checks.append(
            (
                "Model tool probe",
                tool_ok,
                "function call received" if tool_ok else "no function call returned",
            )
        )
    except Exception as exc:  # noqa: BLE001 - diagnostic result
        checks.append(
            (
                "Model tool probe",
                False,
                _model_probe_error(exc, api_key=backend.api_key),
            )
        )

    class ProbeResponse(BaseModel):
        status: str

    try:
        structured = llm.with_structured_output(ProbeResponse).invoke(
            [human_message("Return status ok.")]
        )
        structured_ok = structured.status.strip().lower() == "ok"
        checks.append(
            (
                "Model structured probe",
                structured_ok,
                (
                    "schema response received"
                    if structured_ok
                    else f"unexpected status={structured.status!r}"
                ),
            )
        )
    except Exception as exc:  # noqa: BLE001 - diagnostic result
        checks.append(
            (
                "Model structured probe",
                False,
                _model_probe_error(exc, api_key=backend.api_key),
            )
        )
    return checks


def _probe_doctor_embedding(
    args: argparse.Namespace,
) -> tuple[str, bool, str]:
    api_key = None
    store = None
    try:
        route = _embedding_route_for_args(args)
        _check_view_dependencies(["vector"], embedding_provider=route.provider)
        backend_kwargs = route.embedding_backend_kwargs()
        client_kwargs = route.client_kwargs()
        api_key = client_kwargs.get("api_key")
        if api_key is None and route.provider != "huggingface":
            api_key = os.environ.get("CODENIB_EMBEDDING_API_KEY")
            if api_key:
                client_kwargs["api_key"] = api_key
        backend_kwargs.update(client_kwargs)

        from .index.embedding.vector_store import CodeVectorStore

        store = CodeVectorStore(
            embedding_model=route.model,
            embedding_provider=route.provider,
            dimension=route.dimension,
            **backend_kwargs,
        )
        actual = store.dimension
        if actual != route.dimension:
            return (
                "Embedding probe",
                False,
                f"expected dimension {route.dimension}, received {actual}",
            )
        return (
            "Embedding probe",
            True,
            f"vector received; dimension={actual}",
        )
    except Exception as exc:  # noqa: BLE001 - diagnostic result
        return (
            "Embedding probe",
            False,
            _model_probe_error(exc, api_key=api_key),
        )
    finally:
        if store is not None:
            try:
                store.close()
            except Exception:  # noqa: BLE001 - best-effort diagnostic cleanup
                pass


def _run_doctor(args: argparse.Namespace) -> int:
    required = set(args.require or ["core"])
    rows = _doctor_rows(args)
    graph_report = None
    if args.repo:
        from .graph.setup import diagnose_graph_setup

        repo_path = resolve_repo_path(args.repo)
        resolved_selection = _resolve_repository_source_selection(repo_path, args)
        languages = _selected_languages(
            repo_path,
            args.language,
            source_selection=resolved_selection.selection,
        )
        graph_report = diagnose_graph_setup(repo_path, languages)
    if args.probe_model:
        rows["agent"].extend(_probe_doctor_model(args))
    if args.probe_embedding:
        rows["semantic"].append(_probe_doctor_embedding(args))
    failed_required = False
    print(f"CodeNib {package_version()}")
    for group, checks in rows.items():
        marker = "required" if group in required else "optional"
        print(f"\n{group} ({marker})")
        for label, ok, detail in checks:
            status = "OK" if ok else "MISSING"
            print(f"  [{status:<7}] {label}: {detail}")
            if group in required and not ok:
                failed_required = True
        if group == "graph" and graph_report is not None:
            for setup in graph_report.languages:
                if setup.state == "ready":
                    status = "OK"
                elif setup.state == "missing":
                    status = "MISSING"
                else:
                    status = "N/A"
                backend = setup.backend or "no provider"
                command = " ".join(setup.command) if setup.command else "none"
                detail = f"{backend}; command={command}"
                if setup.resolved_command:
                    detail += f"; resolved={setup.resolved_command}"
                if setup.missing:
                    detail += f"; missing={', '.join(setup.missing)}"
                if setup.note:
                    detail += f"; {setup.note}"
                print(
                    f"  [{status:<7}] {setup.display_name} ({setup.language}): "
                    f"{detail}"
                )
            if graph_report.install_hints:
                print("\n  Setup:")
                for hint in graph_report.install_hints:
                    print(f"    - {hint}")
            if group in required and not graph_report.ready:
                failed_required = True
    return 1 if failed_required else 0


def _toolchain_scopes(value: str) -> tuple[str, ...]:
    return ("graph", "lsp") if value == "all" else (value,)


def _toolchain_plan_for_args(args: argparse.Namespace):
    from .toolchains import plan_repository_toolchain

    repo_path = resolve_repo_path(args.repo)
    resolved_selection = _resolve_repository_source_selection(repo_path, args)
    languages = _selected_languages(
        repo_path,
        args.language,
        source_selection=resolved_selection.selection,
    )
    return plan_repository_toolchain(
        repo_path,
        languages,
        scopes=_toolchain_scopes(args.scope),
    )


def _print_toolchain_plan(plan) -> None:
    print(f"Repository: {plan.repository}")
    print(f"Languages:  {', '.join(plan.languages)}")
    print(f"Managed:    {plan.root}")
    print("Providers:")
    if not plan.requirements:
        print("  none")
    for requirement in plan.requirements:
        status = "OK" if requirement.ready else "MISSING"
        resolved = requirement.resolved or requirement.detail or "not found"
        print(
            f"  [{status:<7}] {requirement.display_name:<12} "
            f"{requirement.scope:<5} {requirement.executable}: {resolved}"
        )
    if plan.notes:
        print("Notes:")
        for note in plan.notes:
            print(f"  - {note}")


def _run_toolchain_status(args: argparse.Namespace) -> int:
    plan = _toolchain_plan_for_args(args)
    _print_toolchain_plan(plan)
    return 0 if plan.ready else 1


def _run_toolchain_install(args: argparse.Namespace) -> int:
    from .toolchains import install_requirements

    plan = _toolchain_plan_for_args(args)
    try:
        commands, manual = install_requirements(
            plan.missing,
            root=plan.root,
            dry_run=args.dry_run,
        )
    except RuntimeError as exc:
        raise CLIError(str(exc)) from exc

    heading = "Planned commands" if args.dry_run else "Executed commands"
    print(f"{heading}:")
    if commands:
        for command in commands:
            print(f"  {shlex.join(command)}")
    else:
        print("  none")
    if manual:
        print("Manual prerequisites:")
        for item in manual:
            print(f"  - {item}")
    if args.dry_run:
        return 0

    refreshed = _toolchain_plan_for_args(args)
    print()
    _print_toolchain_plan(refreshed)
    return 0 if refreshed.ready else 1


def _codegraph_error(exc: BaseException) -> CLIError:
    return CLIError(str(exc))


def _codegraph_spec_for_args(repo_path: Path, args: argparse.Namespace):
    from .codegraph_onboarding import (
        CodeGraphOnboardingError,
        load_codegraph_receipt,
        make_server_spec,
        resolve_codenib_command,
    )

    try:
        receipt = load_codegraph_receipt(repo_path)
        explicit = getattr(args, "server_command", None)
        if receipt is not None and explicit is None:
            return receipt.server, receipt
        command, prefix = resolve_codenib_command(explicit)
        server = make_server_spec(
            repo_path,
            command=command,
            command_prefix=prefix,
        )
        if receipt is not None and receipt.server != server:
            raise CodeGraphOnboardingError(
                "--command differs from the managed MCP registration; run "
                "`codenib codegraph uninstall` before changing it"
            )
        return server, receipt
    except (CodeGraphOnboardingError, OSError) as exc:
        raise _codegraph_error(exc) from exc


def _codegraph_clients_for_init(args: argparse.Namespace) -> tuple[str, ...]:
    from .codegraph_onboarding import (
        CodeGraphOnboardingError,
        resolve_requested_clients,
    )

    try:
        return resolve_requested_clients(args.agent)
    except (CodeGraphOnboardingError, OSError) as exc:
        raise _codegraph_error(exc) from exc


def _codegraph_languages_for_init(
    repo_path: Path,
    args: argparse.Namespace,
    receipt,
    *,
    source_selection: RepositorySourceSelection = (DEFAULT_REPOSITORY_SOURCE_SELECTION),
) -> tuple[str, ...]:
    del receipt
    # An omitted --language always means auto-detect the current checkout. This
    # keeps repeated onboarding in sync when a repository adds another language.
    return tuple(
        _selected_codegraph_languages(
            repo_path,
            args.language,
            source_selection=source_selection,
        )
    )


def _codegraph_preflight_clients(
    repo_path: Path,
    clients: Sequence[str],
    server,
    receipt,
) -> dict[str, object]:
    from .codegraph_onboarding import (
        CodeGraphOnboardingError,
        inspect_client_registration,
    )

    observed = {}
    try:
        for client in clients:
            inspection = inspect_client_registration(client, server, repo_path)
            observed[client] = inspection
            managed = receipt is not None and receipt.client(client) is not None
            if inspection.exists and not managed:
                raise CodeGraphOnboardingError(
                    f"{client} already has an unmanaged MCP server named "
                    f"{server.name}; remove or rename it before continuing"
                )
            if inspection.exists and not inspection.matches:
                raise CodeGraphOnboardingError(
                    f"{client} MCP server {server.name} differs from the CodeNib "
                    "receipt; inspect it before using --force with uninstall"
                )
        return observed
    except (CodeGraphOnboardingError, OSError) as exc:
        raise _codegraph_error(exc) from exc


def _codegraph_toolchain_plan(repo_path: Path, languages: Sequence[str]):
    from .toolchains import plan_repository_toolchain

    return plan_repository_toolchain(
        repo_path,
        languages,
        scopes=("graph",),
        allow_project_preparation=False,
    )


def _codegraph_plan_detail(plan) -> str:
    details = [f"{item.display_name}: {item.executable}" for item in plan.missing]
    details.extend(note for note in plan.notes if note.startswith("MISSING:"))
    return "; ".join(details) or "unknown graph prerequisite"


def _codegraph_checkout_snapshot(repo_path: Path) -> bytes:
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
        raise CLIError("CodeGraph initialization requires Git on PATH") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        suffix = f": {detail}" if detail else ""
        raise CLIError("CodeGraph initialization requires a Git working tree" + suffix)
    return bytes(result.stdout)


def _require_unchanged_codegraph_checkout(
    repo_path: Path,
    expected: bytes,
    *,
    stage: str,
) -> None:
    observed = _codegraph_checkout_snapshot(repo_path)
    if observed != expected:
        raise CLIError(
            f"CodeGraph {stage} changed the target checkout; inspect `git status` "
            "and restore or commit the affected files before retrying"
        )


def _run_codegraph_init(args: argparse.Namespace) -> int:
    from .codegraph_onboarding import (
        CodeGraphOnboardingError,
        CodeGraphReceipt,
        add_client_registration,
        inspect_client_registration,
        inspect_server_command,
        write_codegraph_receipt,
    )
    from .toolchains import install_requirements

    repo_path = resolve_repo_path(args.repo)
    resolved_selection = _resolve_repository_source_selection(repo_path, args)
    server, receipt = _codegraph_spec_for_args(repo_path, args)
    try:
        server_inspection = inspect_server_command(server, repo_path)
    except CodeGraphOnboardingError as exc:
        raise _codegraph_error(exc) from exc
    if not server_inspection.ready:
        raise CLIError(
            "CodeGraph MCP command is not this CodeNib runtime: "
            + server_inspection.detail
        )
    languages = _codegraph_languages_for_init(
        repo_path,
        args,
        receipt,
        source_selection=resolved_selection.selection,
    )
    clients = _codegraph_clients_for_init(args)
    observed = _codegraph_preflight_clients(
        repo_path,
        clients,
        server,
        receipt,
    )
    checkout_snapshot = _codegraph_checkout_snapshot(repo_path)
    if checkout_snapshot:
        raise CLIError(
            "CodeGraph initialization requires a clean Git checkout; commit or "
            "stash tracked and untracked changes before retrying"
        )
    plan = _codegraph_toolchain_plan(repo_path, languages)

    if args.dry_run:
        try:
            commands, manual = install_requirements(
                plan.missing,
                root=plan.root,
                dry_run=True,
            )
        except RuntimeError as exc:
            raise CLIError(str(exc)) from exc
        print(f"Repository: {repo_path}")
        print(f"Languages:  {', '.join(languages)}")
        print(
            "Source selection: "
            f"{resolved_selection.action} "
            f"({_source_selection_display(resolved_selection.selection)})"
        )
        print("Planned CodeGraph actions:")
        for command in commands:
            print(f"  toolchain: {shlex.join(command)}")
        for item in manual:
            print(f"  prerequisite: {item}")
        project_blockers = [
            note.removeprefix("MISSING: ")
            for note in plan.notes
            if note.startswith("MISSING:")
        ]
        for blocker in project_blockers:
            print(f"  prerequisite: {blocker}")
        print("  index: bm25, symbol_graph")
        for client in clients:
            action = "reuse" if observed[client].matches else "add"
            print(f"  {client}: {action} {server.name}")
        ready_after_install = not manual and not project_blockers
        print(
            "Readiness:  "
            + ("ready after planned tool install" if ready_after_install else "blocked")
        )
        print("Dry run complete; no tools, indexes, receipts, or clients changed.")
        return 0 if ready_after_install else 1

    _require_modules(
        ("igraph", "google.protobuf", "mcp"),
        extra="graph,mcp",
        feature="CodeGraph agent onboarding",
    )
    if plan.missing:
        try:
            commands, manual = install_requirements(plan.missing, root=plan.root)
        except RuntimeError as exc:
            raise CLIError(str(exc)) from exc
        if commands:
            print("Installed graph tools:")
            for command in commands:
                print(f"  {shlex.join(command)}")
        if manual:
            raise CLIError("CodeGraph needs manual prerequisites: " + "; ".join(manual))
        plan = _codegraph_toolchain_plan(repo_path, languages)
    if not plan.ready:
        raise CLIError(
            "CodeGraph toolchain is not ready: " + _codegraph_plan_detail(plan)
        )
    _require_unchanged_codegraph_checkout(
        repo_path,
        checkout_snapshot,
        stage="toolchain setup",
    )

    _check_view_dependencies(("bm25", "symbol_graph"))
    try:
        manifest, failed = index_repository(
            repo_path,
            languages=languages,
            views=("bm25", "symbol_graph"),
            source_selection=resolved_selection.selection,
            rebuild=args.rebuild,
            allow_graph_project_preparation=False,
            allow_partial_graph_languages=False,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        _require_unchanged_codegraph_checkout(
            repo_path,
            checkout_snapshot,
            stage="indexing",
        )
        raise CLIError(str(exc)) from exc
    _require_unchanged_codegraph_checkout(
        repo_path,
        checkout_snapshot,
        stage="indexing",
    )
    _print_index_summary(manifest, ("bm25", "symbol_graph"))
    if failed:
        raise CLIError("CodeGraph indexing failed for: " + ", ".join(failed))

    managed = receipt or CodeGraphReceipt(repo_path, server, ())
    for client in clients:
        if managed.client(client) is None:
            managed = managed.with_client(client, state="pending")
    try:
        write_codegraph_receipt(managed)
        for client in clients:
            inspection = inspect_client_registration(client, server, repo_path)
            if inspection.exists and not inspection.matches:
                raise CodeGraphOnboardingError(
                    f"{client} MCP server {server.name} changed during indexing; "
                    "refusing to overwrite it"
                )
            if not inspection.exists:
                add_client_registration(client, server, repo_path)
                inspection = inspect_client_registration(client, server, repo_path)
            if not inspection.matches:
                raise CodeGraphOnboardingError(
                    f"{client} did not retain the expected MCP registration"
                )
            managed = managed.with_client(client, state="configured")
            write_codegraph_receipt(managed)
    except (CodeGraphOnboardingError, OSError) as exc:
        _require_unchanged_codegraph_checkout(
            repo_path,
            checkout_snapshot,
            stage="agent registration",
        )
        raise _codegraph_error(exc) from exc
    _require_unchanged_codegraph_checkout(
        repo_path,
        checkout_snapshot,
        stage="agent registration",
    )

    print("\nCodeGraph is ready for coding agents.")
    print(f"MCP server: {server.name}")
    print(f"Clients:    {', '.join(clients)}")
    print("Excluded:   " + _source_selection_display(resolved_selection.selection))
    print(f"Status:     codenib codegraph status {shlex.quote(str(repo_path))}")
    print(
        "Try asking your agent to use explore_context before editing and "
        "dependency_subgraph for impact analysis."
    )
    return 0


def _codegraph_index_report(repo_path: Path) -> dict[str, object]:
    from .compiler.artifact_fingerprints import bm25_artifact_files_match
    from .compiler.checkout_identity import validate_checkout_identity
    from .compiler.graph_artifact import authenticated_graph_artifact_matches
    from .compiler.manifest import MANIFEST_FILENAME, RepoManifest
    from .paths import repo_index_dir

    manifest_path = repo_index_dir(repo_path) / MANIFEST_FILENAME
    report: dict[str, object] = {
        "manifest": str(manifest_path),
        "manifest_present": False,
        "manifest_version": None,
        "bm25": False,
        "bm25_manifest_current": False,
        "bm25_artifact_matches": False,
        "symbol_graph": False,
        "symbol_graph_manifest_current": False,
        "symbol_graph_artifact_matches": False,
        "symbol_graph_complete": False,
        "source_matches": False,
        "source_selection": {
            "recorded": False,
            "exclude_subtrees": [],
            "digest": None,
        },
        "languages": [],
        "detail": "manifest not found",
    }
    try:
        manifest = RepoManifest.load(manifest_path)
        report["manifest_present"] = True
        report["manifest_version"] = manifest.version
        report["languages"] = list(manifest.languages)
        if manifest.source_selection is not None:
            report["source_selection"] = {
                "recorded": True,
                "exclude_subtrees": list(manifest.source_selection.exclude_subtrees),
                "digest": manifest.source_selection_digest,
            }
        bm25_entry = manifest.indexes.get("bm25")
        graph_entry = manifest.indexes.get("symbol_graph")
        report["bm25_manifest_current"] = manifest.index_is_current("bm25")
        report["symbol_graph_manifest_current"] = manifest.index_is_current(
            "symbol_graph"
        )
        if report["bm25_manifest_current"] and bm25_entry is not None:
            report["bm25_artifact_matches"] = bm25_artifact_files_match(
                bm25_entry.path,
                expected_fingerprints=bm25_entry.config.get(
                    "artifact_file_fingerprints"
                ),
            )
        if report["symbol_graph_manifest_current"] and graph_entry is not None:
            report["symbol_graph_artifact_matches"] = (
                authenticated_graph_artifact_matches(graph_entry)
            )
        report["bm25"] = bool(
            report["bm25_manifest_current"] and report["bm25_artifact_matches"]
        )
        report["symbol_graph"] = bool(
            report["symbol_graph_manifest_current"]
            and report["symbol_graph_artifact_matches"]
        )
        report["symbol_graph_complete"] = bool(
            report["symbol_graph"]
            and graph_entry is not None
            and graph_entry.config.get("allow_partial_languages") is False
            and graph_entry.config.get("allow_partial_index") is False
        )
        validate_checkout_identity(
            repo_path,
            manifest,
            artifact_root=manifest_path.parent,
        )
        report["source_matches"] = True
        blockers: list[str] = []
        if manifest.source_selection is None:
            blockers.append(
                "manifest has no repository source-selection contract; "
                "run codegraph init to migrate it"
            )
        if not report["bm25_manifest_current"]:
            blockers.append("bm25 manifest entry is missing, failed, or stale")
        elif not report["bm25_artifact_matches"]:
            blockers.append(
                "bm25 artifact is missing or does not match its fingerprint"
            )
        if not report["symbol_graph_manifest_current"]:
            blockers.append("symbol_graph manifest entry is missing, failed, or stale")
        elif not report["symbol_graph_artifact_matches"]:
            blockers.append(
                "symbol_graph artifact is missing or does not match its fingerprint"
            )
        elif not report["symbol_graph_complete"]:
            blockers.append("symbol_graph was built with partial-language admission")
        report["detail"] = "; ".join(blockers) or "current"
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        report["detail"] = " ".join(str(exc).split())[:400]
    report["ready"] = bool(
        report["bm25"]
        and report["symbol_graph_complete"]
        and report["source_matches"]
        and report["source_selection"]["recorded"]
    )
    return report


def _codegraph_toolchain_report(
    repo_path: Path,
    languages: Sequence[str] | None = None,
) -> dict[str, object]:
    try:
        selected = tuple(languages or _selected_codegraph_languages(repo_path, ()))
        plan = _codegraph_toolchain_plan(repo_path, selected)
        return {
            "ready": plan.ready,
            "languages": list(selected),
            "missing": [item.executable for item in plan.missing],
            "notes": list(plan.notes),
        }
    except (CLIError, OSError, RuntimeError, ValueError) as exc:
        return {
            "ready": False,
            "languages": [],
            "missing": [],
            "notes": [" ".join(str(exc).split())[:400]],
        }


def _codegraph_status_report(repo_path: Path) -> dict[str, object]:
    from .codegraph_onboarding import (
        CodeGraphOnboardingError,
        codegraph_receipt_path,
        inspect_client_registration,
        inspect_server_command,
        load_codegraph_receipt,
    )

    index = _codegraph_index_report(repo_path)
    toolchain = _codegraph_toolchain_report(
        repo_path,
        index.get("languages") or None,
    )
    try:
        receipt = load_codegraph_receipt(repo_path)
    except CodeGraphOnboardingError as exc:
        return {
            "repository": str(repo_path),
            "ready": False,
            "index": index,
            "toolchain": toolchain,
            "server": None,
            "server_command": {
                "ready": False,
                "detail": "integration receipt is invalid",
            },
            "clients": [],
            "receipt_error": str(exc),
        }
    clients: list[dict[str, object]] = []
    server_command = {"ready": False, "detail": "receipt not found"}
    if receipt is not None:
        try:
            inspection = inspect_server_command(receipt.server, repo_path)
            server_command = {
                "ready": inspection.ready,
                "detail": inspection.detail,
            }
        except CodeGraphOnboardingError as exc:
            server_command = {"ready": False, "detail": str(exc)}
        for managed in receipt.clients:
            try:
                inspection = inspect_client_registration(
                    managed.name,
                    receipt.server,
                    repo_path,
                )
                clients.append(
                    {
                        "name": managed.name,
                        "scope": managed.scope,
                        "receipt_state": managed.state,
                        "installed": inspection.installed,
                        "configured": inspection.exists,
                        "matches": inspection.matches,
                        "detail": inspection.detail,
                    }
                )
            except CodeGraphOnboardingError as exc:
                clients.append(
                    {
                        "name": managed.name,
                        "scope": managed.scope,
                        "receipt_state": managed.state,
                        "installed": True,
                        "configured": False,
                        "matches": False,
                        "detail": str(exc),
                    }
                )
    clients_ready = bool(clients) and all(
        item["receipt_state"] == "configured" and item["matches"] for item in clients
    )
    server = (
        {
            "name": receipt.server.name,
            "command": receipt.server.command,
            "args": list(receipt.server.args),
        }
        if receipt is not None
        else None
    )
    ready = bool(
        index["ready"]
        and toolchain["ready"]
        and server_command["ready"]
        and clients_ready
    )
    return {
        "repository": str(repo_path),
        "ready": ready,
        "index": index,
        "toolchain": toolchain,
        "server_command": server_command,
        "server": server,
        "clients": clients,
        "receipt": (
            str(codegraph_receipt_path(repo_path)) if receipt is not None else None
        ),
    }


def _run_codegraph_status(args: argparse.Namespace) -> int:
    repo_path = resolve_repo_path(args.repo)
    report = _codegraph_status_report(repo_path)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if report["ready"] else 1

    print(f"CodeGraph:  {'READY' if report['ready'] else 'NOT READY'}")
    print(f"Repository: {report['repository']}")
    index = report["index"]
    print(
        "Index:      "
        + (
            "bm25 + symbol_graph are current"
            if index["ready"]
            else str(index["detail"])
        )
    )
    source_selection = index.get(
        "source_selection",
        {"recorded": False, "exclude_subtrees": [], "digest": None},
    )
    if source_selection["recorded"]:
        print(
            "Excluded:   " + (", ".join(source_selection["exclude_subtrees"]) or "none")
        )
    elif index.get("manifest_present"):
        print("Excluded:   unrecorded legacy policy")
    else:
        print("Excluded:   unrecorded (manifest missing)")
    toolchain = report["toolchain"]
    toolchain_detail = "ready"
    if not toolchain["ready"]:
        parts = [*toolchain["missing"], *toolchain["notes"]]
        toolchain_detail = "; ".join(parts) or "not ready"
    print("Toolchain:  " + toolchain_detail)
    server_command = report["server_command"]
    print(
        "MCP command: "
        + ("ready" if server_command["ready"] else str(server_command["detail"]))
    )
    if not report["clients"]:
        print("Clients:    no managed Codex or Claude Code registration")
    for client in report["clients"]:
        marker = "OK" if client["matches"] else "MISSING"
        print(
            f"  [{marker:<7}] {client['name']}: {client['detail']} "
            f"({client['scope']})"
        )
    return 0 if report["ready"] else 1


def _selected_uninstall_clients(
    values: Sequence[str] | None,
    receipt,
) -> tuple[str, ...]:
    from .codegraph_onboarding import CODEGRAPH_CLIENTS

    requested = tuple(values or ("all",))
    unknown = sorted(set(requested) - {"all", *CODEGRAPH_CLIENTS})
    if unknown:
        raise CLIError(f"unsupported agent client: {', '.join(unknown)}")
    if "all" in requested and len(requested) != 1:
        raise CLIError("--agent all cannot be combined with another client")
    if requested == ("all",):
        return tuple(item.name for item in receipt.clients)
    return tuple(name for name in CODEGRAPH_CLIENTS if name in requested)


def _run_codegraph_uninstall(args: argparse.Namespace) -> int:
    from .codegraph_onboarding import (
        CodeGraphOnboardingError,
        inspect_client_registration,
        load_codegraph_receipt,
        remove_client_registration,
        remove_codegraph_receipt,
        write_codegraph_receipt,
    )

    repo_path = resolve_repo_path(args.repo)
    try:
        receipt = load_codegraph_receipt(repo_path)
        if receipt is None:
            print("No CodeNib-managed CodeGraph agent registration was found.")
            return 0
        selected = _selected_uninstall_clients(args.agent, receipt)
        for client in selected:
            managed = receipt.client(client)
            if managed is None:
                print(f"{client}: not managed for this repository")
                continue
            inspection = inspect_client_registration(
                client,
                receipt.server,
                repo_path,
            )
            if not inspection.installed:
                raise CodeGraphOnboardingError(
                    f"cannot safely remove {client}: client command not found"
                )
            if inspection.exists and not inspection.matches and not args.force:
                raise CodeGraphOnboardingError(
                    f"refusing to remove drifted {client} registration "
                    f"{receipt.server.name}; inspect it or repeat with --force"
                )
            if args.dry_run:
                action = "remove" if inspection.exists else "reconcile missing"
                print(f"{client}: would {action} {receipt.server.name}")
                continue
            if inspection.exists:
                remove_client_registration(
                    client,
                    receipt.server,
                    repo_path,
                )
                after = inspect_client_registration(
                    client,
                    receipt.server,
                    repo_path,
                )
                if after.exists:
                    raise CodeGraphOnboardingError(
                        f"{client} still reports MCP server {receipt.server.name}"
                    )
            receipt = receipt.without_client(client)
            if receipt.clients:
                write_codegraph_receipt(receipt)
            else:
                remove_codegraph_receipt(receipt)
            print(f"{client}: removed {receipt.server.name}")
        if args.dry_run:
            print("Dry run complete; no client configuration or receipt changed.")
        elif receipt.clients:
            print("CodeGraph indexes were preserved for the remaining clients.")
        else:
            print("Agent registrations removed; CodeGraph indexes were preserved.")
        return 0
    except (CodeGraphOnboardingError, OSError) as exc:
        raise _codegraph_error(exc) from exc


def _add_embedding_route_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--embedding-provider",
        help="embedding backend: huggingface or openai",
    )
    parser.add_argument(
        "--embedding-model",
        help="embedding model id exposed by the selected backend",
    )
    parser.add_argument(
        "--embedding-dimension",
        type=int,
        help="vector width produced by the embedding model",
    )
    parser.add_argument(
        "--embedding-endpoint",
        "--embedding-base-url",
        dest="embedding_endpoint",
        help="API base for a BYO OpenAI-compatible embedding service",
    )
    parser.add_argument(
        "--embedding-api-key-env",
        help="environment variable containing the embedding API key",
    )


def _add_source_selection_arguments(parser: argparse.ArgumentParser) -> None:
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--exclude-dir",
        action="append",
        default=None,
        metavar="PATH",
        help=(
            "replace persisted custom exclusions with this exact "
            "repository-relative subtree; repeat for multiple paths"
        ),
    )
    selection.add_argument(
        "--clear-exclude-dirs",
        action="store_true",
        help="clear persisted custom subtree exclusions",
    )


def _add_jobs_worker_arguments(parser: argparse.ArgumentParser) -> None:
    """Add exact local worker topology and lease arguments."""

    parser.add_argument(
        "repo",
        help="trusted local repository source for eligible jobs",
    )
    parser.add_argument(
        "--cache-dir",
        required=True,
        help="existing compiler cache containing current BM25/vector views",
    )
    parser.add_argument(
        "--catalog",
        required=True,
        help="existing initialized SQLite catalog path",
    )
    parser.add_argument(
        "--cas-root",
        required=True,
        help="existing fully preprovisioned local CAS root",
    )
    parser.add_argument(
        "--workspace-root",
        required=True,
        help="existing private owner-only LocalWorkspaceProvider root",
    )
    parser.add_argument(
        "--repository",
        required=True,
        help="canonical owner/repository key eligible for this worker",
    )
    parser.add_argument(
        "--namespace",
        default="default",
        help="logical catalog namespace",
    )
    parser.add_argument(
        "--lease-duration-ms",
        type=_argparse_positive_int,
        default=30_000,
        help="job lease duration",
    )
    parser.add_argument(
        "--heartbeat-interval-ms",
        type=_argparse_positive_int,
        default=5_000,
        help="worker heartbeat interval (must be less than one third of the lease)",
    )
    parser.add_argument(
        "--scan-limit",
        type=_argparse_positive_int,
        default=64,
        help="maximum advisory candidates examined per page (at most 256)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable compact JSON output",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codenib",
        description="Build and serve repository context for coding agents.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {package_version()}",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    index_parser = subparsers.add_parser(
        "index",
        help="build or update repository indexes",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    index_parser.add_argument("repo", nargs="?", default=".")
    index_parser.add_argument("--preset", choices=_PRESET_CHOICES, default="auto")
    index_parser.add_argument(
        "--language",
        action="append",
        default=[],
        help="source language; repeat or use a comma-separated list",
    )
    index_parser.add_argument(
        "--view",
        action="append",
        default=[],
        help="view override; repeat or use a comma-separated list",
    )
    index_parser.add_argument(
        "--rebuild",
        action="store_true",
        help="force a clean rebuild even when an existing view is current",
    )
    _add_source_selection_arguments(index_parser)
    _add_embedding_route_arguments(index_parser)
    index_parser.add_argument(
        "--publish-retained",
        action="store_true",
        help="atomically publish BM25/vector results to retained storage",
    )
    index_parser.add_argument(
        "--catalog",
        default=None,
        help="existing initialized SQLite catalog used with --publish-retained",
    )
    index_parser.add_argument(
        "--cas-root",
        default=None,
        help="existing fully preprovisioned local CAS root",
    )
    index_parser.add_argument(
        "--workspace-root",
        default=None,
        help="existing private owner-only LocalWorkspaceProvider root",
    )
    index_parser.add_argument(
        "--repository",
        default=None,
        help="canonical owner/repository key for retained publication",
    )
    index_parser.add_argument(
        "--namespace",
        default=None,
        help="logical catalog namespace; defaults to default when publishing",
    )
    index_parser.add_argument(
        "--ref",
        default=None,
        help="repository ref advanced by retained publication; defaults to main",
    )
    index_parser.add_argument(
        "--expected-generation",
        type=_argparse_nonnegative_int,
        default=None,
        help="required current generation for optimistic retained publication",
    )
    index_parser.set_defaults(handler=_run_index)

    wiki_parser = subparsers.add_parser(
        "wiki",
        help="index a repository and open its local Wiki",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    wiki_parser.add_argument("repo", nargs="?", default=".")
    wiki_parser.add_argument("--preset", choices=_PRESET_CHOICES, default="auto")
    wiki_parser.add_argument("--language", action="append", default=[])
    wiki_parser.add_argument("--view", action="append", default=[])
    wiki_parser.add_argument(
        "--rebuild",
        action="store_true",
        help="force a clean rebuild even when an existing view is current",
    )
    _add_source_selection_arguments(wiki_parser)
    _add_embedding_route_arguments(wiki_parser)
    wiki_parser.add_argument(
        "--no-index",
        action="store_true",
        help="reuse an existing manifest without updating it",
    )
    wiki_parser.add_argument(
        "--generate",
        "--agent-wiki",
        dest="agent_wiki",
        action="store_true",
        help="generate conceptual, source-grounded pages with the configured LLM",
    )
    wiki_parser.add_argument(
        "--model",
        help="LiteLLM model string used for Wiki generation and Ask",
    )
    wiki_parser.add_argument(
        "--model-provider",
        help="canonicalize the model through openai or another LiteLLM provider",
    )
    wiki_parser.add_argument(
        "--api-base",
        help="optional OpenAI-compatible API base for the configured model",
    )
    wiki_parser.add_argument(
        "--api-key-env",
        help="name of the environment variable containing the model API key",
    )
    wiki_parser.add_argument(
        "--model-option",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help=(
            "provider-specific LiteLLM option; repeat as needed and use dotted "
            "keys for nested values"
        ),
    )
    wiki_parser.add_argument("--host", default="127.0.0.1")
    wiki_parser.add_argument("--port", type=argparse_tcp_port, default=3000)
    wiki_parser.add_argument("--api-host", default="127.0.0.1")
    wiki_parser.add_argument("--api-port", type=argparse_tcp_port, default=8000)
    wiki_parser.add_argument(
        "--frontend-dir",
        help="path to a prebuilt CodeNib frontend or web source checkout",
    )
    wiki_parser.add_argument("--no-open", action="store_true")
    wiki_parser.add_argument(
        "--no-install-frontend",
        action="store_true",
        help="do not install missing dependencies for a source frontend",
    )
    wiki_parser.add_argument(
        "--audit",
        action="store_true",
        help=(
            "generate every page, run deterministic publication-quality gates, "
            "and exit without starting the frontend"
        ),
    )
    wiki_parser.add_argument(
        "--audit-json",
        action="store_true",
        help="run the Wiki audit and print its complete JSON report",
    )
    wiki_parser.set_defaults(handler=_run_wiki)

    export_parser = subparsers.add_parser(
        "export",
        help="export an indexed repository Wiki for static hosting",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    export_parser.add_argument("repo", nargs="?", default=".")
    export_parser.add_argument(
        "--output",
        help="output directory; defaults beside the repository manifest",
    )
    export_parser.add_argument(
        "--base-path",
        default="/",
        help="URL path where the static site will be mounted",
    )
    export_parser.add_argument(
        "--frontend-dir",
        help="path to a prebuilt CodeNib frontend or web source checkout",
    )
    export_parser.set_defaults(handler=_run_export)

    artifact_parser = subparsers.add_parser(
        "artifact",
        help="package portable repository-context artifacts",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    artifact_subparsers = artifact_parser.add_subparsers(
        dest="artifact_command",
        required=True,
    )
    artifact_pack_parser = artifact_subparsers.add_parser(
        "pack",
        help="stage current manifest views as a portable context artifact",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    artifact_pack_parser.add_argument("repo", nargs="?", default=".")
    artifact_pack_parser.add_argument("--output")
    artifact_pack_parser.add_argument(
        "--repository",
        help="stable owner/repository identity; defaults to origin or directory name",
    )
    artifact_pack_parser.add_argument(
        "--view",
        action="append",
        default=[],
        help="current view to include; repeat or use a comma-separated list",
    )
    artifact_pack_parser.set_defaults(handler=_run_artifact_pack)

    artifact_import_cache_parser = artifact_subparsers.add_parser(
        "import-cache",
        help="import existing compiler query views into retained storage",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    artifact_import_cache_parser.add_argument(
        "repo",
        help="existing repository source captured for the imported cache",
    )
    artifact_import_cache_parser.add_argument(
        "--cache-dir",
        required=True,
        help="existing compiler cache containing repo_manifest.json and selected views",
    )
    artifact_import_cache_parser.add_argument(
        "--view",
        action="append",
        default=[],
        help=(
            "current cache view to import (bm25 or vector); repeat or use a "
            "comma-separated list; defaults to bm25"
        ),
    )
    artifact_import_cache_parser.add_argument(
        "--catalog",
        required=True,
        help="existing initialized SQLite catalog path",
    )
    artifact_import_cache_parser.add_argument(
        "--cas-root",
        required=True,
        help="existing fully preprovisioned local CAS root",
    )
    artifact_import_cache_parser.add_argument(
        "--workspace-root",
        required=True,
        help="existing private owner-only LocalWorkspaceProvider root",
    )
    artifact_import_cache_parser.add_argument(
        "--repository",
        required=True,
        help="canonical owner/repository key",
    )
    artifact_import_cache_parser.add_argument(
        "--namespace",
        default="default",
        help="logical catalog namespace",
    )
    artifact_import_cache_parser.add_argument(
        "--ref",
        default="main",
        help="repository ref advanced by the import",
    )
    artifact_import_cache_parser.add_argument(
        "--expected-generation",
        type=_argparse_nonnegative_int,
        default=0,
        help="required current generation for optimistic ref publication",
    )
    artifact_import_cache_parser.set_defaults(handler=_run_artifact_import_cache)

    artifact_materialize_parser = artifact_subparsers.add_parser(
        "materialize",
        help="materialize one retained catalog ref or snapshot as a context artifact",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    artifact_materialize_parser.add_argument(
        "--catalog",
        required=True,
        help="existing initialized SQLite catalog path",
    )
    artifact_materialize_parser.add_argument(
        "--cas-root",
        required=True,
        help="existing fully preprovisioned local CAS root",
    )
    artifact_materialize_parser.add_argument(
        "--workspace-root",
        required=True,
        help="existing private owner-only LocalWorkspaceProvider root",
    )
    artifact_materialize_parser.add_argument(
        "--repository",
        required=True,
        help="canonical owner/repository key",
    )
    artifact_materialize_parser.add_argument(
        "--namespace",
        default="default",
        help="logical catalog namespace",
    )
    materialize_selector = artifact_materialize_parser.add_mutually_exclusive_group()
    materialize_selector.add_argument(
        "--ref",
        help="repository ref to resolve; defaults to main",
    )
    materialize_selector.add_argument(
        "--snapshot",
        help="immutable snapshot ID to materialize instead of a ref",
    )
    artifact_materialize_parser.add_argument(
        "--expected-generation",
        type=_argparse_positive_int,
        help="required current generation for optimistic ref resolution",
    )
    artifact_materialize_parser.add_argument(
        "--output",
        required=True,
        help="missing output directory below the workspace root",
    )
    artifact_materialize_parser.set_defaults(handler=_run_artifact_materialize)

    artifact_verify_parser = artifact_subparsers.add_parser(
        "verify",
        help="verify artifact integrity and optionally bind an exact checkout",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    artifact_verify_parser.add_argument("path")
    artifact_verify_parser.add_argument(
        "--repo",
        help="exact checkout used to verify commit and source identity",
    )
    artifact_verify_parser.add_argument(
        "--repository",
        help="expected owner/repository identity",
    )
    artifact_verify_parser.add_argument(
        "--commit",
        help="expected full Git commit",
    )
    artifact_verify_parser.set_defaults(handler=_run_artifact_verify)

    artifact_fetch_parser = artifact_subparsers.add_parser(
        "fetch",
        help="download and verify a commit-matched GitHub Actions artifact",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    artifact_fetch_parser.add_argument("repository", help="GitHub owner/repository")
    artifact_fetch_parser.add_argument(
        "--repo",
        default=".",
        help="exact local checkout to bind",
    )
    artifact_fetch_parser.add_argument("--commit", help="full commit; defaults to HEAD")
    artifact_fetch_parser.add_argument("--output")
    artifact_fetch_parser.add_argument("--artifact-name")
    artifact_fetch_parser.add_argument(
        "--token-env",
        default="GH_TOKEN",
        help="environment variable containing a GitHub token with Actions read",
    )
    artifact_fetch_parser.add_argument(
        "--github-api-url",
        default=os.environ.get("GITHUB_API_URL", "https://api.github.com"),
    )
    artifact_fetch_parser.add_argument("--force", action="store_true")
    artifact_fetch_parser.set_defaults(handler=_run_artifact_fetch)

    artifact_config_parser = artifact_subparsers.add_parser(
        "mcp-config",
        help="render a Codex, Claude, or generic MCP configuration",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    artifact_config_parser.add_argument("path")
    artifact_config_parser.add_argument("--repo", default=".")
    artifact_config_parser.add_argument(
        "--host",
        choices=("claude", "codex", "json"),
        required=True,
    )
    artifact_config_parser.add_argument("--name", default="codenib")
    artifact_config_parser.add_argument("--command", default="codenib")
    artifact_config_parser.add_argument("--repository")
    artifact_config_parser.add_argument(
        "--claude-scope",
        choices=("local", "project", "user"),
        default="project",
    )
    artifact_config_parser.set_defaults(handler=_run_artifact_mcp_config)

    jobs_parser = subparsers.add_parser(
        "jobs",
        help="inspect and execute durable index jobs",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    jobs_subparsers = jobs_parser.add_subparsers(
        dest="jobs_command",
        required=True,
    )
    jobs_run_once_parser = jobs_subparsers.add_parser(
        "run-once",
        help="scan one bounded page and execute at most one supported job",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    _add_jobs_worker_arguments(jobs_run_once_parser)
    jobs_run_once_parser.set_defaults(handler=_run_jobs_run_once)

    jobs_run_parser = jobs_subparsers.add_parser(
        "run",
        help="continuously execute supported jobs with cursor-fair scanning",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    _add_jobs_worker_arguments(jobs_run_parser)
    jobs_run_parser.add_argument(
        "--initial-idle-delay-ms",
        type=_argparse_positive_int,
        default=250,
        help="delay after the first complete idle catalog cycle",
    )
    jobs_run_parser.add_argument(
        "--max-idle-delay-ms",
        type=_argparse_positive_int,
        default=5_000,
        help="maximum exponential delay between complete idle cycles",
    )
    jobs_run_parser.add_argument(
        "--max-cycles",
        type=_argparse_positive_int,
        help="stop after this many complete keyspace cycles instead of running forever",
    )
    jobs_run_parser.set_defaults(handler=_run_jobs_continuous)

    publish_parser = subparsers.add_parser(
        "publish",
        help="incrementally build and publish a static Wiki plus context artifact",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    publish_parser.add_argument("repo", nargs="?", default=".")
    publish_parser.add_argument(
        "--preset",
        choices=("auto", "fast", "semantic"),
        default="auto",
    )
    publish_parser.add_argument("--language", action="append", default=[])
    publish_parser.add_argument("--view", action="append", default=[])
    publish_parser.add_argument(
        "--rebuild",
        action="store_true",
        help="force a clean rebuild even when an existing view is current",
    )
    _add_source_selection_arguments(publish_parser)
    _add_embedding_route_arguments(publish_parser)
    publish_parser.add_argument("--site-output")
    publish_parser.add_argument("--context-output")
    publish_parser.add_argument(
        "--repository",
        help="stable owner/repository identity; defaults to origin or directory name",
    )
    publish_parser.add_argument(
        "--base-path",
        default="/",
        help="URL path where the static site will be mounted",
    )
    publish_parser.add_argument(
        "--frontend-dir",
        help="path to a prebuilt CodeNib frontend or web source checkout",
    )
    publish_parser.set_defaults(handler=_run_publish)

    mcp_parser = subparsers.add_parser(
        "mcp",
        help="serve an indexed repository over MCP stdio",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    mcp_parser.add_argument(
        "path",
        nargs="?",
        default=None,
        help="repository directory or repo_manifest.json",
    )
    mcp_parser.add_argument(
        "--artifact",
        help="verified portable context artifact directory",
    )
    mcp_parser.add_argument(
        "--repo",
        help="optional exact checkout bound to --artifact or retained startup",
    )
    mcp_parser.add_argument(
        "--repository",
        help=(
            "expected owner/repository identity for --artifact, or the required "
            "catalog repository key for retained startup"
        ),
    )
    mcp_parser.add_argument(
        "--catalog",
        help="existing initialized SQLite catalog for retained startup",
    )
    mcp_parser.add_argument(
        "--cas-root",
        help="existing fully preprovisioned local CAS root",
    )
    mcp_parser.add_argument(
        "--workspace-root",
        help="existing private owner-only LocalWorkspaceProvider root",
    )
    mcp_parser.add_argument(
        "--output",
        help="missing retained materialization directory below the workspace root",
    )
    mcp_parser.add_argument(
        "--namespace",
        help="logical retained catalog namespace; defaults to default",
    )
    retained_mcp_selector = mcp_parser.add_mutually_exclusive_group()
    retained_mcp_selector.add_argument(
        "--ref",
        help="retained repository ref to resolve once; defaults to main",
    )
    retained_mcp_selector.add_argument(
        "--snapshot",
        help="immutable retained snapshot ID to load instead of a ref",
    )
    mcp_parser.add_argument(
        "--expected-generation",
        type=_argparse_positive_int,
        help="required current generation for retained ref resolution",
    )
    mcp_parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
    )
    mcp_parser.add_argument(
        "--tool-surface",
        choices=("full", "explore"),
        default="full",
        help="MCP tool surface to expose",
    )
    mcp_parser.add_argument(
        "--runtime-probe",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    mcp_parser.set_defaults(handler=_run_mcp)

    codegraph_parser = subparsers.add_parser(
        "codegraph",
        help="prepare CodeGraph and connect it to coding agents",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    codegraph_subparsers = codegraph_parser.add_subparsers(
        dest="codegraph_command",
        required=True,
    )
    codegraph_init_parser = codegraph_subparsers.add_parser(
        "init",
        help="install graph tools, index a repository, and configure MCP clients",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    codegraph_init_parser.add_argument("repo", nargs="?", default=".")
    codegraph_init_parser.add_argument(
        "--language",
        action="append",
        default=[],
        help="source language override; repeat or use a comma-separated list",
    )
    codegraph_init_parser.add_argument(
        "--agent",
        action="append",
        choices=("auto", "codex", "claude"),
        help=(
            "agent client to configure; repeat for both, or omit to auto-detect "
            "installed clients"
        ),
    )
    codegraph_init_parser.add_argument(
        "--command",
        dest="server_command",
        help="CodeNib executable recorded in the MCP client configuration",
    )
    codegraph_init_parser.add_argument(
        "--rebuild",
        action="store_true",
        help="force a clean rebuild of the graph views",
    )
    _add_source_selection_arguments(codegraph_init_parser)
    codegraph_init_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show toolchain, index, and client actions without changing state",
    )
    codegraph_init_parser.set_defaults(handler=_run_codegraph_init)

    codegraph_status_parser = codegraph_subparsers.add_parser(
        "status",
        help="verify the graph index, source identity, toolchain, and clients",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    codegraph_status_parser.add_argument("repo", nargs="?", default=".")
    codegraph_status_parser.add_argument(
        "--json",
        action="store_true",
        help="print a machine-readable status report",
    )
    codegraph_status_parser.set_defaults(handler=_run_codegraph_status)

    codegraph_uninstall_parser = codegraph_subparsers.add_parser(
        "uninstall",
        help="remove only CodeNib-managed agent registrations",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    codegraph_uninstall_parser.add_argument("repo", nargs="?", default=".")
    codegraph_uninstall_parser.add_argument(
        "--agent",
        action="append",
        choices=("all", "codex", "claude"),
        help="managed client to remove; repeat as needed (default: all)",
    )
    codegraph_uninstall_parser.add_argument(
        "--force",
        action="store_true",
        help="remove a managed server even when its native config has drifted",
    )
    codegraph_uninstall_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show removals without changing client configuration or receipts",
    )
    codegraph_uninstall_parser.set_defaults(handler=_run_codegraph_uninstall)

    toolchain_parser = subparsers.add_parser(
        "toolchain",
        help="inspect or install repository language providers",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    toolchain_subparsers = toolchain_parser.add_subparsers(
        dest="toolchain_command",
        required=True,
    )
    for command, handler in (
        ("status", _run_toolchain_status),
        ("install", _run_toolchain_install),
    ):
        command_parser = toolchain_subparsers.add_parser(
            command,
            formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        )
        command_parser.add_argument("repo", nargs="?", default=".")
        command_parser.add_argument(
            "--language",
            action="append",
            default=[],
            help="source language override; repeat or use a comma-separated list",
        )
        command_parser.add_argument(
            "--scope",
            choices=("graph", "lsp", "all"),
            default="graph",
            help="provider surface to inspect or install",
        )
        if command == "install":
            command_parser.add_argument(
                "--dry-run",
                action="store_true",
                help="print pinned package-manager commands without running them",
            )
        command_parser.set_defaults(handler=handler)

    doctor_parser = subparsers.add_parser(
        "doctor",
        help="check local runtime capabilities",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    doctor_parser.add_argument(
        "repo",
        nargs="?",
        help="repository used for language-specific graph checks",
    )
    doctor_parser.add_argument(
        "--language",
        action="append",
        default=[],
        help="source language override; repeat or use a comma-separated list",
    )
    doctor_parser.add_argument(
        "--require",
        action="append",
        choices=("core", "wiki", "semantic", "graph", "agent", "mcp"),
        help="capability group that must pass; repeat as needed",
    )
    doctor_parser.add_argument(
        "--model",
        help="LiteLLM model string to validate",
    )
    doctor_parser.add_argument(
        "--model-provider",
        help="canonicalize the model through openai or another LiteLLM provider",
    )
    doctor_parser.add_argument(
        "--api-base",
        help="optional OpenAI-compatible API base to validate",
    )
    doctor_parser.add_argument(
        "--api-key-env",
        help="name of the environment variable containing the model API key",
    )
    doctor_parser.add_argument(
        "--model-option",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help=(
            "provider-specific LiteLLM option to validate or probe; repeat as " "needed"
        ),
    )
    doctor_parser.add_argument(
        "--probe-model",
        action="store_true",
        help=(
            "send minimal text, tool-calling, and structured-output requests "
            "after validating configuration"
        ),
    )
    _add_embedding_route_arguments(doctor_parser)
    doctor_parser.add_argument(
        "--probe-embedding",
        action="store_true",
        help="send one embedding request and verify the returned vector width",
    )
    doctor_parser.set_defaults(handler=_run_doctor)

    return parser


def run(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        from .toolchains import activate_managed_toolchain

        activate_managed_toolchain()
        return int(args.handler(args) or 0)
    except CLIError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 130


def main() -> int:
    return run()


__all__ = [
    "build_parser",
    "detect_languages",
    "index_repository",
    "main",
    "normalize_languages",
    "package_version",
    "resolve_manifest_path",
    "run",
]
