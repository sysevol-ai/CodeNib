#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""
Integration tests for multilingual SCIP indexing via unified run_pipeline().
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from codeminer.dataset.swebench_multilingual import SwebenchMultilingualDataset
from codeminer.ls_router import LSIndexer

pytestmark = pytest.mark.integration_serial


_REPO_KEYWORDS = {
    "cpp": ["fmtlib/", "nlohmann/", "jqlang/", "redis/", "valkey-io/", "micropython/"],
    "rust": ["astral-sh/ruff", "pola-rs/polars", "tokio-rs/", "rust-lang/"],
    "ts": [
        "axios/",
        "vuejs/",
        "mui/",
        "darkreader/",
        "sveltejs/",
        "expressjs/",
        "insomnia/",
        "dayjs/",
    ],
    "go": ["caddyserver/", "gin-gonic/", "gohugoio/", "hashicorp/", "prometheus/"],
}


def _pick_swebench_multilingual_instance(language: str) -> dict:
    dataset_obj = SwebenchMultilingualDataset(split="test", filter_instance=".*")
    rows = dataset_obj.load()
    for row in rows:
        if any(k in row["repo"] for k in _REPO_KEYWORDS[language]):
            return dict(row)
    raise RuntimeError(f"No SWE-bench_Multilingual instance for {language}")


def _ensure_cpp_compdb(repo: Path) -> None:
    compdb = repo / "build" / "compile_commands.json"
    if compdb.exists():
        return
    subprocess.run(
        [
            "cmake",
            "-S",
            str(repo),
            "-B",
            str(repo / "build"),
            "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def _has_node(ig, expected_unified_name, node_type=None):
    """Check if any node has exactly this unified_name (strict match).

    File/directory nodes are matched by ``name`` since they don't carry
    ``unified_name``.  All other nodes are matched by ``unified_name``.
    """
    for v in ig.vs:
        if node_type in ("file", "directory"):
            matched = v["name"] == expected_unified_name
        else:
            matched = (
                v.attributes().get("unified_name") or ""
            ) == expected_unified_name
        if matched:
            if node_type is None or v["type"] == node_type:
                return True
    return False


def _vertex_matches(v, expected):
    """Match a vertex by unified_name, falling back to name for file/dir nodes."""
    un = v.attributes().get("unified_name") or ""
    return un == expected or v["name"] == expected


def _has_edge(ig, src_expected, tgt_expected, edge_type=None):
    """Check if an edge exists between nodes matching by unified_name or name."""
    for e in ig.es:
        if _vertex_matches(ig.vs[e.source], src_expected) and _vertex_matches(
            ig.vs[e.target], tgt_expected
        ):
            if edge_type is None or e["type"] == edge_type:
                return True
    return False


# Expected nodes / edges for the first SWE-bench instance of each language.
# All symbol nodes use unified_name format: file_path:SymbolDisplay
# File nodes match by name (the file path itself).
_EXPECTED = {
    # fmtlib/fmt (fmtlib__fmt-1683) — clangd indexer
    "cpp": {
        "nodes": [
            ("include/fmt/core.h", "file"),
            ("include/fmt/core.h:fmt.print()", "function"),
            ("include/fmt/format.h:fmt.to_string()", "function"),
            ("include/fmt/core.h:fmt.basic_string_view", "class"),
            ("include/fmt/os.h:fmt.buffered_file", "class"),
            ("include/fmt/os.h:fmt.file", "class"),
            ("include/fmt/os.h:fmt.buffered_file.get()", "method"),
            ("src/os.cc:fmt.file.write()", "method"),
        ],
        "edges": [
            (
                "include/fmt/core.h",
                "include/fmt/core.h:fmt.to_string_view()",
                "contain",
            ),
            (
                "include/fmt/core.h:fmt.detail.make_arg()",
                "include/fmt/core.h:fmt.basic_format_arg",
                "reference",
            ),
        ],
    },
    # astral-sh/ruff (astral-sh__ruff-15309)
    "rust": {
        "nodes": [
            ("crates/ruff_linter/src/linter.rs", "file"),
            ("crates/ruff_linter/src/linter.rs:check_path()", "function"),
            ("crates/ruff_linter/src/directives.rs:extract_directives()", "function"),
            (
                "crates/ruff_python_resolver/src/resolver.rs:resolve_import()",
                "function",
            ),
            ("crates/ruff_linter/src/settings/types.rs:PythonVersion", "class"),
            ("crates/ruff_linter/src/line_width.rs:IndentWidth", "class"),
            ("crates/ruff_linter/src/source_kind.rs:SourceKind", "class"),
            (
                "crates/ruff_linter/src/source_kind.rs:SourceKind.source_code()",
                "method",
            ),
        ],
        "edges": [
            (
                "crates/ruff_linter/src/linter.rs",
                "crates/ruff_linter/src/linter.rs:check_path()",
                "contain",
            ),
            (
                "crates/ruff_linter/src/linter.rs:check_path()",
                "crates/ruff_linter/src/settings/mod.rs:LinterSettings",
                "reference",
            ),
        ],
    },
    # axios/axios (axios__axios-4731)
    "ts": {
        "nodes": [
            ("index.d.ts", "file"),
            ("index.d.ts:Axios.request()", "method"),
            ("index.d.ts:Axios.get()", "method"),
            ("index.d.ts:Axios.post()", "method"),
            ("index.d.ts:AxiosError", "class"),
            ("index.d.ts:AxiosHeaders", "class"),
            ("index.d.ts:AxiosRequestConfig", "class"),
            ("index.d.ts:AxiosResponse", "class"),
            ("index.d.ts:CancelToken", "class"),
        ],
        "edges": [
            ("index.d.ts:Axios", "index.d.ts:Axios.request()", "contain"),
            ("test/typescript/axios.ts", "index.d.ts:Axios.get()", "reference"),
        ],
    },
    # caddyserver/caddy (caddyserver__caddy-4774) — scip-go indexer
    # Predicted from decoder source (scip_decode_go.py) + caddy source:
    #   internal_module = "github.com/caddyserver/caddy/v2"
    #   _make_symbol_key: strips backtick module prefix → rel_pkg="" for root pkg
    #   _classify_symbol_type: # → class, ().  → function/method, . after # → field
    #   _get_unified_name: file_path:Display (# replaced with ., () for func/method)
    "go": {
        "nodes": [
            ("caddy.go", "file"),
            # type Config struct — descriptor `…/caddy/v2`/Config# → class
            ("caddy.go:Config", "class"),
            # func Run(cfg *Config) — descriptor …/Run(). → function
            ("caddy.go:Run()", "function"),
            # func Load(…) — descriptor …/Load(). → function
            ("caddy.go:Load()", "function"),
            # func Stop() — descriptor …/Stop(). → function
            ("caddy.go:Stop()", "function"),
            # func Validate(cfg *Config) — function
            ("caddy.go:Validate()", "function"),
            # type App interface — descriptor …/App# → class
            ("caddy.go:App", "class"),
            ("modules.go", "file"),
            # type Module interface — descriptor …/Module# → class
            ("modules.go:Module", "class"),
            # type ModuleInfo struct — class
            ("modules.go:ModuleInfo", "class"),
            # type ModuleID string — type alias, descriptor …/ModuleID# → class
            ("modules.go:ModuleID", "class"),
            # func RegisterModule(instance Module) — function
            ("modules.go:RegisterModule()", "function"),
            # func GetModule(name string) — function
            ("modules.go:GetModule()", "function"),
            ("context.go", "file"),
            # type Context struct — class
            ("context.go:Context", "class"),
            # func (ctx Context) LoadModule(…) — has # and (). → method
            ("context.go:Context.LoadModule()", "method"),
        ],
        "edges": [
            # File → symbol containment (definition in that file)
            ("caddy.go", "caddy.go:Config", "contain"),
            ("caddy.go", "caddy.go:Run()", "contain"),
            ("modules.go", "modules.go:RegisterModule()", "contain"),
            # Cross-file reference: caddy.go calls NewContext(Context{…})
            ("caddy.go", "context.go:Context", "reference"),
        ],
    },
}


def _tools_ready(language: str) -> bool:
    if language == "cpp":
        return bool(shutil.which("clangd")) and bool(shutil.which("cmake"))
    if language == "rust":
        return bool(shutil.which("rust-analyzer"))
    if language == "ts":
        return bool(shutil.which("scip-typescript"))
    if language == "go":
        return bool(shutil.which("scip-go"))
    return False


@pytest.mark.parametrize("language", ["cpp", "rust", "ts", "go"])
def test_run_pipeline_swebench_multilingual_instance(language: str) -> None:
    """Serial-backend regression test on a pinned SWE-bench_Multilingual
    instance. Writes outputs to ``~/.codeminer/<instance_id>/`` so that
    ``test_scip_core.py`` picks up the same ``index.decoded`` on the next run
    (cache shared across CI jobs on self-hosted runner).
    """
    if not _tools_ready(language):
        pytest.skip(f"Required tooling for {language} is not available in PATH")

    try:
        instance = _pick_swebench_multilingual_instance(language)
    except Exception as exc:
        pytest.skip(
            f"SWE-bench_Multilingual unavailable or no matching instance: {exc}"
        )

    dataset_obj = SwebenchMultilingualDataset(split="test", filter_instance=".*")
    dataset_obj.process_instance(instance)
    repo_path = Path(dataset_obj.get_repo_path(instance))
    if language == "cpp":
        _ensure_cpp_compdb(repo_path)

    output_dir = Path.home() / ".codeminer" / instance["instance_id"]
    output_dir.mkdir(parents=True, exist_ok=True)

    kwargs = {"infer_tsconfig": True} if language == "ts" else {}
    indexer = LSIndexer(
        project_root=repo_path,
        output_dir=output_dir,
        language=language,
        decoder_backend="serial",
    )
    # skip_level="decode" (not "graph"): reuse cached index.scip / index.decoded
    # but rebuild the graph each run. Using "graph" loads a stale graph.pkl
    # from a prior decoder version — the scip-core parity job then compares
    # new core output against that stale cache and fails.
    graph = indexer.run_pipeline(skip_level="decode", report_profile=False, **kwargs)

    assert graph is not None, f"run_pipeline returned None for {language} (dataset)"
    ig = graph.graph
    assert len(ig.vs) > 0, f"no graph nodes for {language} (dataset)"

    expected = _EXPECTED.get(language)
    if expected:
        for name, node_type in expected["nodes"]:
            assert _has_node(
                ig, name, node_type
            ), f"[{language}] missing node: {name!r} (type={node_type})"
        for src, tgt, edge_type in expected["edges"]:
            assert _has_edge(
                ig, src, tgt, edge_type
            ), f"[{language}] missing edge: {src!r} -> {tgt!r} ({edge_type})"
