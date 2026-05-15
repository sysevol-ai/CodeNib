#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Walk each pilot repo's first-parent commit chain and benchmark three
graph-update strategies per step:

  1. fully-rebuild — wipe indexer cache, re-index from scratch.
  2. file-level-patch (patcher mode="naive") — delete + rebuild every
     vertex of every modified file, no symbol-level diff.
  3. symbol-level-patch (patcher mode="symbol") — the production
     incremental patcher (``patcher_base.py``).

Each strategy is run inside one long-lived LSP server per (instance, strategy)
to amortise LSP cold-start across the chain. Strategies run as phases:

  Phase 1 — symbol-level-patch, one LSP, N steps
  Phase 2 — fully-rebuild, cold-start each step (skip when only docs changed)
  Phase 3 — file-level-patch, one LSP, N steps

Output: one JSONL row per (instance, step) with each strategy's t_s, v, e,
status, patcher stats, profiler section breakdown, and an aggregate LSP
call summary. ``notes`` flags commits with no source diff
(``"no-source-change"``) or super-large diffs (``"too_large"``); both are
kept in the output (not skipped) — they reflect real-world commit traffic.

Usage:
    # Use the hardcoded DEFAULT_INSTANCES list:
    python scripts/profile_incremental_graph.py --steps 20

    # Pass instances inline:
    python scripts/profile_incremental_graph.py \
        --instances "gin-gonic__gin-1805:70a0aba3...,pydata__xarray-3151:118f4d9..." \
        --steps 20

    # Or from a JSON file:
    python scripts/profile_incremental_graph.py --config instances.json --steps 20
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
# Allow the C++ pybind decoder to load when present (faster SCIP decode).
_CORE = HERE.parent / "build" / "core"
if _CORE.exists():
    sys.path.insert(0, str(_CORE))

from codeminer.code_chunker import CodeChunker  # noqa: E402
from codeminer.graph.code_graph import CodeGraph  # noqa: E402
from codeminer.graph.incremental.graph_patcher import GraphPatcher  # noqa: E402
from codeminer.ls_router import LSIndexer  # noqa: E402

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_CACHE_ROOT = Path("/mnt/data/codeminer")
DEFAULT_STEPS = 20
DEFAULT_TIMEOUT_S = 600
DEFAULT_MAX_DELTA_LINES = 5000
DEFAULT_MAX_WALK = 200
DEFAULT_OUTPUT = Path("profile_incremental.jsonl")

# Hardcoded fallback instance list — 10 per language × 5 languages = 50.
# Override with --instances or --config. Each tuple is
# (instance_id, language, base_commit). Bases match the chain experiment.
DEFAULT_INSTANCES: List[Tuple[str, str, str]] = [
    # C/C++
    ("fmtlib__fmt-2310", "cpp", "7612f18dc8e0112e64e0845a1ebe9da6cfb8a123"),
    ("fmtlib__fmt-3248", "cpp", "840ec8569ddb304345f082bb82d733742f431504"),
    ("jqlang__jq-2235", "cpp", "b8816caf0a7f58c6483f4344e222a6fea47732e8"),
    ("jqlang__jq-2750", "cpp", "98f709d0c1fd87a8b6a3e10c679442667712b264"),
    (
        "micropython__micropython-13569",
        "cpp",
        "2d7fb9a715d8c9f1f7ba73a62cfd587fa6ffc24a",
    ),
    (
        "micropython__micropython-15898",
        "cpp",
        "b0ba151102a6c1b2e0e4c419c6482a64677c9b40",
    ),
    ("redis__redis-10068", "cpp", "e88f6acb94c77c9a5b81f0b2a8bd132b2a5c3d3c"),
    ("redis__redis-13338", "cpp", "94b9072e44af0bae8cfe2de5cfa4af7b8e399759"),
    ("valkey-io__valkey-790", "cpp", "35a188833335332980a16239ac2efebf241c8a6a"),
    ("valkey-io__valkey-1842", "cpp", "4de8cf3567b4dfd3a541219462b22d46c730c4b4"),
    # Go
    ("caddyserver__caddy-4943", "go", "7f6a328b4744bbdd5ee07c5eccb3f897390ff982"),
    ("caddyserver__caddy-5404", "go", "960150bb034dc9a549ee7289b1a4eb4abafeb30a"),
    ("gin-gonic__gin-1805", "go", "70a0aba3e423246be37462cfdaedd510c26c566e"),
    ("gin-gonic__gin-2121", "go", "2ee0e963942d91be7944dfcc07dc8c02a4a78566"),
    ("gohugoio__hugo-12171", "go", "a322282e70c2935c79f9c0f5c593bfdd032eb964"),
    ("gohugoio__hugo-12343", "go", "060cce0a910ffd9dc4e92efbe758fc5f187c3145"),
    ("hashicorp__terraform-34814", "go", "310331bdc6b747b68b2e2dd6be257dfc78677973"),
    ("hashicorp__terraform-35611", "go", "047c1af0b4a9e1e650f3811d011bcb92b2ceb15a"),
    ("prometheus__prometheus-10633", "go", "64fc3e58fec13af55968db89a484b9e0c5425bd6"),
    ("prometheus__prometheus-11859", "go", "5ec1b4baaf01dae0b22567db53886c83b74e0e76"),
    # Python
    ("astropy__astropy-12907", "python", "d16bfe05a744909de4b27f5875fe0d4ed41ce607"),
    ("astropy__astropy-13033", "python", "298ccb478e6bf092953bca67a3d29dc6c35f6752"),
    (
        "matplotlib__matplotlib-13989",
        "python",
        "a3e2897bfaf9eaac1d6649da535c4e721c89fa69",
    ),
    (
        "matplotlib__matplotlib-14623",
        "python",
        "d65c9ca20ddf81ef91199e6d819f9d3506ef477c",
    ),
    ("pydata__xarray-2905", "python", "7c4e2ac83f7b4306296ff9b7b51aaf016e5ad614"),
    ("pydata__xarray-3151", "python", "118f4d996e7711c9aced916e6049af9f28d5ec66"),
    (
        "scikit-learn__scikit-learn-10297",
        "python",
        "b90661d6a46aa3619d3eec94d5281f5888add501",
    ),
    (
        "scikit-learn__scikit-learn-10844",
        "python",
        "97523985b39ecde369d83352d7c3baf403b60a22",
    ),
    ("sympy__sympy-11618", "python", "360290c4c401e386db60723ddb0109ed499c9f6e"),
    ("sympy__sympy-12096", "python", "d7c3045115693e887bcd03599b7ca4650ac5f2cb"),
    # Rust
    ("astral-sh__ruff-15309", "rust", "75a24bbc67aa31b825b6326cfb6e6afdf3ca90d5"),
    ("astral-sh__ruff-15330", "rust", "b2a0d68d70ee690ea871fe9b3317be43075ddb33"),
    ("nushell__nushell-12901", "rust", "cc9f41e553333b1ad0aa4185a912f7a52355a238"),
    ("nushell__nushell-12950", "rust", "0c5a67f4e5bf17a2fffbe3cbb54792a6464b3237"),
    ("sharkdp__bat-2201", "rust", "02a9d191ed06e6e37d4e352f12e25e65e8037dd0"),
    ("sharkdp__bat-2260", "rust", "c14ce4f7caa36fe524975c51f7aae3d5a852d1f9"),
    ("tokio-rs__tokio-4898", "rust", "9d9488db67136651f85d839efcb5f61aba8531c9"),
    ("tokio-rs__tokio-6551", "rust", "0a85a9662d30139c779ea49a59be30db0f292b5d"),
    ("uutils__coreutils-6575", "rust", "bc0b4880e000b881a1cabc733b2d261f3526d15e"),
    ("uutils__coreutils-6682", "rust", "b89a6255a9cb5ee09867707f395d02929273dc45"),
    # TS/JS
    ("axios__axios-4731", "ts", "c30252f685e8f4326722de84923fcbc8cf557f06"),
    ("axios__axios-4738", "ts", "9bb016f95e6de871a44f3276fd06562704a0abb0"),
    ("babel__babel-13928", "ts", "5134505bf93013c5fa7df66704df8d04becb7f7d"),
    ("babel__babel-14532", "ts", "fbd065256ca00d68089293ad48ea9aeba5481914"),
    ("facebook__docusaurus-10130", "ts", "02e38d8ccf7811af27a9c15ddbadf4d26cfc0eab"),
    ("facebook__docusaurus-10309", "ts", "5e9e1d051b2217b95498ecbc10f7e33f64e7a4d3"),
    ("preactjs__preact-2757", "ts", "b17a932342bfdeeaf1dc0fbe4f436c83e258d6c8"),
    ("preactjs__preact-2896", "ts", "a9f7e676dc03b5008b8483e0937fc27c1af8287f"),
    ("vuejs__core-11589", "ts", "3653bc0f45d6fedf84e29b64ca52584359c383c0"),
    ("vuejs__core-11870", "ts", "67d6596d40b1807b9cd8eb0d9282932ea77be3c0"),
]

LANG_EXTS: Dict[str, set] = {
    "cpp": {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx"},
    "go": {".go"},
    "python": {".py"},
    "rust": {".rs"},
    "ts": {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"},
}

LANG_PATHSPEC: Dict[str, List[str]] = {
    "cpp": ["*.c", "*.cc", "*.cpp", "*.cxx", "*.h", "*.hh", "*.hpp", "*.hxx"],
    "go": ["*.go"],
    "python": ["*.py"],
    "rust": ["*.rs"],
    "ts": ["*.ts", "*.tsx", "*.js", "*.jsx", "*.mjs", "*.cjs"],
}

_TEST_TOKENS = (
    "test",
    "tests",
    "testing",
    "spec",
    "specs",
    "__tests__",
    "testdata",
    "fixtures",
)


def is_test_path(path: str) -> bool:
    p = path.lower()
    parts = p.split("/")
    base = parts[-1] if parts else ""
    for part in parts:
        if part in _TEST_TOKENS:
            return True
        if part.startswith("test_"):
            return True
    if base.startswith("test_"):
        return True
    if base.endswith("_test.go") or base.endswith("_test.py"):
        return True
    if base.endswith((".test.ts", ".test.tsx", ".test.js", ".test.jsx")):
        return True
    if base.endswith((".spec.ts", ".spec.js")):
        return True
    return False


# ---------------------------------------------------------------------------
# git helpers
# ---------------------------------------------------------------------------


def run_git(args: Sequence[str], cwd: Path, timeout: int = 120):
    return subprocess.run(
        ["git"] + list(args),
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def checkout(repo: Path, sha: str) -> None:
    r = run_git(["checkout", "-f", sha], cwd=repo, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"checkout {sha[:12]} failed: {r.stderr[-400:]}")
    # Preserve LSP/indexer caches AND compile-database artifacts across
    # steps. Without these excludes ``git clean -fd`` wipes:
    #   .cache/clangd/index, target/rust-analyzer, node_modules,
    #   compile_commands.json / build
    run_git(
        [
            "clean",
            "-fd",
            "-e",
            ".cache",
            "-e",
            "target",
            "-e",
            "node_modules",
            "-e",
            "compile_commands.json",
            "-e",
            "build",
        ],
        cwd=repo,
        timeout=60,
    )


def resolve_default_branch(repo: Path) -> Optional[str]:
    r = run_git(["symbolic-ref", "refs/remotes/origin/HEAD"], cwd=repo, timeout=15)
    if r.returncode == 0:
        ref = r.stdout.strip()
        if ref:
            return ref
    for cand in ("origin/main", "origin/master", "origin/develop", "origin/trunk"):
        r = run_git(["rev-parse", "--verify", cand], cwd=repo, timeout=15)
        if r.returncode == 0:
            return cand
    return None


def first_parent_commits(repo: Path, base: str, limit: int) -> List[str]:
    """SHAs strictly after ``base`` on first-parent path, oldest-first."""
    upper = resolve_default_branch(repo)
    if upper is None:
        return []
    r = run_git(
        ["log", "--first-parent", "--reverse", "--format=%H", f"{base}..{upper}"],
        cwd=repo,
        timeout=120,
    )
    if r.returncode != 0:
        return []
    out = [s for s in r.stdout.splitlines() if s.strip()]
    return out[:limit]


def diff_shortstat(
    repo: Path, base: str, target: str, pathspec: Optional[List[str]] = None
) -> Tuple[int, int]:
    """Return (lines_changed, files_changed). lines = insertions + deletions."""
    args = ["diff", "--shortstat", f"{base}..{target}"]
    if pathspec:
        args.append("--")
        args.extend(pathspec)
    r = run_git(args, cwd=repo, timeout=120)
    if r.returncode != 0:
        return (0, 0)
    line = r.stdout.strip()
    files = ins = dels = 0
    for tok in line.split(","):
        tok = tok.strip()
        try:
            if "file" in tok:
                files = int(tok.split()[0])
            elif "insertion" in tok:
                ins = int(tok.split()[0])
            elif "deletion" in tok:
                dels = int(tok.split()[0])
        except (ValueError, IndexError):
            # Malformed token — best-effort parse of shortstat, skip this
            # field rather than failing the whole diff.
            pass
    return (ins + dels, files)


def diff_file_list(
    repo: Path,
    base: str,
    target: str,
    pathspec: Optional[List[str]] = None,
    exclude_test: bool = True,
) -> List[str]:
    args = ["diff", "--name-only", f"{base}..{target}"]
    if pathspec:
        args.append("--")
        args.extend(pathspec)
    r = run_git(args, cwd=repo, timeout=120)
    if r.returncode != 0:
        return []
    out = []
    for f in r.stdout.splitlines():
        f = f.strip()
        if not f:
            continue
        if exclude_test and is_test_path(f):
            continue
        out.append(f)
    return out


def git_show_to_file(repo: Path, sha: str, path: str, dest: Path) -> bool:
    r = subprocess.run(
        ["git", "show", f"{sha}:{path}"],
        cwd=str(repo),
        capture_output=True,
        timeout=30,
    )
    if r.returncode != 0:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(r.stdout)
    return True


def _chunk_to_symbol_map(
    chunker, file_path: Path, relative_path: str
) -> Dict[Tuple[str, str], str]:
    try:
        chunks = chunker.chunk_file(str(file_path), relative_path=relative_path)
    except Exception:
        return {}
    out: Dict[Tuple[str, str], str] = {}
    for c in chunks:
        if c.chunk_type in ("file", "header"):
            continue
        h = hashlib.sha1(c.content.encode("utf-8", errors="replace")).hexdigest()
        out[(c.node_id, c.chunk_type)] = h
    return out


def compute_delta_symbols(
    repo: Path, base: str, target: str, changed_files: List[str], language: str
) -> Tuple[int, int, int]:
    """Return (added, removed, modified) symbol counts via tree-sitter."""
    if not changed_files:
        return (0, 0, 0)
    chunker = CodeChunker(language=language, chunk_depth=2)
    added = removed = modified = 0
    with tempfile.TemporaryDirectory(prefix="chainbench_") as tmpdir:
        tmp = Path(tmpdir)
        for rel_path in changed_files:
            base_file = tmp / "base" / rel_path
            tgt_file = tmp / "target" / rel_path
            has_base = git_show_to_file(repo, base, rel_path, base_file)
            has_target = git_show_to_file(repo, target, rel_path, tgt_file)
            base_map = (
                _chunk_to_symbol_map(chunker, base_file, rel_path) if has_base else {}
            )
            tgt_map = (
                _chunk_to_symbol_map(chunker, tgt_file, rel_path) if has_target else {}
            )
            base_keys = set(base_map.keys())
            tgt_keys = set(tgt_map.keys())
            added += len(tgt_keys - base_keys)
            removed += len(base_keys - tgt_keys)
            for k in base_keys & tgt_keys:
                if base_map[k] != tgt_map[k]:
                    modified += 1
    return (added, removed, modified)


# ---------------------------------------------------------------------------
# Cold-start helpers
# ---------------------------------------------------------------------------


def clear_indexer_cache(repo: Path, language: str) -> None:
    """Wipe per-language indexer caches so the next rebuild is cold."""
    paths: List[Path] = []
    if language in ("cpp", "c"):
        paths.append(repo / ".cache" / "clangd")
    elif language in ("ts", "typescript"):
        paths.append(repo / "node_modules" / ".cache" / "scip-typescript")
    elif language == "rust":
        paths.append(repo / "target" / "rust-analyzer")
    for p in paths:
        if p.exists():
            try:
                shutil.rmtree(p)
            except Exception:
                # Cache wipe is best-effort: if a permission error or in-use
                # file prevents removal, the rebuild may warm-start but that
                # just inflates t_rebuild (not a correctness issue).
                pass


# ---------------------------------------------------------------------------
# SIGALRM per-strategy timeout
# ---------------------------------------------------------------------------


class StrategyTimeout(Exception):
    pass


def _alarm_handler(signum, frame):
    raise StrategyTimeout()


def run_with_timeout(seconds: int, fn, *args, **kwargs):
    if seconds <= 0:
        return fn(*args, **kwargs)
    prev = signal.signal(signal.SIGALRM, _alarm_handler)
    signal.alarm(seconds)
    try:
        return fn(*args, **kwargs)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, prev)


# ---------------------------------------------------------------------------
# LSP profile aggregation
# ---------------------------------------------------------------------------


def _summarize_lsp_profile(path: Path) -> Dict[str, Any]:
    if not path or not path.exists():
        return {"calls_total": 0, "calls_by_method": {}, "ms_by_method": {}}
    by_method: Dict[str, Dict[str, float]] = {}
    total = 0
    for line in path.open():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        m = r.get("m", "?")
        d = by_method.setdefault(m, {"count": 0, "ms": 0.0, "empty": 0})
        d["count"] += 1
        d["ms"] += r.get("ms", 0)
        if (r.get("n", 0) or 0) == 0:
            d["empty"] += 1
        total += 1
    return {
        "calls_total": total,
        "calls_by_method": {m: int(v["count"]) for m, v in by_method.items()},
        "ms_by_method": {m: round(v["ms"], 1) for m, v in by_method.items()},
        "empty_by_method": {m: int(v["empty"]) for m, v in by_method.items()},
    }


# ---------------------------------------------------------------------------
# Strategy 1: full rebuild at one commit
# ---------------------------------------------------------------------------


def _do_rebuild(
    language: str, repo: Path, target_sha: str, out_dir: Path
) -> Dict[str, Any]:
    """Cold-start full SCIP/clangd rebuild at ``target_sha``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for f in ("index.scip", "index.decoded", "graph.pkl"):
        (out_dir / f).unlink(missing_ok=True)

    checkout(repo, target_sha)
    indexer = LSIndexer(
        project_root=repo,
        output_dir=out_dir,
        language=language,
        decoder_backend="core",
    )
    t0 = time.monotonic()
    g = indexer.run_pipeline(
        output_file=str(out_dir / "graph.pkl"),
        skip_level=None,
        reset_profiler=True,
        report_profile=False,
    )
    elapsed = time.monotonic() - t0
    if g is None:
        raise RuntimeError("rebuild returned no graph")
    return {
        "t_s": elapsed,
        "v": g.graph.vcount(),
        "e": g.graph.ecount(),
        "status": "ok",
    }


def run_rebuild_step(
    target: Dict[str, Any],
    cache_root: Path,
    step_out_dir: Path,
    prev_rebuild_pkl: Optional[Path],
    timeout_s: int,
    log,
) -> Dict[str, Any]:
    """Run a rebuild for one step. When the commit touched 0 source files,
    skip the cold rebuild and reuse the previous step's rebuild graph.
    """
    iid = target["instance_id"]
    lang = target["language"]
    repo = cache_root / iid / "repo"
    rebuild_pkl = step_out_dir / "rebuild.pkl"

    is_no_source = target.get("delta_files_test_excluded", 0) == 0
    if is_no_source and prev_rebuild_pkl is not None and prev_rebuild_pkl.exists():
        log(f"  rebuild step{target['step']} SKIPPED (no source diff)")
        try:
            shutil.copyfile(str(prev_rebuild_pkl), str(rebuild_pkl))
            g = CodeGraph.load_graph(str(rebuild_pkl))
            return {
                "t_s": 0.0,
                "v": g.graph.vcount(),
                "e": g.graph.ecount(),
                "status": "ok",
                "skipped_no_source": True,
            }
        except Exception as e:
            return {
                "status": "error",
                "error": f"reuse-prev failed: {type(e).__name__}: {e}",
            }

    log(f"  rebuild step{target['step']} {target['to_commit'][:8]} (cold start)")
    try:
        clear_indexer_cache(repo, lang)
        info = run_with_timeout(
            timeout_s,
            _do_rebuild,
            lang,
            repo,
            target["to_commit"],
            step_out_dir,
        )
        # _do_rebuild writes graph.pkl; rename to rebuild.pkl
        src = step_out_dir / "graph.pkl"
        if src.exists() and src != rebuild_pkl:
            shutil.move(str(src), str(rebuild_pkl))
        return info
    except StrategyTimeout:
        return {"status": "timeout", "t_s": timeout_s}
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------
# Strategies 2/3: patcher phase (one long-lived LSP, walk the whole chain)
# ---------------------------------------------------------------------------


def _warmup_lsp(
    patcher, repo: Path, base_sha: str, target_sha: str, language: str
) -> float:
    """Open changed files + dummy ``definition`` so LSP first-call cost
    falls outside the timed region. Excluded from t_s.
    """
    t0 = time.monotonic()
    try:
        names = subprocess.check_output(
            ["git", "diff", "--name-only", base_sha, target_sha],
            cwd=str(repo),
            text=True,
            stderr=subprocess.DEVNULL,
        ).split()
    except subprocess.CalledProcessError:
        names = []
    exts = LANG_EXTS.get(language, set())
    abs_paths = [repo / n for n in names if Path(n).suffix in exts]
    lsp = getattr(patcher, "lsp_client", None) or getattr(
        getattr(patcher, "_impl", None), "lsp_client", None
    )
    if lsp is None:
        return 0.0
    for p in abs_paths:
        if p.is_file():
            try:
                lsp.open_document(str(p))
            except Exception:
                # Warmup is best-effort: a single file failing to open
                # just means its first real query inside the timed region
                # will pay the lazy-init cost. Don't abort the run.
                pass
    for p in abs_paths:
        if not p.is_file():
            continue
        try:
            syms = lsp.document_symbol(str(p)) or []
            real_pos = None

            def _walk(items):
                nonlocal real_pos
                for s in items or []:
                    if real_pos:
                        return
                    sel = s.get("selectionRange", s.get("range", {}))
                    line = sel.get("start", {}).get("line")
                    char = sel.get("start", {}).get("character", 0)
                    if line is not None:
                        real_pos = (line, char)
                        return
                    _walk(s.get("children", []))

            _walk(syms)
            if real_pos:
                lsp.definition(str(p), real_pos[0], real_pos[1])
        except Exception:
            # Warmup is best-effort. If documentSymbol / definition fails
            # here, the first call inside the timed region absorbs the cost
            # instead — measurement noise, not a fatal error.
            pass
    return time.monotonic() - t0


def run_patcher_phase(
    strategy: str,
    mode: str,
    chain: List[Dict[str, Any]],
    base_graph_pkl: Path,
    cache_root: Path,
    out_root: Path,
    timeout_s: int,
    lsp_profile_dir: Optional[Path],
    log,
    out_filename: str,
) -> Dict[int, Dict[str, Any]]:
    """Run ``mode`` patcher across the chain with a single long-lived LSP.

    The graph mutates in memory across steps, so each step inherits the
    prior step's output without pickle round-trips.

    Returns ``{step -> per-step sub-record}``.
    """
    iid = chain[0]["instance_id"]
    lang = chain[0]["language"]
    repo = cache_root / iid / "repo"
    sub_records: Dict[int, Dict[str, Any]] = {}

    try:
        g = CodeGraph.load_graph(str(base_graph_pkl))
    except Exception as e:
        for t in chain:
            sub_records[t["step"]] = {"status": "error", "error": f"load_base: {e}"}
        return sub_records

    patcher = GraphPatcher(
        project_root=str(repo),
        code_graph=g,
        language=lang,
        mode=mode,
    )

    t0 = time.monotonic()
    try:
        patcher.start_lsp()
    except Exception as e:
        for t in chain:
            sub_records[t["step"]] = {"status": "error", "error": f"start_lsp: {e}"}
        return sub_records
    cold_start_total = time.monotonic() - t0

    # Phase-warmup: at base commit, open + force-resolve every file the
    # chain will touch. Forces lazy LSP indexing before any timed region.
    phase_warmup_t0 = time.monotonic()
    base_commit = chain[0]["from_commit"]
    try:
        checkout(repo, base_commit)
    except Exception as exc:
        # Setup-time error: warmup wants to open files at the base state,
        # but a per-step checkout will run again before each step's timed
        # region. Log so a failed run isn't silent.
        log(f"  warning: phase-warmup checkout to base failed: {exc}")
    if lang != "cpp":
        try:
            exts = LANG_EXTS.get(lang, set())
            seen_files: set = set()
            for t in chain:
                try:
                    names = subprocess.check_output(
                        [
                            "git",
                            "diff",
                            "--name-only",
                            t["from_commit"],
                            t["to_commit"],
                        ],
                        cwd=str(repo),
                        text=True,
                        stderr=subprocess.DEVNULL,
                    ).split()
                except subprocess.CalledProcessError:
                    names = []
                for n in names:
                    if Path(n).suffix in exts:
                        seen_files.add(n)
            lsp = getattr(patcher, "lsp_client", None) or getattr(
                getattr(patcher, "_impl", None), "lsp_client", None
            )
            if lsp is not None:
                for rel in sorted(seen_files):
                    abs_p = repo / rel
                    if not abs_p.is_file():
                        continue
                    try:
                        lsp.open_document(str(abs_p))
                    except Exception:
                        # Single-file warmup-open failure is non-fatal;
                        # see best-effort rationale in _warmup_lsp.
                        pass
        except Exception:
            # The whole phase-warmup block is non-fatal — if any branch of
            # the union-of-changed-files enumeration or lsp probing blows
            # up, the per-step warmup inside the loop is the fallback.
            pass
    phase_warmup_s = time.monotonic() - phase_warmup_t0
    log(
        f"  {strategy}: LSP cold-start {cold_start_total:.1f}s + "
        f"phase-warmup {phase_warmup_s:.1f}s, running {len(chain)} steps"
    )

    try:
        for i, t in enumerate(chain):
            step = t["step"]
            step_dir = out_root / iid / f"step{step}"
            step_dir.mkdir(parents=True, exist_ok=True)
            out_pkl = step_dir / out_filename

            # Per-step LSP profile capture.
            profile_path: Optional[Path] = None
            if lsp_profile_dir is not None:
                lsp_profile_dir.mkdir(parents=True, exist_ok=True)
                profile_path = lsp_profile_dir / f"{iid}_step{step}_{strategy}.jsonl"
                profile_path.unlink(missing_ok=True)
                os.environ["LSP_PROFILE_PATH"] = str(profile_path)
            else:
                os.environ.pop("LSP_PROFILE_PATH", None)

            try:
                checkout(repo, t["to_commit"])
            except Exception as e:
                sub_records[step] = {
                    "status": "error",
                    "error": f"checkout: {type(e).__name__}: {e}",
                }
                os.environ.pop("LSP_PROFILE_PATH", None)
                continue

            # Per-step warmup (cpp skipped: patcher_cpp doesn't query LSP).
            warmup_t0 = time.monotonic()
            if lang != "cpp":
                try:
                    _warmup_lsp(patcher, repo, t["from_commit"], t["to_commit"], lang)
                except Exception:
                    # Warmup-the-step is best-effort: any failure just shifts
                    # the first-call cost into the timed region.
                    pass
            warmup_s = time.monotonic() - warmup_t0

            log(
                f"  {strategy} step{step} {t['to_commit'][:8]} "
                f"L{t['delta_lines']} F{t['delta_files_test_excluded']} "
                f"S{t['delta_symbols']}"
            )
            try:
                changed = patcher.detect_changed_files(
                    str(repo),
                    t["from_commit"],
                    t["to_commit"],
                    extensions=LANG_EXTS.get(lang),
                )
                pt0 = time.monotonic()
                stats = run_with_timeout(
                    timeout_s,
                    patcher.patch_files,
                    changed,
                    earlier_commit=t["from_commit"],
                    later_commit=t["to_commit"],
                )
                elapsed = time.monotonic() - pt0
                g.save_graph(str(out_pkl))
                sub: Dict[str, Any] = {
                    "t_s": elapsed,
                    "cold_start_s": cold_start_total if i == 0 else 0.0,
                    "warmup_s": warmup_s,
                    "v": g.graph.vcount(),
                    "e": g.graph.ecount(),
                    "status": "ok",
                    "stats": {
                        k: v
                        for k, v in stats.items()
                        if isinstance(v, (int, float, str, bool))
                    },
                    "profile": stats.get("profile") or {},
                }
            except StrategyTimeout:
                sub = {
                    "status": "timeout",
                    "t_s": timeout_s,
                    "cold_start_s": cold_start_total if i == 0 else 0.0,
                    "warmup_s": warmup_s,
                }
            except Exception as e:
                sub = {
                    "status": "error",
                    "error": f"{type(e).__name__}: {e}",
                    "cold_start_s": cold_start_total if i == 0 else 0.0,
                    "warmup_s": warmup_s,
                }
            sub["lsp"] = (
                _summarize_lsp_profile(profile_path)
                if profile_path
                else {"calls_total": 0, "calls_by_method": {}, "ms_by_method": {}}
            )
            os.environ.pop("LSP_PROFILE_PATH", None)
            sub_records[step] = sub
    finally:
        try:
            patcher.stop_lsp()
        except Exception:
            # Teardown is best-effort: a hung/dead LSP child should not
            # propagate an exception out of `finally` and mask the real
            # result we already wrote to sub_records.
            pass

    return sub_records


# ---------------------------------------------------------------------------
# Commit chain construction
# ---------------------------------------------------------------------------


def build_chain(
    iid: str,
    language: str,
    base_commit: str,
    cache_root: Path,
    n_steps: int,
    max_walk: int,
    max_delta_lines: int,
    log,
) -> List[Dict[str, Any]]:
    """Walk first-parent commits and emit ``n_steps`` rows. ``notes`` flags
    no-source / too-large commits but the chain still advances past them so
    real-world commit traffic is faithfully represented.
    """
    repo = cache_root / iid / "repo"
    if not repo.exists():
        log(f"[{iid}] repo missing: {repo}")
        return []

    pathspec = LANG_PATHSPEC.get(language, [])
    candidates = first_parent_commits(repo, base_commit, max_walk)
    out: List[Dict[str, Any]] = []
    prev = base_commit
    step = 0
    for sha in candidates:
        files_all = diff_file_list(repo, prev, sha, pathspec, exclude_test=False)
        files_excl = diff_file_list(repo, prev, sha, pathspec, exclude_test=True)
        if not files_excl:
            delta_lines = 0
        else:
            delta_lines, _ = diff_shortstat(repo, prev, sha, files_excl)
        added, removed, modified = compute_delta_symbols(
            repo,
            prev,
            sha,
            files_excl,
            language,
        )

        if delta_lines > max_delta_lines:
            notes = "too_large"
        elif len(files_excl) == 0:
            notes = "no-source-change"
        else:
            notes = None

        step += 1
        out.append(
            {
                "instance_id": iid,
                "language": language,
                "step": step,
                "from_commit": prev,
                "to_commit": sha,
                "delta_lines": delta_lines,
                "delta_files": len(files_all),
                "delta_files_test_excluded": len(files_excl),
                "delta_symbols": added + removed + modified,
                "delta_symbols_added": added,
                "delta_symbols_removed": removed,
                "delta_symbols_modified": modified,
                "notes": notes,
            }
        )
        prev = sha
        if step >= n_steps:
            break
    return out


# ---------------------------------------------------------------------------
# Per-instance orchestration
# ---------------------------------------------------------------------------


def process_instance(
    iid: str,
    language: str,
    base_commit: str,
    cache_root: Path,
    n_steps: int,
    max_walk: int,
    max_delta_lines: int,
    timeout_s: int,
    out_root: Path,
    lsp_profile_dir: Optional[Path],
    log,
) -> List[Dict[str, Any]]:
    """Build chain, then run three strategies as phases. Returns one result
    row per step (each row contains rebuild/naive/ours sub-records)."""
    chain = build_chain(
        iid, language, base_commit, cache_root, n_steps, max_walk, max_delta_lines, log
    )
    if not chain:
        return []
    repo = cache_root / iid / "repo"

    # The base graph for the patcher is loaded from the instance's pre-existing
    # rebuild at ``base_commit``. Each pilot repo is expected to have one at
    # ``<cache_root>/<iid>/graph.pkl``.
    base_graph_pkl = cache_root / iid / "graph.pkl"
    if not base_graph_pkl.exists():
        log(f"[{iid}] missing base graph: {base_graph_pkl}")
        return [
            {
                "instance_id": iid,
                "language": language,
                "error": f"no base graph at {base_graph_pkl}",
            }
        ]

    # Initial per-step record (merged with strategy sub-records below).
    step_recs: Dict[int, Dict[str, Any]] = {}
    for t in chain:
        step_recs[t["step"]] = {
            "instance_id": t["instance_id"],
            "language": t["language"],
            "step": t["step"],
            "from_commit": t["from_commit"],
            "to_commit": t["to_commit"],
            "delta_lines": t["delta_lines"],
            "delta_files": t["delta_files"],
            "delta_files_test_excluded": t["delta_files_test_excluded"],
            "delta_symbols": t["delta_symbols"],
            "delta_symbols_added": t["delta_symbols_added"],
            "delta_symbols_removed": t["delta_symbols_removed"],
            "delta_symbols_modified": t["delta_symbols_modified"],
            "notes": t["notes"],
            "started": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    log(f"=== {iid} ({language}) — {len(chain)} steps ===")

    # Phase 1: symbol-level patcher.
    log(f"=== {iid} phase 1/3: symbol-level-patch ===")
    symbol_subs = run_patcher_phase(
        "symbol-level-patch",
        "symbol",
        chain,
        base_graph_pkl,
        cache_root,
        out_root,
        timeout_s,
        lsp_profile_dir,
        log,
        "symbol-level-patch.pkl",
    )
    for step, sub in symbol_subs.items():
        step_recs[step]["symbol-level-patch"] = sub

    # Phase 2: cold-start rebuild per step (skip when no source diff).
    log(f"=== {iid} phase 2/3: fully-rebuild ===")
    prev_rb_pkl: Optional[Path] = None
    for t in chain:
        step_dir = out_root / iid / f"step{t['step']}"
        step_dir.mkdir(parents=True, exist_ok=True)
        sub = run_rebuild_step(t, cache_root, step_dir, prev_rb_pkl, timeout_s, log)
        step_recs[t["step"]]["fully-rebuild"] = sub
        rb_pkl = step_dir / "rebuild.pkl"
        if rb_pkl.exists():
            prev_rb_pkl = rb_pkl

    # Phase 3: file-level (naive-mode) patcher, with clangd cache cleared.
    log(f"=== {iid} phase 3/3: file-level-patch ===")
    clear_indexer_cache(repo, language)
    file_subs = run_patcher_phase(
        "file-level-patch",
        "naive",
        chain,
        base_graph_pkl,
        cache_root,
        out_root,
        timeout_s,
        lsp_profile_dir,
        log,
        "file-level-patch.pkl",
    )
    for step, sub in file_subs.items():
        step_recs[step]["file-level-patch"] = sub

    # Speedups + finished timestamp.
    out: List[Dict[str, Any]] = []
    for t in chain:
        rec = step_recs[t["step"]]
        rb = rec.get("fully-rebuild", {})
        fl = rec.get("file-level-patch", {})
        sl = rec.get("symbol-level-patch", {})
        tr = rb.get("t_s") if rb.get("status") == "ok" else None
        tf = fl.get("t_s") if fl.get("status") == "ok" else None
        ts = sl.get("t_s") if sl.get("status") == "ok" else None
        rec["speedup_vs_fully_rebuild"] = (tr / ts) if (tr and ts) else None
        rec["speedup_vs_file_level_patch"] = (tf / ts) if (tf and ts) else None
        rec["finished"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        out.append(rec)
    return out


# ---------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------


def parse_instances_inline(spec: str, cache_root: Path) -> List[Tuple[str, str, str]]:
    """Parse ``"iid:sha"`` or ``"iid:lang:sha"`` (comma-separated).

    Two-field form ``iid:sha``: language inferred from the cloned repo's
    file extensions. Three-field form ``iid:lang:sha`` skips inference.
    Drops entries with unknown language (with a warning).
    """
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        tokens = part.split(":")
        if len(tokens) == 3:
            iid, lang, sha = tokens[0].strip(), tokens[1].strip(), tokens[2].strip()
        elif len(tokens) == 2:
            iid, sha = tokens[0].strip(), tokens[1].strip()
            lang = _infer_language(cache_root / iid / "repo")
            if not lang:
                print(
                    f"warning: language inference failed for {iid!r}; "
                    f"skipping. Pass iid:lang:sha to disambiguate.",
                    file=sys.stderr,
                )
                continue
        else:
            print(
                f"warning: cannot parse instance spec {part!r}; "
                f'expected "iid:sha" or "iid:lang:sha"',
                file=sys.stderr,
            )
            continue
        out.append((iid, lang, sha))
    return out


def parse_instances_config(path: Path) -> List[Tuple[str, str, str]]:
    """Parse JSON: ``[{"instance_id":..., "language":..., "base_commit":...}, ...]``"""
    rows = json.loads(path.read_text())
    return [(r["instance_id"], r["language"], r["base_commit"]) for r in rows]


def _infer_language(repo: Path) -> Optional[str]:
    if not repo.is_dir():
        return None
    counts: Dict[str, int] = {lang: 0 for lang in LANG_EXTS}
    for f in repo.rglob("*"):
        if f.is_file():
            for lang, exts in LANG_EXTS.items():
                if f.suffix in exts:
                    counts[lang] += 1
                    break
        if sum(counts.values()) > 200:
            break
    if not any(counts.values()):
        return None
    return max(counts, key=counts.get)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--instances",
        default=None,
        help='Comma-separated "iid:base_sha" pairs. '
        "Overrides DEFAULT_INSTANCES and --config.",
    )
    ap.add_argument(
        "--config",
        type=Path,
        default=None,
        help="JSON file with [{instance_id, language, base_commit}, ...]. "
        "Overrides DEFAULT_INSTANCES.",
    )
    ap.add_argument(
        "--steps",
        type=int,
        default=DEFAULT_STEPS,
        help=f"steps per instance (default {DEFAULT_STEPS})",
    )
    ap.add_argument(
        "--max-walk",
        type=int,
        default=DEFAULT_MAX_WALK,
        help=f"max first-parent commits to consider per instance "
        f"(default {DEFAULT_MAX_WALK})",
    )
    ap.add_argument(
        "--max-delta-lines",
        type=int,
        default=DEFAULT_MAX_DELTA_LINES,
        help='delta_lines above this gets notes="too_large" '
        f"(default {DEFAULT_MAX_DELTA_LINES}); NOT skipped",
    )
    ap.add_argument(
        "--timeout-s",
        type=int,
        default=DEFAULT_TIMEOUT_S,
        help=f"per-strategy wall-clock cap, seconds (default " f"{DEFAULT_TIMEOUT_S})",
    )
    ap.add_argument(
        "--cache-root",
        type=Path,
        default=DEFAULT_CACHE_ROOT,
        help=f"where instance repos live (default {DEFAULT_CACHE_ROOT})",
    )
    ap.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"jsonl output path (default {DEFAULT_OUTPUT})",
    )
    ap.add_argument(
        "--lsp-profile-dir",
        type=Path,
        default=None,
        help="if set, capture per-call LSP profile jsonl per "
        "(iid, step, strategy) into this dir",
    )
    ap.add_argument(
        "--out-root",
        type=Path,
        default=None,
        help="dir for per-(iid, step) graph pkls (default: alongside output)",
    )
    ap.add_argument(
        "--resume",
        action="store_true",
        help="skip (iid, step) rows that already exist in --output",
    )
    args = ap.parse_args()

    # Resolve the instance list.
    if args.instances:
        instances = parse_instances_inline(args.instances, args.cache_root)
    elif args.config:
        instances = parse_instances_config(args.config)
    else:
        instances = list(DEFAULT_INSTANCES)
    if not instances:
        print("error: no instances to run", file=sys.stderr)
        sys.exit(2)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.out_root is None:
        args.out_root = args.output.parent / "chain_work"

    # Load already-done keys for --resume.
    done: set = set()
    if args.resume and args.output.exists():
        for line in args.output.open():
            try:
                r = json.loads(line)
            except Exception:
                continue
            iid = r.get("instance_id")
            step = r.get("step")
            if iid and step is not None:
                done.add((iid, step))

    def log(msg: str) -> None:
        print(msg, flush=True)

    log(
        f"instances={len(instances)} steps={args.steps} "
        f"timeout_s={args.timeout_s} output={args.output}"
    )

    fh = args.output.open("a")
    try:
        for iid, language, base_sha in instances:
            # Whole-instance short-circuit: if every step is already in the
            # output file, skip the entire chain so we don't re-walk it just
            # to throw away the rows at write-time.
            if args.resume and all((iid, s) in done for s in range(1, args.steps + 1)):
                log(f"[{iid}] all {args.steps} steps already in output, skipping")
                continue
            rows = process_instance(
                iid,
                language,
                base_sha,
                args.cache_root,
                args.steps,
                args.max_walk,
                args.max_delta_lines,
                args.timeout_s,
                args.out_root,
                args.lsp_profile_dir,
                log,
            )
            for r in rows:
                key = (r.get("instance_id"), r.get("step"))
                if key in done:
                    log(f"[{iid}] step{r.get('step')} resume-skip (already in output)")
                    continue
                fh.write(json.dumps(r) + "\n")
                fh.flush()
    finally:
        fh.close()
    log(f"done. wrote {args.output}")


if __name__ == "__main__":
    main()
