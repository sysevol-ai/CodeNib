# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Integration tests for SCIP cold-start → incremental graph patch pipeline.

Part 1: simple_repos — controlled environment.
Part 2: SWE-bench repos — real commit ranges with detect_changed_files.

Flow per test:
    1. SCIP cold-start (LSIndexer.run_pipeline) builds the initial CodeGraph
    2. Apply code changes (manual edit or real commit range)
    3. GraphPatcher incrementally updates the graph
    4. Assert vertex additions / removals via unified_name

Requirements:
    - SCIP toolchain: rust-analyzer, scip-typescript, scip-python, scip-go, clangd
    - LSP servers: rust-analyzer, typescript-language-server, pyright-langserver, gopls, clangd
    - Network access for cloning SWE-bench repos (first run only)

Usage:
    pytest test/graph/incremental/test_graph_patcher.py -v
    pytest test/graph/incremental/test_graph_patcher.py -k simple -v
    pytest test/graph/incremental/test_graph_patcher.py -k swebench -v
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from codeminer.graph.code_graph import CodeGraph
from codeminer.graph.incremental.graph_patcher import GraphPatcher
from codeminer.graph.incremental.lsp_client import LSPClient
from codeminer.ls_router import LSIndexer

pytestmark = pytest.mark.integration_serial


def test_ls_indexer_graph_patch_import():
    """Smoke test: LSIndexer.graph_patch lazy imports resolve correctly."""
    assert hasattr(LSIndexer, "graph_patch")
    from codeminer.graph.incremental.graph_patcher import LANGUAGE_EXTENSIONS

    assert isinstance(LANGUAGE_EXTENSIONS, dict)


SIMPLE_REPOS = Path(__file__).resolve().parent.parent / "scip" / "simple_repos"
REPO_CACHE_DIRS = [
    Path.home() / ".codeminer" / "repos",
]

# Ensure LSP/SCIP tool directories are on PATH
_EXTRA_PATHS = [
    str(Path.home() / ".npm-global" / "bin"),
    str(Path.home() / ".cargo" / "bin"),
    str(Path.home() / "go" / "bin"),
]
for _p in _EXTRA_PATHS:
    if _p not in os.environ.get("PATH", ""):
        os.environ["PATH"] = _p + ":" + os.environ.get("PATH", "")

# SCIP tool name per language (for availability checks)
_SCIP_TOOLS = {
    "rust": "rust-analyzer",
    "ts": "scip-typescript",
    "python": "scip-python",
    "go": "scip-go",
    "cpp": "clangd",
}

# ── Helpers ──────────────────────────────────────────────────────────


def _skip_if_no_scip(language: str):
    """Skip if SCIP indexer for this language is not available."""
    tool = _SCIP_TOOLS.get(language)
    if tool and not shutil.which(tool):
        pytest.skip(f"SCIP tool {tool!r} not installed")


def _skip_if_no_lsp(language: str):
    if not LSPClient.check_lsp_available(language):
        pytest.skip(f"LSP server for {language} not installed")


def _git(*args, cwd):
    return subprocess.check_output(
        ["git"] + list(args),
        cwd=str(cwd),
        text=True,
        stderr=subprocess.DEVNULL,
    ).strip()


def _init_git(repo_dir: Path):
    """Initialize a git repo and commit all files."""
    _git("init", cwd=repo_dir)
    _git("add", ".", cwd=repo_dir)
    _git("commit", "-m", "initial", cwd=repo_dir)


def _build_graph_with_scip(
    project_root: Path,
    language: str,
    output_dir: Path,
    **kwargs,
) -> CodeGraph:
    """Build a CodeGraph via SCIP cold-start (LSIndexer.run_pipeline).

    Uses skip_level="graph" to reuse cached graph.pkl if available,
    falling back to full pipeline if not.
    """
    indexer = LSIndexer(
        project_root=project_root,
        output_dir=output_dir,
        language=language,
    )
    # Try loading cached graph first
    graph = indexer.run_pipeline(skip_level="graph", report_profile=False, **kwargs)
    if graph is None:
        # No cache, do full pipeline
        graph = indexer.run_pipeline(skip_level=None, report_profile=False, **kwargs)
    assert graph is not None, f"SCIP run_pipeline returned None for {language}"
    return graph


def _get_unified_names(g: CodeGraph) -> set[str]:
    """Collect all unified_name values from the graph (skipping None/empty)."""
    result = set()
    for v in g.graph.vs:
        un = v.attributes().get("unified_name")
        if un:
            result.add(un)
    return result


def _get_vertex_label(v) -> str:
    """Get the best identifier for a vertex: unified_name if set, else name."""
    return v.attributes().get("unified_name") or v["name"]


def _has_ref_edge(g: CodeGraph, src_label: str, tgt_label: str) -> bool:
    """Check if a reference edge exists between vertices identified by label.

    Label is matched against unified_name first, then name (for file vertices).
    """
    from codeminer.types import EDGE_TYPE_REFERENCE

    for e in g.graph.es:
        if e["type"] != EDGE_TYPE_REFERENCE:
            continue
        src_l = _get_vertex_label(g.graph.vs[e.source])
        tgt_l = _get_vertex_label(g.graph.vs[e.target])
        if src_l == src_label and tgt_l == tgt_label:
            return True
    return False


def _count_ref_edges_involving(g: CodeGraph, label: str) -> int:
    """Count reference edges where either end matches label exactly."""
    from codeminer.types import EDGE_TYPE_REFERENCE

    count = 0
    for e in g.graph.es:
        if e["type"] != EDGE_TYPE_REFERENCE:
            continue
        src_l = _get_vertex_label(g.graph.vs[e.source])
        tgt_l = _get_vertex_label(g.graph.vs[e.target])
        if src_l == label or tgt_l == label:
            count += 1
    return count


def _ensure_swebench_repo(repo_name: str, base_commit: str) -> Path:
    """Find or clone a repo and checkout base_commit."""
    dirname = repo_name.replace("/", "_")

    # Search existing cache directories first
    for cache_dir in REPO_CACHE_DIRS:
        repo_dir = cache_dir / dirname
        if repo_dir.exists():
            break
    else:
        # Clone to first cache dir
        cache_dir = REPO_CACHE_DIRS[0]
        cache_dir.mkdir(parents=True, exist_ok=True)
        repo_dir = cache_dir / dirname
        subprocess.run(
            [
                "git",
                "clone",
                "--quiet",
                f"https://github.com/{repo_name}.git",
                str(repo_dir),
            ],
            check=True,
            capture_output=True,
        )
    # Checkout base commit (use short hash to avoid issues with partial clones)
    short = base_commit[:12]
    try:
        _git("checkout", "-f", short, cwd=repo_dir)
    except subprocess.CalledProcessError:
        _git("fetch", "--all", cwd=repo_dir)
        _git("checkout", "-f", short, cwd=repo_dir)
    _git("clean", "-fd", cwd=repo_dir)
    return repo_dir


# ═════════════════════════════════════════════════════════════════════
# Part 1: Simple Repos Integration Tests
# ═════════════════════════════════════════════════════════════════════


class TestSimpleRust:
    """Comprehensive graph-patch test for Rust.

    One SCIP build, multiple sequential patch rounds covering:
    add / remove / modify-body / same-file-edge / cross-file / line-shift.
    """

    @pytest.fixture
    def rust_repo(self, tmp_path):
        _skip_if_no_scip("rust")
        _skip_if_no_lsp("rust")
        src = SIMPLE_REPOS / "rust_simple"
        if not src.exists():
            pytest.skip("rust_simple not found")
        dst = tmp_path / "rust_simple"
        shutil.copytree(src, dst)
        _init_git(dst)
        return dst

    def test_graph_patch(self, rust_repo, tmp_path):
        """One commit covers: add + modify + remove + shift + cross-file."""
        g = _build_graph_with_scip(rust_repo, "rust", tmp_path / "scip_out")

        # ── Cold-start verification ──
        unames = _get_unified_names(g)
        assert "src/math_utils.rs:add()" in unames
        assert "src/math_utils.rs:is_prime()" in unames
        assert "src/shapes.rs:Rectangle" in unames
        assert _has_ref_edge(
            g, "src/main.rs:test_math_utils()", "src/math_utils.rs:add()"
        )
        assert _has_ref_edge(
            g, "src/main.rs:test_math_utils()", "src/math_utils.rs:is_prime()"
        )
        assert _has_ref_edge(g, "src/main.rs:test_shapes()", "src/shapes.rs:Rectangle")

        base = _git("rev-parse", "HEAD", cwd=rust_repo)
        math_file = rust_repo / "src" / "math_utils.rs"
        main_file = rust_repo / "src" / "main.rs"

        # ── Single commit: add subtract, modify add body, remove is_prime,
        #    shift lines (comments at top), cross-file call ──

        # 1. Remove is_prime (brace counting)
        lines = math_file.read_text().splitlines(keepends=True)
        new_lines, skip, depth = [], False, 0
        for line in lines:
            if not skip and "pub fn is_prime" in line:
                skip, depth = True, 0
            if skip:
                depth += line.count("{") - line.count("}")
                if depth <= 0 and "}" in line:
                    skip = False
                continue
            new_lines.append(line)
        content = "".join(new_lines)

        # 2. Modify add() body
        content = content.replace("a + b", "let r = a + b;\n    r")

        # 3. Add comments at top (shifts all lines)
        content = "// Shift comment 1\n// Shift comment 2\n" + content

        # 4. Add subtract()
        content += "\n\npub fn subtract(a: i32, b: i32) -> i32 {\n    a - b\n}\n"
        math_file.write_text(content)

        # 5. Cross-file call from main.rs
        main_content = main_file.read_text()
        main_content = main_content.replace(
            "test_math_utils();",
            "test_math_utils();\n    let _ = math_utils::subtract(10, 3);",
        )
        main_file.write_text(main_content)

        _git("add", ".", cwd=rust_repo)
        _git("commit", "-m", "add+modify+remove+shift", cwd=rust_repo)

        patcher = GraphPatcher(str(rust_repo), g, "rust")
        changed = GraphPatcher.detect_changed_files(
            str(rust_repo),
            base,
            "HEAD",
            extensions={".rs"},
        )
        patcher.patch_files(changed, earlier_commit=base, later_commit="HEAD")

        unames = _get_unified_names(g)

        # add: new vertex
        assert "src/math_utils.rs:subtract()" in unames, "add: subtract vertex"
        # modify: vertex survives
        assert "src/math_utils.rs:add()" in unames, "modify: add survives"
        edges_add = _count_ref_edges_involving(g, "src/math_utils.rs:add()")
        assert edges_add > 0, "modify: add() edges survive"
        # remove: vertex gone
        assert "src/math_utils.rs:is_prime()" not in unames, "remove: is_prime gone"
        assert (
            _count_ref_edges_involving(g, "src/math_utils.rs:is_prime()") == 0
        ), "remove: is_prime edges gone"
        # shift: multiply survives with edge
        assert "src/math_utils.rs:multiply()" in unames, "shift: multiply survives"
        assert _has_ref_edge(
            g, "src/main.rs:test_math_utils()", "src/math_utils.rs:multiply()"
        ), "shift: multiply edge survives"
        # cross-file: main→subtract
        assert _has_ref_edge(
            g, "src/main.rs:main()", "src/math_utils.rs:subtract()"
        ), "cross-file: main→subtract"
        # shapes edges unchanged
        assert _has_ref_edge(
            g, "src/main.rs:test_shapes()", "src/shapes.rs:Rectangle"
        ), "unchanged: shapes edge survives"


class TestSimpleTypeScript:
    """Comprehensive graph-patch test for TypeScript."""

    @pytest.fixture
    def ts_repo(self, tmp_path):
        _skip_if_no_scip("ts")
        _skip_if_no_lsp("typescript")
        src = SIMPLE_REPOS / "typescript_simple"
        if not src.exists():
            pytest.skip("typescript_simple not found")
        dst = tmp_path / "typescript_simple"
        shutil.copytree(src, dst)
        _init_git(dst)
        return dst

    def test_graph_patch(self, ts_repo, tmp_path):
        """One commit: add + remove + modify + cross-file."""
        g = _build_graph_with_scip(
            ts_repo,
            "ts",
            tmp_path / "scip_out",
            infer_tsconfig=True,
        )

        unames = _get_unified_names(g)
        assert "src/math.ts:add()" in unames
        assert "src/math.ts:subtract()" in unames
        assert _has_ref_edge(g, "src/main.ts:wrapper()", "src/math.ts:add()")

        base = _git("rev-parse", "HEAD", cwd=ts_repo)
        math_file = ts_repo / "src" / "math.ts"
        main_file = ts_repo / "src" / "main.ts"

        # 1. Remove subtract (brace counting)
        lines = math_file.read_text().splitlines(keepends=True)
        new_lines, skip, depth = [], False, 0
        for line in lines:
            if not skip and "export function subtract" in line:
                skip, depth = True, 0
            if skip:
                depth += line.count("{") - line.count("}")
                if depth <= 0 and "}" in line:
                    skip = False
                continue
            new_lines.append(line)
        content = "".join(new_lines)

        # 2. Modify add body
        content = content.replace("return a + b;", "const r = a + b;\n    return r;")

        # 3. Add divide
        content += (
            "\nexport function divide("
            "a: number, b: number): number {\n"
            "    return a / b;\n}\n"
        )
        math_file.write_text(content)

        # 4. Cross-file call
        main_content = main_file.read_text()
        main_content = main_content.replace(
            "import { add, multiply, subtract, Calculator } from './math';",
            "import { add, multiply, divide, Calculator } from './math';",
        )
        main_content += "\nconst q = divide(100, 4);\nconsole.log(q);\n"
        main_file.write_text(main_content)

        _git("add", ".", cwd=ts_repo)
        _git("commit", "-m", "add+modify+remove", cwd=ts_repo)

        patcher = GraphPatcher(str(ts_repo), g, "ts")
        changed = GraphPatcher.detect_changed_files(
            str(ts_repo),
            base,
            "HEAD",
            extensions={".ts", ".tsx", ".js", ".jsx"},
        )
        patcher.patch_files(changed, earlier_commit=base, later_commit="HEAD")

        unames = _get_unified_names(g)
        assert "src/math.ts:divide()" in unames, "add: divide vertex"
        assert "src/math.ts:subtract()" not in unames, "remove: subtract gone"
        assert "src/math.ts:add()" in unames, "modify: add survives"
        assert _has_ref_edge(
            g, "src/main.ts:wrapper()", "src/math.ts:add()"
        ), "modify: edge survives"
        assert _has_ref_edge(
            g, "src/main.ts", "src/math.ts:divide()"
        ), "cross-file: main→divide"


class TestSimpleCpp:
    """Comprehensive graph-patch test for C++."""

    @pytest.fixture
    def cpp_repo(self, tmp_path):
        _skip_if_no_scip("cpp")
        _skip_if_no_lsp("cpp")
        src = SIMPLE_REPOS / "cpp_simple"
        if not src.exists():
            pytest.skip("cpp_simple not found")
        dst = tmp_path / "cpp_simple"
        shutil.copytree(src, dst)
        subprocess.run(
            [
                "cmake",
                "-S",
                str(dst),
                "-B",
                str(dst / "build"),
                "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
            ],
            check=True,
            capture_output=True,
        )
        compdb = dst / "build" / "compile_commands.json"
        link = dst / "compile_commands.json"
        if compdb.exists() and not link.exists():
            os.symlink(str(compdb), str(link))
        _init_git(dst)
        return dst

    def test_graph_patch(self, cpp_repo, tmp_path):
        """One commit: add + remove + modify + cross-file."""
        g = _build_graph_with_scip(cpp_repo, "cpp", tmp_path / "scip_out")

        unames = _get_unified_names(g)
        assert "src/math_utils.cpp:MathUtils.add()" in unames
        assert "src/math_utils.cpp:MathUtils.isPrime()" in unames
        assert _has_ref_edge(
            g, "src/main.cpp:testMathUtils()", "src/math_utils.cpp:MathUtils.add()"
        )
        assert _has_ref_edge(
            g, "src/main.cpp:testShapes()", "include/shape.h:Rectangle"
        )

        base = _git("rev-parse", "HEAD", cwd=cpp_repo)
        math_file = cpp_repo / "src" / "math_utils.cpp"
        header = cpp_repo / "include" / "math_utils.h"
        main_file = cpp_repo / "src" / "main.cpp"

        # 1. Remove isPrime (brace counting)
        lines = math_file.read_text().splitlines(keepends=True)
        new_lines, skip, depth = [], False, 0
        for line in lines:
            if not skip and "bool isPrime" in line:
                skip, depth = True, 0
            if skip:
                depth += line.count("{") - line.count("}")
                if depth <= 0 and "{" not in line and "}" in line:
                    skip = False
                    continue
                continue
            new_lines.append(line)
        content = "".join(new_lines)

        # 2. Modify add body
        content = content.replace("return a + b;", "int r = a + b;\n    return r;")

        # 3. Add subtract
        content += "\nint MathUtils::subtract(int a, int b) {\n    return a - b;\n}\n"
        math_file.write_text(content)

        # 4. Header declaration
        hcontent = header.read_text()
        hcontent = hcontent.replace(
            "} // namespace MathUtils",
            "int subtract(int a, int b);\n\n} // namespace MathUtils",
        )
        header.write_text(hcontent)

        # 5. Cross-file call
        main_content = main_file.read_text()
        main_content = main_content.replace(
            "testMathUtils();",
            "testMathUtils();\n    int d = MathUtils::subtract(10, 3);\n"
            "    cout << d << endl;",
        )
        main_file.write_text(main_content)

        _git("add", ".", cwd=cpp_repo)
        _git("commit", "-m", "add+modify+remove", cwd=cpp_repo)

        patcher = GraphPatcher(str(cpp_repo), g, "cpp")
        changed = GraphPatcher.detect_changed_files(
            str(cpp_repo),
            base,
            "HEAD",
            extensions={".cpp", ".cc", ".c", ".h", ".hpp"},
        )
        stats = patcher.patch_files(changed, earlier_commit=base, later_commit="HEAD")
        assert stats["vertices_created"] > 0

        unames = _get_unified_names(g)
        assert (
            "src/math_utils.cpp:MathUtils.subtract()" in unames
        ), "add: subtract vertex"
        assert (
            "src/math_utils.cpp:MathUtils.isPrime()" not in unames
        ), "remove: isPrime gone"
        assert "src/math_utils.cpp:MathUtils.add()" in unames, "modify: add survives"
        assert _has_ref_edge(
            g, "src/main.cpp:main()", "src/math_utils.cpp:MathUtils.subtract()"
        ), "cross-file: main→subtract"
        assert _has_ref_edge(
            g, "src/main.cpp:testShapes()", "include/shape.h:Rectangle"
        ), "unchanged: shapes survives"


class TestSimpleGo:
    """Comprehensive graph-patch test for Go."""

    @pytest.fixture
    def go_repo(self, tmp_path):
        _skip_if_no_scip("go")
        _skip_if_no_lsp("go")
        src = SIMPLE_REPOS / "go_simple"
        if not src.exists():
            pytest.skip("go_simple not found")
        dst = tmp_path / "go_simple"
        shutil.copytree(src, dst)
        _init_git(dst)
        return dst

    def test_graph_patch(self, go_repo, tmp_path):
        """One commit: add + remove + modify + cross-file."""
        g = _build_graph_with_scip(go_repo, "go", tmp_path / "scip_out")

        unames = _get_unified_names(g)
        assert "calculator.go:Calculator" in unames
        assert "calculator.go:Calculator.Add()" in unames
        assert "calculator.go:Calculator.LastResult()" in unames
        assert "greet.go:Greet()" in unames
        assert "main.go:main()" in unames

        base = _git("rev-parse", "HEAD", cwd=go_repo)
        calc_file = go_repo / "calculator.go"
        main_file = go_repo / "main.go"

        # 1. Remove LastResult (brace counting)
        lines = calc_file.read_text().splitlines(keepends=True)
        new_lines, skip, depth = [], False, 0
        for line in lines:
            if not skip and "LastResult()" in line and "func" in line:
                skip, depth = True, 0
                while new_lines and new_lines[-1].strip().startswith("//"):
                    new_lines.pop()
            if skip:
                depth += line.count("{") - line.count("}")
                if depth <= 0 and "}" in line:
                    skip = False
                continue
            new_lines.append(line)
        content = "".join(new_lines)

        # 2. Modify Add body
        content = content.replace(
            "result := a + b",
            'result := a + b\n\tfmt.Println("Adding:", a, b)',
        )
        if '"fmt"' not in content:
            content = content.replace("package main", 'package main\n\nimport "fmt"')

        # 3. Add Multiply
        content += (
            "\n// Multiply multiplies two integers.\n"
            "func (c *Calculator) Multiply(a, b int) int {\n"
            "    result := a * b\n"
            "    c.History = append(c.History, result)\n"
            "    return result\n"
            "}\n"
        )
        calc_file.write_text(content)

        # 4. Cross-file call
        main_content = main_file.read_text()
        main_content = main_content.replace(
            'fmt.Println("Result:", result)',
            'fmt.Println("Result:", result)\n'
            "\tprod := calc.Multiply(3, 7)\n"
            '\tfmt.Println("Product:", prod)',
        )
        main_file.write_text(main_content)

        _git("add", ".", cwd=go_repo)
        _git("commit", "-m", "add+modify+remove", cwd=go_repo)

        patcher = GraphPatcher(str(go_repo), g, "go")
        changed = GraphPatcher.detect_changed_files(
            str(go_repo),
            base,
            "HEAD",
            extensions={".go"},
        )
        patcher.patch_files(changed, earlier_commit=base, later_commit="HEAD")

        unames = _get_unified_names(g)
        assert "calculator.go:Calculator.Multiply()" in unames, "add: Multiply vertex"
        assert (
            "calculator.go:Calculator.LastResult()" not in unames
        ), "remove: LastResult gone"
        assert "calculator.go:Calculator.Add()" in unames, "modify: Add survives"
        assert "greet.go:Greet()" in unames, "unchanged: Greet survives"


# ═════════════════════════════════════════════════════════════════════
# Part 2: SWE-bench Real-Commit Integration Tests
# Uses actual commit ranges (no manual patches / HuggingFace).
# Flow: checkout earlier~N → SCIP cold-start → checkout later
#       → detect_changed_files → LSP graph-patch → assert symbols.
# ═════════════════════════════════════════════════════════════════════


def _get_earlier_commit(repo_dir: Path, later_commit: str, n_back: int) -> str:
    """Get a commit N steps before later_commit."""
    # Use short hash to avoid git issues with partial/blobless clones
    short = later_commit[:12]
    full = _git("rev-parse", f"{short}~{n_back}", cwd=repo_dir)
    return full[:12]


class TestSWEBenchCpp:
    """fmtlib/fmt: instance fmt-2317 (from codeminer-base-dataset)."""

    REPO = "fmtlib/fmt"
    LATER = "ece4b4b33a96928e3d92f4965f6deeb8e3a6e6b0"
    N_BACK = 5

    @pytest.fixture(scope="class")
    def repo_dir(self):
        _skip_if_no_scip("cpp")
        _skip_if_no_lsp("cpp")
        if not shutil.which("cmake"):
            pytest.skip("cmake not installed")
        try:
            repo = _ensure_swebench_repo(self.REPO, self.LATER)
        except Exception as e:
            pytest.skip(f"Cannot clone repo: {e}")
            return None  # unreachable; satisfies linter
        subprocess.run(
            [
                "cmake",
                "-S",
                str(repo),
                "-B",
                str(repo / "build"),
                "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
            ],
            capture_output=True,
        )
        return repo

    def test_graph_patch(self, repo_dir, tmp_path):
        earlier = _get_earlier_commit(repo_dir, self.LATER, self.N_BACK)

        _git("checkout", "-f", earlier, cwd=repo_dir)
        g = _build_graph_with_scip(
            repo_dir,
            "cpp",
            Path.home() / ".codeminer" / "scip_cache" / self.REPO.replace("/", "_"),
        )

        _git("checkout", "-f", self.LATER[:12], cwd=repo_dir)
        changed = GraphPatcher.detect_changed_files(
            str(repo_dir),
            earlier,
            self.LATER,
            extensions={".cpp", ".cc", ".cxx", ".c", ".h", ".hpp", ".hxx"},
        )
        assert len(changed["modified"]) > 0

        patcher = GraphPatcher(str(repo_dir), g, "cpp")
        stats = patcher.patch_files(
            changed, earlier_commit=earlier, later_commit=self.LATER[:12]
        )
        assert stats["files_modified"] >= 1
        assert stats["vertices_created"] > 0

        unames_after = _get_unified_names(g)
        # Graph should still have reasonable size
        assert g.graph.vcount() > 0
        assert g.graph.ecount() > 0

        # ── Strict vertex assertions (5+) ──
        assert "include/fmt/core.h:fmt.basic_format_specs" in unames_after
        assert "include/fmt/core.h:fmt.detail.specs_setter" in unames_after
        assert "include/fmt/core.h:fmt.detail.specs_setter.on_zero()" in unames_after
        assert "include/fmt/format.h:fmt.detail.write_nonfinite()" in unames_after
        assert "include/fmt/format.h:fmt.detail.write_padded()" in unames_after
        assert "include/fmt/format.h:fmt.detail.vformat_to()" in unames_after

        # .idx incremental should have produced refs
        assert stats.get("refs_outgoing", 0) > 0, f"Expected C++ .idx refs, got {stats}"

        # ── Strict cross-file edge assertions (format.h → core.h) ──
        assert _has_ref_edge(
            g,
            "include/fmt/format.h:fmt.detail.for_each_codepoint()",
            "include/fmt/core.h:fmt.string_view",
        ), "for_each_codepoint() → string_view"
        assert _has_ref_edge(
            g,
            "include/fmt/format.h:fmt.detail.compute_width()",
            "include/fmt/core.h:fmt.string_view",
        ), "compute_width() → string_view"
        assert _has_ref_edge(
            g,
            "include/fmt/format.h:fmt.detail.write_bytes()",
            "include/fmt/core.h:fmt.string_view",
        ), "write_bytes() → string_view"
        assert _has_ref_edge(
            g,
            "include/fmt/format.h:fmt.vformat()",
            "include/fmt/core.h:fmt.string_view",
        ), "vformat() → string_view"
        assert _has_ref_edge(
            g,
            "include/fmt/format.h:fmt.system_error()",
            "include/fmt/core.h:fmt.string_view",
        ), "system_error() → string_view"


class TestSWEBenchGo:
    """caddyserver/caddy: instance caddy-5870 (from codeminer-base-dataset).
    N_BACK=5 to include reverseproxy changes for richer edge testing.
    """

    REPO = "caddyserver/caddy"
    LATER = "fae195ac7eb9cce1d81e43e5a0d34e9f3e4c7086"
    N_BACK = 5

    @pytest.fixture(scope="class")
    def repo_dir(self):
        _skip_if_no_scip("go")
        _skip_if_no_lsp("go")
        try:
            return _ensure_swebench_repo(self.REPO, self.LATER)
        except Exception as e:
            pytest.skip(f"Cannot clone repo: {e}")
            return None  # unreachable; satisfies linter

    def test_graph_patch(self, repo_dir, tmp_path):
        earlier = _get_earlier_commit(repo_dir, self.LATER, self.N_BACK)

        _git("checkout", "-f", earlier, cwd=repo_dir)

        # Start LSP early for warm-up (parallel with SCIP)
        patcher = GraphPatcher(str(repo_dir), None, "go")
        patcher.start_lsp()

        g = _build_graph_with_scip(
            repo_dir,
            "go",
            Path.home() / ".codeminer" / "scip_cache" / self.REPO.replace("/", "_"),
        )
        patcher.code_graph = g

        _git("checkout", "-f", self.LATER[:12], cwd=repo_dir)
        changed = GraphPatcher.detect_changed_files(
            str(repo_dir),
            earlier,
            self.LATER,
            extensions={".go"},
        )
        assert len(changed["modified"]) >= 1

        stats = patcher.patch_files(
            changed, earlier_commit=earlier, later_commit=self.LATER[:12]
        )
        assert stats["files_modified"] >= 1
        assert stats["vertices_created"] > 0

        unames_after = _get_unified_names(g)
        # Modified files' symbols should exist (SCIP-Go skips _test.go).
        for f in changed["modified"]:
            if f.endswith("_test.go"):
                continue
            file_syms = [u for u in unames_after if u.startswith(f + ":")]
            assert len(file_syms) > 0, f"Modified file {f} should have symbols"

        # ── Strict vertex assertions (5+) ──
        assert "modules/caddyhttp/reverseproxy/reverseproxy.go:Handler" in unames_after
        assert (
            "modules/caddyhttp/reverseproxy/reverseproxy.go:Handler.ServeHTTP()"
            in unames_after
        )
        assert (
            "modules/caddyhttp/reverseproxy/reverseproxy.go:Handler.Provision()"
            in unames_after
        )
        assert (
            "modules/caddyhttp/reverseproxy/reverseproxy.go:Handler.VerboseLogs"
            in unames_after
        ), "VerboseLogs field (added in this range) should exist"
        assert (
            "modules/caddyhttp/reverseproxy/selectionpolicies.go:LeastConnSelection"
            in unames_after
        )

        # Check that LSP discovered at least some reference edges
        total_refs = stats.get("refs_incoming", 0) + stats.get("refs_outgoing", 0)
        assert total_refs > 0, f"Expected reference edges for Go, got {total_refs}"

        # ── Strict cross-file edge assertions (10+) ──
        assert _has_ref_edge(
            g,
            "modules/caddytls/connpolicy.go:ConnectionPolicy.buildStandardTLSConfig()",
            "modules/caddytls/values.go:CipherSuiteID()",
        ), "connpolicy.go:buildStandardTLSConfig → values.go:CipherSuiteID()"
        assert _has_ref_edge(
            g,
            "modules/caddytls/connpolicy.go:ConnectionPolicy.buildStandardTLSConfig()",
            "modules/caddytls/values.go:SupportedCurves",
        ), "connpolicy.go:buildStandardTLSConfig → values.go:SupportedCurves"
        assert _has_ref_edge(
            g,
            "modules/caddytls/connpolicy.go:ConnectionPolicy.buildStandardTLSConfig()",
            "modules/caddytls/values.go:SupportedProtocols",
        ), "connpolicy.go:buildStandardTLSConfig → values.go:SupportedProtocols"
        assert _has_ref_edge(
            g,
            "modules/caddytls/connpolicy.go:setDefaultTLSParams()",
            "modules/caddytls/values.go:getOptimalDefaultCipherSuites()",
        ), "connpolicy.go:setDefaultTLSParams → values.go:getOptimalDefaultCipherSuites()"
        # builtins.go → values.go (unmodified file referencing values.go)
        assert _has_ref_edge(
            g,
            "caddyconfig/httpcaddyfile/builtins.go:parseTLS()",
            "modules/caddytls/values.go:SupportedProtocols",
        ), "builtins.go:parseTLS → values.go:SupportedProtocols"
        assert _has_ref_edge(
            g,
            "caddyconfig/httpcaddyfile/builtins.go:parseTLS()",
            "modules/caddytls/values.go:SupportedCurves",
        ), "builtins.go:parseTLS → values.go:SupportedCurves"
        assert _has_ref_edge(
            g,
            "caddyconfig/httpcaddyfile/builtins.go:parseTLS()",
            "modules/caddytls/values.go:CipherSuiteNameSupported()",
        ), "builtins.go:parseTLS → values.go:CipherSuiteNameSupported()"
        # automation.go → values.go
        assert _has_ref_edge(
            g,
            "modules/caddytls/automation.go:AutomationPolicy.Provision()",
            "modules/caddytls/values.go:supportedCertKeyTypes",
        ), "automation.go:Provision → values.go:supportedCertKeyTypes"
        # replacer.go → values.go
        assert _has_ref_edge(
            g,
            "modules/caddyhttp/replacer.go:getReqTLSReplacement()",
            "modules/caddytls/values.go:ProtocolName()",
        ), "replacer.go:getReqTLSReplacement → values.go:ProtocolName()"
        # fastcgi → values.go
        assert _has_ref_edge(
            g,
            "modules/caddyhttp/reverseproxy/fastcgi/fastcgi.go:Transport.buildEnv()",
            "modules/caddytls/values.go:SupportedCipherSuites()",
        ), "fastcgi.go:Transport.buildEnv → values.go:SupportedCipherSuites()"


class TestSWEBenchRust:
    """astral-sh/ruff: instance ruff-15309 (from codeminer-base-dataset)."""

    REPO = "astral-sh/ruff"
    LATER = "75a24bbc67aa31b825b6326cfb6e6afdf3ca90d5"
    N_BACK = 3

    @pytest.fixture(scope="class")
    def repo_dir(self):
        _skip_if_no_scip("rust")
        _skip_if_no_lsp("rust")
        try:
            return _ensure_swebench_repo(self.REPO, self.LATER)
        except Exception as e:
            pytest.skip(f"Cannot clone repo: {e}")
            return None  # unreachable; satisfies linter

    def test_graph_patch(self, repo_dir, tmp_path):
        earlier = _get_earlier_commit(repo_dir, self.LATER, self.N_BACK)

        _git("checkout", "-f", earlier, cwd=repo_dir)

        # Start LSP early for warm-up (parallel with SCIP)
        patcher = GraphPatcher(str(repo_dir), None, "rust")
        patcher.start_lsp()

        g = _build_graph_with_scip(
            repo_dir,
            "rust",
            Path.home() / ".codeminer" / "scip_cache" / self.REPO.replace("/", "_"),
        )
        patcher.code_graph = g

        _git("checkout", "-f", self.LATER[:12], cwd=repo_dir)
        changed = GraphPatcher.detect_changed_files(
            str(repo_dir),
            earlier,
            self.LATER,
            extensions={".rs"},
        )
        assert len(changed["modified"]) >= 1

        stats = patcher.patch_files(
            changed, earlier_commit=earlier, later_commit=self.LATER[:12]
        )
        assert stats["files_modified"] >= 1
        # These commits edit existing Rust declarations but add no Rust
        # symbols. The incremental path should preserve those vertex
        # identities while refreshing references in the changed ranges.
        assert stats.get("vertices_affected_preserved", 0) > 0

        unames_after = _get_unified_names(g)
        # Modified files' symbols should exist
        for f in changed["modified"]:
            file_syms = [u for u in unames_after if u.startswith(f + ":")]
            assert len(file_syms) > 0, f"Modified file {f} should have symbols"

        # Edges should be preserved or rediscovered
        total_refs = stats.get("refs_incoming", 0) + stats.get("refs_outgoing", 0)
        assert total_refs > 0, f"Expected reference edges for Rust, got {total_refs}"
        # Overall edges

        # ── Strict vertex assertions ──
        eq_prefix = "crates/ruff_linter/src/rules/pylint/rules/eq_without_hash.rs:"
        assert f"{eq_prefix}EqWithoutHash" in unames_after
        assert f"{eq_prefix}object_without_hash_method()" in unames_after
        assert f"{eq_prefix}EqHash" in unames_after
        assert f"{eq_prefix}EqHash.from_class()" in unames_after
        assert f"{eq_prefix}HasMethod" in unames_after

        # ── Strict cross-file edge assertions (10+) ──
        # object_without_hash_method() uses Checker, Diagnostic, StmtClassDef
        assert _has_ref_edge(
            g,
            f"{eq_prefix}object_without_hash_method()",
            "crates/ruff_linter/src/checkers/ast/mod.rs:Checker",
        ), "object_without_hash_method() → Checker"
        assert _has_ref_edge(
            g,
            f"{eq_prefix}object_without_hash_method()",
            "crates/ruff_diagnostics/src/diagnostic.rs:Diagnostic",
        ), "object_without_hash_method() → Diagnostic"
        assert _has_ref_edge(
            g,
            f"{eq_prefix}object_without_hash_method()",
            "crates/ruff_diagnostics/src/diagnostic.rs:Diagnostic.new()",
        ), "object_without_hash_method() → Diagnostic.new()"
        assert _has_ref_edge(
            g,
            f"{eq_prefix}object_without_hash_method()",
            "crates/ruff_python_ast/src/nodes.rs:StmtClassDef",
        ), "object_without_hash_method() → StmtClassDef"
        assert _has_ref_edge(
            g,
            f"{eq_prefix}object_without_hash_method()",
            "crates/ruff_python_ast/src/nodes.rs:StmtClassDef.name",
        ), "object_without_hash_method() → StmtClassDef.name"
        assert _has_ref_edge(
            g,
            f"{eq_prefix}object_without_hash_method()",
            "crates/ruff_linter/src/checkers/ast/mod.rs:Checker.diagnostics",
        ), "object_without_hash_method() → Checker.diagnostics"
        # EqHash.from_class() uses any_member_declaration, ClassMemberKind
        assert _has_ref_edge(
            g,
            f"{eq_prefix}EqHash.from_class()",
            "crates/ruff_python_semantic/src/analyze/class.rs:any_member_declaration()",
        ), "EqHash.from_class() → any_member_declaration()"
        assert _has_ref_edge(
            g,
            f"{eq_prefix}EqHash.from_class()",
            "crates/ruff_python_ast/src/nodes.rs:StmtClassDef",
        ), "EqHash.from_class() → StmtClassDef"
        assert _has_ref_edge(
            g,
            f"{eq_prefix}EqHash.from_class()",
            "crates/ruff_python_semantic/src/analyze/class.rs:ClassMemberDeclaration.kind()",
        ), "EqHash.from_class() → ClassMemberDeclaration.kind()"
        assert _has_ref_edge(
            g,
            f"{eq_prefix}object_without_hash_method()",
            "crates/ruff_python_ast/src/nodes.rs:Identifier<Ranged>.range()",
        ), "object_without_hash_method() → Identifier.range()"


class TestSWEBenchPython:
    """scikit-learn/scikit-learn: instance sklearn-10297 (from codeminer-base-dataset)."""

    REPO = "scikit-learn/scikit-learn"
    LATER = "b90661d6a46aa3619d3eec94d5281f5888add501"
    N_BACK = 3

    @pytest.fixture(scope="class")
    def repo_dir(self):
        _skip_if_no_scip("python")
        _skip_if_no_lsp("python")
        try:
            return _ensure_swebench_repo(self.REPO, self.LATER)
        except Exception as e:
            pytest.skip(f"Cannot clone repo: {e}")
            return None  # unreachable; satisfies linter

    def test_graph_patch(self, repo_dir, tmp_path):
        earlier = _get_earlier_commit(repo_dir, self.LATER, self.N_BACK)

        _git("checkout", "-f", earlier, cwd=repo_dir)

        # Start LSP early for warm-up (parallel with SCIP)
        patcher = GraphPatcher(str(repo_dir), None, "python")
        patcher.start_lsp()

        g = _build_graph_with_scip(
            repo_dir,
            "python",
            Path.home() / ".codeminer" / "scip_cache" / self.REPO.replace("/", "_"),
        )
        patcher.code_graph = g

        _git("checkout", "-f", self.LATER[:12], cwd=repo_dir)
        changed = GraphPatcher.detect_changed_files(
            str(repo_dir),
            earlier,
            self.LATER,
            extensions={".py"},
        )
        assert len(changed["modified"]) >= 1

        stats = patcher.patch_files(
            changed, earlier_commit=earlier, later_commit=self.LATER[:12]
        )
        assert stats["files_modified"] >= 1
        assert stats["vertices_created"] > 0

        unames_after = _get_unified_names(g)
        # Modified files' symbols should exist
        for f in changed["modified"]:
            file_syms = [u for u in unames_after if u.startswith(f + ":")]
            assert len(file_syms) > 0, f"Modified file {f} should have symbols"

        # Edges should be preserved or rediscovered
        total_refs = stats.get("refs_incoming", 0) + stats.get("refs_outgoing", 0)
        assert total_refs > 0, f"Expected reference edges for Python, got {total_refs}"
        # Overall edges

        # ── Strict vertex assertions (5+) ──
        assert "sklearn/cluster/k_means_.py:KMeans" in unames_after
        assert "sklearn/cluster/k_means_.py:k_means()" in unames_after
        assert "sklearn/cluster/k_means_.py:MiniBatchKMeans" in unames_after
        assert "sklearn/cluster/k_means_.py:KMeans.fit()" in unames_after
        assert "sklearn/cluster/k_means_.py:KMeans.predict()" in unames_after
        assert "sklearn/naive_bayes.py:GaussianNB" in unames_after
        assert "sklearn/naive_bayes.py:BaseDiscreteNB" in unames_after
        assert "sklearn/naive_bayes.py:MultinomialNB" in unames_after

        # _check_fit_data was renamed to _check_test_data
        assert (
            "sklearn/cluster/k_means_.py:KMeans._check_fit_data()" not in unames_after
        ), "_check_fit_data should be removed after patch"
        assert (
            "sklearn/cluster/k_means_.py:KMeans._check_test_data()" in unames_after
        ), "_check_test_data (renamed) should exist after patch"

        # ── Strict cross-file edge assertions (10+) ──
        # k_means_.py → utils
        assert _has_ref_edge(
            g,
            "sklearn/cluster/k_means_.py:k_means()",
            "sklearn/utils/validation.py:check_array()",
        ), "k_means() → check_array()"
        assert _has_ref_edge(
            g,
            "sklearn/cluster/k_means_.py:k_means()",
            "sklearn/utils/validation.py:check_random_state()",
        ), "k_means() → check_random_state()"
        assert _has_ref_edge(
            g,
            "sklearn/cluster/k_means_.py:k_means()",
            "sklearn/utils/extmath.py:row_norms()",
        ), "k_means() → row_norms()"
        assert _has_ref_edge(
            g,
            "sklearn/cluster/k_means_.py:KMeans.fit()",
            "sklearn/utils/validation.py:check_random_state()",
        ), "KMeans.fit() → check_random_state()"
        assert _has_ref_edge(
            g,
            "sklearn/cluster/k_means_.py:KMeans.transform()",
            "sklearn/utils/validation.py:check_is_fitted()",
        ), "KMeans.transform() → check_is_fitted()"
        # k_means_.py → metrics
        assert _has_ref_edge(
            g,
            "sklearn/cluster/k_means_.py:KMeans._transform()",
            "sklearn/metrics/pairwise.py:euclidean_distances()",
        ), "KMeans._transform() → euclidean_distances()"
        # k_means_.py inheritance → base
        assert _has_ref_edge(
            g, "sklearn/cluster/k_means_.py:KMeans", "sklearn/base.py:BaseEstimator"
        ), "KMeans → BaseEstimator (inheritance)"
        # naive_bayes.py → utils
        assert _has_ref_edge(
            g,
            "sklearn/naive_bayes.py:GaussianNB.fit()",
            "sklearn/utils/validation.py:check_X_y()",
        ), "GaussianNB.fit() → check_X_y()"
        assert _has_ref_edge(
            g,
            "sklearn/naive_bayes.py:BaseDiscreteNB.fit()",
            "sklearn/utils/validation.py:check_X_y()",
        ), "BaseDiscreteNB.fit() → check_X_y()"
        assert _has_ref_edge(
            g, "sklearn/naive_bayes.py:BaseNB", "sklearn/base.py:BaseEstimator"
        ), "BaseNB → BaseEstimator (inheritance)"

        # _check_fit_data was deleted — its edges should be gone
        check_fit_edges = _count_ref_edges_involving(
            g, "sklearn/cluster/k_means_.py:KMeans._check_fit_data()"
        )
        assert (
            check_fit_edges == 0
        ), f"_check_fit_data edges should be gone, got {check_fit_edges}"


class TestSWEBenchTypeScript:
    """preactjs/preact: instance preact-2896 (from codeminer-base-dataset)."""

    REPO = "preactjs/preact"
    LATER = "a9f7e676dc03b5008b8483e0937fc27c1af8287f"
    N_BACK = 3

    @pytest.fixture(scope="class")
    def repo_dir(self):
        _skip_if_no_scip("ts")
        _skip_if_no_lsp("typescript")
        try:
            return _ensure_swebench_repo(self.REPO, self.LATER)
        except Exception as e:
            pytest.skip(f"Cannot clone repo: {e}")
            return None  # unreachable; satisfies linter

    def test_graph_patch(self, repo_dir, tmp_path):
        earlier = _get_earlier_commit(repo_dir, self.LATER, self.N_BACK)

        _git("checkout", "-f", earlier, cwd=repo_dir)

        # Start LSP early for warm-up (parallel with SCIP)
        patcher = GraphPatcher(str(repo_dir), None, "ts")
        patcher.start_lsp()

        g = _build_graph_with_scip(
            repo_dir,
            "ts",
            Path.home() / ".codeminer" / "scip_cache" / self.REPO.replace("/", "_"),
            infer_tsconfig=True,
        )
        patcher.code_graph = g

        _git("checkout", "-f", self.LATER[:12], cwd=repo_dir)
        changed = GraphPatcher.detect_changed_files(
            str(repo_dir),
            earlier,
            self.LATER,
            extensions={".ts", ".tsx", ".js", ".jsx"},
        )
        assert len(changed["modified"]) >= 1

        stats = patcher.patch_files(
            changed, earlier_commit=earlier, later_commit=self.LATER[:12]
        )
        assert stats["files_modified"] >= 1
        assert stats["vertices_created"] > 0

        unames_after = _get_unified_names(g)
        # Modified files' symbols should exist (except re-export-only files like index.js)
        for f in changed["modified"]:
            if f.endswith("index.js") or f.endswith("index.ts"):
                continue  # Re-export files may have no symbols
            file_syms = [u for u in unames_after if u.startswith(f + ":")]
            assert len(file_syms) > 0, f"Modified file {f} should have symbols"

        # Edges should be preserved or rediscovered
        total_refs = stats.get("refs_incoming", 0) + stats.get("refs_outgoing", 0)
        assert total_refs > 0, f"Expected reference edges for TS, got {total_refs}"
        # Overall edges

        # ── Strict vertex assertions (5+) ──
        assert "compat/src/portals.js:Portal()" in unames_after
        assert "compat/src/portals.js:createPortal()" in unames_after
        assert "compat/src/portals.js:ContextProvider()" in unames_after
        assert "src/diff/children.js:diffChildren()" in unames_after
        assert "src/diff/children.js:toChildArray()" in unames_after
        assert "src/diff/children.js:reorderChildren()" in unames_after

        # ── Strict cross-file edge assertions (10+) ──
        # portals.js imports { createElement, render } from 'preact'
        # SCIP resolves 'preact' to the .d.ts declarations with preact/ prefix
        assert _has_ref_edge(
            g, "compat/src/portals.js:Portal()", "src/index.d.ts:preact/render()"
        ), "Portal() → preact/render()"
        assert _has_ref_edge(
            g,
            "compat/src/portals.js:createPortal()",
            "src/index.d.ts:preact/createElement()",
        ), "createPortal() → preact/createElement()"

        # children.js: import { diff, unmount, applyRef } from './index'
        assert _has_ref_edge(
            g, "src/diff/children.js:diffChildren()", "src/diff/index.js:diff()"
        ), "diffChildren → diff()"
        assert _has_ref_edge(
            g, "src/diff/children.js:diffChildren()", "src/diff/index.js:unmount()"
        ), "diffChildren → unmount()"

        # children.js: import { getDomSibling } from '../component'
        assert _has_ref_edge(
            g, "src/diff/children.js:diffChildren()", "src/component.js:getDomSibling()"
        ), "diffChildren → getDomSibling()"

        # children.js: import { createVNode, Fragment } from '../create-element'
        assert _has_ref_edge(
            g,
            "src/diff/children.js:diffChildren()",
            "src/create-element.js:createVNode()",
        ), "diffChildren → createVNode()"

        # children.js: import { removeNode } from '../util'
        assert _has_ref_edge(
            g, "src/diff/children.js:diffChildren()", "src/util.js:removeNode()"
        ), "diffChildren → removeNode()"

        # children.js: import { EMPTY_OBJ, EMPTY_ARR } from '../constants'
        assert _has_ref_edge(
            g, "src/diff/children.js:diffChildren()", "src/constants.js:EMPTY_OBJ"
        ), "diffChildren → EMPTY_OBJ"
