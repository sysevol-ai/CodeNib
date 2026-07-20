#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Benchmark graph-update strategies along real repository commit chains.

For each instance repo, walk its first-parent commit chain ``N`` steps and, at
every step, run all three strategies independently and record per-step timing:

  1. ``fully-rebuild`` — rebuild the graph through the same LSP provider used
     by the incremental arms (the active SCIP/clangd route remains available
     as an explicit backend comparison).
  2. ``file-level-patch`` — the patcher in naive (file-granularity) mode:
     delete + rebuild every vertex/edge of each modified file, no symbol diff.
  3. ``symbol-level-patch`` — the production incremental patcher
     (``codeminer.graph.incremental.patcher_base``): rewire edges by changed
     lines + affected symbols.

The file- and symbol-level strategies each use one long-lived LSP across the
chain. Their order is deterministically counterbalanced across repositories,
and each phase starts from the same base graph and checkout. C/C++ has a
separate ``backend-incremental`` arm because its ``.idx`` patcher does not
implement the file/symbol mode distinction.

Output: one JSONL row per ``(instance, step)`` carrying each strategy's
``t_s``/``v``/``e``/``status``, the patcher stats, an LSP call summary, and the
derived ``speedup_vs_fully_rebuild`` / ``speedup_vs_file_level_patch`` ratios.
Incremental ``t_s`` covers change detection, graph repair, and persistence;
LSP startup is reported separately and amortized across the chain. Every
successful patched graph is compared with a fresh rebuild at the same commit,
both globally and on facts touching changed files.
``notes`` flags commits with no source diff (``"no-source-change"``) or
super-large diffs (``"too_large"``); both are kept (not skipped) because they
reflect real-world commit traffic.

The heavy ``codeminer.*`` imports (chunker, graph, patcher, indexer) are loaded
lazily inside the functions that need them so the pure string/parse helpers can
be imported and unit-tested without a full dev environment.

Usage::

    # Use the hardcoded DEFAULT_INSTANCES list (5 steps each):
    python scripts/profiling/profile_incremental_graph.py --steps 5

    # Pass instances inline ("iid:base_sha" or "iid:lang:base_sha"):
    python scripts/profiling/profile_incremental_graph.py \\
        --instances "gin-gonic__gin-1805:go:70a0aba,pydata__xarray-3151:python:118f4d9" \\
        --steps 5

    # Or from a JSON file:
    python scripts/profiling/profile_incremental_graph.py --config instances.json --steps 5
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = HERE.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
# Allow the C++ pybind decoder to load when present (faster SCIP decode).
_CORE = _PROJECT_ROOT / "build" / "core"
if _CORE.exists() and str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

from codeminer.eval.maintenance_manifest import build_manifest  # noqa: E402
from codeminer.eval.maintenance_manifest import load_manifest, write_manifest

# ---------------------------------------------------------------------------
# Lazy importers — keep module import light so the pure helpers (and their
# unit tests) don't drag in litellm / faiss / torch / tree-sitter.
# ---------------------------------------------------------------------------


def _CodeChunker():
    from codeminer.code_chunker import CodeChunker

    return CodeChunker


def _CodeGraph():
    from codeminer.graph.code_graph import CodeGraph

    return CodeGraph


def _graph_schema_version() -> int:
    from codeminer.graph.code_graph import _SCHEMA_VERSION

    return _SCHEMA_VERSION


def _GraphPatcher():
    from codeminer.graph.incremental.graph_patcher import GraphPatcher

    return GraphPatcher


def _LSIndexer():
    from codeminer.ls_router import LSIndexer

    return LSIndexer


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_CACHE_ROOT = Path("/mnt/data/codeminer")
DEFAULT_STEPS = 5
DEFAULT_TIMEOUT_S = 600
DEFAULT_MAX_DELTA_LINES = 5000
DEFAULT_MAX_WALK = 200
DEFAULT_BEHAVIOR_REQUESTS_PER_CAPABILITY = 256
DEFAULT_OUTPUT = Path("profile_incremental.jsonl")
PROTOCOL_VERSION = 22
# Base artifacts depend only on full-oracle construction semantics. Keep this
# separate from the benchmark protocol so patcher/statistics changes do not
# invalidate expensive, otherwise identical cold-build graphs.
BASE_BUILD_PROTOCOL_VERSION = 17
DEFAULT_REFERENCE_TIMEOUT_S = 120.0
DEFAULT_REFERENCE_RETRIES = 1
DEFAULT_LSP_REQUEST_CONCURRENCY = 10
DEFAULT_STRATEGY_ATTEMPTS = 2
DEFAULT_LOG_LEVEL = "WARNING"


def experiment_config_id(
    *,
    graph_route: str,
    graph_schema_version: int,
    timeout_s: int,
    reference_timeout_s: float,
    reference_retries: int,
    lsp_request_concurrency: int,
    strategy_attempts: int,
    runtime_log_level: str = DEFAULT_LOG_LEVEL,
) -> str:
    """Fingerprint settings that change graph execution or admission."""

    payload = {
        "graph_route": graph_route,
        "graph_schema_version": graph_schema_version,
        "timeout_s": timeout_s,
        "reference_timeout_s": reference_timeout_s,
        "reference_retries": reference_retries,
        "lsp_request_concurrency": lsp_request_concurrency,
        "strategy_attempts": strategy_attempts,
        "runtime_log_level": runtime_log_level,
        "behavior_requests_per_capability": DEFAULT_BEHAVIOR_REQUESTS_PER_CAPABILITY,
        "base_build_protocol_version": BASE_BUILD_PROTOCOL_VERSION,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# Hardcoded fallback instance list. Override with --instances or --config.
# Each tuple is (instance_id, language, base_commit). Start at 5 steps; the
# issue expands to 10 once the first results are in.
DEFAULT_INSTANCES: List[Tuple[str, str, str]] = [
    # C/C++
    ("redis__redis-10068", "cpp", "e88f6acb94c77c9a5b81f0b2a8bd132b2a5c3d3c"),
    ("jqlang__jq-2235", "cpp", "b8816caf0a7f58c6483f4344e222a6fea47732e8"),
    # Go
    ("caddyserver__caddy-4943", "go", "7f6a328b4744bbdd5ee07c5eccb3f897390ff982"),
    ("gin-gonic__gin-1805", "go", "70a0aba3e423246be37462cfdaedd510c26c566e"),
    # Python
    ("pydata__xarray-3151", "python", "118f4d996e7711c9aced916e6049af9f28d5ec66"),
    (
        "scikit-learn__scikit-learn-10297",
        "python",
        "b90661d6a46aa3619d3eec94d5281f5888add501",
    ),
    # Rust
    ("tokio-rs__tokio-4898", "rust", "9d9488db67136651f85d839efcb5f61aba8531c9"),
    ("astral-sh__ruff-15309", "rust", "75a24bbc67aa31b825b6326cfb6e6afdf3ca90d5"),
    # TS/JS
    ("preactjs__preact-2757", "ts", "b17a932342bfdeeaf1dc0fbe4f436c83e258d6c8"),
    ("axios__axios-4731", "ts", "c30252f685e8f4326722de84923fcbc8cf557f06"),
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
    """True when ``path`` looks like a test/fixture file the symbol-diff
    should exclude (test churn isn't representative of source-graph load)."""
    p = path.lower()
    parts = p.split("/")
    base = parts[-1] if parts else ""
    for part in parts:
        if part in _TEST_TOKENS:
            return True
        if part.startswith("test_"):
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
    # Preserve LSP/indexer caches AND compile-database artifacts across steps.
    # Without these excludes ``git clean -fd`` wipes .cache/clangd/index,
    # target/rust-analyzer, node_modules, compile_commands.json / build.
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
    """SHAs strictly after ``base`` on the first-parent path, oldest-first."""
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
            # Best-effort shortstat parse: skip a malformed field rather than
            # failing the whole diff.
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
    chunker = _CodeChunker()(language=language, chunk_depth=2)
    added = removed = modified = 0
    with tempfile.TemporaryDirectory(prefix="chainbench_") as tmpdir:
        tmp = Path(tmpdir)
        tmp_resolved = tmp.resolve()
        for rel_path in changed_files:
            base_file = tmp / "base" / rel_path
            tgt_file = tmp / "target" / rel_path
            # Defensive: a malformed diff entry with ``..`` segments could let
            # ``tmp / rel_path`` escape the temp dir. Drop anything that
            # doesn't resolve under tmp.
            try:
                base_file.resolve().relative_to(tmp_resolved)
                tgt_file.resolve().relative_to(tmp_resolved)
            except ValueError:
                continue
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
                # Cache wipe is best-effort: a permission/in-use error just
                # warm-starts the rebuild (inflates t_rebuild), not a
                # correctness issue.
                pass


# ---------------------------------------------------------------------------
# SIGALRM per-strategy timeout
# ---------------------------------------------------------------------------


class StrategyTimeout(TimeoutError):
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


def _summarize_lsp_profile(path: Optional[Path]) -> Dict[str, Any]:
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


def _fact_overlap_metrics(predicted, reference) -> Dict[str, Any]:
    """Multiset precision/recall/F1 with explicit empty-set semantics."""
    predicted_counts = Counter(predicted)
    reference_counts = Counter(reference)
    predicted_n = sum(predicted_counts.values())
    reference_n = sum(reference_counts.values())
    overlap = sum((predicted_counts & reference_counts).values())
    if not predicted_n and not reference_n:
        precision = recall = f1 = 1.0
    else:
        precision = overlap / predicted_n if predicted_n else 0.0
        recall = overlap / reference_n if reference_n else 0.0
        f1 = (
            2.0 * precision * recall / (precision + recall)
            if precision + recall
            else 0.0
        )
    return {
        "predicted": predicted_n,
        "reference": reference_n,
        "overlap": overlap,
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


def _vertex_file(vertex) -> Optional[str]:
    from codeminer.types import NODE_TYPE_FILE

    attrs = vertex.attributes()
    file_path = attrs.get("file")
    if file_path:
        return str(file_path)
    if attrs.get("type") == NODE_TYPE_FILE:
        return str(vertex["name"])
    return None


_SCIP_COMMIT_VERSION_RE = re.compile(r"^(scip-\S+\s+\S+\s+\S+)\s+[0-9a-f]{40}(?=\s|$)")


def _normalize_semantic_identity(value: Any, *, source_mapped: bool) -> Any:
    """Canonicalize commit-valued package metadata on external SCIP stubs."""

    if source_mapped or not isinstance(value, str):
        return value
    return _SCIP_COMMIT_VERSION_RE.sub(r"\1 <commit>", value)


def _vertex_identity(vertex) -> tuple:
    """Stable semantic key that ignores igraph ids and internal vertex names."""
    attrs = vertex.attributes()
    node_type = str(attrs.get("type") or "")
    unified_name = attrs.get("unified_name")
    label_kind = "unified" if unified_name else "name"
    label = unified_name or vertex["name"]
    source_mapped = any(
        attrs.get(field) is not None
        for field in ("start_line", "end_line", "selection_line")
    )
    return (
        node_type,
        label_kind,
        _normalize_semantic_identity(str(label), source_mapped=source_mapped),
        _normalize_semantic_identity(_vertex_file(vertex), source_mapped=source_mapped),
        attrs.get("start_line"),
        attrs.get("end_line"),
        attrs.get("selection_line"),
        attrs.get("selection_character"),
    )


def _graph_fact_sets(code_graph, changed_files: set[str]) -> Dict[str, Counter]:
    """Normalize a graph into whole-graph and changed-slice fact multisets."""
    from codeminer.types import EDGE_TYPE_REFERENCE

    vertices = code_graph.graph.vs
    vertex_keys = [_vertex_identity(vertex) for vertex in vertices]
    vertex_files = [_vertex_file(vertex) for vertex in vertices]

    facts: Dict[str, Counter] = {
        "vertices": Counter(vertex_keys),
        "edges": Counter(),
        "reference_edges": Counter(),
        "changed_vertices": Counter(),
        "changed_edges": Counter(),
        "changed_reference_edges": Counter(),
    }
    for key, file_path in zip(vertex_keys, vertex_files, strict=False):
        if file_path in changed_files:
            facts["changed_vertices"][key] += 1

    for edge in code_graph.graph.es:
        attrs = edge.attributes()
        edge_type = str(attrs.get("type") or "")
        anchor_file = attrs.get("anchor_file")
        anchor_line = attrs.get("anchor_line")
        fact = (
            edge_type,
            vertex_keys[edge.source],
            vertex_keys[edge.target],
            str(anchor_file) if anchor_file is not None else None,
            int(anchor_line) if anchor_line is not None else None,
        )
        facts["edges"][fact] += 1
        if edge_type == EDGE_TYPE_REFERENCE:
            facts["reference_edges"][fact] += 1

        changed = (
            vertex_files[edge.source] in changed_files
            or vertex_files[edge.target] in changed_files
            or anchor_file in changed_files
        )
        if changed:
            facts["changed_edges"][fact] += 1
            if edge_type == EDGE_TYPE_REFERENCE:
                facts["changed_reference_edges"][fact] += 1
    return facts


def compare_graphs(
    predicted_graph,
    reference_graph,
    changed_files: set[str],
) -> Dict[str, Any]:
    """Compare a patched graph with a fresh rebuild at the same commit."""
    predicted = _graph_fact_sets(predicted_graph, changed_files)
    reference = _graph_fact_sets(reference_graph, changed_files)
    return {
        name: _fact_overlap_metrics(predicted[name], reference[name])
        for name in predicted
    }


def compare_graph_behavior(
    predicted_graph,
    reference_graph,
    *,
    project_root: Path,
    changed_files: set[str],
    max_per_capability: int = DEFAULT_BEHAVIOR_REQUESTS_PER_CAPABILITY,
) -> Dict[str, Any]:
    """Replay source-location LSP requests against two static graph providers."""
    from codeminer.agent.lsp_provider import (
        CAPABILITY_DEFINITION,
        CAPABILITY_REFERENCES,
        StaticLSPProvider,
    )
    from codeminer.eval.agent_runner.lsp_provider_validation import (
        compare_static_lsp_provider,
        default_lsp_provider_fingerprint,
    )
    from codeminer.eval.agent_runner.lsp_replay_benchmark import (
        generate_lsp_replay_requests,
    )

    requests = generate_lsp_replay_requests(
        reference_graph,
        project_root=project_root,
        capabilities=(CAPABILITY_DEFINITION, CAPABILITY_REFERENCES),
        max_per_capability=max_per_capability,
        file_paths=sorted(changed_files),
    )
    if not requests:
        return {
            "status": "not_evaluable",
            "contract": "static_lsp_path_start_line_v1",
            "request_count": 0,
            "equivalent_count": 0,
            "equivalent_fraction": None,
            "exact": False,
            "by_capability": {},
            "mismatch_examples": [],
        }

    predicted_provider = StaticLSPProvider(predicted_graph)
    reference_provider = StaticLSPProvider(reference_graph)

    def invoke_reference(capability: str, arguments: Dict[str, Any]):
        if capability == CAPABILITY_DEFINITION:
            return reference_provider.definition(**arguments)
        if capability == CAPABILITY_REFERENCES:
            return reference_provider.references(**arguments)
        raise ValueError(f"unsupported behavior request: {capability!r}")

    comparisons = compare_static_lsp_provider(
        requests,
        graph=predicted_graph,
        static_provider=predicted_provider,
        reference_provider=invoke_reference,
        reference_provider_name="fresh_rebuild_static",
        fingerprint_selector=default_lsp_provider_fingerprint,
    )
    by_capability: Dict[str, Dict[str, int]] = {}
    mismatch_examples = []
    equivalent_count = 0
    error_count = 0
    for comparison in comparisons:
        capability = comparison.request.normalized_capability
        group = by_capability.setdefault(
            capability,
            {"requests": 0, "equivalent": 0, "mismatched": 0, "errors": 0},
        )
        group["requests"] += 1
        if comparison.same_result is True:
            equivalent_count += 1
            group["equivalent"] += 1
        elif comparison.same_result is False:
            group["mismatched"] += 1
            if len(mismatch_examples) < 5:
                mismatch_examples.append(comparison.request.request_id)
        else:
            error_count += 1
            group["errors"] += 1

    total = len(comparisons)
    return {
        "status": "ok" if not error_count else "error",
        "contract": "static_lsp_path_start_line_v1",
        "request_count": total,
        "equivalent_count": equivalent_count,
        "equivalent_fraction": equivalent_count / total,
        "exact": equivalent_count == total,
        "error_count": error_count,
        "by_capability": by_capability,
        "mismatch_examples": mismatch_examples,
    }


def _changed_paths(changed: Dict[str, Any]) -> set[str]:
    paths = set(changed.get("modified", []))
    paths.update(changed.get("added", []))
    paths.update(changed.get("deleted", []))
    for old_path, new_path in changed.get("renamed", []):
        paths.add(old_path)
        paths.add(new_path)
    return paths


def patcher_phase_order(instance_id: str) -> tuple[str, str]:
    """Counterbalance warm-cache order deterministically across instances."""
    parity = hashlib.sha256(instance_id.encode("utf-8")).digest()[0] & 1
    if parity:
        return ("file-level-patch", "symbol-level-patch")
    return ("symbol-level-patch", "file-level-patch")


# ---------------------------------------------------------------------------
# Strategy 1: full rebuild at one commit
# ---------------------------------------------------------------------------


def _do_rebuild(
    language: str,
    repo: Path,
    target_sha: str,
    out_dir: Path,
    graph_route: str = "lsp",
    reference_timeout_s: float = DEFAULT_REFERENCE_TIMEOUT_S,
    reference_retries: int = DEFAULT_REFERENCE_RETRIES,
    lsp_request_concurrency: int = DEFAULT_LSP_REQUEST_CONCURRENCY,
) -> Dict[str, Any]:
    """Cold-start full graph rebuild at ``target_sha``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for f in ("index.scip", "index.decoded", "index.lsp.json", "graph.pkl"):
        (out_dir / f).unlink(missing_ok=True)

    checkout(repo, target_sha)
    indexer = _LSIndexer()(
        project_root=repo,
        output_dir=out_dir,
        language=language,
        graph_route=graph_route,
    )
    t0 = time.monotonic()
    pipeline_kwargs = (
        {
            "include_references": True,
            "reference_timeout_s": reference_timeout_s,
            "request_timeout_s": reference_timeout_s,
            "reference_retries": reference_retries,
            "reference_concurrency": lsp_request_concurrency,
            "strict_references": True,
        }
        if graph_route == "lsp"
        else {}
    )
    g = indexer.run_pipeline(
        output_file=str(out_dir / "graph.pkl"),
        skip_level=None,
        reset_profiler=True,
        report_profile=False,
        **pipeline_kwargs,
    )
    elapsed = time.monotonic() - t0
    if g is None:
        raise RuntimeError("rebuild returned no graph")
    return {
        "t_s": elapsed,
        "v": g.graph.vcount(),
        "e": g.graph.ecount(),
        "status": "ok",
        "graph_route": graph_route,
        "graph_schema_version": _graph_schema_version(),
        "reference_timeout_s": reference_timeout_s,
        "request_timeout_s": reference_timeout_s,
        "reference_retries": reference_retries,
        "lsp_request_concurrency": lsp_request_concurrency,
    }


def prepare_base_graph(
    instance_id: str,
    language: str,
    base_commit: str,
    cache_root: Path,
    out_root: Path,
    timeout_s: int,
    log,
    *,
    graph_route: str = "lsp",
    reference_timeout_s: float = DEFAULT_REFERENCE_TIMEOUT_S,
    reference_retries: int = DEFAULT_REFERENCE_RETRIES,
    lsp_request_concurrency: int = DEFAULT_LSP_REQUEST_CONCURRENCY,
    strategy_attempts: int = DEFAULT_STRATEGY_ATTEMPTS,
    force: bool = False,
) -> Path:
    """Build or reuse a schema-valid graph tied to the requested base commit."""
    base_dir = out_root / instance_id / "base"
    graph_path = base_dir / "graph.pkl"
    metadata_path = base_dir / "meta.json"

    if not force and graph_path.is_file() and metadata_path.is_file():
        try:
            metadata = json.loads(metadata_path.read_text())
            if (
                metadata.get("instance_id") == instance_id
                and metadata.get("language") == language
                and metadata.get("base_commit") == base_commit
                and metadata.get("graph_route") == graph_route
                and metadata.get(
                    "base_build_protocol_version",
                    metadata.get("protocol_version"),
                )
                == BASE_BUILD_PROTOCOL_VERSION
                and metadata.get("reference_timeout_s") == reference_timeout_s
                and metadata.get("request_timeout_s") == reference_timeout_s
                and metadata.get("reference_retries") == reference_retries
                and metadata.get(
                    "lsp_request_concurrency",
                    metadata.get("full_reference_concurrency"),
                )
                == lsp_request_concurrency
            ):
                graph = _CodeGraph().load_graph(str(graph_path))
                log(
                    f"[{instance_id}] reuse validated base graph "
                    f"V={graph.graph.vcount()} E={graph.graph.ecount()}"
                )
                return graph_path
        except Exception as exc:
            log(
                f"[{instance_id}] base graph cache rejected: {type(exc).__name__}: {exc}"
            )

    repo = cache_root / instance_id / "repo"
    if not repo.is_dir():
        raise FileNotFoundError(repo)
    base_dir.mkdir(parents=True, exist_ok=True)
    log(f"[{instance_id}] build base graph at {base_commit[:12]}")
    attempt_failures = []
    recovery_s = 0.0
    info = None
    for attempt in range(1, strategy_attempts + 1):
        attempt_t0 = time.monotonic()
        try:
            info = run_with_timeout(
                timeout_s,
                _do_rebuild,
                language,
                repo,
                base_commit,
                base_dir,
                graph_route,
                reference_timeout_s,
                reference_retries,
                lsp_request_concurrency,
            )
            break
        except Exception as exc:
            elapsed = time.monotonic() - attempt_t0
            attempt_failures.append(
                {
                    "attempt": attempt,
                    "status": (
                        "timeout" if isinstance(exc, StrategyTimeout) else "error"
                    ),
                    "error": f"{type(exc).__name__}: {exc}",
                    "wall_s": elapsed,
                }
            )
            if attempt >= strategy_attempts:
                if isinstance(exc, StrategyTimeout):
                    raise TimeoutError(
                        f"base graph exceeded {timeout_s}s on "
                        f"{strategy_attempts} attempts"
                    ) from exc
                raise
            recovery_s += elapsed
            log(
                f"[{instance_id}] base build attempt {attempt}/"
                f"{strategy_attempts} failed ({type(exc).__name__}: {exc}); "
                "restarting from a clean graph"
            )
    if info is None:
        raise RuntimeError("base graph build produced no result")
    metadata_path.write_text(
        json.dumps(
            {
                "protocol_version": PROTOCOL_VERSION,
                "base_build_protocol_version": BASE_BUILD_PROTOCOL_VERSION,
                "instance_id": instance_id,
                "language": language,
                "base_commit": base_commit,
                "graph_route": graph_route,
                "graph_schema_version": _graph_schema_version(),
                "reference_timeout_s": reference_timeout_s,
                "request_timeout_s": reference_timeout_s,
                "reference_retries": reference_retries,
                "lsp_request_concurrency": lsp_request_concurrency,
                "strategy_attempt_limit": strategy_attempts,
                "build_attempts": len(attempt_failures) + 1,
                "recovery_s": recovery_s,
                "prior_attempt_failures": attempt_failures,
                "build_t_s": info["t_s"],
                "vertices": info["v"],
                "edges": info["e"],
                "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
            indent=2,
        )
    )
    return graph_path


def attach_graph_fidelity(
    record: Dict[str, Any],
    out_root: Path,
    strategies: Sequence[str],
    project_root: Path,
    target_commit: str,
) -> None:
    """Compare successful incremental outputs with the same-commit rebuild."""
    rebuild = record.get("fully-rebuild", {})
    if rebuild.get("status") != "ok":
        return

    try:
        checkout(project_root, target_commit)
    except Exception as exc:
        error = f"checkout_target: {type(exc).__name__}: {exc}"
        for strategy in strategies:
            sub = record.get(strategy, {})
            if sub.get("status") == "ok":
                sub["fidelity"] = {"status": "error", "error": error}
        return

    step_dir = out_root / record["instance_id"] / f"step{record['step']}"
    reference_path = step_dir / "rebuild.pkl"
    try:
        reference_graph = _CodeGraph().load_graph(str(reference_path))
    except Exception as exc:
        rebuild["fidelity_reference_error"] = f"{type(exc).__name__}: {exc}"
        return

    for strategy in strategies:
        sub = record.get(strategy, {})
        if sub.get("status") != "ok":
            continue
        predicted_path = step_dir / f"{strategy}.pkl"
        try:
            predicted_graph = _CodeGraph().load_graph(str(predicted_path))
            metrics = compare_graphs(
                predicted_graph,
                reference_graph,
                set(sub.get("changed_files", [])),
            )
            raw_graph_whole_exact = all(
                metrics[name]["precision"] == 1.0 and metrics[name]["recall"] == 1.0
                for name in ("vertices", "edges")
            )
            raw_graph_changed_exact = all(
                metrics[name]["precision"] == 1.0 and metrics[name]["recall"] == 1.0
                for name in ("changed_vertices", "changed_edges")
            )
            changed_files = set(sub.get("changed_files", []))
            behavior = compare_graph_behavior(
                predicted_graph,
                reference_graph,
                project_root=project_root,
                changed_files=changed_files,
            )
            behavior_exact = behavior.get("exact") is True
            if not changed_files and behavior.get("status") == "not_evaluable":
                behavior = {
                    **behavior,
                    "status": "skipped_no_source",
                    "reason": "no_changed_source_files",
                    "exact": True,
                }
                behavior_exact = True
            sub["fidelity"] = {
                "status": "ok",
                "behavior_exact": behavior_exact,
                "equivalence_guardrail_pass": (
                    behavior_exact and raw_graph_whole_exact and raw_graph_changed_exact
                ),
                "behavior": behavior,
                "raw_graph": {
                    "whole_exact": raw_graph_whole_exact,
                    "changed_exact": raw_graph_changed_exact,
                    "metrics": metrics,
                },
            }
        except Exception as exc:
            sub["fidelity"] = {
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }


def run_rebuild_step(
    target: Dict[str, Any],
    cache_root: Path,
    step_out_dir: Path,
    prev_rebuild_pkl: Optional[Path],
    timeout_s: int,
    log,
    *,
    graph_route: str = "lsp",
    reference_timeout_s: float = DEFAULT_REFERENCE_TIMEOUT_S,
    reference_retries: int = DEFAULT_REFERENCE_RETRIES,
    lsp_request_concurrency: int = DEFAULT_LSP_REQUEST_CONCURRENCY,
    strategy_attempts: int = DEFAULT_STRATEGY_ATTEMPTS,
) -> Dict[str, Any]:
    """Run a rebuild for one step. When the commit touched 0 source files,
    skip the cold rebuild and reuse the previous step's rebuild graph."""
    iid = target["instance_id"]
    lang = target["language"]
    repo = cache_root / iid / "repo"
    rebuild_pkl = step_out_dir / "rebuild.pkl"

    is_no_source = target.get("delta_files", 0) == 0
    if is_no_source and prev_rebuild_pkl is not None and prev_rebuild_pkl.exists():
        log(f"  rebuild step{target['step']} SKIPPED (no source diff)")
        try:
            shutil.copyfile(str(prev_rebuild_pkl), str(rebuild_pkl))
            g = _CodeGraph().load_graph(str(rebuild_pkl))
            return {
                "t_s": 0.0,
                "v": g.graph.vcount(),
                "e": g.graph.ecount(),
                "status": "ok",
                "skipped_no_source": True,
                "strategy_attempts": 0,
                "strategy_attempt_limit": strategy_attempts,
            }
        except Exception as e:
            return {
                "status": "error",
                "error": f"reuse-prev failed: {type(e).__name__}: {e}",
            }

    attempt_failures = []
    recovery_s = 0.0
    for attempt in range(1, strategy_attempts + 1):
        log(
            f"  rebuild step{target['step']} {target['to_commit'][:8]} "
            f"(cold start, attempt {attempt}/{strategy_attempts})"
        )
        attempt_t0 = time.monotonic()
        try:
            clear_indexer_cache(repo, lang)
            info = run_with_timeout(
                timeout_s,
                _do_rebuild,
                lang,
                repo,
                target["to_commit"],
                step_out_dir,
                graph_route,
                reference_timeout_s,
                reference_retries,
                lsp_request_concurrency,
            )
            src = step_out_dir / "graph.pkl"
            if src.exists() and src != rebuild_pkl:
                shutil.move(str(src), str(rebuild_pkl))
            info.update(
                {
                    "strategy_attempts": attempt,
                    "strategy_attempt_limit": strategy_attempts,
                    "recovery_s": recovery_s,
                    "prior_attempt_failures": attempt_failures,
                    "fault_inclusive_t_s": info["t_s"] + recovery_s,
                }
            )
            return info
        except Exception as exc:
            elapsed = time.monotonic() - attempt_t0
            status = "timeout" if isinstance(exc, StrategyTimeout) else "error"
            attempt_failures.append(
                {
                    "attempt": attempt,
                    "status": status,
                    "error": f"{type(exc).__name__}: {exc}",
                    "wall_s": elapsed,
                }
            )
            if attempt < strategy_attempts:
                recovery_s += elapsed
                log(
                    f"  rebuild step{target['step']} attempt {attempt}/"
                    f"{strategy_attempts} failed ({type(exc).__name__}: {exc}); "
                    "restarting"
                )
                continue
            return {
                "status": status,
                "t_s": elapsed if status == "timeout" else None,
                "error": f"{type(exc).__name__}: {exc}",
                "strategy_attempts": attempt,
                "strategy_attempt_limit": strategy_attempts,
                "recovery_s": recovery_s,
                "prior_attempt_failures": attempt_failures[:-1],
            }
    raise AssertionError("unreachable rebuild attempt loop")


# ---------------------------------------------------------------------------
# Incremental patcher phase (one long-lived backend, walk the whole chain)
# ---------------------------------------------------------------------------


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
    *,
    graph_route: str = "lsp",
    reference_timeout_s: float = DEFAULT_REFERENCE_TIMEOUT_S,
    reference_retries: int = DEFAULT_REFERENCE_RETRIES,
    lsp_request_concurrency: int = DEFAULT_LSP_REQUEST_CONCURRENCY,
) -> Dict[int, Dict[str, Any]]:
    """Run the ``mode`` patcher across the chain with a single long-lived LSP.

    The graph mutates in memory across steps, so each step inherits the prior
    step's output without pickle round-trips. Returns ``{step -> sub-record}``.
    """
    iid = chain[0]["instance_id"]
    lang = chain[0]["language"]
    repo = cache_root / iid / "repo"
    sub_records: Dict[int, Dict[str, Any]] = {}

    try:
        g = _CodeGraph().load_graph(str(base_graph_pkl))
    except Exception as e:
        for t in chain:
            sub_records[t["step"]] = {"status": "error", "error": f"load_base: {e}"}
        return sub_records

    try:
        checkout(repo, chain[0]["from_commit"])
    except Exception as e:
        for t in chain:
            sub_records[t["step"]] = {
                "status": "error",
                "error": f"checkout_base: {type(e).__name__}: {e}",
            }
        return sub_records

    try:
        patcher = _GraphPatcher()(
            project_root=str(repo),
            code_graph=g,
            language=lang,
            mode=mode,
            respect_backend_exclusions=graph_route != "lsp",
            reference_timeout_s=reference_timeout_s,
            reference_retries=reference_retries,
            reference_concurrency=lsp_request_concurrency,
            strict_lsp_requests=True,
        )
    except Exception as exc:
        log(f"  {strategy}: construct failed: {type(exc).__name__}: {exc}")
        for t in chain:
            sub_records[t["step"]] = {
                "status": "error",
                "error": f"construct: {type(exc).__name__}: {exc}",
            }
        return sub_records

    uses_lsp = lang != "cpp"
    server_start_s = 0.0
    workspace_warmup_s = 0.0
    workspace_warmup_files = 0
    workspace_initial_idle = None
    workspace_idle = None
    setup_attempts = 0
    cold_start_total = 0.0
    if uses_lsp:
        setup_error: Exception | None = None
        for attempt in range(1, 3):
            setup_attempts = attempt
            start_t0 = time.monotonic()
            start_recorded = False
            attempt_warmup_s = 0.0
            try:
                patcher.start_lsp()
                server_start_s += time.monotonic() - start_t0
                start_recorded = True
                if graph_route == "lsp":
                    warmup_t0 = time.monotonic()
                    try:
                        workspace_warmup_files = patcher.warmup_workspace()
                    finally:
                        attempt_warmup_s = time.monotonic() - warmup_t0
                        workspace_warmup_s += attempt_warmup_s
                    workspace_initial_idle = patcher.workspace_initial_idle
                    workspace_idle = patcher.workspace_warmup_idle
                    if workspace_initial_idle is not True or workspace_idle is not True:
                        raise TimeoutError(
                            "LSP workspace did not become idle after warmup"
                        )
                log(
                    f"  {strategy}: workspace warmup "
                    f"{workspace_warmup_files} files in "
                    f"{attempt_warmup_s:.1f}s (attempt {attempt}/2)"
                )
                setup_error = None
                break
            except Exception as exc:
                if not start_recorded:
                    server_start_s += max(time.monotonic() - start_t0, 0.0)
                setup_error = exc
                try:
                    patcher.stop_lsp()
                except Exception:
                    pass
                if attempt < 2:
                    log(
                        f"  {strategy}: backend setup attempt {attempt}/2 "
                        f"failed ({type(exc).__name__}: {exc}); restarting"
                    )
        if setup_error is not None:
            log(
                f"  {strategy}: backend start failed after {setup_attempts} "
                f"attempts: {type(setup_error).__name__}: {setup_error}"
            )
            for t in chain:
                sub_records[t["step"]] = {
                    "status": "error",
                    "error": f"start_lsp: {setup_error}",
                }
            return sub_records
        cold_start_total = server_start_s + workspace_warmup_s

    backend_setup = {
        "server_start_s": server_start_s,
        "workspace_warmup_s": workspace_warmup_s,
        "workspace_warmup_files": workspace_warmup_files,
        "workspace_initial_idle": workspace_initial_idle,
        "workspace_idle": workspace_idle,
        "setup_attempts": setup_attempts,
        "phase_cold_start_s": cold_start_total,
    }

    log(
        f"  {strategy}: backend setup {cold_start_total:.1f}s "
        f"(start {server_start_s:.1f}s + warmup {workspace_warmup_s:.1f}s), "
        f"running {len(chain)} steps"
    )

    # ``poisoned`` flips True after any timeout/exception inside
    # ``patch_files``. The in-memory graph mutates in place and the LSP
    # JSON-RPC socket can be left in an undefined state after a SIGALRM
    # interrupt, so measurements past that point are unreliable. Remaining
    # steps are recorded as ``aborted_prior_step_poisoned`` instead of
    # silently producing bad t_s / v / e numbers.
    poisoned = False
    poisoned_at: Optional[int] = None

    try:
        for i, t in enumerate(chain):
            step = t["step"]
            step_dir = out_root / iid / f"step{step}"
            step_dir.mkdir(parents=True, exist_ok=True)
            out_pkl = step_dir / out_filename

            if poisoned:
                sub_records[step] = {
                    "status": "aborted_prior_step_poisoned",
                    "poisoned_at_step": poisoned_at,
                }
                continue

            # Per-step LSP profile capture.
            profile_path: Optional[Path] = None
            if lsp_profile_dir is not None:
                lsp_profile_dir.mkdir(parents=True, exist_ok=True)
                profile_path = lsp_profile_dir / f"{iid}_step{step}_{strategy}.jsonl"
                profile_path.unlink(missing_ok=True)
            # Backend startup is outside the per-step profile. All requests
            # triggered by this update remain inside both the profile and t_s.
            os.environ.pop("LSP_PROFILE_PATH", None)

            try:
                checkout(repo, t["to_commit"])
            except Exception as e:
                poisoned = True
                poisoned_at = step
                sub_records[step] = {
                    "status": "error",
                    "error": f"checkout: {type(e).__name__}: {e}",
                }
                os.environ.pop("LSP_PROFILE_PATH", None)
                continue

            if profile_path is not None:
                os.environ["LSP_PROFILE_PATH"] = str(profile_path)

            log(
                f"  {strategy} step{step} {t['to_commit'][:8]} "
                f"L{t['delta_lines']} F{t['delta_files']} "
                f"S{t['delta_symbols']}"
            )
            total_t0 = time.monotonic()
            try:
                detect_t0 = time.monotonic()
                changed = patcher.detect_changed_files(
                    str(repo),
                    t["from_commit"],
                    t["to_commit"],
                    extensions=LANG_EXTS.get(lang),
                )
                detect_s = time.monotonic() - detect_t0
                pt0 = time.monotonic()
                stats = run_with_timeout(
                    timeout_s,
                    patcher.patch_files,
                    changed,
                    earlier_commit=t["from_commit"],
                    later_commit=t["to_commit"],
                )
                patch_s = time.monotonic() - pt0
                persist_t0 = time.monotonic()
                g.save_graph(str(out_pkl))
                persist_s = time.monotonic() - persist_t0
                elapsed = time.monotonic() - total_t0
                stats = stats or {}
                sub: Dict[str, Any] = {
                    "t_s": elapsed,
                    "detect_s": detect_s,
                    "patch_s": patch_s,
                    "persist_s": persist_s,
                    "amortized_t_s": elapsed + cold_start_total / len(chain),
                    "cold_start_s": cold_start_total if i == 0 else 0.0,
                    **backend_setup,
                    "v": g.graph.vcount(),
                    "e": g.graph.ecount(),
                    "status": "ok",
                    "changed_files": sorted(_changed_paths(changed)),
                    "stats": {
                        k: v
                        for k, v in stats.items()
                        if isinstance(v, (int, float, str, bool))
                    },
                    "profile": stats.get("profile") or {},
                }
            except StrategyTimeout:
                # Graph + LSP state are now suspect — poison the rest of the
                # phase so we don't report numbers off a half-mutated graph or
                # a broken JSON-RPC pipe.
                poisoned = True
                poisoned_at = step
                log(
                    f"  {strategy} step{step}: timeout — "
                    f"poisoning remaining {len(chain) - i - 1} step(s)"
                )
                sub = {
                    "status": "timeout",
                    "t_s": time.monotonic() - total_t0,
                    "cold_start_s": cold_start_total if i == 0 else 0.0,
                    **backend_setup,
                }
            except Exception as e:
                poisoned = True
                poisoned_at = step
                log(
                    f"  {strategy} step{step}: {type(e).__name__}: {e} — "
                    f"poisoning remaining {len(chain) - i - 1} step(s)"
                )
                sub = {
                    "status": "error",
                    "error": f"{type(e).__name__}: {e}",
                    "cold_start_s": cold_start_total if i == 0 else 0.0,
                    **backend_setup,
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
            if uses_lsp:
                patcher.stop_lsp()
        except Exception:
            # Teardown is best-effort: a hung/dead LSP child should not
            # propagate out of ``finally`` and mask the real result we already
            # wrote to sub_records.
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
    real-world commit traffic is faithfully represented."""
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
        if not files_all:
            delta_lines = 0
        else:
            delta_lines, _ = diff_shortstat(repo, prev, sha, files_all)
        if not files_excl:
            delta_lines_test_excluded = 0
        else:
            delta_lines_test_excluded, _ = diff_shortstat(repo, prev, sha, files_excl)
        added, removed, modified = compute_delta_symbols(
            repo,
            prev,
            sha,
            files_all,
            language,
        )
        indexed_added, indexed_removed, indexed_modified = compute_delta_symbols(
            repo,
            prev,
            sha,
            files_excl,
            language,
        )

        if delta_lines > max_delta_lines:
            notes = "too_large"
        elif len(files_all) == 0:
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
                "delta_lines_test_excluded": delta_lines_test_excluded,
                "delta_files_test_excluded": len(files_excl),
                "delta_symbols": added + removed + modified,
                "delta_symbols_test_excluded": (
                    indexed_added + indexed_removed + indexed_modified
                ),
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
    rebuild_base: bool = False,
    chain_override: Optional[List[Dict[str, Any]]] = None,
    transition_manifest_id: Optional[str] = None,
    graph_route: str = "lsp",
    reference_timeout_s: float = DEFAULT_REFERENCE_TIMEOUT_S,
    reference_retries: int = DEFAULT_REFERENCE_RETRIES,
    lsp_request_concurrency: int = DEFAULT_LSP_REQUEST_CONCURRENCY,
    strategy_attempts: int = DEFAULT_STRATEGY_ATTEMPTS,
    experiment_config_id_value: Optional[str] = None,
    runtime_log_level: str = DEFAULT_LOG_LEVEL,
    row_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> List[Dict[str, Any]]:
    """Build one chain and run independent update strategies from its base."""
    chain = chain_override
    if chain is None:
        chain = build_chain(
            iid,
            language,
            base_commit,
            cache_root,
            n_steps,
            max_walk,
            max_delta_lines,
            log,
        )
    if not chain:
        return []
    repo = cache_root / iid / "repo"
    try:
        base_graph_pkl = prepare_base_graph(
            iid,
            language,
            base_commit,
            cache_root,
            out_root,
            timeout_s,
            log,
            graph_route=graph_route,
            reference_timeout_s=reference_timeout_s,
            reference_retries=reference_retries,
            lsp_request_concurrency=lsp_request_concurrency,
            strategy_attempts=strategy_attempts,
            force=rebuild_base,
        )
    except Exception as exc:
        log(f"[{iid}] base graph failed: {type(exc).__name__}: {exc}")
        failed_rows = [
            {
                "instance_id": iid,
                "protocol_version": PROTOCOL_VERSION,
                "experiment_config_id": experiment_config_id_value,
                "runtime_log_level": runtime_log_level,
                "transition_manifest_id": transition_manifest_id,
                "language": language,
                "step": 0,
                "base_commit": base_commit,
                "error": f"base_graph: {type(exc).__name__}: {exc}",
            }
        ]
        if row_callback is not None:
            row_callback(failed_rows[0])
        return failed_rows

    # Initial per-step record (merged with strategy sub-records below).
    step_recs: Dict[int, Dict[str, Any]] = {}
    for t in chain:
        step_recs[t["step"]] = {
            "instance_id": t["instance_id"],
            "protocol_version": PROTOCOL_VERSION,
            "experiment_config_id": experiment_config_id_value,
            "runtime_log_level": runtime_log_level,
            "graph_route": graph_route,
            "graph_schema_version": _graph_schema_version(),
            "reference_timeout_s": reference_timeout_s,
            "request_timeout_s": reference_timeout_s,
            "reference_retries": reference_retries,
            "lsp_request_concurrency": lsp_request_concurrency,
            "strategy_attempt_limit": strategy_attempts,
            "transition_manifest_id": transition_manifest_id,
            "language": t["language"],
            "base_commit": base_commit,
            "step": t["step"],
            "from_commit": t["from_commit"],
            "to_commit": t["to_commit"],
            "delta_lines": t["delta_lines"],
            "delta_files": t["delta_files"],
            "delta_lines_test_excluded": t["delta_lines_test_excluded"],
            "delta_files_test_excluded": t["delta_files_test_excluded"],
            "delta_symbols": t["delta_symbols"],
            "delta_symbols_test_excluded": t["delta_symbols_test_excluded"],
            "delta_symbols_added": t["delta_symbols_added"],
            "delta_symbols_removed": t["delta_symbols_removed"],
            "delta_symbols_modified": t["delta_symbols_modified"],
            "notes": t["notes"],
            "started": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    log(f"=== {iid} ({language}) — {len(chain)} steps ===")

    if language == "cpp":
        patcher_specs = [("backend-incremental", "symbol")]
        primary_strategy = "backend-incremental"
    else:
        modes = {
            "file-level-patch": "naive",
            "symbol-level-patch": "symbol",
        }
        patcher_specs = [(name, modes[name]) for name in patcher_phase_order(iid)]
        primary_strategy = "symbol-level-patch"

    phase_order = [name for name, _mode in patcher_specs] + ["fully-rebuild"]
    for record in step_recs.values():
        record["phase_order"] = phase_order
        record["primary_incremental_strategy"] = primary_strategy

    for phase, (strategy, mode) in enumerate(patcher_specs, start=1):
        log(f"=== {iid} phase {phase}/{len(phase_order)}: {strategy} ===")
        attempt_failures = []
        recovery_s = 0.0
        subs = {}
        for attempt in range(1, strategy_attempts + 1):
            for target in chain:
                artifact = out_root / iid / f"step{target['step']}" / f"{strategy}.pkl"
                artifact.unlink(missing_ok=True)
            clear_indexer_cache(repo, language)
            attempt_t0 = time.monotonic()
            subs = run_patcher_phase(
                strategy,
                mode,
                chain,
                base_graph_pkl,
                cache_root,
                out_root,
                timeout_s,
                lsp_profile_dir,
                log,
                f"{strategy}.pkl",
                graph_route=graph_route,
                reference_timeout_s=reference_timeout_s,
                reference_retries=reference_retries,
                lsp_request_concurrency=lsp_request_concurrency,
            )
            failed = [
                {
                    "step": target["step"],
                    "status": subs.get(target["step"], {}).get("status", "missing"),
                    "error": subs.get(target["step"], {}).get("error"),
                }
                for target in chain
                if subs.get(target["step"], {}).get("status") != "ok"
            ]
            if not failed:
                break
            attempt_wall_s = time.monotonic() - attempt_t0
            attempt_failures.append(
                {
                    "attempt": attempt,
                    "wall_s": attempt_wall_s,
                    "failures": failed,
                }
            )
            if attempt < strategy_attempts:
                recovery_s += attempt_wall_s
                log(
                    f"  {strategy}: phase attempt {attempt}/{strategy_attempts} "
                    f"failed at step{failed[0]['step']} ({failed[0]['status']}); "
                    "restarting from the base graph"
                )

        for sub in subs.values():
            sub["strategy_attempts"] = attempt
            sub["strategy_attempt_limit"] = strategy_attempts
            sub["recovery_s"] = recovery_s
            sub["attempt_failures"] = attempt_failures
            if sub.get("status") == "ok" and sub.get("amortized_t_s") is not None:
                sub["fault_inclusive_amortized_t_s"] = sub[
                    "amortized_t_s"
                ] + recovery_s / len(chain)
        for step, sub in subs.items():
            step_recs[step][strategy] = sub

    # A fresh same-commit graph is both the latency baseline and fidelity oracle.
    rebuild_phase = len(phase_order)
    log(f"=== {iid} phase {rebuild_phase}/{len(phase_order)}: fully-rebuild ===")
    out: List[Dict[str, Any]] = []
    prev_rb_pkl: Optional[Path] = base_graph_pkl
    for t in chain:
        step_dir = out_root / iid / f"step{t['step']}"
        step_dir.mkdir(parents=True, exist_ok=True)
        sub = run_rebuild_step(
            t,
            cache_root,
            step_dir,
            prev_rb_pkl,
            timeout_s,
            log,
            graph_route=graph_route,
            reference_timeout_s=reference_timeout_s,
            reference_retries=reference_retries,
            lsp_request_concurrency=lsp_request_concurrency,
            strategy_attempts=strategy_attempts,
        )
        step_recs[t["step"]]["fully-rebuild"] = sub
        rb_pkl = step_dir / "rebuild.pkl"
        if rb_pkl.exists():
            prev_rb_pkl = rb_pkl
        rec = step_recs[t["step"]]
        attach_graph_fidelity(
            rec,
            out_root,
            [name for name, _mode in patcher_specs],
            repo,
            t["to_commit"],
        )
        finalized = finalize_step_record(rec)
        out.append(finalized)
        if row_callback is not None:
            row_callback(finalized)
    return out


def finalize_step_record(rec: Dict[str, Any]) -> Dict[str, Any]:
    """Compute derived speedups + finished timestamp for one step record.

    Pure helper (no I/O) so the per-step comparison can be unit-tested with
    mocked sub-records.
    """
    rb = rec.get("fully-rebuild", {})
    primary_name = rec.get("primary_incremental_strategy", "symbol-level-patch")
    primary = rec.get(primary_name, {})
    fl = rec.get("file-level-patch", {})
    tr = rb.get("t_s") if rb.get("status") == "ok" else None
    tf = fl.get("amortized_t_s", fl.get("t_s")) if fl.get("status") == "ok" else None
    ts = (
        primary.get("amortized_t_s", primary.get("t_s"))
        if primary.get("status") == "ok"
        else None
    )
    steady_tf = fl.get("t_s") if fl.get("status") == "ok" else None
    steady_ts = primary.get("t_s") if primary.get("status") == "ok" else None
    rec["speedup_basis"] = "amortized_t_s"
    rec["speedup_vs_fully_rebuild"] = (tr / ts) if (tr and ts) else None
    rec["speedup_vs_file_level_patch"] = (tf / ts) if (tf and ts) else None
    rec["steady_speedup_vs_fully_rebuild"] = (
        (tr / steady_ts) if (tr and steady_ts) else None
    )
    rec["steady_speedup_vs_file_level_patch"] = (
        (steady_tf / steady_ts) if (steady_tf and steady_ts) else None
    )
    fidelity = primary.get("fidelity", {})
    file_fidelity = fl.get("fidelity", {})
    primary_guarded = fidelity.get("status") == "ok" and fidelity.get(
        "equivalence_guardrail_pass"
    )
    file_guarded = file_fidelity.get("status") == "ok" and file_fidelity.get(
        "equivalence_guardrail_pass"
    )
    rec["equivalence_guarded_speedup_vs_fully_rebuild"] = (
        rec["speedup_vs_fully_rebuild"] if primary_guarded else None
    )
    rec["file_level_equivalence_guarded_speedup_vs_fully_rebuild"] = (
        (tr / tf) if file_guarded and tr and tf else None
    )
    rec["equivalence_guarded_speedup_vs_file_level_patch"] = (
        rec["speedup_vs_file_level_patch"] if primary_guarded and file_guarded else None
    )
    rec["finished"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return rec


def _record_is_resumable(
    record: Dict[str, Any], experiment_config_id_value: Optional[str] = None
) -> bool:
    """Require every declared arm and its fidelity before skipping a row."""
    if record.get("protocol_version") != PROTOCOL_VERSION or not record.get("step"):
        return False
    if (
        experiment_config_id_value is not None
        and record.get("experiment_config_id") != experiment_config_id_value
    ):
        return False
    rebuild = record.get("fully-rebuild", {})
    strategies = [
        name for name in record.get("phase_order", []) if name != "fully-rebuild"
    ]
    if not strategies or rebuild.get("status") != "ok":
        return False
    return all(
        record.get(strategy, {}).get("status") == "ok"
        and record.get(strategy, {}).get("fidelity", {}).get("status") == "ok"
        for strategy in strategies
    )


# ---------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------


def parse_instances_inline(spec: str, cache_root: Path) -> List[Tuple[str, str, str]]:
    """Parse ``"iid:sha"`` or ``"iid:lang:sha"`` (comma-separated).

    Two-field form ``iid:sha``: language inferred from the cloned repo's file
    extensions. Three-field form ``iid:lang:sha`` skips inference. Drops
    entries with unknown language (with a warning).
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
    """Parse JSON: ``[{"instance_id":.., "language":.., "base_commit":..}, ...]``"""
    rows = json.loads(path.read_text())
    return [(r["instance_id"], r["language"], r["base_commit"]) for r in rows]


def _infer_language(repo: Path) -> Optional[str]:
    """Sample up to 200 tracked files to guess the dominant language.

    Uses ``os.walk`` with in-place dir pruning to skip dot-dirs (``.git``,
    ``.cache``) and known build/dependency trees so the scan doesn't traverse
    millions of objects on large repos.
    """
    if not repo.is_dir():
        return None
    counts: Dict[str, int] = {lang: 0 for lang in LANG_EXTS}
    prune = {"node_modules", "target", "build", "dist", "vendor", "__pycache__"}
    sampled = 0
    for _dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in prune]
        for name in filenames:
            suffix = Path(name).suffix
            for lang, exts in LANG_EXTS.items():
                if suffix in exts:
                    counts[lang] += 1
                    break
            sampled += 1
            if sampled > 200:
                break
        if sampled > 200:
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
        help='Comma-separated "iid:base_sha" or "iid:lang:base_sha" entries. '
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
        "--transition-manifest",
        type=Path,
        default=None,
        help="frozen maintenance manifest; overrides --instances, --config, "
        "and commit-chain discovery",
    )
    ap.add_argument(
        "--instance",
        action="append",
        default=[],
        help="run only this instance_id; repeat to select multiple instances",
    )
    ap.add_argument(
        "--max-steps",
        type=int,
        default=None,
        help="pilot-only cap on transitions loaded from a frozen manifest",
    )
    ap.add_argument(
        "--write-transition-manifest",
        type=Path,
        default=None,
        help="write the discovered chains as a frozen maintenance manifest",
    )
    ap.add_argument(
        "--manifest-only",
        action="store_true",
        help="write --write-transition-manifest and exit without benchmarking",
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
        help="max first-parent commits to consider per instance "
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
        help=f"per-strategy wall-clock cap, seconds (default {DEFAULT_TIMEOUT_S})",
    )
    ap.add_argument(
        "--lsp-request-timeout-s",
        "--lsp-reference-timeout-s",
        dest="lsp_reference_timeout_s",
        type=float,
        default=DEFAULT_REFERENCE_TIMEOUT_S,
        help="strict materialization RPC timeout "
        f"(default {DEFAULT_REFERENCE_TIMEOUT_S:g})",
    )
    ap.add_argument(
        "--lsp-reference-retries",
        type=int,
        default=DEFAULT_REFERENCE_RETRIES,
        help="retries for timed-out/cancelled reference requests "
        f"(default {DEFAULT_REFERENCE_RETRIES})",
    )
    ap.add_argument(
        "--lsp-request-concurrency",
        "--full-reference-concurrency",
        dest="lsp_request_concurrency",
        type=int,
        default=DEFAULT_LSP_REQUEST_CONCURRENCY,
        help="maximum in-flight LSP requests during reference materialization "
        f"(default {DEFAULT_LSP_REQUEST_CONCURRENCY})",
    )
    ap.add_argument(
        "--strategy-attempts",
        type=int,
        default=DEFAULT_STRATEGY_ATTEMPTS,
        help="maximum clean restarts for each base/rebuild/patch strategy "
        f"(default {DEFAULT_STRATEGY_ATTEMPTS})",
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
    ap.add_argument(
        "--rebuild-base",
        action="store_true",
        help="rebuild each schema-valid base graph even when a matching artifact exists",
    )
    ap.add_argument(
        "--graph-route",
        choices=("lsp", "active"),
        default="lsp",
        help="cold-start/base graph provider; 'lsp' gives a same-provider "
        "incremental comparison, while 'active' evaluates the production "
        "SCIP/clangd route (default: lsp)",
    )
    ap.add_argument(
        "--log-level",
        choices=("WARNING", "ERROR", "CRITICAL"),
        default=DEFAULT_LOG_LEVEL,
        help="fixed benchmark logging level (default: WARNING)",
    )
    args = ap.parse_args()
    numeric_log_level = getattr(logging, args.log_level)
    logging.getLogger().setLevel(numeric_log_level)
    from codeminer.log_utils import logging_manager

    logging_manager.rich_handler.setLevel(numeric_log_level)
    if args.strategy_attempts < 1:
        ap.error("--strategy-attempts must be positive")
    if args.lsp_reference_timeout_s <= 0:
        ap.error("--lsp-request-timeout-s must be positive")
    if args.lsp_reference_retries < 0:
        ap.error("--lsp-reference-retries must be non-negative")
    if args.lsp_request_concurrency < 1:
        ap.error("--lsp-request-concurrency must be positive")
    experiment_id = experiment_config_id(
        graph_route=args.graph_route,
        graph_schema_version=_graph_schema_version(),
        timeout_s=args.timeout_s,
        reference_timeout_s=args.lsp_reference_timeout_s,
        reference_retries=args.lsp_reference_retries,
        lsp_request_concurrency=args.lsp_request_concurrency,
        strategy_attempts=args.strategy_attempts,
        runtime_log_level=args.log_level,
    )

    # Resolve the instance list.
    frozen_chains: Dict[str, List[Dict[str, Any]]] = {}
    transition_manifest_id: Optional[str] = None
    if args.transition_manifest:
        instances, frozen_chains, transition_manifest_id = load_manifest(
            args.transition_manifest
        )
    elif args.instances:
        instances = parse_instances_inline(args.instances, args.cache_root)
    elif args.config:
        instances = parse_instances_config(args.config)
    else:
        instances = list(DEFAULT_INSTANCES)
    if not instances:
        print("error: no instances to run", file=sys.stderr)
        sys.exit(2)
    if args.instance:
        selected = set(args.instance)
        instances = [entry for entry in instances if entry[0] in selected]
        missing = selected - {entry[0] for entry in instances}
        if missing:
            ap.error(f"instances not present in workload: {sorted(missing)}")
        if frozen_chains:
            frozen_chains = {
                instance_id: frozen_chains[instance_id] for instance_id in selected
            }
    if args.max_steps is not None:
        if not frozen_chains:
            ap.error("--max-steps requires --transition-manifest")
        frozen_chains = {
            instance_id: chain[: args.max_steps]
            for instance_id, chain in frozen_chains.items()
        }
    if args.manifest_only and not args.write_transition_manifest:
        print(
            "error: --manifest-only requires --write-transition-manifest",
            file=sys.stderr,
        )
        sys.exit(2)

    if args.write_transition_manifest:
        if frozen_chains:
            chains_to_write = frozen_chains
            selection = {
                "method": "frozen-manifest-copy",
                "source_manifest_id": transition_manifest_id,
            }
        else:
            chains_to_write = {
                iid: build_chain(
                    iid,
                    language,
                    base_sha,
                    args.cache_root,
                    args.steps,
                    args.max_walk,
                    args.max_delta_lines,
                    print,
                )
                for iid, language, base_sha in instances
            }
            selection = {
                "method": "first-parent",
                "steps_per_instance": args.steps,
                "max_walk": args.max_walk,
                "max_delta_lines": args.max_delta_lines,
                "source_pathspec": "language-specific",
                "test_files_retained": True,
            }
        payload = build_manifest(instances, chains_to_write, selection=selection)
        transition_manifest_id = write_manifest(args.write_transition_manifest, payload)
        frozen_chains = chains_to_write
        print(
            f"wrote transition manifest {args.write_transition_manifest} "
            f"id={transition_manifest_id}"
        )
        if args.manifest_only:
            return

    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.out_root is None:
        args.out_root = args.output.parent / "chain_work"

    # Load already-done successful step keys for --resume. Step-0 preparation
    # errors remain retriable and therefore do not short-circuit an instance.
    done: set = set()
    done_steps: Dict[str, set] = {}
    if args.resume and args.output.exists():
        for line in args.output.open():
            try:
                r = json.loads(line)
            except Exception:
                continue
            iid = r.get("instance_id")
            step = r.get("step")
            if iid and step is not None and _record_is_resumable(r, experiment_id):
                done.add((iid, step))
                done_steps.setdefault(iid, set()).add(step)

    def log(msg: str) -> None:
        print(msg, flush=True)

    log(
        f"instances={len(instances)} steps={args.max_steps or args.steps} "
        f"timeout_s={args.timeout_s} graph_route={args.graph_route} "
        f"request_policy={args.lsp_reference_timeout_s}s/"
        f"{args.lsp_reference_retries}retry "
        f"lsp_request_concurrency={args.lsp_request_concurrency} "
        f"strategy_attempts={args.strategy_attempts} "
        f"experiment_config={experiment_id[:12]} "
        f"output={args.output}"
    )

    fh = args.output.open("a")
    try:

        def persist_row(row: Dict[str, Any]) -> None:
            key = (row.get("instance_id"), row.get("step"))
            if key in done:
                log(
                    f"[{row.get('instance_id')}] step{row.get('step')} "
                    "resume-skip (already in output)"
                )
                return
            fh.write(json.dumps(row) + "\n")
            fh.flush()
            done.add(key)
            if key[0] is not None and isinstance(key[1], int):
                done_steps.setdefault(key[0], set()).add(key[1])

        for iid, language, base_sha in instances:
            # Whole-instance short-circuit. Repos whose walk produces fewer
            # than --steps rows cannot be detected without re-walking, so they
            # fall through to the per-row resume-skip.
            if args.resume and not args.rebuild_base:
                steps_done = done_steps.get(iid, set())
                expected_steps = len(frozen_chains.get(iid, [])) or args.steps
                if all(s in steps_done for s in range(1, expected_steps + 1)):
                    log(f"[{iid}] resume-skip (all {expected_steps} steps in output)")
                    continue
            process_instance(
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
                rebuild_base=args.rebuild_base,
                chain_override=frozen_chains.get(iid) if frozen_chains else None,
                transition_manifest_id=transition_manifest_id,
                graph_route=args.graph_route,
                reference_timeout_s=args.lsp_reference_timeout_s,
                reference_retries=args.lsp_reference_retries,
                lsp_request_concurrency=args.lsp_request_concurrency,
                strategy_attempts=args.strategy_attempts,
                experiment_config_id_value=experiment_id,
                runtime_log_level=args.log_level,
                row_callback=persist_row,
            )
    finally:
        fh.close()
    log(f"done. wrote {args.output}")


if __name__ == "__main__":
    main()
