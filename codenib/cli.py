# SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""User-facing CodeNib command line interface."""

from __future__ import annotations

import argparse
import importlib.util
import os
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
    from .paths import REPO_INDEX_DIRNAME

    path = Path(value).expanduser().resolve()
    if path.is_dir():
        path = path / REPO_INDEX_DIRNAME / MANIFEST_FILENAME
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
    from .paths import REPO_INDEX_DIRNAME

    registry = IndexBuilderRegistry()
    register_default_builders(registry, languages=list(languages))
    compiler = IndexCompiler(
        registry,
        IndexCompilerConfig(
            index_types=list(views),
            languages=list(languages),
        ),
    )
    manifest_path = repo_path / REPO_INDEX_DIRNAME / MANIFEST_FILENAME
    if manifest_path.is_file() and not rebuild:
        manifest = compiler.update_repo(str(repo_path), index_types=list(views))
    else:
        manifest = compiler.compile_repo(str(repo_path), index_types=list(views))

    failed = [
        view
        for view in views
        if view not in manifest.indexes or manifest.indexes[view].status != "fresh"
    ]
    return manifest, failed


def _print_index_summary(manifest, views: Sequence[str]) -> None:
    from .compiler.manifest import MANIFEST_FILENAME
    from .paths import REPO_INDEX_DIRNAME

    manifest_path = Path(manifest.repo_path) / REPO_INDEX_DIRNAME / MANIFEST_FILENAME
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


def _run_wiki(args: argparse.Namespace) -> int:
    repo_path = resolve_repo_path(args.repo)
    languages = _selected_languages(repo_path, args.language)
    views = _selected_views(args.preset, args.view)
    _check_view_dependencies(views)
    if args.agent_wiki or args.model or args.api_base or args.api_key_env:
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

    local = prepare_local_wiki(
        repo_path,
        manifest_path,
        frontend_port=args.port,
        agent_wiki=args.agent_wiki,
        model=args.model,
        api_base=args.api_base,
        api_key_env=args.api_key_env,
    )
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
            ("igraph", "matplotlib"),
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
    try:
        from .llm import litellm_chat

        result = litellm_chat.litellm.validate_environment(
            model=model,
            api_base=api_base,
            api_key=api_key,
        )
    except Exception as exc:  # noqa: BLE001 - diagnostic must report, not crash
        return ("Model configuration", False, f"{model}; {exc}")

    missing = result.get("missing_keys") or []
    ok = bool(result.get("keys_in_environment")) and not missing
    endpoint = f"; endpoint={api_base}" if api_base else ""
    detail = f"{model}{endpoint}"
    if missing:
        detail += "; missing " + ", ".join(str(key) for key in missing)
    return ("Model configuration", ok, detail)


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
                "SCIP or clangd",
                any(
                    shutil.which(tool)
                    for tool in ("scip-python", "scip-go", "rust-analyzer", "clangd")
                ),
                next(
                    (
                        tool
                        for tool in (
                            "scip-python",
                            "scip-go",
                            "rust-analyzer",
                            "clangd",
                        )
                        if shutil.which(tool)
                    ),
                    "missing",
                ),
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
        model_check = _doctor_model_config(args)
        if model_check is not None:
            rows["agent"].append(model_check)
    return rows


def _probe_doctor_model(args: argparse.Namespace) -> tuple[bool, str]:
    model = (
        args.model
        or os.environ.get("CODENIB_DEMO_WIKI_MODEL")
        or os.environ.get("CODENIB_DEMO_MODEL")
    )
    if not model:
        return False, "set --model or CODENIB_DEMO_MODEL"
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
    try:
        from .llm.litellm_chat import LiteLLMChat, RetryConfig

        response = LiteLLMChat(
            model=model,
            temperature=0.0,
            max_tokens=8,
            api_base=api_base,
            api_key=api_key,
            retry=RetryConfig(max_retries=0),
        ).complete(
            [{"role": "user", "content": "Reply with OK."}],
            timeout=20,
        )
    except Exception as exc:  # noqa: BLE001 - diagnostic result
        return False, str(exc)
    return bool(response), "response received" if response else "empty response"


def _run_doctor(args: argparse.Namespace) -> int:
    required = set(args.require or ["core"])
    rows = _doctor_rows(args)
    if args.probe_model:
        rows["agent"].append(("Model probe", *_probe_doctor_model(args)))
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
        "--probe-model",
        action="store_true",
        help="send one minimal model request after validating configuration",
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
