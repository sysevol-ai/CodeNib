# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""User-facing CodeNib command line interface."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shlex
import shutil
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from ._version import package_version
from .provider_routes import (
    InferenceRoute,
    normalize_provider,
    resolve_embedding_artifact_route,
    resolve_inference_route,
)
from .repository_filters import DEFAULT_IGNORED_DIRS
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


class CLIError(RuntimeError):
    """A user-actionable command error."""


@dataclass(frozen=True, slots=True)
class _ResolvedModelBackend:
    model: str
    api_base: str | None
    api_key: str | None = field(default=None, repr=False)
    auth_source: str | None = None


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


def detect_languages(repo_path: str | os.PathLike[str]) -> list[str]:
    """Detect supported source languages, ordered by source-file count."""
    from .languages import extension_to_language_map

    root = Path(repo_path).expanduser().resolve()
    extension_map = extension_to_language_map("chunker")
    counts: Counter[str] = Counter()

    for _current_root, dirs, files in os.walk(root):
        dirs[:] = sorted(
            directory
            for directory in dirs
            if directory not in DEFAULT_IGNORED_DIRS
            and not directory.startswith(".codenib")
        )
        for filename in files:
            language = extension_map.get(Path(filename).suffix.lower())
            if language:
                counts[language] += 1

    return [
        language
        for language, _ in sorted(
            counts.items(),
            key=lambda item: (-item[1], item[0]),
        )
    ]


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
    path = Path(value).expanduser().resolve()
    if not path.is_dir():
        raise CLIError(f"repository directory does not exist: {path}")
    return path


def resolve_manifest_path(value: str) -> Path:
    """Resolve either a repository directory or a manifest path."""
    from .compiler.manifest import MANIFEST_FILENAME
    from .paths import legacy_repo_index_dir, repo_index_dir

    path = Path(value).expanduser().resolve()
    if path.is_dir():
        candidates = (
            repo_index_dir(path) / MANIFEST_FILENAME,
            legacy_repo_index_dir(path) / MANIFEST_FILENAME,
        )
        path = next(
            (candidate for candidate in candidates if candidate.is_file()),
            candidates[0],
        )
    if not path.is_file():
        raise CLIError(
            f"manifest not found: {path}\n"
            "Run `codenib index <repo>` before starting this command."
        )
    return path


def _selected_languages(repo_path: Path, explicit: Iterable[str]) -> list[str]:
    languages = normalize_languages(explicit)
    if languages:
        return languages
    languages = detect_languages(repo_path)
    if not languages:
        raise CLIError(
            "no supported source language was detected; pass --language explicitly"
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


def index_repository(
    repo_path: Path,
    *,
    languages: Sequence[str],
    views: Sequence[str],
    rebuild: bool = False,
    embedding_provider: str = "huggingface",
    embedding_model: str | None = None,
    embedding_dimension: int | None = None,
    embedding_endpoint: str | None = None,
    embedding_credential_env: str | None = None,
):
    """Build or update the requested repository views."""
    from .toolchains import activate_managed_toolchain

    activate_managed_toolchain()
    from .compiler.index_builders import IndexBuilderRegistry, register_default_builders
    from .compiler.index_compiler import IndexCompiler, IndexCompilerConfig
    from .compiler.manifest import MANIFEST_FILENAME
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
        allow_partial_graph_languages=True,
    )
    compiler = IndexCompiler(
        registry,
        IndexCompilerConfig(
            index_types=list(views),
            languages=list(languages),
        ),
    )
    cache_dir = repo_index_dir(repo_path)
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


def _run_index(
    args: argparse.Namespace,
    *,
    selected_views: Sequence[str] | None = None,
) -> int:
    repo_path = resolve_repo_path(args.repo)
    languages = _selected_languages(repo_path, args.language)
    views = (
        list(selected_views)
        if selected_views is not None
        else _selected_views_for_args(args)
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
    manifest, failed = index_repository(repo_path, **index_kwargs)
    _print_index_summary(manifest, views)
    if failed:
        print(
            f"Failed views: {', '.join(failed)}",
            file=sys.stderr,
        )
        return 1
    return 0


def _run_mcp(args: argparse.Namespace) -> int:
    _require_modules(("mcp",), extra="mcp", feature="the MCP server")
    from .mcp.server import main as mcp_main

    if args.artifact:
        repo_path = resolve_repo_path(args.repo)
        command = [
            "--artifact",
            str(Path(args.artifact).expanduser().resolve()),
            "--repo",
            str(repo_path),
            "--log-level",
            args.log_level,
            "--tool-surface",
            args.tool_surface,
        ]
        if args.repository:
            command.extend(("--repository", args.repository))
        mcp_main(command)
    else:
        manifest_path = resolve_manifest_path(args.path)
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


def _run_export(args: argparse.Namespace) -> int:
    repo_path = resolve_repo_path(args.repo)
    manifest_path = resolve_manifest_path(str(repo_path))
    output_dir = (
        Path(args.output).expanduser().resolve()
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


def _require_clean_artifact_checkout(repo_path: Path) -> None:
    """Require portable artifacts to describe the checked-out Git commit."""

    from .source_fingerprint import repository_source_is_dirty

    if repository_source_is_dirty(repo_path):
        raise CLIError(
            "portable context artifacts require a clean Git checkout at HEAD. "
            "Commit or stash source-visible changes, or use `codenib wiki` or "
            "`codenib export` to inspect the current working tree."
        )


def _paths_overlap(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def _validate_publish_outputs(
    repo_path: Path,
    *,
    site_output: Path | None,
    context_output: Path | None,
) -> None:
    """Reject destructive publication paths before any index work starts."""

    if (
        site_output is not None
        and context_output is not None
        and _paths_overlap(site_output, context_output)
    ):
        raise CLIError("Wiki and context artifact outputs must not overlap")

    from .paths import repo_index_dir

    protected = (
        (repo_path, "repository"),
        (repo_index_dir(repo_path).expanduser().resolve(), "index state"),
    )
    for output, output_name in (
        (site_output, "Wiki"),
        (context_output, "context artifact"),
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
    _require_clean_artifact_checkout(repo_path)
    manifest_path = resolve_manifest_path(str(repo_path))
    from .artifacts import stage_context_artifact
    from .compiler.manifest import RepoManifest

    manifest = RepoManifest.load(manifest_path)
    output_dir = (
        Path(args.output).expanduser().resolve()
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
            "artifact operations require a Git checkout with a full resolved HEAD"
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
        Path(args.site_output).expanduser().resolve() if args.site_output else None
    )
    context_output = (
        Path(args.context_output).expanduser().resolve()
        if args.context_output
        else None
    )
    _validate_publish_outputs(
        repo_path,
        site_output=site_output,
        context_output=context_output,
    )
    _require_clean_artifact_checkout(repo_path)

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
    site_output = site_output or _default_distribution_dir(
        manifest_path, "wiki", manifest.commit
    )
    context_output = context_output or _default_distribution_dir(
        manifest_path, "context", manifest.commit
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

    registry = RepoRegistry(config)
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
    languages = _selected_languages(repo_path, args.language)
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
        languages = _selected_languages(repo_path, args.language)
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
    languages = _selected_languages(repo_path, args.language)
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
        help="rebuild instead of incrementally updating an existing manifest",
    )
    _add_embedding_route_arguments(index_parser)
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
    wiki_parser.add_argument("--rebuild", action="store_true")
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
    publish_parser.add_argument("--rebuild", action="store_true")
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
        default=".",
        help="repository directory or repo_manifest.json",
    )
    mcp_parser.add_argument(
        "--artifact",
        help="verified portable context artifact directory",
    )
    mcp_parser.add_argument(
        "--repo",
        default=".",
        help="exact checkout bound to --artifact",
    )
    mcp_parser.add_argument(
        "--repository",
        help="expected owner/repository identity for --artifact",
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
    mcp_parser.set_defaults(handler=_run_mcp)

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
