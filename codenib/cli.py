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
import shutil
import sys
from collections import Counter
from pathlib import Path
from typing import Iterable, Sequence

from ._version import package_version
from .repository_filters import DEFAULT_IGNORED_DIRS

_PRESET_VIEWS = {
    "fast": ("bm25",),
    "semantic": ("bm25", "vector"),
    "graph": ("bm25", "symbol_graph"),
    "full": ("bm25", "vector", "symbol_graph", "zoekt"),
}


class CLIError(RuntimeError):
    """A user-actionable command error."""


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


def index_repository(
    repo_path: Path,
    *,
    languages: Sequence[str],
    views: Sequence[str],
    rebuild: bool = False,
):
    """Build or update the requested repository views."""
    from .compiler.index_builders import IndexBuilderRegistry, register_default_builders
    from .compiler.index_compiler import IndexCompiler, IndexCompilerConfig
    from .compiler.manifest import MANIFEST_FILENAME
    from .paths import repo_index_dir

    registry = IndexBuilderRegistry()
    register_default_builders(
        registry,
        languages=list(languages),
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


def _run_index(args: argparse.Namespace) -> int:
    repo_path = resolve_repo_path(args.repo)
    languages = _selected_languages(repo_path, args.language)
    views = _selected_views(args.preset, args.view)
    _check_view_dependencies(views)
    manifest, failed = index_repository(
        repo_path,
        languages=languages,
        views=views,
        rebuild=args.rebuild,
    )
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
    manifest_path = resolve_manifest_path(args.path)
    from .mcp.server import main as mcp_main

    mcp_main([str(manifest_path), "--log-level", args.log_level])
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


def _run_wiki(args: argparse.Namespace) -> int:
    repo_path = resolve_repo_path(args.repo)
    languages = _selected_languages(repo_path, args.language)
    views = _selected_views(args.preset, args.view)
    _check_view_dependencies(views)
    audit = bool(args.audit or args.audit_json)
    if (
        args.agent_wiki
        or audit
        or args.model
        or args.api_base
        or args.api_key_env
        or args.model_option
    ):
        _require_modules(
            ("litellm",),
            extra="agent",
            feature="model-backed Wiki features",
        )
    if args.api_key_env and not os.environ.get(args.api_key_env):
        raise CLIError(
            "API key environment variable is unset or empty: " f"{args.api_key_env}"
        )

    if args.no_index:
        manifest_path = resolve_manifest_path(str(repo_path))
    else:
        manifest, failed = index_repository(
            repo_path,
            languages=languages,
            views=views,
            rebuild=args.rebuild,
        )
        _print_index_summary(manifest, views)
        if failed:
            raise CLIError(
                "the Wiki was not started because requested views failed: "
                + ", ".join(failed)
            )
        manifest_path = resolve_manifest_path(str(repo_path))

    from .web.launcher import launch_local_wiki
    from .web.local import prepare_local_wiki

    try:
        local = prepare_local_wiki(
            repo_path,
            manifest_path,
            frontend_port=args.port,
            agent_wiki=args.agent_wiki or audit,
            model=args.model,
            api_base=args.api_base,
            api_key_env=args.api_key_env,
            model_options=_model_options_for_args(args),
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


def _check_view_dependencies(views: Sequence[str]) -> None:
    if "vector" in views:
        _require_modules(
            ("faiss", "sentence_transformers"),
            extra="semantic",
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
    model = (
        getattr(args, "model", None)
        or os.environ.get("CODENIB_DEMO_WIKI_MODEL")
        or os.environ.get("CODENIB_DEMO_MODEL")
    )
    if not model:
        return None
    api_base = (
        getattr(args, "api_base", None)
        or os.environ.get("CODENIB_DEMO_WIKI_API_BASE")
        or os.environ.get("CODENIB_DEMO_API_BASE")
    )
    key_env = getattr(args, "api_key_env", None)
    api_key = (
        os.environ.get(key_env)
        if key_env
        else os.environ.get("CODENIB_DEMO_WIKI_API_KEY")
        or os.environ.get("CODENIB_DEMO_API_KEY")
    )
    if key_env and not api_key:
        return (
            "Model configuration",
            False,
            f"{model}; {key_env} is unset or empty",
        )
    if not _check_module("litellm"):
        return ("Model configuration", False, f"{model}; LiteLLM is missing")
    options = _model_options_for_args(
        args,
        include_wiki_environment=(
            not getattr(args, "model", None)
            and bool(os.environ.get("CODENIB_DEMO_WIKI_MODEL"))
        ),
    )
    try:
        from .llm.diagnostics import diagnose_model_backend

        report = diagnose_model_backend(
            model=model,
            api_base=api_base,
            api_key=api_key,
            auth_source=(
                key_env
                or (
                    "CODENIB_DEMO_WIKI_API_KEY"
                    if os.environ.get("CODENIB_DEMO_WIKI_API_KEY")
                    else (
                        "CODENIB_DEMO_API_KEY"
                        if os.environ.get("CODENIB_DEMO_API_KEY")
                        else None
                    )
                )
            ),
            options=options,
        )
    except Exception as exc:  # noqa: BLE001 - diagnostic must report, not crash
        return ("Model configuration", False, f"{model}; {exc}")
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
        "semantic": [
            (
                "sentence-transformers",
                _check_module("sentence_transformers"),
                ("installed" if _check_module("sentence_transformers") else "missing"),
            ),
            (
                "FAISS",
                _check_module("faiss"),
                "installed" if _check_module("faiss") else "missing",
            ),
        ],
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
    model = (
        args.model
        or os.environ.get("CODENIB_DEMO_WIKI_MODEL")
        or os.environ.get("CODENIB_DEMO_MODEL")
    )
    if not model:
        return [
            ("Model text probe", False, "set --model or CODENIB_DEMO_MODEL"),
            ("Model tool probe", False, "skipped: model is not configured"),
            ("Model structured probe", False, "skipped: model is not configured"),
        ]
    api_base = (
        args.api_base
        or os.environ.get("CODENIB_DEMO_WIKI_API_BASE")
        or os.environ.get("CODENIB_DEMO_API_BASE")
    )
    api_key = (
        os.environ.get(args.api_key_env)
        if args.api_key_env
        else os.environ.get("CODENIB_DEMO_WIKI_API_KEY")
        or os.environ.get("CODENIB_DEMO_API_KEY")
    )
    options = _model_options_for_args(
        args,
        include_wiki_environment=(
            not getattr(args, "model", None)
            and bool(os.environ.get("CODENIB_DEMO_WIKI_MODEL"))
        ),
    )
    if args.api_key_env and not api_key:
        detail = f"{args.api_key_env} is unset or empty"
        return [
            ("Model text probe", False, detail),
            ("Model tool probe", False, "skipped: credentials are missing"),
            ("Model structured probe", False, "skipped: credentials are missing"),
        ]
    try:
        from pydantic import BaseModel

        from .llm.litellm_chat import LiteLLMChat, RetryConfig, human_message

        probe_options = dict(options)
        probe_options.setdefault("timeout", 20)
        llm = LiteLLMChat(
            model=model,
            temperature=0.0,
            max_tokens=32,
            api_base=api_base,
            api_key=api_key,
            extra_kwargs=probe_options,
            retry=RetryConfig(max_retries=0),
        )
        response = llm.complete(
            [{"role": "user", "content": "Reply with OK."}],
        )
    except Exception as exc:  # noqa: BLE001 - diagnostic result
        detail = _model_probe_error(exc, api_key=api_key)
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
                _model_probe_error(exc, api_key=api_key),
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
                _model_probe_error(exc, api_key=api_key),
            )
        )
    return checks


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
    index_parser.add_argument("--preset", choices=tuple(_PRESET_VIEWS), default="fast")
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
    index_parser.set_defaults(handler=_run_index)

    wiki_parser = subparsers.add_parser(
        "wiki",
        help="index a repository and open its local Wiki",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    wiki_parser.add_argument("repo", nargs="?", default=".")
    wiki_parser.add_argument("--preset", choices=tuple(_PRESET_VIEWS), default="fast")
    wiki_parser.add_argument("--language", action="append", default=[])
    wiki_parser.add_argument("--view", action="append", default=[])
    wiki_parser.add_argument("--rebuild", action="store_true")
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
    wiki_parser.add_argument("--port", type=int, default=3000)
    wiki_parser.add_argument("--api-host", default="127.0.0.1")
    wiki_parser.add_argument("--api-port", type=int, default=8000)
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
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
    )
    mcp_parser.set_defaults(handler=_run_mcp)

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
    doctor_parser.set_defaults(handler=_run_doctor)

    return parser


def run(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
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
